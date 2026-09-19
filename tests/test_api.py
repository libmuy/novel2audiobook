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

    def test_output_mp3_returns_newest_file_not_alphabetically_first(self, client, tmp_path):
        """API 触发的混音输出文件名是 {novel_id}_{chapter_id}，CLI 触发的是历史
        遗留命名 chapter_XXXX——字母序 "chapter_" 排在小说 id 前面，按字母序取
        第一个文件会稳定地把过期文件当成最新成品返回。必须按 mtime 取最新。"""
        import time
        nid = client.post("/api/novels", json={"title": "新旧文件测试"}).json()["novel_id"]
        ch_id = self._new_chapter(client, nid)
        client.put(f"/api/novels/{nid}/chapters/{ch_id}/raw", files={"file": ("raw.txt", b"x")})
        output_dir = tmp_path / "library" / nid / "chapters" / ch_id / "output"
        output_dir.mkdir(parents=True)
        (output_dir / "chapter_0001.mp3").write_bytes(b"OLD")
        time.sleep(0.05)
        (output_dir / f"{nid}_{ch_id}.mp3").write_bytes(b"NEW")

        resp = client.get(f"/api/novels/{nid}/chapters/{ch_id}/output.mp3")
        assert resp.status_code == 200
        assert resp.content == b"NEW"

    def test_chapter_assets_reports_referenced_and_missing(self, client, tmp_path):
        nid = client.post("/api/novels", json={"title": "素材引用测试"}).json()["novel_id"]
        ch_id = self._new_chapter(client, nid)
        client.put(f"/api/novels/{nid}/chapters/{ch_id}/raw", files={"file": ("raw.txt", b"x")})
        ch_dir = tmp_path / "library" / nid / "chapters" / ch_id
        timeline = {
            "chapter_id": ch_id,
            "items": [
                {"seg_id": 1, "bgm": "rain_heavy", "sfx": "sword_clash"},
                {"seg_id": 2, "bgm": "rain_heavy", "sfx": None},
            ],
        }
        (ch_dir / "timeline.json").write_text(json.dumps(timeline))
        # 只让 sword_clash 真实存在，rain_heavy 缺失
        sfx_dir = tmp_path / "assets" / "sfx"
        sfx_dir.mkdir(parents=True)
        (sfx_dir / "sword_clash.wav").write_bytes(b"fake")

        resp = client.get(f"/api/novels/{nid}/chapters/{ch_id}/assets")
        assert resp.status_code == 200
        body = resp.json()
        assert body["referenced"]["bgm"] == ["rain_heavy"]
        assert body["referenced"]["sfx"] == ["sword_clash"]
        assert body["missing"]["bgm"] == ["rain_heavy"]
        assert body["missing"]["sfx"] == []
        assert body["segment_counts"] == {"with_bgm": 2, "with_sfx": 1, "total": 2}
        assert body["mix"]["mixed_with_assets"] is None  # 还没混过音，没有 sidecar

    def test_refresh_timeline_assets_syncs_from_script(self, client, tmp_path):
        nid = client.post("/api/novels", json={"title": "同步测试"}).json()["novel_id"]
        ch_id = self._new_chapter(client, nid)
        client.put(f"/api/novels/{nid}/chapters/{ch_id}/raw", files={"file": ("raw.txt", b"x")})
        ch_dir = tmp_path / "library" / nid / "chapters" / ch_id
        script = [{"seg_id": 1, "speaker": "narrator", "text": "a", "emotion": "neutral",
                   "sfx": "sword_clash", "bgm": None}]
        (ch_dir / "script_final.json").write_text(json.dumps(script))
        timeline = {"chapter_id": ch_id, "items": [{"seg_id": 1, "sfx": None, "bgm": None}]}
        (ch_dir / "timeline.json").write_text(json.dumps(timeline))

        resp = client.post(f"/api/novels/{nid}/chapters/{ch_id}/timeline/refresh-assets")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "updated": 1}
        saved = json.loads((ch_dir / "timeline.json").read_text())
        assert saved["items"][0]["sfx"] == "sword_clash"


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

    def _make_available(self, tmp_path, kind, name):
        # list_available_assets() 扫描 assets/sfx、assets/ambience 目录，
        # 公开字段名是 bgm/sfx，内部目录名是 ambience/sfx——见 src/utils.py
        dirname = "ambience" if kind == "bgm" else "sfx"
        d = tmp_path / "assets" / dirname
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.wav").write_bytes(b"fake wav")

    def test_update_segment_sfx_valid_name(self, client, tmp_path):
        self._make_available(tmp_path, "sfx", "sword_clash")
        script = [{"seg_id": 1, "speaker": "narrator", "text": "a", "emotion": "neutral"}]
        nid, ch_id = self._chapter_with_script(client, tmp_path, script)
        resp = client.patch(f"/api/novels/{nid}/chapters/{ch_id}/segments/1", json={"sfx": "sword_clash"})
        assert resp.status_code == 200
        saved = json.loads((tmp_path / "library" / nid / "chapters" / ch_id / "script_final.json").read_text())
        assert saved[0]["sfx"] == "sword_clash"

    def test_update_segment_sfx_unknown_name_rejected(self, client, tmp_path):
        script = [{"seg_id": 1, "speaker": "narrator", "text": "a", "emotion": "neutral"}]
        nid, ch_id = self._chapter_with_script(client, tmp_path, script)
        resp = client.patch(f"/api/novels/{nid}/chapters/{ch_id}/segments/1", json={"sfx": "nope"})
        assert resp.status_code == 400

    def test_update_segment_bgm_null_clears(self, client, tmp_path):
        script = [{"seg_id": 1, "speaker": "narrator", "text": "a", "emotion": "neutral", "bgm": "rain_heavy"}]
        nid, ch_id = self._chapter_with_script(client, tmp_path, script)
        resp = client.patch(f"/api/novels/{nid}/chapters/{ch_id}/segments/1", json={"bgm": None})
        assert resp.status_code == 200
        saved = json.loads((tmp_path / "library" / nid / "chapters" / ch_id / "script_final.json").read_text())
        assert saved[0]["bgm"] is None

    def test_batch_update_segments_bgm_unknown_name_rejected(self, client, tmp_path):
        script = [{"seg_id": 1, "speaker": "narrator", "text": "a", "emotion": "neutral"}]
        nid, ch_id = self._chapter_with_script(client, tmp_path, script)
        resp = client.post(f"/api/novels/{nid}/chapters/{ch_id}/segments/batch",
                           json={"seg_ids": [1], "set": {"bgm": "nope"}})
        assert resp.status_code == 400


