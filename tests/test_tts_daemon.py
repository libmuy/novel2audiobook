"""
测试 src/pipeline/tts_daemon.py（计划 003：常驻 TTS 服务的进程内句柄）。

全程不启动真实子进程、不发真实 socket 连接、不碰真实 GPU/llama-server：
subprocess.Popen 用一个假对象替身，IndexTTSDaemon._wait_ready/_request 按需
monkeypatch 掉，只验证本模块自己的编排逻辑（该不该停/起 llama-server、
状态文件写了什么、换手耗时记到了哪个 key）。真实的模型加载/socket 协议
分别由 src/tools/inference/precompute_embeddings.py 附带的 CPU-only 冒烟脚本与
--serve 协议冒烟脚本单独验证过。
"""
import json
import os
import pytest
from src.runtime import gpu_arbiter
from src.pipeline import tts_daemon


class FakePopen:
    """subprocess.Popen 的替身：不真的起进程，只记录调用、可控 poll()/kill()"""

    def __init__(self, pid=12345):
        self.pid = pid
        self.killed = False
        self._exited = False

    def poll(self):
        return 0 if self._exited else None

    def kill(self):
        self.killed = True
        self._exited = True


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    state_dir = str(tmp_path / "tts_daemon")
    monkeypatch.setattr(gpu_arbiter, "TTS_DAEMON_STATE_DIR", state_dir)
    monkeypatch.setattr(gpu_arbiter, "TTS_DAEMON_STATE_PATH", os.path.join(state_dir, "state.json"))
    monkeypatch.setattr(gpu_arbiter, "TTS_DAEMON_SOCKET_PATH", os.path.join(state_dir, "daemon.sock"))
    monkeypatch.setattr(gpu_arbiter, "SWAP_HISTORY_PATH", str(tmp_path / "gpu_arbiter" / "swap_history.json"))
    return tmp_path


@pytest.fixture
def available_daemon(tmp_path, isolated_state):
    """构造一个 is_available()==True 的 daemon：venv/repo/checkpoints 路径都存在（占位文件即可）"""
    python_bin = tmp_path / "env" / "bin" / "python"
    python_bin.parent.mkdir(parents=True)
    python_bin.touch()
    infer_script = tmp_path / "indextts_infer.py"
    infer_script.touch()
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    checkpoints_dir = tmp_path / "checkpoints"
    checkpoints_dir.mkdir()

    config = {
        "tts": {"index_tts": {
            "python_bin": str(python_bin), "infer_script": str(infer_script),
            "repo_dir": str(repo_dir), "checkpoints_dir": str(checkpoints_dir),
            "daemon_startup_timeout_sec": 30,
        }},
        "llm": {"api_base": "http://localhost:8080/v1", "serve_port": 8080,
                "serve_model_registry_name": "qwen-test", "server_start_timeout_sec": 60},
    }
    return tts_daemon.IndexTTSDaemon(config)


class TestIsAvailable:
    def test_missing_paths_means_unavailable(self, tmp_path, isolated_state):
        config = {"tts": {"index_tts": {
            "python_bin": str(tmp_path / "no_such_python"),
            "infer_script": str(tmp_path / "no_such_script.py"),
            "repo_dir": str(tmp_path / "no_such_repo"),
            "checkpoints_dir": str(tmp_path / "no_such_checkpoints"),
        }}}
        daemon = tts_daemon.IndexTTSDaemon(config)
        assert daemon.is_available() is False

    def test_all_paths_present_means_available(self, available_daemon):
        assert available_daemon.is_available() is True


class TestIsRunning:
    def test_delegates_to_gpu_arbiter(self, isolated_state, monkeypatch):
        monkeypatch.setattr(gpu_arbiter, "is_tts_daemon_running", lambda: True)
        assert tts_daemon.IndexTTSDaemon().is_running() is True
        monkeypatch.setattr(gpu_arbiter, "is_tts_daemon_running", lambda: False)
        assert tts_daemon.IndexTTSDaemon().is_running() is False


