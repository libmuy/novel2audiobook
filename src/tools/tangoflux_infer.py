#!/usr/bin/env python3
"""
TangoFlux 批量推理脚本（音效 sfx）。

运行环境：独立的 tangoflux venv，不与项目主 venv/src/tools/acestep 环境
混用（理由同 docs/indextts_setup.md）。由 src/pipeline/asset_gen.py 的 TangoFluxBackend
通过子进程调用，协议与 src/tools/acestep_infer.py 一致（单次加载、批量循环、
jobs.json -> result.json）。

选型背景：最初计划用 Stable Audio 3 Small SFX，但该模型在 HuggingFace 上是
gated repo，申请访问不是自动通过（实测卡在 403 Forbidden），账号审批流程
太麻烦，遂按原计划里"退路"改用 TangoFlux——公开仓库无需申请、纯 PyTorch、
轻量（3-5GB），个人/研究用途下 non-commercial 许可没有约束（见项目最初确认的
使用场景）。

设备选择：默认 CPU（同 Stable Audio Small 的思路——避免和 llama-server /
ACE-Step 抢显存，SFX 素材生成频率低、单条时长短，CPU 慢一点完全可接受）。

用法：
    python src/tools/tangoflux_infer.py \
        --checkpoints-dir <HF 缓存根目录（HF_HOME），见下方说明> \
        --jobs-file <输入任务 JSON 路径> \
        --result-file <输出结果 JSON 路径>

jobs-file 内容：[{"id","kind","prompt","negative_prompt","duration_sec","seed",
                  "sample_rate","out"}, ...]（kind 恒为 "sfx"）
result-file 内容：{"<id>": {"ok": true/false, "error": "..."}, ...}

注：TangoFlux 的 generate() 不支持 negative_prompt（没有这个参数），也不支持
直接传 seed（内部没暴露该参数）——这里用 torch.manual_seed() 在调用前手动设置
随机种子来达到同等的可复现效果。duration 官方 CLI 限定 1-30 秒，本项目 sfx
素材全部远小于这个上限（asset_specs.yaml 里最长的 sfx 条目也就几秒），不构成
实际约束。
"""
import argparse
import json
import os
import sys
import traceback

STEPS = 50  # 官方 README 建议：50 步质量更好，25 步更快但质量打折；离线批量生成不追求速度


def main():
    parser = argparse.ArgumentParser(description="TangoFlux 批量推理（音效）")
    parser.add_argument("--checkpoints-dir", required=True,
                         help="HuggingFace 缓存根目录（HF_HOME）——TangoFlux 用标准 "
                              "huggingface_hub snapshot_download 定位权重，非直接模型路径")
    parser.add_argument("--jobs-file", required=True, help="输入任务列表 JSON 文件路径")
    parser.add_argument("--result-file", required=True, help="输出结果 JSON 文件路径")
    parser.add_argument("--device", default="cpu", help="推理设备，默认 cpu 避免和 GPU 上的其他任务抢占")
    args = parser.parse_args()

    # huggingface_hub 读取 HF_HOME 决定缓存根目录，必须在 import 前设置
    os.environ["HF_HOME"] = os.path.abspath(args.checkpoints_dir)

    with open(args.jobs_file, "r", encoding="utf-8") as f:
        jobs = json.load(f)

    results = {}

    try:
        import torch
        import torchaudio
        from tangoflux import TangoFluxInference
    except Exception as e:  # noqa: BLE001 - 顶层导入失败要让调用方看到明确原因
        for job in jobs:
            results[job["id"]] = {"ok": False, "error": f"导入 tangoflux 依赖失败: {e}"}
        _write_results(args.result_file, results)
        sys.exit(1)

    try:
        model = TangoFluxInference(name="declare-lab/TangoFlux", device=args.device)
    except Exception as e:  # noqa: BLE001
        err = f"加载 TangoFlux 模型失败: {e}\n{traceback.format_exc()}"
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

            torch.manual_seed(int(job["seed"]))
            audio = model.generate(
                job["prompt"],
                steps=STEPS,
                duration=int(round(float(job["duration_sec"]))),
            )
            torchaudio.save(out_path, audio, sample_rate=44100)

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
