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

    import src.preflight as preflight_mod
    monkeypatch.setattr(preflight_mod, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(preflight_mod, "TTS_STATS_PATH", str(tmp_path / "tts_stats.json"))

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


class TestNodesAPI:
    """树的增删改 + reorder + 两步确认删除"""

    def _create_novel(self, client, **levels):
        resp = client.post("/api/novels", json={"title": "树测试", "levels": levels or {"volume": True}})
        return resp.json()["novel_id"]

    def test_create_volume_and_chapter_nodes(self, client):
        nid = self._create_novel(client, volume=True)
        r1 = client.post(f"/api/novels/{nid}/nodes", json={"type": "volume", "title": "第一卷", "parent_id": None})
        assert r1.status_code == 200
        vol_id = r1.json()["node_id"]
        assert vol_id

        r2 = client.post(f"/api/novels/{nid}/nodes",
                         json={"type": "chapter", "title": "第一章", "parent_id": vol_id})
        assert r2.status_code == 200
        ch_id = r2.json()["node_id"]
        assert ch_id.startswith("ch_")

        tree = client.get(f"/api/novels/{nid}/tree").json()["novel"]["tree"]
        assert tree[0]["id"] == vol_id
        assert tree[0]["children"][0]["id"] == ch_id

    def test_create_two_volumes_get_distinct_ids(self, client):
        nid = self._create_novel(client, volume=True)
        r1 = client.post(f"/api/novels/{nid}/nodes", json={"type": "volume", "title": "卷一"})
        r2 = client.post(f"/api/novels/{nid}/nodes", json={"type": "volume", "title": "卷二"})
        assert r1.json()["node_id"] != r2.json()["node_id"]

    def test_tree_inlines_chapter_status(self, client, tmp_path):
        """GET /tree 应该把每个 chapter 节点的 status 内联进去，
        不能让前端自己拼两个平行结构"""
        nid = self._create_novel(client, volume=True)
        vol_id = client.post(f"/api/novels/{nid}/nodes", json={"type": "volume", "title": "卷一"}).json()["node_id"]
        ch_id = client.post(f"/api/novels/{nid}/nodes",
                            json={"type": "chapter", "title": "章一", "parent_id": vol_id}).json()["node_id"]
        client.put(f"/api/novels/{nid}/chapters/{ch_id}/raw", files={"file": ("raw.txt", b"x")})

        tree = client.get(f"/api/novels/{nid}/tree").json()["novel"]["tree"]
        chapter_node = tree[0]["children"][0]
        assert chapter_node["id"] == ch_id
        assert "status" in chapter_node and chapter_node["status"]

    def test_create_node_rejects_disallowed_level(self, client):
        nid = self._create_novel(client, volume=False, part=False)
        resp = client.post(f"/api/novels/{nid}/nodes", json={"type": "volume", "title": "不该存在的卷"})
        assert resp.status_code == 400

    def test_update_node_title(self, client):
        nid = self._create_novel(client, volume=True)
        vol_id = client.post(f"/api/novels/{nid}/nodes",
                             json={"type": "volume", "title": "旧标题"}).json()["node_id"]
        resp = client.patch(f"/api/novels/{nid}/nodes/{vol_id}", json={"title": "新标题"})
        assert resp.status_code == 200
        tree = client.get(f"/api/novels/{nid}/tree").json()["novel"]["tree"]
        assert tree[0]["title"] == "新标题"

    def test_reorder_node(self, client):
        nid = self._create_novel(client, volume=True)
        v1 = client.post(f"/api/novels/{nid}/nodes", json={"type": "volume", "title": "卷一"}).json()["node_id"]
        v2 = client.post(f"/api/novels/{nid}/nodes", json={"type": "volume", "title": "卷二"}).json()["node_id"]
        resp = client.post(f"/api/novels/{nid}/nodes/reorder",
                           json={"node_id": v2, "new_parent_id": None, "new_index": 0})
        assert resp.status_code == 200
        tree = client.get(f"/api/novels/{nid}/tree").json()["novel"]["tree"]
        assert [n["id"] for n in tree] == [v2, v1]

    def test_delete_node_preview_does_not_delete(self, client):
        nid = self._create_novel(client, volume=True)
        vol_id = client.post(f"/api/novels/{nid}/nodes", json={"type": "volume", "title": "卷一"}).json()["node_id"]
        ch_id = client.post(f"/api/novels/{nid}/nodes",
                            json={"type": "chapter", "title": "章一", "parent_id": vol_id}).json()["node_id"]

        preview = client.delete(f"/api/novels/{nid}/nodes/{vol_id}")
        assert preview.status_code == 200
        assert preview.json() == {"affected_chapters": 1, "has_audio": False, "confirmed": False}

        # 没带 confirm，树必须原封不动
        tree = client.get(f"/api/novels/{nid}/tree").json()["novel"]["tree"]
        assert tree[0]["id"] == vol_id
        assert tree[0]["children"][0]["id"] == ch_id

    def test_delete_node_confirmed_moves_chapter_dir_to_trash(self, client, tmp_path):
        nid = self._create_novel(client, volume=True)
        vol_id = client.post(f"/api/novels/{nid}/nodes", json={"type": "volume", "title": "卷一"}).json()["node_id"]
        ch_id = client.post(f"/api/novels/{nid}/nodes",
                            json={"type": "chapter", "title": "章一", "parent_id": vol_id}).json()["node_id"]
        client.put(f"/api/novels/{nid}/chapters/{ch_id}/raw", files={"file": ("raw.txt", b"content")})

        ch_dir = tmp_path / "library" / nid / "chapters" / ch_id
        assert ch_dir.is_dir()

        resp = client.delete(f"/api/novels/{nid}/nodes/{vol_id}?confirm=true")
        assert resp.status_code == 200
        assert resp.json()["confirmed"] is True

        tree = client.get(f"/api/novels/{nid}/tree").json()["novel"]["tree"]
        assert tree == []
        assert not ch_dir.is_dir(), "章节目录不应该原地保留（应移入 .trash/）"
        trash_dir = tmp_path / "library" / nid / ".trash"
        assert trash_dir.is_dir() and len(list(trash_dir.iterdir())) == 1


class TestChaptersAPI:
    def _new_chapter(self, client, nid):
        return client.post(f"/api/novels/{nid}/nodes", json={"type": "chapter", "title": "章一"}).json()["node_id"]

    def test_upload_raw_new_chapter(self, client, tmp_path):
        nid = client.post("/api/novels", json={"title": "章节测试"}).json()["novel_id"]
        ch_id = self._new_chapter(client, nid)
        resp = client.put(f"/api/novels/{nid}/chapters/{ch_id}/raw", files={"file": ("raw.txt", b"hello")})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "confirmed": True}
        raw_path = tmp_path / "library" / nid / "chapters" / ch_id / "raw.txt"
        assert raw_path.read_bytes() == b"hello"

    def test_reimport_without_confirm_previews_and_does_not_touch_files(self, client, tmp_path):
        nid = client.post("/api/novels", json={"title": "重导测试"}).json()["novel_id"]
        ch_id = self._new_chapter(client, nid)
        client.put(f"/api/novels/{nid}/chapters/{ch_id}/raw", files={"file": ("raw.txt", b"old content")})

        ch_dir = tmp_path / "library" / nid / "chapters" / ch_id
        (ch_dir / "script_final.json").write_text("[]")
        (ch_dir / "audio_cache").mkdir()
        (ch_dir / "audio_cache" / "x.wav").write_bytes(b"fake")

        preview = client.put(f"/api/novels/{nid}/chapters/{ch_id}/raw", files={"file": ("raw.txt", b"new content")})
        assert preview.status_code == 200
        body = preview.json()
        assert body["confirmed"] is False
        assert "script_final.json" in body["will_remove"]
        assert body["kept_cache_count"] == 1
        # 没带 confirm，任何文件都不该被动
        assert (ch_dir / "script_final.json").exists()
        assert (ch_dir / "raw.txt").read_bytes() == b"old content"

    def test_reimport_confirmed_clears_downstream_keeps_cache(self, client, tmp_path):
        nid = client.post("/api/novels", json={"title": "重导测试2"}).json()["novel_id"]
        ch_id = self._new_chapter(client, nid)
        client.put(f"/api/novels/{nid}/chapters/{ch_id}/raw", files={"file": ("raw.txt", b"old content")})

        ch_dir = tmp_path / "library" / nid / "chapters" / ch_id
        (ch_dir / "script_final.json").write_text("[]")
        (ch_dir / "audio_cache").mkdir()
        (ch_dir / "audio_cache" / "x.wav").write_bytes(b"fake")

        resp = client.put(f"/api/novels/{nid}/chapters/{ch_id}/raw?confirm=true",
                          files={"file": ("raw.txt", b"new content")})
        assert resp.status_code == 200
        assert not (ch_dir / "script_final.json").exists()
        assert (ch_dir / "audio_cache" / "x.wav").exists()
        assert (ch_dir / "raw.txt").read_bytes() == b"new content"

    def test_timeline_404_before_tts_then_readable(self, client, tmp_path):
        nid = client.post("/api/novels", json={"title": "timeline测试"}).json()["novel_id"]
        ch_id = self._new_chapter(client, nid)
        client.put(f"/api/novels/{nid}/chapters/{ch_id}/raw", files={"file": ("raw.txt", b"x")})

        assert client.get(f"/api/novels/{nid}/chapters/{ch_id}/timeline").status_code == 404

        ch_dir = tmp_path / "library" / nid / "chapters" / ch_id
        (ch_dir / "timeline.json").write_text(json.dumps({"chapter_id": ch_id, "items": [{"seg_id": 1}]}))
        resp = client.get(f"/api/novels/{nid}/chapters/{ch_id}/timeline")
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 1

    def test_segment_audio_404_before_synthesis_then_found(self, client, tmp_path):
        nid = client.post("/api/novels", json={"title": "音频测试"}).json()["novel_id"]
        ch_id = self._new_chapter(client, nid)
        client.put(f"/api/novels/{nid}/chapters/{ch_id}/raw", files={"file": ("raw.txt", b"x")})
        ch_dir = tmp_path / "library" / nid / "chapters" / ch_id
        script = [{"seg_id": 1, "speaker": "narrator", "text": "你好", "emotion": "neutral"}]
        (ch_dir / "script_final.json").write_text(json.dumps(script))

        assert client.get(f"/api/novels/{nid}/chapters/{ch_id}/segments/1/audio").status_code == 404

        from src.utils import calculate_md5
        (ch_dir / "audio_cache").mkdir()
        key = calculate_md5("narrator_你好_neutral")
        (ch_dir / "audio_cache" / f"{key}.wav").write_bytes(b"RIFF....")
        resp = client.get(f"/api/novels/{nid}/chapters/{ch_id}/segments/1/audio")
        assert resp.status_code == 200
        assert resp.content == b"RIFF...."

    def test_segment_audio_unbound_speaker_404(self, client, tmp_path):
        nid = client.post("/api/novels", json={"title": "未绑定测试"}).json()["novel_id"]
        ch_id = self._new_chapter(client, nid)
        client.put(f"/api/novels/{nid}/chapters/{ch_id}/raw", files={"file": ("raw.txt", b"x")})
        ch_dir = tmp_path / "library" / nid / "chapters" / ch_id
        script = [{"seg_id": 1, "speaker": None, "text": "你好", "emotion": "neutral"}]
        (ch_dir / "script_final.json").write_text(json.dumps(script))
        assert client.get(f"/api/novels/{nid}/chapters/{ch_id}/segments/1/audio").status_code == 404

    def test_output_mp3_404_then_found(self, client, tmp_path):
        nid = client.post("/api/novels", json={"title": "成品测试"}).json()["novel_id"]
        ch_id = self._new_chapter(client, nid)
        client.put(f"/api/novels/{nid}/chapters/{ch_id}/raw", files={"file": ("raw.txt", b"x")})

        assert client.get(f"/api/novels/{nid}/chapters/{ch_id}/output.mp3").status_code == 404

        ch_dir = tmp_path / "library" / nid / "chapters" / ch_id
        output_dir = ch_dir / "output"
        output_dir.mkdir()
        (output_dir / "chapter_0001.mp3").write_bytes(b"ID3fake")
        resp = client.get(f"/api/novels/{nid}/chapters/{ch_id}/output.mp3")
        assert resp.status_code == 200
        assert resp.content == b"ID3fake"


