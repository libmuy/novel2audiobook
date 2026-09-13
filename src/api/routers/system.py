"""系统路由"""
import os
import yaml
from fastapi import APIRouter
from src.api.deps import get_config, set_config
from src import monitor
from src.utils import resolve_path

router = APIRouter(tags=["system"])

# 允许修改的配置白名单
ALLOWED_CONFIG_KEYS = {
    "tts.engine", "tts.sample_rate",
    "mixing.output_format", "mixing.bitrate",
    "server.cpu_workers", "server.monitor_interval_ms", "server.library_root",
}


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
    config = get_config()
    applied_keys = []
    rejected_keys = []
    for key, value in data.items():
        if key not in ALLOWED_CONFIG_KEYS:
            rejected_keys.append(key)
            continue
        parts = key.split(".")
        section = config
        for p in parts[:-1]:
            section = section.setdefault(p, {})
        section[parts[-1]] = value
        applied_keys.append(key)

    if applied_keys:
        # 写回磁盘，保证重启后配置仍然生效；不能只改内存里的 _config
        config_path = _config_file_path()
        tmp_path = config_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        os.replace(tmp_path, config_path)
        set_config(config)

    return {"ok": bool(applied_keys), "applied_keys": applied_keys, "rejected_keys": rejected_keys}


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
