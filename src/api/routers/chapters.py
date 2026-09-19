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


@router.get("/{cid}/assets")
def get_chapter_assets(nid: str, cid: str):
    """本章引用的素材、缺失的素材、以及是否需要重新混音。

    "引用" 直接读 timeline.json（没有 timeline 就退到 script_final.json，
    该章还没跑过 TTS 的情况），跟真实素材库（utils.list_available_assets）
    做差得到缺失列表；这部分是"timeline 里写了什么"，跟下面 mix 字段（
    "上一次真正混音时发生了什么"，来自 mix_meta.json sidecar）是两个独立的
    信息源——章节可能引用了素材但还没重新混音，也可能混过音之后又编辑了
    分块的 sfx/bgm，这时 referenced 和 mix.bgm_names 会不一致，那正是
    "需要重新混音"的信号。"""
    from src.utils import list_available_assets

    ch_dir = _get_chapter_dir(nid, cid)
    if not os.path.isdir(ch_dir):
        raise HTTPException(404, f"章节 {cid} 不存在")

    timeline_path = os.path.join(ch_dir, "timeline.json")
    script_path = os.path.join(ch_dir, "script_final.json")
    items = None
    if os.path.exists(timeline_path):
        with open(timeline_path, "r", encoding="utf-8") as f:
            items = json.load(f).get("items", [])
    elif os.path.exists(script_path):
        with open(script_path, "r", encoding="utf-8") as f:
            items = json.load(f)

    bgm_referenced = sorted({it.get("bgm") for it in (items or []) if it.get("bgm")})
    sfx_referenced = sorted({it.get("sfx") for it in (items or []) if it.get("sfx")})
    with_bgm = sum(1 for it in (items or []) if it.get("bgm"))
    with_sfx = sum(1 for it in (items or []) if it.get("sfx"))

    available = list_available_assets()
    missing_bgm = [n for n in bgm_referenced if n not in available.get("bgm", [])]
    missing_sfx = [n for n in sfx_referenced if n not in available.get("sfx", [])]

    mix_meta = None
    mix_meta_path = os.path.join(ch_dir, "output", "mix_meta.json")
    if os.path.exists(mix_meta_path):
        with open(mix_meta_path, "r", encoding="utf-8") as f:
            mix_meta = json.load(f)

    stale = False
    if mix_meta is not None:
        mixed_bgm = set(mix_meta.get("bgm_names") or [])
        stale = bool(mix_meta.get("voice_only")) is False and set(bgm_referenced) != mixed_bgm

    return {
        "referenced": {"bgm": bgm_referenced, "sfx": sfx_referenced},
        "missing": {"bgm": missing_bgm, "sfx": missing_sfx},
        "segment_counts": {"with_bgm": with_bgm, "with_sfx": with_sfx, "total": len(items or [])},
        "mix": {
            "mixed_with_assets": (not mix_meta.get("voice_only")) if mix_meta else None,
            "mixed_at": mix_meta.get("mixed_at") if mix_meta else None,
            "stale": stale,
        },
    }


_OUTPUT_MEDIA_TYPES = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac"}


@router.get("/{cid}/output.mp3")
def get_output_audio(nid: str, cid: str):
    """成品实际格式由 mixing.output_format 决定，不一定是 mp3——
    URL 路径固定叫 output.mp3 只是为了前端调用方便，这里按实际扩展名找文件、
    按实际格式设 Content-Type，浏览器 <audio> 播放看的是 Content-Type 不是 URL。

    按 mtime 取最新文件，不是按文件名字母序：API 触发的混音输出文件名是
    `{novel_id}_{chapter_id}`，CLI 触发的历史遗留命名是 `chapter_XXXX`——
    两条路径都用过的章节，目录里会同时存在这两个文件，字母序 "chapter_"
    比 novel_id 更靠前，会稳定地把过期文件当成最新成品返回。"""
    ch_dir = _get_chapter_dir(nid, cid)
    output_dir = os.path.join(ch_dir, "output")
    if os.path.isdir(output_dir):
        candidates = []
        for fname in os.listdir(output_dir):
            ext = os.path.splitext(fname)[1].lower()
            if ext in _OUTPUT_MEDIA_TYPES:
                fpath = os.path.join(output_dir, fname)
                candidates.append((os.path.getmtime(fpath), fpath, ext))
        if candidates:
            _, fpath, ext = max(candidates, key=lambda c: c[0])
            return FileResponse(fpath, media_type=_OUTPUT_MEDIA_TYPES[ext])
    raise HTTPException(404, "还没有生成成品音频")


@router.post("/{cid}/timeline/refresh-assets")
def refresh_timeline_assets(nid: str, cid: str):
    """把 script_final.json 里每个分块当前的 sfx/bgm 同步进 timeline.json。

    工作台编辑分块的 sfx/bgm 只改 script_final.json；混音器读的是 TTS 时
    打好快照的 timeline.json（tts_engine.py 在生成时把 sfx/bgm 抄了一份进
    timeline item）。没有这个接口的话，编辑完 sfx/bgm 要等一次完整重新 TTS
    才能在混音里生效——这个接口只做纯数据同步，不碰音频，不需要 GPU。"""
    ch_dir = _get_chapter_dir(nid, cid)
    script_path = os.path.join(ch_dir, "script_final.json")
    timeline_path = os.path.join(ch_dir, "timeline.json")
    if not os.path.exists(script_path):
        raise HTTPException(404, "无定稿剧本")
    if not os.path.exists(timeline_path):
        raise HTTPException(404, "无时间线文件（该章节还没有跑过 TTS）")

    with open(script_path, "r", encoding="utf-8") as f:
        script = json.load(f)
    with open(timeline_path, "r", encoding="utf-8") as f:
        timeline = json.load(f)

    sfx_by_seg = {str(seg.get("seg_id")): seg.get("sfx") for seg in script}
    bgm_by_seg = {str(seg.get("seg_id")): seg.get("bgm") for seg in script}

    updated = 0
    for item in timeline.get("items", []):
        key = str(item.get("seg_id"))
        if key not in sfx_by_seg:
            continue
        new_sfx = sfx_by_seg[key]
        new_bgm = bgm_by_seg[key]
        if item.get("sfx") != new_sfx or item.get("bgm") != new_bgm:
            item["sfx"] = new_sfx
            item["bgm"] = new_bgm
            updated += 1

    tmp_path = timeline_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(timeline, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, timeline_path)

    return {"ok": True, "updated": updated}
