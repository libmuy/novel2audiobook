"""
测试 src/audio_mixer.py 模块的各个函数
"""
import os
import json
import wave
import struct
import math
import pytest
from pydub import AudioSegment
from src import audio_mixer, utils
from tests.conftest import create_sample_wav_file


class TestMergeIntervals:
    """区间合并功能测试"""

    def test_merge_intervals_no_overlap(self):
        """无重叠区间保持独立"""
        intervals = [(0, 100), (500, 600)]
        result = audio_mixer._merge_intervals(intervals)
        assert len(result) == 2
        assert result[0] == (0, 100)
        assert result[1] == (500, 600)

    def test_merge_intervals_with_overlap(self):
        """重叠区间合并"""
        intervals = [(0, 100), (90, 200)]
        result = audio_mixer._merge_intervals(intervals)
        assert len(result) == 1
        assert result[0] == (0, 200)

    def test_merge_intervals_gap_within_threshold(self):
        """间隙在阈值内的区间合并"""
        intervals = [(0, 100), (200, 300)]
        result = audio_mixer._merge_intervals(intervals, gap_ms=150.0)
        assert len(result) == 1
        assert result[0][0] == 0
        assert result[0][1] == 300

    def test_merge_intervals_gap_exceed_threshold(self):
        """间隙超过阈值的区间保持独立"""
        intervals = [(0, 100), (400, 500)]
        result = audio_mixer._merge_intervals(intervals, gap_ms=150.0)
        assert len(result) == 2

    def test_merge_intervals_empty_list(self):
        """空列表"""
        result = audio_mixer._merge_intervals([])
        assert result == []

    def test_merge_intervals_single_interval(self):
        """单个区间"""
        result = audio_mixer._merge_intervals([(100, 200)])
        assert len(result) == 1
        assert result[0] == (100, 200)

    def test_merge_intervals_unsorted_input(self):
        """未排序的输入"""
        intervals = [(500, 600), (0, 100), (90, 200)]
        result = audio_mixer._merge_intervals(intervals)
        assert len(result) == 2
        # 应该自动排序后合并


class TestApplyDucking:
    """自动闪避功能测试"""

    def test_apply_ducking_no_intervals(self, tmp_chapter_dir):
        """无闪避区间，音频保持不变"""
        # 创建一个简单的背景音
        bgm_path = os.path.join(tmp_chapter_dir, "bgm.wav")
        create_sample_wav_file(bgm_path, duration_ms=5000)
        bgm_track = AudioSegment.from_file(bgm_path)

        result = audio_mixer._apply_ducking(bgm_track, [], -6.0, 300, 5000)

        # 应该返回一个音频
        assert isinstance(result, AudioSegment)
        assert len(result) > 0

    def test_apply_ducking_with_intervals(self, tmp_chapter_dir):
        """有闪避区间时应用降低"""
        bgm_path = os.path.join(tmp_chapter_dir, "bgm.wav")
        create_sample_wav_file(bgm_path, duration_ms=5000)
        bgm_track = AudioSegment.from_file(bgm_path)

        # 在 1000-2000ms 之间应用闪避
        duck_intervals = [(1000, 2000)]
        result = audio_mixer._apply_ducking(bgm_track, duck_intervals, -6.0, 300, 5000)

        assert isinstance(result, AudioSegment)
        assert len(result) > 0

    def test_apply_ducking_returns_audio_segment(self, tmp_chapter_dir):
        """返回 AudioSegment 对象"""
        bgm_path = os.path.join(tmp_chapter_dir, "bgm.wav")
        create_sample_wav_file(bgm_path, duration_ms=2000)
        bgm_track = AudioSegment.from_file(bgm_path)

        result = audio_mixer._apply_ducking(bgm_track, [(500, 1000)], -6.0, 200, 2000)

        assert isinstance(result, AudioSegment)


class TestApplyGainRamp:
    """增益渐变功能测试"""

    def test_apply_gain_ramp_basic(self, tmp_chapter_dir):
        """基本增益渐变"""
        wav_path = os.path.join(tmp_chapter_dir, "test.wav")
        create_sample_wav_file(wav_path, duration_ms=1000)
        segment = AudioSegment.from_file(wav_path)

        result = audio_mixer._apply_gain_ramp(segment, 0.0, -6.0)

        assert isinstance(result, AudioSegment)
        assert len(result) > 0

    def test_apply_gain_ramp_empty_segment(self):
        """空 segment"""
        empty_seg = AudioSegment.silent(duration=0)
        result = audio_mixer._apply_gain_ramp(empty_seg, 0.0, -6.0)
        assert len(result) == 0

    def test_apply_gain_ramp_flat_gain(self, tmp_chapter_dir):
        """相同的起止增益"""
        wav_path = os.path.join(tmp_chapter_dir, "test.wav")
        create_sample_wav_file(wav_path, duration_ms=1000)
        segment = AudioSegment.from_file(wav_path)

        result = audio_mixer._apply_gain_ramp(segment, -3.0, -3.0)

        assert isinstance(result, AudioSegment)


