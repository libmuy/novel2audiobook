"""
日志装配：文件轮转 sink + 终端 handler + print/stderr tee + request/task 归因过滤器。

设计要点（docs/superpowers/specs/2026-09-25-logging-design.md）：
1. 文件侧只有一个写入权威 _FileSink（自带锁与轮转）：logging 的 _SinkHandler 与
   stdout/stderr 的 tee 都写它，两条路径互不重复、轮转不打架。
2. console handler 绑定 tee 安装**前**的原始 stderr，logging 输出不经过 tee，
   因此不会被抄进文件第二遍（终端全量显示，文件每条恰好一份）。
3. request_id/task_id 走 contextvar + logging Filter 注入，任意模块（含领域层、
   任务线程）的日志自动归因；请求/任务之外为空串，不占位。
4. 日志绝不打断主流程：sink 写入、tee 写入的异常一律吞掉。
"""
import contextvars
import logging
import os
import sys
import threading

# 归因 contextvar：默认空串，Filter 里转成 " [id]" 或空
request_id_var = contextvars.ContextVar("n2a_request_id", default="")
task_id_var = contextvars.ContextVar("n2a_task_id", default="")

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s]%(request_id)s%(task_id)s %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
DEFAULT_LOG_FILE = ".cache/logs/n2a.log"  # 相对项目根（用 utils.resolve_path 动态解析）

# setup/reset 的进程级状态（幂等与还原用）
_state = {
    "installed": False,
    "orig_stdout": None,
    "orig_stderr": None,
    "prev_root_level": None,  # 卸载时还原 root level，避免影响后续测试
    "handlers": [],  # 本模块装到 root 上的 handler，卸载时只动自己的
}


class _ContextFilter(logging.Filter):
    """把 request_id/task_id 注入每条 record；无上下文时为空串（格式里不占位）"""

    def filter(self, record):
        rid = request_id_var.get()
        tid = task_id_var.get()
        record.request_id = f" [{rid}]" if rid else ""
        record.task_id = f" [{tid}]" if tid else ""
        return True


class _FileSink:
    """日志文件唯一写入方：自带锁 + 按大小轮转（仿 RotatingFileHandler 语义）。

    轮转命名：n2a.log → n2a.log.1 → ... → n2a.log.{backup_count}（最老的删除）。
    """

    def __init__(self, path: str, max_bytes: int, backup_count: int):
        self.path = path
        self.max_bytes = int(max_bytes) if max_bytes else 0
        self.backup_count = max(0, int(backup_count))
        self._lock = threading.Lock()
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._fp = open(path, "a", encoding="utf-8")

    def write(self, text: str) -> None:
        try:
            with self._lock:
                if self.max_bytes and self._fp.tell() + len(text.encode("utf-8")) > self.max_bytes:
                    self._rollover()
                self._fp.write(text)
                self._fp.flush()
        except Exception:
            pass  # 日志失败绝不影响主流程

    def _rollover(self) -> None:
        self._fp.close()
        if self.backup_count <= 0:
            try:
                os.remove(self.path)
            except OSError:
                pass
        else:
            oldest = f"{self.path}.{self.backup_count}"
            if os.path.exists(oldest):
                try:
                    os.remove(oldest)
                except OSError:
                    pass
            for i in range(self.backup_count - 1, 0, -1):
                src = f"{self.path}.{i}"
                if os.path.exists(src):
                    try:
                        os.replace(src, f"{self.path}.{i + 1}")
                    except OSError:
                        pass
            try:
                os.replace(self.path, f"{self.path}.1")
            except OSError:
                pass
        self._fp = open(self.path, "a", encoding="utf-8")

    def close(self) -> None:
        try:
            with self._lock:
                self._fp.close()
        except Exception:
            pass


class _SinkHandler(logging.Handler):
    """把格式化后的记录写进 _FileSink（与 tee 共用同一个 sink 与锁）"""

    def __init__(self, sink: "_FileSink", formatter: logging.Formatter, log_filter: logging.Filter):
        super().__init__()
        self._sink = sink
        self.setFormatter(formatter)
        self.addFilter(log_filter)

    def emit(self, record) -> None:
        try:
            self._sink.write(self.format(record) + "\n")
        except Exception:
            pass

    def close(self) -> None:
        try:
            super().close()
        except Exception:
            pass


