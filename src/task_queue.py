"""
后台任务队列（计划 005）

双 lane 模型：
- gpu lane: 恒为 1 worker，承载 parse/tts/precompute_embedding
- cpu lane: server.cpu_workers 个 worker，承载 mix/import

纯标准库实现（threading/queue/dataclasses/json），不引入第三方任务库。
"""
import glob
import json
import logging
import os
import random
import string
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional

from src.utils import PROJECT_ROOT, load_global_config

logger = logging.getLogger(__name__)

# 任务状态常量
STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

# 任务类型常量
TYPE_PARSE = "parse"
TYPE_TTS = "tts"
TYPE_MIX = "mix"
TYPE_PRECOMPUTE_EMBEDDING = "precompute_embedding"
TYPE_IMPORT = "import"
TYPE_ASSET_GEN = "asset_gen"

# Lane 映射
TASK_LANE = {
    TYPE_PARSE: "gpu",
    TYPE_TTS: "gpu",
    TYPE_PRECOMPUTE_EMBEDDING: "gpu",
    # asset_gen 必须走 gpu lane：SubprocessAudioGenBackend.generate_batch 内部会进
    # LlmSuspendedForGpu（停/起 llama-server，不可重入），跟 parse/tts 必须互斥。
    # 队列自己不做 GPU 仲裁——单 worker 的 gpu lane 本身就是那个互斥机制。
    TYPE_ASSET_GEN: "gpu",
    TYPE_MIX: "cpu",
    TYPE_IMPORT: "cpu",
}

# 流水线 handler 注册表（延迟导入，避免循环依赖）
TASK_HANDLERS = {}


def is_supported_type(task_type: str) -> bool:
    """任务类型是否真的有 handler 注册——`TYPE_IMPORT` 这种"声明了常量和 lane
    但没注册 handler"的类型会返回 False。供 API 层在提交前把明显的类型拼写错误
    挡在门口，而不是让它静默落进 cpu lane、异步跑起来才失败。"""
    _register_handlers()
    return task_type in TASK_HANDLERS


def _register_handlers():
    if TASK_HANDLERS:
        return
    from src.llm_parser import process_chapter_parse
    from src.tts_engine import process_chapter_tts
    from src.audio_mixer import mix_chapter
    from src.roles import precompute_embedding
    TASK_HANDLERS[TYPE_PARSE] = _handle_parse
    TASK_HANDLERS[TYPE_TTS] = _handle_tts
    TASK_HANDLERS[TYPE_MIX] = _handle_mix
    TASK_HANDLERS[TYPE_PRECOMPUTE_EMBEDDING] = _handle_precompute_embedding
    TASK_HANDLERS[TYPE_ASSET_GEN] = _handle_asset_gen


def _handle_parse(task, ctx):
    from src.llm_parser import process_chapter_parse
    from src import library
    chapter_dir = library.get_chapter_dir(task.novel_id, task.chapter_id)
    process_chapter_parse(chapter_dir, progress_cb=ctx.progress, should_cancel=ctx.should_cancel)


def _handle_tts(task, ctx):
    from src.tts_engine import process_chapter_tts
    from src import library
    chapter_dir = library.get_chapter_dir(task.novel_id, task.chapter_id)
    t0 = time.time()
    timeline_path = process_chapter_tts(chapter_dir, progress_cb=ctx.progress, should_cancel=ctx.should_cancel)
    elapsed = time.time() - t0
    _record_tts_timing(timeline_path, elapsed)


def _record_tts_timing(timeline_path: str, elapsed_seconds: float):
    """按新合成（非缓存命中）的句子数摊薄总耗时，喂给 preflight 的历史统计。
    只在真的合成过新句子时记录，避免一次几乎全命中缓存的运行把平均值拉得虚低。"""
    try:
        with open(timeline_path, "r", encoding="utf-8") as f:
            timeline = json.load(f)
        newly_synthesized = sum(1 for item in timeline.get("items", []) if not item.get("cached"))
        if newly_synthesized > 0:
            from src.preflight import update_tts_stats
            update_tts_stats(elapsed_seconds / newly_synthesized)
    except (OSError, json.JSONDecodeError, KeyError):
        pass


def _handle_mix(task, ctx):
    from src.audio_mixer import mix_chapter
    from src import library
    chapter_dir = library.get_chapter_dir(task.novel_id, task.chapter_id)
    params = task.params or {}
    # with_assets 显式为真时才关掉 voice_only；不传参数时保持 None，让
    # mix_chapter 遵循 mixing.voice_only 的配置默认值（见 audio_mixer.py）
    voice_only = False if params.get("with_assets") else None
    output_stem = params.get("output_stem") or f"{task.novel_id}_{task.chapter_id}"
    mix_chapter(chapter_dir, voice_only=voice_only, output_stem=output_stem)


