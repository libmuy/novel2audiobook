#!/usr/bin/env python3
"""
成品 MP3 机器验收脚本（对应验收目标 #4：ffprobe 可解析 / 时长合理 / 含真实语音 /
无爆音）。用于 Stage 5 全量验收，非 pytest 单元测试（依赖真实合成产物，非纯离线）。

用法：
    python tools/validate_output_audio.py <mp3_path> [--min-duration-ms N]
"""
import argparse
import json
import subprocess
import sys

import numpy as np
from pydub import AudioSegment


def ffprobe_duration_ms(path: str) -> float:
    """用 ffprobe 解析文件，确认是合法音频容器并取时长（毫秒）"""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(out.stdout)
    return float(data["format"]["duration"]) * 1000.0


def _dominant_period_strength(frame: np.ndarray, lag_min: int, lag_max: int):
    """返回 (主周期滞后样本数, 归一化自相关峰值)；能量过低（静音帧）返回 (None, 0.0)"""
    frame = frame - np.mean(frame)
    energy = np.sum(frame ** 2)
    if energy < 1e-6 or len(frame) <= lag_max:
        return None, 0.0
    corr = np.correlate(frame, frame, mode="full")
    corr = corr[len(corr) // 2:]
    corr = corr / (corr[0] + 1e-9)
    window = corr[lag_min:lag_max]
    peak_idx = int(np.argmax(window))
    return peak_idx + lag_min, float(window[peak_idx])


def _looks_like_fixed_tone(samples: np.ndarray, frame_rate: int, frame_ms: float = 200.0) -> bool:
    """
    检测“固定周期音”特征：mock 占位音是逐样本 ±固定值的方波，周期恒定不变、
    从头到尾无静音无变化；真实语音即便有浊音段（自相关也会较高），
    其基频会随韵律漂移、且存在辅音/停顿等非周期段，不会全程保持同一周期。
    覆盖 lag 30~1000 样本（对应 24~800Hz，覆盖常见基频与本项目 mock 周期 200 样本/120Hz）。
    """
    frame_len = int(frame_rate * frame_ms / 1000.0)
    lag_min, lag_max = 30, min(1000, frame_len - 1)
    if frame_len <= lag_max or len(samples) < frame_len * 3:
        return False

    lags, strengths = [], []
    for start in range(0, len(samples) - frame_len, frame_len):
        lag, strength = _dominant_period_strength(samples[start:start + frame_len], lag_min, lag_max)
        if lag is not None:
            lags.append(lag)
            strengths.append(strength)

    if len(strengths) < 3:
        return False
    strengths = np.array(strengths)
    lags = np.array(lags)
    high_mask = strengths > 0.9
    high_ratio = float(np.mean(high_mask))
    lag_std = float(np.std(lags[high_mask])) if np.any(high_mask) else float("inf")
    # 要求几乎每一帧都高度自相关，且主周期几乎不变——真实语音很难同时满足这两点
    return high_ratio > 0.9 and lag_std < 3.0


def analyze(path: str, min_duration_ms: float) -> list:
    """返回问题列表；空列表代表通过全部检查"""
    problems = []

    try:
        duration_ms = ffprobe_duration_ms(path)
    except Exception as e:
        return [f"ffprobe 无法解析文件: {e}"]

    if duration_ms < min_duration_ms:
        problems.append(f"时长 {duration_ms:.0f}ms 低于预期下限 {min_duration_ms:.0f}ms")

    seg = AudioSegment.from_file(path)
    samples = np.array(seg.get_array_of_samples()).astype(np.float64)
    if seg.channels > 1:
        samples = samples.reshape((-1, seg.channels)).mean(axis=1)

    # 1) 非静音：整体 RMS 不能过低
    if seg.dBFS == float("-inf") or seg.dBFS < -50.0:
        problems.append(f"整体响度过低（{seg.dBFS} dBFS），疑似静音/占位音")

    # 2) 非占位波形：mock 引擎生成固定周期方波，MP3 有损编码会在时域引入
    #    大量量化/振铃噪声，采样值数量、频谱平坦度等简单判据在编码后都会失真，
    #    因此改用分帧自相关检测“固定周期音”特征（见 _looks_like_fixed_tone 注释）。
    n = min(len(samples), seg.frame_rate * 5)  # 最多取前 5 秒
    if _looks_like_fixed_tone(samples[:n], seg.frame_rate):
        problems.append("检测到全程固定周期的自相关特征，疑似占位方波而非真实语音")

    # 3) 无爆音：满幅采样点占比应很低
    max_val = float(2 ** (8 * seg.sample_width - 1) - 1)
    clip_ratio = float(np.mean(np.abs(samples) >= max_val * 0.999)) if len(samples) else 0.0
    if clip_ratio > 0.001:
        problems.append(f"疑似爆音，满幅采样点占比 {clip_ratio*100:.3f}% 超过 0.1% 阈值")

    return problems


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mp3_path")
    parser.add_argument("--min-duration-ms", type=float, default=1000.0)
    args = parser.parse_args()

    problems = analyze(args.mp3_path, args.min_duration_ms)
    if problems:
        print(f"✗ {args.mp3_path} 未通过验收检查：")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    else:
        print(f"✓ {args.mp3_path} 通过全部机器验收检查")
        sys.exit(0)


if __name__ == "__main__":
    main()
