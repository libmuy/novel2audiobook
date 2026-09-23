"""素材引擎选择（注册表 + 配置别名）与引擎漂移检测。这个工厂之前完全没有测试。"""
import json
import logging
import os

import pytest

from src.pipeline import asset_gen
from src.runtime import preflight

SPECS = {
    "ambience": {"rain": {"description": "", "prompt": "rain", "negative_prompt": "", "duration_sec": 1.0, "seed": 1}},
    "sfx": {"hit": {"description": "", "prompt": "hit", "negative_prompt": "", "duration_sec": 1.0, "seed": 2}},
}


class _RealLike(asset_gen.MockAudioGenBackend):
    """伪装成真实引擎（name 不是 mock），生成逻辑复用 Mock"""

    def __init__(self, name):
        super().__init__()
        self.name = name


def _write_asset(assets_dir, kind, name, spec, **meta_overrides):
    d = os.path.join(assets_dir, kind)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{name}.wav"), "wb") as f:
        f.write(b"RIFFfake")
    meta = {"name": name, "kind": kind, "spec_hash": asset_gen.compute_spec_hash(spec),
            "engine": "audioldm", "used_fallback": False}
    meta.update(meta_overrides)
    meta = {k: v for k, v in meta.items() if v is not None}
    with open(os.path.join(d, f"{name}.meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)


class TestResolveEngineId:
    @pytest.mark.parametrize("raw,expected", [
        ("audioldm", "audioldm"), ("tangoflux", "tangoflux"), ("ace_step", "ace_step"), ("mock", "mock"),
        # 现有 global_config.yaml 里的展示串——零迁移
        ("AudioLDM-S-Full-v2", "audioldm"), ("TangoFlux", "tangoflux"), ("ACE-Step 1.5", "ace_step"),
        ("  MOCK ", "mock"), ("Ace_Step", "ace_step"),
    ])
    def test_known(self, raw, expected):
        assert asset_gen.resolve_engine_id(raw) == expected

    @pytest.mark.parametrize("raw", ["", None, "gpt-audio", "stable-audio"])
    def test_unknown_is_none(self, raw):
        assert asset_gen.resolve_engine_id(raw) is None

    def test_registry_keys_match_class_names(self):
        for engine_id, cls in asset_gen.ASSET_BACKENDS.items():
            assert cls.name == engine_id


class TestExpectedEngine:
    def test_shipped_config_display_strings(self):
        cfg = {"asset_gen": {"ambience_engine": "AudioLDM-S-Full-v2", "sfx_engine": "TangoFlux"}}
        assert asset_gen.expected_engine("ambience", cfg) == "audioldm"
        assert asset_gen.expected_engine("sfx", cfg) == "tangoflux"

    def test_config_actually_selects_the_engine(self):
        """以前配置值只进日志，实际引擎硬编码 —— 现在改配置就换引擎"""
        cfg = {"asset_gen": {"ambience_engine": "ace_step", "sfx_engine": "audioldm"}}
        assert asset_gen.expected_engine("ambience", cfg) == "ace_step"
        assert asset_gen.expected_engine("sfx", cfg) == "audioldm"

    def test_missing_config_falls_back_to_previous_hardcoded_defaults(self):
        assert asset_gen.expected_engine("ambience", {}) == "audioldm"
        assert asset_gen.expected_engine("sfx", {}) == "tangoflux"

    def test_unknown_value_warns_and_uses_default(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert asset_gen.expected_engine("sfx", {"asset_gen": {"sfx_engine": "nonsense"}}) == "tangoflux"
        assert "nonsense" in caplog.text


class TestBuildBackend:
    def test_selected_engine_class_is_built_when_available(self, monkeypatch):
        monkeypatch.setattr(asset_gen.SubprocessAudioGenBackend, "is_available", lambda self: True)
        cfg = {"asset_gen": {"ambience_engine": "ace_step"}}
        assert isinstance(asset_gen.build_asset_gen_backend("ambience", cfg), asset_gen.AceStepBackend)
        assert isinstance(asset_gen.build_asset_gen_backend("sfx", {}), asset_gen.TangoFluxBackend)

    def test_unavailable_env_falls_back_to_mock(self):
        assert isinstance(asset_gen.build_asset_gen_backend("ambience", {}), asset_gen.MockAudioGenBackend)

    def test_explicit_mock_config_does_not_warn(self, caplog):
        with caplog.at_level(logging.WARNING):
            b = asset_gen.build_asset_gen_backend("sfx", {"asset_gen": {"sfx_engine": "mock"}})
        assert isinstance(b, asset_gen.MockAudioGenBackend) and "回退" not in caplog.text

    def test_engine_argument_overrides_config(self, monkeypatch):
        """CLI --backend 用它：覆盖配置里选的引擎"""
        monkeypatch.setattr(asset_gen.SubprocessAudioGenBackend, "is_available", lambda self: True)
        b = asset_gen.build_asset_gen_backend("ambience", {"asset_gen": {"ambience_engine": "audioldm"}},
                                              engine="tangoflux")
        assert isinstance(b, asset_gen.TangoFluxBackend)

    def test_unknown_explicit_engine_raises(self):
        with pytest.raises(ValueError, match="未知"):
            asset_gen.build_asset_gen_backend("sfx", {}, engine="nope")

    def test_mock_backend_accepts_config_arg_for_uniform_construction(self):
        assert asset_gen.MockAudioGenBackend({"x": 1}).name == "mock"


class TestEngineDrift:
    CFG = {"asset_gen": {"ambience_engine": "AudioLDM-S-Full-v2", "sfx_engine": "TangoFlux"}}

    def _status(self, tmp_path, config=None):
        rows = asset_gen.get_asset_status_list(SPECS, str(tmp_path), config or self.CFG)
        return {r["name"]: r for r in rows}

    def test_foreign_engine_is_flagged_stale(self, tmp_path):
        _write_asset(str(tmp_path), "ambience", "rain", SPECS["ambience"]["rain"], engine="ace_step")
        row = self._status(tmp_path)["rain"]
        assert row["status"] == "STALE(引擎已变更)"
        assert row["engine"] == "ace_step" and row["expected_engine"] == "audioldm"

    def test_matching_engine_is_ok(self, tmp_path):
        _write_asset(str(tmp_path), "ambience", "rain", SPECS["ambience"]["rain"], engine="audioldm")
        assert self._status(tmp_path)["rain"]["status"] == "OK"

    def test_placeholder_keeps_its_own_status_not_misread_as_drift(self, tmp_path):
        _write_asset(str(tmp_path), "sfx", "hit", SPECS["sfx"]["hit"], engine="mock", used_fallback=True)
        assert self._status(tmp_path)["hit"]["status"] == "OK(占位/Mock)"

    def test_old_meta_without_engine_field_is_not_drift(self, tmp_path):
        _write_asset(str(tmp_path), "sfx", "hit", SPECS["sfx"]["hit"], engine=None)
        assert self._status(tmp_path)["hit"]["status"] == "OK"

    def test_explicitly_configured_mock_never_reports_drift(self, tmp_path):
        """没人想把真实素材换成占位音"""
        _write_asset(str(tmp_path), "sfx", "hit", SPECS["sfx"]["hit"], engine="tangoflux")
        cfg = {"asset_gen": {"sfx_engine": "mock"}}
        assert self._status(tmp_path, cfg)["hit"]["status"] == "OK"

    def test_spec_change_takes_priority_over_engine_drift(self, tmp_path):
        _write_asset(str(tmp_path), "ambience", "rain", SPECS["ambience"]["rain"], engine="ace_step",
                     spec_hash="old")
        assert self._status(tmp_path)["rain"]["status"] == "STALE(spec已变更)"

    def test_missing_wav_reports_missing_with_expected_engine(self, tmp_path):
        row = self._status(tmp_path)["hit"]
        assert row["status"] == "MISSING" and row["expected_engine"] == "tangoflux"


class TestGenerationHonoursDrift:
    """引擎漂移不进 spec_hash：否则这次改动一落地整库素材就全部失效"""

    def test_engine_is_not_part_of_the_spec_hash(self):
        spec = SPECS["ambience"]["rain"]
        assert asset_gen.compute_spec_hash({**spec, "engine": "ace_step"}) == asset_gen.compute_spec_hash(spec)
        assert asset_gen.compute_spec_hash({**spec, "engine": "audioldm"}) == asset_gen.compute_spec_hash(spec)

    def test_drifted_asset_is_kept_without_force_and_regenerated_with_force(self, tmp_path):
        _write_asset(str(tmp_path), "ambience", "rain", SPECS["ambience"]["rain"], engine="ace_step")
        new = _RealLike("audioldm")
        bm = {"ambience": new, "sfx": new}

        summary = asset_gen.generate_assets(specs=SPECS, assets_dir=str(tmp_path), kinds=["ambience"],
                                            backend_map=bm, config={})
        assert summary["skipped"] == ["rain"] and summary["generated"] == []

        summary = asset_gen.generate_assets(specs=SPECS, assets_dir=str(tmp_path), kinds=["ambience"],
                                            backend_map=bm, config={}, force=True)
        assert summary["generated"] == ["rain"]
        meta = json.load(open(tmp_path / "ambience" / "rain.meta.json", encoding="utf-8"))
        assert meta["engine"] == "audioldm"

    def test_placeholder_is_retried_when_a_real_engine_is_available(self, tmp_path):
        """预检早就承诺「Mock 占位将尝试用真实引擎重新生成」，之前生成这一侧却把它当缓存跳过"""
        _write_asset(str(tmp_path), "sfx", "hit", SPECS["sfx"]["hit"], engine="mock", used_fallback=True)
        real = _RealLike("tangoflux")
        summary = asset_gen.generate_assets(specs=SPECS, assets_dir=str(tmp_path), kinds=["sfx"],
                                            backend_map={"sfx": real}, config={})
        assert summary["generated"] == ["hit"]

    def test_placeholder_stays_cached_when_backend_is_still_mock(self, tmp_path):
        """这次仍然是 Mock：照旧命中缓存，别每次运行都白重铺一遍占位音（自检的缓存回归靠这个）"""
        _write_asset(str(tmp_path), "sfx", "hit", SPECS["sfx"]["hit"], engine="mock", used_fallback=True)
        mock = asset_gen.MockAudioGenBackend()
        summary = asset_gen.generate_assets(specs=SPECS, assets_dir=str(tmp_path), kinds=["sfx"],
                                            backend_map={"sfx": mock}, config={})
        assert summary["skipped"] == ["hit"]


class TestPreflightHonoursDrift:
    ROWS = [{"name": "rain", "kind": "ambience", "status": "STALE(引擎已变更)"}]

    def _patch(self, monkeypatch):
        monkeypatch.setattr(asset_gen, "get_asset_status_list", lambda *a, **k: self.ROWS)

    def test_drift_is_skip_without_force_matching_generate_assets(self, monkeypatch):
        self._patch(monkeypatch)
        r = preflight.preflight_assets()
        assert r["summary"] == {"create": 0, "overwrite": 0, "skip": 1}
        assert "强制重新生成" in r["assets"][0]["reason"]

    def test_drift_is_overwrite_with_force(self, monkeypatch):
        self._patch(monkeypatch)
        r = preflight.preflight_assets({"force": True})
        assert r["summary"] == {"create": 0, "overwrite": 1, "skip": 0}
