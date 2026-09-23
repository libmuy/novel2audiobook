"""
多小说数据层：管理 data/library/ 下 novel.yaml 树结构与章节目录。

核心设计决策：树结构只活在 novel.yaml 里，磁盘上章节目录永远是扁平的。
部/卷的增删和排序只重写 YAML，不移动任何目录，audio_cache 零风险。
"""
import os
import re
import json
import shutil
import threading
from datetime import datetime

import yaml

from src.utils import (
    resolve_path, get_project_root, load_global_config,
    get_chapter_dir as _utils_get_chapter_dir,
)
from src.domain.status_tracker import get_all_chapters_status

try:
    from pypinyin import lazy_pinyin
except ImportError:
    lazy_pinyin = None

LIBRARY_DIR_NAME = os.path.join("data", "library")
NOVEL_FILE_NAME = "novel.yaml"
TRASH_DIR_NAME = ".trash"
SCHEMA_VERSION = 1
VALID_NODE_TYPES = ("part", "volume", "chapter")

# 每本小说一把 RLock，字典本身用全局锁保护
_locks: dict = {}
_locks_guard = threading.Lock()


def _get_novel_lock(novel_id: str) -> threading.RLock:
    with _locks_guard:
        if novel_id not in _locks:
            _locks[novel_id] = threading.RLock()
        return _locks[novel_id]


# 公开别名：调用方（如 API 路由）需要把"读树、改内存、写回"串成一个临界区时用
# 这个，不用伸手拿模块内部的 _get_novel_lock。
novel_lock = _get_novel_lock


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------

# server.library_root 的读取缓存。library_root() 被几乎每个路径函数高频调用，不能每次读盘解析
# YAML；以项目根为键（测试里 monkeypatch PROJECT_ROOT 时自然隔离）。配置被 PATCH 改动后由
# src/api/deps.set_config 调 invalidate_library_root() 清掉，下次调用重新读。
_root_cache: dict = {}


def invalidate_library_root():
    _root_cache.clear()


def library_root(library_dir: str = None) -> str:
    """小说库根目录。显式传入的 library_dir 优先；否则取配置 server.library_root
    （相对项目根或绝对路径），缺省 LIBRARY_DIR_NAME。"""
    if library_dir is not None:
        return library_dir
    project_root = get_project_root()
    name = _root_cache.get(project_root)
    if name is None:
        server_cfg = load_global_config().get("server", {}) or {}
        name = _root_cache[project_root] = server_cfg.get("library_root") or LIBRARY_DIR_NAME
    return resolve_path(name)


def get_novel_dir(novel_id: str, library_dir: str = None) -> str:
    return os.path.join(library_root(library_dir), novel_id)


def get_chapters_dir(novel_id: str, library_dir: str = None) -> str:
    return os.path.join(get_novel_dir(novel_id, library_dir), "chapters")


def get_chapter_dir(novel_id: str, chapter_id: str, library_dir: str = None) -> str:
    """调用 src.utils.get_chapter_dir 做 ID 归一化，避免重复实现。"""
    base = get_chapters_dir(novel_id, library_dir)
    return _utils_get_chapter_dir(chapter_id, base_dir=base)


# --------------------------------------------------------------------------
# 原子写
# --------------------------------------------------------------------------

