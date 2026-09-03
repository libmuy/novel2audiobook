#!/usr/bin/env python3
"""
IndexTTS-2.5 批量推理脚本。

运行环境：独立的 tools/indextts_env venv（Python 3.11 + ROCm torch），
不与项目主 venv 混用。由 src/tts_engine.py 的 IndexTTSBackend 通过子进程调用。

设计为“单次加载、批量合成”：模型加载（GPT/语义编解码器/s2mel/BigVGAN/参考音色
提取等）耗时数十秒，若每句话起一个子进程会被加载开销拖垮，因此一次调用接收一个
任务列表 JSON 文件，加载模型一次后循环合成，逐条写状态到输出 JSON，
方便调用方（Python 主进程）逐条判定成功/失败并按需回退 Mock。

用法：
    python tools/indextts_infer.py \
        --repo-dir <index-tts 源码目录，用于 sys.path> \
        --checkpoints-dir <权重目录> \
        --jobs-file <输入任务 JSON 路径> \
        --result-file <输出结果 JSON 路径>

jobs-file 内容：[{"id": "...", "text": "...", "ref_audio": "...", "lang": "ZH",
                  "emo_vector": [0,0,0,0,0,0,0,0], "duration_factor": 1.0,
                  "out": "输出 wav 绝对路径"}, ...]
result-file 内容：{"<id>": {"ok": true/false, "error": "..."}, ...}
"""
import argparse
import json
import os
import sys
import traceback


def main():
    parser = argparse.ArgumentParser(description="IndexTTS-2.5 批量推理")
    parser.add_argument("--repo-dir", required=True, help="index-tts 源码仓库目录")
    parser.add_argument("--checkpoints-dir", required=True, help="IndexTTS-2.5 权重目录")
    parser.add_argument("--jobs-file", required=True, help="输入任务列表 JSON 文件路径")
    parser.add_argument("--result-file", required=True, help="输出结果 JSON 文件路径")
    parser.add_argument("--use-bf16", action="store_true", default=True)
    args = parser.parse_args()

    sys.path.insert(0, args.repo_dir)
    # IndexTTS2 内部把 HF_HUB_CACHE 硬编码为相对路径 './checkpoints/hf_cache'，
    # 因此必须在导入前把 cwd 切到仓库目录，并确保 repo_dir/checkpoints 是指向
    # 真实权重目录的软链接（见 docs/indextts_setup.md 的环境搭建步骤）。
    os.chdir(args.repo_dir)

    with open(args.jobs_file, "r", encoding="utf-8") as f:
        jobs = json.load(f)

    results = {}

    try:
        from indextts.infer_v2_5 import IndexTTS2
    except Exception as e:  # noqa: BLE001 - 顶层导入失败要让调用方看到明确原因
        for job in jobs:
            results[job["id"]] = {"ok": False, "error": f"导入 IndexTTS2 失败: {e}"}
        _write_results(args.result_file, results)
        sys.exit(1)

    try:
        tts = IndexTTS2(
            cfg_path=os.path.join(args.checkpoints_dir, "config.yaml"),
            model_dir=args.checkpoints_dir,
            use_bf16=args.use_bf16,
        )
    except Exception as e:  # noqa: BLE001
        err = f"加载 IndexTTS2 模型失败: {e}\n{traceback.format_exc()}"
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
            tts.infer(
                spk_audio_prompt=job["ref_audio"],
                text=job["text"],
                output_path=out_path,
                lang=job.get("lang", "ZH"),
                emo_vector=job.get("emo_vector"),
                duration_factor=job.get("duration_factor", 1.0),
                verbose=False,
            )
            ok = os.path.exists(out_path) and os.path.getsize(out_path) > 0
            results[job_id] = {"ok": ok, "error": None if ok else "输出文件未生成或为空"}
        except Exception as e:  # noqa: BLE001 - 单句失败不应中断整批合成
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
