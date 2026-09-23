"""
LLM 剧本解析模块

架构：LLMBackend 抽象出两种实现——
- QwenLLMBackend：通过 OpenAI 兼容接口调用本地 llama-server 上的 Qwen 模型，
  按段落切分小说文本，请求逐句拆分为带角色/情感/音效的剧本 JSON。
- HeuristicBackend：不依赖网络的规则版回退实现，在 LLM 不可达/解析失败时兜底，
  也用于 `cli.py test` 自检（保证自检不依赖外部服务）。

说话人 ID 统一在 process_chapter_parse 中经 src.domain.roles 归一化：
精确/别名/拼音归一匹配已注册角色 -> 未命中则留空（speaker: null），由人工后续指派。
"""
import os
import re
import json
import logging

import requests

from src.utils import update_chapter_status, load_global_config, list_available_assets
from src.domain import roles as roles_mod
from src.runtime.pipeline_errors import TaskCancelled

logger = logging.getLogger(__name__)

VALID_EMOTIONS = {"neutral", "happy", "angry", "sad", "serious", "afraid", "surprised", "calm"}


# --------------------------------------------------------------------------
# 段落切分
# --------------------------------------------------------------------------

def split_paragraphs(text: str) -> list:
    """按空行切分章节文本为段落列表，过滤空白段与纯场景分隔符"""
    raw_paragraphs = re.split(r"\n\s*\n", text)
    paragraphs = []
    for p in raw_paragraphs:
        p = p.strip()
        if not p or p in ("※", "***", "---"):
            continue
        paragraphs.append(p)
    return paragraphs


# --------------------------------------------------------------------------
# 启发式回退解析器（无 LLM 依赖）
# --------------------------------------------------------------------------

_QUOTE_SPLIT = re.compile(r"“([^”]*)”")

_ANGRY_HINTS = ("厉声", "怒", "吼", "骂", "辱")
_AFRAID_HINTS = ("怕", "恐惧", "颤抖", "发麻", "心跳猛地")
_SAD_HINTS = ("咳血", "泪", "哭", "叹")

# 素材名 -> 触发关键词，用于从文本内容猜测环境音/音效（无 LLM 时的粗略近似）
_BGM_KEYWORDS = {
    "rain_heavy": ("雨", "水珠", "滴落", "潮湿"),
}
_SFX_KEYWORDS = {
    "sword_clash": ("刀剑", "兵器相交", "剑鸣", "刀光"),
}


def _guess_emotion(text: str, is_dialogue: bool) -> str:
    for kw in _ANGRY_HINTS:
        if kw in text:
            return "angry"
    for kw in _AFRAID_HINTS:
        if kw in text:
            return "afraid"
    for kw in _SAD_HINTS:
        if kw in text:
            return "sad"
    return "serious" if is_dialogue else "neutral"


def _guess_asset(text: str, keyword_map: dict, available: list) -> str:
    for asset_name, keywords in keyword_map.items():
        if asset_name not in available:
            continue
        if any(kw in text for kw in keywords):
            return asset_name
    return None


class HeuristicBackend:
    """规则版兜底解析器：按引号切分叙述/对话，猜测说话人与情感"""

    name = "heuristic"

    def parse_paragraph(self, paragraph: str, manifest: dict, assets: dict) -> list:
        segments = []
        pos = 0
        for m in _QUOTE_SPLIT.finditer(paragraph):
            narration_before = paragraph[pos:m.start()].strip()
            if narration_before:
                segments.append(self._make_segment(narration_before, manifest, assets, is_dialogue=False))
            dialogue_text = m.group(1).strip()
            # 说话人线索：优先看对话后紧跟的叙述（如 “……”柳禾的声音……），
            # 其次看对话前的叙述
            tail_start = m.end()
            tail_window = paragraph[tail_start:tail_start + 20]
            speaker_name = roles_mod.guess_name_from_context(tail_window, manifest) \
                or roles_mod.guess_name_from_context(narration_before[-20:], manifest)
            if dialogue_text:
                segments.append(self._make_segment(
                    dialogue_text, manifest, assets, is_dialogue=True, speaker_hint=speaker_name
                ))
            pos = m.end()
        remainder = paragraph[pos:].strip()
        if remainder:
            segments.append(self._make_segment(remainder, manifest, assets, is_dialogue=False))
        return segments

    def _make_segment(self, text, manifest, assets, is_dialogue, speaker_hint=None):
        speaker = speaker_hint if is_dialogue else "narrator"
        return {
            "speaker": speaker,
            "text": text,
            "emotion": _guess_emotion(text, is_dialogue),
            "sfx": _guess_asset(text, _SFX_KEYWORDS, assets.get("sfx", [])),
            "bgm": _guess_asset(text, _BGM_KEYWORDS, assets.get("bgm", [])),
        }


