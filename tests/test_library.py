"""
src/domain/library.py 单元测试
"""
import os
import pytest
import yaml

from src.domain.library import (
    create_novel, load_novel, save_novel, delete_novel, list_novels,
    slugify_novel_id, validate_tree, find_node, tree_insert, tree_move,
    tree_rename, tree_delete, iter_chapters, alloc_chapter_id,
    add_chapter, import_chapter_raw, delete_chapter, validate_tree_vs_disk,
    get_novel_dir, get_chapters_dir, get_chapter_dir,
)


class TestSlugify:
    def test_chinese_to_pinyin(self):
        result = slugify_novel_id("仙逆", set())
        assert result.startswith("nv_")
        assert "xian" in result and "ni" in result

    def test_duplicate_appends_suffix(self):
        base = slugify_novel_id("仙逆", set())
        assert slugify_novel_id("仙逆", {base}) == f"{base}_2"

    def test_duplicate_appends_increasing_suffix(self):
        base = slugify_novel_id("仙逆", set())
        assert slugify_novel_id("仙逆", {base, f"{base}_2"}) == f"{base}_3"

    def test_empty_title_fallback(self):
        result = slugify_novel_id("", set())
        assert result.startswith("nv_")


class TestNovelCRUD:
    def test_create_and_load_roundtrip(self, tmp_path):
        lib = str(tmp_path / "library")
        nid = create_novel("测试书", description="描述", levels={"part": False, "volume": True}, library_dir=lib)
        assert nid.startswith("nv_")

        data = load_novel(nid, library_dir=lib)
        assert data["novel_id"] == nid
        assert data["title"] == "测试书"
        assert data["description"] == "描述"
        assert data["levels"] == {"part": False, "volume": True}
        assert data["next_chapter_seq"] == 1
        assert data["tree"] == []
        assert data["schema_version"] == 1
        assert "created_at" in data
        assert "updated_at" in data

    def test_create_novel_makes_dirs(self, tmp_path):
        lib = str(tmp_path / "library")
        nid = create_novel("新书", library_dir=lib)
        assert os.path.isdir(get_novel_dir(nid, library_dir=lib))
        assert os.path.isdir(get_chapters_dir(nid, library_dir=lib))

    def test_list_novels_sorted_by_title(self, tmp_path):
        lib = str(tmp_path / "library")
        create_novel("测试书B", library_dir=lib)
        create_novel("测试书A", library_dir=lib)
        novels = list_novels(library_dir=lib)
        assert len(novels) == 2
        titles = [n["title"] for n in novels]
        assert titles == sorted(titles)

    def test_list_novels_skips_dirs_without_yaml(self, tmp_path):
        lib = str(tmp_path / "library")
        os.makedirs(os.path.join(lib, "no_yaml_dir"))
        novels = list_novels(library_dir=lib)
        assert all("no_yaml_dir" not in n["novel_id"] for n in novels)

    def test_list_novels_includes_per_state_counts(self, tmp_path):
        """计划 011：列表页要按四态渲染双色进度条，不用逐本再拉一次 /tree。"""
        lib = str(tmp_path / "library")
        nid = create_novel("带章节的书", library_dir=lib)
        from src.domain.library import add_chapter
        add_chapter(nid, "第一章", "正文", library_dir=lib)
        add_chapter(nid, "第二章", "正文", library_dir=lib)
        novels = list_novels(library_dir=lib)
        counts = novels[0]["counts"]
        assert counts["total"] == 2
        assert counts["unparsed"] == 2  # 只有 raw.txt，还没解析
        assert counts["parsed"] == 0 and counts["voiced"] == 0 and counts["stale"] == 0

    def test_list_novels_empty_novel_has_zeroed_counts(self, tmp_path):
        lib = str(tmp_path / "library")
        create_novel("空书", library_dir=lib)
        counts = list_novels(library_dir=lib)[0]["counts"]
        assert counts == {"total": 0, "unparsed": 0, "parsed": 0, "voiced": 0, "stale": 0}

    def test_delete_novel_moves_to_trash(self, tmp_path):
        lib = str(tmp_path / "library")
        nid = create_novel("待删", library_dir=lib)
        novel_dir = get_novel_dir(nid, library_dir=lib)
        assert os.path.isdir(novel_dir)

        delete_novel(nid, library_dir=lib)
        assert not os.path.isdir(novel_dir)
        trash = os.path.join(lib, ".trash")
        assert os.path.isdir(trash)
        entries = os.listdir(trash)
        assert any(nid in e for e in entries)


