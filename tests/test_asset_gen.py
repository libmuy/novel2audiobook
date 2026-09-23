"""
测试 src/pipeline/asset_gen.py 模块的各个函数
"""
import os
import json
import wave
import pytest
from pydub import AudioSegment

from src.pipeline import asset_gen


# --------------------------------------------------------------------------
# 公共 fixture
# --------------------------------------------------------------------------

@pytest.fixture
def sample_specs():
    """一份最小可用的素材 spec 结构（不落盘），覆盖 ambience 与 sfx 各一条"""
    return {
        "ambience": {
            "test_rain": {
                "description": "测试用雨声",
                "prompt": "test rain ambience",
                "negative_prompt": "music",
                "duration_sec": 3.0,
                "seed": 1,
            },
        },
        "sfx": {
            "test_clash": {
                "description": "测试用碰撞声",
                "prompt": "test clash sound",
                "negative_prompt": "music",
                "duration_sec": 1.0,
                "seed": 2,
            },
        },
    }


@pytest.fixture
def tmp_assets_dir(tmp_project_dir):
    """隔离的素材输出目录（不落在真实 data/assets/ 下）"""
    return os.path.join(tmp_project_dir, "assets")


@pytest.fixture
def mock_backend_map():
    mock = asset_gen.MockAudioGenBackend()
    return {"ambience": mock, "sfx": mock}


class _AlwaysFailBackend:
    """模拟真实后端整批任务全部失败，用于测试逐条回退 Mock 的行为"""

    name = "fake_real_backend"

    def generate_batch(self, jobs: list) -> dict:
        return {job["id"]: False for job in jobs}


class _PartialFailBackend:
    """模拟真实后端部分任务失败：只让指定的 fail_ids 失败，其余交给一个真实生成器代跑"""

    name = "fake_real_backend"

    def __init__(self, fail_ids):
        self.fail_ids = set(fail_ids)
        self._delegate = asset_gen.MockAudioGenBackend()

    def generate_batch(self, jobs: list) -> dict:
        results = {}
        for job in jobs:
            if job["id"] in self.fail_ids:
                results[job["id"]] = False
            else:
                results[job["id"]] = self._delegate.generate(
                    job["kind"], job["prompt"], job["duration_sec"], job["seed"],
                    job["out"], job.get("sample_rate", 44100),
                )
        return results


# --------------------------------------------------------------------------
# Spec 加载
# --------------------------------------------------------------------------

class TestLoadAssetSpecs:
    def test_load_real_spec_file(self):
        """真实项目的 asset_specs.yaml 能被正确加载"""
        specs = asset_gen.load_asset_specs()
        assert "ambience" in specs and "sfx" in specs
        assert "rain_heavy" in specs["ambience"]
        assert "sword_clash" in specs["sfx"]
        assert specs["ambience"]["rain_heavy"]["prompt"]

    def test_load_missing_file_returns_empty_structure(self):
        specs = asset_gen.load_asset_specs("/nonexistent/asset_specs.yaml")
        assert specs == {"ambience": {}, "sfx": {}}

    def test_load_skips_entry_without_prompt(self, tmp_path):
        spec_file = tmp_path / "specs.yaml"
        spec_file.write_text(
            "ambience:\n"
            "  bad_entry:\n"
            "    description: 缺少 prompt\n"
            "  good_entry:\n"
            "    prompt: valid prompt\n"
            "    duration_sec: 10\n"
            "    seed: 1\n",
            encoding="utf-8",
        )
        specs = asset_gen.load_asset_specs(str(spec_file))
        assert "bad_entry" not in specs["ambience"]
        assert "good_entry" in specs["ambience"]

    def test_load_fills_defaults(self, tmp_path):
        spec_file = tmp_path / "specs.yaml"
        spec_file.write_text(
            "sfx:\n  minimal:\n    prompt: just a prompt\n",
            encoding="utf-8",
        )
        specs = asset_gen.load_asset_specs(str(spec_file))
        entry = specs["sfx"]["minimal"]
        assert entry["description"] == ""
        assert entry["negative_prompt"] == ""
        assert entry["duration_sec"] == 30.0
        assert entry["seed"] == 0


