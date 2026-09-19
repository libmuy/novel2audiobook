"""
批量预检模块（计划 005）

用户点击批量操作前，先扫描一遍要动到哪些东西，弹一次确认。
"""
import glob
import json
import os

from src.utils import PROJECT_ROOT, load_global_config
from src import status_tracker


TTS_STATS_PATH = os.path.join(PROJECT_ROOT, ".cache", "tts_stats.json")


def preflight(task_type: str, novel_id: str, chapter_ids: list = None,
              library_dir: str = None) -> dict:
    """
    返回：
    {"chapters": [{"chapter_id": "ch_0001", "action": "create|overwrite|skip",
                   "reason": "已有成品 MP3，将被重新生成"}],
     "summary": {"create": 8, "overwrite": 3, "skip": 1},
     "invalidated_cache_count": 0,
     "estimated_gpu_minutes": None}"""
    if library_dir is None:
        library_dir = os.path.join(PROJECT_ROOT, "library")

    chapters_dir = os.path.join(library_dir, novel_id, "chapters")
    if not os.path.isdir(chapters_dir):
        return {"chapters": [], "summary": {"create": 0, "overwrite": 0, "skip": 0},
                "invalidated_cache_count": 0, "estimated_gpu_minutes": None}

    # 获取所有章节状态
    all_status = status_tracker.get_all_chapters_status(chapters_dir)

    # 如果指定了 chapter_ids，只处理这些
    if chapter_ids:
        all_status = [s for s in all_status if s["chapter_id"] in chapter_ids]

    result_chapters = []
    summary = {"create": 0, "overwrite": 0, "skip": 0}
    invalidated_cache_count = 0

    for ch_status in all_status:
        ch_id = ch_status["chapter_id"]
        action, reason = _determine_action(task_type, ch_status)
        result_chapters.append({
            "chapter_id": ch_id,
            "action": action,
            "reason": reason,
        })
        summary[action] += 1

    estimated_gpu_minutes = _estimate_gpu_minutes(task_type, summary["create"] + summary["overwrite"])

    return {
        "chapters": result_chapters,
        "summary": summary,
        "invalidated_cache_count": invalidated_cache_count,
        "estimated_gpu_minutes": estimated_gpu_minutes,
    }


def preflight_assets(params: dict = None) -> dict:
    """asset_gen 的预检：不是章节维度，而是素材维度。返回结构跟章节预检同形
    （summary 三个计数不变），现有的通用预检弹窗不用改；逐条明细放在 assets 里，
    chapters 留空。状态映射复用 asset_gen.get_asset_status_list，不重写判定逻辑。
    estimated_gpu_minutes 恒为 None——不编造没有历史数据的耗时。"""
    from src import asset_gen
    params = params or {}
    kinds = params.get("kinds")
    only = set(params["only"]) if params.get("only") else None
    force = bool(params.get("force"))

    summary = {"create": 0, "overwrite": 0, "skip": 0}
    assets = []
    for row in asset_gen.get_asset_status_list():
        if kinds and row["kind"] not in kinds:
            continue
        if only and row["name"] not in only:
            continue
        status = row["status"]
        if status == "MISSING":
            action, reason = "create", "尚未生成"
        elif status == "STALE(引擎已变更)":
            # 哈希没变，generate_assets 不会自动重做它（否则一改配置就整库重生成）；
            # 只有强制重新生成才会用新引擎重做，预检要跟实际行为一致
            if force:
                action, reason = "overwrite", "引擎已变更，将用新引擎重新生成"
            else:
                action, reason = "skip", "引擎已变更（旧音频仍可用），需「强制重新生成」才会用新引擎重做"
        elif status.startswith("STALE"):
            action, reason = "overwrite", "规格已变更，将重新生成"
        elif status == "OK(占位/Mock)":
            action, reason = "overwrite", "当前是 Mock 占位，将尝试用真实引擎重新生成"
        elif force:
            action, reason = "overwrite", "强制重新生成"
        else:
            action, reason = "skip", "已是最新"
        summary[action] += 1
        assets.append({"name": row["name"], "kind": row["kind"], "action": action, "reason": reason})

    return {"chapters": [], "assets": assets, "summary": summary,
            "invalidated_cache_count": 0, "estimated_gpu_minutes": None}


def _determine_action(task_type: str, ch_status: dict) -> tuple:
    """根据任务类型和章节状态，决定 action 和 reason"""
    status = ch_status["status"]

    if task_type == "parse":
        if ch_status["raw"]:
            if ch_status["draft"] and status not in ("UNKNOWN",):
                return "skip", "已有剧本草稿"
            return "create", "将解析正文为剧本草稿"
        return "skip", "无原始文本"

    elif task_type == "tts":
        if not ch_status["final"]:
            return "skip", "无定稿剧本，需先完成解析"
        if ch_status["timeline"] and status == "completed":
            if ch_status["mp3"]:
                return "overwrite", "已有成品 TTS，将重新生成"
            return "overwrite", "已有时间线但无成品，将重新生成"
        if ch_status["timeline"]:
            return "overwrite", "时间线存在但状态陈旧，将重新生成"
        return "create", "将基于定稿剧本生成人声"

    elif task_type == "mix":
        if not ch_status["timeline"]:
            return "skip", "无时间线数据，需先完成 TTS"
        if ch_status["output"] and status == "completed":
            return "overwrite", "已有成品 MP3，将重新混音"
        return "create", "将时间线混音为成品 MP3"

    return "skip", f"未知任务类型: {task_type}"


def _estimate_gpu_minutes(task_type: str, affected_count: int) -> float:
    """基于历史数据估算 GPU 耗时；没有历史数据返回 None"""
    if task_type != "tts":
        return None
    if affected_count == 0:
        return 0.0

    stats = _load_tts_stats()
    avg_seconds_per_sentence = stats.get("avg_seconds_per_sentence")
    if avg_seconds_per_sentence is None:
        return None

    # 假设平均每章 100 句（粗略估计，实际应从 script_final.json 读取）
    estimated_sentences = affected_count * 100
    estimated_seconds = estimated_sentences * avg_seconds_per_sentence
    return round(estimated_seconds / 60.0, 1)


def _load_tts_stats() -> dict:
    """读取 TTS 合成统计"""
    if not os.path.exists(TTS_STATS_PATH):
        return {}
    try:
        with open(TTS_STATS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def update_tts_stats(seconds_per_sentence: float):
    """更新 TTS 合成统计（在 TTS handler 完成后调用）"""
    stats = _load_tts_stats()
    avg = stats.get("avg_seconds_per_sentence")
    count = stats.get("sample_count", 0)

    if avg is None:
        stats["avg_seconds_per_sentence"] = round(seconds_per_sentence, 2)
        # 首次采样必须把计数记成 1：之前漏了这句，第二次采样读到 count=0，
        # 加权平均退化成"直接覆盖第一次"，历史统计永远只有最后一次的值
        stats["sample_count"] = 1
    else:
        # 滑动平均
        new_count = count + 1
        stats["avg_seconds_per_sentence"] = round((avg * count + seconds_per_sentence) / new_count, 2)
        stats["sample_count"] = new_count

    os.makedirs(os.path.dirname(TTS_STATS_PATH), exist_ok=True)
    tmp_path = TTS_STATS_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, TTS_STATS_PATH)
