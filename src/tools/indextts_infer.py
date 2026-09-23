#!/usr/bin/env python3
"""
IndexTTS-2.5 批量推理脚本。

运行环境：独立的 indextts venv（Python 3.11 + ROCm torch，路径见 config/local_config.yaml 的 tts.index_tts.python_bin），
不与项目主 venv 混用。由 src/pipeline/tts_engine.py 的 IndexTTSBackend 通过子进程调用。

设计为“单次加载、批量合成”：模型加载（GPT/语义编解码器/s2mel/BigVGAN/参考音色
提取等）耗时数十秒，若每句话起一个子进程会被加载开销拖垮，因此一次调用接收一个
任务列表 JSON 文件，加载模型一次后循环合成，逐条写状态到输出 JSON，
方便调用方（Python 主进程）逐条判定成功/失败并按需回退 Mock。

批量模式用法（一次性子进程，`cli.py tts` 走这条路径，不变）：
    python tools/indextts_infer.py \
        --repo-dir <index-tts 源码目录，用于 sys.path> \
        --checkpoints-dir <权重目录> \
        --jobs-file <输入任务 JSON 路径> \
        --result-file <输出结果 JSON 路径>

jobs-file 内容：[{"id": "...", "text": "...", "ref_audio": "...", "lang": "ZH",
                  "emo_vector": [0,0,0,0,0,0,0,0], "duration_factor": 1.0,
                  "out": "输出 wav 绝对路径"}, ...]
result-file 内容：{"<id>": {"ok": true/false, "error": "..."}, ...}

常驻模式用法（计划 003；由 src/pipeline/tts_daemon.py 启动/管理，避免 Tab 4 试听/
webui 每次调用都要付一次模型加载的开销）：
    python tools/indextts_infer.py \
        --repo-dir <...> --checkpoints-dir <...> \
        --serve --socket <unix domain socket 路径>

--serve 模式在 Unix socket 上逐个接受连接，每个连接发一行 JSON 请求、
收一行 JSON 响应后关闭（不并发处理请求——GPU 上的合成本来就该串行）：
    {"cmd": "ping"} -> {"ok": true}
    {"cmd": "synthesize_batch", "jobs": [...]} -> {"ok": true, "results": {...}}
    {"cmd": "shutdown"} -> {"ok": true}（响应后进程退出）
"""
import argparse
import hashlib
import json
import os
import socket
import sys
import tempfile
import traceback
from datetime import datetime, timezone

import torch

EMBEDDING_CACHE_FILENAME = "speaker_embeddings.pt"
# 与 .pt 同目录的纯 JSON 元数据镜像：项目主 venv（src/domain/roles.py 等）不装 torch，
# 没法 torch.load(.pt) 来判断缓存是否还有效，所以把 ref_audio_md5/model_version/
# created_at 额外镜像一份成 JSON，供主 venv 侧只读状态查询用，不参与推理逻辑。
EMBEDDING_META_FILENAME = "speaker_embeddings.meta.json"


# --------------------------------------------------------------------------
# Speaker embedding 预计算/持久化（计划 001）：
#
# IndexTTS2.infer_generator() 内部按 spk_audio_prompt 是否变化决定要不要重新
# 跑 Wav2Vec2Bert + CAMPPlus + length_regulator 提取参考音色特征（见
# infer_v2_5.py 中 cache_spk_cond / cache_spk_audio_prompt 的判断逻辑）。
# 该缓存只在单个进程内有效；本模块把提取结果落盘到参考音频同目录下的
# speaker_embeddings.pt，供下次子进程/常驻服务启动后直接复用，跳过在线提取。
#
# 缓存失效条件：参考音频 MD5 变化、IndexTTS 模型版本变化、或 .pt 缺失/损坏。
# --------------------------------------------------------------------------

def _embedding_cache_path(ref_audio_abspath: str) -> str:
    return os.path.join(os.path.dirname(ref_audio_abspath), EMBEDDING_CACHE_FILENAME)


