"""
扫描 chapters/ 目录，统计并打印各章节文件存在情况与处理进度
"""
import os
import json

# 状态机各阶段产物，按流转顺序排列，用于陈旧性检测
_STAGE_FILES = ["raw.txt", "script_draft.json", "script_final.json", "timeline.json"]


def _is_stale(path: str) -> bool:
    """检测某章节是否存在“上游文件比下游产物更新”的陈旧状态（如 raw.txt 被替换但未重新解析）"""
    dir_path = os.path.dirname(path) if os.path.isfile(path) else path
    mtimes = []
    for fname in _STAGE_FILES:
        fpath = os.path.join(dir_path, fname)
        if os.path.exists(fpath):
            mtimes.append(os.path.getmtime(fpath))
        else:
            mtimes.append(None)
    for i in range(len(mtimes) - 1):
        if mtimes[i] is not None and mtimes[i + 1] is not None and mtimes[i] > mtimes[i + 1]:
            return True
    return False


def get_all_chapters_status(chapters_dir: str = "chapters") -> list:
    """获取所有章节的状态概览表"""
    if not os.path.exists(chapters_dir):
        return []

    results = []
    chapter_folders = sorted([
        d for d in os.listdir(chapters_dir)
        if os.path.isdir(os.path.join(chapters_dir, d))
    ])

    for folder in chapter_folders:
        path = os.path.join(chapters_dir, folder)
        has_raw = os.path.exists(os.path.join(path, "raw.txt"))
        has_draft = os.path.exists(os.path.join(path, "script_draft.json"))
        has_final = os.path.exists(os.path.join(path, "script_final.json"))
        has_timeline = os.path.exists(os.path.join(path, "timeline.json"))

        output_dir = os.path.join(path, "output")
        has_mp3 = False
        if os.path.exists(output_dir):
            mp3_files = [f for f in os.listdir(output_dir) if f.endswith(".mp3")]
            has_mp3 = len(mp3_files) > 0

        audio_cache_dir = os.path.join(path, "audio_cache")
        cache_count = 0
        if os.path.exists(audio_cache_dir):
            cache_count = len([f for f in os.listdir(audio_cache_dir) if f.endswith(".wav")])

        # 获取状态 JSON 标记
        status_file = os.path.join(path, ".status.json")
        status_tag = "UNKNOWN"
        if os.path.exists(status_file):
            try:
                with open(status_file, "r", encoding="utf-8") as f:
                    status_tag = json.load(f).get("status", "UNKNOWN")
            except Exception:
                pass

        # completed 状态必须有真实存在的 mp3 产物，否则视为陈旧/失真状态
        if status_tag == "completed" and not has_mp3:
            status_tag = "STALE(no mp3)"
        elif _is_stale(path):
            status_tag = f"STALE({status_tag})"

        results.append({
            "chapter_id": folder,
            "status": status_tag,
            "raw": has_raw,
            "draft": has_draft,
            "final": has_final,
            "timeline": has_timeline,
            "mp3": has_mp3,
            "audio_cache_count": cache_count
        })

    return results


def print_status_table(chapters_dir: str = "chapters"):
    """格式化打印各章节进度表"""
    status_list = get_all_chapters_status(chapters_dir)
    if not status_list:
        print("未发现任何章节工作区 directory in 'chapters/'.")
        return

    print("=" * 95)
    print(f"{'章节 ID':<10} | {'Raw':<5} | {'Draft':<5} | {'Final':<5} | {'Timeline':<8} | {'Audio':<5} | {'状态':<18} | {'Cache Files'}")
    print("=" * 95)
    for s in status_list:
        raw_str = "✓" if s["raw"] else "✗"
        draft_str = "✓" if s["draft"] else "✗"
        final_str = "✓" if s["final"] else "✗"
        timeline_str = "✓" if s["timeline"] else "✗"
        mp3_str = "✓" if s["mp3"] else "✗"
        print(f"{s['chapter_id']:<10} | {raw_str:<5} | {draft_str:<5} | {final_str:<5} | {timeline_str:<8} | {mp3_str:<5} | {s['status']:<18} | {s['audio_cache_count']} wavs")
    print("=" * 95)
