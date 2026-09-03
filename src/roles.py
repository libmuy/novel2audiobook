"""
角色清单管理：加载/解析/自动注册说话人角色。

设计目标（对应 speaker ID 三层处理策略）：
1. 提示词约束：调用方（llm_parser）在 prompt 中注入 list_role_names() 结果，
   要求 LLM 尽量直接输出已注册角色名或角色 ID。
2. 归一化：resolve_role_id() 依次尝试——精确 ID 匹配 -> 中文名精确匹配 ->
   别名表匹配 -> 拼音归一后匹配已注册角色。
3. 自动注册 + 兜底：都未命中时，register_role() 自动创建新角色（继承
   narrator 的默认 TTS 参数、复用 narrator 的参考音频作为占位），
   写回角色清单；若注册被禁止或异常，调用方应回退为 narrator。
"""
import os
import re
import json
import shutil

from src.utils import resolve_path

try:
    from pypinyin import lazy_pinyin
except ImportError:  # 极端情况下离线环境缺少该库，退化为不做拼音归一
    lazy_pinyin = None

MANIFEST_REL_PATH = os.path.join("roles", "roles_manifest.json")

# 常见别名 -> 角色 ID。可持续补充。
ROLE_ALIASES = {
    "旁白": "narrator",
    "林动": "lin_dong",
}


def _manifest_path(roles_dir: str = None) -> str:
    if roles_dir is not None:
        return os.path.join(roles_dir, "roles_manifest.json")
    return resolve_path(MANIFEST_REL_PATH)


def load_manifest(roles_dir: str = None) -> dict:
    """读取角色清单，文件不存在时返回空结构"""
    path = _manifest_path(roles_dir)
    if not os.path.exists(path):
        return {"roles": {}}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("roles", {})
    return data


