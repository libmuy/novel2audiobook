"""
系统资源监控模块（零依赖，不装 psutil）

读取 sysfs 和 /proc 获取 GPU/CPU/内存使用情况。
"""
import os
import re

from src.utils import load_global_config


def detect_drm_card() -> str:
    """扫 /sys/class/drm/card*/device/，返回第一张有 gpu_busy_percent 文件的卡路径。
    找不到返回 None（比如跑在没有独显的机器上），上层要能优雅降级。"""
    drm_base = "/sys/class/drm"
    if not os.path.isdir(drm_base):
        return None
    for entry in sorted(os.listdir(drm_base)):
        if not entry.startswith("card"):
            continue
        busy_path = os.path.join(drm_base, entry, "device", "gpu_busy_percent")
        if os.path.exists(busy_path):
            return os.path.join(drm_base, entry)
    return None


def read_gpu(card_path: str = None) -> dict:
    """读三个 sysfs 文件，返回
    {"busy_percent": int, "vram_used_bytes": int, "vram_total_bytes": int}。
    读不到任何一个就返回 None。"""
    if card_path is None:
        card_path = detect_drm_card()
    if card_path is None:
        return None

    device_dir = os.path.join(card_path, "device")
    busy_path = os.path.join(device_dir, "gpu_busy_percent")
    vram_used_path = os.path.join(device_dir, "mem_info_vram_used")
    vram_total_path = os.path.join(device_dir, "mem_info_vram_total")

    try:
        with open(busy_path, "r") as f:
            busy_percent = int(f.read().strip())
        with open(vram_used_path, "r") as f:
            vram_used = int(f.read().strip())
        with open(vram_total_path, "r") as f:
            vram_total = int(f.read().strip())
    except (OSError, ValueError):
        return None

    return {
        "busy_percent": busy_percent,
        "vram_used_bytes": vram_used,
        "vram_total_bytes": vram_total,
    }


def read_cpu_percent() -> float:
    """读 /proc/stat 的第一行，与模块内保存的上一次快照做差分。
    第一次调用没有基准，返回 0.0。"""
    if not os.path.exists("/proc/stat"):
        return 0.0

    with open("/proc/stat", "r") as f:
        line = f.readline()

    parts = line.split()
    if len(parts) < 5:
        return 0.0

    # cpu  user nice system idle iowait irq softirq steal
    values = [int(p) for p in parts[1:9]]
    total = sum(values)
    idle = values[3]

    prev = getattr(read_cpu_percent, "_prev", None)
    read_cpu_percent._prev = (total, idle)

    if prev is None:
        return 0.0

    prev_total, prev_idle = prev
    delta_total = total - prev_total
    delta_idle = idle - prev_idle

    if delta_total == 0:
        return 0.0

    return round((1.0 - delta_idle / delta_total) * 100.0, 1)


def read_memory() -> dict:
    """读 /proc/meminfo，返回 {"used_bytes": ..., "total_bytes": ...}。
    used = MemTotal - MemAvailable（不是减 MemFree，那个数字会吓人）。"""
    if not os.path.exists("/proc/meminfo"):
        return None

    mem_total = None
    mem_available = None

    with open("/proc/meminfo", "r") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                mem_total = int(line.split()[1]) * 1024  # kB -> bytes
            elif line.startswith("MemAvailable:"):
                mem_available = int(line.split()[1]) * 1024  # kB -> bytes
            if mem_total is not None and mem_available is not None:
                break

    if mem_total is None or mem_available is None:
        return None

    return {
        "used_bytes": mem_total - mem_available,
        "total_bytes": mem_total,
    }


def snapshot(config: dict = None) -> dict:
    """汇总上面三个，返回给前端资源条的完整结构。"""
    if config is None:
        config = load_global_config()

    drm_card = config.get("server", {}).get("drm_card")
    if not drm_card:
        drm_card = detect_drm_card()

    gpu = read_gpu(drm_card)
    cpu_percent = read_cpu_percent()
    memory = read_memory()

    return {
        "gpu": gpu,
        "cpu_percent": cpu_percent,
        "memory": memory,
    }
