#!/usr/bin/env python3
"""
AudioLDM 批量推理脚本（环境音 ambience，替换 ACE-Step 1.5）。

运行环境：独立的 /srv/unsafe/dev-env/venvs/audioldm venv，不与项目主 venv/tools/acestep_env 混用
（理由同 docs/indextts_setup.md）。由 src/asset_gen.py 的 AudioLDMBackend 通过
子进程调用，协议与 tools/tangoflux_infer.py 一致（单次加载、批量循环、
jobs.json -> result.json）。

选型背景：ACE-Step 1.5 本质是音乐生成模型（text2music），用来生成"雨声/矿洞/
风声"这类写实环境录音会带出音乐化的调性音色（见诊断记录：频谱分析显示多条
ambience 素材有异常突出的单一音高主峰）。AudioLDM 的训练数据是真实音频事件
（AudioCaps 等），且是标准 diffusers pipeline，不需要像 ACE-Step 那样 clone
官方仓库、装 ROCm 专用 torch、处理"pip install -e . 会把 torch 换回 CUDA 版"
之类的坑，可以直接用 CPU 推理（模型仅约 0.2B 参数，远小于 ACE-Step XL 档位）。

diffusers 的 AudioLDMPipeline 原生支持 negative_prompt（走 classifier-free
guidance），这是相对 ACE-Step 的实质性修复——之前 negative_prompt 字段从未
真正传给模型。

用法：
    python tools/audioldm_infer.py \
        --checkpoints-dir <HF 缓存根目录（HF_HOME），见下方说明> \
        --jobs-file <输入任务 JSON 路径> \
        --result-file <输出结果 JSON 路径>

jobs-file 内容：[{"id","kind","prompt","negative_prompt","duration_sec","seed",
                  "sample_rate","out"}, ...]（kind 恒为 "ambience"）
result-file 内容：{"<id>": {"ok": true/false, "error": "..."}, ...}

注：AudioLDM 原生输出采样率由 vocoder 配置决定（cvssp/audioldm-s-full-v2 是
16kHz），比 target_sample_rate（24kHz）低；后续统一后处理会重采样，环境音
以低/中频内容为主，不构成实际音质问题。
"""
import argparse
import json
import os
import sys
import traceback

MODEL_NAME = "cvssp/audioldm-s-full-v2"


def main():
    parser = argparse.ArgumentParser(description="AudioLDM 批量推理（环境音）")
    parser.add_argument("--checkpoints-dir", required=True,
                         help="HuggingFace 缓存根目录（HF_HOME）——AudioLDM 用标准 "
                              "diffusers from_pretrained 定位/下载权重，非直接模型路径")
    parser.add_argument("--jobs-file", required=True, help="输入任务列表 JSON 文件路径")
    parser.add_argument("--result-file", required=True, help="输出结果 JSON 文件路径")
    parser.add_argument("--device", default="cpu", help="推理设备，默认 cpu（模型小，CPU 推理可接受，规避 ROCm 兼容性风险）")
    args = parser.parse_args()

    # huggingface_hub 读取 HF_HOME 决定缓存根目录，必须在 import 前设置
    os.environ["HF_HOME"] = os.path.abspath(args.checkpoints_dir)

    with open(args.jobs_file, "r", encoding="utf-8") as f:
        jobs = json.load(f)

    results = {}

    try:
        import torch
        import soundfile as sf
        from diffusers import AudioLDMPipeline
    except Exception as e:  # noqa: BLE001 - 顶层导入失败要让调用方看到明确原因
        for job in jobs:
            results[job["id"]] = {"ok": False, "error": f"导入 diffusers/torch 依赖失败: {e}"}
        _write_results(args.result_file, results)
        sys.exit(1)

    try:
        pipe = AudioLDMPipeline.from_pretrained(MODEL_NAME, torch_dtype=torch.float32)
        pipe = pipe.to(args.device)
        sample_rate = pipe.vocoder.config.sampling_rate
    except Exception as e:  # noqa: BLE001
        err = f"加载 AudioLDM 模型失败: {e}\n{traceback.format_exc()}"
        for job in jobs:
            results[job["id"]] = {"ok": False, "error": err}
        _write_results(args.result_file, results)
        sys.exit(1)

    for job in jobs:
        job_id = job["id"]
        try:
            out_path = job["out"]
            out_dir = os.path.dirname(out_path)
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)

            generator = torch.Generator(device=args.device).manual_seed(int(job["seed"]))
            negative_prompt = job.get("negative_prompt") or None
            audio = pipe(
                job["prompt"],
                negative_prompt=negative_prompt,
                num_inference_steps=10,
                audio_length_in_s=float(job["duration_sec"]),
                generator=generator,
            ).audios[0]
            sf.write(out_path, audio, samplerate=sample_rate)

            ok = os.path.exists(out_path) and os.path.getsize(out_path) > 0
            results[job_id] = {"ok": ok, "error": None if ok else "输出文件未生成或为空"}
        except Exception as e:  # noqa: BLE001 - 单条失败不应中断整批
            results[job_id] = {"ok": False, "error": f"{e}\n{traceback.format_exc()}"}
        finally:
            # 每条任务写一次，方便调用方在超时/中断时读取已完成的部分结果
            _write_results(args.result_file, results)

    sys.exit(0)


def _write_results(path: str, results: dict):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


if __name__ == "__main__":
    main()