class TestMixChapter:
    """混音主函数功能测试"""

    def test_mix_chapter_creates_output(self, tmp_chapter_dir, tmp_roles_dir, sample_timeline_json):
        """生成输出 MP3 文件"""
        # 为 timeline 中的每个 item 创建对应的音频文件
        for item in sample_timeline_json["items"]:
            audio_path = os.path.join(tmp_chapter_dir, item["audio_path"])
            create_sample_wav_file(audio_path, duration_ms=1000)

        output_path = audio_mixer.mix_chapter(tmp_chapter_dir, timeline_data=sample_timeline_json)

        assert os.path.exists(output_path)

    def test_mix_chapter_output_format(self, tmp_chapter_dir, tmp_roles_dir, sample_timeline_json):
        """输出文件是 MP3 或 WAV"""
        # 创建音频文件
        for item in sample_timeline_json["items"]:
            audio_path = os.path.join(tmp_chapter_dir, item["audio_path"])
            create_sample_wav_file(audio_path, duration_ms=1000)

        output_path = audio_mixer.mix_chapter(tmp_chapter_dir, timeline_data=sample_timeline_json)

        assert output_path.endswith(".mp3") or output_path.endswith(".wav")

    def test_mix_chapter_missing_audio_file(self, tmp_chapter_dir, sample_timeline_json):
        """引用的音频文件不存在时抛异常"""
        # 不创建 audio_cache 文件
        with pytest.raises(FileNotFoundError):
            audio_mixer.mix_chapter(tmp_chapter_dir, timeline_data=sample_timeline_json)

    def test_mix_chapter_missing_timeline(self, tmp_chapter_dir):
        """timeline.json 不存在时抛异常"""
        with pytest.raises(FileNotFoundError):
            audio_mixer.mix_chapter(tmp_chapter_dir)

    def test_mix_chapter_output_playable(self, tmp_chapter_dir, tmp_roles_dir, sample_timeline_json):
        """生成的音频文件可以打开"""
        # 创建音频文件
        for item in sample_timeline_json["items"]:
            audio_path = os.path.join(tmp_chapter_dir, item["audio_path"])
            create_sample_wav_file(audio_path, duration_ms=1000)

        output_path = audio_mixer.mix_chapter(tmp_chapter_dir, timeline_data=sample_timeline_json)

        # 应该能用 pydub 打开
        try:
            audio = AudioSegment.from_file(output_path)
            assert len(audio) > 0
        except Exception as e:
            pytest.fail(f"生成的音频文件无法打开: {e}")

    def test_mix_chapter_updates_status(self, tmp_chapter_dir, tmp_roles_dir, sample_timeline_json):
        """更新 .status.json"""
        # 创建音频文件
        for item in sample_timeline_json["items"]:
            audio_path = os.path.join(tmp_chapter_dir, item["audio_path"])
            create_sample_wav_file(audio_path, duration_ms=1000)

        audio_mixer.mix_chapter(tmp_chapter_dir, timeline_data=sample_timeline_json)

        status_file = os.path.join(tmp_chapter_dir, ".status.json")
        assert os.path.exists(status_file)

        import json

        with open(status_file, "r", encoding="utf-8") as f:
            status_data = json.load(f)

        assert status_data.get("status") == "completed"

    def test_mix_chapter_respects_output_format_wav(self, tmp_chapter_dir, tmp_roles_dir, sample_timeline_json):
        """mixing.output_format=wav 时应该真的导出 wav，不是硬编码 mp3"""
        for item in sample_timeline_json["items"]:
            audio_path = os.path.join(tmp_chapter_dir, item["audio_path"])
            create_sample_wav_file(audio_path, duration_ms=1000)

        config = {"mixing": {"voice_only": True, "output_format": "wav"}}
        output_path = audio_mixer.mix_chapter(tmp_chapter_dir, timeline_data=sample_timeline_json, config=config)

        assert output_path.endswith(".wav")
        assert os.path.exists(output_path)

    def test_mix_chapter_defaults_to_mp3_when_unset(self, tmp_chapter_dir, tmp_roles_dir, sample_timeline_json):
        """不配置 output_format 时保持原有行为（默认 mp3）"""
        for item in sample_timeline_json["items"]:
            audio_path = os.path.join(tmp_chapter_dir, item["audio_path"])
            create_sample_wav_file(audio_path, duration_ms=1000)

        config = {"mixing": {"voice_only": True}}
        output_path = audio_mixer.mix_chapter(tmp_chapter_dir, timeline_data=sample_timeline_json, config=config)

        assert output_path.endswith(".mp3")

    def test_mix_chapter_invalid_output_format_falls_back_to_mp3(self, tmp_chapter_dir, tmp_roles_dir, sample_timeline_json):
        """不认识的格式名不应该让整个混音失败，退回 mp3"""
        for item in sample_timeline_json["items"]:
            audio_path = os.path.join(tmp_chapter_dir, item["audio_path"])
            create_sample_wav_file(audio_path, duration_ms=1000)

        config = {"mixing": {"voice_only": True, "output_format": "ogg-not-supported"}}
        output_path = audio_mixer.mix_chapter(tmp_chapter_dir, timeline_data=sample_timeline_json, config=config)

        assert output_path.endswith(".mp3")


