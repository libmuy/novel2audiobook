"""素材分类 + 标签（asset_specs.yaml 的 category/tags 字段与 category_tree 顶层键）"""
import os
import shutil

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api import deps
from src.runtime.task_queue import TaskQueue
from src.pipeline import asset_gen, asset_specs_store as store

REAL_SPEC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "assets", "asset_specs.yaml")


@pytest.fixture
def client(tmp_path, monkeypatch):
    import src.utils
    monkeypatch.setattr(src.utils, "PROJECT_ROOT", str(tmp_path))
    (tmp_path / "data" / "assets").mkdir(parents=True)
    shutil.copy(REAL_SPEC, tmp_path / "data" / "assets" / "asset_specs.yaml")
    config = {"server": {"cpu_workers": 1}, "llm": {}, "tts": {}, "mixing": {}}
    deps.set_config(config)
    deps.set_queue(TaskQueue(config=config, tasks_dir=str(tmp_path / "tasks"),
                             library_dir=str(tmp_path / "library")))
    return TestClient(create_app(), raise_server_exceptions=False)


def _text(tmp_path):
    return (tmp_path / "data" / "assets" / "asset_specs.yaml").read_text(encoding="utf-8")


def _row(client, kind, name):
    return next(r for r in client.get(f"/api/asset-specs?kind={kind}").json()["specs"] if r["name"] == name)


class TestSpecFields:
    def test_existing_specs_read_back_empty_not_missing_keys(self, client):
        row = _row(client, "sfx", "sword_clash")
        assert row["category"] == "" and row["tags"] == []

    def test_create_with_category_and_tags_reads_back(self, client):
        r = client.post("/api/asset-specs", json={
            "kind": "sfx", "name": "glass_break", "prompt": "glass", "category": " 环境/室内 ",
            "tags": ["玻璃", " 玻璃 ", "", "易碎"]})
        assert r.status_code == 201
        row = _row(client, "sfx", "glass_break")
        assert row["category"] == "环境/室内"      # 去空白
        assert row["tags"] == ["玻璃", "易碎"]      # 去空白、去重、去空串

    def test_empty_values_are_not_written_to_the_file(self, client, tmp_path):
        client.post("/api/asset-specs", json={"kind": "sfx", "name": "plain", "prompt": "x", "category": "", "tags": []})
        assert "category:" not in _text(tmp_path).split("plain:")[1].split("\n\n")[0]

    def test_patch_sets_and_clears(self, client):
        client.patch("/api/asset-specs/sfx/sword_clash", json={"category": "战斗", "tags": ["金属"]})
        row = _row(client, "sfx", "sword_clash")
        assert (row["category"], row["tags"]) == ("战斗", ["金属"])
        client.patch("/api/asset-specs/sfx/sword_clash", json={"category": "", "tags": []})
        row = _row(client, "sfx", "sword_clash")
        assert (row["category"], row["tags"]) == ("", [])

    def test_patch_without_the_fields_leaves_them_alone(self, client):
        client.patch("/api/asset-specs/sfx/sword_clash", json={"category": "战斗", "tags": ["金属"]})
        client.patch("/api/asset-specs/sfx/sword_clash", json={"seed": 9})
        row = _row(client, "sfx", "sword_clash")
        assert (row["category"], row["tags"]) == ("战斗", ["金属"])

    def test_bad_tags_type_rejected(self, client):
        assert client.post("/api/asset-specs", json={
            "kind": "sfx", "name": "bad", "prompt": "x", "tags": "not-a-list"}).status_code == 422

    def test_metadata_edit_keeps_comments_and_status(self, client, tmp_path):
        before = _text(tmp_path)
        inline_note = "# 诊断发现：TangoFlux 对原 3 秒时长只在前 ~1.9 秒生成了真实内容"
        assert inline_note in before
        client.patch("/api/asset-specs/sfx/sword_clash", json={"category": "战斗", "tags": ["金属"]})
        after = _text(tmp_path)
        assert inline_note in after and "# 音效/背景音素材库唯一真相源。" in after
        client.put("/api/asset-category-tree", json={"tree": [{"title": "战斗", "children": []}]})
        after = _text(tmp_path)
        assert inline_note in after and "# 音效/背景音素材库唯一真相源。" in after


