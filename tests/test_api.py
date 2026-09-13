"""
测试 src/api/ FastAPI 应用
"""
import json
import os
import io

import pytest
from fastapi.testclient import TestClient
from src.api.app import create_app
from src.api import deps
from src.task_queue import TaskQueue


@pytest.fixture
def client(tmp_path, monkeypatch):
    """创建隔离的测试客户端"""
    library_dir = tmp_path / "library"
    roles_dir = tmp_path / "roles"
    tasks_dir = tmp_path / "tasks"
    os.makedirs(library_dir)
    os.makedirs(roles_dir)
    os.makedirs(tasks_dir)

    # Monkeypatch PROJECT_ROOT and related modules
    import src.utils
    monkeypatch.setattr(src.utils, "PROJECT_ROOT", str(tmp_path))

    import src.derived_index
    monkeypatch.setattr(src.derived_index, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(src.derived_index, "ROLE_REFS_PATH",
                        str(tmp_path / "cache" / "index" / "role_refs.json"))

    import src.task_queue as tq_mod
    monkeypatch.setattr(tq_mod, "PROJECT_ROOT", str(tmp_path))

    config = {"server": {"cpu_workers": 2}, "llm": {}, "tts": {}, "mixing": {}}
    deps.set_config(config)

    queue = TaskQueue(config=config, tasks_dir=str(tasks_dir), library_dir=str(library_dir))
    deps.set_queue(queue)

    app = create_app()
    return TestClient(app, raise_server_exceptions=False)


class TestNovelsAPI:
    def test_list_novels_empty(self, client):
        resp = client.get("/api/novels")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_create_novel(self, client):
        resp = client.post("/api/novels", json={"title": "测试小说"})
        assert resp.status_code == 200
        assert "novel_id" in resp.json()

    def test_get_novel(self, client):
        resp = client.post("/api/novels", json={"title": "测试"})
        nid = resp.json()["novel_id"]
        resp = client.get(f"/api/novels/{nid}")
        assert resp.status_code == 200
        assert resp.json()["title"] == "测试"


class TestRolesAPI:
    def test_list_roles(self, client):
        resp = client.get("/api/roles")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


class TestTasksAPI:
    def test_list_tasks_empty(self, client):
        resp = client.get("/api/tasks")
        assert resp.status_code == 200
        assert resp.json() == []


class TestSystemAPI:
    def test_get_config(self, client):
        resp = client.get("/api/config")
        assert resp.status_code == 200
        assert "server" in resp.json()

    def test_monitor(self, client):
        resp = client.get("/api/monitor")
        assert resp.status_code == 200
        assert "gpu" in resp.json()

    def test_assets(self, client):
        resp = client.get("/api/assets")
        assert resp.status_code == 200
