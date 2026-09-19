"""
TTS 推理模块 (支持基于 MD5 的哈希增量合成)

架构：TTSBackend 抽象出两种实现——
- MockTTSBackend：生成占位波形，无外部依赖，供离线自检 (`cli.py test`) 使用。
- IndexTTSBackend：通过子进程调用独立部署的 IndexTTS-2.5 推理环境（见
  tools/indextts_infer.py 与 docs/indextts_setup.md），按角色参考音频克隆音色、
  按 emotion 映射情感向量、按角色 speed 配置换算语速。GPU 显存与 llama-server
  的 Qwen 模型互斥占用，见 tools/gpu_arbiter.py 的调度逻辑。

无论使用哪种后端，MD5(speaker + text + emotion) 增量缓存逻辑保持不变；
IndexTTS 合成失败时自动降级为 Mock 占位音，保证管线不中断（并记录警告）。
"""
import os
import wave
import struct
import json
import logging
import subprocess
import tempfile

from src.utils import calculate_md5, update_chapter_status, load_global_config, resolve_path
from src import roles as roles_mod
from src.pipeline_errors import TaskCancelled
from src.killable_proc import terminate_process_group

logger = logging.getLogger(__name__)

# IndexTTS-2.5 emo_vector 维度顺序（官方 API）：
# [happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]
EMOTION_TO_VECTOR = {
    "neutral":   [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    "happy":     [0.8, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    "angry":     [0.0, 0.8, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    "sad":       [0.0, 0.0, 0.8, 0.0, 0.0, 0.0, 0.0, 0.0],
    "serious":   [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.3],
    "afraid":    [0.0, 0.0, 0.0, 0.8, 0.0, 0.0, 0.0, 0.0],
    "surprised": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.8, 0.0],
    "calm":      [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.8],
}


def speed_to_duration_factor(speed: float) -> float:
    """角色 speed（>1 更快）换算为 IndexTTS duration_factor（>1 更慢），并夹取到官方允许范围"""
    if not speed or speed <= 0:
        return 1.0
    factor = 1.0 / speed
    return max(0.5, min(2.0, factor))


# --------------------------------------------------------------------------
# TTS 后端
# --------------------------------------------------------------------------

class MockTTSBackend:
    """占位波形合成，无外部依赖，供离线自检使用"""

    name = "mock"

    def is_available(self) -> bool:
        return True

    def synthesize(self, text: str, role_cfg: dict, emotion: str, output_wav_path: str, sample_rate: int) -> bool:
        duration_sec = max(1.0, len(text) * 0.2)
        num_samples = int(sample_rate * duration_sec)
        os.makedirs(os.path.dirname(output_wav_path), exist_ok=True)
        with wave.open(output_wav_path, "w") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(sample_rate)
            frames = bytearray()
            for i in range(num_samples):
                val = int(500 * (1 if (i // 100) % 2 == 0 else -1))
                frames.extend(struct.pack("<h", val))
            f.writeframes(frames)
        return True

    def synthesize_batch(self, jobs: list) -> dict:
        """逐条调用 synthesize()，Mock 引擎无需批量优化"""
        results = {}
        for job in jobs:
            ok = self.synthesize(job["text"], job["role_cfg"], job["emotion"], job["out"], job["sample_rate"])
            results[job["id"]] = ok
        return results


# 真实引擎没合成成功、被 Mock 占位音顶替的音频，旁边写一个 `<md5>.wav.fallback` 标记。
# 缓存探针遇到带标记的 wav 视为「未命中」，下次运行会重试真实引擎，而不是让占位噪音
# 以合法 md5 名字永久命中缓存（之前正是这样：占位音写进 audio_cache/<真实 md5>.wav，
# 章节还被标成 tts_completed，永远不会再有机会换成真声）。
FALLBACK_MARKER_SUFFIX = ".fallback"


def _marker_path(wav_path: str) -> str:
    return wav_path + FALLBACK_MARKER_SUFFIX


def _mark_fallback(wav_path: str) -> None:
    with open(_marker_path(wav_path), "w", encoding="utf-8") as f:
        f.write("占位音：真实引擎未能合成这一句，下次运行会重试\n")


def _clear_fallback_marker(wav_path: str) -> None:
    try:
        os.remove(_marker_path(wav_path))
    except FileNotFoundError:
        pass


def _is_cached(wav_path: str) -> bool:
    return os.path.exists(wav_path) and not os.path.exists(_marker_path(wav_path))


def _delete_unfinished_outputs(jobs: list, results: dict) -> None:
    """删掉真实引擎没报告成功的 job 的输出文件。

    子进程把 wav 直接写到最终路径（audio_cache/<md5>.wav），被杀/崩溃时会在合法 md5 名下
    留下截断甚至 0 字节的文件；缓存探针只判断「文件存在」，会把它当作已合成，之后
    wave.open 读它就炸、整章每次都失败。子进程每完成一个 job 就原子重写 result.json，
    所以「哪些 job 真的完成了」是精确可知的——没完成的一律删掉。"""
    for job in jobs:
        if not results.get(job["id"]):
            try:
                os.remove(job["out"])
            except FileNotFoundError:
                pass


class IndexTTSBackend:
    """
    通过子进程调用独立部署的 IndexTTS-2.5 推理环境（见 docs/indextts_setup.md）。
    该环境使用独立 venv（Python 3.11 + ROCm torch），与本项目主 venv 隔离，
    通过 python_bin + infer_script 两个可执行路径对接。

    模型加载（GPT/语义编解码器/s2mel/BigVGAN 等）耗时数十秒，逐句起子进程会被
    加载开销拖垮，因此本后端始终走批量接口：一次子进程调用加载模型一次，
    循环合成整批任务。
    """

    name = "index_tts_2_5"
    _current_proc = None  # 类属性默认值：没跑过 synthesize_batch 时 terminate_current() 也不会 AttributeError

    def __init__(self, config: dict):
        self.config = config
        tts_cfg = config.get("tts", {}).get("index_tts", {})
        self.python_bin = resolve_path(tts_cfg.get("python_bin", "tools/indextts_env/bin/python"))
        self.infer_script = resolve_path(tts_cfg.get("infer_script", "tools/indextts_infer.py"))
        self.repo_dir = resolve_path(tts_cfg.get("repo_dir", "tools/indextts_repo"))
        self.checkpoints_dir = resolve_path(tts_cfg.get("checkpoints_dir", "/srv/unsafe/models/tts/IndexTTS-2.5"))
        self.timeout = tts_cfg.get("timeout_sec", 1800)

    def is_available(self) -> bool:
        return (
            os.path.exists(self.python_bin)
            and os.path.exists(self.infer_script)
            and os.path.isdir(self.repo_dir)
            and os.path.isdir(self.checkpoints_dir)
        )

    def synthesize(self, text: str, role_cfg: dict, emotion: str, output_wav_path: str, sample_rate: int) -> bool:
        """单句合成：内部仍走批量接口（批量大小为 1），保持接口一致性供直接调用/测试使用"""
        job = {
            "id": "single", "text": text, "role_cfg": role_cfg, "emotion": emotion,
            "out": output_wav_path, "sample_rate": sample_rate,
        }
        return self.synthesize_batch([job]).get("single", False)

    def synthesize_batch(self, jobs: list) -> dict:
        """
        jobs: [{"id", "text", "role_cfg", "emotion", "out", "sample_rate"}, ...]
        返回 {job_id: bool}，某条任务子进程侧异常不影响其余任务的结果。
        """
        if not jobs:
            return {}

        infer_jobs = []
        for job in jobs:
            role_cfg = job["role_cfg"]
            infer_jobs.append({
                "id": job["id"],
                "text": job["text"],
                # indextts_infer.py 子进程会 os.chdir() 到 repo 目录（IndexTTS2 内部把
                # HF_HUB_CACHE 硬编码为相对路径，见 docs/indextts_setup.md），因此这里
                # 传给子进程的路径必须先转绝对路径，否则 ref_audio/out 会被错误解析到
                # repo 目录下
                "ref_audio": os.path.abspath(role_cfg["reference_audio"]),
                "lang": "ZH",
                "emo_vector": EMOTION_TO_VECTOR.get(job["emotion"], EMOTION_TO_VECTOR["neutral"]),
                "duration_factor": speed_to_duration_factor(role_cfg.get("speed", 1.0)),
                "out": os.path.abspath(job["out"]),
            })

        with tempfile.TemporaryDirectory(prefix="indextts_batch_") as tmp_dir:
            jobs_file = os.path.join(tmp_dir, "jobs.json")
            result_file = os.path.join(tmp_dir, "result.json")
            with open(jobs_file, "w", encoding="utf-8") as f:
                json.dump(infer_jobs, f, ensure_ascii=False)

            cmd = [
                self.python_bin, self.infer_script,
                "--repo-dir", self.repo_dir,
                "--checkpoints-dir", self.checkpoints_dir,
                "--jobs-file", jobs_file,
                "--result-file", result_file,
            ]
            from tools.gpu_arbiter import LlmSuspendedForTts  # 延迟导入，避免无网络场景下的循环依赖

            self._current_proc = None
            try:
                with LlmSuspendedForTts(self.config):
                    proc = subprocess.Popen(
                        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        start_new_session=True,  # 独立进程组，取消时可整体 kill
                    )
                    self._current_proc = proc
                    try:
                        out, err = proc.communicate(timeout=self.timeout)
                        if proc.returncode != 0:
                            logger.warning("[IndexTTSBackend] 批量合成子进程返回非零: %s",
                                           (err or b"").decode(errors="replace")[-1000:])
                    except subprocess.TimeoutExpired:
                        logger.warning("[IndexTTSBackend] 批量合成超时（%d 条任务）", len(jobs))
                        # 必须在 with 块**内部**杀：LlmSuspendedForTts.__exit__ 会重启 llama-server，
                        # 若先退出 with 再杀，llama-server 会在这个子进程还占着显存时被拉起来
                        self.terminate_current()
            finally:
                self._current_proc = None

            results = {}
            if os.path.exists(result_file):
                with open(result_file, "r", encoding="utf-8") as f:
                    raw_results = json.load(f)
                for job in jobs:
                    job_result = raw_results.get(job["id"])
                    ok = bool(job_result and job_result.get("ok")) and os.path.exists(job["out"])
                    if job_result and not job_result.get("ok"):
                        logger.warning("[IndexTTSBackend] 句段 %s 合成失败: %s", job["id"], job_result.get("error", "")[:500])
                    results[job["id"]] = ok
            else:
                # 子进程连一条结果都没写出（如加载模型阶段就崩溃），全部标记失败
                for job in jobs:
                    results[job["id"]] = False
            _delete_unfinished_outputs(jobs, results)
            return results

    def terminate_current(self):
        """取消任务时由 TaskQueue 调用。先 terminate 整个进程组，宽限 5 秒再 kill。
        用 os.killpg 而不是 proc.terminate()——start_new_session=True 起的是独立进程组，
        IndexTTS 内部可能还有子进程。"""
        proc = self._current_proc
        if proc is None:
            return
        terminate_process_group(proc)  # SIGTERM → 宽限 5 秒 → SIGKILL → 收尸；保证不会 killpg 到服务器自己
        self._current_proc = None


def build_tts_backend(config: dict = None):
    """按配置选择 TTS 引擎；IndexTTS 环境未就绪时自动回退 Mock，保证管线不中断"""
    if config is None:
        config = load_global_config()
    engine_name = config.get("tts", {}).get("engine", "")

    if "index-tts" in engine_name.lower() or "indextts" in engine_name.lower():
        backend = IndexTTSBackend(config)
        if backend.is_available():
            return backend
        logger.warning(
            "配置要求使用 IndexTTS-2.5 (%s)，但推理环境未就绪（venv/权重缺失），回退到 Mock 占位合成", engine_name
        )
    return MockTTSBackend()


# --------------------------------------------------------------------------
# 增量合成主流程
# --------------------------------------------------------------------------

def generate_tts_incremental(chapter_dir: str, script_final_data: list, sample_rate: int = 24000,
                              backend=None, roles_dir: str = None,
                              progress_cb=None, should_cancel=None, chunk_size=None, config=None) -> dict:
    """
    遍历 JSON 的每一句，使用 MD5(speaker + text + emotion) 计算哈希值作为文件名。
    检查 audio_cache/ 下是否已有该文件。如果有，跳过合成；如果没有，调用 TTS 后端生成。
    返回累加的时间线数据 timeline.json 的结构。
    """
    unbound = [seg.get("seg_id") for seg in script_final_data if not seg.get("speaker")]
    if unbound:
        shown = unbound[:20]
        more = f"（共 {len(unbound)} 个，只列前 20 个）" if len(unbound) > 20 else ""
        raise ValueError(
            f"以下分块尚未绑定角色，无法合成{more}：seg_id={shown}。"
            "请先在配音工作台为它们指派角色。"
        )

    if backend is None:
        backend = build_tts_backend()
    if config is None:
        config = load_global_config()

    audio_cache_dir = os.path.join(chapter_dir, "audio_cache")
    os.makedirs(audio_cache_dir, exist_ok=True)

    manifest = roles_mod.load_manifest(roles_dir)
    used_fallback = False
    # tts.fallback_on_failure（默认 true = 沿用现状：真实引擎失败的句子用 Mock 占位保证整章不中断）。
    # 设为 false 是严格模式：有句子合成失败就让整章失败，不产出带占位音的成品。
    allow_fallback = (config.get("tts") or {}).get("fallback_on_failure", True)

    def _settle_batch(jobs, batch_results):
        """处理一批合成结果：成功的清掉旧的占位标记；失败的按 allow_fallback 决定占位或报错。"""
        nonlocal used_fallback
        failed = [j for j in jobs if not batch_results.get(j["id"])]
        for job in jobs:
            if batch_results.get(job["id"]):
                _clear_fallback_marker(job["out"])
        if not failed:
            return
        if not allow_fallback:
            raise RuntimeError(
                f"{len(failed)} 句合成失败（tts.fallback_on_failure=false，不使用占位音）；"
                "已合成的语音保留在 audio_cache/，修好后重跑会直接续上"
            )
        used_fallback = True
        for job in failed:
            MockTTSBackend().synthesize(job["text"], job["role_cfg"], job["emotion"], job["out"], sample_rate)
            _mark_fallback(job["out"])

    # 第一遍：计算每句的哈希/目标路径，收集尚未缓存的合成任务
    # （哈希值天然去重——同一句台词/情感在章节内重复出现时只合成一次）
    seg_infos = []
    pending_jobs = {}
    for seg in script_final_data:
        speaker = seg.get("speaker", "narrator")
        text = seg.get("text", "")
        emotion = seg.get("emotion", "neutral")
        hash_val = calculate_md5(f"{speaker}_{text}_{emotion}")
        audio_path = os.path.join(audio_cache_dir, f"{hash_val}.wav")
        was_cached = _is_cached(audio_path)  # 带 .fallback 标记的占位音不算命中，要重试真实引擎
        seg_infos.append((seg, audio_path, was_cached))
        if not was_cached and hash_val not in pending_jobs:
            role_cfg = roles_mod.get_role_runtime_config(speaker, manifest, roles_dir)
            pending_jobs[hash_val] = {
                "id": hash_val, "text": text, "role_cfg": role_cfg,
                "emotion": emotion, "out": audio_path, "sample_rate": sample_rate,
            }

    # 报告总任务数
    if progress_cb:
        progress_cb(0, len(script_final_data), f"共 {len(script_final_data)} 句，需新合成 {len(pending_jobs)} 句")

    # 第二遍：批量合成，失败的任务逐条回退 Mock，保证整章合成不中断
    if pending_jobs:
        # 按参考音频路径稳定排序后再下发：script 顺序天然是多角色交替的，
        # IndexTTS 的音色缓存以 spk_audio_prompt 为键（见 infer_v2_5.py 的
        # cache_spk_audio_prompt 判断逻辑），交替顺序会让缓存逐句失效、
        # 每句都重新跑一遍参考音频特征提取；按角色聚拢后同一角色的任务连续
        # 下发，同角色内可以命中缓存，只在切换角色时才重新提取一次。
        # 结果顺序仅影响合成阶段的耗时，不影响 seg_infos 拼接的时间线顺序。
        batch_jobs = sorted(pending_jobs.values(), key=lambda job: job["role_cfg"].get("reference_audio", ""))

        # 检查后端是否支持切批（常驻 daemon 模型常驻显存，切批成本低）
        supports_chunking = getattr(backend, "supports_chunking", False)
        effective_chunk_size = chunk_size or config.get("server", {}).get("gpu_chunk_size", 8)

        if supports_chunking and effective_chunk_size and effective_chunk_size > 0:
            # 常驻 daemon 后端：按已排序列表切连续窗口
            for i in range(0, len(batch_jobs), effective_chunk_size):
                if should_cancel and should_cancel():
                    logger.info("TTS 合成被取消（已合成 %d/%d 句，已缓存的 wav 留在 audio_cache/）",
                                i, len(batch_jobs))
                    raise TaskCancelled(f"用户取消（已合成 {i}/{len(batch_jobs)} 句）")
                chunk = batch_jobs[i:i + effective_chunk_size]
                batch_results = backend.synthesize_batch(chunk)
                _settle_batch(chunk, batch_results)
                if progress_cb:
                    progress_cb(min(i + effective_chunk_size, len(batch_jobs)),
                                len(batch_jobs), f"已合成 {min(i + effective_chunk_size, len(batch_jobs))}/{len(batch_jobs)} 句")
        else:
            # 一次性子进程后端（IndexTTSBackend）：整批下发，切批会重载模型
            batch_results = backend.synthesize_batch(batch_jobs)
            _settle_batch(batch_jobs, batch_results)
            if progress_cb:
                progress_cb(len(batch_jobs), len(batch_jobs), f"已合成 {len(batch_jobs)}/{len(batch_jobs)} 句")

    # 第三遍：按顺序读取实际时长，拼出时间线
    timeline_items = []
    current_time_ms = 0.0
    # 句间静音：以前硬编码 200ms。它在这里被烘焙进 timeline.json 的 start_time_ms，
    # 所以改 tts.segment_gap_ms 后要重跑 TTS 才生效（wav 全部命中缓存，只重算时间线，很快）
    segment_gap_ms = float((config.get("tts") or {}).get("segment_gap_ms", 200.0))
    for seg, audio_path, was_cached in seg_infos:
        seg_id = seg.get("seg_id")
        speaker = seg.get("speaker", "narrator")
        text = seg.get("text", "")
        emotion = seg.get("emotion", "neutral")
        sfx = seg.get("sfx")
        bgm = seg.get("bgm")
        is_cached = was_cached

        with wave.open(audio_path, "r") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            duration_ms = (frames / float(rate)) * 1000.0

        item = {
            "seg_id": seg_id,
            "speaker": speaker,
            "text": text,
            "emotion": emotion,
            "audio_path": os.path.relpath(audio_path, chapter_dir),
            "start_time_ms": current_time_ms,
            "duration_ms": duration_ms,
            "sfx": sfx,
            "bgm": bgm,
            "cached": is_cached,
            # 这一句是不是 Mock 占位音（真实引擎没合成出来）。下次运行会重试它，但在那之前
            # 时间线/混音要能看出来，别让「已完成」掩盖掉里面有噪音
            "fallback": os.path.exists(_marker_path(audio_path)),
        }
        timeline_items.append(item)
        current_time_ms += duration_ms + segment_gap_ms

    timeline_data = {
        "chapter_id": os.path.basename(os.path.abspath(chapter_dir)),
        "total_duration_ms": current_time_ms,
        "tts_engine": backend.name,
        "used_fallback": used_fallback,
        "items": timeline_items,
    }

    timeline_path = os.path.join(chapter_dir, "timeline.json")
    with open(timeline_path, "w", encoding="utf-8") as f:
        json.dump(timeline_data, f, ensure_ascii=False, indent=2)

    update_chapter_status(chapter_dir, "tts_completed")
    return timeline_data


def process_chapter_tts(chapter_dir: str, roles_dir: str = None, backend=None,
                        progress_cb=None, should_cancel=None) -> str:
    """
    基于 script_final.json 执行哈希增量 TTS，如果 script_final.json 不存在则抛出错误。

    backend 默认为 None——生产路径下由 generate_tts_incremental 按
    global_config.yaml 的配置选择真实引擎；测试/自检必须显式传入
    backend=MockTTSBackend()，避免意外触发真实 GPU 推理。
    """
    script_final_path = os.path.join(chapter_dir, "script_final.json")
    if not os.path.exists(script_final_path):
        raise FileNotFoundError(
            f"未找到定稿剧本 {script_final_path}！"
            "请先基于 script_draft.json 确认并创建 script_final.json 后再运行 TTS 任务。"
        )

    with open(script_final_path, "r", encoding="utf-8") as f:
        script_final_data = json.load(f)

    generate_tts_incremental(chapter_dir, script_final_data, backend=backend, roles_dir=roles_dir,
                             progress_cb=progress_cb, should_cancel=should_cancel)
    return os.path.join(chapter_dir, "timeline.json")