class TestEnsureStarted:
    def test_already_running_is_noop(self, available_daemon, monkeypatch):
        monkeypatch.setattr(gpu_arbiter, "is_tts_daemon_running", lambda: True)
        popen_calls = []
        monkeypatch.setattr(tts_daemon.subprocess, "Popen", lambda *a, **k: popen_calls.append(1) or FakePopen())

        result = available_daemon.ensure_started()

        assert result == {"ok": True, "error": None, "already_running": True}
        assert popen_calls == []  # 已经在跑，不应该起新的子进程

    def test_env_not_ready_returns_error_without_touching_subprocess(self, tmp_path, isolated_state, monkeypatch):
        monkeypatch.setattr(gpu_arbiter, "is_tts_daemon_running", lambda: False)
        daemon = tts_daemon.IndexTTSDaemon({"tts": {"index_tts": {
            "python_bin": str(tmp_path / "missing"), "repo_dir": str(tmp_path / "missing_repo"),
            "checkpoints_dir": str(tmp_path / "missing_ckpt"),
        }}})
        popen_calls = []
        monkeypatch.setattr(tts_daemon.subprocess, "Popen", lambda *a, **k: popen_calls.append(1) or FakePopen())

        result = daemon.ensure_started()

        assert result["ok"] is False
        assert "未就绪" in result["error"]
        assert popen_calls == []

    def test_success_when_llm_was_running_stops_it_and_records_state(self, available_daemon, monkeypatch):
        monkeypatch.setattr(gpu_arbiter, "is_tts_daemon_running", lambda: False)
        monkeypatch.setattr(gpu_arbiter, "is_server_up", lambda *a, **k: True)  # llm 之前在跑
        stop_calls = []
        monkeypatch.setattr(gpu_arbiter, "stop_llama_server", lambda port: stop_calls.append(port))
        start_calls = []
        monkeypatch.setattr(gpu_arbiter, "start_llama_server", lambda *a, **k: start_calls.append(a))
        monkeypatch.setattr(tts_daemon.subprocess, "Popen", lambda *a, **k: FakePopen(pid=999))
        monkeypatch.setattr(tts_daemon.IndexTTSDaemon, "_wait_ready", lambda self, proc, timeout: True)

        result = available_daemon.ensure_started()

        assert result == {"ok": True, "error": None, "already_running": False}
        assert stop_calls == [8080]
        assert start_calls == []  # 成功路径不应该在这里恢复 llm，恢复是 shutdown() 的职责

        state = gpu_arbiter.read_tts_daemon_state()
        assert state["pid"] == 999
        assert state["llm_was_running"] is True
        assert gpu_arbiter.get_expected_swap_seconds("llm->tts") is not None

    def test_success_when_llm_was_not_running_records_none_to_tts(self, available_daemon, monkeypatch):
        monkeypatch.setattr(gpu_arbiter, "is_tts_daemon_running", lambda: False)
        monkeypatch.setattr(gpu_arbiter, "is_server_up", lambda *a, **k: False)  # llm 本来就没在跑
        stop_calls = []
        monkeypatch.setattr(gpu_arbiter, "stop_llama_server", lambda port: stop_calls.append(port))
        monkeypatch.setattr(tts_daemon.subprocess, "Popen", lambda *a, **k: FakePopen(pid=1000))
        monkeypatch.setattr(tts_daemon.IndexTTSDaemon, "_wait_ready", lambda self, proc, timeout: True)

        result = available_daemon.ensure_started()

        assert result["ok"] is True
        assert stop_calls == []  # llm 本来没在跑，不该被"停止"
        state = gpu_arbiter.read_tts_daemon_state()
        assert state["llm_was_running"] is False
        assert gpu_arbiter.get_expected_swap_seconds("none->tts") is not None

    def test_load_timeout_kills_process_and_restores_llm(self, available_daemon, monkeypatch):
        monkeypatch.setattr(gpu_arbiter, "is_tts_daemon_running", lambda: False)
        monkeypatch.setattr(gpu_arbiter, "is_server_up", lambda *a, **k: True)
        monkeypatch.setattr(gpu_arbiter, "stop_llama_server", lambda port: None)
        start_calls = []
        monkeypatch.setattr(gpu_arbiter, "start_llama_server", lambda *a, **k: start_calls.append(a))
        fake_proc = FakePopen(pid=2000)
        monkeypatch.setattr(tts_daemon.subprocess, "Popen", lambda *a, **k: fake_proc)
        monkeypatch.setattr(tts_daemon.IndexTTSDaemon, "_wait_ready", lambda self, proc, timeout: False)

        result = available_daemon.ensure_started()

        assert result["ok"] is False
        assert "未就绪" in result["error"]
        assert fake_proc.killed is True
        assert len(start_calls) == 1  # 失败要把 llm 恢复回去
        assert gpu_arbiter.read_tts_daemon_state() is None  # 不应该留下 state.json