class TestSegmentsAPI:
    def _chapter_with_script(self, client, tmp_path, script):
        nid = client.post("/api/novels", json={"title": "分块测试"}).json()["novel_id"]
        ch_id = client.post(f"/api/novels/{nid}/nodes",
                            json={"type": "chapter", "title": "章一"}).json()["node_id"]
        client.put(f"/api/novels/{nid}/chapters/{ch_id}/raw", files={"file": ("raw.txt", b"x")})
        ch_dir = tmp_path / "library" / nid / "chapters" / ch_id
        (ch_dir / "script_final.json").write_text(json.dumps(script))
        return nid, ch_id

    def test_update_segment_speaker(self, client, tmp_path):
        script = [{"seg_id": 1, "speaker": "narrator", "text": "a", "emotion": "neutral"}]
        nid, ch_id = self._chapter_with_script(client, tmp_path, script)
        resp = client.patch(f"/api/novels/{nid}/chapters/{ch_id}/segments/1", json={"speaker": "su_yan"})
        assert resp.status_code == 200
        saved = json.loads((tmp_path / "library" / nid / "chapters" / ch_id / "script_final.json").read_text())
        assert saved[0]["speaker"] == "su_yan"

    def test_update_segment_speaker_null_clears_binding(self, client, tmp_path):
        script = [{"seg_id": 1, "speaker": "narrator", "text": "a", "emotion": "neutral"}]
        nid, ch_id = self._chapter_with_script(client, tmp_path, script)
        resp = client.patch(f"/api/novels/{nid}/chapters/{ch_id}/segments/1", json={"speaker": None})
        assert resp.status_code == 200
        saved = json.loads((tmp_path / "library" / nid / "chapters" / ch_id / "script_final.json").read_text())
        assert saved[0]["speaker"] is None

    def test_batch_update_segments(self, client, tmp_path):
        script = [
            {"seg_id": 1, "speaker": "narrator", "text": "a", "emotion": "neutral"},
            {"seg_id": 2, "speaker": "narrator", "text": "b", "emotion": "neutral"},
        ]
        nid, ch_id = self._chapter_with_script(client, tmp_path, script)
        resp = client.post(f"/api/novels/{nid}/chapters/{ch_id}/segments/batch",
                           json={"seg_ids": [1, 2], "set": {"emotion": "happy"}})
        assert resp.status_code == 200
        assert resp.json()["updated"] == 2
        saved = json.loads((tmp_path / "library" / nid / "chapters" / ch_id / "script_final.json").read_text())
        assert all(seg["emotion"] == "happy" for seg in saved)


