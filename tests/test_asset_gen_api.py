"""asset_gen 任务类型：API 提交 / 预检 / handler / 进度与取消"""
import os
import time

import pytest
from fastapi.testclient import TestClient

from src import asset_gen, task_queue
from src.api import deps
from src.api.app import create_app
from src.pipeline_errors import TaskCancelled
from src.task_queue import TaskQueue, Task, TaskContext


@pytest.fixture
def client(tmp_path, monkeypatch):
    import src.utils
    monkeypatch.setattr(src.utils, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(task_queue, "PROJECT_ROOT", str(tmp_path))
    (tmp_path / "data" / "assets").mkdir(parents=True)
    (tmp_path / "data" / "assets" / "asset_specs.yaml").write_text(
        "sfx:\n  hit:\n    prompt: a hit\n    duration_sec: 1\n    seed: 1\n"
        "ambience:\n  wind:\n    prompt: wind\n    duration_sec: 3\n    seed: 2\n", encoding="utf-8")
    config = {"server": {"cpu_workers": 1}, "llm": {}, "tts": {}, "mixing": {}}
    deps.set_config(config)
    q = TaskQueue(config=config, tasks_dir=str(tmp_path / "tasks"), library_dir=str(tmp_path / "library"))
    deps.set_queue(q)
    c = TestClient(create_app(), raise_server_exceptions=False)
    c.queue = q
    return c


class TestSubmitApi:
    def test_submit_without_scope_or_novel(self, client):
        """核心回归：以前无章节的任务在 POST /tasks 必现 400「未找到可处理的章节」"""
        resp = client.post("/api/tasks", json={"type": "asset_gen"})
        assert resp.status_code == 200
        task = resp.json()["tasks"][0]
        assert task["chapter_id"] is None and task["novel_id"] is None
        assert task["lane"] == "gpu"

    def test_params_forwarded(self, client):
        resp = client.post("/api/tasks", json={"type": "asset_gen", "params": {"only": ["hit"], "force": True}})
        assert resp.json()["tasks"][0]["params"] == {"only": ["hit"], "force": True}

    @pytest.mark.parametrize("params", [{"bogus": 1}, {"kinds": ["music"]}, {"only": "hit"}])
    def test_bad_params_400(self, client, params):
        assert client.post("/api/tasks", json={"type": "asset_gen", "params": params}).status_code == 400

    def test_precompute_embedding_now_submittable(self, client):
        """顺带修好：这个类型早就有 handler，但以前同一个原因从来没法通过 API 提交"""
        resp = client.post("/api/tasks", json={"type": "precompute_embedding", "params": {"role_id": "narrator"}})
        assert resp.status_code == 200

    def test_precompute_embedding_without_role_id_is_400(self, client):
        """以前缺 role_id 会被接受，变成一个静默空操作的「成功」任务"""
        assert client.post("/api/tasks", json={"type": "precompute_embedding"}).status_code == 400
        assert client.post("/api/tasks", json={"type": "precompute_embedding", "params": {}}).status_code == 400

    def test_chapter_scoped_type_still_requires_novel_id(self, client):
        assert client.post("/api/tasks", json={"type": "mix", "scope": {}}).status_code == 400

    def test_mix_rejects_unknown_param(self, client):
        resp = client.post("/api/tasks", json={"type": "mix", "novel_id": "x", "params": {"nope": 1}})
        assert resp.status_code == 400

    def test_preflight_asset_gen(self, client):
        body = client.post("/api/tasks/preflight", json={"type": "asset_gen"}).json()
        assert body["summary"] == {"create": 2, "overwrite": 0, "skip": 0}
        assert body["chapters"] == [] and body["estimated_gpu_minutes"] is None
        assert {a["name"] for a in body["assets"]} == {"hit", "wind"}

    def test_sse_stream_has_no_novel_filter(self):
        """asset_gen 任务没有 novel_id，SSE 必须照常推——别哪天有人给 events.py 加 novel 过滤"""
        import inspect
        from src.api.routers import events
        assert "novel_id" not in inspect.getsource(events)


class TestHandler:
    def test_handler_forwards_params_and_callbacks(self, monkeypatch):
        calls = {}

        def fake(**kw):
            calls.update(kw)
            return {"generated": ["a"], "fallback": [], "skipped": [], "error": []}

        monkeypatch.setattr(asset_gen, "generate_assets", fake)
        logs = []
        ctx = TaskContext(log_fn=logs.append, progress_fn=lambda *a: None, should_cancel_fn=lambda: False)
        task = Task(id="t", type="asset_gen", lane="gpu",
                    params={"kinds": ["sfx"], "only": ["a"], "force": True})
        task_queue._handle_asset_gen(task, ctx)

        assert calls["kinds"] == ["sfx"] and calls["only"] == {"a"} and calls["force"] is True
        assert calls["progress_cb"] == ctx.progress and calls["should_cancel"] == ctx.should_cancel
        assert logs and "生成 1 条" in logs[0]

    def test_asset_gen_runs_through_queue(self, client, monkeypatch):
        """端到端：真的走 worker 线程 + gpu lane，Mock 引擎，最后 succeeded"""
        mock = asset_gen.MockAudioGenBackend()
        orig = asset_gen.generate_assets
        monkeypatch.setattr(asset_gen, "generate_assets",
                            lambda **kw: orig(backend_map={"ambience": mock, "sfx": mock}, **kw))
        client.queue.start()
        try:
            tid = client.post("/api/tasks", json={"type": "asset_gen"}).json()["tasks"][0]["id"]
            for _ in range(100):
                t = client.get(f"/api/tasks/{tid}").json()
                if t["state"] in ("succeeded", "failed", "cancelled"):
                    break
                time.sleep(0.1)
            assert t["state"] == "succeeded", t
            assert t["progress"]["done"] == t["progress"]["total"] == 2
        finally:
            client.queue.stop()


class TestGenerateAssetsCallbacks:
    def _specs(self):
        return {"sfx": {"a": {"prompt": "x", "duration_sec": 1.0, "seed": 1, "description": "", "negative_prompt": ""},
                        "b": {"prompt": "y", "duration_sec": 1.0, "seed": 2, "description": "", "negative_prompt": ""}},
                "ambience": {"c": {"prompt": "z", "duration_sec": 2.0, "seed": 3, "description": "", "negative_prompt": ""}}}

    def _run(self, tmp_path, **kw):
        mock = asset_gen.MockAudioGenBackend()
        return asset_gen.generate_assets(specs=self._specs(), assets_dir=str(tmp_path),
                                         backend_map={"ambience": mock, "sfx": mock}, config={}, **kw)

    def test_progress_is_monotonic_with_stable_total(self, tmp_path):
        events = []
        self._run(tmp_path, progress_cb=lambda d, t, m: events.append((d, t)))
        assert events and all(t == 3 for _, t in events)
        dones = [d for d, _ in events]
        assert dones == sorted(dones) and dones[-1] == 3

    def test_total_excludes_cache_hits(self, tmp_path):
        self._run(tmp_path)
        events = []
        self._run(tmp_path, progress_cb=lambda d, t, m: events.append((d, t)))
        assert events == []  # 全部命中缓存，没有要生成的，也就没有进度事件

    def test_cancel_before_start_raises_and_writes_nothing(self, tmp_path):
        with pytest.raises(TaskCancelled):
            self._run(tmp_path, should_cancel=lambda: True)
        assert not list(tmp_path.rglob("*.wav"))

    def test_cancel_between_kinds_keeps_finished_kind(self, tmp_path):
        def cancel():
            # 第一个 kind（ambience，VALID_KINDS 顺序）的素材落盘后才取消，即「第二个 kind 开始前取消」。
            # 按语义判断而不是数「第几次检查」：每个 kind 内的检查次数会随实现增减（阶段 12 加了一次）。
            return (tmp_path / "ambience" / "c.wav").exists()

        with pytest.raises(TaskCancelled):
            self._run(tmp_path, should_cancel=cancel)
        assert (tmp_path / "ambience" / "c.wav").exists()
        assert not (tmp_path / "sfx" / "a.wav").exists()
