"""
测试 src/tts_engine.py 模块的各个函数
"""
import os
import json
import wave
import pytest
from src import tts_engine, roles as roles_mod, utils
from tests.conftest import create_sample_wav_file


class TestMockTTSBackend:
    """Mock TTS 后端功能测试"""

    def test_mock_backend_is_available(self):
        """Mock 后端始终可用"""
        backend = tts_engine.MockTTSBackend()
        assert backend.is_available() is True

    def test_mock_backend_synthesize_creates_file(self, tmp_chapter_dir, tmp_roles_dir):
        """synthesize 创建 WAV 文件"""
        backend = tts_engine.MockTTSBackend()
        manifest = roles_mod.load_manifest(tmp_roles_dir)
        role_cfg = roles_mod.get_role_runtime_config("narrator", manifest, tmp_roles_dir)

        output_path = os.path.join(tmp_chapter_dir, "audio_cache", "test.wav")
        result = backend.synthesize("测试文本", role_cfg, "neutral", output_path, 24000)

        assert result is True
        assert os.path.exists(output_path)

    def test_mock_backend_synthesize_valid_wav(self, tmp_chapter_dir, tmp_roles_dir):
        """生成有效的 WAV 文件"""
        backend = tts_engine.MockTTSBackend()
        manifest = roles_mod.load_manifest(tmp_roles_dir)
        role_cfg = roles_mod.get_role_runtime_config("narrator", manifest, tmp_roles_dir)

        output_path = os.path.join(tmp_chapter_dir, "audio_cache", "test.wav")
        backend.synthesize("测试文本", role_cfg, "neutral", output_path, 24000)

        # 验证 WAV 文件的有效性
        with wave.open(output_path, "r") as wf:
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2
            assert wf.getframerate() == 24000
            assert wf.getnframes() > 0

    def test_mock_backend_multiple_synthesize(self, tmp_chapter_dir, tmp_roles_dir):
        """连续多次合成（模拟批量操作）"""
        backend = tts_engine.MockTTSBackend()
        manifest = roles_mod.load_manifest(tmp_roles_dir)
        role_cfg = roles_mod.get_role_runtime_config("narrator", manifest, tmp_roles_dir)

        # 依次调用 synthesize，模拟批量操作的效果
        job1_out = os.path.join(tmp_chapter_dir, "audio_cache", "job1.wav")
        job2_out = os.path.join(tmp_chapter_dir, "audio_cache", "job2.wav")

        result1 = backend.synthesize("文本1", role_cfg, "neutral", job1_out, 24000)
        result2 = backend.synthesize("文本2", role_cfg, "happy", job2_out, 24000)

        assert result1 is True
        assert result2 is True
        assert os.path.exists(job1_out)
        assert os.path.exists(job2_out)


