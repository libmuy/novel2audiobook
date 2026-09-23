"""IndexTTSBackend 的子进程行为。此前整个后端零覆盖——用 FakePopen，不需要真实 GPU。"""
import json
import os
import signal
import subprocess
import threading
import time

import pytest

from src import tts_engine


class FakePopen:
    """按 behavior 行为的假子进程。communicate 时按 script 写 result.json 和输出 wav。"""
    instances = []

    def __init__(self, cmd, **kwargs):
        self.cmd = cmd
        self.kwargs = kwargs
        self.pid = 5150
        self.returncode = None
        FakePopen.instances.append(self)

    # 由测试设置
    behavior = "ok"          # ok | timeout | crash
    ok_ids = ()              # 报告成功并写出 wav 的 job
    partial_ids = ()         # 写了一半（截断）wav 但没报告成功的 job
    stderr = b""

    started = None           # behavior == "block" 时由测试设置的 threading.Event

    def communicate(self, timeout=None):
        cls = type(self)
        if cls.behavior == "timeout" and self.returncode is None:
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        if cls.behavior == "block":
            # 模拟推理进行中：先写出被截断的输出，再一直阻塞到被 killpg 杀掉
            for j in json.load(open(self.cmd[self.cmd.index("--jobs-file") + 1], encoding="utf-8")):
                if j["id"] in cls.partial_ids:
                    with open(j["out"], "wb") as f:
                        f.write(b"RI")
            if cls.started:
                cls.started.set()
            end = time.time() + 10
            while self.returncode is None and time.time() < end:
                time.sleep(0.005)
            return b"", b""
        jobs = json.load(open(self.cmd[self.cmd.index("--jobs-file") + 1], encoding="utf-8"))
        results = {}
        for j in jobs:
            if j["id"] in cls.ok_ids:
                with open(j["out"], "wb") as f:
                    f.write(b"RIFF" + b"\0" * 100)
                results[j["id"]] = {"ok": True}
            elif j["id"] in cls.partial_ids:
                with open(j["out"], "wb") as f:
                    f.write(b"RI")  # 被杀时写到一半
                results[j["id"]] = {"ok": False, "error": "killed"}
        if cls.behavior != "crash" or results:
            with open(self.cmd[self.cmd.index("--result-file") + 1], "w", encoding="utf-8") as f:
                json.dump(results, f)
        self.returncode = 1 if cls.behavior == "crash" else 0
        return b"", cls.stderr

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


