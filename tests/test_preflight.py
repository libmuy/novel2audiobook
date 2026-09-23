"""
测试 src/runtime/preflight.py 批量预检模块
"""
import json
import os

import pytest
from src.runtime import preflight


@pytest.fixture
def isolated_library(tmp_path, monkeypatch):
    """创建隔离的 library 目录"""
    library_dir = tmp_path / "library"
    os.makedirs(library_dir)
    monkeypatch.setattr(preflight, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(preflight, "TTS_STATS_PATH", str(tmp_path / "tts_stats.json"))
    return library_dir


def _create_chapter(library_dir, novel_id, ch_id, *, raw=False, draft=False,
                    final=False, timeline=False, mp3=False, status_tag=None):
    """创建章节目录及指定文件"""
    ch_dir = library_dir / novel_id / "chapters" / ch_id
    os.makedirs(ch_dir, exist_ok=True)

    if raw:
        with open(ch_dir / "raw.txt", "w") as f:
            f.write("正文内容")
    if draft:
        with open(ch_dir / "script_draft.json", "w") as f:
            json.dump([], f)
    if final:
        with open(ch_dir / "script_final.json", "w") as f:
            json.dump([], f)
    if timeline:
        with open(ch_dir / "timeline.json", "w") as f:
            json.dump({}, f)
    if mp3:
        output_dir = ch_dir / "output"
        os.makedirs(output_dir)
        with open(output_dir / "chapter_0001.mp3", "wb") as f:
            f.write(b"fake mp3")

    if status_tag:
        with open(ch_dir / ".status.json", "w") as f:
            json.dump({"status": status_tag}, f)


class TestPreflightParse:
    def test_parse_creates_draft(self, isolated_library):
        _create_chapter(isolated_library, "nv1", "ch_0001", raw=True)
        result = preflight.preflight("parse", "nv1", library_dir=str(isolated_library))
        assert result["summary"]["create"] == 1
        assert result["chapters"][0]["action"] == "create"

    def test_parse_skips_without_raw(self, isolated_library):
        _create_chapter(isolated_library, "nv1", "ch_0001")
        result = preflight.preflight("parse", "nv1", library_dir=str(isolated_library))
        assert result["summary"]["skip"] == 1
        assert result["chapters"][0]["action"] == "skip"


class TestPreflightTTS:
    def test_tts_creates_for_final_without_timeline(self, isolated_library):
        _create_chapter(isolated_library, "nv1", "ch_0001", final=True)
        result = preflight.preflight("tts", "nv1", library_dir=str(isolated_library))
        assert result["summary"]["create"] == 1

    def test_tts_overwrites_existing(self, isolated_library):
        _create_chapter(isolated_library, "nv1", "ch_0001", final=True, timeline=True,
                        mp3=True, status_tag="completed")
        result = preflight.preflight("tts", "nv1", library_dir=str(isolated_library))
        assert result["summary"]["overwrite"] == 1

    def test_tts_skips_without_final(self, isolated_library):
        _create_chapter(isolated_library, "nv1", "ch_0001", draft=True)
        result = preflight.preflight("tts", "nv1", library_dir=str(isolated_library))
        assert result["summary"]["skip"] == 1


class TestPreflightMix:
    def test_mix_creates_for_timeline_without_mp3(self, isolated_library):
        _create_chapter(isolated_library, "nv1", "ch_0001", timeline=True)
        result = preflight.preflight("mix", "nv1", library_dir=str(isolated_library))
        assert result["summary"]["create"] == 1

    def test_mix_overwrites_existing(self, isolated_library):
        _create_chapter(isolated_library, "nv1", "ch_0001", timeline=True, mp3=True,
                        status_tag="completed")
        result = preflight.preflight("mix", "nv1", library_dir=str(isolated_library))
        assert result["summary"]["overwrite"] == 1

    def test_mix_overwrites_when_output_is_wav(self, isolated_library):
        """成品是 wav/flac（mixing.output_format）时不该被误判成"还没混过"而给 create"""
        _create_chapter(isolated_library, "nv1", "ch_0001", timeline=True, status_tag="completed")
        out = isolated_library / "nv1" / "chapters" / "ch_0001" / "output"
        os.makedirs(out)
        with open(out / "chapter_0001.wav", "wb") as f:
            f.write(b"RIFF")
        result = preflight.preflight("mix", "nv1", library_dir=str(isolated_library))
        assert result["summary"]["overwrite"] == 1

    def test_mix_skips_without_timeline(self, isolated_library):
        _create_chapter(isolated_library, "nv1", "ch_0001", final=True)
        result = preflight.preflight("mix", "nv1", library_dir=str(isolated_library))
        assert result["summary"]["skip"] == 1


class TestPreflightChapters:
    def test_specific_chapters(self, isolated_library):
        _create_chapter(isolated_library, "nv1", "ch_0001", raw=True)
        _create_chapter(isolated_library, "nv1", "ch_0002", raw=True)
        _create_chapter(isolated_library, "nv1", "ch_0003")

        result = preflight.preflight("parse", "nv1", chapter_ids=["ch_0001"],
                                     library_dir=str(isolated_library))
        assert len(result["chapters"]) == 1
        assert result["chapters"][0]["chapter_id"] == "ch_0001"


class TestTTSStats:
    def test_update_and_load(self, isolated_library):
        preflight.update_tts_stats(2.5)
        stats = preflight._load_tts_stats()
        assert stats["avg_seconds_per_sentence"] == 2.5
        assert stats.get("sample_count", 1) >= 1

    def test_second_sample_is_averaged_not_overwritten(self, isolated_library):
        """回归：首次采样没记 sample_count，第二次采样会直接覆盖第一次"""
        preflight.update_tts_stats(2.0)
        preflight.update_tts_stats(4.0)
        stats = preflight._load_tts_stats()
        assert stats["avg_seconds_per_sentence"] == 3.0
        assert stats["sample_count"] == 2

    def test_estimate_gpu_minutes_with_stats(self, isolated_library):
        preflight.update_tts_stats(2.0)
        minutes = preflight._estimate_gpu_minutes("tts", 1)
        assert minutes is not None
        assert minutes > 0

    def test_estimate_gpu_minutes_without_stats(self, isolated_library):
        minutes = preflight._estimate_gpu_minutes("tts", 1)
        assert minutes is None

    def test_estimate_gpu_minutes_non_tts(self, isolated_library):
        minutes = preflight._estimate_gpu_minutes("parse", 1)
        assert minutes is None


class TestPreflightAssets:
    def _fake_rows(self, monkeypatch, rows):
        from src.pipeline import asset_gen
        monkeypatch.setattr(asset_gen, "get_asset_status_list", lambda *a, **k: rows)

    ROWS = [
        {"name": "a", "kind": "sfx", "status": "MISSING"},
        {"name": "b", "kind": "sfx", "status": "STALE(spec已变更)"},
        {"name": "c", "kind": "ambience", "status": "OK"},
        {"name": "d", "kind": "ambience", "status": "OK(占位/Mock)"},
    ]

    def test_maps_status_to_actions(self, monkeypatch):
        self._fake_rows(monkeypatch, self.ROWS)
        r = preflight.preflight_assets()
        assert r["summary"] == {"create": 1, "overwrite": 2, "skip": 1}
        assert r["chapters"] == []
        assert r["estimated_gpu_minutes"] is None  # 不编造耗时

    def test_force_overwrites_ok(self, monkeypatch):
        self._fake_rows(monkeypatch, self.ROWS)
        r = preflight.preflight_assets({"force": True})
        assert r["summary"] == {"create": 1, "overwrite": 3, "skip": 0}

    def test_kinds_and_only_filters(self, monkeypatch):
        self._fake_rows(monkeypatch, self.ROWS)
        assert preflight.preflight_assets({"kinds": ["sfx"]})["summary"]["create"] == 1
        r = preflight.preflight_assets({"only": ["c"]})
        assert r["summary"] == {"create": 0, "overwrite": 0, "skip": 1}
