#!/usr/bin/env python3
"""
GPU 显存仲裁：RX 7900XTX 由 llama-server（Qwen3.8，本项目 parse 阶段依赖）与
GPU 密集型批处理阶段——IndexTTS-2.5（tts）、ACE-Step（assets，见
src/asset_gen.py）——共用，这些阶段常规配置下都无法与 llama-server 同时装入显存，
因此本模块保证互斥：批量任务前暂停 llama-server 腾出显存，任务结束后（无论成功
与否）恢复其运行，使系统回到用户预期的默认状态（llama-server 常驻服务于其他用途）。

供 src/tts_engine.IndexTTSBackend、src/asset_gen.SubprocessAudioGenBackend 在各自
批量任务前后调用；也可独立以脚本方式运行：`python tools/gpu_arbiter.py {status|stop|start}`。
"""
import os
import subprocess
import sys
import time
import logging

import requests

logger = logging.getLogger(__name__)


def _find_listening_pid(port: int) -> int:
    """通过 ss 查找监听指定端口的进程 PID，未找到返回 None"""
    try:
        out = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    for line in out.splitlines():
        if f":{port} " in line or line.rstrip().endswith(f":{port}"):
            # 形如 users:(("llama-server",pid=2659,fd=6))
            if "pid=" in line:
                pid_str = line.split("pid=")[1].split(",")[0]
                try:
                    return int(pid_str)
                except ValueError:
                    continue
    return None


def is_server_up(api_base: str, timeout: float = 3.0) -> bool:
    try:
        resp = requests.get(f"{api_base}/models", timeout=timeout)
        return resp.status_code == 200
    except requests.RequestException:
        return False


def stop_llama_server(port: int, wait_sec: float = 15.0) -> bool:
    """停止监听指定端口的 llama-server 进程，返回是否确实停止了一个进程"""
    pid = _find_listening_pid(port)
    if pid is None:
        return False
    logger.info("停止 llama-server (pid=%d, port=%d) 以释放显存给 IndexTTS", pid, port)
    subprocess.run(["kill", str(pid)], check=False)
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        if _find_listening_pid(port) is None:
            return True
        time.sleep(0.5)
    return True  # 已发送 kill，即便进程退出较慢也不阻塞主流程


def start_llama_server(model_registry_name: str, port: int, api_base: str, startup_timeout_sec: float = 180.0) -> bool:
    """通过 `ai llm serve <name> --port <port>` 拉起 llama-server，并等待其响应就绪"""
    if is_server_up(api_base, timeout=2.0):
        return True
    logger.info("重新拉起 llama-server: ai llm serve %s --port %d", model_registry_name, port)
    subprocess.Popen(
        ["ai", "llm", "serve", model_registry_name, "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    deadline = time.time() + startup_timeout_sec
    while time.time() < deadline:
        if is_server_up(api_base, timeout=2.0):
            logger.info("llama-server 已就绪 (port=%d)", port)
            return True
        time.sleep(2.0)
    logger.warning("llama-server 在 %.0fs 内未就绪 (port=%d)，请手动检查", startup_timeout_sec, port)
    return False


class LlmSuspendedForGpu:
    """
    上下文管理器：进入时若 llama-server 正在运行则停止，退出时恢复
    （无论 with 代码块是否抛出异常，均尝试恢复，避免影响其他用途）。
    命名不再局限于 "Tts"——tts/assets 等任何需要独占显存的批处理阶段都可复用。
    """

    def __init__(self, config: dict):
        llm_cfg = config.get("llm", {})
        self.port = llm_cfg.get("serve_port", 8080)
        self.api_base = llm_cfg.get("api_base", f"http://localhost:{self.port}/v1")
        self.model_registry_name = llm_cfg.get("serve_model_registry_name", "")
        self.startup_timeout = llm_cfg.get("server_start_timeout_sec", 180)
        self._was_running = False

    def __enter__(self):
        self._was_running = is_server_up(self.api_base, timeout=2.0)
        if self._was_running:
            stop_llama_server(self.port)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._was_running and self.model_registry_name:
            start_llama_server(self.model_registry_name, self.port, self.api_base, self.startup_timeout)
        return False  # 不吞异常


# 向后兼容别名：src/tts_engine.py 沿用旧名字导入，行为完全一致
LlmSuspendedForTts = LlmSuspendedForGpu


if __name__ == "__main__":
    # 允许直接 `python tools/gpu_arbiter.py` 运行（而非 `python -m tools.gpu_arbiter`），
    # 需手动把项目根目录加入 sys.path 才能找到 src 包。
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from src.utils import load_global_config  # noqa: E402

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = load_global_config()
    llm_cfg = cfg.get("llm", {})
    action = sys.argv[1] if len(sys.argv) > 1 else "status"

    if action == "status":
        up = is_server_up(llm_cfg.get("api_base", "http://localhost:8080/v1"))
        print("llama-server:", "运行中" if up else "未运行")
    elif action == "stop":
        stopped = stop_llama_server(llm_cfg.get("serve_port", 8080))
        print("已停止" if stopped else "未发现运行中的 llama-server")
    elif action == "start":
        ok = start_llama_server(
            llm_cfg.get("serve_model_registry_name", ""),
            llm_cfg.get("serve_port", 8080),
            llm_cfg.get("api_base", "http://localhost:8080/v1"),
            llm_cfg.get("server_start_timeout_sec", 180),
        )
        sys.exit(0 if ok else 1)
    else:
        print(f"未知操作: {action}（可用: status|stop|start）")
        sys.exit(1)