def save_manifest(manifest: dict, roles_dir: str = None):
    path = _manifest_path(roles_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


def list_role_names(manifest: dict) -> list:
    """返回 [(role_id, 中文名), ...]，供 LLM 提示词使用"""
    return [(rid, info.get("name", rid)) for rid, info in manifest.get("roles", {}).items()]


def _to_role_id(chinese_name: str) -> str:
    """把中文名转换为拼音风格的角色 ID，如 苏砚 -> su_yan"""
    if lazy_pinyin is None:
        # 无拼音库时退化为按字符哈希，保证至少是合法且稳定的标识符
        return "role_" + str(abs(hash(chinese_name)) % 100000)
    parts = lazy_pinyin(chinese_name)
    return "_".join(p.lower() for p in parts if p)


def resolve_role_id(raw_speaker: str, manifest: dict) -> str:
    """
    将 LLM/启发式解析出的说话人字符串归一化为已注册角色 ID。
    命中返回角色 ID；未命中返回 None（调用方决定是否 register_role 或回退 narrator）。
    """
    if not raw_speaker:
        return None
    raw_speaker = raw_speaker.strip()
    roles = manifest.get("roles", {})

    # 1) 精确 ID 匹配
    if raw_speaker in roles:
        return raw_speaker

    # 2) 精确中文名匹配
    for rid, info in roles.items():
        if info.get("name") == raw_speaker:
            return rid

    # 3) 别名表匹配
    if raw_speaker in ROLE_ALIASES:
        alias_id = ROLE_ALIASES[raw_speaker]
        if alias_id in roles:
            return alias_id

    # 4) 拼音归一匹配（应对 LLM 输出拼音、下划线写法不一致等情况）
    candidate_id = _to_role_id(raw_speaker)
    if candidate_id in roles:
        return candidate_id

    return None


def register_role(chinese_name: str, manifest: dict, roles_dir: str = None,
                   gender: str = "unknown", description: str = "") -> str:
    """
    自动注册新角色：
    - 角色 ID 由中文名拼音生成
    - config.json 继承 narrator 的语速/音高默认值
    - reference.wav 复用 narrator 的参考音频作为占位（后续可人工替换为更贴合的音色）
    返回新角色 ID；若因任何原因注册失败，抛出异常由调用方兜底捕获。
    """
    role_id = _to_role_id(chinese_name)
    if not role_id:
        raise ValueError(f"无法为角色名生成合法 ID: {chinese_name!r}")

    roles = manifest.setdefault("roles", {})
    if role_id in roles:
        return role_id  # 已存在（并发/重复调用），直接复用

    base = roles_dir if roles_dir is not None else resolve_path("roles")
    narrator_dir = os.path.join(base, "narrator")
    new_role_dir = os.path.join(base, role_id)
    os.makedirs(new_role_dir, exist_ok=True)

    # 继承 narrator 的默认 TTS 参数
    narrator_cfg_path = os.path.join(narrator_dir, "config.json")
    if os.path.exists(narrator_cfg_path):
        with open(narrator_cfg_path, "r", encoding="utf-8") as f:
            base_cfg = json.load(f)
    else:
        base_cfg = {"speed": 1.0, "pitch": 0.0}

    new_cfg = {
        "role_id": role_id,
        "name": chinese_name,
        "speed": base_cfg.get("speed", 1.0),
        "pitch": base_cfg.get("pitch", 0.0),
        "auto_registered": True,
    }
    with open(os.path.join(new_role_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump(new_cfg, f, ensure_ascii=False, indent=2)

    # 参考音频占位：复用 narrator 的参考音频，待人工替换为更贴合角色的音色
    narrator_ref = os.path.join(narrator_dir, "reference.wav")
    new_ref = os.path.join(new_role_dir, "reference.wav")
    if os.path.exists(narrator_ref) and not os.path.exists(new_ref):
        shutil.copyfile(narrator_ref, new_ref)

    roles[role_id] = {
        "name": chinese_name,
        "gender": gender,
        "config_path": os.path.join("roles", role_id, "config.json"),
        "reference_audio": os.path.join("roles", role_id, "reference.wav"),
        "description": description or f"自动注册角色（占位音色，来自 narrator）",
    }
    save_manifest(manifest, roles_dir)
    return role_id


# 中文姓名粗提取：2-4 个连续汉字，且不是常见非人名词。用于从叙述句中猜测
# 说话人（如"柳禾的声音从肺里挤出来"中提取"柳禾"）。仅供启发式回退解析器使用，
# 精度低于真实 LLM 解析，可接受。
_NAME_PATTERN = re.compile(r"[一-龥]{2,4}")
_NON_NAME_STOPWORDS = {"这几天", "这条支", "苏砚今日", "苏砚左手"}


def get_role_runtime_config(role_id: str, manifest: dict, roles_dir: str = None) -> dict:
    """
    返回某角色用于 TTS 合成的运行时配置：speed / pitch / reference_audio 绝对路径。
    找不到角色或 config.json 缺失时返回安全默认值，保证调用方不因单个角色异常而中断合成。
    """
    base = roles_dir if roles_dir is not None else resolve_path("roles")
    info = manifest.get("roles", {}).get(role_id, {})
    cfg = {"speed": 1.0, "pitch": 0.0}

    cfg_path = os.path.join(base, role_id, "config.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
            cfg["speed"] = file_cfg.get("speed", 1.0)
            cfg["pitch"] = file_cfg.get("pitch", 0.0)
        except (json.JSONDecodeError, OSError):
            pass

    ref_audio = os.path.join(base, role_id, "reference.wav")
    if not os.path.exists(ref_audio):
        ref_audio = os.path.join(base, "narrator", "reference.wav")  # 兜底复用旁白音色

    cfg["role_id"] = role_id
    cfg["name"] = info.get("name", role_id)
    cfg["reference_audio"] = ref_audio
    return cfg


def guess_name_from_context(text: str, manifest: dict) -> str:
    """在一段叙述文本中查找已知角色的中文名，找到返回中文名，否则返回 None"""
    for rid, info in manifest.get("roles", {}).items():
        name = info.get("name")
        if name and name in text:
            return name
    return None