class TestValidateTree:
    def test_part_node_rejected_when_level_disabled(self, tmp_path):
        data = {
            "novel_id": "test", "levels": {"part": False, "volume": True},
            "tree": [{"type": "part", "id": "p1", "title": "P"}],
            "next_chapter_seq": 1,
        }
        with pytest.raises(ValueError, match="part"):
            validate_tree(data)

    def test_volume_node_rejected_when_level_disabled(self, tmp_path):
        data = {
            "novel_id": "test", "levels": {"part": False, "volume": False},
            "tree": [{"type": "volume", "id": "v1", "title": "V"}],
            "next_chapter_seq": 1,
        }
        with pytest.raises(ValueError, match="volume"):
            validate_tree(data)

    def test_chapter_with_children_rejected(self):
        data = {
            "novel_id": "test", "levels": {"part": False, "volume": True},
            "tree": [
                {"type": "chapter", "id": "ch_0001", "title": "C",
                 "children": [{"type": "chapter", "id": "ch_0002", "title": "C2"}]}
            ],
            "next_chapter_seq": 3,
        }
        with pytest.raises(ValueError, match="chapter"):
            validate_tree(data)

    def test_duplicate_ids_rejected(self):
        data = {
            "novel_id": "test", "levels": {"part": False, "volume": True},
            "tree": [
                {"type": "chapter", "id": "ch_0001", "title": "A"},
                {"type": "chapter", "id": "ch_0001", "title": "B"},
            ],
            "next_chapter_seq": 3,
        }
        with pytest.raises(ValueError, match="重复"):
            validate_tree(data)

    def test_next_seq_must_exceed_max(self):
        data = {
            "novel_id": "test", "levels": {"part": False, "volume": False},
            "tree": [{"type": "chapter", "id": "ch_0005", "title": "A"}],
            "next_chapter_seq": 3,
        }
        with pytest.raises(ValueError, match="next_chapter_seq"):
            validate_tree(data)

    def test_valid_tree_passes(self):
        data = {
            "novel_id": "test", "levels": {"part": False, "volume": True},
            "tree": [
                {"type": "volume", "id": "vol_001", "title": "V1",
                 "children": [
                     {"type": "chapter", "id": "ch_0001", "title": "C1"},
                     {"type": "chapter", "id": "ch_0002", "title": "C2"},
                 ]},
                {"type": "chapter", "id": "ch_0003", "title": "Extra"},
            ],
            "next_chapter_seq": 4,
        }
        validate_tree(data)  # 不应抛异常


