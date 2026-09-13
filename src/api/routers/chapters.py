"""章节路由"""
import os
import json
from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from src import library, status_tracker
from src.api.deps import get_config
from src import derived_index
from src.utils import calculate_md5

router = APIRouter(prefix="/novels/{nid}/chapters", tags=["chapters"])


def _get_chapter_dir(nid: str, cid: str) -> str:
    # 委托给 library.get_chapter_dir：它用 resolve_path() 在调用时动态读取
    # PROJECT_ROOT，而不是像 `from src.utils import PROJECT_ROOT` 那样在模块
    # 导入时把值冻结下来——后者会导致测试里 monkeypatch PROJECT_ROOT 不生效，
    # 请求实际落到真实项目目录（这个坑之前是真的踩过一次，见 005 review）。
    return library.get_chapter_dir(nid, cid)


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

    # 新章节（还没有 raw.txt）：直接写入，不涉及"重新导入"的覆盖语义
    if not has_existing:
        os.makedirs(ch_dir, exist_ok=True)
        content = file.file.read()
        with open(raw_path, "wb") as f:
            f.write(content)
        return {"ok": True, "confirmed": True}

    # 已有 raw.txt：这是"重新导入"，会清空下游产物（保留 audio_cache）
    if not confirm:
        will_remove = [
            fname for fname in ("script_draft.json", "script_final.json", "timeline.json")
            if os.path.exists(os.path.join(ch_dir, fname))
        ]
        if os.path.isdir(os.path.join(ch_dir, "output")):
            will_remove.append("output/")
        cache_dir = os.path.join(ch_dir, "audio_cache")
        kept_cache_count = (
            len([f for f in os.listdir(cache_dir) if f.endswith(".wav")])
            if os.path.isdir(cache_dir) else 0
        )
        return {
            "will_overwrite": True, "confirmed": False,
            "will_remove": will_remove, "kept_cache_count": kept_cache_count,
        }

    content = file.file.read()
    result = library.import_chapter_raw(nid, cid, content.decode("utf-8"))
    return {"ok": True, "confirmed": True, **result}


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


@router.get("/{cid}/timeline")
def get_timeline(nid: str, cid: str):
    ch_dir = _get_chapter_dir(nid, cid)
    path = os.path.join(ch_dir, "timeline.json")
    if not os.path.exists(path):
        raise HTTPException(404, "无时间线文件（该章节还没有跑过 TTS）")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@router.get("/{cid}/segments/{seg_id}/audio")
def get_segment_audio(nid: str, cid: str, seg_id: str):
    """分块本身不存 md5，音频缓存文件名是 speaker+text+emotion 的哈希，
    这里复用 src.tts_engine 里同样的算法现算一次去查 audio_cache/。"""
    ch_dir = _get_chapter_dir(nid, cid)
    final_path = os.path.join(ch_dir, "script_final.json")
    if not os.path.exists(final_path):
        raise HTTPException(404, "无定稿剧本")
    with open(final_path, "r", encoding="utf-8") as f:
        segments = json.load(f)
    seg = next((s for s in segments if str(s.get("seg_id")) == str(seg_id)), None)
    if seg is None:
        raise HTTPException(404, f"分块 {seg_id} 不存在")
    speaker = seg.get("speaker")
    if not speaker:
        raise HTTPException(404, "该分块尚未绑定角色，不存在人声")
    key = f"{speaker}_{seg.get('text', '')}_{seg.get('emotion', 'neutral')}"
    audio_path = os.path.join(ch_dir, "audio_cache", f"{calculate_md5(key)}.wav")
    if not os.path.exists(audio_path):
        raise HTTPException(404, "该分块尚未合成")
    return FileResponse(audio_path, media_type="audio/wav")


@router.get("/{cid}/output.mp3")
def get_output_audio(nid: str, cid: str):
    ch_dir = _get_chapter_dir(nid, cid)
    output_dir = os.path.join(ch_dir, "output")
    if os.path.isdir(output_dir):
        mp3s = [f for f in os.listdir(output_dir) if f.endswith(".mp3")]
        if mp3s:
            return FileResponse(os.path.join(output_dir, mp3s[0]), media_type="audio/mpeg")
    raise HTTPException(404, "还没有生成成品 MP3")
