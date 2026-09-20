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
import json
import os
import subprocess
import sys
import time
import logging

import requests

logger = logging.getLogger(__name__)

# 与 src/utils.py 里 PROJECT_ROOT 的计算方式一致（都是本文件/模块所在目录的
# 上一级）；这里不 `from src.utils import ...`，沿用本文件一贯的做法——避免
# 在模块顶层引入 src 包依赖（见文件末尾 __main__ 块的说明）。
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --------------------------------------------------------------------------
# 显式 GPU owner 模型（计划 003）：
#
# 原来的 LlmSuspendedForGpu 是"批处理前静默停 llm、批处理后静默恢复"的隐式
# 仲裁，适合一次性批处理（tts/assets 这类无人值守链路），但常驻 TTS 服务
# 一旦启动就会一直占着显存，不能再用这套"用完即还"的模型。这里改为显式模型：
# get_current_owner() 只读探测谁在占用，plan_swap() 描述换手需要做什么、
# 预计多久（不执行），真正的执行落在各自的 owner 里——llm 侧仍是本文件的
# stop_llama_server/start_llama_server，tts 侧是 src/tts_daemon.py 的
# ensure_started()/shutdown()（它们各自调用本文件的 record_swap_seconds()
# 记录真实耗时）。调用方（cli.py/未来的 webui）必须先展示 plan_swap() 的结果
# 让用户确认，再触发对应 owner 的执行函数——本文件不做任何自动仲裁。
# --------------------------------------------------------------------------

OWNER_LLM = "llm"
OWNER_TTS = "tts"

TTS_DAEMON_STATE_DIR = os.path.join(PROJECT_ROOT, ".cache", "tts_daemon")
TTS_DAEMON_STATE_PATH = os.path.join(TTS_DAEMON_STATE_DIR, "state.json")
TTS_DAEMON_SOCKET_PATH = os.path.join(TTS_DAEMON_STATE_DIR, "daemon.sock")

SWAP_HISTORY_PATH = os.path.join(PROJECT_ROOT, ".cache", "gpu_arbiter", "swap_history.json")

# 孤儿恢复标记：记录哪个进程停掉了 llama-server，以便进程被强杀后能恢复
LLM_SUSPENDED_PATH = os.path.join(PROJECT_ROOT, ".cache", "gpu_arbiter", "llm_suspended.json")


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


DEFAULT_SERVE_COMMAND = "ai"


def serve_command_from_config(config: dict) -> str:
    """拉起 llama-server 的外部命令名，取配置 tools.llm_serve_command（local_config.yaml），缺省 ai"""
    return (config.get("tools") or {}).get("llm_serve_command") or DEFAULT_SERVE_COMMAND


def start_llama_server(model_registry_name: str, port: int, api_base: str, startup_timeout_sec: float = 180.0,
                       serve_command: str = DEFAULT_SERVE_COMMAND) -> bool:
    """通过 `<serve_command> llm serve <name> --port <port>`（默认 ai）拉起 llama-server，并等待其响应就绪"""
    if is_server_up(api_base, timeout=2.0):
        return True
    logger.info("重新拉起 llama-server: %s llm serve %s --port %d", serve_command, model_registry_name, port)
    subprocess.Popen(
        [serve_command, "llm", "serve", model_registry_name, "--port", str(port)],
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


def recover_orphaned_suspension(config: dict = None) -> dict:
    """
    检查是否存在「llama-server 被某个已经死掉的进程停用后没人负责恢复」的孤儿状态。
    存在就把 llama-server 拉回来并清掉标记文件。
    返回 {"recovered": bool, "reason": str}。
    只在服务启动/关闭这类明确的时机调用，不要做成后台轮询。
    """
    if not os.path.exists(LLM_SUSPENDED_PATH):
        return {"recovered": False, "reason": "无孤儿标记"}

    try:
        with open(LLM_SUSPENDED_PATH, "r", encoding="utf-8") as f:
            marker = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"recovered": False, "reason": "标记文件损坏"}

    pid = marker.get("pid")
    if pid is None:
        # 标记文件没有 pid，视为损坏
        try:
            os.remove(LLM_SUSPENDED_PATH)
        except OSError:
            pass
        return {"recovered": False, "reason": "标记文件无 pid 字段"}

    if is_pid_alive(pid):
        return {"recovered": False, "reason": f"pid {pid} 仍在运行，非孤儿"}

    # pid 已死，需要恢复 llama-server
    if config is None:
        from src.utils import load_global_config
        config = load_global_config()

    llm_cfg = config.get("llm", {})
    model_registry_name = marker.get("model_registry_name", llm_cfg.get("serve_model_registry_name", ""))
    port = llm_cfg.get("serve_port", 8080)
    api_base = llm_cfg.get("api_base", f"http://localhost:{port}/v1")
    startup_timeout = llm_cfg.get("server_start_timeout_sec", 180)

    ok = start_llama_server(model_registry_name, port, api_base, startup_timeout,
                            serve_command=serve_command_from_config(config))
    try:
        os.remove(LLM_SUSPENDED_PATH)
    except OSError:
        pass

    if ok:
        return {"recovered": True, "reason": f"pid {pid} 已死，已恢复 llama-server"}
    else:
        return {"recovered": False, "reason": f"pid {pid} 已死，但恢复 llama-server 失败"}


