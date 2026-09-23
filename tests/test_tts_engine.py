"""
测试 src/pipeline/tts_engine.py 模块的各个函数
"""
import os
import json
import wave
import pytest
from src.pipeline import tts_engine
from src.domain import roles as roles_mod
from src import utils
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

    @pytest.mark.parametrize("gap,expected", [(None, 200.0), (0, 0.0), (500, 500.0)])
    def test_segment_gap_ms_is_configurable(self, tmp_chapter_dir, tmp_roles_dir, sample_script_json, gap, expected):
        """句间静音以前硬编码 200ms；现在读 tts.segment_gap_ms，缺省仍是 200（旧行为不变）"""
        config = {"tts": {} if gap is None else {"segment_gap_ms": gap}}
        result = tts_engine.generate_tts_incremental(
            tmp_chapter_dir, sample_script_json, backend=tts_engine.MockTTSBackend(),
            roles_dir=tmp_roles_dir, config=config,
        )
        items = result["items"]
        assert len(items) >= 2
        for a, b in zip(items, items[1:]):
            assert b["start_time_ms"] - (a["start_time_ms"] + a["duration_ms"]) == pytest.approx(expected)

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

    def test_generate_tts_incremental_batch_sorted_by_reference_audio(
        self, tmp_chapter_dir, tmp_roles_dir, sample_script_json
    ):
        """
        下发给 synthesize_batch 的任务应按 reference_audio 聚拢，而不是原始
        script 顺序（narrator/su_yan/narrator/lin_dong 交替）——避免 IndexTTS
        的音色缓存因说话人交替而逐句失效。时间线顺序（按 seg_id）不受影响，
        由另一条用例单独验证。
        """
        captured_jobs = []

        class RecordingBackend(tts_engine.MockTTSBackend):
            def synthesize_batch(self, jobs):
                captured_jobs.extend(jobs)
                return super().synthesize_batch(jobs)

        tts_engine.generate_tts_incremental(
            tmp_chapter_dir,
            sample_script_json,
            backend=RecordingBackend(),
            roles_dir=tmp_roles_dir,
        )

        ref_audios = [job["role_cfg"]["reference_audio"] for job in captured_jobs]
        assert ref_audios == sorted(ref_audios), "下发批次未按 reference_audio 排序"

    def test_generate_tts_incremental_timeline_order_unaffected_by_batch_sort(
        self, tmp_chapter_dir, tmp_roles_dir, sample_script_json
    ):
        """无论合成批次如何重排，timeline items 仍按原始 script 顺序（seg_id）输出"""
        result = tts_engine.generate_tts_incremental(
            tmp_chapter_dir,
            sample_script_json,
            backend=tts_engine.MockTTSBackend(),
            roles_dir=tmp_roles_dir,
        )

        seg_ids = [item["seg_id"] for item in result["items"]]
        assert seg_ids == [seg["seg_id"] for seg in sample_script_json]

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


class _ScriptedRealBackend:
    """伪装成真实引擎（name 不是 mock）：fail_ids 里的 job 报告失败，其余用 Mock 的音频当作真声写出"""

    name = "fake_real_engine"

    def __init__(self, fail_texts=()):
        self.fail_texts = set(fail_texts)
        self.calls = []

    def synthesize_batch(self, jobs):
        self.calls.append([j["text"] for j in jobs])
        results = {}
        for j in jobs:
            if j["text"] in self.fail_texts:
                results[j["id"]] = False
                continue
            tts_engine.MockTTSBackend().synthesize(j["text"], j["role_cfg"], j["emotion"], j["out"], j["sample_rate"])
            results[j["id"]] = True
        return results


