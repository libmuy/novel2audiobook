"""
常驻 IndexTTS-2.5 推理服务的进程内句柄（计划 003）。

背景：src/tts_engine.IndexTTSBackend 每次调用都起一个全新子进程加载模型
（GPT/语义编解码器/s2mel/BigVGAN/参考音色提取等，耗时数十秒），批量合成整章
可以摊掉这个开销，但交互式试听（未来 webui 的 Tab 4）每次都要重付一次这个
代价，体验上不可接受。本模块管理一个常驻的 `tools/indextts_infer.py --serve`
子进程：加载一次模型，之后通过 Unix socket 反复收发合成请求。

生命周期由调用方（CLI/未来的 webui）显式驱动：
- 启不启动、要不要为此先停掉 llama-server，都通过
  tools.gpu_arbiter.plan_swap() 描述给用户确认后才调用 ensure_started()；
- 本模块自己**不做任何空闲超时自动退出**——卸载显存这件事必须由用户主动
  触发（见 CLAUDE.md/docs/plan/003-*.md 的"显式确认换手"设计），不能在用户
  还在调音色的时候悄悄把模型卸掉。

与 IndexTTSBackend（一次性批量子进程，cli.py tts 走这条路径）完全独立、
互不影响：常驻服务只用于 Tab 4 试听这类交互式小批量请求。
"""
import json
import os
import socket
import subprocess
import time
from datetime import datetime, timezone

from src.utils import resolve_path, load_global_config
from tools import gpu_arbiter

# 有意不在本模块复制一份 pidfile/socket 路径常量：路径/格式由
# tools/gpu_arbiter.py 统一定义（它也要读这份状态来做 owner 探测），本模块
# 处处直接引用 gpu_arbiter.TTS_DAEMON_*，确保读写用的是同一份路径——复制
# 一份字符串常量看似无害，一旦测试/未来重构里只改了一处会立刻读写不一致，
# 见测试里对这一点的专门验证。


