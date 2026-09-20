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


def resolve_optional_path(path) -> str:
    """resolve_path 的可缺省版：配置里没给（None/空串）就返回 None，而不是回落到某台机器的
    路径字面量。调用方据此判定"环境未配置"并降级（is_available() 判假）。"""
    return resolve_path(path) if path else None


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


LOCAL_CONFIG_NAME = "local_config.yaml"


def _deep_merge(base: dict, overlay: dict) -> dict:
    """dict 按键递归合并；其余类型（标量、列表）由 overlay 整体替换。返回新 dict，不改入参。"""
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def read_yaml_dict(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_global_config(config_path: str = None, local_path: str = None) -> dict:
    """读取全局配置：global_config.yaml（入库、与机器无关的参数），再用 local_config.yaml
    （gitignore、本机专属的路径/外部命令）按键深合并覆盖。

    只有**不传 config_path** 时才叠加 local 层：显式指定配置文件的调用方（测试用的临时
    配置等）要的就是那一个文件，不该被本机的 local_config.yaml 悄悄改写。需要叠加时
    显式传 local_path。
    """
    if config_path is None:
        config_path = os.path.join(PROJECT_ROOT, "global_config.yaml")
        if local_path is None:
            local_path = os.path.join(PROJECT_ROOT, LOCAL_CONFIG_NAME)
    else:
        config_path = resolve_path(config_path)
    config = read_yaml_dict(config_path)
    if local_path is not None:
        config = _deep_merge(config, read_yaml_dict(resolve_path(local_path)))
    return config


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