class TestRolesAPI:
    def test_list_roles(self, client):
        resp = client.get("/api/roles")
        assert resp.status_code == 200
        assert resp.json() == []  # 隔离目录里没有真实项目的 6 个角色

    def test_delete_narrator_forbidden(self, client):
        resp = client.delete("/api/roles/narrator")
        assert resp.status_code == 400

    def test_role_has_embedding_status_and_reference_flag(self, client):
        role_id = client.post("/api/roles", json={"name": "测试角色"}).json()["role_id"]
        role = client.get("/api/roles").json()[0]
        assert role["id"] == role_id
        assert role["has_reference"] is False
        assert role["embedding_status"]["exists"] is False

    def test_reference_audio_404_then_served(self, client):
        role_id = client.post("/api/roles", json={"name": "测试角色2"}).json()["role_id"]
        assert client.get(f"/api/roles/{role_id}/reference").status_code == 404

        resp = client.put(f"/api/roles/{role_id}/reference",
                          files={"file": ("ref.wav", b"RIFFfake", "audio/wav")})
        assert resp.status_code == 200

        resp = client.get(f"/api/roles/{role_id}/reference")
        assert resp.status_code == 200
        assert resp.content == b"RIFFfake"

        role = next(r for r in client.get("/api/roles").json() if r["id"] == role_id)
        assert role["has_reference"] is True