def _md5_file(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def try_load_cached_embedding(tts, ref_audio_abspath: str) -> bool:
    """
    尝试从磁盘上的 speaker_embeddings.pt 加载预计算的音色缓存并直接填充到
    tts.cache_* 字段，命中时调用方可跳过 tts.infer() 内部的在线提取。
    校验 MD5 与模型版本；任何不一致或加载异常都视为未命中，返回 False，
    调用方应回退到正常的在线提取路径（不中断合成）。
    """
    cache_path = _embedding_cache_path(ref_audio_abspath)
    if not os.path.exists(cache_path):
        return False
    try:
        data = torch.load(cache_path, map_location=tts.device)
    except Exception:
        return False

    required_keys = ("spk_cond_emb", "style", "prompt_condition", "ref_mel", "ref_audio_md5")
    if not all(k in data for k in required_keys):
        return False
    if not os.path.exists(ref_audio_abspath) or data["ref_audio_md5"] != _md5_file(ref_audio_abspath):
        return False
    model_version = getattr(tts, "model_version", None)
    if model_version is not None and data.get("model_version") != model_version:
        return False

    tts.cache_spk_cond = data["spk_cond_emb"]
    tts.cache_s2mel_style = data["style"]
    tts.cache_s2mel_prompt = data["prompt_condition"]
    tts.cache_mel = data["ref_mel"]
    # 必须设为下面 infer_generator 用于判等的确切字符串，否则它会认为参考音频
    # "变了"，刚填好的缓存立刻被清空、重新在线提取（见 infer_v2_5.py 620 行附近）。
    tts.cache_spk_audio_prompt = ref_audio_abspath
    return True


def save_embedding_cache(tts, ref_audio_abspath: str):
    """
    把当前 tts.cache_* 落盘为 speaker_embeddings.pt，供下次复用。
    仅当 tts.cache_spk_audio_prompt 确实等于目标参考音频时才保存，避免批量
    任务中缓存被后续任务覆盖后，错把别的角色的 embedding 当成这个角色存下。
    """
    if getattr(tts, "cache_spk_audio_prompt", None) != ref_audio_abspath or tts.cache_spk_cond is None:
        return
    ref_audio_md5 = _md5_file(ref_audio_abspath)
    model_version = getattr(tts, "model_version", None)
    created_at = datetime.now(timezone.utc).isoformat()

    cache_path = _embedding_cache_path(ref_audio_abspath)
    tmp_path = cache_path + ".tmp"
    torch.save({
        "spk_cond_emb": tts.cache_spk_cond,
        "style": tts.cache_s2mel_style,
        "prompt_condition": tts.cache_s2mel_prompt,
        "ref_mel": tts.cache_mel,
        "ref_audio_md5": ref_audio_md5,
        "model_version": model_version,
        "created_at": created_at,
    }, tmp_path)
    os.replace(tmp_path, cache_path)

    meta_path = os.path.join(os.path.dirname(cache_path), EMBEDDING_META_FILENAME)
    meta_tmp_path = meta_path + ".tmp"
    with open(meta_tmp_path, "w", encoding="utf-8") as f:
        json.dump({
            "ref_audio_md5": ref_audio_md5,
            "model_version": model_version,
            "created_at": created_at,
        }, f, ensure_ascii=False, indent=2)
    os.replace(meta_tmp_path, meta_path)


def extract_and_cache(tts, ref_audio_abspath: str, force: bool = False) -> bool:
    """
    确保 ref_audio_abspath 对应的音色特征已经在 tts.cache_* 中就绪，并落盘。
    force=False 时优先复用磁盘上已有效的 .pt；force=True 或缓存无效时，
    用一句极短占位文本触发一次真实的 infer()（只为复用其内部现成的提取逻辑，
    生成的占位音频写到临时目录后即丢弃），随后落盘。
    返回 True 表示本次确实重新计算了；False 表示直接复用了磁盘上的缓存。
    供 tools/precompute_embeddings.py 调用。
    """
    if not force and try_load_cached_embedding(tts, ref_audio_abspath):
        return False
    tts.cache_spk_audio_prompt = None  # 强制下面这次 infer 走真实在线提取分支
    with tempfile.TemporaryDirectory(prefix="n2a_embed_probe_") as tmp_dir:
        probe_out = os.path.join(tmp_dir, "probe.wav")
        tts.infer(
            spk_audio_prompt=ref_audio_abspath,
            text="测试",
            output_path=probe_out,
            lang="ZH",
            verbose=False,
        )
    save_embedding_cache(tts, ref_audio_abspath)
    return True


def _run_batch(tts, jobs: list, on_job_done=None) -> dict:
    """
    批量合成的核心循环：--jobs-file 一次性模式与 --serve 常驻模式共用同一份
    逻辑（含 embedding 缓存的加载/落盘），避免维护两份容易出现行为分歧的合成
    代码。on_job_done(results) 在每条任务完成后回调一次（一次性模式用它做
    增量写盘，方便调用方在超时/中断时读到已完成的部分结果；常驻模式不需要，
    传 None 即可，最后一次性把整批结果通过 socket 返回）。
    """
    results = {}
    for job in jobs:
        job_id = job["id"]
        try:
            out_path = job["out"]
            out_dir = os.path.dirname(out_path)
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)

            ref_audio = job["ref_audio"]
            # 三种情况：1) 上一条任务就是同一角色，进程内缓存已经是热的，
            # 什么都不用做；2) 磁盘上有该角色预计算好的 speaker_embeddings.pt，
            # 加载后跳过在线提取；3) 都没有，走 tts.infer() 内部的正常在线提取，
            # 提取完再落盘供下次复用（同批次后续任务、下次子进程调用、
            # 常驻服务都能受益）。
            already_warm = tts.cache_spk_audio_prompt == ref_audio
            loaded_from_disk = False if already_warm else try_load_cached_embedding(tts, ref_audio)

            tts.infer(
                spk_audio_prompt=ref_audio,
                text=job["text"],
                output_path=out_path,
                lang=job.get("lang", "ZH"),
                emo_vector=job.get("emo_vector"),
                duration_factor=job.get("duration_factor", 1.0),
                verbose=False,
            )

            if not already_warm and not loaded_from_disk:
                save_embedding_cache(tts, ref_audio)

            ok = os.path.exists(out_path) and os.path.getsize(out_path) > 0
            results[job_id] = {"ok": ok, "error": None if ok else "输出文件未生成或为空"}
        except Exception as e:  # noqa: BLE001 - 单句失败不应中断整批合成
            results[job_id] = {"ok": False, "error": f"{e}\n{traceback.format_exc()}"}
        if on_job_done is not None:
            on_job_done(results)
    return results


