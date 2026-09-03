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

from src.utils import calculate_md5, update_chapter_status, load_global_config, resolve_path
from src import roles as roles_mod

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


class IndexTTSBackend:
    """
    通过子进程调用独立部署的 IndexTTS-2.5 推理环境（见 docs/indextts_setup.md）。
    该环境使用独立 venv（Python 3.11 + ROCm torch），与本项目主 venv 隔离，
    通过 python_bin + infer_script 两个可执行路径对接。
    """

    name = "index_tts_2_5"

    def __init__(self, config: dict):
        tts_cfg = config.get("tts", {}).get("index_tts", {})
        self.python_bin = resolve_path(tts_cfg.get("python_bin", "tools/indextts_env/bin/python"))
        self.infer_script = resolve_path(tts_cfg.get("infer_script", "tools/indextts_infer.py"))
        self.checkpoints_dir = resolve_path(tts_cfg.get("checkpoints_dir", "/srv/unsafe/models/tts/IndexTTS-2.5"))
        self.timeout = tts_cfg.get("timeout_sec", 120)

    def is_available(self) -> bool:
        return (
            os.path.exists(self.python_bin)
            and os.path.exists(self.infer_script)
            and os.path.isdir(self.checkpoints_dir)
        )

    def synthesize(self, text: str, role_cfg: dict, emotion: str, output_wav_path: str, sample_rate: int) -> bool:
        os.makedirs(os.path.dirname(output_wav_path), exist_ok=True)
        emo_vector = EMOTION_TO_VECTOR.get(emotion, EMOTION_TO_VECTOR["neutral"])
        duration_factor = speed_to_duration_factor(role_cfg.get("speed", 1.0))

        cmd = [
            self.python_bin, self.infer_script,
            "--checkpoints-dir", self.checkpoints_dir,
            "--ref-audio", role_cfg["reference_audio"],
            "--text", text,
            "--lang", "ZH",
            "--emo-vector", ",".join(str(v) for v in emo_vector),
            "--duration-factor", str(duration_factor),
            "--sample-rate", str(sample_rate),
            "--out", output_wav_path,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            logger.warning("[IndexTTSBackend] 合成超时（角色=%s，文本=%.20s…）", role_cfg.get("role_id"), text)
            return False
        if result.returncode != 0 or not os.path.exists(output_wav_path):
            logger.warning(
                "[IndexTTSBackend] 合成失败（角色=%s）: %s", role_cfg.get("role_id"), result.stderr[-500:]
            )
            return False
        return True


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
                              backend=None, roles_dir: str = None) -> dict:
    """
    遍历 JSON 的每一句，使用 MD5(speaker + text + emotion) 计算哈希值作为文件名。
    检查 audio_cache/ 下是否已有该文件。如果有，跳过合成；如果没有，调用 TTS 后端生成。
    返回累加的时间线数据 timeline.json 的结构。
    """
    if backend is None:
        backend = build_tts_backend()

    audio_cache_dir = os.path.join(chapter_dir, "audio_cache")
    os.makedirs(audio_cache_dir, exist_ok=True)

    manifest = roles_mod.load_manifest(roles_dir)
    timeline_items = []
    current_time_ms = 0.0
    used_fallback = False

    for seg in script_final_data:
        seg_id = seg.get("seg_id")
        speaker = seg.get("speaker", "narrator")
        text = seg.get("text", "")
        emotion = seg.get("emotion", "neutral")
        sfx = seg.get("sfx")
        bgm = seg.get("bgm")

        hash_key = f"{speaker}_{text}_{emotion}"
        hash_val = calculate_md5(hash_key)
        filename = f"{hash_val}.wav"
        audio_path = os.path.join(audio_cache_dir, filename)

        if not os.path.exists(audio_path):
            role_cfg = roles_mod.get_role_runtime_config(speaker, manifest, roles_dir)
            ok = backend.synthesize(text, role_cfg, emotion, audio_path, sample_rate)
            if not ok:
                # 主后端失败时逐句回退 Mock，保证整章合成不中断
                used_fallback = True
                MockTTSBackend().synthesize(text, role_cfg, emotion, audio_path, sample_rate)
            is_cached = False
        else:
            is_cached = True

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
        }
        timeline_items.append(item)
        current_time_ms += duration_ms + 200.0

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


def process_chapter_tts(chapter_dir: str, roles_dir: str = None) -> str:
    """
    基于 script_final.json 执行哈希增量 TTS，如果 script_final.json 不存在则抛出错误。
    """
    script_final_path = os.path.join(chapter_dir, "script_final.json")
    if not os.path.exists(script_final_path):
        raise FileNotFoundError(
            f"未找到定稿剧本 {script_final_path}！"
            "请先基于 script_draft.json 确认并创建 script_final.json 后再运行 TTS 任务。"
        )

    with open(script_final_path, "r", encoding="utf-8") as f:
        script_final_data = json.load(f)

    generate_tts_incremental(chapter_dir, script_final_data, roles_dir=roles_dir)
    return os.path.join(chapter_dir, "timeline.json")
