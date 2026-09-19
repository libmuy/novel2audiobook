"""
素材规格（assets/asset_specs.yaml）的写入口。

读取仍然走 src/asset_gen.load_asset_specs（yaml.safe_load，有损投影：只留
五个字段、类型强转）；写入必须用 ruamel.yaml 的 round-trip 模式——这份文件
有 20 行的字段说明头和挂在具体条目上的行内诊断注释（见 asset_specs.yaml 里
door_creak 那段），yaml.safe_dump 回写会把它们全部冲掉。

只提供 create/update/delete 三个原语，不支持改名：素材名会被写进
script_final.json / timeline.json 的 sfx/bgm 字段，改名会让所有引用旧名的
章节静默失联；"删除 + 新建"是唯一诚实的操作。
"""
import io
import os
import re
import threading

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from src.utils import load_global_config, resolve_path

VALID_KINDS = ("ambience", "sfx")
DEFAULT_SPEC_PATH = "assets/asset_specs.yaml"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,47}$")
_SPEC_FIELDS = ("description", "prompt", "negative_prompt", "duration_sec", "seed")

# API 是多线程的，asset_gen 任务也会并发读文件：所有写操作串行化
SPEC_LOCK = threading.Lock()


class SpecError(ValueError):
    """校验失败（400）。"""


class SpecNotFound(KeyError):
    """条目不存在（404）。"""


class SpecExists(ValueError):
    """条目已存在（409）。"""


def spec_file_path(config: dict = None) -> str:
    """规格文件路径：读 asset_gen.spec_file 配置，缺省回落到默认路径。
    （这个配置项之前定义了但从没人读过；写入口必须跟读取方认同一个路径。）"""
    if config is None:
        config = load_global_config()
    rel = (config.get("asset_gen") or {}).get("spec_file") or DEFAULT_SPEC_PATH
    return resolve_path(rel)


def round_trip_yaml() -> YAML:
    """保留注释/引号/缩进的 round-trip YAML（asset_specs 和 global_config 共用）。
    width 拉大避免长 prompt 被折行；把 None 显式写成 `null`——ruamel 默认写成空值，
    会把 `drm_card: null  # 注释` 改写成 `drm_card:  # 注释`，语义相同但产生无谓 diff。"""
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    y.representer.add_representer(
        type(None), lambda self, data: self.represent_scalar("tag:yaml.org,2002:null", "null"))
    return y


_yaml = round_trip_yaml  # 旧名保留


def load_raw(path: str = None):
    path = path or spec_file_path()
    if not os.path.exists(path):
        doc = CommentedMap()
        return doc
    with open(path, "r", encoding="utf-8") as f:
        return _yaml().load(f) or CommentedMap()


def save_raw(doc, path: str = None):
    path = path or spec_file_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    buf = io.StringIO()
    _yaml().dump(doc, buf)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(buf.getvalue())
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------

def validate_kind(kind: str):
    if kind not in VALID_KINDS:
        raise SpecError(f"kind 必须是 {' / '.join(VALID_KINDS)}，收到 {kind!r}")


def validate_name(name: str):
    """名字会直接变成 assets/{kind}/{name}.wav 的文件名，并进入 LLM 提示词的
    素材词表——限制成安全的小写标识符，杜绝路径分隔符和奇怪字符。"""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise SpecError("素材名只能包含小写字母、数字、下划线，以字母或数字开头，最长 48 个字符")


def validate_spec(spec: dict):
    if not str(spec.get("prompt", "")).strip():
        raise SpecError("prompt 不能为空")
    duration = spec.get("duration_sec")
    if duration is not None:
        try:
            ok = 0 < float(duration) <= 60
        except (TypeError, ValueError):
            ok = False
        if not ok:
            raise SpecError("duration_sec 必须在 (0, 60] 秒之间")
    seed = spec.get("seed")
    if seed is not None:
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise SpecError("seed 必须是非负整数")


# --------------------------------------------------------------------------
# 写原语
# --------------------------------------------------------------------------

def create_spec(kind: str, name: str, spec: dict, path: str = None):
    validate_kind(kind)
    validate_name(name)
    validate_spec(spec)
    with SPEC_LOCK:
        doc = load_raw(path)
        bucket = doc.get(kind)
        if bucket is None:
            doc[kind] = CommentedMap()
            bucket = doc[kind]
        if name in bucket:
            raise SpecExists(f"素材 {kind}/{name} 已存在")
        entry = CommentedMap()
        for field in _SPEC_FIELDS:
            if field in spec and spec[field] is not None:
                entry[field] = spec[field]
        bucket[name] = entry
        save_raw(doc, path)


def update_spec(kind: str, name: str, patch: dict, path: str = None):
    validate_kind(kind)
    validate_name(name)
    with SPEC_LOCK:
        doc = load_raw(path)
        bucket = doc.get(kind) or {}
        if name not in bucket:
            raise SpecNotFound(f"素材 {kind}/{name} 不存在")
        entry = bucket[name]
        merged = {f: entry.get(f) for f in _SPEC_FIELDS}
        merged.update({k: v for k, v in patch.items() if k in _SPEC_FIELDS and v is not None})
        validate_spec(merged)
        for field, value in patch.items():
            if field in _SPEC_FIELDS and value is not None:
                entry[field] = value  # 原地改值，条目上的行内注释保留
        save_raw(doc, path)


def delete_spec(kind: str, name: str, delete_files: bool = False, path: str = None,
                assets_dir: str = None) -> bool:
    """删除规格条目。默认只删定义，.wav/.meta.json 保留（跟角色分类"删除不
    清空引用"同一套容忍策略）；delete_files=True 时连生成的文件一起删。
    返回是否真的删了文件。"""
    validate_kind(kind)
    validate_name(name)
    with SPEC_LOCK:
        doc = load_raw(path)
        bucket = doc.get(kind) or {}
        if name not in bucket:
            raise SpecNotFound(f"素材 {kind}/{name} 不存在")
        del bucket[name]
        save_raw(doc, path)

    files_deleted = False
    if delete_files:
        base = assets_dir or resolve_path("assets")
        for ext in (".wav", ".meta.json"):
            fp = os.path.join(base, kind, f"{name}{ext}")
            if os.path.exists(fp):
                os.remove(fp)
                files_deleted = True
    return files_deleted
