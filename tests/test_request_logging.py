"""
测试 src/api/middleware.py 请求日志：元数据、request_id 归因、体摘要、级别决策
"""
import logging
import os

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api import deps
from src.api.middleware import _log_level, _body_summary, RequestLoggingMiddleware
from src.runtime.log_setup import setup_logging, reset_logging
from src.runtime.task_queue import TaskQueue


@pytest.fixture
def client(tmp_path, monkeypatch):
    """与 test_api.py 同构的隔离客户端（PROJECT_ROOT/配置/队列全部指向 tmp）"""
    library_dir = tmp_path / "data" / "library"
    roles_dir = tmp_path / "data" / "roles"
    tasks_dir = tmp_path / "tasks"
    os.makedirs(library_dir)
    os.makedirs(roles_dir)
    os.makedirs(tasks_dir)

    import src.utils
    monkeypatch.setattr(src.utils, "PROJECT_ROOT", str(tmp_path))
    # create_app 会按 PROJECT_ROOT 挂载 src/web/static；补一个最小 index 让 "/" 能 200
    static_dir = tmp_path / "src" / "web" / "static"
    os.makedirs(static_dir, exist_ok=True)
    (static_dir / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    import src.domain.derived_index
    monkeypatch.setattr(src.domain.derived_index, "PROJECT_ROOT", str(tmp_path))
    import src.runtime.task_queue as tq_mod
    monkeypatch.setattr(tq_mod, "PROJECT_ROOT", str(tmp_path))

    config = {"server": {"cpu_workers": 2}, "llm": {}, "tts": {}, "mixing": {}}
    deps.set_config(config)
    queue = TaskQueue(config=config, tasks_dir=str(tasks_dir), library_dir=str(library_dir))
    deps.set_queue(queue)

    app = create_app()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def logfile(tmp_path):
    """装配日志到 tmp 文件（含 contextvar 归因 Filter），结束必还原系统流"""
    path = str(tmp_path / "req.log")
    setup_logging({"logging": {"level": "INFO", "file": path}})
    yield path
    reset_logging()


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestRequestMetadata:
    def test_api_get_logged_with_request_id(self, client, logfile):
        resp = client.get("/api/novels")
        assert resp.status_code == 200
        content = _read(logfile)
        assert "REQ GET /api/novels -> 200" in content
        # request_id 由 contextvar Filter 注入（形如 [a1b2c3d4]）
        import re
        assert re.search(r"\[n2a\.access\] \[[0-9a-f]{8}\] REQ GET /api/novels", content)

    def test_query_string_logged(self, client, logfile):
        client.get("/api/novels?foo=bar")
        assert "REQ GET /api/novels?foo=bar -> 200" in _read(logfile)

    def test_json_body_summary_logged(self, client, logfile):
        resp = client.post("/api/novels", json={"title": "日志测试小说"})
        assert resp.status_code == 200
        content = _read(logfile)
        assert "body(application/json," in content
        assert "日志测试小说" in content

    def test_multipart_logs_size_but_not_content(self, client, logfile):
        created = client.post("/api/roles", json={"name": "日志测试角色", "gender": "unknown"})
        assert created.status_code in (200, 201)
        rid = created.json()["role_id"]
        secret = "绝密音频内容-MARKER"
        resp = client.put(
            f"/api/roles/{rid}/reference",
            files={"file": ("ref.txt", secret.encode("utf-8"), "text/plain")},
        )
        assert resp.status_code == 200
        content = _read(logfile)
        assert "multipart/form-data," in content
        # 摘要只记类型与字节数，不出现文件内容
        assert "MARKER" not in content


class TestLogLevels:
    def test_404_is_warning(self, client, caplog):
        with caplog.at_level(logging.INFO, logger="n2a.access"):
            resp = client.get("/api/novels/nosuchnovel")
        assert resp.status_code == 404
        recs = [r for r in caplog.records
                if r.name == "n2a.access" and "REQ GET /api/novels/nosuchnovel" in r.getMessage()]
        assert recs and recs[0].levelname == "WARNING"

    def test_static_is_debug_only(self, client, caplog):
        with caplog.at_level(logging.DEBUG, logger="n2a.access"):
            resp = client.get("/")
        assert resp.status_code == 200
        recs = [r for r in caplog.records if r.name == "n2a.access" and r.getMessage().startswith("REQ GET /")]
        assert recs and recs[0].levelno == logging.DEBUG

    def test_log_level_decision(self):
        assert _log_level("/api/novels", 200) == logging.INFO
        assert _log_level("/api/novels", 404) == logging.WARNING
        assert _log_level("/api/novels", 500) == logging.ERROR
        assert _log_level("/api/novels", None) == logging.ERROR      # 异常未出 response.start
        assert _log_level("/api/events", 200) == logging.DEBUG      # SSE 长连接降噪
        assert _log_level("/js/app.js", 200) == logging.DEBUG       # 静态文件


class TestBodySummary:
    def test_no_body(self):
        scope = {"headers": []}
        assert _body_summary(scope, 0, b"") == ""

    def test_multipart_only_type_and_size(self):
        scope = {"headers": [(b"content-type", b"multipart/form-data; boundary=abc")]}
        out = _body_summary(scope, 1234, b"--abc\r\n...")
        assert "multipart/form-data, 1234B" in out
        assert "boundary" not in out  # 只留类型，不带参数

    def test_json_truncated_and_flattened(self):
        scope = {"headers": [(b"content-type", b"application/json")]}
        payload = ('{"text": "' + "长" * 300 + '热情"}').encode("utf-8")
        out = _body_summary(scope, len(payload), payload)
        assert out.endswith("...") or "..." in out
        assert "\n" not in out

    def test_over_prefix_limit_uses_only_prefix(self):
        scope = {"headers": [(b"content-type", b"application/json")]}
        payload = (b'{"a":"' + b"x" * 4000 + b'"}')
        out = _body_summary(scope, len(payload), payload[:2048])
        # 摘要基于 2KB 前缀，不含尾部
        assert "x" * 2100 not in out


class TestMiddlewareStructure:
    def test_passthrough_for_lifespan_scope(self):
        """非 http scope（lifespan/websocket）必须原样透传，不得打日志"""
        calls = []

        async def inner(scope, receive, send):
            calls.append(scope["type"])

        mw = RequestLoggingMiddleware(inner)

        import asyncio
        asyncio.run(mw({"type": "lifespan"}, None, None))
        assert calls == ["lifespan"]
