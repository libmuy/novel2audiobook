"""分块路由"""
import json
import os
from fastapi import APIRouter, HTTPException
from src.domain import library
from src.api.schemas import SegmentUpdate, SegmentBatch
from src.utils import list_available_assets

router = APIRouter(prefix="/novels/{nid}/chapters/{cid}/segments", tags=["segments"])


def _validate_asset_name(kind: str, name):
    """校验用户显式指定的 sfx/bgm 素材名真的存在。跟 llm_parser.py 对 LLM
    输出的处理不同——那边是把认不出的名字静默纠正成 None（模型幻觉，不能
    阻断解析流程），这里是用户在界面上手动选的，选错了应该报错，不能悄悄丢弃。"""
    if name is None:
        return
    available = list_available_assets().get(kind, [])
    if name not in available:
        raise HTTPException(400, f"素材 {name} 不存在（{kind}）")


def _get_script_path(nid: str, cid: str) -> str:
    # 见 chapters.py 里同样的注释：委托给 library.get_chapter_dir 做动态路径解析
    return os.path.join(library.get_chapter_dir(nid, cid), "script_final.json")


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
def update_segment(nid: str, cid: str, seg_id: str, data: SegmentUpdate):
    # exclude_unset：只应用请求体里实际出现的字段，
    # 这样 speaker 传 null 才能跟"没传 speaker"区分开（清空绑定 vs 不改）
    updates = data.model_dump(exclude_unset=True)
    if "sfx" in updates:
        _validate_asset_name("sfx", updates["sfx"])
    if "bgm" in updates:
        _validate_asset_name("bgm", updates["bgm"])
    script = _load_script(nid, cid)
    for seg in script:
        if str(seg.get("seg_id")) == str(seg_id):
            if "text" in updates and updates["text"] is not None:
                seg["text"] = updates["text"]
            if "speaker" in updates:
                seg["speaker"] = updates["speaker"]
            if "emotion" in updates and updates["emotion"] is not None:
                seg["emotion"] = updates["emotion"]
            if "sfx" in updates:
                seg["sfx"] = updates["sfx"]
            if "bgm" in updates:
                seg["bgm"] = updates["bgm"]
            _save_script(nid, cid, script)
            return {"ok": True}
    raise HTTPException(404, f"分块 {seg_id} 不存在")


@router.post("/batch")
def batch_update_segments(nid: str, cid: str, data: SegmentBatch):
    seg_ids = data.seg_ids
    updates = data.set
    if "sfx" in updates:
        _validate_asset_name("sfx", updates["sfx"])
    if "bgm" in updates:
        _validate_asset_name("bgm", updates["bgm"])
    script = _load_script(nid, cid)
    updated = 0
    for seg in script:
        if str(seg.get("seg_id")) in [str(s) for s in seg_ids]:
            if "speaker" in updates:
                seg["speaker"] = updates["speaker"]
            if "emotion" in updates and updates["emotion"] is not None:
                seg["emotion"] = updates["emotion"]
            if "sfx" in updates:
                seg["sfx"] = updates["sfx"]
            if "bgm" in updates:
                seg["bgm"] = updates["bgm"]
            updated += 1
    _save_script(nid, cid, script)
    return {"ok": True, "updated": updated}
