"""SSE 事件流路由"""
import asyncio
import json
import time
from fastapi import APIRouter
from starlette.responses import StreamingResponse
from src.api.deps import get_queue
from src import monitor

router = APIRouter(tags=["events"])

# 资源监控推送间隔（毫秒）
DEFAULT_MONITOR_INTERVAL_MS = 1000


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
        monitor_interval = DEFAULT_MONITOR_INTERVAL_MS / 1000.0
        try:
            while True:
                now = time.time()

                # 推送资源监控
                if now - last_resource_push >= monitor_interval:
                    try:
                        snap = monitor.snapshot()
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