class TestMixChapterVoiceOnly:
    """纯人声混音模式功能测试（阶段 A3：效果音先延后，避免占位音悄悄混入成片）"""

    def _make_timeline_with_effects(self, tmp_chapter_dir, sfx_name="no_such_sfx_xyz", bgm_name="no_such_bgm_xyz"):
        """构造一份引用了不存在的 sfx/bgm 名称的 timeline，用来验证两条路径都不会
        再往共享 assets/ 目录写占位文件（旧行为会静默调用 generate_mock_audio_file）。"""
        timeline = {
            "chapter_id": "ch_0001",
            "total_duration_ms": 3000.0,
            "tts_engine": "mock",
            "used_fallback": False,
            "items": [
                {
                    "seg_id": 1, "speaker": "narrator", "text": "测试",
                    "emotion": "neutral", "audio_path": "audio_cache/hash1.wav",
                    "start_time_ms": 0.0, "duration_ms": 1000.0,
                    "sfx": sfx_name, "bgm": bgm_name, "cached": False,
                },
            ],
        }
        for item in timeline["items"]:
            audio_path = os.path.join(tmp_chapter_dir, item["audio_path"])
            create_sample_wav_file(audio_path, duration_ms=1000)
        return timeline

    def test_default_config_is_voice_only(self, tmp_chapter_dir, tmp_roles_dir):
        """未显式传 voice_only、且 config 里没有 mixing.voice_only 键时，默认按纯人声处理
        （效果音阶段尚未开始，默认不能悄悄把占位音混进成片）"""
        timeline = self._make_timeline_with_effects(tmp_chapter_dir)
        sfx_path = os.path.join(utils.assets_dir(), "sfx", "no_such_sfx_xyz.wav")
        bgm_path = os.path.join(utils.assets_dir(), "ambience", "no_such_bgm_xyz.wav")

        output_path = audio_mixer.mix_chapter(
            tmp_chapter_dir, timeline_data=timeline, config={"mixing": {}}
        )

        assert os.path.exists(output_path)
        assert not os.path.exists(sfx_path), "voice_only 模式不该为缺失的 sfx 在共享 assets/ 目录生成占位文件"
        assert not os.path.exists(bgm_path), "voice_only 模式不该为缺失的 bgm 在共享 assets/ 目录生成占位文件"

    def test_explicit_voice_only_true_ignores_missing_effects(self, tmp_chapter_dir, tmp_roles_dir):
        """显式 voice_only=True：即使 timeline 引用了不存在的 sfx/bgm，也不报错、不生成占位文件"""
        timeline = self._make_timeline_with_effects(tmp_chapter_dir)
        output_path = audio_mixer.mix_chapter(
            tmp_chapter_dir, timeline_data=timeline, config={"mixing": {"voice_only": True}}, voice_only=True
        )
        assert os.path.exists(output_path)

    def test_voice_only_false_missing_effects_skips_without_writing_placeholder(
        self, tmp_chapter_dir, tmp_roles_dir
    ):
        """
        voice_only=False（--with-assets）时，缺失的 sfx/bgm 应该被跳过并记录警告，
        而不是像旧行为那样静默调用 generate_mock_audio_file 往共享 assets/ 目录写文件。
        """
        timeline = self._make_timeline_with_effects(tmp_chapter_dir)
        sfx_path = os.path.join(utils.assets_dir(), "sfx", "no_such_sfx_xyz.wav")
        bgm_path = os.path.join(utils.assets_dir(), "ambience", "no_such_bgm_xyz.wav")
        assert not os.path.exists(sfx_path) and not os.path.exists(bgm_path)  # 前置条件

        output_path = audio_mixer.mix_chapter(
            tmp_chapter_dir, timeline_data=timeline, config={"mixing": {}}, voice_only=False
        )

        assert os.path.exists(output_path)
        assert not os.path.exists(sfx_path), "mix 是读路径，缺素材不该反过来写共享 assets/ 目录"
        assert not os.path.exists(bgm_path), "mix 是读路径，缺素材不该反过来写共享 assets/ 目录"

    def test_mix_writes_meta_sidecar_with_missing_assets(self, tmp_chapter_dir, tmp_roles_dir):
        """voice_only=False 且素材缺失：混音仍然成功（warn-and-skip 契约不变），
        但 output/mix_meta.json 要如实记录缺了哪些素材。"""
        timeline = self._make_timeline_with_effects(tmp_chapter_dir)
        output_path = audio_mixer.mix_chapter(
            tmp_chapter_dir, timeline_data=timeline, config={"mixing": {}}, voice_only=False
        )
        assert os.path.exists(output_path)

        meta_path = os.path.join(tmp_chapter_dir, "output", "mix_meta.json")
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        assert meta["voice_only"] is False
        assert meta["output_file"] == os.path.basename(output_path)
        assert meta["missing_assets"] == {"bgm": ["no_such_bgm_xyz"], "sfx": ["no_such_sfx_xyz"]}
        assert meta["bgm_names"] == ["no_such_bgm_xyz"]
        assert meta["sfx_count"] == 1

    def test_mix_meta_sidecar_voice_only_records_no_assets(self, tmp_chapter_dir, tmp_roles_dir):
        """纯人声混音：sidecar 记 voice_only=True，且不把 timeline 里潜在的素材
        引用当成"用上了"——这次实际上没混进任何素材。"""
        timeline = self._make_timeline_with_effects(tmp_chapter_dir)
        audio_mixer.mix_chapter(
            tmp_chapter_dir, timeline_data=timeline, config={"mixing": {}}, voice_only=True
        )
        with open(os.path.join(tmp_chapter_dir, "output", "mix_meta.json"), "r", encoding="utf-8") as f:
            meta = json.load(f)
        assert meta["voice_only"] is True
        assert meta["bgm_names"] == []
        assert meta["sfx_count"] == 0
        assert meta["missing_assets"] == {"bgm": [], "sfx": []}

    def test_voice_only_true_shorter_than_full_mix_path_no_crash_on_none_effects(
        self, tmp_chapter_dir, tmp_roles_dir, sample_timeline_json
    ):
        """voice_only=True 时，sfx/bgm 均为 None 的普通 timeline 依然正常出片"""
        for item in sample_timeline_json["items"]:
            audio_path = os.path.join(tmp_chapter_dir, item["audio_path"])
            create_sample_wav_file(audio_path, duration_ms=1000)

        output_path = audio_mixer.mix_chapter(
            tmp_chapter_dir, timeline_data=sample_timeline_json, voice_only=True
        )
        assert os.path.exists(output_path)