class TestShutdown:
    def test_no_state_file_is_noop(self, isolated_state):
        result = tts_daemon.IndexTTSDaemon().shutdown()
        assert result == {"ok": True, "error": None, "was_running": False}

    def test_shuts_down_and_restores_llm_when_it_was_running(self, isolated_state, monkeypatch):
        os.makedirs(gpu_arbiter.TTS_DAEMON_STATE_DIR, exist_ok=True)
        with open(gpu_arbiter.TTS_DAEMON_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"pid": 12345, "llm_was_running": True}, f)

        monkeypatch.setattr(tts_daemon.IndexTTSDaemon, "_request", lambda self, payload, timeout: {"ok": True})
        monkeypatch.setattr(gpu_arbiter, "is_pid_alive", lambda pid: False)  # 优雅退出后进程已经没了
        start_calls = []
        monkeypatch.setattr(gpu_arbiter, "start_llama_server", lambda *a, **k: start_calls.append(a))

        result = tts_daemon.IndexTTSDaemon().shutdown()

        assert result == {"ok": True, "error": None, "was_running": True}
        assert len(start_calls) == 1
        assert gpu_arbiter.read_tts_daemon_state() is None
        assert not os.path.exists(gpu_arbiter.TTS_DAEMON_SOCKET_PATH)
        assert gpu_arbiter.get_expected_swap_seconds("tts->llm") is not None

    def test_does_not_restore_llm_when_it_was_not_running(self, isolated_state, monkeypatch):
        os.makedirs(gpu_arbiter.TTS_DAEMON_STATE_DIR, exist_ok=True)
        with open(gpu_arbiter.TTS_DAEMON_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"pid": 12345, "llm_was_running": False}, f)

        monkeypatch.setattr(tts_daemon.IndexTTSDaemon, "_request", lambda self, payload, timeout: {"ok": True})
        monkeypatch.setattr(gpu_arbiter, "is_pid_alive", lambda pid: False)
        start_calls = []
        monkeypatch.setattr(gpu_arbiter, "start_llama_server", lambda *a, **k: start_calls.append(a))

        result = tts_daemon.IndexTTSDaemon().shutdown()

        assert result["ok"] is True
        assert start_calls == []

    def test_unreachable_daemon_falls_back_to_kill(self, isolated_state, monkeypatch):
        os.makedirs(gpu_arbiter.TTS_DAEMON_STATE_DIR, exist_ok=True)
        with open(gpu_arbiter.TTS_DAEMON_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"pid": 12345, "llm_was_running": False}, f)

        def _raise(self, payload, timeout):
            raise ConnectionRefusedError("no one home")
        monkeypatch.setattr(tts_daemon.IndexTTSDaemon, "_request", _raise)
        # while 循环里的存活探测先返回 False（不进入等待循环、不真的 sleep 15s），
        # 紧接着 kill 前的最终确认返回 True，从而验证"仍然活着就该 kill"这条分支
        alive_sequence = iter([False, True])
        monkeypatch.setattr(gpu_arbiter, "is_pid_alive", lambda pid: next(alive_sequence, False))
        kill_calls = []
        monkeypatch.setattr(tts_daemon.os, "kill", lambda pid, sig: kill_calls.append((pid, sig)))

        result = tts_daemon.IndexTTSDaemon().shutdown()

        assert result["ok"] is True
        assert kill_calls == [(12345, 9)]