# --------------------------------------------------------------------------
# 真实 Qwen LLM 后端
# --------------------------------------------------------------------------

_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)

_SYSTEM_PROMPT_TEMPLATE = """你是小说转有声书的剧本解析器。将给定的一段小说文本，按“说话人”切分为句子/子句级的剧本片段。

规则：
1. 每个片段只能属于一个说话人；叙述性文字的说话人固定为 "narrator"。
2. speaker 字段只能填 "narrator"（一切叙述性文字）或下面这份已注册角色清单里的中文名之一：
{role_table}
   如果某句台词的说话人不在这份清单里，speaker 必须填 null。
   不要编造新名字，也不要硬套一个不相干的角色——填 null 比填错更有价值，后续会由人工指派。
3. emotion 字段必须是以下之一：{emotions}。
4. sfx（音效）字段只能从这个词表中选择，或者为 null（没有则填 null，不要编造）：{sfx_list}
5. bgm（环境音）字段只能从这个词表中选择，或者为 null：{bgm_list}
6. 严格按输入文本的顺序和原文内容切分，不要增删、翻译或改写正文内容；对话保留引号内的原文（不含引号本身）。
7. 只输出一个 JSON 数组，数组每项形如：
   {{"speaker": "..." 或 null, "text": "...", "emotion": "...", "sfx": null, "bgm": null}}
   不要输出任何解释文字、Markdown 代码块标记或多余内容。
"""


def _render_asset_list(names: list, descriptions: dict) -> str:
    """渲染素材词表为「名字（中文描述）」，方便 LLM 按语义选材；描述缺失时退化为裸名字"""
    parts = []
    for name in names:
        desc = descriptions.get(name)
        parts.append(f"{name}（{desc}）" if desc else name)
    return "、".join(parts)


def _render_role_table(manifest: dict) -> str:
    """把角色清单渲染成「- 中文名：简介」的多行文本，供 prompt 注入。"""
    roles = manifest.get("roles", {})
    if not roles:
        return "  （暂无已注册角色）"
    lines = []
    for rid, info in roles.items():
        name = info.get("name", rid)
        desc = info.get("description", "")
        lines.append(f"  - {name}：{desc}" if desc else f"  - {name}")
    return "\n".join(lines)


