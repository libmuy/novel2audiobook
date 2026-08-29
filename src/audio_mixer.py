"""
Python 自动闪避混音引擎 (Audio Ducking)
"""
import os
import wave
import struct
import math
import json
from pydub import AudioSegment
from src.utils import load_global_config, update_chapter_status


def generate_mock_audio_file(filepath: str, duration_ms: int = 5000, sample_rate: int = 24000):
    """如果背景音或音效文件不存在，自动生成占位 audio 文件"""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    num_samples = int(sample_rate * (duration_ms / 1000.0))
    with wave.open(filepath, "w") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        frames = bytearray()
        for i in range(num_samples):
            # 简易正弦波占位
            val = int(1000 * math.sin(2 * math.pi * 440 * i / sample_rate))
            frames.extend(struct.pack("<h", val))
        f.writeframes(frames)


def mix_chapter(chapter_dir: str, timeline_data: dict = None, config: dict = None) -> str:
    """
    引入 pydub，读取 timeline.json。
    将人声轨拼接，遍历人声计算 RMS 响度，实现自动闪避（Audio Ducking）逻辑——
    当人声音量大于阈值时，环境音轨(BGM)降低至指定音量比例 (如 30%)，
    瞬时音效(SFX)在特定时间戳 Overlay。
    导出为 output/chapter_XXXX.mp3。
    """
    if config is None:
        config = load_global_config()

    mixing_cfg = config.get("mixing", {})
    duck_thresh = mixing_cfg.get("ducking_threshold", -20.0)
    duck_ratio = mixing_cfg.get("ducking_volume_ratio", 0.3)
    # 将 volume_ratio 转化为 pydub dB 变化量: 20 * log10(ratio)
    duck_db_change = 20.0 * math.log10(duck_ratio) if duck_ratio > 0 else -10.0

    timeline_path = os.path.join(chapter_dir, "timeline.json")
    if timeline_data is None:
        if not os.path.exists(timeline_path):
            raise FileNotFoundError(f"未找到时间线文件: {timeline_path}")
        with open(timeline_path, "r", encoding="utf-8") as f:
            timeline_data = json.load(f)

    items = timeline_data.get("items", [])
    total_duration_ms = int(timeline_data.get("total_duration_ms", 1000)) + 1000

    # 1. 创建基底主轨 (静音轨)
    vocal_track = AudioSegment.silent(duration=total_duration_ms, frame_rate=24000)
    bgm_track = AudioSegment.silent(duration=total_duration_ms, frame_rate=24000)
    sfx_track = AudioSegment.silent(duration=total_duration_ms, frame_rate=24000)

    # 记录哪些时间段（以毫秒为单位）有人声发言且 RMS 超过阈值
    duck_intervals = []

    for item in items:
        audio_rel_path = item.get("audio_path")
        audio_full_path = os.path.join(chapter_dir, audio_rel_path)
        start_ms = int(item.get("start_time_ms", 0))

        if os.path.exists(audio_full_path):
            vocal_seg = AudioSegment.from_file(audio_full_path)
            vocal_track = vocal_track.overlay(vocal_seg, position=start_ms)

            # 检测响度 RMS 并记录 Ducking 区间
            if vocal_seg.dBFS > duck_thresh:
                end_ms = start_ms + len(vocal_seg)
                duck_intervals.append((start_ms, end_ms))

        # 叠加 SFX
        sfx_name = item.get("sfx")
        if sfx_name:
            sfx_file = os.path.join("assets", "sfx", f"{sfx_name}.wav")
            if not os.path.exists(sfx_file):
                generate_mock_audio_file(sfx_file, duration_ms=1000)
            sfx_seg = AudioSegment.from_file(sfx_file)
            sfx_track = sfx_track.overlay(sfx_seg, position=start_ms)

        # 叠加 BGM (环境音循环)
        bgm_name = item.get("bgm")
        if bgm_name:
            bgm_file = os.path.join("assets", "ambience", f"{bgm_name}.wav")
            if not os.path.exists(bgm_file):
                generate_mock_audio_file(bgm_file, duration_ms=5000)
            bgm_seg = AudioSegment.from_file(bgm_file)
            # 加载 BGM 片段并贴在当前时间段
            item_duration = int(item.get("duration_ms", 2000))
            loop_count = math.ceil(item_duration / len(bgm_seg)) if len(bgm_seg) > 0 else 1
            bgm_sub = (bgm_seg * loop_count)[:item_duration]
            bgm_track = bgm_track.overlay(bgm_sub, position=start_ms)

    # 2. 执行 Audio Ducking (自动闪避逻辑)
    # 对 bgm_track 在 duck_intervals 区间降低音量
    if duck_intervals:
        # 分块/切割 bgm_track 实施闪避
        ducked_bgm = AudioSegment.silent(duration=total_duration_ms, frame_rate=24000)
        # 简单逐段叠加与衰减处理
        last_pos = 0
        for d_start, d_end in duck_intervals:
            if d_start > last_pos:
                normal_part = bgm_track[last_pos:d_start]
                ducked_bgm = ducked_bgm.overlay(normal_part, position=last_pos)

            ducked_part = bgm_track[d_start:d_end] + duck_db_change
            ducked_bgm = ducked_bgm.overlay(ducked_part, position=d_start)
            last_pos = d_end

        if last_pos < total_duration_ms:
            remainder_part = bgm_track[last_pos:total_duration_ms]
            ducked_bgm = ducked_bgm.overlay(remainder_part, position=last_pos)

        bgm_track = ducked_bgm

    # 3. 三轨终极混音 (人声 + 闪避后BGM + SFX)
    final_mix = vocal_track.overlay(bgm_track).overlay(sfx_track)

    # 4. 导出成品 MP3 文件
    ch_id = os.path.basename(os.path.abspath(chapter_dir))
    output_dir = os.path.join(chapter_dir, "output")
    os.makedirs(output_dir, exist_ok=True)
    output_mp3_path = os.path.join(output_dir, f"{ch_id}.mp3")

    # 导出 MP3 (如果不具备 ffmpeg，退回到 wav 格式导出)
    try:
        final_mix.export(output_mp3_path, format="mp3", bitrate=mixing_cfg.get("bitrate", "192k"))
    except Exception:
        # 备选导出 WAV
        output_wav_path = os.path.join(output_dir, f"{ch_id}.wav")
        final_mix.export(output_wav_path, format="wav")
        output_mp3_path = output_wav_path

    update_chapter_status(chapter_dir, "completed")
    return output_mp3_path