class LlmSuspendedForGpu:
    """
    上下文管理器：进入时若 llama-server 正在运行则停止，退出时恢复
    （无论 with 代码块是否抛出异常，均尝试恢复，避免影响其他用途）。
    命名不再局限于 "Tts"——tts/assets 等任何需要独占显存的批处理阶段都可复用。

    这是"一次性批处理"专用的隐式自动换手（tts/assets 的一次性子进程调用），
    行为保持不变；常驻 TTS 服务的显式换手走 get_current_owner()/plan_swap()
    + src/tts_daemon.py，两套机制并存，互不影响。这里额外把 stop/start
    llama-server 各自的真实耗时记进 swap_history，供 plan_swap() 展示。
    """

    def __init__(self, config: dict):
        llm_cfg = config.get("llm", {})
        self.port = llm_cfg.get("serve_port", 8080)
        self.api_base = llm_cfg.get("api_base", f"http://localhost:{self.port}/v1")
        self.model_registry_name = llm_cfg.get("serve_model_registry_name", "")
        self.startup_timeout = llm_cfg.get("server_start_timeout_sec", 180)
        self.serve_command = serve_command_from_config(config)
        self._was_running = False

    def __enter__(self):
        self._was_running = is_server_up(self.api_base, timeout=2.0)
        if self._was_running:
            t0 = time.time()
            stop_llama_server(self.port)
            record_swap_seconds("llm_stop", time.time() - t0)
            # 写孤儿恢复标记：记录当前 pid 和 model_registry_name
            os.makedirs(os.path.dirname(LLM_SUSPENDED_PATH), exist_ok=True)
            marker = {
                "pid": os.getpid(),
                "since": time.strftime("%Y-%m-%d %H:%M:%S"),
                "model_registry_name": self.model_registry_name,
            }
            tmp_path = LLM_SUSPENDED_PATH + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(marker, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, LLM_SUSPENDED_PATH)
        return self

    def __exit__(self, exc_type, exc, tb):
        # 标记文件必须在 start_llama_server 完成（或失败）之后才删——如果先删标记
        # 再重启，进程恰好在 start_llama_server 执行期间（最长可达 startup_timeout
        # 秒）被强杀，孤儿恢复机制就找不到这次停用的痕迹，llama-server 会永久停摆，
        # 这正是 recover_orphaned_suspension 存在的意义。无论恢复成功与否都要删。
        if self._was_running:
            if self.model_registry_name:
                t0 = time.time()
                start_llama_server(self.model_registry_name, self.port, self.api_base, self.startup_timeout,
                                   serve_command=self.serve_command)
                record_swap_seconds("llm_start", time.time() - t0)
            try:
                os.remove(LLM_SUSPENDED_PATH)
            except OSError:
                pass
        return False  # 不吞异常


# 向后兼容别名：src/tts_engine.py 沿用旧名字导入，行为完全一致
LlmSuspendedForTts = LlmSuspendedForGpu


# --------------------------------------------------------------------------
# 常驻 TTS 服务的 pidfile（由 src/tts_daemon.py 写入/删除，本文件只负责读取
# 探测，不负责启停——启停需要知道怎么加载 IndexTTS2 模型，那是 src/ 侧的事）
# --------------------------------------------------------------------------

