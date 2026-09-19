"""src/killable_proc.py：进程组终止（TTS 与素材生成共用）"""
import os
import signal
import subprocess

import pytest

from src import killable_proc


class FakeProc:
    def __init__(self, pid=4242, exits_on=(signal.SIGTERM,)):
        self.pid = pid
        self.returncode = None
        self.exits_on = set(exits_on)
        self.calls = []

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode

    def terminate(self):
        self.calls.append("terminate")
        if signal.SIGTERM in self.exits_on:
            self.returncode = -signal.SIGTERM

    def kill(self):
        self.calls.append("kill")
        self.returncode = -signal.SIGKILL


@pytest.fixture
def grouped(monkeypatch):
    """子进程在独立进程组里（start_new_session=True 的正常情况）；记录 killpg 调用"""
    sent = []
    holder = {}
    monkeypatch.setattr(os, "getpgid", lambda pid: 900000 + pid)  # 明显不等于本进程的进程组

    def fake_killpg(pgid, sig):
        sent.append((pgid, sig))
        proc = holder["proc"]
        if sig in proc.exits_on or sig == signal.SIGKILL:
            proc.returncode = -sig

    monkeypatch.setattr(os, "killpg", fake_killpg)
    return sent, holder


class TestTerminateProcessGroup:
    def test_sigterm_the_group_then_reaps(self, grouped):
        sent, holder = grouped
        proc = holder["proc"] = FakeProc()
        killable_proc.terminate_process_group(proc, grace_sec=0.01)
        assert sent == [(900000 + 4242, signal.SIGTERM)]
        assert proc.returncode == -signal.SIGTERM

    def test_escalates_to_sigkill_when_sigterm_is_ignored(self, grouped):
        sent, holder = grouped
        proc = holder["proc"] = FakeProc(exits_on=())  # 忽略 SIGTERM
        killable_proc.terminate_process_group(proc, grace_sec=0.01)
        assert [sig for _, sig in sent] == [signal.SIGTERM, signal.SIGKILL]
        assert proc.returncode == -signal.SIGKILL  # 最后 wait() 收了尸

    def test_already_exited_process_is_left_alone(self, grouped):
        sent, holder = grouped
        proc = holder["proc"] = FakeProc()
        proc.returncode = 0
        killable_proc.terminate_process_group(proc)
        assert sent == []

    def test_none_is_a_noop(self):
        killable_proc.terminate_process_group(None)

    def test_never_killpg_when_child_shares_our_process_group(self, monkeypatch):
        """最危险的错误：子进程没用 start_new_session，killpg 会连 API 服务器自己一起杀。
        必须退化成只终止这一个进程。"""
        killed_groups = []
        monkeypatch.setattr(os, "getpgid", lambda pid: os.getpgrp())  # 跟服务器同组
        monkeypatch.setattr(os, "killpg", lambda pgid, sig: killed_groups.append((pgid, sig)))
        proc = FakeProc()
        assert killable_proc.in_own_process_group(proc) is False
        killable_proc.terminate_process_group(proc, grace_sec=0.01)
        assert killed_groups == []
        assert proc.calls == ["terminate"]

    def test_shared_group_escalation_uses_kill_not_killpg(self, monkeypatch):
        killed_groups = []
        monkeypatch.setattr(os, "getpgid", lambda pid: os.getpgrp())
        monkeypatch.setattr(os, "killpg", lambda pgid, sig: killed_groups.append((pgid, sig)))
        proc = FakeProc(exits_on=())
        killable_proc.terminate_process_group(proc, grace_sec=0.01)
        assert killed_groups == [] and proc.calls == ["terminate", "kill"]

    def test_vanished_process_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(os, "getpgid", lambda pid: (_ for _ in ()).throw(ProcessLookupError()))
        proc = FakeProc()
        assert killable_proc.in_own_process_group(proc) is False
        killable_proc.terminate_process_group(proc, grace_sec=0.01)  # 不抛异常
