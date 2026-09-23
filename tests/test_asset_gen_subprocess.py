"""SubprocessAudioGenBackend 的子进程行为（此前用阻塞的 subprocess.run：不分进程组、超时只打日志泄漏子进程）"""
import json
import os
import signal
import subprocess
import threading
import time

import pytest

from src import asset_gen


class FakePopen:
    instances = []
    behavior = "ok"
    stderr = b""
    started = None

    def __init__(self, cmd, **kwargs):
        self.cmd, self.kwargs = cmd, kwargs
        self.pid = 6161
        self.returncode = None
        FakePopen.instances.append(self)

    def communicate(self, timeout=None):
        if FakePopen.behavior == "timeout" and self.returncode is None:
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        if FakePopen.behavior == "block":  # 推理进行中：阻塞到被 killpg 杀掉
            if FakePopen.started:
                FakePopen.started.set()
            end = time.time() + 10
            while self.returncode is None and time.time() < end:
                time.sleep(0.005)
            return b"", b""
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

    import src.tools.gpu_arbiter as ga
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


class TestCancel:
    def _run(self, backend, tmp_path, scope):
        from src import cancel_scope
        out = {}

        def target():
            cancel_scope.activate(scope)
            try:
                out["results"] = backend.generate_batch(_jobs(tmp_path))
            finally:
                cancel_scope.deactivate()

        t = threading.Thread(target=target)
        t.start()
        return t, out

    def test_cancel_while_the_child_runs_kills_it_inside_the_arbiter_block(self, env, tmp_path):
        from src.cancel_scope import CancelScope
        backend, events = env
        FakePopen.behavior, FakePopen.started = "block", threading.Event()
        scope = CancelScope()
        t, out = self._run(backend, tmp_path, scope)
        assert FakePopen.started.wait(3)
        scope.cancel()
        t.join(5)
        assert not t.is_alive()
        assert events == ["arbiter_enter", ("killpg", signal.SIGTERM), "arbiter_exit"]
        assert out["results"] == {"a": False}

    def test_scope_cancelled_before_popen_still_kills_the_child(self, env, tmp_path):
        from src.cancel_scope import CancelScope
        backend, events = env
        FakePopen.behavior = "block"
        scope = CancelScope()
        scope.cancel()
        t, out = self._run(backend, tmp_path, scope)
        t.join(5)
        assert not t.is_alive()
        assert events == ["arbiter_enter", ("killpg", signal.SIGTERM), "arbiter_exit"]


class TestGenerateAssetsCancel:
    """取消后不能给未完成的条目铺占位噪音并写入真实 spec_hash（那会把「取消」变成「成功」）"""

    def test_cancel_after_the_batch_writes_no_placeholder_and_no_meta(self, tmp_path):
        from src.pipeline_errors import TaskCancelled
        specs = {"ambience": {}, "sfx": {"a": {"description": "", "prompt": "p", "negative_prompt": "",
                                               "duration_sec": 1.0, "seed": 1}}}

        class KilledBackend:
            name = "fake_real_engine"

            def generate_batch(self, jobs):
                return {j["id"]: False for j in jobs}  # 子进程被杀：一条都没完成

        cancelled = {"flag": False}

        def should_cancel():
            return cancelled["flag"]

        backend = KilledBackend()
        orig = backend.generate_batch

        def generate_and_cancel(jobs):
            cancelled["flag"] = True  # 取消发生在子进程运行期间
            return orig(jobs)

        backend.generate_batch = generate_and_cancel
        with pytest.raises(TaskCancelled):
            asset_gen.generate_assets(specs=specs, assets_dir=str(tmp_path), backend_map={"sfx": backend},
                                      should_cancel=should_cancel)
        assert not (tmp_path / "sfx" / "a.wav").exists()
        assert not (tmp_path / "sfx" / "a.meta.json").exists()