class TestRoleCategoriesAPI:
    def test_empty_by_default(self, client):
        resp = client.get("/api/role-categories")
        assert resp.status_code == 200
        assert resp.json() == {"categories": []}

    def test_put_persists_and_dedupes(self, client):
        resp = client.put("/api/role-categories", json={"categories": ["主角", "配角", "主角", " ", ""]})
        assert resp.status_code == 200
        assert resp.json()["categories"] == ["主角", "配角"]

        resp = client.get("/api/role-categories")
        assert resp.json()["categories"] == ["主角", "配角"]

    def test_put_rejects_non_list(self, client):
        resp = client.put("/api/role-categories", json={"categories": "主角"})
        assert resp.status_code == 400

    def test_deleting_category_does_not_touch_existing_roles(self, client):
        client.put("/api/role-categories", json={"categories": ["主角"]})
        role_id = client.post("/api/roles", json={"name": "苏砚", "category": "主角"}).json()["role_id"]

        client.put("/api/role-categories", json={"categories": []})

        role = next(r for r in client.get("/api/roles").json() if r["id"] == role_id)
        assert role["category"] == "主角"  # 分类被摘掉了，角色自己的字段不受影响


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

    def test_submit_unknown_type_rejected(self, client):
        """未知任务类型在提交时就该 400，不该静默落进 cpu lane 异步失败"""
        nid, vol_id = self._novel_with_two_chapters(client)
        resp = client.post("/api/tasks", json={"type": "nope", "novel_id": nid,
                                               "scope": {"chapter_ids": ["ch_0001"]}})
        assert resp.status_code == 400

    def test_submit_import_type_rejected(self, client):
        """import 类型声明了常量和 lane 但从没注册过 handler，是个哑弹，提交也要 400"""
        nid, vol_id = self._novel_with_two_chapters(client)
        resp = client.post("/api/tasks", json={"type": "import", "novel_id": nid,
                                               "scope": {"chapter_ids": ["ch_0001"]}})
        assert resp.status_code == 400

    def test_preflight_unknown_type_rejected(self, client):
        nid, vol_id = self._novel_with_two_chapters(client)
        resp = client.post("/api/tasks/preflight", json={"type": "nope", "novel_id": nid,
                                                          "scope": {"chapter_ids": ["ch_0001"]}})
        assert resp.status_code == 400

    def test_submit_mix_with_params_persists_params(self, client):
        nid, vol_id = self._novel_with_two_chapters(client)
        resp = client.post("/api/tasks", json={"type": "mix", "novel_id": nid,
                                               "scope": {"chapter_ids": ["ch_0001"]},
                                               "params": {"with_assets": True}})
        assert resp.status_code == 200
        assert resp.json()["tasks"][0]["params"] == {"with_assets": True}


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

    def test_patch_config_accepts_mixing_voice_only(self, client, tmp_path):
        cfg_path = tmp_path / "global_config.yaml"
        cfg_path.write_text("mixing:\n  voice_only: true\n")
        resp = client.patch("/api/config", json={"mixing.voice_only": False})
        assert resp.status_code == 200
        assert resp.json()["applied_keys"] == ["mixing.voice_only"]

        import yaml
        on_disk = yaml.safe_load(cfg_path.read_text())
        assert on_disk["mixing"]["voice_only"] is False

    def test_patch_config_coerces_string_bool_for_voice_only(self, client, tmp_path):
        """JSON 字符串 "false" 在 Python 里是真值，写配置前必须强制转成真正的
        bool，否则前端传什么字符串都会被当成"开"，voice_only 就永远关不掉。"""
        cfg_path = tmp_path / "global_config.yaml"
        cfg_path.write_text("mixing:\n  voice_only: true\n")
        resp = client.patch("/api/config", json={"mixing.voice_only": "false"})
        assert resp.status_code == 200

        import yaml
        on_disk = yaml.safe_load(cfg_path.read_text())
        assert on_disk["mixing"]["voice_only"] is False