class TestComputeSpecHash:
    def test_hash_deterministic(self, sample_specs):
        spec = sample_specs["ambience"]["test_rain"]
        assert asset_gen.compute_spec_hash(spec) == asset_gen.compute_spec_hash(dict(spec))

    def test_hash_changes_on_prompt_change(self, sample_specs):
        spec = sample_specs["ambience"]["test_rain"]
        other = dict(spec, prompt="a different prompt")
        assert asset_gen.compute_spec_hash(spec) != asset_gen.compute_spec_hash(other)

    def test_hash_changes_on_seed_change(self, sample_specs):
        spec = sample_specs["sfx"]["test_clash"]
        other = dict(spec, seed=999)
        assert asset_gen.compute_spec_hash(spec) != asset_gen.compute_spec_hash(other)

    def test_hash_unaffected_by_description_change(self, sample_specs):
        """description 是纯说明文字，改动不应使增量缓存失效"""
        spec = sample_specs["sfx"]["test_clash"]
        other = dict(spec, description="完全不同的说明文字")
        assert asset_gen.compute_spec_hash(spec) == asset_gen.compute_spec_hash(other)


class TestGetAssetDescriptions:
    def test_maps_ambience_to_bgm_key(self, sample_specs):
        descriptions = asset_gen.get_asset_descriptions(sample_specs)
        assert descriptions["bgm"]["test_rain"] == "测试用雨声"
        assert descriptions["sfx"]["test_clash"] == "测试用碰撞声"

    def test_missing_name_returns_empty_dict_lookup(self, sample_specs):
        descriptions = asset_gen.get_asset_descriptions(sample_specs)
        assert descriptions["bgm"].get("unknown_name") is None


# --------------------------------------------------------------------------
# MockAudioGenBackend
# --------------------------------------------------------------------------

class TestMockAudioGenBackend:
    def test_is_available_always_true(self):
        assert asset_gen.MockAudioGenBackend().is_available() is True

    def test_generate_creates_valid_wav(self, tmp_path):
        out_path = str(tmp_path / "out.wav")
        ok = asset_gen.MockAudioGenBackend().generate("sfx", "prompt", 1.0, 42, out_path, sample_rate=24000)
        assert ok is True
        assert os.path.exists(out_path)
        with wave.open(out_path, "r") as wf:
            assert wf.getnchannels() == 1
            assert wf.getframerate() == 24000

    def test_generate_deterministic_with_seed(self, tmp_path):
        p1 = str(tmp_path / "a.wav")
        p2 = str(tmp_path / "b.wav")
        backend = asset_gen.MockAudioGenBackend()
        backend.generate("ambience", "prompt", 1.0, 7, p1)
        backend.generate("ambience", "prompt", 1.0, 7, p2)
        assert open(p1, "rb").read() == open(p2, "rb").read()

    def test_generate_batch(self, tmp_path):
        jobs = [
            {"id": "a", "kind": "sfx", "prompt": "x", "duration_sec": 1.0, "seed": 1, "out": str(tmp_path / "a.wav")},
            {"id": "b", "kind": "ambience", "prompt": "y", "duration_sec": 1.0, "seed": 2, "out": str(tmp_path / "b.wav")},
        ]
        results = asset_gen.MockAudioGenBackend().generate_batch(jobs)
        assert results == {"a": True, "b": True}
        assert os.path.exists(str(tmp_path / "a.wav"))
        assert os.path.exists(str(tmp_path / "b.wav"))


# --------------------------------------------------------------------------
# 后处理
# --------------------------------------------------------------------------

class TestPostProcessAmbience:
    def test_output_sample_rate_and_channels(self):
        seg = AudioSegment.silent(duration=3000, frame_rate=44100).set_channels(2)
        result = asset_gen._post_process_ambience(seg, target_sample_rate=24000, crossfade_ms=500, target_dbfs=-20.0)
        assert result.frame_rate == 24000
        assert result.channels == 1

    def test_output_shorter_by_crossfade_duration(self):
        seg = AudioSegment.silent(duration=3000, frame_rate=24000)
        crossfade_ms = 500
        result = asset_gen._post_process_ambience(seg, target_sample_rate=24000, crossfade_ms=crossfade_ms, target_dbfs=-20.0)
        assert abs(len(result) - (3000 - crossfade_ms)) <= 2

    def test_handles_short_input_without_crashing(self):
        seg = AudioSegment.silent(duration=10, frame_rate=24000)
        result = asset_gen._post_process_ambience(seg, target_sample_rate=24000, crossfade_ms=2000, target_dbfs=-20.0)
        assert isinstance(result, AudioSegment)


class TestPostProcessSfx:
    def test_output_sample_rate_and_channels(self):
        seg = AudioSegment.silent(duration=1000, frame_rate=44100).set_channels(2)
        result = asset_gen._post_process_sfx(seg, target_sample_rate=24000, target_dbfs=-6.0)
        assert result.frame_rate == 24000
        assert result.channels == 1

    def test_handles_empty_segment(self):
        seg = AudioSegment.silent(duration=0, frame_rate=24000)
        result = asset_gen._post_process_sfx(seg, target_sample_rate=24000, target_dbfs=-6.0)
        assert isinstance(result, AudioSegment)