class IndexTTSDaemon:
    """
    对常驻 IndexTTS-2.5 推理子进程的启动/复用/关闭封装。

    ensure_started() 是 target_owner="tts" 这次换手的实际执行者（对应
    tools/gpu_arbiter.plan_swap("tts") 描述的步骤）；shutdown() 是换回
    target_owner="llm" 的执行者。两者都会把真实耗时记进
    gpu_arbiter.record_swap_seconds()，供下次 plan_swap() 展示。
    """

    supports_chunking = True  # 常驻 daemon 模型常驻显存，切批成本低

    def __init__(self, config: dict = None):
        self.config = config if config is not None else load_global_config()
        tts_cfg = self.config.get("tts", {}).get("index_tts", {})
        self.python_bin = resolve_path(tts_cfg.get("python_bin", "tools/indextts_env/bin/python"))
        self.infer_script = resolve_path(tts_cfg.get("infer_script", "tools/indextts_infer.py"))
        self.repo_dir = resolve_path(tts_cfg.get("repo_dir", "tools/indextts_repo"))
        self.checkpoints_dir = tts_cfg.get("checkpoints_dir", "/srv/unsafe/models/tts/IndexTTS-2.5")
        self.startup_timeout = tts_cfg.get("daemon_startup_timeout_sec", 180)
        self._llm_cfg = self.config.get("llm", {})

    def is_available(self) -> bool:
        return (
            os.path.exists(self.python_bin)
            and os.path.exists(self.infer_script)
            and os.path.isdir(self.repo_dir)
            and os.path.isdir(self.checkpoints_dir)
        )

    def is_running(self) -> bool:
        return gpu_arbiter.is_tts_daemon_running()

    # ----------------------------------------------------------------
    # 启动（swap -> tts 的执行者）
    # ----------------------------------------------------------------

    def ensure_started(self) -> dict:
        """
        确保常驻服务已启动并加载好模型。已在运行则直接返回（幂等）。
        调用方必须已经在用户确认换手后才调用本函数——本函数本身不弹确认，
        只管执行（对应 tools.gpu_arbiter.plan_swap("tts") 描述的步骤）。
        返回 {"ok": bool, "error": str|None, "already_running": bool}。
        """
        if self.is_running():
            return {"ok": True, "error": None, "already_running": True}
        if not self.is_available():
            return {
                "ok": False,
                "error": "IndexTTS 推理环境未就绪（venv/权重缺失），无法启动常驻服务",
                "already_running": False,
            }

        os.makedirs(gpu_arbiter.TTS_DAEMON_STATE_DIR, exist_ok=True)
        if os.path.exists(gpu_arbiter.TTS_DAEMON_SOCKET_PATH):
            os.remove(gpu_arbiter.TTS_DAEMON_SOCKET_PATH)

        swap_start = time.time()
        llm_was_running = gpu_arbiter.is_server_up(
            self._llm_cfg.get("api_base", f"http://localhost:{self._llm_cfg.get('serve_port', 8080)}/v1"),
            timeout=2.0,
        )
        if llm_was_running:
            gpu_arbiter.stop_llama_server(self._llm_cfg.get("serve_port", 8080))

        proc = subprocess.Popen(
            [
                self.python_bin, self.infer_script,
                "--repo-dir", self.repo_dir, "--checkpoints-dir", self.checkpoints_dir,
                "--serve", "--socket", gpu_arbiter.TTS_DAEMON_SOCKET_PATH,
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )

        if not self._wait_ready(proc, self.startup_timeout):
            if proc.poll() is None:
                proc.kill()
            if llm_was_running:
                self._restart_llm()
            return {
                "ok": False,
                "error": f"常驻服务在 {self.startup_timeout}s 内未就绪（模型加载失败或超时，见子进程日志）",
                "already_running": False,
            }

        elapsed = time.time() - swap_start
        with open(gpu_arbiter.TTS_DAEMON_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "pid": proc.pid,
                "socket_path": gpu_arbiter.TTS_DAEMON_SOCKET_PATH,
                "llm_was_running": llm_was_running,
                "started_at": datetime.now(timezone.utc).isoformat(),
            }, f, ensure_ascii=False, indent=2)

        swap_key = f"{gpu_arbiter.OWNER_LLM if llm_was_running else 'none'}->{gpu_arbiter.OWNER_TTS}"
        gpu_arbiter.record_swap_seconds(swap_key, elapsed)
        return {"ok": True, "error": None, "already_running": False}

    def _wait_ready(self, proc: subprocess.Popen, timeout_sec: float) -> bool:
        """轮询直到子进程能通过 socket 应答 ping，或子进程提前退出/超时"""
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if proc.poll() is not None:
                return False  # 子进程提前退出，必然是加载失败
            if os.path.exists(gpu_arbiter.TTS_DAEMON_SOCKET_PATH):
                try:
                    resp = self._request({"cmd": "ping"}, timeout=5.0)
                    if resp.get("ok"):
                        return True
                except OSError:
                    pass  # socket 文件已 bind 但 listen 还没就绪，正常重试
            time.sleep(1.0)
        return False

    def _restart_llm(self):
        gpu_arbiter.start_llama_server(
            self._llm_cfg.get("serve_model_registry_name", ""),
            self._llm_cfg.get("serve_port", 8080),
            self._llm_cfg.get("api_base", "http://localhost:8080/v1"),
            self._llm_cfg.get("server_start_timeout_sec", 180),
        )

    # ----------------------------------------------------------------
    # 关闭（swap -> llm 的执行者）
    # ----------------------------------------------------------------

    def shutdown(self) -> dict:
        """
        关闭常驻服务：请求子进程优雅退出（连不上就直接按 pid kill 兜底），
        若启动时为了腾显存停过 llama-server，这里负责恢复
        （"回到用户预期的默认状态"）。不做空闲超时自动调用。
        返回 {"ok": bool, "error": str|None, "was_running": bool}。
        """
        state = gpu_arbiter.read_tts_daemon_state()
        if not state:
            return {"ok": True, "error": None, "was_running": False}

        swap_start = time.time()
        try:
            self._request({"cmd": "shutdown"}, timeout=10.0)
        except OSError:
            pass  # 连不上就直接走下面的 pid 兜底

        pid = state.get("pid")
        deadline = time.time() + 15.0
        while pid and gpu_arbiter.is_pid_alive(pid) and time.time() < deadline:
            time.sleep(0.5)
        if pid and gpu_arbiter.is_pid_alive(pid):
            try:
                os.kill(pid, 9)
            except OSError:
                pass

        for path in (gpu_arbiter.TTS_DAEMON_STATE_PATH, gpu_arbiter.TTS_DAEMON_SOCKET_PATH):
            if os.path.exists(path):
                os.remove(path)

        if state.get("llm_was_running"):
            self._restart_llm()

        elapsed = time.time() - swap_start
        gpu_arbiter.record_swap_seconds(f"{gpu_arbiter.OWNER_TTS}->{gpu_arbiter.OWNER_LLM}", elapsed)
        return {"ok": True, "error": None, "was_running": True}

    # ----------------------------------------------------------------
    # 合成请求
    # ----------------------------------------------------------------

    def synthesize_batch(self, jobs: list, timeout: float = 600.0) -> dict:
        """
        jobs: 与 src.tts_engine.IndexTTSBackend.synthesize_batch 相同的入参形状
        （[{"id", "text", "role_cfg", "emotion", "out", "sample_rate"}, ...]），
        这里转换成常驻服务认识的字段并复用 src.tts_engine 里现成的
        EMOTION_TO_VECTOR / speed_to_duration_factor，不重复维护第二份映射表。
        返回 {job_id: bool}，与 IndexTTSBackend 的返回形状一致。
        """
        if not jobs:
            return {}
        if not self.is_running():
            raise RuntimeError("常驻 TTS 服务未运行，请先调用 ensure_started()")

        from src.tts_engine import EMOTION_TO_VECTOR, speed_to_duration_factor

        infer_jobs = []
        for job in jobs:
            role_cfg = job["role_cfg"]
            infer_jobs.append({
                "id": job["id"],
                "text": job["text"],
                "ref_audio": os.path.abspath(role_cfg["reference_audio"]),
                "lang": "ZH",
                "emo_vector": EMOTION_TO_VECTOR.get(job["emotion"], EMOTION_TO_VECTOR["neutral"]),
                "duration_factor": speed_to_duration_factor(role_cfg.get("speed", 1.0)),
                "out": os.path.abspath(job["out"]),
            })

        resp = self._request({"cmd": "synthesize_batch", "jobs": infer_jobs}, timeout=timeout)
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error") or "常驻服务合成失败（无详细错误信息）")
        raw_results = resp.get("results", {})
        return {job["id"]: bool(raw_results.get(job["id"], {}).get("ok")) for job in jobs}

    def _request(self, payload: dict, timeout: float) -> dict:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(gpu_arbiter.TTS_DAEMON_SOCKET_PATH)
            wf = sock.makefile("wb")
            wf.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            wf.flush()
            rf = sock.makefile("rb")
            line = rf.readline()
            if not line:
                raise ConnectionError("常驻服务未返回响应（连接被提前关闭）")
            return json.loads(line.decode("utf-8"))
        finally:
            sock.close()
