#!/usr/bin/env python3
"""
生成角色“种子参考音频”（roles/<role_id>/reference.wav）。

背景：项目脚手架自带的 reference.wav 实测是 0.5 秒纯静音占位文件，
IndexTTS-2.5 的零样本声音克隆完全依赖参考音频里的真实音色，喂静音会导致
克隆失败/产出垃圾音频。本机没有可用的录音设备，也不适合未经许可爬取
互联网上的真人声音去克隆（IndexTTS 官方声明不核验参考音频的授权，克隆前
获取同意是使用者的责任）——因此用本机离线的 espeak-ng（一种传统共振峰
合成器，产出的是"电子合成音"而非任何真实个人的声音，不涉及授权问题）
朗读一段带角色气质的文本，生成约 8~10 秒的种子参考音频。

后续如有真人配音/授权样本，直接替换对应 roles/<role_id>/reference.wav 即可，
IndexTTS 合成质量会显著提升；种子音频只是让管线能先跑通真实的克隆流程。

用法：
    python tools/generate_seed_reference.py [--force]

--force 会覆盖已存在的“看起来正常”的 reference.wav；默认只处理检测到是
静音/过短（<2秒）的占位文件，避免误覆盖已经手动放好的真实录音。
"""
import argparse
import os
import subprocess
import sys

from pydub import AudioSegment

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ESPEAK_BIN = "/srv/unsafe/tools/espeak_ng/bin/espeak-ng"
ESPEAK_DATA = "/srv/unsafe/tools/espeak_ng/lib/x86_64-linux-gnu/espeak-ng-data"
ESPEAK_LIB_DIR = "/srv/unsafe/tools/espeak_ng/lib/x86_64-linux-gnu"

TARGET_SAMPLE_RATE = 24000

# 角色 -> (朗读文本, pitch 0-99, speed wpm) —— 用 pitch/speed 粗略区分音色气质，
# 而非依赖 mbrola 变体（本机未装 mbrola 合成器数据）。
ROLE_SEED_TEXT = {
    "narrator": (
        "夜色渐渐深了，山间的风穿过林梢，发出低沉而悠长的声音。"
        "他站在原地，缓缓地环顾四周，心中盘算着接下来该如何应对。"
        "故事还在继续，命运的齿轮已经悄然转动。",
        45, 150,
    ),
    "lin_dong": (
        "今天我一定要变得更强！无论前面有多少艰难险阻，我都不会退缩半步！"
        "总有一天，我要让所有看不起我的人都刮目相看！",
        60, 185,
    ),
    "su_yan": (
        "这几天攒的碎灵石，凑不齐娘的一副止咳散，那药一天都断不得。"
        "撬石不能急，急则力散，还容易带下浮石。今日之事，我心里都清楚。",
        50, 170,
    ),
    "liu_he": (
        "回来了？药换回来了，娘放心喝。戴上这块玉，别摘。"
        "锅里给你留着薯根粥，还温着。",
        35, 130,
    ),
    "ma_tie_cheng": (
        "四两五，按规矩该一枚半。今儿这批次，按一枚算，三成记账。"
        "愣什么？上秤！看他跪不跪。",
        30, 160,
    ),
}
DEFAULT_SEED = ("这是一段用于生成参考音色的示例朗读文本，内容与角色设定无关。", 50, 165)


def is_placeholder(wav_path: str, min_duration_ms: int = 2000) -> bool:
    if not os.path.exists(wav_path):
        return True
    seg = AudioSegment.from_file(wav_path)
    return len(seg) < min_duration_ms or seg.dBFS == float("-inf")


def synthesize_with_espeak(text: str, pitch: int, speed_wpm: int, out_wav: str):
    env = dict(os.environ, LD_LIBRARY_PATH=ESPEAK_LIB_DIR)
    cmd = [
        ESPEAK_BIN, "--path", ESPEAK_DATA,
        "-v", "zh", "-p", str(pitch), "-s", str(speed_wpm),
        text, "-w", out_wav,
    ]
    subprocess.run(cmd, env=env, check=True, capture_output=True)


def generate_for_role(role_id: str, force: bool = False) -> bool:
    role_dir = os.path.join(PROJECT_ROOT, "roles", role_id)
    ref_path = os.path.join(role_dir, "reference.wav")
    if not force and not is_placeholder(ref_path):
        print(f"[skip] {role_id}: reference.wav 已是有效音频，跳过（如需强制重生成加 --force）")
        return False

    text, pitch, speed = ROLE_SEED_TEXT.get(role_id, DEFAULT_SEED)
    os.makedirs(role_dir, exist_ok=True)
    raw_wav = ref_path + ".espeak_raw.wav"
    synthesize_with_espeak(text, pitch, speed, raw_wav)

    seg = AudioSegment.from_file(raw_wav)
    seg = seg.set_frame_rate(TARGET_SAMPLE_RATE).set_channels(1)
    seg.export(ref_path, format="wav")
    os.remove(raw_wav)

    print(f"[ok] {role_id}: 生成种子参考音频 {ref_path}（{len(seg)/1000:.1f}s, {seg.dBFS:.1f} dBFS）")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="覆盖已存在的看起来有效的 reference.wav")
    parser.add_argument("--roles", nargs="*", default=None, help="只处理指定角色 ID；默认处理 roles/ 下所有已注册角色")
    args = parser.parse_args()

    if not os.path.exists(ESPEAK_BIN):
        print(f"错误：未找到 espeak-ng 可执行文件 {ESPEAK_BIN}，请先按 docs/indextts_setup.md 搭建", file=sys.stderr)
        sys.exit(1)

    roles_dir = os.path.join(PROJECT_ROOT, "roles")
    role_ids = args.roles or [
        d for d in os.listdir(roles_dir) if os.path.isdir(os.path.join(roles_dir, d))
    ]

    any_generated = False
    for role_id in sorted(role_ids):
        if generate_for_role(role_id, force=args.force):
            any_generated = True

    if not any_generated:
        print("没有角色需要生成（都已有有效参考音频）。")


if __name__ == "__main__":
    main()
