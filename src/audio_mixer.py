"""
Python 自动闪避混音引擎 (Audio Ducking)
"""
import os
import wave
import struct
import math
import json
import logging
import numpy as np
from pydub import AudioSegment
from src.utils import load_global_config, update_chapter_status, resolve_path

logger = logging.getLogger(__name__)

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
                            crossfade_ms: int = 400, missing_out: list = None) -> AudioSegment:
    """
    将连续使用同一 bgm 的句段合并为"场景"，铺一条连续环境音轨（而非逐句独立贴片），
    场景切换处做 crossfade，避免环境音断续闪烁。

    missing_out 是可选的输出参数（不是返回值——直接调用这个函数的既有测试断言
    返回值就是 AudioSegment，改成 tuple 会破坏它们）：传入一个 list，缺失的
    bgm 素材名会被 append 进去，供调用方（mix_chapter）写进 mix_meta.json 的
    sidecar。不传就是原来的行为，什么都不收集。
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

    # 2) 逐场景铺连续环境音，每段独立 fade in/out 起到场景切换处的 crossfade 效果
    for start_ms, end_ms, bgm_name in scenes:
        if bgm_name is None:
            continue
        bgm_file = resolve_path(os.path.join("assets", "ambience", f"{bgm_name}.wav"))
        if not os.path.exists(bgm_file):
            # mix 是读路径，缺素材不该在这里静默补一份占位音写进共享的 assets/
            # 目录（那是 src/asset_gen.py 的职责）；跳过这段场景音即可，
            # 不影响人声。
            logger.warning("场景 [%d, %d) 引用的环境音 %r 不存在（%s），跳过该段环境音",
                            start_ms, end_ms, bgm_name, bgm_file)
            if missing_out is not None and bgm_name not in missing_out:
                missing_out.append(bgm_name)
            continue
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
            "请先重新运行 `./run.sh tts` 生成完整音轨。"
        )


def mix_chapter(chapter_dir: str, timeline_data: dict = None, config: dict = None,
                voice_only: bool = None, output_stem: str = None) -> str:
    """
    引入 pydub，读取 timeline.json。
    将人声轨拼接；voice_only=False 时还会遍历人声计算 RMS 响度，实现自动闪避
    （Audio Ducking）——人声音量大于阈值时环境音轨(BGM)降低至指定音量比例，
    瞬时音效(SFX)在特定时间戳 Overlay。
    导出为 output/chapter_XXXX.mp3。

    voice_only 未显式传入时，取 global_config.yaml 的 mixing.voice_only
    （当前阶段默认 True——效果音/环境音流水线尚未做，先只出人声成片，
    避免任何缺素材时的占位音悄悄混进成片）。
    """
    if config is None:
        config = load_global_config()

    mixing_cfg = config.get("mixing", {})
    if voice_only is None:
        voice_only = mixing_cfg.get("voice_only", True)
    duck_thresh = mixing_cfg.get("ducking_threshold", -20.0)
    duck_ratio = mixing_cfg.get("ducking_volume_ratio", 0.3)
    duck_fade_ms = mixing_cfg.get("ducking_fade_ms", 300)
    ambience_gain_db = mixing_cfg.get("ambience_gain_db", -18.0)
    sfx_limit_db = mixing_cfg.get("sfx_limit_dbfs", -3.0)
    # 闪避量有两个来源：mixing.ducking_gain_db（dB，设置页用它）优先；没设才回落到
    # 旧的 ducking_volume_ratio（线性比例，不是 dB——设置页不暴露它，因为用户在「闪避」
    # 字段里填 -10 会撞上下面 else 分支恰好得到 -10 dB，误以为字段单位是 dB）
    if mixing_cfg.get("ducking_gain_db") is not None:
        duck_db_change = float(mixing_cfg["ducking_gain_db"])
    else:
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

    # 记录哪些时间段（以毫秒为单位）有人声发言且 RMS 超过阈值（仅 voice_only=False 时才用得上）
    duck_intervals = []
    # 下面这几个只用来喂 mix_meta.json sidecar（见函数末尾）——只在真的处理素材
    # 的分支里收集：voice_only 这次没有实际叠加任何素材，sidecar 要如实记录
    # "这次没混进任何素材"，而不是把 timeline 里潜在的引用当成已经用上了
    # （timeline 里本身引用了哪些素材，是 Phase 2 的 GET .../assets 接口另外算的）
    bgm_names_referenced = []
    sfx_segment_count = 0
    missing_bgm = []
    missing_sfx = []

    for item in items:
        audio_rel_path = item.get("audio_path")
        audio_full_path = os.path.join(chapter_dir, audio_rel_path)
        start_ms = int(item.get("start_time_ms", 0))

        vocal_seg = _load_and_normalize(audio_full_path)
        vocal_track = vocal_track.overlay(vocal_seg, position=start_ms)

        if voice_only:
            continue  # 纯人声模式：不叠加音效、也不需要为闪避收集区间

        if vocal_seg.dBFS > duck_thresh:
            duck_intervals.append((start_ms, start_ms + len(vocal_seg)))

        sfx_name = item.get("sfx")
        if sfx_name:
            sfx_segment_count += 1
            sfx_file = resolve_path(os.path.join("assets", "sfx", f"{sfx_name}.wav"))
            if not os.path.exists(sfx_file):
                # 同上：不静默补占位音写进共享 assets/，跳过这条音效即可
                logger.warning("句段 %s 引用的音效 %r 不存在（%s），跳过该条音效叠加",
                                item.get("seg_id"), sfx_name, sfx_file)
                if sfx_name not in missing_sfx:
                    missing_sfx.append(sfx_name)
            else:
                sfx_seg = _load_and_normalize(sfx_file)
                if sfx_seg.dBFS > sfx_limit_db:
                    sfx_seg = sfx_seg.apply_gain(sfx_limit_db - sfx_seg.dBFS)  # 限幅防爆音
                sfx_track = sfx_track.overlay(sfx_seg, position=start_ms)

    if voice_only:
        bgm_track = AudioSegment.silent(duration=total_duration_ms, frame_rate=MIX_SAMPLE_RATE)
    else:
        bgm_names_referenced = sorted({item.get("bgm") for item in items if item.get("bgm")})
        # 2. 场景级连续环境音轨（替代逐句硬贴，消除断续感），并施加基础增益
        bgm_track = _build_scene_bgm_track(items, total_duration_ms, chapter_dir, missing_out=missing_bgm)
        if len(bgm_track) > 0 and bgm_track.dBFS != float("-inf"):
            bgm_track = bgm_track.apply_gain(ambience_gain_db - bgm_track.dBFS)

        # 3. 执行 Audio Ducking (自动闪避逻辑，边界做增益渐变)
        bgm_track = _apply_ducking(bgm_track, duck_intervals, duck_db_change, duck_fade_ms, total_duration_ms)

    # 4. 三轨终极混音 (人声 + 闪避后BGM + SFX)
    final_mix = vocal_track.overlay(bgm_track).overlay(sfx_track)

    # 5. 导出成品文件，格式由 mixing.output_format 决定（默认 mp3，兼容旧行为）
    if output_stem is None:
        base = os.path.basename(os.path.abspath(chapter_dir))
        num = base[3:] if base.startswith("ch_") else base
        output_stem = f"chapter_{num}"
    output_dir = os.path.join(chapter_dir, "output")
    os.makedirs(output_dir, exist_ok=True)

    output_format = str(mixing_cfg.get("output_format") or "mp3").lower()
    if output_format not in ("mp3", "wav", "flac"):
        output_format = "mp3"
    output_path = os.path.join(output_dir, f"{output_stem}.{output_format}")

    try:
        export_kwargs = {"bitrate": mixing_cfg.get("bitrate", "192k")} if output_format == "mp3" else {}
        final_mix.export(output_path, format=output_format, **export_kwargs)
        output_mp3_path = output_path
    except Exception:
        # 目标格式的编码器不可用（比如系统 ffmpeg 没编译 flac 支持）时，
        # 回退到 wav——pydub/ffmpeg 原生支持，几乎不会失败
        output_wav_path = os.path.join(output_dir, f"{output_stem}.wav")
        final_mix.export(output_wav_path, format="wav")
        output_mp3_path = output_wav_path

    _write_mix_meta(output_dir, {
        "voice_only": bool(voice_only),
        "output_file": os.path.basename(output_mp3_path),
        "format": os.path.splitext(output_mp3_path)[1].lstrip("."),
        "bgm_names": bgm_names_referenced,
        "sfx_count": sfx_segment_count,
        "missing_assets": {"bgm": sorted(missing_bgm), "sfx": sorted(missing_sfx)},
        "mixed_at": _now_str(),
    })

    update_chapter_status(chapter_dir, "completed")
    return output_mp3_path


def _now_str() -> str:
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _write_mix_meta(output_dir: str, data: dict):
    """写混音结果的 sidecar（output/mix_meta.json），记录这次混音是否带了素材、
    引用/缺失了哪些素材。不往 .status.json 里加字段——那个文件由三处不同的
    写入点（parse/tts/mix）整体覆写，混音相关的字段很容易在下次 parse/tts 后
    被冲掉；sidecar 跟 output/ 目录同生命周期，逻辑更干净。"""
    path = os.path.join(output_dir, "mix_meta.json")
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)