class TestEqualPowerCrossfade:
    def test_result_length_matches_duration(self):
        a = AudioSegment.silent(duration=500, frame_rate=24000)
        b = AudioSegment.silent(duration=500, frame_rate=24000)
        result = asset_gen._equal_power_crossfade(a, b, 500)
        assert abs(len(result) - 500) <= 2

    def test_zero_duration_falls_back_to_overlay(self):
        a = AudioSegment.silent(duration=500, frame_rate=24000)
        b = AudioSegment.silent(duration=500, frame_rate=24000)
        result = asset_gen._equal_power_crossfade(a, b, 0)
        assert isinstance(result, AudioSegment)


# --------------------------------------------------------------------------
# generate_assets 增量生成主流程
# --------------------------------------------------------------------------

class TestGenerateAssets:
    def test_generates_all_specs(self, sample_specs, tmp_assets_dir, mock_backend_map):
        summary = asset_gen.generate_assets(specs=sample_specs, assets_dir=tmp_assets_dir, backend_map=mock_backend_map)
        assert summary["skipped"] == []
        assert summary["error"] == []
        assert set(summary["fallback"]) == {"test_rain", "test_clash"}  # mock 后端一律标记 fallback
        assert os.path.exists(os.path.join(tmp_assets_dir, "ambience", "test_rain.wav"))
        assert os.path.exists(os.path.join(tmp_assets_dir, "sfx", "test_clash.wav"))
        assert os.path.exists(os.path.join(tmp_assets_dir, "ambience", "test_rain.meta.json"))

    def test_meta_records_provenance(self, sample_specs, tmp_assets_dir, mock_backend_map):
        asset_gen.generate_assets(specs=sample_specs, assets_dir=tmp_assets_dir, backend_map=mock_backend_map)
        meta_path = os.path.join(tmp_assets_dir, "sfx", "test_clash.meta.json")
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        assert meta["engine"] == "mock"
        assert meta["used_fallback"] is True
        assert meta["spec_hash"] == asset_gen.compute_spec_hash(sample_specs["sfx"]["test_clash"])

    def test_second_run_skips_everything(self, sample_specs, tmp_assets_dir, mock_backend_map):
        asset_gen.generate_assets(specs=sample_specs, assets_dir=tmp_assets_dir, backend_map=mock_backend_map)
        summary_2 = asset_gen.generate_assets(specs=sample_specs, assets_dir=tmp_assets_dir, backend_map=mock_backend_map)
        assert set(summary_2["skipped"]) == {"test_rain", "test_clash"}
        assert summary_2["generated"] == []
        assert summary_2["fallback"] == []

    def test_force_regenerates_everything(self, sample_specs, tmp_assets_dir, mock_backend_map):
        asset_gen.generate_assets(specs=sample_specs, assets_dir=tmp_assets_dir, backend_map=mock_backend_map)
        summary_2 = asset_gen.generate_assets(
            specs=sample_specs, assets_dir=tmp_assets_dir, backend_map=mock_backend_map, force=True
        )
        assert summary_2["skipped"] == []
        assert set(summary_2["fallback"]) == {"test_rain", "test_clash"}

    def test_changed_spec_only_invalidates_that_entry(self, sample_specs, tmp_assets_dir, mock_backend_map):
        asset_gen.generate_assets(specs=sample_specs, assets_dir=tmp_assets_dir, backend_map=mock_backend_map)

        changed_specs = json.loads(json.dumps(sample_specs))
        changed_specs["sfx"]["test_clash"]["prompt"] = "a totally different prompt"

        summary_2 = asset_gen.generate_assets(specs=changed_specs, assets_dir=tmp_assets_dir, backend_map=mock_backend_map)
        assert summary_2["skipped"] == ["test_rain"]
        assert summary_2["fallback"] == ["test_clash"]

    def test_only_filter_restricts_to_named_assets(self, sample_specs, tmp_assets_dir, mock_backend_map):
        summary = asset_gen.generate_assets(
            specs=sample_specs, assets_dir=tmp_assets_dir, backend_map=mock_backend_map, only={"test_rain"}
        )
        assert set(summary["fallback"]) == {"test_rain"}
        assert not os.path.exists(os.path.join(tmp_assets_dir, "sfx", "test_clash.wav"))

    def test_kind_filter_restricts_to_one_kind(self, sample_specs, tmp_assets_dir, mock_backend_map):
        summary = asset_gen.generate_assets(
            specs=sample_specs, assets_dir=tmp_assets_dir, backend_map=mock_backend_map, kinds=["sfx"]
        )
        assert set(summary["fallback"]) == {"test_clash"}
        assert not os.path.exists(os.path.join(tmp_assets_dir, "ambience", "test_rain.wav"))

    def test_real_backend_success_records_real_engine(self, sample_specs, tmp_assets_dir):
        real_like = asset_gen.MockAudioGenBackend()
        real_like.name = "fake_real_backend"  # 让它伪装成"真实"后端但复用 Mock 的生成逻辑
        summary = asset_gen.generate_assets(
            specs=sample_specs, assets_dir=tmp_assets_dir,
            backend_map={"ambience": real_like, "sfx": real_like},
        )
        assert set(summary["generated"]) == {"test_rain", "test_clash"}
        meta_path = os.path.join(tmp_assets_dir, "ambience", "test_rain.meta.json")
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        assert meta["engine"] == "fake_real_backend"
        assert meta["used_fallback"] is False

    def test_real_backend_total_failure_falls_back_to_mock(self, sample_specs, tmp_assets_dir):
        fail_backend = _AlwaysFailBackend()
        summary = asset_gen.generate_assets(
            specs=sample_specs, assets_dir=tmp_assets_dir,
            backend_map={"ambience": fail_backend, "sfx": fail_backend},
        )
        assert set(summary["fallback"]) == {"test_rain", "test_clash"}
        assert os.path.exists(os.path.join(tmp_assets_dir, "sfx", "test_clash.wav"))
        with open(os.path.join(tmp_assets_dir, "sfx", "test_clash.meta.json"), "r", encoding="utf-8") as f:
            meta = json.load(f)
        assert meta["engine"] == "mock"
        assert meta["used_fallback"] is True

    def test_real_backend_partial_failure_only_flags_failed_job(self, sample_specs, tmp_assets_dir):
        partial = _PartialFailBackend(fail_ids={"test_clash"})
        summary = asset_gen.generate_assets(
            specs=sample_specs, assets_dir=tmp_assets_dir,
            backend_map={"ambience": partial, "sfx": partial},
        )
        assert "test_rain" in summary["generated"]
        assert "test_clash" in summary["fallback"]

    def test_empty_specs_produce_no_output(self, tmp_assets_dir, mock_backend_map):
        summary = asset_gen.generate_assets(specs={"ambience": {}, "sfx": {}}, assets_dir=tmp_assets_dir, backend_map=mock_backend_map)
        assert summary == {"generated": [], "fallback": [], "skipped": [], "error": []}