def read_tts_daemon_state() -> dict:
    """读取常驻 TTS 服务的 pidfile 状态；不存在或损坏都返回 None"""
    if not os.path.exists(TTS_DAEMON_STATE_PATH):
        return None
    try:
        with open(TTS_DAEMON_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def is_pid_alive(pid: int) -> bool:
    """PID 对应的进程是否还活着（供 src/tts_daemon.py 的 shutdown 兜底轮询复用）"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 进程存在但属于别的用户/权限，也算活着
    except OSError:
        return False
    return True


def is_tts_daemon_running() -> bool:
    """常驻 TTS 服务是否仍在运行（pidfile 存在且记录的 PID 真的活着）"""
    state = read_tts_daemon_state()
    if not state or "pid" not in state:
        return False
    return is_pid_alive(state["pid"])


def get_current_owner(config: dict = None) -> str:
    """
    只读探测当前 GPU 显存被谁占用：OWNER_TTS / OWNER_LLM / None（都没检测到）。
    两者同时被探测到时（正常不应该发生）优先返回 OWNER_TTS——常驻服务的生命
    周期由本项目自己的 pidfile 管理，判断更可靠；llama-server 是外部
    `ai llm serve` 命令拉起的，这里只能探测端口是否响应。
    不探测 "assets"：ACE-Step/TangoFlux 都是一次性子进程，跑完即退出，没有
    常驻状态可查（若同时手动在跑 `cli.py assets gen`，需要调用方自行避让）。
    """
    if is_tts_daemon_running():
        return OWNER_TTS
    if config is None:
        from src.utils import load_global_config  # 延迟导入，理由同文件顶部
        config = load_global_config()
    llm_cfg = config.get("llm", {})
    api_base = llm_cfg.get("api_base", f"http://localhost:{llm_cfg.get('serve_port', 8080)}/v1")
    if is_server_up(api_base, timeout=2.0):
        return OWNER_LLM
    return None


# --------------------------------------------------------------------------
# 换手耗时历史：不是靠拍脑袋估算，是把每次真实发生的换手耗时记下来，
# 下次用真实数据告诉用户"大概要等多久"（而不是拿 server_start_timeout_sec
# 这种超时上限充数）。
# --------------------------------------------------------------------------

def _load_swap_history() -> dict:
    if not os.path.exists(SWAP_HISTORY_PATH):
        return {}
    try:
        with open(SWAP_HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def record_swap_seconds(swap_key: str, seconds: float):
    """记录一次真实换手/子步骤耗时，key 例如 'llm_stop'/'llm_start'/'llm->tts'/'tts->llm'"""
    os.makedirs(os.path.dirname(SWAP_HISTORY_PATH), exist_ok=True)
    history = _load_swap_history()
    history[swap_key] = round(seconds, 1)
    tmp_path = SWAP_HISTORY_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, SWAP_HISTORY_PATH)


def get_expected_swap_seconds(swap_key: str) -> float:
    """返回该 key 上次记录的真实耗时；从未记录过返回 None（调用方不应编造数字）"""
    return _load_swap_history().get(swap_key)


def plan_swap(target_owner: str, config: dict = None) -> dict:
    """
    无副作用：描述把 GPU 交给 target_owner 需要做什么、预计多久，供调用方
    （CLI/未来的 webui）展示给用户确认——本函数只描述，不执行。
    target_owner 目前只支持 OWNER_LLM / OWNER_TTS（"assets" 走一次性子进程，
    不在常驻生命周期管理范围内，见 get_current_owner 的说明）。

    返回 {"current_owner", "target_owner", "noop", "steps": [...], "estimated_seconds"}；
    noop=True 表示当前已经是目标 owner，调用方应直接跳过确认、什么都不做。
    真正执行换手：target_owner=="tts" 调 src/tts_daemon.IndexTTSDaemon().ensure_started()，
    target_owner=="llm" 调同一个类的 .shutdown()（把常驻服务关掉、恢复 llama-server）。
    """
    if target_owner not in (OWNER_LLM, OWNER_TTS):
        raise ValueError(f"plan_swap 目前只支持 target_owner in ('llm', 'tts')，收到 {target_owner!r}")

    current = get_current_owner(config)
    if current == target_owner:
        return {"current_owner": current, "target_owner": target_owner, "noop": True,
                "steps": [], "estimated_seconds": 0.0}

    steps = []
    if current == OWNER_LLM:
        steps.append("停止 llama-server")
    elif current == OWNER_TTS:
        steps.append("停止常驻 TTS 服务")
    if target_owner == OWNER_LLM:
        steps.append("启动 llama-server 并等待其就绪")
    else:
        steps.append("启动常驻 TTS 服务并加载 IndexTTS2 模型")

    swap_key = f"{current or 'none'}->{target_owner}"
    return {"current_owner": current, "target_owner": target_owner, "noop": False,
            "steps": steps, "estimated_seconds": get_expected_swap_seconds(swap_key)}


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
            serve_command=serve_command_from_config(cfg),
        )
        sys.exit(0 if ok else 1)
    else:
        print(f"未知操作: {action}（可用: status|stop|start）")
        sys.exit(1)
