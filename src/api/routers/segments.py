"""分块路由"""
import json
import os
from fastapi import APIRouter, HTTPException
from src.utils import PROJECT_ROOT

router = APIRouter(prefix="/novels/{nid}/chapters/{cid}/segments", tags=["segments"])


def _get_script_path(nid: str, cid: str) -> str:
    return os.path.join(PROJECT_ROOT, "library", nid, "chapters", cid, "script_final.json")


def _load_script(nid: str, cid: str) -> list:
    path = _get_script_path(nid, cid)
    if not os.path.exists(path):
        raise HTTPException(404, "无剧本文件")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_script(nid: str, cid: str, script: list):
    path = _get_script_path(nid, cid)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(script, f, ensure_ascii=False, indent=2)


@router.patch("/{seg_id}")
def update_segment(nid: str, cid: str, seg_id: str, data: dict):
    from src.api.schemas import SegmentUpdate
    script = _load_script(nid, cid)
    for seg in script:
        if str(seg.get("seg_id")) == str(seg_id):
            if "text" in data and data["text"] is not None:
                seg["text"] = data["text"]
            if "speaker" in data:
                seg["speaker"] = data["speaker"]
            if "emotion" in data and data["emotion"] is not None:
                seg["emotion"] = data["emotion"]
            _save_script(nid, cid, script)
            return {"ok": True}
    raise HTTPException(404, f"分块 {seg_id} 不存在")


@router.post("/batch")
def batch_update_segments(nid: str, cid: str, data: dict):
    seg_ids = data.get("seg_ids", [])
    updates = data.get("set", {})
    script = _load_script(nid, cid)
    updated = 0
    for seg in script:
        if str(seg.get("seg_id")) in [str(s) for s in seg_ids]:
            if "speaker" in updates:
                seg["speaker"] = updates["speaker"]
            if "emotion" in updates and updates["emotion"] is not None:
                seg["emotion"] = updates["emotion"]
            updated += 1
    _save_script(nid, cid, script)
    return {"ok": True, "updated": updated}
