"""
Python 自动闪避混音引擎 (Audio Ducking)
"""
import os
import wave
import struct
import math
import json
import numpy as np
from pydub import AudioSegment
from src.utils import load_global_config, update_chapter_status, resolve_path

MIX_SAMPLE_RATE = 24000


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
            val = int(1000 * math.sin(2 * math.pi * 440 * i / sample_rate))
            frames.extend(struct.pack("<h", val))
        f.writeframes(frames)


def _load_and_normalize(path: str) -> AudioSegment:
    """加载音频并统一采样率/声道，避免不同素材混音时产生音高/速度失真"""
    seg = AudioSegment.from_file(path)
    if seg.frame_rate != MIX_SAMPLE_RATE:
        seg = seg.set_frame_rate(MIX_SAMPLE_RATE)
    if seg.channels != 1:
        seg = seg.set_channels(1)
    return seg


def _merge_intervals(intervals: list, gap_ms: float = 150.0) -> list:
    """合并相邻/重叠的闪避区间，避免短句之间 BGM 音量反复跳变"""
    if not intervals:
        return []
    intervals = sorted(intervals)
    merged = [list(intervals[0])]
    for start, end in intervals[1:]:
        if start <= merged[-1][1] + gap_ms:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [tuple(iv) for iv in merged]


def _apply_gain_ramp(segment: AudioSegment, start_db: float, end_db: float) -> AudioSegment:
    """对一段音频施加线性增益渐变（pydub 原生 fade 只能渐变到静音，这里用 numpy 实现渐变到目标电平）"""
    if len(segment) == 0:
        return segment
    samples = np.array(segment.get_array_of_samples()).astype(np.float64)
    channels = segment.channels
    n_frames = len(samples) // channels
    gains_db = np.linspace(start_db, end_db, n_frames)
    gains_linear = 10.0 ** (gains_db / 20.0)
    if channels > 1:
        gains_linear = np.repeat(gains_linear, channels)
    samples = samples * gains_linear
    max_val = float(2 ** (8 * segment.sample_width - 1) - 1)
    samples = np.clip(samples, -max_val - 1, max_val)
    new_data = samples.astype(segment.array_type)
    return segment._spawn(new_data.tobytes())


def _apply_ducking(bgm_track: AudioSegment, duck_intervals: list, duck_db_change: float,
                    fade_ms: float, total_duration_ms: int) -> AudioSegment:
    """对 bgm_track 在合并后的闪避区间内降低音量，边界做增益渐变而非硬切"""
    if not duck_intervals:
        return bgm_track

    merged = _merge_intervals(duck_intervals)
    ducked_bgm = AudioSegment.silent(duration=total_duration_ms, frame_rate=MIX_SAMPLE_RATE)
    last_pos = 0

    for d_start, d_end in merged:
        d_start = max(0, int(d_start))
        d_end = min(total_duration_ms, int(d_end))
        if d_start > last_pos:
            normal_part = bgm_track[last_pos:d_start]
            ducked_bgm = ducked_bgm.overlay(normal_part, position=last_pos)

        fade = min(fade_ms, (d_end - d_start) / 2) if d_end > d_start else 0
        segment = bgm_track[d_start:d_end]
        if fade > 0:
            pre = segment[:int(fade)]
            mid = segment[int(fade):int(len(segment) - fade)]
            post = segment[int(len(segment) - fade):]
            pre = _apply_gain_ramp(pre, 0.0, duck_db_change)
            mid = mid + duck_db_change
            post = _apply_gain_ramp(post, duck_db_change, 0.0)
            segment = pre + mid + post
        else:
            segment = segment + duck_db_change
        ducked_bgm = ducked_bgm.overlay(segment, position=d_start)
        last_pos = d_end

    if last_pos < total_duration_ms:
        remainder_part = bgm_track[last_pos:total_duration_ms]
        ducked_bgm = ducked_bgm.overlay(remainder_part, position=last_pos)

    return ducked_bgm