def _handle_precompute_embedding(task, ctx):
    from src.roles import precompute_embedding, load_manifest
    manifest = load_manifest()
    role_id = task.params.get("role_id") if task.params else None
    if role_id:
        precompute_embedding(role_id, manifest)


def _handle_asset_gen(task, ctx):
    """素材库增量生成：不绑定小说/章节（跟 precompute_embedding 一样是全局任务），
    参数走 task.params：kinds / only / force。"""
    from src.asset_gen import generate_assets
    p = task.params or {}
    summary = generate_assets(
        kinds=p.get("kinds"),
        only=set(p["only"]) if p.get("only") else None,
        force=bool(p.get("force")),
        progress_cb=ctx.progress,
        should_cancel=ctx.should_cancel,
    )
    ctx.log(f"生成 {len(summary['generated'])} 条，占位 {len(summary['fallback'])} 条，"
            f"跳过 {len(summary['skipped'])} 条，失败 {len(summary['error'])} 条")


@dataclass
class Task:
    id: str
    type: str
    lane: str
    novel_id: str = None  # 全局任务（asset_gen / precompute_embedding）没有小说
    chapter_id: str = None
    group_id: str = None
    params: dict = None
    state: str = STATE_QUEUED
    progress: dict = None
    created_at: str = None
    started_at: str = None
    finished_at: str = None
    error: str = None
    log_path: str = None


@dataclass
class TaskContext:
    """提供给 handler 的上下文"""
    log_fn: Callable = None
    progress_fn: Callable = None
    should_cancel_fn: Callable = None

    def log(self, msg: str):
        if self.log_fn:
            self.log_fn(msg)

    def progress(self, done: int, total: int, message: str = ""):
        if self.progress_fn:
            self.progress_fn(done, total, message)

    def should_cancel(self) -> bool:
        if self.should_cancel_fn:
            return self.should_cancel_fn()
        return False


def _generate_task_id() -> str:
    ts_ms = int(time.time() * 1000)
    rand_suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"tsk_{ts_ms}_{rand_suffix}"


def _generate_group_id() -> str:
    ts_ms = int(time.time() * 1000)
    rand_suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"grp_{ts_ms}_{rand_suffix}"


