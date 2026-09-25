"""
纯 ASGI 请求日志中间件：方法/路径/查询/状态码/耗时 + 请求体摘要，request_id 归因。

为什么不用 Starlette 的 BaseHTTPMiddleware：SSE（/api/events，StreamingResponse）
需要端到端流式，BaseHTTPMiddleware 会多包一层任务且有已知的流式/断连坑；纯 ASGI
只旁路观察，不缓冲响应体。

设计要点（docs/superpowers/specs/2026-09-25-logging-design.md）：
- 包装 receive 只做"计数 + 保留 2KB 前缀"，不缓存整包（角色参考音频是 MB 级 WAV）
- JSON/表单/文本记 200 字摘要；multipart 只记类型+字节数，不解析内容
- 4xx→WARNING、5xx→ERROR；静态文件与 /api/events(SSE) 一律 DEBUG（默认 INFO 下不可见）
- 不记录请求头（含潜在敏感头）
"""
import logging
import time
import uuid

from src.runtime.log_setup import request_id_var

logger = logging.getLogger("n2a.access")

MAX_BODY_PREFIX = 2048      # 只保留前 2KB 用于摘要
BODY_SUMMARY_LIMIT = 200    # 摘要文本截断长度
_SSE_PATH = "/api/events"
_STATIC_EXTS = (".js", ".css", ".html", ".woff2", ".svg", ".png", ".ico", ".map", ".txt")


def _header(scope, name: bytes) -> str:
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1", "replace")
    return ""


def _log_level(path: str, status) -> int:
    """级别决策独立成函数，便于单测：静态/SSE → DEBUG；4xx→WARNING；5xx/异常→ERROR；其余 INFO"""
    is_api = path.startswith("/api/")
    if not is_api or path == _SSE_PATH:
        return logging.DEBUG
    if status is None:
        return logging.ERROR
    if status >= 500:
        return logging.ERROR
    if status >= 400:
        return logging.WARNING
    return logging.INFO


def _body_summary(scope, size: int, prefix: bytes) -> str:
    if size <= 0:
        return ""
    ctype = _header(scope, b"content-type").split(";")[0].strip().lower() or "unknown"
    if ctype.startswith("multipart/"):
        return f" | body({ctype}, {size}B)"          # 只记类型与大小，不解析内容
    text = prefix.decode("utf-8", "replace")
    if ctype.startswith(("application/json", "text/", "application/x-www-form-urlencoded")) \
            or ctype == "unknown":
        text = " ".join(text.split())                 # 压掉换行与连续空白
        if len(text) > BODY_SUMMARY_LIMIT:
            text = text[:BODY_SUMMARY_LIMIT] + "..."
        return f" | body({ctype}, {size}B): {text}" if text else f" | body({ctype}, {size}B)"
    return f" | body({ctype}, {size}B)"


class RequestLoggingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "?")
        query = scope.get("query_string", b"") or b""
        rid = uuid.uuid4().hex[:8]

        # 路由内可通过 request.state.request_id 拿到；日志行则靠 contextvar Filter 注入
        scope.setdefault("state", {})["request_id"] = rid
        token = request_id_var.set(rid)

        body = {"size": 0, "prefix": b""}

        async def receive_wrapper():
            message = await receive()
            if message.get("type") == "http.request":
                chunk = message.get("body", b"") or b""
                body["size"] += len(chunk)
                room = MAX_BODY_PREFIX - len(body["prefix"])
                if room > 0:
                    body["prefix"] += chunk[:room]
            return message

        status_box = {"status": None}

        async def send_wrapper(message):
            if message.get("type") == "http.response.start":
                status_box["status"] = message.get("status")
            await send(message)

        errored = False
        started = time.perf_counter()
        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        except Exception:
            errored = True   # 记一条 500 再原样抛出，交给上层 ServerError 中间件
            raise
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            status = status_box["status"] or (500 if errored else None)
            status_text = str(status) if status is not None else "-"
            qs = f"?{query.decode('latin-1', 'replace')}" if query else ""
            line = (f"REQ {method} {path}{qs} -> {status_text} {elapsed_ms:.0f}ms"
                    f"{_body_summary(scope, body['size'], body['prefix'])}")
            logger.log(_log_level(path, status), line)
            request_id_var.reset(token)
