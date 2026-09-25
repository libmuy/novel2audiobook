"""
测试 POST /api/frontend-logs：合法批次落统一日志（FRONTEND 行）、截断、
超批/空批/超大 body 400、进程内滑动窗口限流 429
"""
import logging
import os

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api import deps
from src.api.routers import logs as logs_router
from src.runtime.log_setup import setup_logging, reset_logging
from src.runtime.task_queue import TaskQueue


@pytest.fixture
def client(tmp_path, monkeypatch):
    """与 test_request_logging.py 同构的隔离客户端"""
    library_dir = tmp_path / "data" / "library"
    roles_dir = tmp_path / "data" / "roles"
    tasks_dir = tmp_path / "tasks"
    os.makedirs(library_dir)
    os.makedirs(roles_dir)
    os.makedirs(tasks_dir)

    import src.utils
    monkeypatch.setattr(src.utils, "PROJECT_ROOT", str(tmp_path))
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

    logs_router._rate_timestamps.clear()  # 限流窗口是进程级的，测试间要归零

    app = create_app()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def logfile(tmp_path):
    """装配日志到 tmp 文件（logger('frontend') 经 root handler 落盘），结束必还原"""
    path = str(tmp_path / "n2a.log")
    setup_logging({"logging": {"level": "INFO", "file": path}})
    yield path
    reset_logging()


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _post(client, entries):
    return client.post("/api/frontend-logs", json={"entries": entries})


class TestFrontendLogsEndpoint:
    def test_valid_batch_logged(self, client, logfile):
        resp = _post(client, [{
            "message": "TypeError: cannot read property x",
            "stack": "at foo (app.js:10)",
            "level": "error",
            "url": "app.js:10",
            "count": 2,
        }])
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "logged": 1}
        content = _read(logfile)
        assert "FRONTEND x2 app.js:10 | TypeError: cannot read property x | at foo (app.js:10)" in content
        assert "ERROR" in content

    def test_non_error_level_logged_as_info(self, client, logfile):
        resp = _post(client, [{"message": "只是提示", "level": "log"}])
        assert resp.status_code == 200
        line = [l for l in _read(logfile).splitlines() if "FRONTEND" in l][0]
        assert "INFO" in line
        assert "| 只是提示 | -" in line  # 无 stack 时占位

    def test_long_message_truncated(self, client, logfile):
        resp = _post(client, [{"message": "A" * 800, "stack": "S" * 2000}])
        assert resp.status_code == 200
        line = [l for l in _read(logfile).splitlines() if "FRONTEND" in l][0]
        assert "A" * 500 in line
        assert "A" * 501 not in line
        assert "…[截断]" in line
        assert "S" * 1000 in line
        assert "S" * 1001 not in line

    def test_batch_over_20_rejected(self, client, logfile):
        entries = [{"message": f"m{i}"} for i in range(21)]
        assert _post(client, entries).status_code == 400
        assert "FRONTEND" not in _read(logfile)

    def test_empty_entries_rejected(self, client, logfile):
        assert _post(client, []).status_code == 400

    def test_invalid_json_rejected(self, client, logfile):
        resp = client.post("/api/frontend-logs", content=b"not-json{{",
                           headers={"Content-Type": "application/json"})
        assert resp.status_code == 400

    def test_oversized_body_rejected(self, client, logfile):
        resp = _post(client, [{"message": "B" * 70000}])
        assert resp.status_code == 400

    def test_rate_limit_30_batches_then_429(self, client, logfile):
        logs_router._rate_timestamps.clear()
        for i in range(30):
            resp = _post(client, [{"message": f"batch{i}"}])
            assert resp.status_code == 200, f"第 {i + 1} 批应放行"
        resp = _post(client, [{"message": "overflow"}])
        assert resp.status_code == 429
        logs_router._rate_timestamps.clear()