def _atomic_write_json(path: str, data: dict):
    """原子写 JSON 文件"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _read_task_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


class TaskQueue:
    """双 lane 后台任务队列"""

    def __init__(self, config=None, tasks_dir=None, library_dir=None):
        self.config = config or load_global_config()
        self.tasks_dir = tasks_dir or os.path.join(PROJECT_ROOT, ".cache", "tasks")
        self.library_dir = library_dir or os.path.join(PROJECT_ROOT, "library")

        server_cfg = self.config.get("server", {})
        self.cpu_workers = server_cfg.get("cpu_workers", 2)

        self._tasks: dict = {}  # task_id -> Task
        self._cancel_tokens: dict = {}  # task_id -> threading.Event
        self._gpu_lock = threading.Lock()  # GPU lane 严格单 worker
        self._cpu_semaphore = threading.Semaphore(self.cpu_workers)
        self._listeners: list = []
        self._listener_lock = threading.Lock()

        self._gpu_queue = []
        self._cpu_queue = []
        self._queue_lock = threading.Lock()
        self._queue_condition = threading.Condition(self._queue_lock)

        self._gpu_worker = None
        self._cpu_workers = []
        self._running = False
        self._stop_event = threading.Event()

        # 事件推送节流
        self._last_progress_push: dict = {}  # task_id -> timestamp
        self._progress_throttle_sec = 0.5

    def start(self):
        """拉起 worker 线程（daemon 线程）"""
        _register_handlers()
        self._running = True
        self._stop_event.clear()

        self._gpu_worker = threading.Thread(target=self._gpu_worker_loop, daemon=True, name="gpu-worker")
        self._gpu_worker.start()

        for i in range(self.cpu_workers):
            t = threading.Thread(target=self._cpu_worker_loop, daemon=True, name=f"cpu-worker-{i}")
            self._cpu_workers.append(t)
            t.start()

        logger.info("任务队列已启动: gpu_workers=1, cpu_workers=%d", self.cpu_workers)

    def stop(self, wait=True):
        """停止 worker，等待当前任务收尾"""
        self._stop_event.set()
        with self._queue_condition:
            self._queue_condition.notify_all()

        if self._gpu_worker and self._gpu_worker.is_alive():
            self._gpu_worker.join(timeout=30)
        for t in self._cpu_workers:
            if t.is_alive():
                t.join(timeout=30)
        self._running = False
        logger.info("任务队列已停止")

    def submit(self, type: str, novel_id: str, chapter_id: str = None,
               group_id: str = None, params: dict = None) -> Task:
        task_id = _generate_task_id()
        lane = TASK_LANE.get(type, "cpu")
        now = time.strftime("%Y-%m-%d %H:%M:%S")

        task = Task(
            id=task_id, type=type, lane=lane, novel_id=novel_id,
            chapter_id=chapter_id, group_id=group_id, params=params,
            state=STATE_QUEUED, created_at=now,
        )

        self._tasks[task_id] = task
        self._persist_task(task)
        self._emit_event("task_update", {"task": asdict(task)})

        with self._queue_condition:
            if lane == "gpu":
                self._gpu_queue.append(task_id)
            else:
                self._cpu_queue.append(task_id)
            self._queue_condition.notify_all()

        return task

    def submit_global(self, type: str, params: dict = None) -> Task:
        """提交不属于任何小说/章节的全局任务（asset_gen、precompute_embedding）"""
        return self.submit(type, None, params=params)

    def submit_batch(self, type: str, novel_id: str, chapter_ids: list,
                     params: dict = None) -> tuple:
        group_id = _generate_group_id()
        tasks = []
        for cid in chapter_ids:
            task = self.submit(type, novel_id, chapter_id=cid, group_id=group_id, params=params)
            tasks.append(task)
        return group_id, tasks

    def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task:
            return False

        if task.state == STATE_QUEUED:
            task.state = STATE_CANCELLED
            task.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
            self._persist_task(task)
            self._emit_event("task_update", {"task": asdict(task)})
            return True

        if task.state == STATE_RUNNING:
            token = self._cancel_tokens.get(task_id)
            if token:
                token.set()
            return True

        return False

    def cancel_group(self, group_id: str) -> int:
        count = 0
        for task in list(self._tasks.values()):
            if task.group_id == group_id and task.state in (STATE_QUEUED, STATE_RUNNING):
                if self.cancel(task.id):
                    count += 1
        return count

    def get(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def list(self, state: str = None, group_id: str = None, limit: int = 200) -> list:
        results = []
        # 拍个快照再遍历：submit() 会从别的线程往 self._tasks 里插入新 key，
        # 直接遍历 .values() 可能撞上 "dictionary changed size during iteration"
        for task in list(self._tasks.values()):
            if state and task.state != state:
                continue
            if group_id and task.group_id != group_id:
                continue
            results.append(task)
            if len(results) >= limit:
                break
        return results

    def read_log(self, task_id: str, offset: int = 0) -> tuple:
        task = self._tasks.get(task_id)
        if not task or not task.log_path or not os.path.exists(task.log_path):
            return ("", offset)
        try:
            with open(task.log_path, "r", encoding="utf-8") as f:
                f.seek(offset)
                text = f.read()
                new_offset = f.tell()
                return (text, new_offset)
        except OSError:
            return ("", offset)

    def add_listener(self, fn: Callable):
        with self._listener_lock:
            self._listeners.append(fn)

    def remove_listener(self, fn: Callable):
        with self._listener_lock:
            self._listeners = [l for l in self._listeners if l is not fn]

    def recover_on_startup(self) -> int:
        """扫描所有 state==running 的任务，一律改写为 failed"""
        count = 0
        for task in list(self._tasks.values()):
            if task.state == STATE_RUNNING:
                task.state = STATE_FAILED
                task.error = "服务重启中断"
                task.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
                self._persist_task(task)
                self._emit_event("task_update", {"task": asdict(task)})
                count += 1
        return count

    def cleanup_old_tasks(self, max_age_days: int = 7):
        """清理超过 max_age_days 天的任务记录"""
        cutoff = time.time() - max_age_days * 86400
        to_remove = []
        for task_id, task in list(self._tasks.items()):
            if task.state not in (STATE_SUCCEEDED, STATE_FAILED, STATE_CANCELLED):
                continue
            try:
                created = time.mktime(time.strptime(task.created_at, "%Y-%m-%d %H:%M:%S"))
                if created < cutoff:
                    to_remove.append(task_id)
            except (ValueError, TypeError):
                continue

        for task_id in to_remove:
            task = self._tasks.pop(task_id, None)
            if task and task.log_path and os.path.exists(task.log_path):
                try:
                    os.remove(task.log_path)
                except OSError:
                    pass
            state_path = os.path.join(self.tasks_dir, f"{task_id}.json")
            if os.path.exists(state_path):
                try:
                    os.remove(state_path)
                except OSError:
                    pass

    def _load_tasks_from_disk(self):
        """启动时从磁盘加载任务"""
        if not os.path.isdir(self.tasks_dir):
            return
        for path in glob.glob(os.path.join(self.tasks_dir, "*.json")):
            data = _read_task_file(path)
            if not data:
                continue
            task = Task(**{k: v for k, v in data.items() if k in Task.__dataclass_fields__})
            self._tasks[task.id] = task

    # ----------------------------------------------------------------
    # 内部方法
    # ----------------------------------------------------------------

    def _persist_task(self, task: Task):
        path = os.path.join(self.tasks_dir, f"{task.id}.json")
        _atomic_write_json(path, asdict(task))

    def _emit_event(self, event_type: str, data: dict):
        with self._listener_lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn({"type": event_type, "data": data, "timestamp": time.time()})
            except Exception:
                logger.exception("事件监听器异常")

    def _should_throttle_progress(self, task_id: str) -> bool:
        now = time.time()
        last = self._last_progress_push.get(task_id, 0)
        if now - last < self._progress_throttle_sec:
            return True
        self._last_progress_push[task_id] = now
        return False

    def _gpu_worker_loop(self):
        while not self._stop_event.is_set():
            task_id = None
            with self._queue_condition:
                while not self._gpu_queue and not self._stop_event.is_set():
                    self._queue_condition.wait(timeout=1.0)
                if self._gpu_queue:
                    task_id = self._gpu_queue.pop(0)

            if task_id:
                self._execute_task(task_id)

    def _cpu_worker_loop(self):
        while not self._stop_event.is_set():
            task_id = None
            with self._queue_condition:
                while not self._cpu_queue and not self._stop_event.is_set():
                    self._queue_condition.wait(timeout=1.0)
                if self._cpu_queue:
                    task_id = self._cpu_queue.pop(0)

            if task_id:
                self._execute_task(task_id)

    def _execute_task(self, task_id: str):
        task = self._tasks.get(task_id)
        if not task or task.state == STATE_CANCELLED:
            return

        # GPU lane 严格单 worker
        if task.lane == "gpu":
            self._gpu_lock.acquire()
        else:
            self._cpu_semaphore.acquire()

        try:
            if task.state == STATE_CANCELLED:
                return

            task.state = STATE_RUNNING
            task.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
            self._persist_task(task)
            self._emit_event("task_update", {"task": asdict(task)})

            # 创建取消 token
            cancel_token = threading.Event()
            self._cancel_tokens[task.id] = cancel_token

            # 创建日志文件
            log_dir = os.path.join(self.tasks_dir)
            os.makedirs(log_dir, exist_ok=True)
            task.log_path = os.path.join(log_dir, f"{task.id}.log")

            handler = TASK_HANDLERS.get(task.type)
            if not handler:
                task.state = STATE_FAILED
                task.error = f"未知任务类型: {task.type}"
                task.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
                self._persist_task(task)
                self._emit_event("task_update", {"task": asdict(task)})
                return

            ctx = TaskContext(
                log_fn=lambda msg: self._task_log(task, msg),
                progress_fn=lambda done, total, msg: self._task_progress(task, done, total, msg),
                should_cancel_fn=cancel_token.is_set,
            )

            handler(task, ctx)

            if cancel_token.is_set():
                task.state = STATE_CANCELLED
            else:
                task.state = STATE_SUCCEEDED
            task.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")

        except Exception as e:
            from src.pipeline_errors import TaskCancelled
            if isinstance(e, TaskCancelled):
                task.state = STATE_CANCELLED
                task.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
                self._task_log(task, f"任务被取消: {e}")
            else:
                task.state = STATE_FAILED
                task.error = str(e)
                task.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
                self._task_log(task, f"任务异常: {e}")
                logger.exception("任务 %s 执行异常", task_id)
        finally:
            self._cancel_tokens.pop(task.id, None)
            self._persist_task(task)
            self._emit_event("task_update", {"task": asdict(task)})

            if task.lane == "gpu":
                self._gpu_lock.release()
            else:
                self._cpu_semaphore.release()

    def _task_log(self, task: Task, msg: str):
        if not task.log_path:
            return
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        try:
            with open(task.log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass

    def _task_progress(self, task: Task, done: int, total: int, message: str = ""):
        task.progress = {"done": done, "total": total, "message": message}
        if not self._should_throttle_progress(task.id):
            self._emit_event("task_update", {"task": asdict(task)})
