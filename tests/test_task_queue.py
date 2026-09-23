"""
测试 src/task_queue.py 后台任务队列

全部用 Mock 后端和 tmp_path，不碰真实 GPU。
"""
import json
import os
import threading
import time

import pytest
from src import task_queue
from src.task_queue import TaskQueue, Task, _generate_task_id


@pytest.fixture
def isolated_queue(tmp_path, monkeypatch):
    """创建隔离的任务队列实例"""
    tasks_dir = str(tmp_path / "tasks")
    library_dir = str(tmp_path / "library")
    os.makedirs(tasks_dir, exist_ok=True)
    os.makedirs(library_dir, exist_ok=True)

    monkeypatch.setattr(task_queue, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(task_queue, "load_global_config", lambda: {
        "server": {"cpu_workers": 2}
    })

    q = TaskQueue(config={"server": {"cpu_workers": 2}}, tasks_dir=tasks_dir, library_dir=library_dir)
    return q


def _make_mock_handler(delay=0.0, should_fail=False):
    """创建一个 mock handler 用于测试"""
    def handler(task, ctx):
        if delay > 0:
            time.sleep(delay)
        if should_fail:
            raise RuntimeError("模拟失败")
        ctx.log("任务完成")
    return handler


class TestTaskDataclass:
    def test_task_defaults(self):
        task = Task(id="tsk_1", type="parse", lane="gpu", novel_id="nv_test")
        assert task.state == "queued"
        assert task.chapter_id is None
        assert task.group_id is None

    def test_task_to_dict(self):
        task = Task(id="tsk_1", type="parse", lane="gpu", novel_id="nv_test")
        d = task.__dict__
        assert d["id"] == "tsk_1"
        assert d["state"] == "queued"


class TestSubmit:
    def test_submit_single_task(self, isolated_queue):
        task = isolated_queue.submit("parse", "nv_test", chapter_id="ch_0001")
        assert task.id.startswith("tsk_")
        assert task.state == "queued"
        assert task.type == "parse"
        assert task.lane == "gpu"
        assert task.novel_id == "nv_test"
        assert task.chapter_id == "ch_0001"

    def test_submit_persists_to_disk(self, isolated_queue):
        task = isolated_queue.submit("parse", "nv_test")
        path = os.path.join(isolated_queue.tasks_dir, f"{task.id}.json")
        assert os.path.exists(path)
        with open(path) as f:
            data = json.load(f)
        assert data["id"] == task.id

    def test_submit_batch_returns_group_id(self, isolated_queue):
        group_id, tasks = isolated_queue.submit_batch("tts", "nv_test", ["ch_0001", "ch_0002", "ch_0003"])
        assert group_id.startswith("grp_")
        assert len(tasks) == 3
        assert all(t.group_id == group_id for t in tasks)

    def test_lane_assignment(self, isolated_queue):
        parse_task = isolated_queue.submit("parse", "nv_test")
        mix_task = isolated_queue.submit("mix", "nv_test")
        assert parse_task.lane == "gpu"
        assert mix_task.lane == "cpu"


class TestCancel:
    def test_cancel_queued_task(self, isolated_queue):
        task = isolated_queue.submit("parse", "nv_test")
        result = isolated_queue.cancel(task.id)
        assert result is True
        assert isolated_queue.get(task.id).state == "cancelled"

    def test_cancel_running_task(self, isolated_queue, monkeypatch):
        cancel_called = threading.Event()

        def mock_handler(task, ctx):
            # 等待取消信号
            while not ctx.should_cancel():
                time.sleep(0.01)
            cancel_called.set()

        monkeypatch.setitem(task_queue.TASK_HANDLERS, "parse", mock_handler)

        task = isolated_queue.submit("parse", "nv_test")
        isolated_queue.start()
        time.sleep(0.1)  # 等任务开始执行

        result = isolated_queue.cancel(task.id)
        assert result is True
        cancel_called.wait(timeout=5.0)

        isolated_queue.stop()
        # 任务最终状态应该是 cancelled
        final_task = isolated_queue.get(task.id)
        assert final_task.state == "cancelled"

    def test_cancel_nonexistent_task(self, isolated_queue):
        result = isolated_queue.cancel("tsk_nonexistent")
        assert result is False

    def test_cancel_group(self, isolated_queue):
        group_id, tasks = isolated_queue.submit_batch("tts", "nv_test", ["ch_0001", "ch_0002"])
        count = isolated_queue.cancel_group(group_id)
        assert count == 2
        for t in tasks:
            assert isolated_queue.get(t.id).state == "cancelled"


class TestExecuteTask:
    def test_execute_succeeds(self, isolated_queue, monkeypatch):
        monkeypatch.setitem(task_queue.TASK_HANDLERS, "parse", _make_mock_handler())
        task = isolated_queue.submit("parse", "nv_test")

        isolated_queue.start()
        time.sleep(0.5)
        isolated_queue.stop()

        final = isolated_queue.get(task.id)
        assert final.state == "succeeded"
        assert final.finished_at is not None
        assert final.log_path and os.path.exists(final.log_path)

    def test_execute_failure(self, isolated_queue, monkeypatch):
        monkeypatch.setitem(task_queue.TASK_HANDLERS, "parse", _make_mock_handler(should_fail=True))
        task = isolated_queue.submit("parse", "nv_test")

        isolated_queue.start()
        time.sleep(0.5)
        isolated_queue.stop()

        final = isolated_queue.get(task.id)
        assert final.state == "failed"
        assert "模拟失败" in final.error

    def test_failure_does_not_block_other_tasks(self, isolated_queue, monkeypatch):
        call_count = [0]

        def counting_handler(task, ctx):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("第一个任务失败")

        monkeypatch.setitem(task_queue.TASK_HANDLERS, "parse", counting_handler)

        t1 = isolated_queue.submit("parse", "nv_test")
        t2 = isolated_queue.submit("parse", "nv_test")

        isolated_queue.start()
        time.sleep(1.0)
        isolated_queue.stop()

        assert isolated_queue.get(t1.id).state == "failed"
        assert isolated_queue.get(t2.id).state == "succeeded"

    def test_cancelled_task_not_executed(self, isolated_queue, monkeypatch):
        handler_called = threading.Event()

        def slow_handler(task, ctx):
            handler_called.set()
            time.sleep(10)

        monkeypatch.setitem(task_queue.TASK_HANDLERS, "parse", slow_handler)

        task = isolated_queue.submit("parse", "nv_test")
        isolated_queue.cancel(task.id)  # 先取消

        isolated_queue.start()
        time.sleep(0.5)
        isolated_queue.stop()

        assert not handler_called.is_set()
        assert isolated_queue.get(task.id).state == "cancelled"


class TestGPULane:
    def test_gpu_lane_single_worker(self, isolated_queue, monkeypatch):
        """GPU lane 同一时刻只有一个任务在 running"""
        running_count = [0]
        max_concurrent = [0]
        lock = threading.Lock()

        def slow_handler(task, ctx):
            with lock:
                running_count[0] += 1
                max_concurrent[0] = max(max_concurrent[0], running_count[0])
            time.sleep(0.5)
            with lock:
                running_count[0] -= 1

        monkeypatch.setitem(task_queue.TASK_HANDLERS, "parse", slow_handler)

        for _ in range(3):
            isolated_queue.submit("parse", "nv_test")

        isolated_queue.start()
        time.sleep(2.0)
        isolated_queue.stop()

        assert max_concurrent[0] == 1


class TestCPULane:
    def test_cpu_lane_concurrent(self, isolated_queue, monkeypatch):
        """CPU lane 能并发"""
        running_count = [0]
        max_concurrent = [0]
        lock = threading.Lock()

        def slow_handler(task, ctx):
            with lock:
                running_count[0] += 1
                max_concurrent[0] = max(max_concurrent[0], running_count[0])
            time.sleep(0.3)
            with lock:
                running_count[0] -= 1

        monkeypatch.setitem(task_queue.TASK_HANDLERS, "mix", slow_handler)

        for _ in range(4):
            isolated_queue.submit("mix", "nv_test")

        isolated_queue.start()
        time.sleep(2.0)
        isolated_queue.stop()

        assert max_concurrent[0] >= 2


class TestRecoverOnStartup:
    def test_recover_running_tasks(self, isolated_queue):
        # 手动创建一个 running 状态的任务
        task = isolated_queue.submit("parse", "nv_test")
        task.state = "running"
        task.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        isolated_queue._persist_task(task)
        isolated_queue._tasks[task.id] = task

        count = isolated_queue.recover_on_startup()
        assert count == 1
        assert isolated_queue.get(task.id).state == "failed"
        assert "服务重启中断" in isolated_queue.get(task.id).error


class TestListener:
    def test_listener_receives_events(self, isolated_queue, monkeypatch):
        events = []
        isolated_queue.add_listener(lambda e: events.append(e))

        monkeypatch.setitem(task_queue.TASK_HANDLERS, "parse", _make_mock_handler())
        task = isolated_queue.submit("parse", "nv_test")

        isolated_queue.start()
        time.sleep(0.5)
        isolated_queue.stop()

        event_types = [e["type"] for e in events]
        assert "task_update" in event_types
        # 至少有 queued -> running -> succeeded 的事件
        task_states = [e["data"]["task"]["state"] for e in events if e["type"] == "task_update"]
        assert "queued" in task_states
        assert "succeeded" in task_states

    def test_remove_listener(self, isolated_queue):
        events = []
        fn = lambda e: events.append(e)
        isolated_queue.add_listener(fn)
        isolated_queue.remove_listener(fn)

        isolated_queue.submit("parse", "nv_test")
        assert len(events) == 0


class TestReadLog:
    def test_read_log_empty(self, isolated_queue):
        task = isolated_queue.submit("parse", "nv_test")
        text, offset = isolated_queue.read_log(task.id)
        assert text == ""

    def test_read_log_with_content(self, isolated_queue):
        task = isolated_queue.submit("parse", "nv_test")
        # Set log_path manually since submit doesn't set it
        task.log_path = os.path.join(isolated_queue.tasks_dir, f"{task.id}.log")
        with open(task.log_path, "w") as f:
            f.write("[10:00:00] 开始\n[10:00:01] 完成\n")
        text, offset = isolated_queue.read_log(task.id)
        assert "开始" in text
        assert "完成" in text


class TestHandleMix:
    """_handle_mix 是否正确把 task.params 转成 mix_chapter 的 voice_only/
    output_stem 参数——不直接跑真实 mix_chapter（需要真实 timeline+音频文件），
    monkeypatch 成一个记录调用参数的桩函数。"""

    def _make_task(self, novel_id="nv1", chapter_id="ch_0001", params=None):
        return Task(id="t1", type="mix", lane="cpu", novel_id=novel_id,
                    chapter_id=chapter_id, params=params)

    def _patch(self, monkeypatch, tmp_path):
        """_handle_mix 里是函数内 `from src import library`/`from src.audio_mixer
        import mix_chapter`，要打到真实模块对象上，monkeypatch 局部导入绑定的
        名字（比如 task_queue.library）不会生效——那只是在 task_queue 模块的
        命名空间里添了个无关属性，函数体内重新 import 时还是会拿到原始模块。"""
        calls = []
        import src.library as library_mod
        import src.audio_mixer as am
        monkeypatch.setattr(library_mod, "get_chapter_dir",
                            lambda nid, cid: str(tmp_path / nid / cid))
        monkeypatch.setattr(am, "mix_chapter",
                            lambda chapter_dir, **kw: calls.append(kw) or "out.mp3")
        return calls

    def test_with_assets_true_disables_voice_only(self, monkeypatch, tmp_path):
        calls = self._patch(monkeypatch, tmp_path)

        task = self._make_task(params={"with_assets": True})
        ctx = task_queue.TaskContext()
        task_queue._handle_mix(task, ctx)

        assert calls[0]["voice_only"] is False
        assert calls[0]["output_stem"] == "nv1_ch_0001"

    def test_without_params_keeps_voice_only_none(self, monkeypatch, tmp_path):
        """没传 params 时 voice_only 必须是 None（遵循配置默认值），不是 True——
        传 True 会覆盖掉用户在设置页里打开的 mixing.voice_only=False。"""
        calls = self._patch(monkeypatch, tmp_path)

        task = self._make_task(params=None)
        ctx = task_queue.TaskContext()
        task_queue._handle_mix(task, ctx)

        assert calls[0]["voice_only"] is None

    def test_explicit_output_stem_overrides_default(self, monkeypatch, tmp_path):
        calls = self._patch(monkeypatch, tmp_path)

        task = self._make_task(params={"output_stem": "custom_name"})
        ctx = task_queue.TaskContext()
        task_queue._handle_mix(task, ctx)

        assert calls[0]["output_stem"] == "custom_name"


class TestPrecomputeEmbeddingHandler:
    """回归：以前 handler 丢弃 precompute_embedding 的 {"ok","error"} 返回值，
    环境未就绪/超时/子进程失败全被记成任务「成功」。"""

    @pytest.fixture
    def arbiter(self, monkeypatch):
        events = []

        class FakeArbiter:
            def __init__(self, config=None):
                pass

            def __enter__(self):
                events.append("enter")

            def __exit__(self, *exc):
                events.append("exit")
                return False

        import src.tools.gpu_arbiter as ga
        monkeypatch.setattr(ga, "LlmSuspendedForGpu", FakeArbiter)
        return events

    def _patch_roles(self, monkeypatch, precondition=None, result=None):
        import src.roles as roles_mod
        monkeypatch.setattr(roles_mod, "load_manifest", lambda *a, **k: {"roles": {"r1": {}}})
        monkeypatch.setattr(roles_mod, "embedding_precondition_error", lambda *a, **k: precondition)
        monkeypatch.setattr(roles_mod, "precompute_embedding",
                            lambda *a, **k: result if result is not None else {"ok": True, "error": None})

    def _task(self, params):
        return Task(id="t", type="precompute_embedding", lane="gpu", params=params)

    def test_failed_result_makes_the_task_fail_with_the_reason(self, monkeypatch, arbiter):
        self._patch_roles(monkeypatch, result={"ok": False, "error": "预计算超时"})
        logs = []
        ctx = task_queue.TaskContext(log_fn=logs.append)
        with pytest.raises(RuntimeError, match="预计算超时"):
            task_queue._handle_precompute_embedding(self._task({"role_id": "r1"}), ctx)
        assert arbiter == ["enter", "exit"]  # 失败也要走完换手的退出（恢复 llama-server）
        assert any("预计算超时" in l for l in logs)

    def test_success_runs_inside_gpu_handoff(self, monkeypatch, arbiter):
        self._patch_roles(monkeypatch)
        task_queue._handle_precompute_embedding(self._task({"role_id": "r1"}), task_queue.TaskContext())
        assert arbiter == ["enter", "exit"]

    def test_env_not_ready_fails_before_touching_the_gpu(self, monkeypatch, arbiter):
        """环境没就绪就别去停 llama-server——为注定失败的任务白停一次 LLM 服务"""
        self._patch_roles(monkeypatch, precondition="IndexTTS 推理环境未就绪（venv/权重缺失）")
        with pytest.raises(RuntimeError, match="未就绪"):
            task_queue._handle_precompute_embedding(self._task({"role_id": "r1"}), task_queue.TaskContext())
        assert arbiter == []

    @pytest.mark.parametrize("params", [None, {}, {"role_id": ""}])
    def test_missing_role_id_fails_instead_of_silent_noop(self, monkeypatch, arbiter, params):
        self._patch_roles(monkeypatch)
        with pytest.raises(ValueError, match="role_id"):
            task_queue._handle_precompute_embedding(self._task(params), task_queue.TaskContext())
        assert arbiter == []

    def test_end_to_end_through_the_queue_state_is_failed_with_error(self, isolated_queue, monkeypatch, arbiter):
        self._patch_roles(monkeypatch, result={"ok": False, "error": "IndexTTS 推理环境未就绪"})
        isolated_queue.start()
        try:
            task = isolated_queue.submit_global("precompute_embedding", params={"role_id": "r1"})
            for _ in range(100):
                if isolated_queue.get(task.id).state in ("succeeded", "failed", "cancelled"):
                    break
                time.sleep(0.05)
        finally:
            isolated_queue.stop()
        done = isolated_queue.get(task.id)
        assert done.state == "failed"
        assert "未就绪" in done.error
