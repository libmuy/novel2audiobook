"""素材规格 CRUD（asset_specs_store + /api/asset-specs）"""
import os
import shutil

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api import deps
from src.task_queue import TaskQueue
from src import asset_gen, asset_specs_store as store

REAL_SPEC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "assets", "asset_specs.yaml")


@pytest.fixture
def client(tmp_path, monkeypatch):
    """隔离环境：PROJECT_ROOT 指到 tmp，assets/asset_specs.yaml 是真实文件的副本"""
    import src.utils
    monkeypatch.setattr(src.utils, "PROJECT_ROOT", str(tmp_path))
    (tmp_path / "data" / "assets").mkdir(parents=True)
    shutil.copy(REAL_SPEC, tmp_path / "data" / "assets" / "asset_specs.yaml")

    config = {"server": {"cpu_workers": 1}, "llm": {}, "tts": {}, "mixing": {}}
    deps.set_config(config)
    deps.set_queue(TaskQueue(config=config, tasks_dir=str(tmp_path / "tasks"),
                             library_dir=str(tmp_path / "library")))
    return TestClient(create_app(), raise_server_exceptions=False)


def _spec_text(tmp_path):
    return (tmp_path / "data" / "assets" / "asset_specs.yaml").read_text(encoding="utf-8")


def _gen_mock(tmp_path):
    mock = asset_gen.MockAudioGenBackend()
    asset_gen.generate_assets(assets_dir=str(tmp_path / "data" / "assets"),
                              backend_map={"ambience": mock, "sfx": mock})


class TestList:
    def test_lists_real_specs_with_missing_status(self, client):
        rows = client.get("/api/asset-specs").json()["specs"]
        assert len(rows) == 12
        assert {r["status"] for r in rows} == {"MISSING"}
        assert {r["kind"] for r in rows} == {"ambience", "sfx"}

    def test_filter_by_kind(self, client):
        rows = client.get("/api/asset-specs?kind=sfx").json()["specs"]
        assert rows and all(r["kind"] == "sfx" for r in rows)

    def test_unknown_kind_400(self, client):
        assert client.get("/api/asset-specs?kind=nope").status_code == 400


class TestCreate:
    BODY = {"kind": "sfx", "name": "glass_break", "prompt": "glass shattering once"}

    def test_create_appears_missing(self, client):
        resp = client.post("/api/asset-specs", json=self.BODY)
        assert resp.status_code == 201
        assert resp.json()["status"] == "MISSING"
        names = [r["name"] for r in client.get("/api/asset-specs?kind=sfx").json()["specs"]]
        assert "glass_break" in names

    def test_duplicate_409(self, client):
        client.post("/api/asset-specs", json=self.BODY)
        assert client.post("/api/asset-specs", json=self.BODY).status_code == 409

    @pytest.mark.parametrize("name", ["Rain Heavy", "../x", "a/b", "", "UPPER", "x" * 60])
    def test_bad_name_400(self, client, name):
        assert client.post("/api/asset-specs", json={**self.BODY, "name": name}).status_code == 400

    def test_empty_prompt_400(self, client):
        assert client.post("/api/asset-specs", json={**self.BODY, "prompt": "  "}).status_code == 400

    def test_unknown_kind_400(self, client):
        assert client.post("/api/asset-specs", json={**self.BODY, "kind": "music"}).status_code == 400

    @pytest.mark.parametrize("duration", [0, -1, 61])
    def test_bad_duration_400(self, client, duration):
        assert client.post("/api/asset-specs", json={**self.BODY, "duration_sec": duration}).status_code == 400