class QwenLLMBackend:
    """通过本地 llama-server 的 OpenAI 兼容接口调用 Qwen 模型"""

    name = "qwen_llm"

    def __init__(self, config: dict):
        llm_cfg = config.get("llm", {})
        self.api_base = llm_cfg.get("api_base", "http://localhost:8080/v1")
        self.model_name = llm_cfg.get("model_name", "")
        self.temperature = llm_cfg.get("temperature", 0.3)
        self.max_tokens = llm_cfg.get("max_tokens", 2048)
        self.timeout = llm_cfg.get("request_timeout_sec", 60)
        self.connect_timeout = llm_cfg.get("connect_check_timeout_sec", 3)

    def is_available(self) -> bool:
        try:
            resp = requests.get(f"{self.api_base}/models", timeout=self.connect_timeout)
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def _build_system_prompt(self, manifest: dict, assets: dict) -> str:
        role_table = _render_role_table(manifest)

        try:
            from src.pipeline.asset_gen import get_asset_descriptions
            descriptions = get_asset_descriptions()
        except Exception as e:  # noqa: BLE001 - spec 文件缺失/格式错误不应影响解析主流程
            logger.warning("加载素材中文描述失败，词表退化为裸名字: %s", e)
            descriptions = {"sfx": {}, "bgm": {}}

        sfx_list = _render_asset_list(assets.get("sfx", []), descriptions.get("sfx", {})) \
            or "（无可用音效，一律填 null）"
        bgm_list = _render_asset_list(assets.get("bgm", []), descriptions.get("bgm", {})) \
            or "（无可用环境音，一律填 null）"
        return _SYSTEM_PROMPT_TEMPLATE.format(
            role_table=role_table,
            emotions="、".join(sorted(VALID_EMOTIONS)),
            sfx_list=sfx_list,
            bgm_list=bgm_list,
        )

    def _call_chat(self, system_prompt: str, user_text: str) -> str:
        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        resp = requests.post(f"{self.api_base}/chat/completions", json=payload, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    @staticmethod
    def _extract_json_array(raw_content: str) -> list:
        match = _JSON_ARRAY_RE.search(raw_content)
        if not match:
            raise ValueError(f"响应中未找到 JSON 数组: {raw_content[:200]!r}")
        return json.loads(match.group(0))

    def parse_paragraph(self, paragraph: str, manifest: dict, assets: dict) -> list:
        system_prompt = self._build_system_prompt(manifest, assets)
        last_err = None
        for attempt in range(2):
            try:
                content = self._call_chat(system_prompt, paragraph)
                items = self._extract_json_array(content)
                segments = []
                for item in items:
                    text = str(item.get("text", "")).strip()
                    if not text:
                        continue
                    emotion = item.get("emotion") if item.get("emotion") in VALID_EMOTIONS else "neutral"
                    sfx = item.get("sfx") if item.get("sfx") in assets.get("sfx", []) else None
                    bgm = item.get("bgm") if item.get("bgm") in assets.get("bgm", []) else None
                    raw_speaker = item.get("speaker")
                    speaker = None if raw_speaker is None else str(raw_speaker).strip()
                    if speaker in ("", "null", "None", "NULL", "未知", "无"):
                        speaker = None
                    segments.append({
                        "speaker": speaker,
                        "text": text,
                        "emotion": emotion,
                        "sfx": sfx,
                        "bgm": bgm,
                    })
                return segments
            except Exception as e:  # noqa: BLE001 - 需要兜底任何解析/网络异常
                last_err = e
                logger.warning("[QwenLLMBackend] 第 %d 次尝试解析段落失败: %s", attempt + 1, e)
        raise RuntimeError(f"QwenLLMBackend 解析段落失败（已重试）: {last_err}")


# --------------------------------------------------------------------------
# 顶层调度
# --------------------------------------------------------------------------

def build_backend(config: dict):
    """优先尝试真实 Qwen LLM 后端；不可达时返回启发式回退后端"""
    qwen_backend = QwenLLMBackend(config)
    if qwen_backend.is_available():
        return qwen_backend
    logger.warning("Qwen LLM 服务 (%s) 不可达，回退到启发式解析器", qwen_backend.api_base)
    return HeuristicBackend()


def parse_text_to_json(text: str, backend=None, roles_dir: str = None,
                       progress_cb=None, should_cancel=None) -> list:
    """
    解析长文本为细颗粒度（句子级）的剧本 JSON 数据结构。

    返回字段说明：
    - seg_id: 句段序号 (1, 2, ...)
    - speaker: 说话人角色 ID（已归一化）；未绑定角色时为 None，由人工后续指派
    - text: 台词/旁白文本
    - emotion: 情感说明
    - sfx: 伴随音效（data/assets/sfx 词表内的名称，或 None）
    - bgm: 背景音乐（data/assets/ambience 词表内的名称，或 None）
    """
    if backend is None:
        backend = build_backend(load_global_config())

    manifest = roles_mod.load_manifest(roles_dir)
    assets = list_available_assets()
    paragraphs = split_paragraphs(text)

    script_segments = []
    seg_id = 1
    for idx, para in enumerate(paragraphs):
        if should_cancel and should_cancel():
            raise TaskCancelled(f"用户取消（已完成 {idx}/{len(paragraphs)} 段）")
        try:
            raw_segments = backend.parse_paragraph(para, manifest, assets)
        except Exception as e:  # noqa: BLE001 - 单段失败不应中断整章解析
            logger.warning("段落解析失败，改用启发式回退处理该段: %s", e)
            raw_segments = HeuristicBackend().parse_paragraph(para, manifest, assets)

        for seg in raw_segments:
            raw_speaker = seg.get("speaker")
            if raw_speaker is None:
                role_id = None
            elif raw_speaker == "narrator":
                role_id = "narrator"
            else:
                role_id = roles_mod.resolve_role_id(raw_speaker, manifest)

            script_segments.append({
                "seg_id": seg_id,
                "speaker": role_id,
                "text": seg["text"],
                "emotion": seg.get("emotion", "neutral"),
                "sfx": seg.get("sfx"),
                "bgm": seg.get("bgm"),
            })
            seg_id += 1

        if progress_cb:
            progress_cb(idx + 1, len(paragraphs), f"已解析 {idx + 1}/{len(paragraphs)} 段")

    return script_segments


def process_chapter_parse(chapter_dir: str, roles_dir: str = None,
                          progress_cb=None, should_cancel=None) -> str:
    """
    读取 raw.txt，生成 script_draft.json
    """
    raw_path = os.path.join(chapter_dir, "raw.txt")
    if not os.path.exists(raw_path):
        raise FileNotFoundError(f"未找到原始文本文件: {raw_path}")

    with open(raw_path, "r", encoding="utf-8") as f:
        text = f.read()

    script_draft = parse_text_to_json(text, roles_dir=roles_dir,
                                      progress_cb=progress_cb, should_cancel=should_cancel)

    draft_path = os.path.join(chapter_dir, "script_draft.json")
    with open(draft_path, "w", encoding="utf-8") as f:
        json.dump(script_draft, f, ensure_ascii=False, indent=2)

    update_chapter_status(chapter_dir, "parsed_draft")
    return draft_path
