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
import subprocess
import threading

from src.utils import resolve_path, load_global_config, calculate_file_md5

try:
    from pypinyin import lazy_pinyin
except ImportError:  # 极端情况下离线环境缺少该库，退化为不做拼音归一
    lazy_pinyin = None

MANIFEST_REL_PATH = os.path.join("roles", "roles_manifest.json")

# 与 tools/indextts_infer.py 的 EMBEDDING_CACHE_FILENAME / EMBEDDING_META_FILENAME
# 保持一致的文件名约定。本模块跑在项目主 venv（不装 torch），不能 torch.load(.pt)，
# 只通过同名 .meta.json 做只读状态查询；两边各自维护一份常量，避免主 venv
# 反向 import tools.indextts_infer（它顶层 `import torch`，主 venv 没装会直接炸）。
EMBEDDING_FILENAME = "speaker_embeddings.pt"
EMBEDDING_META_FILENAME = "speaker_embeddings.meta.json"

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


_MANIFEST_LOCK = threading.Lock()


def save_manifest(manifest: dict, roles_dir: str = None):
    """原子写（tmp + os.replace）+ 进程内加锁。roles_manifest.json 是唯一还没
    原子化的共享清单（对比 library._atomic_write_yaml、derived_index 的缓存写入）；
    分类树 PUT、标签 PATCH 都是读-改-写，非原子写在中途崩溃会留下截断的 JSON。
    注意：锁只保护写，不保护"读-改-写"整个周期——跟改动前一致，这里不引入新的
    并发语义。"""
    path = _manifest_path(roles_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with _MANIFEST_LOCK:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)


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
                   gender: str = "unknown", description: str = "",
                   category: str = "", tags: list = None) -> str:
    """
    自动注册新角色：
    - 角色 ID 由中文名拼音生成
    - config.json 继承 narrator 的语速/音高默认值
    - reference.wav 复用 narrator 的参考音频作为占位（后续可人工替换为更贴合的音色）
    category/tags 只在新建时写入；角色已存在的早返回路径不会去改它们。
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
    # 只在真的有值时才写：没分类/没标签的角色，manifest 条目保持跟改动前一模一样
    if category:
        roles[role_id]["category"] = category
    if tags:
        roles[role_id]["tags"] = list(tags)
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


# --------------------------------------------------------------------------
# Speaker embedding 预计算管理（计划 001）+ 角色增删（计划 002 Tab 2 消费）
# --------------------------------------------------------------------------

def get_embedding_status(role_id: str, manifest: dict, roles_dir: str = None) -> dict:
    """
    只读查询某角色 speaker_embeddings.pt 的状态，供 webui 展示。不依赖 torch
    （本模块跑在主 venv），只读同目录下的 .meta.json 与当前 reference.wav 的
    MD5 做比对。

    返回 {"exists": bool, "valid": bool, "created_at": str|None, "stale_reason": str|None}：
    - exists=False：从未预计算过
    - exists=True, valid=False：.pt 存在但已过期/元数据缺失损坏，stale_reason 说明原因
    - valid=True：可以放心复用，无需重新预计算
    """
    base = roles_dir if roles_dir is not None else resolve_path("roles")
    role_dir = os.path.join(base, role_id)
    ref_audio = os.path.join(role_dir, "reference.wav")
    pt_path = os.path.join(role_dir, EMBEDDING_FILENAME)
    meta_path = os.path.join(role_dir, EMBEDDING_META_FILENAME)

    status = {"exists": os.path.exists(pt_path), "valid": False, "created_at": None, "stale_reason": None}
    if not status["exists"]:
        status["stale_reason"] = "尚未预计算"
        return status

    if not os.path.exists(meta_path):
        status["stale_reason"] = "缺少元数据文件（可能来自旧版本流程），建议重新预计算"
        return status
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (json.JSONDecodeError, OSError):
        status["stale_reason"] = "元数据文件损坏，建议重新预计算"
        return status

    status["created_at"] = meta.get("created_at")
    if not os.path.exists(ref_audio):
        status["stale_reason"] = "参考音频不存在"
        return status
    if meta.get("ref_audio_md5") != calculate_file_md5(ref_audio):
        status["stale_reason"] = "参考音频已变化，需要重新预计算"
        return status

    status["valid"] = True
    return status


def _embedding_env(config: dict) -> dict:
    tts_cfg = config.get("tts", {}).get("index_tts", {})
    return {
        "tts_cfg": tts_cfg,
        "python_bin": resolve_path(tts_cfg.get("python_bin", "/srv/unsafe/dev-env/venvs/indextts/bin/python")),
        "script": resolve_path("tools/precompute_embeddings.py"),
        "repo_dir": resolve_path(tts_cfg.get("repo_dir", "tools/indextts_repo")),
        "checkpoints_dir": tts_cfg.get("checkpoints_dir", "/srv/unsafe/dev-env/models/tts/IndexTTS-2.5"),
    }


def embedding_precondition_error(role_id: str, manifest: dict, config: dict = None):
    """预计算的廉价前置检查（角色已注册、IndexTTS 推理环境就绪），失败返回错误文案，
    通过返回 None。不碰 GPU、不起子进程——调用方可以在停 llama-server 腾显存**之前**先
    过一遍，别为一个注定失败的任务白白停一次 LLM 服务。"""
    if role_id not in manifest.get("roles", {}):
        return f"角色 {role_id!r} 未注册"
    if config is None:
        config = load_global_config()
    env = _embedding_env(config)
    if not (os.path.exists(env["python_bin"]) and os.path.exists(env["script"])
            and os.path.isdir(env["repo_dir"]) and os.path.isdir(env["checkpoints_dir"])):
        return "IndexTTS 推理环境未就绪（venv/权重缺失），无法预计算 embedding"
    return None


def precompute_embedding(role_id: str, manifest: dict, roles_dir: str = None,
                         config: dict = None, force: bool = True) -> dict:
    """
    为单个角色触发 speaker embedding 预计算：以子进程方式调用
    tools/precompute_embeddings.py（复用与 IndexTTSBackend 相同的独立 venv/
    权重路径配置，见 global_config.yaml 的 tts.index_tts 段）。

    这是一次真实的 GPU 推理操作，会与运行中的 llama-server 竞争显存——
    调用方（webui/CLI）必须在用户显式确认换手之后才调用本函数；本函数
    本身不做任何自动的 GPU 仲裁/暂停 llama-server 的动作。

    返回 {"ok": bool, "error": str|None}。
    """
    if config is None:
        config = load_global_config()
    err = embedding_precondition_error(role_id, manifest, config)
    if err:
        return {"ok": False, "error": err}
    env = _embedding_env(config)
    tts_cfg, python_bin, script = env["tts_cfg"], env["python_bin"], env["script"]
    repo_dir, checkpoints_dir = env["repo_dir"], env["checkpoints_dir"]
    base = roles_dir if roles_dir is not None else resolve_path("roles")

    cmd = [
        python_bin, script,
        "--repo-dir", repo_dir, "--checkpoints-dir", checkpoints_dir,
        "--roles-dir", base, "--role", role_id,
    ]
    if force:
        cmd.append("--force")

    try:
        # 单个角色的 embedding 只要几分钟，用专门的 precompute_timeout_sec；不设才回落到
        # 整章 TTS 的 timeout_sec（10800 秒）——那个超时拿来管一个挂死的预计算，
        # 会把唯一的 GPU 通道占住 3 小时
        timeout = tts_cfg.get("precompute_timeout_sec", tts_cfg.get("timeout_sec", 1800))
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "预计算超时"}

    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or proc.stdout)[-2000:]}
    return {"ok": True, "error": None}


def set_role_reference(role_id: str, wav_path: str, manifest: dict, roles_dir: str = None):
    """
    用新的 wav 文件替换角色的 reference.wav，并使旧的 speaker_embeddings.pt /
    .meta.json 立即失效（直接删除而非等下次 MD5 比对，避免任何绕过
    get_embedding_status 校验的调用路径误用陈旧音色）。role_id 必须已注册。
    """
    if role_id not in manifest.get("roles", {}):
        raise ValueError(f"角色 {role_id!r} 未注册，无法设置参考音频")
    if not os.path.exists(wav_path):
        raise FileNotFoundError(f"参考音频不存在: {wav_path}")

    base = roles_dir if roles_dir is not None else resolve_path("roles")
    role_dir = os.path.join(base, role_id)
    os.makedirs(role_dir, exist_ok=True)
    shutil.copyfile(wav_path, os.path.join(role_dir, "reference.wav"))

    for fname in (EMBEDDING_FILENAME, EMBEDDING_META_FILENAME):
        stale_path = os.path.join(role_dir, fname)
        if os.path.exists(stale_path):
            os.remove(stale_path)


def delete_role(role_id: str, manifest: dict, roles_dir: str = None):
    """
    删除一个角色：从清单中移除并删除其整个目录（config.json / reference.wav /
    speaker_embeddings.pt 等）。narrator 是所有角色 TTS 兜底音色的来源
    （见 get_role_runtime_config 与 register_role 的占位逻辑），禁止删除。
    角色本就不存在时静默返回（幂等）。
    """
    if role_id == "narrator":
        raise ValueError("不允许删除 narrator（其他角色注册/合成兜底依赖它）")

    roles = manifest.get("roles", {})
    if role_id not in roles:
        return

    base = roles_dir if roles_dir is not None else resolve_path("roles")
    role_dir = os.path.join(base, role_id)
    if os.path.isdir(role_dir):
        shutil.rmtree(role_dir)

    del roles[role_id]
    save_manifest(manifest, roles_dir)