class TestGenerateTTSIncremental:
    """增量 TTS 生成功能测试"""

    def test_generate_tts_incremental_creates_timeline(self, tmp_chapter_dir, tmp_roles_dir, sample_script_json):
        """生成 timeline.json"""
        result = tts_engine.generate_tts_incremental(
            tmp_chapter_dir,
            sample_script_json,
            backend=tts_engine.MockTTSBackend(),
            roles_dir=tmp_roles_dir,
        )

        timeline_path = os.path.join(tmp_chapter_dir, "timeline.json")
        assert os.path.exists(timeline_path)

    def test_generate_tts_incremental_timeline_structure(self, tmp_chapter_dir, tmp_roles_dir, sample_script_json):
        """timeline 数据结构正确"""
        result = tts_engine.generate_tts_incremental(
            tmp_chapter_dir,
            sample_script_json,
            backend=tts_engine.MockTTSBackend(),
            roles_dir=tmp_roles_dir,
        )

        assert "chapter_id" in result
        assert "total_duration_ms" in result
        assert "tts_engine" in result
        assert "used_fallback" in result
        assert "items" in result
        assert isinstance(result["items"], list)

    def test_generate_tts_incremental_segment_count(self, tmp_chapter_dir, tmp_roles_dir, sample_script_json):
        """时间线句段数与输入相同"""
        result = tts_engine.generate_tts_incremental(
            tmp_chapter_dir,
            sample_script_json,
            backend=tts_engine.MockTTSBackend(),
            roles_dir=tmp_roles_dir,
        )

        assert len(result["items"]) == len(sample_script_json)

    def test_generate_tts_incremental_item_structure(self, tmp_chapter_dir, tmp_roles_dir, sample_script_json):
        """时间线 item 结构"""
        result = tts_engine.generate_tts_incremental(
            tmp_chapter_dir,
            sample_script_json,
            backend=tts_engine.MockTTSBackend(),
            roles_dir=tmp_roles_dir,
        )

        for item in result["items"]:
            assert "seg_id" in item
            assert "speaker" in item
            assert "text" in item
            assert "emotion" in item
            assert "audio_path" in item
            assert "start_time_ms" in item
            assert "duration_ms" in item
            assert "cached" in item

    def test_generate_tts_incremental_caching(self, tmp_chapter_dir, tmp_roles_dir, sample_script_json):
        """第二次调用应全部命中缓存"""
        # 第一次生成
        result1 = tts_engine.generate_tts_incremental(
            tmp_chapter_dir,
            sample_script_json,
            backend=tts_engine.MockTTSBackend(),
            roles_dir=tmp_roles_dir,
        )

        # 第二次生成
        result2 = tts_engine.generate_tts_incremental(
            tmp_chapter_dir,
            sample_script_json,
            backend=tts_engine.MockTTSBackend(),
            roles_dir=tmp_roles_dir,
        )

        # 所有项都应该被标记为缓存命中
        assert all(item["cached"] for item in result2["items"])

    def test_generate_tts_incremental_audio_cache_deduplication(self, tmp_chapter_dir, tmp_roles_dir):
        """相同 speaker + text + emotion 的不同句段只合成一次"""
        # 构造两个相同的句段
        script_data = [
            {
                "seg_id": 1,
                "speaker": "narrator",
                "text": "相同文本",
                "emotion": "neutral",
                "sfx": None,
                "bgm": None,
            },
            {
                "seg_id": 2,
                "speaker": "narrator",
                "text": "相同文本",
                "emotion": "neutral",
                "sfx": None,
                "bgm": None,
            },
        ]

        tts_engine.generate_tts_incremental(
            tmp_chapter_dir,
            script_data,
            backend=tts_engine.MockTTSBackend(),
            roles_dir=tmp_roles_dir,
        )

        # 检查 audio_cache 目录中的文件数量
        cache_dir = os.path.join(tmp_chapter_dir, "audio_cache")
        wav_files = [f for f in os.listdir(cache_dir) if f.endswith(".wav")]

        # 应该只有 1 个 WAV 文件（去重后）
        assert len(wav_files) == 1

    def test_generate_tts_incremental_updates_status(self, tmp_chapter_dir, tmp_roles_dir, sample_script_json):
        """更新 .status.json"""
        tts_engine.generate_tts_incremental(
            tmp_chapter_dir,
            sample_script_json,
            backend=tts_engine.MockTTSBackend(),
            roles_dir=tmp_roles_dir,
        )

        status_file = os.path.join(tmp_chapter_dir, ".status.json")
        assert os.path.exists(status_file)

        with open(status_file, "r", encoding="utf-8") as f:
            status_data = json.load(f)

        assert status_data.get("status") == "tts_completed"

    def test_generate_tts_incremental_audio_paths_exist(self, tmp_chapter_dir, tmp_roles_dir, sample_script_json):
        """时间线中引用的音频文件都存在"""
        result = tts_engine.generate_tts_incremental(
            tmp_chapter_dir,
            sample_script_json,
            backend=tts_engine.MockTTSBackend(),
            roles_dir=tmp_roles_dir,
        )

        for item in result["items"]:
            audio_rel_path = item["audio_path"]
            audio_full_path = os.path.join(tmp_chapter_dir, audio_rel_path)
            assert os.path.exists(audio_full_path), f"音频文件不存在: {audio_full_path}"


class TestProcessChapterTts:
    """章节 TTS 处理功能测试"""

    def test_process_chapter_tts_creates_timeline(self, tmp_chapter_dir, tmp_roles_dir, sample_script_json):
        """创建 timeline.json"""
        final_path = os.path.join(tmp_chapter_dir, "script_final.json")
        with open(final_path, "w", encoding="utf-8") as f:
            json.dump(sample_script_json, f)

        timeline_path = tts_engine.process_chapter_tts(
            tmp_chapter_dir, roles_dir=tmp_roles_dir, backend=tts_engine.MockTTSBackend()
        )

        assert os.path.exists(timeline_path)
        assert timeline_path.endswith("timeline.json")

    def test_process_chapter_tts_missing_script_final(self, tmp_chapter_dir, tmp_roles_dir):
        """script_final.json 不存在时抛异常"""
        with pytest.raises(FileNotFoundError):
            tts_engine.process_chapter_tts(tmp_chapter_dir, roles_dir=tmp_roles_dir)

    def test_process_chapter_tts_valid_json(self, tmp_chapter_dir, tmp_roles_dir, sample_script_json):
        """生成有效的 JSON"""
        final_path = os.path.join(tmp_chapter_dir, "script_final.json")
        with open(final_path, "w", encoding="utf-8") as f:
            json.dump(sample_script_json, f)

        timeline_path = tts_engine.process_chapter_tts(
            tmp_chapter_dir, roles_dir=tmp_roles_dir, backend=tts_engine.MockTTSBackend()
        )

        with open(timeline_path, "r", encoding="utf-8") as f:
            timeline_data = json.load(f)

        assert isinstance(timeline_data, dict)
        assert "items" in timeline_data


class TestSpeedToDurationFactor:
    """速度转换函数测试"""

    def test_speed_to_duration_factor_normal(self):
        """正常速度转换"""
        factor = tts_engine.speed_to_duration_factor(1.0)
        assert factor == 1.0

    def test_speed_to_duration_factor_faster_speed(self):
        """更快的速度转换"""
        factor = tts_engine.speed_to_duration_factor(2.0)
        assert 0 < factor < 1.0

    def test_speed_to_duration_factor_slower_speed(self):
        """更慢的速度转换"""
        factor = tts_engine.speed_to_duration_factor(0.5)
        assert factor > 1.0

    def test_speed_to_duration_factor_clamped(self):
        """夹取在允许范围内"""
        factor1 = tts_engine.speed_to_duration_factor(0.1)
        factor2 = tts_engine.speed_to_duration_factor(10.0)
        assert 0.5 <= factor1 <= 2.0
        assert 0.5 <= factor2 <= 2.0
