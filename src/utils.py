"""
通用工具函数库
"""
import os
import hashlib
import json
from datetime import datetime
import yaml

# 项目根目录：src/utils.py 的上一级目录。所有“公共资产”（assets/、roles/、
# global_config.yaml）一律基于此路径解析，避免因运行时 CWD 不同而找不到文件。
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_project_root() -> str:
    """返回项目根目录的绝对路径"""
    return PROJECT_ROOT


def resolve_path(relative_path: str) -> str:
    """将相对于项目根目录的路径解析为绝对路径；已是绝对路径则原样返回"""
    if os.path.isabs(relative_path):
        return relative_path
    return os.path.join(PROJECT_ROOT, relative_path)


def calculate_md5(key_string: str) -> str:
    """计算文本串的 MD5 哈希值"""
    return hashlib.md5(key_string.encode("utf-8")).hexdigest()


def calculate_file_md5(file_path: str) -> str:
    """计算文件内容的 MD5（分块读取），用于判断 reference.wav 等资源是否变化"""
    h = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_global_config(config_path: str = None) -> dict:
    """读取全局 YAML 配置文件（默认取项目根目录下的 global_config.yaml）"""
    if config_path is None:
        config_path = os.path.join(PROJECT_ROOT, "global_config.yaml")
    else:
        config_path = resolve_path(config_path)
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


def list_available_assets() -> dict:
    """
    扫描 assets/sfx 与 assets/ambience 目录，返回可用素材名词表（不含扩展名）。
    供 LLM 解析提示词与混音器做“词表约束”校验，避免生成引用不存在文件的 sfx/bgm。
    """
    result = {"sfx": [], "bgm": []}
    sfx_dir = os.path.join(PROJECT_ROOT, "assets", "sfx")
    bgm_dir = os.path.join(PROJECT_ROOT, "assets", "ambience")
    if os.path.isdir(sfx_dir):
        result["sfx"] = sorted(
            os.path.splitext(f)[0] for f in os.listdir(sfx_dir) if f.endswith(".wav")
        )
    if os.path.isdir(bgm_dir):
        result["bgm"] = sorted(
            os.path.splitext(f)[0] for f in os.listdir(bgm_dir) if f.endswith(".wav")
        )
    return result


def update_chapter_status(chapter_dir: str, status: str, chapter_id: str = None):
    """更新章节工作区内的 .status.json 标记"""
    status_file = os.path.join(chapter_dir, ".status.json")
    ch_id = chapter_id or os.path.basename(os.path.abspath(chapter_dir))
    data = {
        "chapter_id": ch_id,
        "status": status,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    with open(status_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