class TestFallbackDoesNotPoisonTheCache:
    """回归：真实引擎没合成成功的句子被 Mock 占位音顶替，写进 audio_cache/<真实 md5>.wav，
    章节还被标成 tts_completed——占位噪音以合法名字永久命中缓存，再也没机会换成真声。"""

    def _texts(self, script):
        return [s["text"] for s in script]

    def _run(self, chapter_dir, script, roles_dir, backend, config=None):
        return tts_engine.generate_tts_incremental(chapter_dir, script, backend=backend,
                                                   roles_dir=roles_dir, config=config or {})

    def test_failed_sentence_gets_a_marker_and_timeline_flags_it(self, tmp_chapter_dir, tmp_roles_dir, sample_script_json):
        bad = self._texts(sample_script_json)[0]
        result = self._run(tmp_chapter_dir, sample_script_json, tmp_roles_dir, _ScriptedRealBackend([bad]))

        assert result["used_fallback"] is True
        flagged = [it for it in result["items"] if it["fallback"]]
        assert flagged and all(it["text"] == bad for it in flagged)
        for it in flagged:
            wav = os.path.join(tmp_chapter_dir, it["audio_path"])
            assert os.path.exists(wav) and os.path.exists(wav + ".fallback")
        assert all(it["fallback"] is False for it in result["items"] if it["text"] != bad)

    def test_rerun_retries_the_placeholder_with_the_real_engine(self, tmp_chapter_dir, tmp_roles_dir, sample_script_json):
        bad = self._texts(sample_script_json)[0]
        self._run(tmp_chapter_dir, sample_script_json, tmp_roles_dir, _ScriptedRealBackend([bad]))

        healed = _ScriptedRealBackend()  # 这次真实引擎能合成了
        result = self._run(tmp_chapter_dir, sample_script_json, tmp_roles_dir, healed)

        assert healed.calls == [[bad]]  # 只重试之前占位的那一句，其余命中缓存
        assert result["used_fallback"] is False
        assert all(it["fallback"] is False for it in result["items"])
        markers = [f for f in os.listdir(os.path.join(tmp_chapter_dir, "audio_cache")) if f.endswith(".fallback")]
        assert markers == []  # 成功后标记被清掉

    def test_still_failing_keeps_the_marker_and_keeps_retrying(self, tmp_chapter_dir, tmp_roles_dir, sample_script_json):
        bad = self._texts(sample_script_json)[0]
        self._run(tmp_chapter_dir, sample_script_json, tmp_roles_dir, _ScriptedRealBackend([bad]))
        again = _ScriptedRealBackend([bad])
        result = self._run(tmp_chapter_dir, sample_script_json, tmp_roles_dir, again)
        assert again.calls == [[bad]]
        assert any(it["fallback"] for it in result["items"]) and result["used_fallback"] is True

    def test_fully_synthesized_chapter_is_fully_cached_on_rerun(self, tmp_chapter_dir, tmp_roles_dir, sample_script_json):
        self._run(tmp_chapter_dir, sample_script_json, tmp_roles_dir, _ScriptedRealBackend())
        again = _ScriptedRealBackend()
        result = self._run(tmp_chapter_dir, sample_script_json, tmp_roles_dir, again)
        assert again.calls == [] and all(it["cached"] for it in result["items"])

    def test_strict_mode_fails_the_chapter_instead_of_writing_placeholders(self, tmp_chapter_dir, tmp_roles_dir, sample_script_json):
        """tts.fallback_on_failure=false：宁可整章失败，也不产出带占位音的成品"""
        bad = self._texts(sample_script_json)[0]
        with pytest.raises(RuntimeError, match="fallback_on_failure"):
            self._run(tmp_chapter_dir, sample_script_json, tmp_roles_dir, _ScriptedRealBackend([bad]),
                      config={"tts": {"fallback_on_failure": False}})
        assert not os.path.exists(os.path.join(tmp_chapter_dir, "timeline.json"))
        cache = os.path.join(tmp_chapter_dir, "audio_cache")
        assert [f for f in os.listdir(cache) if f.endswith(".fallback")] == []
        # 已经合成成功的句子留在缓存里，修好后重跑直接续上
        healed = _ScriptedRealBackend()
        self._run(tmp_chapter_dir, sample_script_json, tmp_roles_dir, healed)
        assert healed.calls == [[bad]]

    def test_default_is_lenient_matching_previous_behaviour(self, tmp_chapter_dir, tmp_roles_dir, sample_script_json):
        bad = self._texts(sample_script_json)[0]
        result = self._run(tmp_chapter_dir, sample_script_json, tmp_roles_dir, _ScriptedRealBackend([bad]))
        assert os.path.exists(os.path.join(tmp_chapter_dir, "timeline.json"))
        assert result["used_fallback"] is True

    def test_marker_files_are_not_counted_as_cached_wavs_in_status(self, tmp_chapter_dir, tmp_roles_dir, sample_script_json):
        from src.domain import status_tracker
        bad = self._texts(sample_script_json)[0]
        self._run(tmp_chapter_dir, sample_script_json, tmp_roles_dir, _ScriptedRealBackend([bad]))
        cache = os.path.join(tmp_chapter_dir, "audio_cache")
        n_wav = len([f for f in os.listdir(cache) if f.endswith(".wav")])
        status = status_tracker.get_all_chapters_status(os.path.dirname(tmp_chapter_dir))[0]
        assert status["audio_cache_count"] == n_wav


class TestCancelDoesNotTurnIntoSuccess:
    """回归（阶段 12）：取消会杀掉子进程，未完成的句子结果为 False。以前的流程紧接着给它们写
    Mock 占位噪音、标 tts_completed——「取消」变成了「完成」。"""

    def test_cancel_after_the_batch_raises_and_writes_no_placeholders_or_timeline(
            self, tmp_chapter_dir, tmp_roles_dir, sample_script_json):
        from src.runtime.pipeline_errors import TaskCancelled
        texts = [s["text"] for s in sample_script_json]
        first = texts[0]

        class KilledMidway(_ScriptedRealBackend):
            def synthesize_batch(self, jobs):
                res = super().synthesize_batch(jobs)
                # 模拟被杀：只有第一句真正完成，其余的输出已被后端删掉
                for j in jobs:
                    if j["text"] != first:
                        res[j["id"]] = False
                        if os.path.exists(j["out"]):
                            os.remove(j["out"])
                return res

        with pytest.raises(TaskCancelled):
            tts_engine.generate_tts_incremental(tmp_chapter_dir, sample_script_json, backend=KilledMidway(),
                                                roles_dir=tmp_roles_dir, config={}, should_cancel=lambda: True)

        cache = os.path.join(tmp_chapter_dir, "audio_cache")
        wavs = [f for f in os.listdir(cache) if f.endswith(".wav")]
        assert len(wavs) == 1, "只有真正合成完的那一句留在缓存里，没有占位噪音"
        assert [f for f in os.listdir(cache) if f.endswith(".fallback")] == []
        assert not os.path.exists(os.path.join(tmp_chapter_dir, "timeline.json"))

        # 重跑直接续上：已合成的命中缓存，其余重新合成
        healed = _ScriptedRealBackend()
        result = tts_engine.generate_tts_incremental(tmp_chapter_dir, sample_script_json, backend=healed,
                                                     roles_dir=tmp_roles_dir, config={})
        assert first not in [t for call in healed.calls for t in call]
        assert result["used_fallback"] is False
