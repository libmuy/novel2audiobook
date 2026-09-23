"""
通用分类树（角色分类，以后素材分类也可以复用）。

节点形状：{"id": str, "title": str, "children": [节点, ...]}——跟 library.py 的
小说树同一套 id/title/children 约定，但**不复用 library.py 本体**：
library.validate_tree 硬编码了 part/volume/chapter 三种类型和 levels.* 校验，
save_novel 又无条件调用它，分类树根本走不进那条持久化路径。这里只借它的
纯列表递归思路（find / insert / move 含环检测 / rename / delete），全部操作
一个普通的 list，不碰磁盘。

分类的扁平投影（flatten_paths）：深度优先、"/" 连接的路径，如
["主角", "主角/男主", "配角"]。它就是 roles_manifest.json 里既有的扁平
`categories` 数组——树是真相源，扁平数组每次树变更后重新生成，旧前端和旧测试
继续读它，不用改。
"""
import uuid

MAX_DEPTH = 4
PATH_SEP = "/"


class TreeError(ValueError):
    """树结构不合法 / 操作对象不存在。"""


def new_id() -> str:
    return "cat_" + uuid.uuid4().hex[:8]


def find_node(tree: list, node_id: str) -> tuple:
    """返回 (node, siblings_list, index)；找不到返回 (None, None, -1)。"""
    for i, node in enumerate(tree):
        if node.get("id") == node_id:
            return node, tree, i
        found = find_node(node.get("children") or [], node_id)
        if found[0] is not None:
            return found
    return None, None, -1


def collect_subtree_ids(node: dict) -> set:
    ids = set()
    for child in node.get("children") or []:
        ids.add(child.get("id"))
        ids |= collect_subtree_ids(child)
    return ids


def insert_node(tree: list, parent_id, node: dict, index: int = None) -> None:
    """parent_id 为 None 插到根；index 为 None 追加到末尾。"""
    if parent_id is None:
        target = tree
    else:
        parent, _, _ = find_node(tree, parent_id)
        if parent is None:
            raise TreeError(f"父节点不存在: {parent_id}")
        target = parent.setdefault("children", [])
    if index is None:
        target.append(node)
    else:
        target.insert(index, node)


def move_node(tree: list, node_id: str, new_parent_id, new_index: int = None) -> None:
    node, siblings, idx = find_node(tree, node_id)
    if node is None:
        raise TreeError(f"节点不存在: {node_id}")
    if new_parent_id is not None and (
            new_parent_id == node_id or new_parent_id in collect_subtree_ids(node)):
        raise TreeError(f"不能把节点 {node_id} 移动到它自己的子树内")
    siblings.pop(idx)
    insert_node(tree, new_parent_id, node, new_index)


def rename_node(tree: list, node_id: str, title: str) -> None:
    node, _, _ = find_node(tree, node_id)
    if node is None:
        raise TreeError(f"节点不存在: {node_id}")
    node["title"] = title


def delete_node(tree: list, node_id: str) -> None:
    """只改树。角色自己的 category 字符串不受影响——跟扁平分类"删除不清空
    引用"同一条容忍策略（tests/test_api.py 里有专门的用例锁着）。"""
    node, siblings, idx = find_node(tree, node_id)
    if node is None:
        raise TreeError(f"节点不存在: {node_id}")
    siblings.pop(idx)


def assign_missing_ids(tree: list) -> list:
    """给没有 id 的节点补 id（原地修改并返回 tree），保证整棵树 id 唯一。"""
    seen = set()

    def walk(nodes):
        for n in nodes:
            nid = n.get("id")
            if not nid or nid in seen:
                nid = new_id()
                while nid in seen:
                    nid = new_id()
                n["id"] = nid
            seen.add(nid)
            n.setdefault("children", [])
            walk(n["children"])

    walk(tree)
    return tree


