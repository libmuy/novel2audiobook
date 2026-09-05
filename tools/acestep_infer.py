#!/usr/bin/env python3
"""
ACE-Step 1.5 批量推理脚本（BGM / 环境音 ambience）。

运行环境：独立的 tools/acestep_env venv（Python 3.11 + ROCm torch），不与项目主
venv 混用。由 src/asset_gen.py 的 AceStepBackend 通过子进程调用。

设计为"单次加载、批量合成"，理由与 tools/indextts_infer.py 相同：DiT + LM 模型
加载耗时数十秒，若每条素材起一个子进程会被加载开销拖垮，因此一次调用接收一个
任务列表 JSON 文件，加载模型一次后循环合成，逐条写状态到输出 JSON。

用法：
    python tools/acestep_infer.py \
        --repo-dir <ACE-Step-1.5 源码目录，用于 sys.path> \
        --checkpoints-dir <权重目录> \
        --jobs-file <输入任务 JSON 路径> \
        --result-file <输出结果 JSON 路径>

jobs-file 内容：[{"id","kind","prompt","negative_prompt","duration_sec","seed",
                  "sample_rate","out"}, ...]
                （kind 恒为 "ambience"；字段协议见
                 src/asset_gen.py:SubprocessAudioGenBackend.generate_batch）
result-file 内容：{"<id>": {"ok": true/false, "error": "..."}, ...}

注：ACE-Step 的 GenerationParams 没有独立的负向提示词槽位——它靠 caption/lyrics
正向描述内容，音色/质量由 guidance_scale 等参数控制，因此 negative_prompt 字段
在这里不会被使用，仅作为跨后端（Stable Audio 支持负向提示）统一协议保留。
"""
import argparse
import json
import os
import sys
import traceback

# ACE-Step 通过 config_path（模型名）而非直接路径定位权重，实际权重目录由
# ACESTEP_CHECKPOINTS_DIR 环境变量决定（默认落在 project_root/checkpoints 下）。
# 必须在 import acestep.* 之前设置，见 docs/audiogen_setup.md「共享权重目录」一节。
DIT_CONFIG_PATH = "acestep-v15-xl-sft"     # 24GB 显存档位，见 ACE-Step INSTALL.md 选型表
LM_MODEL_PATH = "acestep-5Hz-lm-1.7B"
LM_BACKEND = "pt"                          # ROCm/RDNA3 上 vLLM 兼容性差，pt 后端更稳


def main():
    parser = argparse.ArgumentParser(description="ACE-Step 1.5 批量推理（BGM/环境音）")
    parser.add_argument("--repo-dir", required=True, help="ACE-Step-1.5 源码仓库目录")
    parser.add_argument("--checkpoints-dir", required=True, help="ACE-Step-1.5 权重目录")
    parser.add_argument("--jobs-file", required=True, help="输入任务列表 JSON 文件路径")
    parser.add_argument("--result-file", required=True, help="输出结果 JSON 文件路径")
    parser.add_argument("--device", default="cuda", help="ROCm 下 PyTorch 设备字符串仍是 'cuda'")
    args = parser.parse_args()

    os.environ["ACESTEP_CHECKPOINTS_DIR"] = os.path.abspath(args.checkpoints_dir)
    # ACE-Step 在 ROCm 下默认用 fp32（实测日志会提示 "using dtype=torch.float32
    # (set ACESTEP_ROCM_DTYPE=bfloat16 ... to override)"）；本机 RX 7900XTX 实测
    # bf16 结果正常、显存/内存占用减半、明显更快，见 docs/audiogen_setup.md 已知坑。
    os.environ.setdefault("ACESTEP_ROCM_DTYPE", "bfloat16")
    sys.path.insert(0, args.repo_dir)

    with open(args.jobs_file, "r", encoding="utf-8") as f:
        jobs = json.load(f)

    results = {}

    try:
        import soundfile as sf
        from acestep.handler import AceStepHandler
        from acestep.llm_inference import LLMHandler
        from acestep.inference import GenerationParams, GenerationConfig, generate_music
    except Exception as e:  # noqa: BLE001 - 顶层导入失败要让调用方看到明确原因
        for job in jobs:
            results[job["id"]] = {"ok": False, "error": f"导入 ACE-Step 依赖失败: {e}"}
        _write_results(args.result_file, results)
        sys.exit(1)

    try:
        dit_handler = AceStepHandler()
        dit_handler.initialize_service(
            project_root=args.repo_dir,
            config_path=DIT_CONFIG_PATH,
            device=args.device,
        )
        llm_handler = LLMHandler()
        llm_handler.initialize(
            checkpoint_dir=os.environ["ACESTEP_CHECKPOINTS_DIR"],
            lm_model_path=LM_MODEL_PATH,
            backend=LM_BACKEND,
            device=args.device,
        )
    except Exception as e:  # noqa: BLE001
        err = f"加载 ACE-Step 模型失败: {e}\n{traceback.format_exc()}"
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

            seed = int(job["seed"])
            params = GenerationParams(
                task_type="text2music",
                caption=job["prompt"],
                lyrics="[Instrumental]",
                instrumental=True,
                duration=float(job["duration_sec"]),
                seed=seed,
            )
            config = GenerationConfig(batch_size=1, use_random_seed=False, seeds=[seed])

            result = generate_music(dit_handler, llm_handler, params, config, save_dir=None)
            if not result.success or not result.audios:
                raise RuntimeError(result.error or "生成结果为空（result.audios 为空）")

            audio = result.audios[0]
            tensor = audio["tensor"]
            sample_rate = audio.get("sample_rate", 48000)
            arr = tensor.numpy()
            if arr.ndim == 2:
                arr = arr.T  # tensor 是 [channels, samples]，soundfile 要 [samples, channels]
            sf.write(out_path, arr, sample_rate)

            ok = os.path.exists(out_path) and os.path.getsize(out_path) > 0
            results[job_id] = {"ok": ok, "error": None if ok else "输出文件未生成或为空"}
        except Exception as e:  # noqa: BLE001 - 单条素材失败不应中断整批
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
