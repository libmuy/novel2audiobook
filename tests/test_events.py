"""src/api/routers/events.py：GPU 占用方探测的节流缓存（计划 011）。

完整的 SSE 流是个不会终止的生成器，端到端验证放在 tests/test_web_ui.py
（用真实 uvicorn server + httpx.stream 短超时读几个事件就断开）；这里只测
`_current_owner()` 本身的节流/缓存逻辑，不用真的起服务器。
"""
from src.api.routers import events


def test_probes_on_first_call(monkeypatch):
    monkeypatch.setattr(events, "_owner_cache", {"value": "idle", "at": 0.0})
    calls = []

    def fake_owner():
        calls.append(1)
        return "llm"

    monkeypatch.setattr(events.gpu_arbiter, "get_current_owner", fake_owner)
    assert events._current_owner() == "llm"
    assert len(calls) == 1


def test_reuses_cached_value_within_window(monkeypatch):
    monkeypatch.setattr(events, "_owner_cache", {"value": "tts", "at": __import__("time").time()})
    calls = []
    monkeypatch.setattr(events.gpu_arbiter, "get_current_owner", lambda: calls.append(1) or "llm")
    assert events._current_owner() == "tts"  # 命中缓存，不重新探测
    assert calls == []


def test_reprobes_after_window_expires(monkeypatch):
    stale_at = __import__("time").time() - events._OWNER_PROBE_MIN_INTERVAL_SEC - 1
    monkeypatch.setattr(events, "_owner_cache", {"value": "tts", "at": stale_at})
    monkeypatch.setattr(events.gpu_arbiter, "get_current_owner", lambda: "llm")
    assert events._current_owner() == "llm"


def test_none_owner_maps_to_idle_string(monkeypatch):
    monkeypatch.setattr(events, "_owner_cache", {"value": "tts", "at": 0.0})
    monkeypatch.setattr(events.gpu_arbiter, "get_current_owner", lambda: None)
    assert events._current_owner() == "idle"


def test_probe_exception_falls_back_to_idle(monkeypatch):
    monkeypatch.setattr(events, "_owner_cache", {"value": "tts", "at": 0.0})

    def boom():
        raise RuntimeError("探测失败")

    monkeypatch.setattr(events.gpu_arbiter, "get_current_owner", boom)
    assert events._current_owner() == "idle"