class TestGenerateMockAudioFile:
    """占位音频文件生成功能测试"""

    def test_generate_mock_audio_file_creates_file(self, tmp_chapter_dir):
        """创建占位 WAV 文件"""
        output_path = os.path.join(tmp_chapter_dir, "mock.wav")
        audio_mixer.generate_mock_audio_file(output_path, duration_ms=2000)

        assert os.path.exists(output_path)

    def test_generate_mock_audio_file_duration(self, tmp_chapter_dir):
        """生成的文件时长正确"""
        output_path = os.path.join(tmp_chapter_dir, "mock.wav")
        duration_ms = 3000
        audio_mixer.generate_mock_audio_file(output_path, duration_ms=duration_ms)

        with wave.open(output_path, "r") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            actual_duration_ms = (frames / rate) * 1000
            # 允许小误差
            assert abs(actual_duration_ms - duration_ms) < 100

    def test_generate_mock_audio_file_format(self, tmp_chapter_dir):
        """生成的 WAV 文件格式正确"""
        output_path = os.path.join(tmp_chapter_dir, "mock.wav")
        audio_mixer.generate_mock_audio_file(output_path)

        with wave.open(output_path, "r") as wf:
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2
            assert wf.getframerate() == 24000


class TestBuildSceneBgmTrack:
    """场景级 BGM 轨道生成功能测试"""

    def test_build_scene_bgm_track_empty_items(self, tmp_chapter_dir):
        """空 item 列表"""
        result = audio_mixer._build_scene_bgm_track([], 5000, tmp_chapter_dir)

        assert isinstance(result, AudioSegment)
        assert len(result) >= 5000

    def test_build_scene_bgm_track_with_items(self, tmp_chapter_dir):
        """包含 item 的列表"""
        items = [
            {
                "seg_id": 1,
                "bgm": "rain_heavy",
                "start_time_ms": 0,
                "duration_ms": 2000,
            },
            {
                "seg_id": 2,
                "bgm": None,
                "start_time_ms": 2000,
                "duration_ms": 1000,
            },
        ]

        result = audio_mixer._build_scene_bgm_track(items, 5000, tmp_chapter_dir)

        assert isinstance(result, AudioSegment)

    def test_build_scene_bgm_track_no_bgm(self, tmp_chapter_dir):
        """所有 item 都没有 bgm"""
        items = [
            {
                "seg_id": 1,
                "bgm": None,
                "start_time_ms": 0,
                "duration_ms": 2000,
            },
        ]

        result = audio_mixer._build_scene_bgm_track(items, 3000, tmp_chapter_dir)

        assert isinstance(result, AudioSegment)


