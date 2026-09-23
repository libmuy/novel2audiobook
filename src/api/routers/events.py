"""SSE 事件流路由"""
import asyncio
import json
import time
from fastapi import APIRouter
from starlette.responses import StreamingResponse
from src.api.deps import get_queue, get_config
from src.runtime import monitor, gpu_arbiter

router = APIRouter(tags=["events"])

# 资源监控推送间隔的兜底默认值（毫秒）；实际间隔优先读 server.monitor_interval_ms
DEFAULT_MONITOR_INTERVAL_MS = 1000

# GPU 占用方探测会真的去探测 llama-server 端口，不能跟资源快照一样按
# monitor_interval（可以短到 200ms）高频调用；固定至少 3 秒才重新探测一次，
# 之间的资源事件复用上一次探测到的值。
_OWNER_PROBE_MIN_INTERVAL_SEC = 3.0
_owner_cache = {"value": "idle", "at": 0.0}


def _current_owner() -> str:
    now = time.time()
    if now - _owner_cache["at"] >= _OWNER_PROBE_MIN_INTERVAL_SEC:
        try:
            owner = gpu_arbiter.get_current_owner()
        except Exception:
            owner = None
        _owner_cache["value"] = owner or "idle"
        _owner_cache["at"] = now
    return _owner_cache["value"]


@router.get("/events")
async def event_stream():
    loop = asyncio.get_event_loop()
    queue = get_queue()
    event_queue = asyncio.Queue()

    def on_task_event(event):
        try:
            loop.call_soon_threadsafe(event_queue.put_nowait, event)
        except Exception:
            pass

    queue.add_listener(on_task_event)

    async def generate():
        last_resource_push = 0
        try:
            while True:
                now = time.time()

                # 推送资源监控；间隔按当前配置的 server.monitor_interval_ms 读取，
                # 允许设置页保存后立刻生效，不需要重启进程。
                try:
                    cfg = get_config()
                    interval_ms = cfg.get("server", {}).get("monitor_interval_ms", DEFAULT_MONITOR_INTERVAL_MS)
                except Exception:
                    interval_ms = DEFAULT_MONITOR_INTERVAL_MS
                monitor_interval = max(0.2, (interval_ms or DEFAULT_MONITOR_INTERVAL_MS) / 1000.0)

                if now - last_resource_push >= monitor_interval:
                    try:
                        snap = monitor.snapshot()
                        snap["gpu_owner"] = _current_owner()
                        yield f"event: resource\ndata: {json.dumps(snap, ensure_ascii=False)}\n\n"
                    except Exception:
                        pass
                    last_resource_push = now

                # 推送任务事件（有则推，无则等）
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=0.1)
                    yield f"event: task_update\ndata: {json.dumps(event.get('data', {}), ensure_ascii=False, default=str)}\n\n"
                except asyncio.TimeoutError:
                    pass
        finally:
            queue.remove_listener(on_task_event)

    return StreamingResponse(generate(), media_type="text/event-stream")
