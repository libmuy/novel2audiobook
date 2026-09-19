"""系统路由"""
import re
from fastapi import APIRouter
from src.api.deps import get_config, set_config
from src import config_store, monitor
from src.utils import resolve_path

router = APIRouter(tags=["system"])

# 允许修改的配置白名单 + 每个键的类型/取值范围。此前 patch_config 除了 voice_only 的
# bool 强转外完全不校验：字符串 "-20" 会原样落进 YAML，到混音时才炸（或悄悄降级）。
# 现在写盘前逐键校验，不合法的进 rejected，文件一个字节都不动。
_CONFIG_SPEC = {
    "tts.engine": ("str",),
    "tts.sample_rate": ("int", 8000, 96000),
    "mixing.output_format": ("choice", ("mp3", "wav", "flac")),
    "mixing.bitrate": ("bitrate",),
    "mixing.voice_only": ("bool",),
    "server.cpu_workers": ("int", 1, 16),
    "server.monitor_interval_ms": ("int", 200, 60000),
    "server.library_root": ("str",),
}
ALLOWED_CONFIG_KEYS = set(_CONFIG_SPEC)

_BITRATE_RE = re.compile(r"^\d{2,3}k$")


def _coerce_config_value(key: str, value):
    """返回 (ok, 规范化后的值, 拒绝原因)。bool 键保持宽松（JSON 字符串 "false" 在
    Python 里是真值，直接落盘会把 voice_only 永久钉死成真）；数值键不接受字符串。"""
    kind, *args = _CONFIG_SPEC[key]
    if kind == "bool":
        if isinstance(value, bool):
            return True, value, None
        if isinstance(value, str):
            return True, value.strip().lower() in ("1", "true", "yes", "on"), None
        return True, bool(value), None
    if kind == "int":
        lo, hi = args
        if isinstance(value, bool) or not isinstance(value, int):
            return False, None, f"必须是整数（{lo}–{hi}）"
        if not lo <= value <= hi:
            return False, None, f"超出范围（{lo}–{hi}）"
        return True, value, None
    if kind == "choice":
        if value not in args[0]:
            return False, None, f"必须是 {' / '.join(args[0])} 之一"
        return True, value, None
    if kind == "bitrate":
        if not isinstance(value, str) or not _BITRATE_RE.match(value):
            return False, None, "必须形如 192k"
        return True, value, None
    if not isinstance(value, str) or not value.strip():  # str
        return False, None, "必须是非空字符串"
    return True, value, None


def _config_file_path() -> str:
    # 故意不做成模块级常量：常量会在 system.py 首次被 import 时把 PROJECT_ROOT
    # 冻结下来，测试里 monkeypatch PROJECT_ROOT 就不生效了（这个坑真的踩过，
    # 见 005 review）。resolve_path() 在每次调用时动态读取。
    return resolve_path("global_config.yaml")


@router.get("/config")
def get_config_endpoint():
    return get_config()


@router.patch("/config")
def patch_config(data: dict):
    """响应形状 {ok, applied_keys, rejected_keys} 保持不变（既有前端/测试依赖），
    并附加 rejected: [{key, reason}] 说明每个被拒的原因。全部被拒仍是 HTTP 200。"""
    config = get_config()
    applied = {}
    rejected = []
    for key, value in data.items():
        if key not in ALLOWED_CONFIG_KEYS:
            rejected.append({"key": key, "reason": "不在允许修改的配置白名单内"})
            continue
        ok, coerced, reason = _coerce_config_value(key, value)
        if not ok:
            rejected.append({"key": key, "reason": reason})
            continue
        applied[key] = coerced

    if applied:
        # 先落盘再改内存：写盘失败时内存配置不会跟磁盘不一致。落盘走 round-trip，
        # 只改被更新的键，文件里的注释原样保留（见 src/config_store.py）
        config_store.round_trip_update(_config_file_path(), applied)
        for key, value in applied.items():
            *parents, leaf = key.split(".")
            section = config
            for p in parents:
                section = section.setdefault(p, {})
            section[leaf] = value
        set_config(config)

    return {
        "ok": bool(applied),
        "applied_keys": list(applied),
        "rejected_keys": [r["key"] for r in rejected],
        "rejected": rejected,
    }


@router.get("/gpu/owner")
def get_gpu_owner():
    from tools.gpu_arbiter import get_current_owner
    return {"owner": get_current_owner()}


@router.post("/gpu/swap")
def swap_gpu(data: dict):
    from tools.gpu_arbiter import plan_swap, get_current_owner
    target = data.get("target")
    plan = plan_swap(target)
    return plan


@router.get("/assets")
def list_assets():
    from src.utils import list_available_assets
    return list_available_assets()


@router.get("/monitor")
def get_monitor_snapshot():
    return monitor.snapshot()
