"""SubprocessAudioGenBackend 的子进程行为（此前用阻塞的 subprocess.run：不分进程组、超时只打日志泄漏子进程）"""
import json
import os
import signal
import subprocess

import pytest

from src import asset_gen


class FakePopen:
    instances = []
    behavior = "ok"
    stderr = b""

    def __init__(self, cmd, **kwargs):
        self.cmd, self.kwargs = cmd, kwargs
        self.pid = 6161
        self.returncode = None
        FakePopen.instances.append(self)

    def communicate(self, timeout=None):
        if FakePopen.behavior == "timeout" and self.returncode is None:
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        jobs = json.load(open(self.cmd[self.cmd.index("--jobs-file") + 1], encoding="utf-8"))
        results = {}
        for j in jobs:
            open(j["out"], "wb").write(b"RIFFfake")
            results[j["id"]] = {"ok": True}
        json.dump(results, open(self.cmd[self.cmd.index("--result-file") + 1], "w", encoding="utf-8"))
        self.returncode = 1 if FakePopen.behavior == "crash" else 0
        return b"", FakePopen.stderr

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


@pytest.fixture
def env(monkeypatch):
    events = []
    FakePopen.instances, FakePopen.behavior, FakePopen.stderr = [], "ok", b""

    class RecordingArbiter:
        def __init__(self, config=None):
            pass

        def __enter__(self):
            events.append("arbiter_enter")

        def __exit__(self, *exc):
            events.append("arbiter_exit")
            return False

    import tools.gpu_arbiter as ga
    monkeypatch.setattr(ga, "LlmSuspendedForGpu", RecordingArbiter)
    monkeypatch.setattr(asset_gen.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(os, "getpgid", lambda pid: 900000 + pid)

    def fake_killpg(pgid, sig):
        events.append(("killpg", sig))
        for p in FakePopen.instances:
            p.returncode = -sig

    monkeypatch.setattr(os, "killpg", fake_killpg)
    backend = asset_gen.TangoFluxBackend({"asset_gen": {"tangoflux": {"python_bin": "x", "infer_script": "y",
                                                                      "checkpoints_dir": "z", "timeout_sec": 3}}})
    return backend, events


def _jobs(tmp_path):
    return [{"id": "a", "kind": "sfx", "prompt": "p", "negative_prompt": "", "duration_sec": 1.0,
             "seed": 1, "sample_rate": 44100, "out": str(tmp_path / "a.wav")}]


class TestGenerateBatch:
    def test_runs_in_its_own_session_so_killpg_can_never_hit_the_server(self, env, tmp_path):
        backend, _ = env
        backend.generate_batch(_jobs(tmp_path))
        assert FakePopen.instances[0].kwargs["start_new_session"] is True

    def test_timeout_terminates_the_child_inside_the_arbiter_block(self, env, tmp_path):
        """回归：以前 subprocess.run 超时只打日志，子进程泄漏、继续占着显存"""
        backend, events = env
        FakePopen.behavior = "timeout"
        backend.generate_batch(_jobs(tmp_path))
        assert events == ["arbiter_enter", ("killpg", signal.SIGTERM), "arbiter_exit"]

    def test_success_returns_per_job_results(self, env, tmp_path):
        backend, events = env
        assert backend.generate_batch(_jobs(tmp_path)) == {"a": True}
        assert events == ["arbiter_enter", "arbiter_exit"]

    def test_stderr_bytes_are_decoded_safely(self, env, tmp_path):
        """Popen 给的是 bytes，原来的 [-1000:] 切片假设 text=True"""
        backend, _ = env
        FakePopen.behavior, FakePopen.stderr = "crash", b"\xff\xfe boom"
        backend.generate_batch(_jobs(tmp_path))  # 不抛异常

    def test_current_proc_cleared_and_terminate_safe_when_idle(self, env, tmp_path):
        backend, _ = env
        backend.terminate_current()  # 没在跑：不抛异常
        backend.generate_batch(_jobs(tmp_path))
        assert backend._current_proc is None
