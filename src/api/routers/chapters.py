"""章节路由"""
import os
import json
from fastapi import APIRouter, HTTPException, UploadFile, File
from src import library, status_tracker
from src.utils import PROJECT_ROOT
from src.api.deps import get_config
from src import derived_index

router = APIRouter(prefix="/novels/{nid}/chapters", tags=["chapters"])


def _get_chapter_dir(nid: str, cid: str) -> str:
    return os.path.join(PROJECT_ROOT, "library", nid, "chapters", cid)


@router.get("/{cid}")
def get_chapter(nid: str, cid: str):
    ch_dir = _get_chapter_dir(nid, cid)
    if not os.path.isdir(ch_dir):
        raise HTTPException(404, f"章节 {cid} 不存在")
    all_status = status_tracker.get_all_chapters_status(os.path.dirname(ch_dir))
    for s in all_status:
        if s["chapter_id"] == cid:
            return s
    raise HTTPException(404, f"章节 {cid} 不存在")


@router.put("/{cid}/raw")
def upload_raw(nid: str, cid: str, file: UploadFile = File(...), confirm: bool = False):
    ch_dir = _get_chapter_dir(nid, cid)
    raw_path = os.path.join(ch_dir, "raw.txt")
    has_existing = os.path.exists(raw_path)

    if not confirm and has_existing:
        return {"will_overwrite": True, "confirmed": False}

    os.makedirs(ch_dir, exist_ok=True)
    content = file.file.read()
    with open(raw_path, "wb") as f:
        f.write(content)

    return {"ok": True, "confirmed": True}


@router.get("/{cid}/script")
def get_script(nid: str, cid: str):
    ch_dir = _get_chapter_dir(nid, cid)
    final_path = os.path.join(ch_dir, "script_final.json")
    draft_path = os.path.join(ch_dir, "script_draft.json")

    if os.path.exists(final_path):
        with open(final_path, "r", encoding="utf-8") as f:
            return json.load(f)
    if os.path.exists(draft_path):
        with open(draft_path, "r", encoding="utf-8") as f:
            return json.load(f)
    raise HTTPException(404, "无剧本文件")


@router.put("/{cid}/script")
def upload_script(nid: str, cid: str, script: list):
    ch_dir = _get_chapter_dir(nid, cid)
    final_path = os.path.join(ch_dir, "script_final.json")
    os.makedirs(ch_dir, exist_ok=True)
    with open(final_path, "w", encoding="utf-8") as f:
        json.dump(script, f, ensure_ascii=False, indent=2)
    derived_index.invalidate()
    return {"ok": True}