class TestUpdate:
    def test_description_only_patch_keeps_ok(self, client, tmp_path):
        """compute_spec_hash 不含 description：只改描述不该让状态变 STALE"""
        _gen_mock(tmp_path)
        assert client.get("/api/asset-specs?kind=sfx").json()["specs"][0]["status"].startswith("OK")
        resp = client.patch("/api/asset-specs/sfx/sword_clash", json={"description": "新的描述"})
        assert resp.status_code == 200
        assert resp.json()["status"].startswith("OK")

    def test_prompt_patch_makes_stale(self, client, tmp_path):
        _gen_mock(tmp_path)
        resp = client.patch("/api/asset-specs/sfx/sword_clash", json={"prompt": "a completely different sound"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "STALE(spec已变更)"

    def test_missing_404(self, client):
        assert client.patch("/api/asset-specs/sfx/nope", json={"seed": 1}).status_code == 404

    def test_blank_prompt_patch_400(self, client):
        assert client.patch("/api/asset-specs/sfx/sword_clash", json={"prompt": ""}).status_code == 400


class TestDelete:
    def test_delete_keeps_files_by_default(self, client, tmp_path):
        _gen_mock(tmp_path)
        wav = tmp_path / "data" / "assets" / "sfx" / "sword_clash.wav"
        assert wav.exists()
        resp = client.delete("/api/asset-specs/sfx/sword_clash")
        assert resp.status_code == 200 and resp.json()["files_deleted"] is False
        assert wav.exists()
        names = [r["name"] for r in client.get("/api/asset-specs?kind=sfx").json()["specs"]]
        assert "sword_clash" not in names

    def test_delete_files_true_removes_wav_and_meta(self, client, tmp_path):
        _gen_mock(tmp_path)
        resp = client.delete("/api/asset-specs/sfx/sword_clash?delete_files=true")
        assert resp.json()["files_deleted"] is True
        assert not (tmp_path / "data" / "assets" / "sfx" / "sword_clash.wav").exists()
        assert not (tmp_path / "data" / "assets" / "sfx" / "sword_clash.meta.json").exists()

    def test_delete_missing_404(self, client):
        assert client.delete("/api/asset-specs/sfx/nope").status_code == 404


class TestAudio:
    def test_404_before_generation(self, client):
        assert client.get("/api/asset-specs/sfx/sword_clash/audio").status_code == 404

    def test_served_as_wav_after_generation(self, client, tmp_path):
        _gen_mock(tmp_path)
        resp = client.get("/api/asset-specs/sfx/sword_clash/audio")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/wav"
        assert resp.content[:4] == b"RIFF"
        # 同名重新生成后 URL 不变，必须让浏览器每次重新验证，不能一直播缓存里的旧音频
        assert resp.headers["cache-control"] == "no-cache"
        assert resp.content == (tmp_path / "data" / "assets" / "sfx" / "sword_clash.wav").read_bytes()

    def test_still_playable_when_stale(self, client, tmp_path):
        """规格改了（STALE）但旧音频还在：仍然可以试听旧版本"""
        _gen_mock(tmp_path)
        client.patch("/api/asset-specs/sfx/sword_clash", json={"prompt": "totally different"})
        assert client.get("/api/asset-specs/sfx/sword_clash/audio").status_code == 200

    def test_unknown_kind_400(self, client):
        assert client.get("/api/asset-specs/music/x/audio").status_code == 400

    @pytest.mark.parametrize("name", ["Bad Name", "UPPER", "a.b", "..", "%2e%2e"])
    def test_unsafe_name_400_not_path_traversal(self, client, name):
        assert client.get(f"/api/asset-specs/sfx/{name}/audio").status_code in (400, 404)
        # 关键：不能因为路径里有 .. 而读到 assets 之外的文件
        assert client.get(f"/api/asset-specs/sfx/{name}/audio").status_code != 200

    def test_spec_deleted_but_wav_kept_is_still_servable_by_name(self, client, tmp_path):
        """删规格默认保留 wav（跟角色分类的悬空引用同一套容忍策略）；已有章节仍引用它，
        试听/混音继续可用，只是素材库列表里不再有这张卡片"""
        _gen_mock(tmp_path)
        client.delete("/api/asset-specs/sfx/sword_clash")
        assert client.get("/api/asset-specs/sfx/sword_clash/audio").status_code == 200


class TestCommentPreservation:
    """选 ruamel 而不是 yaml.safe_dump 的全部理由：文件头说明和条目行内诊断注释不能丢"""

    def test_create_patch_delete_keep_header_and_inline_comments(self, client, tmp_path):
        before = _spec_text(tmp_path)
        header_line = "# 音效/背景音素材库唯一真相源。"
        inline_note = "# 诊断发现：TangoFlux 对原 3 秒时长只在前 ~1.9 秒生成了真实内容"
        assert header_line in before and inline_note in before

        client.post("/api/asset-specs", json={"kind": "sfx", "name": "glass_break", "prompt": "glass"})
        client.patch("/api/asset-specs/sfx/sword_clash", json={"seed": 4242})
        client.delete("/api/asset-specs/ambience/rain_heavy")

        after = _spec_text(tmp_path)
        assert header_line in after
        assert inline_note in after
        assert "seed: 4242" in after
        assert "rain_heavy" not in after
        assert "glass_break" in after

    def test_untouched_roundtrip_is_byte_identical(self, tmp_path):
        p = tmp_path / "s.yaml"
        shutil.copy(REAL_SPEC, p)
        store.save_raw(store.load_raw(str(p)), str(p))
        assert p.read_text(encoding="utf-8") == open(REAL_SPEC, encoding="utf-8").read()

    def test_result_still_loadable_by_asset_gen(self, client, tmp_path):
        client.post("/api/asset-specs", json={"kind": "ambience", "name": "cave_drip",
                                              "prompt": "dripping water in a cave", "duration_sec": 8, "seed": 7})
        specs = asset_gen.load_asset_specs()
        assert specs["ambience"]["cave_drip"]["duration_sec"] == 8.0
        assert specs["ambience"]["cave_drip"]["seed"] == 7


class TestSpecFileConfig:
    def test_spec_file_config_key_is_honored(self, tmp_path, monkeypatch):
        """asset_gen.spec_file 之前定义了但从没人读过——现在读写两边都认它"""
        import src.utils
        monkeypatch.setattr(src.utils, "PROJECT_ROOT", str(tmp_path))
        (tmp_path / "custom").mkdir()
        (tmp_path / "custom" / "my.yaml").write_text(
            "sfx:\n  only_one:\n    prompt: hi\n    duration_sec: 2\n    seed: 1\n", encoding="utf-8")
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "global_config.yaml").write_text(
            "asset_gen:\n  spec_file: custom/my.yaml\n", encoding="utf-8")

        assert list(asset_gen.load_asset_specs()["sfx"]) == ["only_one"]
        assert store.spec_file_path().endswith(os.path.join("custom", "my.yaml"))
