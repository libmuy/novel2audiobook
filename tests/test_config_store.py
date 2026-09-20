"""src/config_store.py：global_config.yaml 的 round-trip 写入"""
import os
import shutil

import pytest

from src import config_store

REAL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "global_config.yaml")


def _comment_lines(text):
    return [l for l in text.splitlines() if l.strip().startswith("#")]


def _inline_comments(text):
    return [l.split("#", 1)[1].strip() for l in text.splitlines() if "#" in l and not l.strip().startswith("#")]


class TestRoundTrip:
    def test_untouched_roundtrip_of_real_config_is_byte_identical(self, tmp_path):
        """含 `drm_card: null  # 注释`：ruamel 默认会把 null 写成空值，这里必须保持原样"""
        p = tmp_path / "c.yaml"
        shutil.copy(REAL, p)
        config_store.round_trip_update(str(p), {})
        assert p.read_text(encoding="utf-8") == open(REAL, encoding="utf-8").read()

    def test_update_changes_only_that_value_and_keeps_every_comment(self, tmp_path):
        p = tmp_path / "c.yaml"
        shutil.copy(REAL, p)
        before = p.read_text(encoding="utf-8")
        config_store.round_trip_update(str(p), {"mixing.bitrate": "256k", "server.cpu_workers": 5})
        after = p.read_text(encoding="utf-8")

        assert _comment_lines(before) == _comment_lines(after)
        assert _inline_comments(before) == _inline_comments(after)
        changed = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
        assert len(changed) == 2 and len(before.splitlines()) == len(after.splitlines())
        assert "256k" in after and "cpu_workers: 5" in after

    def test_missing_sections_and_file_are_created(self, tmp_path):
        p = tmp_path / "sub" / "new.yaml"
        config_store.round_trip_update(str(p), {"mixing.voice_only": False, "a.b.c": 1})
        import yaml
        assert yaml.safe_load(p.read_text(encoding="utf-8")) == {"mixing": {"voice_only": False}, "a": {"b": {"c": 1}}}

    def test_no_tmp_file_left_behind(self, tmp_path):
        p = tmp_path / "c.yaml"
        p.write_text("a: 1\n", encoding="utf-8")
        config_store.round_trip_update(str(p), {"a": 2})
        assert os.listdir(tmp_path) == ["c.yaml"]
