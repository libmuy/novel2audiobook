"""
LLM 剧本解析模块 (使用 Qwen3-8B 解析文本为带角色、情感、音效的剧本 JSON)
"""
import os
import json
from src.utils import update_chapter_status


def parse_text_to_json(text: str, roles_list: list = None) -> list:
    """
    解析长文本为细颗粒度（句子级）的剧本 JSON 数据结构。

    返回字段说明：
    - seg_id: 句段序号 (1, 2, ...)
    - speaker: 说话人角色名 (如 narrator, lin_dong)
    - text: 台词/旁白文本
    - emotion: 情感说明 (如 neutral, angry, serious)
    - sfx: 伴随音效 (如 sword_clash, None)
    - bgm: 背景音乐 (如 rain_heavy, None)
    """
    if roles_list is None:
        roles_list = ["narrator", "lin_dong"]

    lines = [line.strip() for line in text.split("\n") if line.strip()]
    script_segments = []

    for idx, line in enumerate(lines, 1):
        # 简单 Mock 逻辑识别说话人与情感/音效
        if "：“" in line or "“" in line:
            speaker = "lin_dong"
            emotion = "angry" if "厉声" in line or "辱" in line else "serious"
            sfx = "sword_clash" if "加倍奉还" in line else None
            bgm = "rain_heavy"
            # 简单清洗包裹的双引号
            clean_text = line.split("“")[-1].replace("”", "").strip()
            if not clean_text:
                clean_text = line
        else:
            speaker = "narrator"
            emotion = "neutral"
            sfx = None
            bgm = "rain_heavy" if "风声萧萧" in line else None
            clean_text = line

        script_segments.append({
            "seg_id": idx,
            "speaker": speaker,
            "text": clean_text,
            "emotion": emotion,
            "sfx": sfx,
            "bgm": bgm
        })

    return script_segments


def process_chapter_parse(chapter_dir: str) -> str:
    """
    读取 raw.txt，生成 script_draft.json
    """
    raw_path = os.path.join(chapter_dir, "raw.txt")
    if not os.path.exists(raw_path):
        raise FileNotFoundError(f"未找到原始文本文件: {raw_path}")

    with open(raw_path, "r", encoding="utf-8") as f:
        text = f.read()

    script_draft = parse_text_to_json(text)

    draft_path = os.path.join(chapter_dir, "script_draft.json")
    with open(draft_path, "w", encoding="utf-8") as f:
        json.dump(script_draft, f, ensure_ascii=False, indent=2)

    update_chapter_status(chapter_dir, "parsed_draft")
    return draft_path