class TestTreeOperations:
    def _make_novel(self, tmp_path):
        lib = str(tmp_path / "library")
        nid = create_novel("树测试", levels={"part": True, "volume": True}, library_dir=lib)
        return nid, lib

    def test_find_node(self, tmp_path):
        nid, lib = self._make_novel(tmp_path)
        data = load_novel(nid, library_dir=lib)
        tree_insert(data, None, {"type": "volume", "id": "vol_001", "title": "V1",
                                 "children": [{"type": "chapter", "id": "ch_0001", "title": "C1"}]})
        data["next_chapter_seq"] = 2
        save_novel(data, library_dir=lib)

        data = load_novel(nid, library_dir=lib)
        node, siblings, idx = find_node(data, "ch_0001")
        assert node is not None
        assert node["id"] == "ch_0001"
        assert idx == 0

        node, _, idx = find_node(data, "nonexistent")
        assert node is None
        assert idx == -1

    def test_tree_move_into_own_subtree_rejected(self, tmp_path):
        nid, lib = self._make_novel(tmp_path)
        data = load_novel(nid, library_dir=lib)
        tree_insert(data, None, {
            "type": "volume", "id": "vol_001", "title": "V1",
            "children": [
                {"type": "chapter", "id": "ch_0001", "title": "C1"},
                {"type": "chapter", "id": "ch_0002", "title": "C2"},
            ]
        })
        data["next_chapter_seq"] = 3
        save_novel(data, library_dir=lib)

        data = load_novel(nid, library_dir=lib)
        with pytest.raises(ValueError, match="子树"):
            tree_move(data, "vol_001", "ch_0001", 0)

    def test_tree_move_valid(self, tmp_path):
        nid, lib = self._make_novel(tmp_path)
        data = load_novel(nid, library_dir=lib)
        tree_insert(data, None, {"type": "chapter", "id": "ch_0001", "title": "C1"})
        tree_insert(data, None, {"type": "chapter", "id": "ch_0002", "title": "C2"})
        tree_insert(data, None, {"type": "chapter", "id": "ch_0003", "title": "C3"})
        data["next_chapter_seq"] = 4
        save_novel(data, library_dir=lib)

        data = load_novel(nid, library_dir=lib)
        tree_move(data, "ch_0003", None, 0)
        assert data["tree"][0]["id"] == "ch_0003"
        save_novel(data, library_dir=lib)

    def test_iter_chapters_whole_book(self, tmp_path):
        nid, lib = self._make_novel(tmp_path)
        data = load_novel(nid, library_dir=lib)
        tree_insert(data, None, {
            "type": "volume", "id": "vol_001", "title": "V1",
            "children": [
                {"type": "chapter", "id": "ch_0001", "title": "C1"},
                {"type": "chapter", "id": "ch_0002", "title": "C2"},
            ]
        })
        tree_insert(data, None, {"type": "chapter", "id": "ch_0003", "title": "C3"})
        data["next_chapter_seq"] = 4
        save_novel(data, library_dir=lib)

        data = load_novel(nid, library_dir=lib)
        assert iter_chapters(data) == ["ch_0001", "ch_0002", "ch_0003"]

    def test_iter_chapters_single_volume(self, tmp_path):
        nid, lib = self._make_novel(tmp_path)
        data = load_novel(nid, library_dir=lib)
        tree_insert(data, None, {
            "type": "volume", "id": "vol_001", "title": "V1",
            "children": [
                {"type": "chapter", "id": "ch_0001", "title": "C1"},
                {"type": "chapter", "id": "ch_0002", "title": "C2"},
            ]
        })
        tree_insert(data, None, {"type": "chapter", "id": "ch_0003", "title": "C3"})
        data["next_chapter_seq"] = 4
        save_novel(data, library_dir=lib)

        data = load_novel(nid, library_dir=lib)
        assert iter_chapters(data, "vol_001") == ["ch_0001", "ch_0002"]
        assert iter_chapters(data, "ch_0003") == ["ch_0003"]


