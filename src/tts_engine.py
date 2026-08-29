"""
TTS 推理模块 (支持基于 MD5 的哈希增量合成)
"""
import os
import wave
import struct
import json
from src.utils import calculate_md5, update_chapter_status


def generate_mock_wav(output_wav_path: str, duration_sec: float = 2.0, sample_rate: int = 24000):
    """生成一个包含基础静音/平滑波形的 mock wav 文件"""
    num_samples = int(sample_rate * duration_sec)
    os.makedirs(os.path.dirname(output_wav_path), exist_ok=True)
    with wave.open(output_wav_path, "w") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        # 填充 samples
        frames = bytearray()
        for i in range(num_samples):
            # 生成简单的低音量波形以便区分非完全静音
            val = int(500 * (1 if (i // 100) % 2 == 0 else -1))
            frames.extend(struct.pack("<h", val))
        f.writeframes(frames)


def generate_tts_incremental(chapter_dir: str, script_final_data: list, sample_rate: int = 24000) -> dict:
    """
    遍历 JSON 的每一句，使用 MD5(speaker + text + emotion) 计算哈希值作为文件名。
    检查 audio_cache/ 下是否已有该文件。如果有，跳过合成；如果没有，调用 Mock 的 TTS 逻辑生成。
    返回累加的时间线数据 timeline.json 的结构。
    """
    audio_cache_dir = os.path.join(chapter_dir, "audio_cache")
    os.makedirs(audio_cache_dir, exist_ok=True)

    timeline_items = []
    current_time_ms = 0.0

    for seg in script_final_data:
        seg_id = seg.get("seg_id")
        speaker = seg.get("speaker", "narrator")
        text = seg.get("text", "")
        emotion = seg.get("emotion", "neutral")
        sfx = seg.get("sfx")
        bgm = seg.get("bgm")

        # 核心逻辑：哈希值计算
        hash_key = f"{speaker}_{text}_{emotion}"
        hash_val = calculate_md5(hash_key)
        filename = f"{hash_val}.wav"
        audio_path = os.path.join(audio_cache_dir, filename)

        # 增量判断
        if not os.path.exists(audio_path):
            # 动态估算时长：每个字 0.2 秒，最少 1 秒
            calc_duration = max(1.0, len(text) * 0.2)
            generate_mock_wav(audio_path, duration_sec=calc_duration, sample_rate=sample_rate)
            is_cached = False
        else:
            is_cached = True

        # 读取实际 wav 获取精确时长(毫秒)
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
            "cached": is_cached
        }
        timeline_items.append(item)
        # 添加 200ms 句间停顿
        current_time_ms += duration_ms + 200.0

    timeline_data = {
        "chapter_id": os.path.basename(os.path.abspath(chapter_dir)),
        "total_duration_ms": current_time_ms,
        "items": timeline_items
    }

    timeline_path = os.path.join(chapter_dir, "timeline.json")
    with open(timeline_path, "w", encoding="utf-8") as f:
        json.dump(timeline_data, f, ensure_ascii=False, indent=2)

    update_chapter_status(chapter_dir, "tts_completed")
    return timeline_data


def process_chapter_tts(chapter_dir: str) -> str:
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

    generate_tts_incremental(chapter_dir, script_final_data)
    return os.path.join(chapter_dir, "timeline.json")