def validate_tree(tree) -> None:
    """id 唯一、标题非空且不含 "/"（否则扁平路径有歧义）、同级标题不重名
    （否则扁平路径重复）、深度不超过 MAX_DEPTH。"""
    if not isinstance(tree, list):
        raise TreeError("分类树必须是数组")
    seen_ids = set()

    def walk(nodes, depth):
        if depth > MAX_DEPTH:
            raise TreeError(f"分类层级不能超过 {MAX_DEPTH} 层")
        seen_titles = set()
        for n in nodes:
            if not isinstance(n, dict):
                raise TreeError("分类节点必须是对象")
            title = str(n.get("title", "")).strip()
            if not title:
                raise TreeError("分类名称不能为空")
            if PATH_SEP in title:
                raise TreeError(f'分类名称不能包含 "{PATH_SEP}"：{title!r}')
            if title in seen_titles:
                raise TreeError(f"同一层级下分类名称重复：{title!r}")
            seen_titles.add(title)
            nid = n.get("id")
            if nid:
                if nid in seen_ids:
                    raise TreeError(f"分类 id 重复：{nid!r}")
                seen_ids.add(nid)
            children = n.get("children") or []
            if not isinstance(children, list):
                raise TreeError("children 必须是数组")
            walk(children, depth + 1)

    walk(tree, 1)


def normalize_tree(tree: list) -> list:
    """去掉标题首尾空白、只保留 id/title/children 三个字段，返回新树。"""
    return [{
        "id": n.get("id"),
        "title": str(n.get("title", "")).strip(),
        "children": normalize_tree(n.get("children") or []),
    } for n in tree]


def flatten_paths(tree: list, prefix: str = "") -> list:
    out = []
    for n in tree:
        path = f"{prefix}{PATH_SEP}{n['title']}" if prefix else n["title"]
        out.append(path)
        out.extend(flatten_paths(n.get("children") or [], path))
    return out


def _id_path_map(tree: list, prefix: str = "") -> dict:
    out = {}
    for n in tree:
        path = f"{prefix}{PATH_SEP}{n['title']}" if prefix else n["title"]
        nid = n.get("id")
        if nid:
            out[nid] = path
        out.update(_id_path_map(n.get("children") or [], path))
    return out


def diff_paths(old_tree: list, new_tree: list) -> tuple:
    """比较保存前后的两棵树（按 id 对应），返回 (renamed, deleted)：

    - renamed：{旧路径: 新路径}，id 还在新树里但路径变了（改名，或换了父级/
      层级导致路径前缀变了）。父节点改名时，它每个子孙节点也会各自出现一条
      （子孙自己的 id 没变，但路径含父节点标题，一并算出新路径），调用方不
      需要再做前缀匹配。
    - deleted：旧树里存在、新树里 id 已经不存在的节点路径集合（父节点被删时
      整棵子树的 id 都消失了，子孙也会各自出现在这里）。

    调用方用这两个映射去同步"引用了某个分类路径字符串"的记录（角色/素材），
    使删除或重命名分类节点后，引用不会变成指向一个不存在的名字。
    """
    old_map = _id_path_map(old_tree)
    new_map = _id_path_map(new_tree)
    renamed = {}
    deleted = set()
    for nid, old_path in old_map.items():
        new_path = new_map.get(nid)
        if new_path is None:
            deleted.add(old_path)
        elif new_path != old_path:
            renamed[old_path] = new_path
    return renamed, deleted


def remap_category(category: str, renamed: dict, deleted: set) -> str:
    """按 diff_paths 的结果重映射单个引用字符串：删除的分类变回"未分类"
    （空字符串），改名的分类换成新路径，其余原样返回。"""
    if not category:
        return category
    if category in deleted:
        return ""
    return renamed.get(category, category)


def tree_from_flat(names: list) -> list:
    """把旧的扁平 categories 合成成一层根节点（只读时合成，不写盘——用户
    真正保存过树之前不迁移任何数据）。id 按顺序确定性生成，多次 GET 稳定。"""
    return [{"id": f"cat_flat_{i + 1}", "title": str(n), "children": []}
            for i, n in enumerate(names)]
