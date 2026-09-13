"""
测试 tools/gpu_arbiter.py 里为常驻 TTS 服务新增的显式 owner 模型
（pidfile 探测 / 换手耗时历史 / plan_swap 描述，计划 003）。

全部隔离在 tmp_path 下进行，不读写真实机器上的 .cache/ 状态；
不发真实网络请求（is_server_up 的调用点在 get_current_owner 测试里被
monkeypatch 掉），也不 fork/kill 真实进程（is_pid_alive 只用当前测试进程
自己的 PID 和一个几乎不可能存在的超大 PID）。
"""
import json
import os
import pytest
from tools import gpu_arbiter


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """把 gpu_arbiter 的运行时状态文件路径重定向到 tmp_path"""
    state_dir = str(tmp_path / "tts_daemon")
    monkeypatch.setattr(gpu_arbiter, "TTS_DAEMON_STATE_DIR", state_dir)
    monkeypatch.setattr(gpu_arbiter, "TTS_DAEMON_STATE_PATH", os.path.join(state_dir, "state.json"))
    monkeypatch.setattr(gpu_arbiter, "TTS_DAEMON_SOCKET_PATH", os.path.join(state_dir, "daemon.sock"))
    monkeypatch.setattr(gpu_arbiter, "SWAP_HISTORY_PATH", str(tmp_path / "gpu_arbiter" / "swap_history.json"))
    monkeypatch.setattr(gpu_arbiter, "LLM_SUSPENDED_PATH", str(tmp_path / "gpu_arbiter" / "llm_suspended.json"))
    return tmp_path


class TestIsPidAlive:
    def test_current_process_is_alive(self):
        assert gpu_arbiter.is_pid_alive(os.getpid()) is True

    def test_bogus_pid_is_not_alive(self):
        assert gpu_arbiter.is_pid_alive(99999999) is False


class TestTtsDaemonState:
    def test_no_state_file_means_not_running(self, isolated_state):
        assert gpu_arbiter.read_tts_daemon_state() is None
        assert gpu_arbiter.is_tts_daemon_running() is False

    def test_state_file_with_alive_pid_means_running(self, isolated_state):
        os.makedirs(gpu_arbiter.TTS_DAEMON_STATE_DIR, exist_ok=True)
        with open(gpu_arbiter.TTS_DAEMON_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "socket_path": "x"}, f)
        assert gpu_arbiter.is_tts_daemon_running() is True

    def test_state_file_with_dead_pid_means_not_running(self, isolated_state):
        os.makedirs(gpu_arbiter.TTS_DAEMON_STATE_DIR, exist_ok=True)
        with open(gpu_arbiter.TTS_DAEMON_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"pid": 99999999, "socket_path": "x"}, f)
        assert gpu_arbiter.is_tts_daemon_running() is False

    def test_corrupted_state_file_treated_as_absent(self, isolated_state):
        os.makedirs(gpu_arbiter.TTS_DAEMON_STATE_DIR, exist_ok=True)
        with open(gpu_arbiter.TTS_DAEMON_STATE_PATH, "w", encoding="utf-8") as f:
            f.write("not valid json {{{")
        assert gpu_arbiter.read_tts_daemon_state() is None
        assert gpu_arbiter.is_tts_daemon_running() is False


class TestSwapHistory:
    def test_no_history_returns_none(self, isolated_state):
        assert gpu_arbiter.get_expected_swap_seconds("llm->tts") is None

    def test_record_and_read_back(self, isolated_state):
        gpu_arbiter.record_swap_seconds("llm->tts", 47.3)
        assert gpu_arbiter.get_expected_swap_seconds("llm->tts") == 47.3

    def test_record_overwrites_previous_value(self, isolated_state):
        gpu_arbiter.record_swap_seconds("llm_stop", 5.0)
        gpu_arbiter.record_swap_seconds("llm_stop", 8.2)
        assert gpu_arbiter.get_expected_swap_seconds("llm_stop") == 8.2

    def test_multiple_keys_coexist(self, isolated_state):
        gpu_arbiter.record_swap_seconds("llm_stop", 5.0)
        gpu_arbiter.record_swap_seconds("llm_start", 12.0)
        assert gpu_arbiter.get_expected_swap_seconds("llm_stop") == 5.0
        assert gpu_arbiter.get_expected_swap_seconds("llm_start") == 12.0


