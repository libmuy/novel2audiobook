"""小说路由"""
from fastapi import APIRouter, HTTPException
from src import library
from src.api.schemas import NovelCreate, NovelUpdate, NodeCreate, NodeUpdate, NodeReorder
from src.utils import PROJECT_ROOT
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


@router.get("/{nid}/tree")
def get_novel_tree(nid: str):
    from src.status_tracker import get_novel_status_summary
    try:
        novel = library.load_novel(nid)
    except FileNotFoundError:
        raise HTTPException(404, f"小说 {nid} 不存在")
    status_summary = get_novel_status_summary(nid)
    return {"novel": novel, "status": status_summary}


@router.post("/{nid}/nodes")
def create_node(nid: str, data: NodeCreate):
    novel = library.load_novel(nid)
    node = {"type": data.type, "title": data.title}
    library.tree_insert(novel, data.parent_id, node)
    library.save_novel(novel)
    return {"ok": True}


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
    affected = library.tree_delete(novel, node_id)
    if not confirm:
        has_audio = False
        for ch_id in affected:
            ch_dir = os.path.join(PROJECT_ROOT, "library", nid, "chapters", ch_id)
            output_dir = os.path.join(ch_dir, "output")
            if os.path.exists(output_dir):
                has_audio = True
                break
        return {"affected_chapters": len(affected), "has_audio": has_audio, "confirmed": False}
    library.save_novel(novel)
    return {"ok": True, "confirmed": True}