class TestRolesAPI:
    def test_list_roles(self, client):
        resp = client.get("/api/roles")
        assert resp.status_code == 200
        assert resp.json() == []  # 隔离目录里没有真实项目的 6 个角色

    def test_delete_narrator_forbidden(self, client):
        resp = client.delete("/api/roles/narrator")
        assert resp.status_code == 400


class TestTasksAPI:
    def test_list_tasks_empty(self, client):
        resp = client.get("/api/tasks")
        assert resp.status_code == 200
        assert resp.json() == []

    def _novel_with_two_chapters(self, client):
        nid = client.post("/api/novels", json={"title": "任务测试", "levels": {"volume": True}}).json()["novel_id"]
        vol_id = client.post(f"/api/novels/{nid}/nodes", json={"type": "volume", "title": "卷一"}).json()["node_id"]
        for _ in range(2):
            ch_id = client.post(f"/api/novels/{nid}/nodes",
                                json={"type": "chapter", "title": "章", "parent_id": vol_id}).json()["node_id"]
            client.put(f"/api/novels/{nid}/chapters/{ch_id}/raw", files={"file": ("raw.txt", b"x")})
        return nid, vol_id

    def test_submit_with_explicit_chapter_ids(self, client):
        nid, vol_id = self._novel_with_two_chapters(client)
        resp = client.post("/api/tasks", json={"type": "mix", "novel_id": nid,
                                               "scope": {"chapter_ids": ["ch_0001"]}})
        assert resp.status_code == 200
        assert len(resp.json()["tasks"]) == 1

    def test_submit_by_node_id_does_not_500(self, client):
        """按"整卷"提交（scope 只给 node_id，不给 chapter_ids）——曾经必现 500"""
        nid, vol_id = self._novel_with_two_chapters(client)
        resp = client.post("/api/tasks", json={"type": "mix", "novel_id": nid,
                                               "scope": {"node_id": vol_id}})
        assert resp.status_code == 200
        assert len(resp.json()["tasks"]) == 2

    def test_submit_whole_book_does_not_500(self, client):
        """按"整本"提交（scope 完全不给 node_id / chapter_ids）——曾经必现 500"""
        nid, vol_id = self._novel_with_two_chapters(client)
        resp = client.post("/api/tasks", json={"type": "mix", "novel_id": nid, "scope": {}})
        assert resp.status_code == 200
        assert len(resp.json()["tasks"]) == 2

    def test_preflight_by_node_id_does_not_500(self, client):
        nid, vol_id = self._novel_with_two_chapters(client)
        resp = client.post("/api/tasks/preflight", json={"type": "mix", "novel_id": nid,
                                                          "scope": {"node_id": vol_id}})
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"chapters", "summary", "invalidated_cache_count", "estimated_gpu_minutes"}
        assert len(body["chapters"]) == 2

    def test_cancel_queued_task(self, client):
        nid, vol_id = self._novel_with_two_chapters(client)
        task = client.post("/api/tasks", json={"type": "mix", "novel_id": nid,
                                               "scope": {"chapter_ids": ["ch_0001"]}}).json()["tasks"][0]
        resp = client.delete(f"/api/tasks/{task['id']}")
        assert resp.status_code == 200


class TestSystemAPI:
    def test_get_config(self, client):
        resp = client.get("/api/config")
        assert resp.status_code == 200
        assert "server" in resp.json()

    def test_patch_config_persists_to_disk(self, client, tmp_path):
        cfg_path = tmp_path / "global_config.yaml"
        cfg_path.write_text("server:\n  cpu_workers: 2\n")

        resp = client.patch("/api/config", json={"server.cpu_workers": 9})
        assert resp.status_code == 200
        assert resp.json()["applied_keys"] == ["server.cpu_workers"]

        import yaml
        on_disk = yaml.safe_load(cfg_path.read_text())
        assert on_disk["server"]["cpu_workers"] == 9

    def test_patch_config_rejects_disallowed_key(self, client):
        resp = client.patch("/api/config", json={"llm.model_name": "hacked"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "llm.model_name" in body["rejected_keys"]

    def test_monitor(self, client):
        resp = client.get("/api/monitor")
        assert resp.status_code == 200
        assert "gpu" in resp.json()

    def test_assets(self, client):
        resp = client.get("/api/assets")
        assert resp.status_code == 200