def _atomic_write_yaml(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _atomic_read_yaml(path: str) -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(f"novel.yaml 不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# --------------------------------------------------------------------------
# 小说 CRUD
# --------------------------------------------------------------------------

def slugify_novel_id(title: str, existing_ids: set) -> str:
    """用 pypinyin 把中文标题转成 nv_<拼音> 形式的 ID；重名时追加 _2 / _3。"""
    if lazy_pinyin is None:
        base = "nv_" + re.sub(r"[^a-z0-9]", "_", title.lower()).strip("_") or "novel"
    else:
        parts = [p for p in lazy_pinyin(title) if p]
        base = "nv_" + "_".join(p.lower() for p in parts) if parts else "nv_novel"
    base = re.sub(r"_+", "_", base).strip("_")
    if not base or base == "nv":
        base = "nv_novel"

    candidate = base
    suffix = 2
    while candidate in existing_ids:
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def list_novels(library_dir: str = None) -> list:
    root = library_root(library_dir)
    if not os.path.isdir(root):
        return []
    results = []
    for entry in sorted(os.listdir(root)):
        novel_path = os.path.join(root, entry)
        if not os.path.isdir(novel_path) or entry.startswith("."):
            continue
        yaml_path = os.path.join(novel_path, NOVEL_FILE_NAME)
        if not os.path.exists(yaml_path):
            continue
        try:
            data = _atomic_read_yaml(yaml_path)
        except Exception:
            continue
        chapters_dir = get_chapters_dir(entry, library_dir)
        chapter_count = 0
        if os.path.isdir(chapters_dir):
            chapter_count = len([
                d for d in os.listdir(chapters_dir)
                if os.path.isdir(os.path.join(chapters_dir, d))
            ])
        # 四态计数：给小说列表页的双色进度条用，避免前端逐本再拉一次 /tree
        # （那样是 N+1，卡片一多列表页就明显变慢）。
        counts = {"total": 0, "unparsed": 0, "parsed": 0, "voiced": 0, "stale": 0}
        for row in get_all_chapters_status(chapters_dir):
            counts["total"] += 1
            counts[row["state"]] = counts.get(row["state"], 0) + 1
        results.append({
            "novel_id": data.get("novel_id", entry),
            "title": data.get("title", ""),
            "description": data.get("description", ""),
            "levels": data.get("levels", {}),
            "chapter_count": chapter_count,
            "updated_at": data.get("updated_at", ""),
            "counts": counts,
        })
    results.sort(key=lambda x: x["title"])
    return results


def create_novel(title: str, description: str = "", levels: dict = None,
                 library_dir: str = None) -> str:
    root = library_root(library_dir)
    os.makedirs(root, exist_ok=True)

    existing_ids = set()
    if os.path.isdir(root):
        existing_ids = {
            d for d in os.listdir(root)
            if os.path.isdir(os.path.join(root, d)) and not d.startswith(".")
        }

    novel_id = slugify_novel_id(title, existing_ids)
    novel_dir = get_novel_dir(novel_id, library_dir)
    os.makedirs(os.path.join(novel_dir, "chapters"), exist_ok=True)

    if levels is None:
        levels = {"part": False, "volume": False}

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data = {
        "schema_version": SCHEMA_VERSION,
        "novel_id": novel_id,
        "title": title,
        "description": description,
        "levels": levels,
        "next_chapter_seq": 1,
        "created_at": now,
        "updated_at": now,
        "tree": [],
    }
    _atomic_write_yaml(os.path.join(novel_dir, NOVEL_FILE_NAME), data)
    return novel_id


def load_novel(novel_id: str, library_dir: str = None) -> dict:
    path = os.path.join(get_novel_dir(novel_id, library_dir), NOVEL_FILE_NAME)
    return _atomic_read_yaml(path)


def save_novel(novel_data: dict, library_dir: str = None) -> None:
    novel_data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    validate_tree(novel_data)
    novel_id = novel_data["novel_id"]
    lock = _get_novel_lock(novel_id)
    with lock:
        path = os.path.join(get_novel_dir(novel_id, library_dir), NOVEL_FILE_NAME)
        _atomic_write_yaml(path, novel_data)


def delete_novel(novel_id: str, library_dir: str = None) -> None:
    root = library_root(library_dir)
    novel_dir = get_novel_dir(novel_id, library_dir)
    if not os.path.isdir(novel_dir):
        return
    trash_root = os.path.join(root, TRASH_DIR_NAME)
    os.makedirs(trash_root, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(trash_root, f"{ts}_{novel_id}")
    shutil.move(novel_dir, dest)


# --------------------------------------------------------------------------
# 树操作
# --------------------------------------------------------------------------

def validate_tree(novel_data: dict) -> None:
    levels = novel_data.get("levels", {})
    tree = novel_data.get("tree", [])
    seen_ids = set()

    def _check(node, parent_type=None, depth=0):
        ntype = node.get("type")
        if ntype not in VALID_NODE_TYPES:
            raise ValueError(f"节点 {node.get('id', '?')} 的 type 非法: {ntype!r}")

        nid = node.get("id")
        if not nid:
            raise ValueError(f"节点缺少 id（type={ntype}, title={node.get('title', '?')!r}）")
        if nid in seen_ids:
            raise ValueError(f"节点 ID 重复: {nid}")
        seen_ids.add(nid)

        # 层级约束
        if ntype == "part" and not levels.get("part", False):
            raise ValueError(f"levels.part 为 false，树中不允许出现 part 节点: {nid}")
        if ntype == "volume" and not levels.get("volume", False):
            raise ValueError(f"levels.volume 为 false，树中不允许出现 volume 节点: {nid}")

        children = node.get("children")
        if ntype == "chapter":
            if children:
                raise ValueError(f"chapter 节点 {nid} 不能拥有 children")
            return

        # part 的 children 只能是 volume 或 chapter；volume 的 children 只能是 chapter
        allowed_child_types = {
            "part": ("volume", "chapter"),
            "volume": ("chapter"),
        }
        if children:
            for child in children:
                child_type = child.get("type")
                if child_type not in allowed_child_types.get(ntype, ()):
                    raise ValueError(
                        f"节点 {nid}（type={ntype}）的 children 不允许包含 type={child_type!r}"
                    )
                _check(child, parent_type=ntype, depth=depth + 1)

    for node in tree:
        _check(node)

    # next_chapter_seq 必须大于树中所有章节序号的最大值
    max_seq = 0
    for ch_id in _collect_chapter_ids(tree):
        m = re.match(r"ch_(\d+)", ch_id)
        if m:
            max_seq = max(max_seq, int(m.group(1)))
    next_seq = novel_data.get("next_chapter_seq", 1)
    if next_seq <= max_seq:
        raise ValueError(
            f"next_chapter_seq ({next_seq}) 必须大于树中最大章节序号 ({max_seq})"
        )


def _collect_chapter_ids(tree: list) -> list:
    ids = []
    for node in tree:
        if node.get("type") == "chapter":
            ids.append(node.get("id"))
        children = node.get("children")
        if children:
            ids.extend(_collect_chapter_ids(children))
    return ids


def find_node(novel_data: dict, node_id: str) -> tuple:
    """返回 (node, siblings_list, index)；找不到返回 (None, None, -1)。"""
    def _search(nodes):
        for i, node in enumerate(nodes):
            if node.get("id") == node_id:
                return node, nodes, i
            children = node.get("children")
            if children:
                result = _search(children)
                if result[0] is not None:
                    return result
        return (None, None, -1)

    return _search(novel_data.get("tree", []))


def tree_insert(novel_data: dict, parent_id: str, node: dict, index: int = None) -> None:
    """parent_id 为 None 表示插到根。index 为 None 表示追加到末尾。"""
    if parent_id is None:
        target_list = novel_data.setdefault("tree", [])
    else:
        parent_node, _, _ = find_node(novel_data, parent_id)
        if parent_node is None:
            raise ValueError(f"父节点不存在: {parent_id}")
        target_list = parent_node.setdefault("children", [])

    if index is None:
        target_list.append(node)
    else:
        target_list.insert(index, node)


def tree_move(novel_data: dict, node_id: str, new_parent_id: str, new_index: int) -> None:
    """拖拽排序用。防止把节点移动到它自己的子树里。"""
    node, siblings, idx = find_node(novel_data, node_id)
    if node is None:
        raise ValueError(f"节点不存在: {node_id}")

    # 检查 new_parent_id 是否是 node 的后代
    if new_parent_id is not None:
        descendants = _collect_subtree_ids(node)
        if new_parent_id in descendants:
            raise ValueError(f"不能把节点 {node_id} 移动到它自己的子树 ({new_parent_id}) 内")

    # 从原位置移除
    siblings.pop(idx)

    # 插入新位置
    tree_insert(novel_data, new_parent_id, node, new_index)


def _collect_subtree_ids(node: dict) -> set:
    ids = set()
    children = node.get("children", [])
    for child in children:
        ids.add(child.get("id"))
        ids.update(_collect_subtree_ids(child))
    return ids


def tree_rename(novel_data: dict, node_id: str, title: str) -> None:
    node, _, _ = find_node(novel_data, node_id)
    if node is None:
        raise ValueError(f"节点不存在: {node_id}")
    node["title"] = title


def tree_delete(novel_data: dict, node_id: str) -> list:
    """从树里摘掉该节点及其整棵子树，返回受影响的 chapter_id 列表。
    只改树，不碰磁盘——磁盘目录的搬迁由调用方处理，见 delete_node()。"""
    node, siblings, idx = find_node(novel_data, node_id)
    if node is None:
        raise ValueError(f"节点不存在: {node_id}")
    siblings.pop(idx)
    return _collect_chapter_ids([node])


def _collect_all_node_ids(tree: list) -> list:
    ids = []
    for node in tree:
        ids.append(node.get("id"))
        children = node.get("children")
        if children:
            ids.extend(_collect_all_node_ids(children))
    return ids


def generate_node_id(novel_data: dict, node_type: str) -> str:
    """为新建的 part/volume 节点生成不重复的 ID（如 vol_001）。
    chapter 节点走 alloc_chapter_id，不要用这个函数。"""
    prefix = node_type[:3]
    existing_ids = set(_collect_all_node_ids(novel_data.get("tree", [])))
    suffix = 1
    candidate = f"{prefix}_{suffix:03d}"
    while candidate in existing_ids:
        suffix += 1
        candidate = f"{prefix}_{suffix:03d}"
    return candidate


def iter_chapters(novel_data: dict, node_id: str = None) -> list:
    """深度优先按树的顺序返回 chapter_id 列表。node_id 为 None 表示整本。"""
    if node_id is None:
        return _collect_chapter_ids(novel_data.get("tree", []))
    node, _, _ = find_node(novel_data, node_id)
    if node is None:
        raise ValueError(f"节点不存在: {node_id}")
    return _collect_chapter_ids([node])


# --------------------------------------------------------------------------
# 章节
# --------------------------------------------------------------------------

def alloc_chapter_id(novel_data: dict) -> str:
    seq = novel_data.get("next_chapter_seq", 1)
    ch_id = f"ch_{seq:04d}"
    novel_data["next_chapter_seq"] = seq + 1
    return ch_id


def create_node(novel_id: str, node_type: str, title: str,
                parent_id: str = None, library_dir: str = None) -> str:
    """新建一个 part/volume/chapter 节点并挂到树上，返回新节点 ID。
    chapter 节点只分配 ID、插入树，不建目录——目录留给后续的 raw 上传/add_chapter 负责，
    这样 POST /nodes（先建空节点）+ PUT .../raw（后传正文）两步式流程才能成立。"""
    lock = _get_novel_lock(novel_id)
    with lock:
        novel_data = load_novel(novel_id, library_dir)
        if node_type == "chapter":
            node_id = alloc_chapter_id(novel_data)
        else:
            node_id = generate_node_id(novel_data, node_type)
        node = {"type": node_type, "id": node_id, "title": title}
        tree_insert(novel_data, parent_id, node)
        save_novel(novel_data, library_dir)
    return node_id


def add_chapter(novel_id: str, title: str, raw_text: str,
                parent_id: str = None, library_dir: str = None) -> str:
    lock = _get_novel_lock(novel_id)
    with lock:
        novel_data = load_novel(novel_id, library_dir)
        ch_id = alloc_chapter_id(novel_data)
        chapter_dir = get_chapter_dir(novel_id, ch_id, library_dir)
        os.makedirs(chapter_dir, exist_ok=True)
        with open(os.path.join(chapter_dir, "raw.txt"), "w", encoding="utf-8") as f:
            f.write(raw_text)
        node = {"type": "chapter", "id": ch_id, "title": title}
        tree_insert(novel_data, parent_id, node)
        save_novel(novel_data, library_dir)
    return ch_id


def promote_draft_to_final(novel_id: str, chapter_id: str, library_dir: str = None) -> bool:
    """只有 script_draft.json、还没有 script_final.json 的章节（解析过但没跑过
    TTS/没被工作台保存过）第一次被编辑时，把草稿原子地"转正"成正稿——工作台
    分块编辑接口只认 script_final.json，之前会对这类章节一律 404，用户必须先
    走一次容量很小的"整章覆盖" PUT /script 才能开始编辑，体验上像是隐藏步骤。
    final 已存在则不动（不覆盖已经人工定过稿的内容），返回是否发生了转正。"""
    chapter_dir = get_chapter_dir(novel_id, chapter_id, library_dir)
    final_path = os.path.join(chapter_dir, "script_final.json")
    draft_path = os.path.join(chapter_dir, "script_draft.json")
    if os.path.exists(final_path) or not os.path.exists(draft_path):
        return False
    lock = _get_novel_lock(novel_id)
    with lock:
        if os.path.exists(final_path):  # 双重检查：拿锁期间可能已被并发请求转正
            return False
        with open(draft_path, "r", encoding="utf-8") as f:
            data = f.read()
        tmp_path = final_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp_path, final_path)
    return True


def import_chapter_raw(novel_id: str, chapter_id: str, raw_text: str,
                       library_dir: str = None) -> dict:
    lock = _get_novel_lock(novel_id)
    with lock:
        chapter_dir = get_chapter_dir(novel_id, chapter_id, library_dir)
        if not os.path.isdir(chapter_dir):
            raise ValueError(f"章节目录不存在: {chapter_dir}")

        # 清空下游产物，保留 audio_cache/
        removed = []
        for fname in ("script_draft.json", "script_final.json", "timeline.json"):
            fpath = os.path.join(chapter_dir, fname)
            if os.path.exists(fpath):
                os.remove(fpath)
                removed.append(fname)
        output_dir = os.path.join(chapter_dir, "output")
        if os.path.isdir(output_dir):
            shutil.rmtree(output_dir)
            removed.append("output/")

        # 重写 raw.txt
        with open(os.path.join(chapter_dir, "raw.txt"), "w", encoding="utf-8") as f:
            f.write(raw_text)

        # 统计保留的 audio_cache
        cache_dir = os.path.join(chapter_dir, "audio_cache")
        kept = 0
        if os.path.isdir(cache_dir):
            kept = len([f for f in os.listdir(cache_dir) if f.endswith(".wav")])

    return {"removed": removed, "kept_cache_count": kept}


def _move_chapter_dir_to_trash(novel_id: str, chapter_id: str, library_dir: str = None) -> bool:
    """把单个章节目录移到 data/library/<nid>/.trash/ 下；目录不存在时什么也不做。
    返回是否真的移动了（供调用方统计）。"""
    chapter_dir = get_chapter_dir(novel_id, chapter_id, library_dir)
    if not os.path.isdir(chapter_dir):
        return False
    trash_dir = os.path.join(get_novel_dir(novel_id, library_dir), TRASH_DIR_NAME)
    os.makedirs(trash_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.move(chapter_dir, os.path.join(trash_dir, f"{ts}_{chapter_id}"))
    return True


def delete_chapter(novel_id: str, chapter_id: str, library_dir: str = None) -> None:
    lock = _get_novel_lock(novel_id)
    with lock:
        novel_data = load_novel(novel_id, library_dir)
        from src.utils import normalize_chapter_id
        ch_id = normalize_chapter_id(chapter_id)
        tree_delete(novel_data, ch_id)
        save_novel(novel_data, library_dir)
        _move_chapter_dir_to_trash(novel_id, ch_id, library_dir)


def delete_node(novel_id: str, node_id: str, library_dir: str = None) -> list:
    """删除树中的一个节点（part/volume/chapter 均可，含整棵子树），
    并把受影响的每个章节目录移到 .trash/（不会用 rmtree 直接删）。
    返回受影响的 chapter_id 列表。"""
    lock = _get_novel_lock(novel_id)
    with lock:
        novel_data = load_novel(novel_id, library_dir)
        affected = tree_delete(novel_data, node_id)
        save_novel(novel_data, library_dir)
        for ch_id in affected:
            _move_chapter_dir_to_trash(novel_id, ch_id, library_dir)
    return affected


# --------------------------------------------------------------------------
# 一致性
# --------------------------------------------------------------------------

def validate_tree_vs_disk(novel_id: str, library_dir: str = None) -> dict:
    novel_data = load_novel(novel_id, library_dir)
    tree_chapters = set(iter_chapters(novel_data))

    chapters_dir = get_chapters_dir(novel_id, library_dir)
    disk_chapters = set()
    if os.path.isdir(chapters_dir):
        disk_chapters = {
            d for d in os.listdir(chapters_dir)
            if os.path.isdir(os.path.join(chapters_dir, d)) and not d.startswith(".")
        }

    return {
        "orphans": sorted(disk_chapters - tree_chapters),
        "missing": sorted(tree_chapters - disk_chapters),
    }
