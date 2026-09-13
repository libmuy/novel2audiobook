"""系统路由"""
import os
from fastapi import APIRouter
from src.api.deps import get_config, set_config
from src import monitor
from src.utils import PROJECT_ROOT, load_global_config

router = APIRouter(tags=["system"])

# 允许修改的配置白名单
ALLOWED_CONFIG_KEYS = {
    "tts.engine", "tts.sample_rate",
    "mixing.output_format", "mixing.bitrate",
    "server.cpu_workers", "server.monitor_interval_ms", "server.library_root",
}


@router.get("/config")
def get_config_endpoint():
    return get_config()


@router.patch("/config")
def patch_config(data: dict):
    config = get_config()
    updated = False
    for key, value in data.items():
        if key not in ALLOWED_CONFIG_KEYS:
            continue
        parts = key.split(".")
        section = config
        for p in parts[:-1]:
            section = section.setdefault(p, {})
        section[parts[-1]] = value
        updated = True

    if updated:
        set_config(config)
    return {"ok": updated}


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