class TestValidateTimelineAssets:
    """时间线资产验证功能测试"""

    def test_validate_timeline_assets_all_present(self, tmp_chapter_dir, sample_timeline_json):
        """所有资产存在"""
        # 创建所有引用的音频文件
        for item in sample_timeline_json["items"]:
            audio_path = os.path.join(tmp_chapter_dir, item["audio_path"])
            create_sample_wav_file(audio_path, duration_ms=1000)

        # 不应该抛异常
        audio_mixer._validate_timeline_assets(sample_timeline_json["items"], tmp_chapter_dir)

    def test_validate_timeline_assets_missing_file(self, tmp_chapter_dir, sample_timeline_json):
        """资产文件缺失"""
        # 不创建任何音频文件
        with pytest.raises(FileNotFoundError):
            audio_mixer._validate_timeline_assets(sample_timeline_json["items"], tmp_chapter_dir)

    def test_validate_timeline_assets_empty_items(self, tmp_chapter_dir):
        """空 item 列表"""
        # 不应该抛异常
        audio_mixer._validate_timeline_assets([], tmp_chapter_dir)


class TestDuckingGainPrecedence:
    """mixing.ducking_gain_db（dB）优先于旧的 ducking_volume_ratio（线性比例，不是 dB）"""

    def _run(self, tmp_chapter_dir, monkeypatch, mixing_cfg):
        seen = {}

        def fake_duck(bgm_track, intervals, db_change, fade_ms, total_ms):
            seen["db"] = db_change
            return bgm_track

        monkeypatch.setattr(audio_mixer, "_apply_ducking", fake_duck)
        audio_path = os.path.join(tmp_chapter_dir, "audio_cache", "a.wav")
        create_sample_wav_file(audio_path, duration_ms=1000)
        timeline = {"chapter_id": "x", "total_duration_ms": 1000, "items": [
            {"seg_id": 1, "audio_path": "audio_cache/a.wav", "start_time_ms": 0.0,
             "duration_ms": 1000.0, "sfx": None, "bgm": None}]}
        audio_mixer.mix_chapter(tmp_chapter_dir, timeline_data=timeline,
                                config={"mixing": mixing_cfg}, voice_only=False)
        return seen["db"]

    def test_gain_db_wins_over_ratio(self, tmp_chapter_dir, monkeypatch):
        assert self._run(tmp_chapter_dir, monkeypatch,
                         {"ducking_gain_db": -6.0, "ducking_volume_ratio": 0.3}) == -6.0

    def test_zero_gain_db_is_respected_not_treated_as_unset(self, tmp_chapter_dir, monkeypatch):
        """0 dB（不闪避）是合法值——用 `is not None` 判断，不能被当成「没设置」回落到比例"""
        assert self._run(tmp_chapter_dir, monkeypatch,
                         {"ducking_gain_db": 0.0, "ducking_volume_ratio": 0.3}) == 0.0

    def test_falls_back_to_ratio_when_unset(self, tmp_chapter_dir, monkeypatch):
        db = self._run(tmp_chapter_dir, monkeypatch, {"ducking_volume_ratio": 0.5})
        assert db == pytest.approx(20 * math.log10(0.5))