class TestGetCurrentOwner:
    def test_tts_daemon_running_takes_priority(self, isolated_state, monkeypatch):
        monkeypatch.setattr(gpu_arbiter, "is_tts_daemon_running", lambda: True)
        monkeypatch.setattr(gpu_arbiter, "is_server_up", lambda *a, **k: True)  # 即使 llm 也"在跑"
        assert gpu_arbiter.get_current_owner({"llm": {}}) == gpu_arbiter.OWNER_TTS

    def test_llm_owner_when_only_llm_up(self, isolated_state, monkeypatch):
        monkeypatch.setattr(gpu_arbiter, "is_tts_daemon_running", lambda: False)
        monkeypatch.setattr(gpu_arbiter, "is_server_up", lambda *a, **k: True)
        assert gpu_arbiter.get_current_owner({"llm": {}}) == gpu_arbiter.OWNER_LLM

    def test_none_when_neither_up(self, isolated_state, monkeypatch):
        monkeypatch.setattr(gpu_arbiter, "is_tts_daemon_running", lambda: False)
        monkeypatch.setattr(gpu_arbiter, "is_server_up", lambda *a, **k: False)
        assert gpu_arbiter.get_current_owner({"llm": {}}) is None


class TestPlanSwap:
    def test_invalid_target_raises(self, isolated_state):
        with pytest.raises(ValueError):
            gpu_arbiter.plan_swap("assets")

    def test_noop_when_already_target(self, isolated_state, monkeypatch):
        monkeypatch.setattr(gpu_arbiter, "get_current_owner", lambda config=None: gpu_arbiter.OWNER_TTS)
        plan = gpu_arbiter.plan_swap(gpu_arbiter.OWNER_TTS)
        assert plan["noop"] is True
        assert plan["steps"] == []
        assert plan["estimated_seconds"] == 0.0

    def test_llm_to_tts_describes_both_steps(self, isolated_state, monkeypatch):
        monkeypatch.setattr(gpu_arbiter, "get_current_owner", lambda config=None: gpu_arbiter.OWNER_LLM)
        plan = gpu_arbiter.plan_swap(gpu_arbiter.OWNER_TTS)
        assert plan["noop"] is False
        assert any("llama-server" in s for s in plan["steps"])
        assert any("常驻" in s for s in plan["steps"])

    def test_none_to_llm_only_describes_start_step(self, isolated_state, monkeypatch):
        monkeypatch.setattr(gpu_arbiter, "get_current_owner", lambda config=None: None)
        plan = gpu_arbiter.plan_swap(gpu_arbiter.OWNER_LLM)
        assert plan["noop"] is False
        assert len(plan["steps"]) == 1
        assert "llama-server" in plan["steps"][0]

    def test_estimated_seconds_uses_history(self, isolated_state, monkeypatch):
        gpu_arbiter.record_swap_seconds("llm->tts", 47.3)
        monkeypatch.setattr(gpu_arbiter, "get_current_owner", lambda config=None: gpu_arbiter.OWNER_LLM)
        plan = gpu_arbiter.plan_swap(gpu_arbiter.OWNER_TTS)
        assert plan["estimated_seconds"] == 47.3

    def test_estimated_seconds_none_without_history(self, isolated_state, monkeypatch):
        monkeypatch.setattr(gpu_arbiter, "get_current_owner", lambda config=None: gpu_arbiter.OWNER_LLM)
        plan = gpu_arbiter.plan_swap(gpu_arbiter.OWNER_TTS)
        assert plan["estimated_seconds"] is None


