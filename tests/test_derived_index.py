"""
测试 src/derived_index.py 角色引用统计索引
"""
import json
import os

import pytest
from src import derived_index


@pytest.fixture
def isolated_library(tmp_path, monkeypatch):
    """创建隔离的 library 目录"""
    library_dir = tmp_path / "library"
    os.makedirs(library_dir)
    monkeypatch.setattr(derived_index, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(derived_index, "ROLE_REFS_PATH", str(tmp_path / "cache" / "index" / "role_refs.json"))
    return library_dir


def _create_novel(library_dir, novel_id, chapters):
    """创建一个小说及其章节"""
    for ch_id, segments in chapters.items():
        ch_dir = library_dir / novel_id / "chapters" / ch_id
        os.makedirs(ch_dir, exist_ok=True)
        script_path = ch_dir / "script_final.json"
        with open(script_path, "w", encoding="utf-8") as f:
            json.dump(segments, f, ensure_ascii=False)


class TestComputeFingerprint:
    def test_empty_library(self, isolated_library):
        fp = derived_index.compute_fingerprint(str(isolated_library))
        assert fp == ""

    def test_fingerprint_changes_with_new_file(self, isolated_library):
        _create_novel(isolated_library, "nv1", {
            "ch_0001": [{"speaker": "narrator", "text": "hello"}]
        })
        fp1 = derived_index.compute_fingerprint(str(isolated_library))

        _create_novel(isolated_library, "nv1", {
            "ch_0001": [{"speaker": "narrator", "text": "hello"}],
            "ch_0002": [{"speaker": "narrator", "text": "world"}]
        })
        fp2 = derived_index.compute_fingerprint(str(isolated_library))
        assert fp1 != fp2


class TestBuildRoleRefs:
    def test_basic_counting(self, isolated_library):
        _create_novel(isolated_library, "nv1", {
            "ch_0001": [
                {"speaker": "su_yan", "text": "hello"},
                {"speaker": "narrator", "text": "narration"},
                {"speaker": "su_yan", "text": "world"},
            ]
        })
        refs = derived_index.build_role_refs(str(isolated_library))
        assert refs["roles"]["su_yan"]["segment_count"] == 2
        assert "nv1" in refs["roles"]["su_yan"]["novels"]
        assert refs["roles"]["su_yan"]["by_novel"]["nv1"] == 2

    def test_unbound_segments_not_counted(self, isolated_library):
        _create_novel(isolated_library, "nv1", {
            "ch_0001": [
                {"speaker": None, "text": "unbound"},
                {"speaker": "su_yan", "text": "bound"},
            ]
        })
        refs = derived_index.build_role_refs(str(isolated_library))
        assert "su_yan" in refs["roles"]
        assert refs["roles"]["su_yan"]["segment_count"] == 1
        assert None not in refs["roles"]

    def test_multi_novel_aggregation(self, isolated_library):
        _create_novel(isolated_library, "nv1", {
            "ch_0001": [{"speaker": "su_yan", "text": "a"}]
        })
        _create_novel(isolated_library, "nv2", {
            "ch_0001": [{"speaker": "su_yan", "text": "b"}]
        })
        refs = derived_index.build_role_refs(str(isolated_library))
        assert len(refs["roles"]["su_yan"]["novels"]) == 2
        assert refs["roles"]["su_yan"]["segment_count"] == 2


class TestGetRoleRefs:
    def test_caches_and_rebuilds(self, isolated_library, monkeypatch):
        _create_novel(isolated_library, "nv1", {
            "ch_0001": [{"speaker": "su_yan", "text": "hello"}]
        })

        refs1 = derived_index.get_role_refs(str(isolated_library))
        assert os.path.exists(str(tmp_path := isolated_library.parent / "cache" / "index" / "role_refs.json") or "")

        # 读取缓存
        refs2 = derived_index.get_role_refs(str(isolated_library))
        assert refs1["fingerprint"] == refs2["fingerprint"]

    def test_invalidates_cache(self, isolated_library):
        _create_novel(isolated_library, "nv1", {
            "ch_0001": [{"speaker": "su_yan", "text": "hello"}]
        })
        derived_index.get_role_refs(str(isolated_library))
        assert os.path.exists(str(isolated_library.parent / "cache" / "index" / "role_refs.json"))

        derived_index.invalidate()
        assert not os.path.exists(str(isolated_library.parent / "cache" / "index" / "role_refs.json"))
