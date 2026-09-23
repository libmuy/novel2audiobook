"""src/domain/category_tree.py 纯函数测试（风格对齐 tests/test_library.py 的树操作测试）"""
import pytest
from src.domain import category_tree as ct


def _tree():
    return [
        {"id": "a", "title": "主角", "children": [
            {"id": "a1", "title": "男主", "children": []},
            {"id": "a2", "title": "女主", "children": []},
        ]},
        {"id": "b", "title": "配角", "children": []},
    ]


class TestOps:
    def test_find_node(self):
        node, siblings, idx = ct.find_node(_tree(), "a2")
        assert node["title"] == "女主" and idx == 1 and len(siblings) == 2
        assert ct.find_node(_tree(), "nope") == (None, None, -1)

    def test_insert_root_child_and_index(self):
        t = _tree()
        ct.insert_node(t, None, {"id": "c", "title": "路人", "children": []})
        ct.insert_node(t, "a", {"id": "a0", "title": "少年", "children": []}, index=0)
        assert [n["id"] for n in t] == ["a", "b", "c"]
        assert t[0]["children"][0]["id"] == "a0"

    def test_insert_under_missing_parent_raises(self):
        with pytest.raises(ct.TreeError):
            ct.insert_node(_tree(), "nope", {"id": "x", "title": "x", "children": []})

    def test_move_between_parents(self):
        t = _tree()
        ct.move_node(t, "a2", "b")
        assert [c["id"] for c in t[0]["children"]] == ["a1"]
        assert [c["id"] for c in t[1]["children"]] == ["a2"]

    def test_move_into_own_subtree_rejected(self):
        t = _tree()
        with pytest.raises(ct.TreeError):
            ct.move_node(t, "a", "a1")
        with pytest.raises(ct.TreeError):
            ct.move_node(t, "a", "a")
        assert ct.find_node(t, "a1")[0] is not None  # 拒绝之后树没被破坏

    def test_rename_and_delete_subtree(self):
        t = _tree()
        ct.rename_node(t, "b", "反派")
        assert t[1]["title"] == "反派"
        ct.delete_node(t, "a")
        assert [n["id"] for n in t] == ["b"]
        assert ct.find_node(t, "a1")[0] is None  # 整棵子树一起没了

    def test_delete_missing_raises(self):
        with pytest.raises(ct.TreeError):
            ct.delete_node(_tree(), "nope")


class TestFlatten:
    def test_flatten_paths_depth_first(self):
        assert ct.flatten_paths(_tree()) == ["主角", "主角/男主", "主角/女主", "配角"]

    def test_tree_from_flat_roundtrip(self):
        t = ct.tree_from_flat(["主角", "配角"])
        assert ct.flatten_paths(t) == ["主角", "配角"]
        assert ct.tree_from_flat(["主角", "配角"]) == t  # id 确定性，多次合成稳定


class TestValidate:
    def test_valid_tree_passes(self):
        ct.validate_tree(_tree())

    @pytest.mark.parametrize("bad", [
        "not a list",
        [{"title": "  "}],
        [{"title": "a/b"}],
        [{"title": "x"}, {"title": "x"}],
        [{"id": "d", "title": "x"}, {"id": "d", "title": "y"}],
    ])
    def test_rejections(self, bad):
        with pytest.raises(ct.TreeError):
            ct.validate_tree(bad)

    def test_same_title_under_different_parents_is_fine(self):
        ct.validate_tree([{"title": "甲", "children": [{"title": "x"}]},
                          {"title": "乙", "children": [{"title": "x"}]}])

    def test_depth_limit(self):
        deep = [{"title": "1", "children": [{"title": "2", "children": [{"title": "3", "children": [
            {"title": "4", "children": [{"title": "5"}]}]}]}]}]
        with pytest.raises(ct.TreeError):
            ct.validate_tree(deep)


class TestAssignAndNormalize:
    def test_assign_missing_ids_unique_and_keeps_existing(self):
        t = [{"title": "a", "children": [{"title": "b"}]}, {"id": "keep", "title": "c"}]
        ct.assign_missing_ids(t)
        ids = [t[0]["id"], t[0]["children"][0]["id"], t[1]["id"]]
        assert len(set(ids)) == 3 and t[1]["id"] == "keep"

    def test_duplicate_ids_get_reassigned(self):
        t = [{"id": "d", "title": "a"}, {"id": "d", "title": "b"}]
        ct.assign_missing_ids(t)
        assert t[0]["id"] != t[1]["id"]

    def test_normalize_strips_and_drops_extra_fields(self):
        out = ct.normalize_tree([{"id": "x", "title": "  甲 ", "junk": 1, "children": []}])
        assert out == [{"id": "x", "title": "甲", "children": []}]