class _TeeStream:
    """把对 sys.stdout/sys.stderr 的裸写（print、第三方库）同时抄进 _FileSink。

    通过 __getattr__ 代理原始流的其余属性（buffer/fileno/isatty/encoding 等），
    不破坏 tqdm、ANSI 颜色、pytest capsys 这类依赖。
    """

    def __init__(self, original, sink: "_FileSink"):
        object.__setattr__(self, "_original", original)
        object.__setattr__(self, "_sink", sink)

    def write(self, data):
        try:
            self._original.write(data)
        except Exception:
            pass
        try:
            if data:
                if isinstance(data, str):
                    self._sink.write(data)
                else:
                    self._sink.write(data.decode("utf-8", "replace"))
        except Exception:
            pass
        return len(data)

    def flush(self):
        try:
            self._original.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_original"), name)


def _resolve_level(raw) -> int:
    if raw is None:
        return logging.INFO
    level = getattr(logging, str(raw).strip().upper(), None)
    if isinstance(level, int):
        return level
    logging.getLogger(__name__).warning("logging.level=%r 非法，回退 INFO", raw)
    return logging.INFO


def _uninstall() -> None:
    """移除本模块装的 handler、还原被 tee 的系统流。幂等。"""
    root = logging.getLogger()
    if _state["installed"] and _state["prev_root_level"] is not None:
        root.setLevel(_state["prev_root_level"])
    _state["prev_root_level"] = None
    for handler in _state["handlers"]:
        try:
            root.removeHandler(handler)
            handler.close()
        except Exception:
            pass
        # _SinkHandler 持有的 sink 单独关（handler.close 不知道 sink）
        sink = getattr(handler, "_sink", None)
        if sink is not None:
            sink.close()
    _state["handlers"] = []

    for name in ("orig_stdout", "orig_stderr"):
        orig = _state[name]
        stream_name = name.replace("orig_", "")
        current = getattr(sys, stream_name, None)
        # 只在当前流仍是我们的 tee 时还原，避免覆盖测试（capsys）之后的替换
        if orig is not None and isinstance(current, _TeeStream):
            setattr(sys, stream_name, orig)
        _state[name] = None
    _state["installed"] = False


def setup_logging(config: dict = None) -> dict:
    """装配日志。幂等（重复调用=先卸再装，取最新配置）。

    config 缺省时读 load_global_config()；`logging.file` 为 null/空则只打终端。
    返回生效配置摘要 {level, file}，便于调用方打印启动信息。
    """
    if config is None:
        from src.utils import load_global_config
        config = load_global_config()
    cfg = (config or {}).get("logging") or {}

    level = _resolve_level(cfg.get("level"))
    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)
    log_filter = _ContextFilter()

    _uninstall()  # 幂等：先清掉上一次装的

    root = logging.getLogger()
    _state["prev_root_level"] = root.level  # 卸载时还原（测试隔离）
    root.setLevel(level)

    # 1) 终端 handler：绑定**当前**（=tee 安装前的）原始 stderr
    orig_stdout, orig_stderr = sys.stdout, sys.stderr
    console = logging.StreamHandler(orig_stderr)
    console.setFormatter(formatter)
    console.addFilter(log_filter)
    root.addHandler(console)
    _state["handlers"].append(console)

    # 2) 文件 sink + logging 文件 handler；日志文件不可用时降级为只打终端
    sink = None
    file_cfg = cfg.get("file", DEFAULT_LOG_FILE)
    if file_cfg:
        try:
            from src.utils import resolve_path
            sink = _FileSink(
                resolve_path(str(file_cfg)),
                cfg.get("max_bytes", 5242880),
                cfg.get("backup_count", 3),
            )
            file_handler = _SinkHandler(sink, formatter, log_filter)
            root.addHandler(file_handler)
            _state["handlers"].append(file_handler)
        except Exception as e:
            logging.getLogger(__name__).warning("日志文件不可用（%s），降级为只打终端: %s", file_cfg, e)
            sink = None

    # 3) tee：仅在有文件 sink 时替换系统流（print / 裸写 stderr 落盘）
    if sink is not None:
        sys.stdout = _TeeStream(orig_stdout, sink)
        sys.stderr = _TeeStream(orig_stderr, sink)

    _state["installed"] = True
    _state["orig_stdout"] = orig_stdout
    _state["orig_stderr"] = orig_stderr
    return {"level": logging.getLevelName(level), "file": str(file_cfg) if sink else None}


def reset_logging() -> None:
    """卸载本模块的 handler 并还原系统流（测试 teardown / 进程收尾用）。幂等。"""
    _uninstall()


def is_installed() -> bool:
    return _state["installed"]
