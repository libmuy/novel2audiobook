"""前端 console 错误收集入口

core.js 把 window error / unhandledrejection / console.error 批量 POST 到这里，
以 logger("frontend") 写进统一日志（行首 FRONTEND），让"前端报错"和后端链路
出现在同一个 grep 目标里。防线三层：body ≤64KB、批 ≤20 条、进程内 30 批/分钟。
"""
import json
import logging
import time
from collections import deque

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

router = APIRouter(tags=["frontend_logs"])
logger = logging.getLogger("frontend")

MAX_BODY_BYTES = 64 * 1024
MAX_ENTRIES = 20
MAX_MESSAGE_CHARS = 500
MAX_STACK_CHARS = 1000
RATE_LIMIT_BATCHES = 30
RATE_WINDOW_SEC = 60.0

# 进程内滑动窗口（单进程部署够用；超限直接 429，不落盘不排队）
_rate_timestamps: deque = deque()


def _rate_limited() -> bool:
    now = time.monotonic()
    while _rate_timestamps and now - _rate_timestamps[0] > RATE_WINDOW_SEC:
        _rate_timestamps.popleft()
    if len(_rate_timestamps) >= RATE_LIMIT_BATCHES:
        return True
    _rate_timestamps.append(now)
    return False


class FrontendLogEntry(BaseModel):
    message: str = ""
    stack: str = ""
    level: str = "error"
    url: str = ""
    count: int = 1


class FrontendLogBatch(BaseModel):
    entries: list[FrontendLogEntry] = Field(default_factory=list)


@router.post("/frontend-logs")
async def receive_frontend_logs(request: Request):
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(status_code=400, detail="body exceeds 64KB")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="invalid JSON")
    try:
        batch = FrontendLogBatch.model_validate(data)
    except ValidationError:
        raise HTTPException(status_code=400, detail="invalid batch shape")
    if not batch.entries:
        raise HTTPException(status_code=400, detail="entries is empty")
    if len(batch.entries) > MAX_ENTRIES:
        raise HTTPException(status_code=400, detail="entries exceeds 20 per batch")
    if _rate_limited():
        raise HTTPException(status_code=429, detail="too many batches")

    for entry in batch.entries:
        message, stack = entry.message, entry.stack
        if len(message) > MAX_MESSAGE_CHARS:
            message = message[:MAX_MESSAGE_CHARS] + "…[截断]"
        if len(stack) > MAX_STACK_CHARS:
            stack = stack[:MAX_STACK_CHARS] + "…[截断]"
        level = logging.ERROR if entry.level == "error" else logging.INFO
        count = f" x{entry.count}" if entry.count > 1 else ""
        logger.log(level, "FRONTEND%s %s | %s | %s",
                   count, entry.url or "-", message, stack or "-")
    return {"ok": True, "logged": len(batch.entries)}