@pytest.fixture
def env(monkeypatch, tmp_path):
    """假 Popen + 记录事件顺序的假仲裁器 + 假 killpg"""
    events = []
    FakePopen.instances = []
    FakePopen.behavior, FakePopen.ok_ids, FakePopen.partial_ids, FakePopen.stderr = "ok", (), (), b""

    class RecordingArbiter:
        def __init__(self, config=None):
            pass

        def __enter__(self):
            events.append("arbiter_enter")

        def __exit__(self, *exc):
            events.append("arbiter_exit")
            return False

    import src.tools.gpu_arbiter as ga
    monkeypatch.setattr(ga, "LlmSuspendedForTts", RecordingArbiter)
    monkeypatch.setattr(tts_engine.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(os, "getpgid", lambda pid: 900000 + pid)

    def fake_killpg(pgid, sig):
        events.append(("killpg", sig))
        for p in FakePopen.instances:
            p.returncode = -sig

    monkeypatch.setattr(os, "killpg", fake_killpg)

    def make_jobs(*ids):
        return [{"id": i, "text": i, "emotion": "neutral", "sample_rate": 24000,
                 "role_cfg": {"reference_audio": str(tmp_path / "ref.wav"), "speed": 1.0},
                 "out": str(tmp_path / f"{i}.wav")} for i in ids]

    backend = tts_engine.IndexTTSBackend({"tts": {"index_tts": {"timeout_sec": 3}}})
    return backend, events, make_jobs


class TestTimeoutOrdering:
    def test_child_is_killed_inside_the_arbiter_block_before_llama_restarts(self, env):
        """回归：以前 TimeoutExpired 先冒出 with 块、llama-server 被重启，之后才杀子进程——
        重启时子进程还占着显存。杀进程必须在仲裁器退出之前。"""
        backend, events, make_jobs = env
        FakePopen.behavior = "timeout"
        results = backend.synthesize_batch(make_jobs("a", "b"))

        assert events == ["arbiter_enter", ("killpg", signal.SIGTERM), "arbiter_exit"]
        assert results == {"a": False, "b": False}

    def test_started_in_its_own_session_so_killpg_is_safe(self, env):
        backend, _, make_jobs = env
        backend.synthesize_batch(make_jobs("a"))
        assert FakePopen.instances[0].kwargs["start_new_session"] is True

    def test_current_proc_is_cleared_afterwards(self, env):
        backend, _, make_jobs = env
        FakePopen.behavior = "timeout"
        backend.synthesize_batch(make_jobs("a"))
        assert backend._current_proc is None


class TestTerminateCurrent:
    def test_safe_on_a_fresh_instance(self):
        """回归：_current_proc 只在 synthesize_batch 里赋值，新实例调用会 AttributeError"""
        tts_engine.IndexTTSBackend({}).terminate_current()

    def test_class_default_exists(self):
        assert tts_engine.IndexTTSBackend._current_proc is None


class TestBatchResults:
    def test_stderr_is_decoded_not_crashing_on_bad_bytes(self, env):
        backend, _, make_jobs = env
        FakePopen.behavior, FakePopen.stderr = "crash", b"\xff\xfe boom"
        assert backend.synthesize_batch(make_jobs("a")) == {"a": False}

    def test_reports_success_per_job(self, env):
        backend, _, make_jobs = env
        FakePopen.ok_ids = ("a",)
        res = backend.synthesize_batch(make_jobs("a", "b"))
        assert res == {"a": True, "b": False}

    def test_truncated_outputs_of_unfinished_jobs_are_deleted(self, env, tmp_path):
        """回归：被杀时子进程在合法 md5 名下留下截断 wav，缓存探针只看「文件存在」，
        之后 wave.open 读它就炸、整章每次都失败。没报告成功的 job 的输出必须删掉。"""
        backend, _, make_jobs = env
        FakePopen.ok_ids, FakePopen.partial_ids = ("a",), ("b",)
        res = backend.synthesize_batch(make_jobs("a", "b"))
        assert res == {"a": True, "b": False}
        assert (tmp_path / "a.wav").exists()
        assert not (tmp_path / "b.wav").exists()

    def test_delete_unfinished_outputs_helper(self, tmp_path):
        keep, drop, absent = tmp_path / "k.wav", tmp_path / "d.wav", tmp_path / "x.wav"
        keep.write_bytes(b"ok")
        drop.write_bytes(b"RI")
        jobs = [{"id": "k", "out": str(keep)}, {"id": "d", "out": str(drop)}, {"id": "x", "out": str(absent)}]
        tts_engine._delete_unfinished_outputs(jobs, {"k": True, "d": False})
        assert keep.exists() and not drop.exists()  # 缺失的文件不抛异常


class TestCancel:
    """任务取消 → CancelScope 里登记的钩子杀掉子进程组，且发生在仲裁器退出之前"""

    def _run_in_thread(self, backend, jobs, scope):
        from src import cancel_scope
        out = {}

        def target():
            cancel_scope.activate(scope)
            try:
                out["results"] = backend.synthesize_batch(jobs)
            finally:
                cancel_scope.deactivate()

        t = threading.Thread(target=target)
        t.start()
        return t, out

    def test_cancel_while_the_child_runs_kills_it_inside_the_arbiter_block(self, env, tmp_path):
        from src.cancel_scope import CancelScope
        backend, events, make_jobs = env
        FakePopen.behavior, FakePopen.partial_ids = "block", ("a",)
        FakePopen.started = threading.Event()
        scope, jobs = CancelScope(), make_jobs("a", "b")
        t, out = self._run_in_thread(backend, jobs, scope)
        assert FakePopen.started.wait(3)
        scope.cancel()
        t.join(5)
        assert not t.is_alive(), "取消之后 synthesize_batch 没有返回（子进程没被杀）"

        assert events == ["arbiter_enter", ("killpg", signal.SIGTERM), "arbiter_exit"]
        assert out["results"] == {"a": False, "b": False}
        assert not os.path.exists(jobs[0]["out"])  # 被杀时写到一半的截断 wav 已删，不会占着合法 md5 名

    def test_scope_cancelled_before_popen_still_kills_the_child(self, env):
        """取消落在任务进入 RUNNING 与 Popen 之间：登记被拒，后端就地杀掉刚起的子进程"""
        from src.cancel_scope import CancelScope
        backend, events, make_jobs = env
        FakePopen.behavior = "block"
        FakePopen.started = None
        scope = CancelScope()
        scope.cancel()
        t, out = self._run_in_thread(backend, make_jobs("a"), scope)
        t.join(5)
        assert not t.is_alive()
        assert events == ["arbiter_enter", ("killpg", signal.SIGTERM), "arbiter_exit"]
        assert out["results"] == {"a": False}

    def test_hook_is_gone_after_a_normal_run(self, env):
        """正常跑完后再取消不能去碰早已结束的子进程（pid 可能已被复用）"""
        from src import cancel_scope
        from src.cancel_scope import CancelScope
        backend, events, make_jobs = env
        scope = CancelScope()
        cancel_scope.activate(scope)
        try:
            backend.synthesize_batch(make_jobs("a"))
        finally:
            cancel_scope.deactivate()
        events.clear()
        scope.cancel()
        assert events == []
