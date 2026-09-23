"""小说路由"""
from fastapi import APIRouter, HTTPException
from src.domain import library
from src.api.schemas import NovelCreate, NovelUpdate, NodeCreate, NodeUpdate, NodeReorder
import os

router = APIRouter(prefix="/novels", tags=["novels"])


@router.get("")
def list_novels():
    return library.list_novels()


@router.post("")
def create_novel(data: NovelCreate):
    novel_id = library.create_novel(data.title, data.description, data.levels)
    return {"novel_id": novel_id}


@router.get("/{nid}")
def get_novel(nid: str):
    try:
        return library.load_novel(nid)
    except FileNotFoundError:
        raise HTTPException(404, f"小说 {nid} 不存在")


@router.patch("/{nid}")
def update_novel(nid: str, data: NovelUpdate):
    novel = library.load_novel(nid)
    if data.title is not None:
        novel["title"] = data.title
    if data.description is not None:
        novel["description"] = data.description
    library.save_novel(novel)
    return {"ok": True}


@router.delete("/{nid}")
def delete_novel(nid: str):
    library.delete_novel(nid)
    return {"ok": True}


def _merge_status_into_tree(nodes: list, status_by_chapter: dict) -> None:
    """把每个 chapter 节点的状态摘要内联进树（原地修改），前端不用自己拿
    两份平行数据再拼一遍。只影响本次响应，不会写回 novel.yaml。"""
    for node in nodes:
        if node.get("type") == "chapter":
            ch_status = status_by_chapter.get(node.get("id"))
            if ch_status:
                node["status"] = ch_status.get("status")
        children = node.get("children")
        if children:
            _merge_status_into_tree(children, status_by_chapter)


def _read_json_or_none(path: str):
    """读一个 JSON；文件不存在、读不了或格式损坏都当作「没有」。"""
    import json
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


@router.get("/{nid}/chapter-stats")
def get_chapter_stats(nid: str):
    """每章的分块数字：segment_count（script_final.json 的分块数）、voiced_count
    （timeline.json 的条目数，即已合成的句子数）、unbound_count（分块里没有绑定
    角色的数量）。

    要逐章解析两个 JSON，所以**不**塞进 /tree 那条每次进页面都走的热路径，前端
    只在对比表可见时才来取。缺 script/timeline 的章节各项给 0，不 404——「还没
    解析」是正常状态，不是错误。"""
    try:
        library.load_novel(nid)
    except FileNotFoundError:
        raise HTTPException(404, f"小说 {nid} 不存在")
    chapters_dir = library.get_chapters_dir(nid)
    stats = {}
    if os.path.isdir(chapters_dir):
        for cid in sorted(os.listdir(chapters_dir)):
            ch_dir = os.path.join(chapters_dir, cid)
            if not os.path.isdir(ch_dir):
                continue
            script = _read_json_or_none(os.path.join(ch_dir, "script_final.json"))
            segments = script if isinstance(script, list) else []
            timeline = _read_json_or_none(os.path.join(ch_dir, "timeline.json"))
            items = timeline.get("items") if isinstance(timeline, dict) else None
            stats[cid] = {
                "segment_count": len(segments),
                "voiced_count": len(items) if isinstance(items, list) else 0,
                "unbound_count": sum(1 for s in segments if isinstance(s, dict) and not s.get("speaker")),
            }
    return {"chapters": stats}


@router.get("/{nid}/tree")
def get_novel_tree(nid: str):
    from src.domain.status_tracker import get_novel_status_summary
    try:
        novel = library.load_novel(nid)
    except FileNotFoundError:
        raise HTTPException(404, f"小说 {nid} 不存在")
    status_summary = get_novel_status_summary(nid)
    status_by_chapter = {c["chapter_id"]: c for c in status_summary.get("chapters", [])}
    _merge_status_into_tree(novel.get("tree", []), status_by_chapter)
    return {"novel": novel, "status": status_summary}


@router.post("/{nid}/nodes")
def create_node(nid: str, data: NodeCreate):
    try:
        node_id = library.create_node(nid, data.type, data.title, data.parent_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "node_id": node_id}


@router.patch("/{nid}/nodes/{node_id}")
def update_node(nid: str, node_id: str, data: NodeUpdate):
    novel = library.load_novel(nid)
    # Find and update node title
    def _find_and_update(nodes, target_id, new_title):
        for n in nodes:
            if n.get("id") == target_id:
                if new_title is not None:
                    n["title"] = new_title
                return True
            if _find_and_update(n.get("children", []), target_id, new_title):
                return True
        return False

    if data.title is not None:
        _find_and_update(novel.get("tree", []), node_id, data.title)
        library.save_novel(novel)
    return {"ok": True}


@router.post("/{nid}/nodes/reorder")
def reorder_node(nid: str, data: NodeReorder):
    novel = library.load_novel(nid)
    library.tree_move(novel, data.node_id, data.new_parent_id, data.new_index)
    library.save_novel(novel)
    return {"ok": True}


@router.delete("/{nid}/nodes/{node_id}")
def delete_node(nid: str, node_id: str, confirm: bool = False):
    novel = library.load_novel(nid)
    try:
        affected = library.iter_chapters(novel, node_id)
    except ValueError:
        raise HTTPException(404, f"节点 {node_id} 不存在")

    if not confirm:
        # 预览模式：只读、不改树也不碰磁盘，真正删除见下面 confirm 分支
        has_audio = False
        for ch_id in affected:
            ch_dir = library.get_chapter_dir(nid, ch_id)
            if os.path.isdir(os.path.join(ch_dir, "output")):
                has_audio = True
                break
        return {"affected_chapters": len(affected), "has_audio": has_audio, "confirmed": False}

    # 确认删除：从树里摘掉节点并把每个受影响章节目录移到 .trash/（不用 rmtree）
    affected = library.delete_node(nid, node_id)
    return {"ok": True, "confirmed": True, "affected_chapters": len(affected)}