class TestSpecHashPinned:
    """category/tags 只是整理用元数据。要是有人「顺手」把它们加进哈希，整个素材库会在
    那一刻全部判为 STALE 并被重新生成——这条测试就是拦这个的。"""

    def test_hash_ignores_category_and_tags(self):
        base = {"prompt": "p", "negative_prompt": "n", "duration_sec": 10.0, "seed": 1, "description": "d"}
        h = asset_gen.compute_spec_hash(base)
        assert asset_gen.compute_spec_hash({**base, "category": "战斗", "tags": ["a", "b"]}) == h

    def test_editing_metadata_keeps_generated_asset_ok(self, client, tmp_path):
        mock = asset_gen.MockAudioGenBackend()
        asset_gen.generate_assets(assets_dir=str(tmp_path / "data" / "assets"), backend_map={"ambience": mock, "sfx": mock})
        assert _row(client, "sfx", "sword_clash")["status"].startswith("OK")
        client.patch("/api/asset-specs/sfx/sword_clash", json={"category": "战斗", "tags": ["金属"]})
        assert _row(client, "sfx", "sword_clash")["status"].startswith("OK")


class TestCategoryTree:
    TREE = [{"title": "环境", "children": [{"title": "室内", "children": []}]}, {"title": "战斗", "children": []}]

    def test_empty_by_default(self, client):
        assert client.get("/api/asset-category-tree").json() == {"tree": [], "categories": []}

    def test_put_then_get_roundtrip_with_ids(self, client):
        put = client.put("/api/asset-category-tree", json={"tree": self.TREE}).json()
        assert put["categories"] == ["环境", "环境/室内", "战斗"]
        assert all(n["id"] for n in put["tree"])
        assert client.get("/api/asset-category-tree").json()["tree"] == put["tree"]

    def test_tree_key_is_not_read_as_an_asset(self, client):
        client.put("/api/asset-category-tree", json={"tree": self.TREE})
        specs = asset_gen.load_asset_specs()
        assert set(specs) == {"ambience", "sfx"}
        assert len(client.get("/api/asset-specs").json()["specs"]) == 12

    def test_ids_preserved_when_provided(self, client):
        res = client.put("/api/asset-category-tree", json={"tree": [{"id": "keep", "title": "a", "children": []}]})
        assert res.json()["tree"][0]["id"] == "keep"

    @pytest.mark.parametrize("tree", [
        [{"title": "", "children": []}],
        [{"title": "a/b", "children": []}],
        [{"title": "重名", "children": []}, {"title": "重名", "children": []}],
    ])
    def test_invalid_tree_400_and_file_untouched(self, client, tmp_path, tree):
        before = _text(tmp_path)
        assert client.put("/api/asset-category-tree", json={"tree": tree}).status_code == 400
        assert _text(tmp_path) == before

    def test_deleting_a_node_does_not_scrub_the_asset_category(self, client):
        client.put("/api/asset-category-tree", json={"tree": self.TREE})
        client.patch("/api/asset-specs/sfx/sword_clash", json={"category": "战斗"})
        client.put("/api/asset-category-tree", json={"tree": []})
        assert _row(client, "sfx", "sword_clash")["category"] == "战斗"  # 悬空引用容忍

    def test_spec_writes_do_not_drop_the_tree(self, client):
        client.put("/api/asset-category-tree", json={"tree": self.TREE})
        client.post("/api/asset-specs", json={"kind": "sfx", "name": "glass_break", "prompt": "glass"})
        client.delete("/api/asset-specs/ambience/rain_heavy")
        assert [n["title"] for n in client.get("/api/asset-category-tree").json()["tree"]] == ["环境", "战斗"]


class TestAssetTags:
    def test_counts_sorted_by_count_then_name(self, client):
        client.patch("/api/asset-specs/sfx/sword_clash", json={"tags": ["金属", "战斗"]})
        client.post("/api/asset-specs", json={"kind": "sfx", "name": "glass_break", "prompt": "g", "tags": ["金属"]})
        assert client.get("/api/asset-tags").json()["tags"] == [
            {"name": "金属", "count": 2}, {"name": "战斗", "count": 1}]

    def test_no_tags_is_empty_list(self, client):
        assert client.get("/api/asset-tags").json() == {"tags": []}
