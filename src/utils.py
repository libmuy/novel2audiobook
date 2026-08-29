"""
通用工具函数库
"""
import os
import hashlib
import json
from datetime import datetime
import yaml


def calculate_md5(key_string: str) -> str:
    """计算文本串的 MD5 哈希值"""
    return hashlib.md5(key_string.encode("utf-8")).hexdigest()


def load_global_config(config_path: str = "global_config.yaml") -> dict:
    """读取全局 YAML 配置文件"""
    if not os.path.exists(config_path):
        return {}
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def normalize_chapter_id(chapter_input: str) -> str:
    """规范化章节 ID 格式，如 '0001' 或 'ch_0001' 统一转换为 'ch_0001'"""
    chapter_input = str(chapter_input).strip()
    if chapter_input.startswith("ch_"):
        return chapter_input
    return f"ch_{chapter_input.zfill(4)}"


def get_chapter_dir(chapter_input: str, base_dir: str = "chapters") -> str:
    """获取章节所在目录路径"""
    ch_id = normalize_chapter_id(chapter_input)
    return os.path.join(base_dir, ch_id)


def update_chapter_status(chapter_dir: str, status: str):
    """更新章节工作区内的 .status.json 标记"""
    status_file = os.path.join(chapter_dir, ".status.json")
    ch_id = os.path.basename(os.path.abspath(chapter_dir))
    data = {
        "chapter_id": ch_id,
        "status": status,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    with open(status_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