# --------------------------------------------------------------------------
# 状态查看
# --------------------------------------------------------------------------

class TestAssetStatus:
    def test_missing_before_generation(self, sample_specs, tmp_assets_dir):
        rows = asset_gen.get_asset_status_list(specs=sample_specs, assets_dir=tmp_assets_dir)
        assert all(row["status"] == "MISSING" for row in rows)

    def test_ok_after_generation(self, sample_specs, tmp_assets_dir, mock_backend_map):
        asset_gen.generate_assets(specs=sample_specs, assets_dir=tmp_assets_dir, backend_map=mock_backend_map)
        rows = asset_gen.get_asset_status_list(specs=sample_specs, assets_dir=tmp_assets_dir)
        assert all(row["status"] == "OK(占位/Mock)" for row in rows)

    def test_stale_after_spec_change(self, sample_specs, tmp_assets_dir, mock_backend_map):
        asset_gen.generate_assets(specs=sample_specs, assets_dir=tmp_assets_dir, backend_map=mock_backend_map)
        changed_specs = json.loads(json.dumps(sample_specs))
        changed_specs["ambience"]["test_rain"]["prompt"] = "different"
        rows = asset_gen.get_asset_status_list(specs=changed_specs, assets_dir=tmp_assets_dir)
        rain_row = next(r for r in rows if r["name"] == "test_rain")
        assert rain_row["status"] == "STALE(spec已变更)"

    def test_print_asset_status_table_does_not_crash(self, sample_specs, tmp_assets_dir, capsys):
        asset_gen.print_asset_status_table(specs=sample_specs, assets_dir=tmp_assets_dir)
        captured = capsys.readouterr()
        assert "test_rain" in captured.out
        assert "test_clash" in captured.out

    def test_print_asset_status_table_empty_specs(self, tmp_assets_dir, capsys):
        asset_gen.print_asset_status_table(specs={"ambience": {}, "sfx": {}}, assets_dir=tmp_assets_dir)
        captured = capsys.readouterr()
        assert "未定义任何素材" in captured.out