class TestSynthesizeBatch:
    def test_empty_jobs_returns_empty_without_checking_running(self, isolated_state, monkeypatch):
        monkeypatch.setattr(gpu_arbiter, "is_tts_daemon_running", lambda: False)
        assert tts_daemon.IndexTTSDaemon().synthesize_batch([]) == {}

    def test_raises_when_not_running(self, isolated_state, monkeypatch):
        monkeypatch.setattr(gpu_arbiter, "is_tts_daemon_running", lambda: False)
        daemon = tts_daemon.IndexTTSDaemon()
        with pytest.raises(RuntimeError):
            daemon.synthesize_batch([{"id": "x", "text": "t", "role_cfg": {"reference_audio": "/a.wav"},
                                       "emotion": "neutral", "out": "/o.wav", "sample_rate": 24000}])

    def test_translates_jobs_and_maps_results(self, isolated_state, monkeypatch, tmp_path):
        monkeypatch.setattr(gpu_arbiter, "is_tts_daemon_running", lambda: True)
        captured = {}

        def fake_request(self, payload, timeout):
            captured["payload"] = payload
            return {"ok": True, "results": {"seg1": {"ok": True}, "seg2": {"ok": False}}}

        monkeypatch.setattr(tts_daemon.IndexTTSDaemon, "_request", fake_request)

        ref_audio = tmp_path / "reference.wav"
        ref_audio.write_bytes(b"x")
        jobs = [
            {"id": "seg1", "text": "你好", "role_cfg": {"reference_audio": str(ref_audio), "speed": 2.0},
             "emotion": "happy", "out": str(tmp_path / "seg1.wav"), "sample_rate": 24000},
            {"id": "seg2", "text": "再见", "role_cfg": {"reference_audio": str(ref_audio), "speed": 1.0},
             "emotion": "neutral", "out": str(tmp_path / "seg2.wav"), "sample_rate": 24000},
        ]

        result = tts_daemon.IndexTTSDaemon().synthesize_batch(jobs)

        assert result == {"seg1": True, "seg2": False}
        sent_jobs = captured["payload"]["jobs"]
        assert sent_jobs[0]["ref_audio"] == str(ref_audio)
        assert sent_jobs[0]["emo_vector"] != sent_jobs[1]["emo_vector"]  # happy vs neutral 不同
        assert sent_jobs[0]["duration_factor"] != sent_jobs[1]["duration_factor"]  # speed 2.0 vs 1.0 不同

    def test_server_error_raises_with_message(self, isolated_state, monkeypatch, tmp_path):
        monkeypatch.setattr(gpu_arbiter, "is_tts_daemon_running", lambda: True)
        monkeypatch.setattr(
            tts_daemon.IndexTTSDaemon, "_request",
            lambda self, payload, timeout: {"ok": False, "error": "模型没加载好"},
        )
        ref_audio = tmp_path / "reference.wav"
        ref_audio.write_bytes(b"x")
        jobs = [{"id": "seg1", "text": "t", "role_cfg": {"reference_audio": str(ref_audio)},
                 "emotion": "neutral", "out": str(tmp_path / "o.wav"), "sample_rate": 24000}]

        with pytest.raises(RuntimeError, match="模型没加载好"):
            tts_daemon.IndexTTSDaemon().synthesize_batch(jobs)