def _build_scene_bgm_track(items: list, total_duration_ms: int, chapter_dir: str,
                            crossfade_ms: int = 400) -> AudioSegment:
    """
    将连续使用同一 bgm 的句段合并为“场景”，铺一条连续环境音轨（而非逐句独立贴片），
    场景切换处做 crossfade，避免环境音断续闪烁。
    """
    bgm_track = AudioSegment.silent(duration=total_duration_ms, frame_rate=MIX_SAMPLE_RATE)
    if not items:
        return bgm_track

    # 1) 把连续相同 bgm 的句段合并为场景区间 [start_ms, end_ms, bgm_name]
    scenes = []
    cur_bgm = None
    cur_start = None
    cur_end = None
    for item in items:
        bgm_name = item.get("bgm")
        start_ms = item.get("start_time_ms", 0)
        end_ms = start_ms + item.get("duration_ms", 0)
        if bgm_name == cur_bgm and cur_bgm is not None:
            cur_end = end_ms
        else:
            if cur_bgm is not None:
                scenes.append((cur_start, cur_end, cur_bgm))
            cur_bgm, cur_start, cur_end = bgm_name, start_ms, end_ms
    if cur_bgm is not None:
        scenes.append((cur_start, cur_end, cur_bgm))

    # 2) 逐场景铺连续环境音，场景间用 crossfade 过渡
    prev_scene_end = None
    for start_ms, end_ms, bgm_name in scenes:
        if bgm_name is None:
            prev_scene_end = None
            continue
        bgm_file = resolve_path(os.path.join("assets", "ambience", f"{bgm_name}.wav"))
        if not os.path.exists(bgm_file):
            generate_mock_audio_file(bgm_file, duration_ms=5000)
        bgm_seg = _load_and_normalize(bgm_file)

        scene_duration = int(end_ms - start_ms)
        if scene_duration <= 0 or len(bgm_seg) == 0:
            continue
        loop_count = math.ceil(scene_duration / len(bgm_seg))
        scene_audio = (bgm_seg * loop_count)[:scene_duration]

        fade = min(crossfade_ms, scene_duration // 2)
        if fade > 0:
            scene_audio = scene_audio.fade_in(fade).fade_out(fade)

        bgm_track = bgm_track.overlay(scene_audio, position=int(start_ms))
        prev_scene_end = end_ms

    return bgm_track


def _validate_timeline_assets(items: list, chapter_dir: str):
    """mix 前校验 timeline 引用的人声缓存文件确实存在，尽早暴露 tts 步骤未完成/被跳过的问题"""
    missing = []
    for item in items:
        audio_rel_path = item.get("audio_path")
        if not audio_rel_path:
            missing.append(item.get("seg_id"))
            continue
        if not os.path.exists(os.path.join(chapter_dir, audio_rel_path)):
            missing.append(item.get("seg_id"))
    if missing:
        raise FileNotFoundError(
            f"timeline.json 中 {len(missing)} 个句段缺失对应人声音频文件（seg_id={missing[:10]}…），"
            "请先重新运行 `python cli.py tts` 生成完整音轨。"
        )


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
    duck_fade_ms = mixing_cfg.get("ducking_fade_ms", 300)
    ambience_gain_db = mixing_cfg.get("ambience_gain_db", -18.0)
    sfx_limit_db = mixing_cfg.get("sfx_limit_dbfs", -3.0)
    duck_db_change = 20.0 * math.log10(duck_ratio) if duck_ratio > 0 else -10.0

    timeline_path = os.path.join(chapter_dir, "timeline.json")
    if timeline_data is None:
        if not os.path.exists(timeline_path):
            raise FileNotFoundError(f"未找到时间线文件: {timeline_path}")
        with open(timeline_path, "r", encoding="utf-8") as f:
            timeline_data = json.load(f)

    items = timeline_data.get("items", [])
    _validate_timeline_assets(items, chapter_dir)
    total_duration_ms = int(timeline_data.get("total_duration_ms", 1000)) + 1000

    # 1. 创建基底主轨 (静音轨)
    vocal_track = AudioSegment.silent(duration=total_duration_ms, frame_rate=MIX_SAMPLE_RATE)
    sfx_track = AudioSegment.silent(duration=total_duration_ms, frame_rate=MIX_SAMPLE_RATE)

    # 记录哪些时间段（以毫秒为单位）有人声发言且 RMS 超过阈值
    duck_intervals = []

    for item in items:
        audio_rel_path = item.get("audio_path")
        audio_full_path = os.path.join(chapter_dir, audio_rel_path)
        start_ms = int(item.get("start_time_ms", 0))

        vocal_seg = _load_and_normalize(audio_full_path)
        vocal_track = vocal_track.overlay(vocal_seg, position=start_ms)
        if vocal_seg.dBFS > duck_thresh:
            duck_intervals.append((start_ms, start_ms + len(vocal_seg)))

        sfx_name = item.get("sfx")
        if sfx_name:
            sfx_file = resolve_path(os.path.join("assets", "sfx", f"{sfx_name}.wav"))
            if not os.path.exists(sfx_file):
                generate_mock_audio_file(sfx_file, duration_ms=1000)
            sfx_seg = _load_and_normalize(sfx_file)
            if sfx_seg.dBFS > sfx_limit_db:
                sfx_seg = sfx_seg.apply_gain(sfx_limit_db - sfx_seg.dBFS)  # 限幅防爆音
            sfx_track = sfx_track.overlay(sfx_seg, position=start_ms)

    # 2. 场景级连续环境音轨（替代逐句硬贴，消除断续感），并施加基础增益
    bgm_track = _build_scene_bgm_track(items, total_duration_ms, chapter_dir)
    if len(bgm_track) > 0 and bgm_track.dBFS != float("-inf"):
        bgm_track = bgm_track.apply_gain(ambience_gain_db - bgm_track.dBFS)

    # 3. 执行 Audio Ducking (自动闪避逻辑，边界做增益渐变)
    bgm_track = _apply_ducking(bgm_track, duck_intervals, duck_db_change, duck_fade_ms, total_duration_ms)

    # 4. 三轨终极混音 (人声 + 闪避后BGM + SFX)
    final_mix = vocal_track.overlay(bgm_track).overlay(sfx_track)

    # 5. 导出成品 MP3 文件（文件名遵循 chapter_XXXX.mp3 约定）
    ch_id = os.path.basename(os.path.abspath(chapter_dir))
    ch_num = ch_id.replace("ch_", "")
    output_dir = os.path.join(chapter_dir, "output")
    os.makedirs(output_dir, exist_ok=True)
    output_mp3_path = os.path.join(output_dir, f"chapter_{ch_num}.mp3")

    try:
        final_mix.export(output_mp3_path, format="mp3", bitrate=mixing_cfg.get("bitrate", "192k"))
    except Exception:
        output_wav_path = os.path.join(output_dir, f"chapter_{ch_num}.wav")
        final_mix.export(output_wav_path, format="wav")
        output_mp3_path = output_wav_path

    update_chapter_status(chapter_dir, "completed")
    return output_mp3_path