class TestLlmSuspendedForGpuMarker:
    """测试 LlmSuspendedForGpu 的孤儿恢复标记文件写入/删除"""

    def test_enter_creates_marker_file(self, isolated_state, monkeypatch):
        monkeypatch.setattr(gpu_arbiter, "is_server_up", lambda *a, **k: True)
        monkeypatch.setattr(gpu_arbiter, "stop_llama_server", lambda *a, **k: True)
        monkeypatch.setattr(gpu_arbiter, "record_swap_seconds", lambda *a, **k: None)

        ctx = gpu_arbiter.LlmSuspendedForGpu({"llm": {"serve_port": 8080}})
        with ctx:
            assert os.path.exists(gpu_arbiter.LLM_SUSPENDED_PATH)
            with open(gpu_arbiter.LLM_SUSPENDED_PATH, "r") as f:
                marker = json.load(f)
            assert marker["pid"] == os.getpid()
            assert "model_registry_name" in marker

    def test_exit_deletes_marker_file(self, isolated_state, monkeypatch):
        monkeypatch.setattr(gpu_arbiter, "is_server_up", lambda *a, **k: True)
        monkeypatch.setattr(gpu_arbiter, "stop_llama_server", lambda *a, **k: True)
        monkeypatch.setattr(gpu_arbiter, "record_swap_seconds", lambda *a, **k: None)
        monkeypatch.setattr(gpu_arbiter, "start_llama_server", lambda *a, **k: True)

        ctx = gpu_arbiter.LlmSuspendedForGpu({"llm": {"serve_port": 8080, "serve_model_registry_name": "test"}})
        with ctx:
            pass
        assert not os.path.exists(gpu_arbiter.LLM_SUSPENDED_PATH)

    def test_exit_deletes_marker_on_exception(self, isolated_state, monkeypatch):
        monkeypatch.setattr(gpu_arbiter, "is_server_up", lambda *a, **k: True)
        monkeypatch.setattr(gpu_arbiter, "stop_llama_server", lambda *a, **k: True)
        monkeypatch.setattr(gpu_arbiter, "record_swap_seconds", lambda *a, **k: None)
        monkeypatch.setattr(gpu_arbiter, "start_llama_server", lambda *a, **k: True)

        ctx = gpu_arbiter.LlmSuspendedForGpu({"llm": {"serve_port": 8080, "serve_model_registry_name": "test"}})
        with pytest.raises(RuntimeError):
            with ctx:
                raise RuntimeError("test exception")
        assert not os.path.exists(gpu_arbiter.LLM_SUSPENDED_PATH)

    def test_no_marker_when_server_not_running(self, isolated_state, monkeypatch):
        monkeypatch.setattr(gpu_arbiter, "is_server_up", lambda *a, **k: False)

        ctx = gpu_arbiter.LlmSuspendedForGpu({"llm": {"serve_port": 8080}})
        with ctx:
            assert not os.path.exists(gpu_arbiter.LLM_SUSPENDED_PATH)


class TestRecoverOrphanedSuspension:
    """测试孤儿恢复功能"""

    def test_no_marker_file(self, isolated_state):
        result = gpu_arbiter.recover_orphaned_suspension()
        assert result["recovered"] is False
        assert "无孤儿标记" in result["reason"]

    def test_pid_alive_no_recovery(self, isolated_state, monkeypatch):
        os.makedirs(os.path.dirname(gpu_arbiter.LLM_SUSPENDED_PATH), exist_ok=True)
        with open(gpu_arbiter.LLM_SUSPENDED_PATH, "w") as f:
            json.dump({"pid": os.getpid(), "since": "2026-01-01", "model_registry_name": "test"}, f)

        result = gpu_arbiter.recover_orphaned_suspension()
        assert result["recovered"] is False
        assert "仍在运行" in result["reason"]
        assert os.path.exists(gpu_arbiter.LLM_SUSPENDED_PATH)

    def test_pid_dead_recovers(self, isolated_state, monkeypatch):
        os.makedirs(os.path.dirname(gpu_arbiter.LLM_SUSPENDED_PATH), exist_ok=True)
        with open(gpu_arbiter.LLM_SUSPENDED_PATH, "w") as f:
            json.dump({"pid": 99999999, "since": "2026-01-01", "model_registry_name": "test"}, f)

        start_called = []
        monkeypatch.setattr(gpu_arbiter, "start_llama_server", lambda *a, **k: start_called.append(True) or True)
        monkeypatch.setattr(gpu_arbiter, "is_server_up", lambda *a, **k: True)

        result = gpu_arbiter.recover_orphaned_suspension({"llm": {"serve_port": 8080}})
        assert result["recovered"] is True
        assert len(start_called) == 1
        assert not os.path.exists(gpu_arbiter.LLM_SUSPENDED_PATH)

    def test_pid_dead_marker_deleted_even_on_failure(self, isolated_state, monkeypatch):
        os.makedirs(os.path.dirname(gpu_arbiter.LLM_SUSPENDED_PATH), exist_ok=True)
        with open(gpu_arbiter.LLM_SUSPENDED_PATH, "w") as f:
            json.dump({"pid": 99999999, "since": "2026-01-01", "model_registry_name": "test"}, f)

        monkeypatch.setattr(gpu_arbiter, "start_llama_server", lambda *a, **k: False)

        result = gpu_arbiter.recover_orphaned_suspension({"llm": {"serve_port": 8080}})
        assert result["recovered"] is False
        assert "失败" in result["reason"]
        assert not os.path.exists(gpu_arbiter.LLM_SUSPENDED_PATH)

    def test_corrupted_marker_file(self, isolated_state):
        os.makedirs(os.path.dirname(gpu_arbiter.LLM_SUSPENDED_PATH), exist_ok=True)
        with open(gpu_arbiter.LLM_SUSPENDED_PATH, "w") as f:
            f.write("not valid json {{{")

        result = gpu_arbiter.recover_orphaned_suspension()
        assert result["recovered"] is False
        assert "损坏" in result["reason"]
