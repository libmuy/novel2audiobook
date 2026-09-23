"""
Pytest 共享配置与 fixture 定义
"""
import os
import shutil
import json
import tempfile
import pytest
from pathlib import Path


@pytest.fixture
def tmp_project_dir():
    """创建临时项目目录结构（带 roles/、assets/ 副本）"""
    tmp_root = tempfile.mkdtemp(prefix="n2a_test_")

    # 创建必要的目录
    os.makedirs(os.path.join(tmp_root, "chapters"), exist_ok=True)
    os.makedirs(os.path.join(tmp_root, "roles"), exist_ok=True)
    os.makedirs(os.path.join(tmp_root, "assets"), exist_ok=True)

    yield tmp_root

    # 清理
    shutil.rmtree(tmp_root, ignore_errors=True)


@pytest.fixture
def tmp_chapters_dir(tmp_project_dir):
    """返回临时项目中的 chapters 目录路径"""
    return os.path.join(tmp_project_dir, "chapters")


@pytest.fixture
def tmp_roles_dir(tmp_project_dir):
    """返回临时项目中的 roles 目录路径，并复制真实项目的 roles 内容"""
    from src.utils import get_project_root

    tmp_roles = os.path.join(tmp_project_dir, "roles")
    real_roles = os.path.join(get_project_root(), "data", "roles")

    # 如果真实 roles 存在，复制其内容
    if os.path.exists(real_roles):
        for item in os.listdir(real_roles):
            src_path = os.path.join(real_roles, item)
            dst_path = os.path.join(tmp_roles, item)
            if os.path.isdir(src_path):
                shutil.copytree(src_path, dst_path, dirs_exist_ok=True)
            else:
                shutil.copy2(src_path, dst_path)

    return tmp_roles


@pytest.fixture
def tmp_chapter_dir(tmp_chapters_dir):
    """创建一个临时章节目录 (ch_0001)"""
    ch_dir = os.path.join(tmp_chapters_dir, "ch_0001")
    os.makedirs(ch_dir, exist_ok=True)
    return ch_dir


@pytest.fixture
def sample_raw_text():
    """示例小说文本（用于解析测试）"""
    return """苏砚缓缓睁开眼睛，黑暗中传来悉悉索索的声音。

"是谁在那里？"苏砚的声音透着冷意。

旁白接着说，他转身看向窗外，雨声淅沥沥地下着。林动从阴影中走了出来，怒火在眼中燃烧。

"苏砚，我不会放过你！"林动厉声吼道。

苏砚轻轻一笑，仿佛在嘲笑某个不值得的对手。刀剑相交的声音在空气中回响。

本场景结束。

第二个段落开始了。这是一段叙述文本，不含对话。
"""


@pytest.fixture
def sample_script_json():
    """示例剧本 JSON 数据"""
    return [
        {
            "seg_id": 1,
            "speaker": "narrator",
            "text": "苏砚缓缓睁开眼睛，黑暗中传来悉悉索索的声音。",
            "emotion": "neutral",
            "sfx": None,
            "bgm": None,
        },
        {
            "seg_id": 2,
            "speaker": "su_yan",
            "text": "是谁在那里？",
            "emotion": "serious",
            "sfx": None,
            "bgm": None,
        },
        {
            "seg_id": 3,
            "speaker": "narrator",
            "text": "他转身看向窗外，雨声淅沥沥地下着。",
            "emotion": "neutral",
            "sfx": None,
            "bgm": "rain_heavy",
        },
        {
            "seg_id": 4,
            "speaker": "lin_dong",
            "text": "苏砚，我不会放过你！",
            "emotion": "angry",
            "sfx": None,
            "bgm": None,
        },
    ]


@pytest.fixture
def sample_timeline_json():
    """示例时间线 JSON 数据（来自 TTS 生成）"""
    return {
        "chapter_id": "ch_0001",
        "total_duration_ms": 5000.0,
        "tts_engine": "mock",
        "used_fallback": False,
        "items": [
            {
                "seg_id": 1,
                "speaker": "narrator",
                "text": "测试音频段1",
                "emotion": "neutral",
                "audio_path": "audio_cache/hash1.wav",
                "start_time_ms": 0.0,
                "duration_ms": 1000.0,
                "sfx": None,
                "bgm": None,
                "cached": False,
            },
            {
                "seg_id": 2,
                "speaker": "narrator",
                "text": "测试音频段2",
                "emotion": "neutral",
                "audio_path": "audio_cache/hash2.wav",
                "start_time_ms": 1200.0,
                "duration_ms": 1000.0,
                "sfx": None,
                "bgm": None,
                "cached": False,
            },
        ],
    }


def create_sample_wav_file(filepath: str, duration_ms: int = 1000, sample_rate: int = 24000):
    """创建一个占位 WAV 文件用于测试"""
    import wave
    import struct
    import math

    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    num_samples = int(sample_rate * (duration_ms / 1000.0))

    with wave.open(filepath, "w") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        frames = bytearray()
        for i in range(num_samples):
            val = int(500 * math.sin(2 * math.pi * 440 * i / sample_rate))
            frames.extend(struct.pack("<h", int(val)))
        f.writeframes(frames)


@pytest.fixture
def sample_wav_file(tmp_chapter_dir):
    """在临时章节目录中创建示例 WAV 文件"""
    wav_path = os.path.join(tmp_chapter_dir, "audio_cache", "test_audio.wav")
    create_sample_wav_file(wav_path, duration_ms=1000)
    return wav_path