def _write_results(path: str, results: dict):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _send_json_line(wfile, obj: dict):
    wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
    wfile.flush()


def _serve_forever(tts, socket_path: str):
    """
    常驻模式主循环：模型已经加载好（调用方保证），在 Unix socket 上逐个接受
    连接，一次处理一个请求（不并发——GPU 上的合成本来就该串行，这也避免了
    多连接同时改 tts.cache_* 造成的竞态）。收到 shutdown 命令后回复并退出，
    由调用方（src/pipeline/tts_daemon.py）负责发起 shutdown 请求 + 兜底 kill。
    """
    if os.path.exists(socket_path):
        os.remove(socket_path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(socket_path)
    srv.listen(1)
    print(f">> 常驻服务已就绪，监听 {socket_path}")
    sys.stdout.flush()

    try:
        while True:
            conn, _ = srv.accept()
            try:
                rf = conn.makefile("rb")
                wf = conn.makefile("wb")
                line = rf.readline()
                if not line:
                    continue
                try:
                    request = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError as e:
                    _send_json_line(wf, {"ok": False, "error": f"请求不是合法 JSON: {e}"})
                    continue

                cmd = request.get("cmd")
                if cmd == "ping":
                    _send_json_line(wf, {"ok": True})
                elif cmd == "synthesize_batch":
                    try:
                        results = _run_batch(tts, request.get("jobs", []))
                        _send_json_line(wf, {"ok": True, "results": results})
                    except Exception as e:  # noqa: BLE001 - 不能让一次请求异常杀死常驻进程
                        _send_json_line(wf, {"ok": False, "error": f"{e}\n{traceback.format_exc()}"})
                elif cmd == "shutdown":
                    _send_json_line(wf, {"ok": True})
                    break
                else:
                    _send_json_line(wf, {"ok": False, "error": f"未知命令: {cmd!r}"})
            finally:
                conn.close()
    finally:
        srv.close()
        if os.path.exists(socket_path):
            os.remove(socket_path)


def _load_model_or_exit(checkpoints_dir: str, use_bf16: bool, jobs_for_error_report, result_file):
    """
    加载 IndexTTS2 模型；批量模式下加载失败要把错误写回 result-file 让调用方
    看到明确原因再退出，常驻模式下没有 result-file，直接打印后退出。
    jobs_for_error_report/result_file 均为 None 时视为常驻模式。
    """
    try:
        from indextts.infer_v2_5 import IndexTTS2
    except Exception as e:  # noqa: BLE001 - 顶层导入失败要让调用方看到明确原因
        if result_file is not None:
            results = {job["id"]: {"ok": False, "error": f"导入 IndexTTS2 失败: {e}"} for job in jobs_for_error_report}
            _write_results(result_file, results)
        else:
            print(f"错误: 导入 IndexTTS2 失败: {e}")
        sys.exit(1)

    try:
        return IndexTTS2(
            cfg_path=os.path.join(checkpoints_dir, "config.yaml"),
            model_dir=checkpoints_dir,
            use_bf16=use_bf16,
        )
    except Exception as e:  # noqa: BLE001
        err = f"加载 IndexTTS2 模型失败: {e}\n{traceback.format_exc()}"
        if result_file is not None:
            results = {job["id"]: {"ok": False, "error": err} for job in jobs_for_error_report}
            _write_results(result_file, results)
        else:
            print(err)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="IndexTTS-2.5 推理（批量一次性 / 常驻两种模式）")
    parser.add_argument("--repo-dir", required=True, help="index-tts 源码仓库目录")
    parser.add_argument("--checkpoints-dir", required=True, help="IndexTTS-2.5 权重目录")
    parser.add_argument("--jobs-file", help="[批量模式] 输入任务列表 JSON 文件路径")
    parser.add_argument("--result-file", help="[批量模式] 输出结果 JSON 文件路径")
    parser.add_argument("--serve", action="store_true", help="常驻模式：加载一次模型后循环从 --socket 接收请求")
    parser.add_argument("--socket", help="[常驻模式] 使用的 Unix domain socket 路径")
    parser.add_argument("--use-bf16", action="store_true", default=True)
    args = parser.parse_args()

    if args.serve:
        if not args.socket:
            print("错误: --serve 模式必须提供 --socket")
            sys.exit(2)
    elif not args.jobs_file or not args.result_file:
        print("错误: 批量模式必须提供 --jobs-file 和 --result-file（或改用 --serve --socket）")
        sys.exit(2)

    sys.path.insert(0, args.repo_dir)
    # IndexTTS2 内部把 HF_HUB_CACHE 硬编码为相对路径 './checkpoints/hf_cache'，
    # 因此必须在导入前把 cwd 切到仓库目录，并确保 repo_dir/checkpoints 是指向
    # 真实权重目录的软链接（见 docs/indextts_setup.md 的环境搭建步骤）。
    os.chdir(args.repo_dir)

    if args.serve:
        tts = _load_model_or_exit(args.checkpoints_dir, args.use_bf16, None, None)
        _serve_forever(tts, args.socket)
        sys.exit(0)

    with open(args.jobs_file, "r", encoding="utf-8") as f:
        jobs = json.load(f)
    tts = _load_model_or_exit(args.checkpoints_dir, args.use_bf16, jobs, args.result_file)
    # 每条任务写一次结果，方便调用方在超时/中断时读取已完成的部分结果
    _run_batch(tts, jobs, on_job_done=lambda r: _write_results(args.result_file, r))
    sys.exit(0)


if __name__ == "__main__":
    main()
