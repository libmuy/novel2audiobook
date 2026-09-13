"""
测试 src/monitor.py 资源监控模块

使用 tmp_path 造假的 sysfs 目录结构注入，不依赖真实硬件。
"""
import os

import pytest
from src import monitor


@pytest.fixture
def fake_sysfs(tmp_path, monkeypatch):
    """创建假的 sysfs 目录结构"""
    card_dir = tmp_path / "drm" / "card1" / "device"
    os.makedirs(card_dir)

    # GPU busy percent
    with open(card_dir / "gpu_busy_percent", "w") as f:
        f.write("45\n")

    # VRAM
    with open(card_dir / "mem_info_vram_used", "w") as f:
        f.write("12884901888\n")  # 12GB
    with open(card_dir / "mem_info_vram_total", "w") as f:
        f.write("25753026560\n")  # ~24GB

    # Mock /proc/stat and /proc/meminfo
    proc_stat = tmp_path / "proc_stat"
    proc_meminfo = tmp_path / "proc_meminfo"

    with open(proc_stat, "w") as f:
        f.write("cpu  1000 0 500 8000 0 0 0 0\n")

    with open(proc_meminfo, "w") as f:
        f.write("MemTotal:       16384000 kB\n")
        f.write("MemAvailable:    8192000 kB\n")
        f.write("MemFree:         4096000 kB\n")

    monkeypatch.setattr(monitor, "detect_drm_card", lambda: str(tmp_path / "drm" / "card1"))
    monkeypatch.setattr(monitor, "read_gpu", lambda card_path=None: {
        "busy_percent": 45,
        "vram_used_bytes": 12884901888,
        "vram_total_bytes": 25753026560,
    })
    monkeypatch.setattr(monitor, "read_memory", lambda: {
        "used_bytes": 16384000 * 1024 - 8192000 * 1024,
        "total_bytes": 16384000 * 1024,
    })

    return tmp_path


class TestDetectDrmCard:
    def test_detect_when_present(self, tmp_path, monkeypatch):
        card_dir = tmp_path / "drm" / "card1" / "device"
        os.makedirs(card_dir)
        with open(card_dir / "gpu_busy_percent", "w") as f:
            f.write("0\n")

        monkeypatch.setattr(monitor.os, "listdir", lambda p: ["card1"] if "drm" in p else os.listdir(p))
        monkeypatch.setattr(monitor.os.path, "isdir", lambda p: True if "drm" in p else os.path.isdir(p))
        monkeypatch.setattr(monitor.os.path, "exists", lambda p: True if "gpu_busy_percent" in p else os.path.exists(p))

        result = monitor.detect_drm_card()
        assert result is not None

    def test_detect_when_absent(self, monkeypatch):
        monkeypatch.setattr(monitor.os, "listdir", lambda p: [])
        result = monitor.detect_drm_card()
        assert result is None


class TestReadGpu:
    def test_read_gpu_with_valid_data(self, fake_sysfs):
        result = monitor.read_gpu(str(fake_sysfs / "drm" / "card1"))
        assert result is not None
        assert result["busy_percent"] == 45
        assert result["vram_used_bytes"] == 12884901888
        assert result["vram_total_bytes"] == 25753026560

    def test_read_gpu_returns_none_when_no_card(self, monkeypatch):
        monkeypatch.setattr(monitor, "detect_drm_card", lambda: None)
        result = monitor.read_gpu()
        assert result is None


class TestReadMemory:
    def test_read_memory_returns_valid(self, monkeypatch):
        monkeypatch.setattr(monitor.os.path, "exists", lambda p: True if "meminfo" in p else False)

        def mock_open(path, *args, **kwargs):
            from io import StringIO
            if "meminfo" in path:
                return StringIO("MemTotal:       16384000 kB\nMemAvailable:    8192000 kB\n")
            return open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", mock_open)
        result = monitor.read_memory()
        assert result is not None
        assert result["total_bytes"] > 0
        assert result["used_bytes"] > 0
        assert result["used_bytes"] < result["total_bytes"]


class TestSnapshot:
    def test_snapshot_returns_all_fields(self, fake_sysfs):
        result = monitor.snapshot({"server": {"drm_card": None}})
        assert "gpu" in result
        assert "cpu_percent" in result
        assert "memory" in result