class TestChapterOperations:
    def test_alloc_chapter_id_increments(self, tmp_path):
        lib = str(tmp_path / "library")
        nid = create_novel("序号测试", library_dir=lib)
        data = load_novel(nid, library_dir=lib)
        assert alloc_chapter_id(data) == "ch_0001"
        assert alloc_chapter_id(data) == "ch_0002"
        assert data["next_chapter_seq"] == 3

    def test_alloc_chapter_id_no_reuse_after_delete(self, tmp_path):
        lib = str(tmp_path / "library")
        nid = create_novel("不复用", library_dir=lib)
        add_chapter(nid, "第一章", "内容", library_dir=lib)
        add_chapter(nid, "第二章", "内容", library_dir=lib)

        delete_chapter(nid, "ch_0001", library_dir=lib)

        data = load_novel(nid, library_dir=lib)
        assert data["next_chapter_seq"] == 3
        ch_id = alloc_chapter_id(data)
        assert ch_id == "ch_0003"

    def test_add_chapter_creates_dir_and_raw(self, tmp_path):
        lib = str(tmp_path / "library")
        nid = create_novel("建章测试", library_dir=lib)
        ch_id = add_chapter(nid, "第一章", "测试正文", library_dir=lib)
        assert ch_id == "ch_0001"

        ch_dir = get_chapter_dir(nid, ch_id, library_dir=lib)
        assert os.path.isdir(ch_dir)
        with open(os.path.join(ch_dir, "raw.txt"), "r", encoding="utf-8") as f:
            assert f.read() == "测试正文"

    def test_delete_chapter_moves_to_trash(self, tmp_path):
        lib = str(tmp_path / "library")
        nid = create_novel("删章测试", library_dir=lib)
        ch_id = add_chapter(nid, "第一章", "内容", library_dir=lib)
        ch_dir = get_chapter_dir(nid, ch_id, library_dir=lib)
        assert os.path.isdir(ch_dir)

        delete_chapter(nid, ch_id, library_dir=lib)
        assert not os.path.isdir(ch_dir)

        trash_dir = os.path.join(get_novel_dir(nid, library_dir=lib), ".trash")
        assert os.path.isdir(trash_dir)
        assert any(ch_id in e for e in os.listdir(trash_dir))

    def test_import_chapter_raw_clears_downstream_keeps_cache(self, tmp_path):
        lib = str(tmp_path / "library")
        nid = create_novel("重导测试", library_dir=lib)
        ch_id = add_chapter(nid, "第一章", "原文", library_dir=lib)

        ch_dir = get_chapter_dir(nid, ch_id, library_dir=lib)
        # 模拟已有下游产物
        for fname in ("script_draft.json", "script_final.json", "timeline.json"):
            with open(os.path.join(ch_dir, fname), "w") as f:
                f.write("{}")
        os.makedirs(os.path.join(ch_dir, "output"))
        os.makedirs(os.path.join(ch_dir, "audio_cache"))
        with open(os.path.join(ch_dir, "audio_cache", "abc.wav"), "w") as f:
            f.write("fake")

        result = import_chapter_raw(nid, ch_id, "新内容", library_dir=lib)
        assert "script_draft.json" in result["removed"]
        assert "script_final.json" in result["removed"]
        assert "timeline.json" in result["removed"]
        assert "output/" in result["removed"]
        assert result["kept_cache_count"] == 1

        assert not os.path.exists(os.path.join(ch_dir, "script_draft.json"))
        assert not os.path.exists(os.path.join(ch_dir, "output"))
        assert os.path.exists(os.path.join(ch_dir, "audio_cache", "abc.wav"))
        with open(os.path.join(ch_dir, "raw.txt"), "r", encoding="utf-8") as f:
            assert f.read() == "新内容"


class TestValidateTreeVsDisk:
    def test_reports_orphans_and_missing(self, tmp_path):
        lib = str(tmp_path / "library")
        nid = create_novel("一致性", library_dir=lib)
        add_chapter(nid, "第一章", "内容", library_dir=lib)

        # 制造 orphan：磁盘上有但树里没有
        chapters_dir = get_chapters_dir(nid, library_dir=lib)
        os.makedirs(os.path.join(chapters_dir, "ch_0099"))

        # 制造 missing：树里有但磁盘上没有
        data = load_novel(nid, library_dir=lib)
        tree_insert(data, None, {"type": "chapter", "id": "ch_0002", "title": "幽灵章"})
        data["next_chapter_seq"] = 3
        save_novel(data, library_dir=lib)

        result = validate_tree_vs_disk(nid, library_dir=lib)
        assert "ch_0099" in result["orphans"]
        assert "ch_0002" in result["missing"]
