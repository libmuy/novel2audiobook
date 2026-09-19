"""真正的取消：CancelScope、kill_on_cancel、TaskQueue 的取消竞态（不需要 GPU）"""
import json
import os
import random
import threading
import time

import pytest

from src import cancel_scope, task_queue
from src.cancel_scope import CancelScope
from src.killable_proc import kill_on_cancel
from src.task_queue import TaskQueue


class TestCancelScope:
    def test_cancel_runs_registered_hooks_once(self):
        scope, calls = CancelScope(), []
        scope.register(lambda: calls.append("a"))
        scope.register(lambda: calls.append("b"))
        scope.cancel()
        scope.cancel()  # 重复取消是空操作，钩子不会再跑
        assert calls == ["a", "b"] and scope.is_cancelled()

    def test_register_after_cancel_is_refused_and_not_stored(self):
        """关掉「注册晚于取消」竞态：返回 False，调用方必须自己收拾"""
        scope, calls = CancelScope(), []
        scope.cancel()
        assert scope.register(lambda: calls.append("late")) is False
        scope.cancel()
        assert calls == []

    def test_hooks_run_outside_the_lock(self):
        """钩子里反过来碰 scope（unregister/register/is_cancelled）不能死锁"""
        scope, done = CancelScope(), threading.Event()

        def hook():
            scope.unregister(hook)
            scope.register(lambda: None)
            scope.is_cancelled()
            done.set()

        scope.register(hook)
        t = threading.Thread(target=scope.cancel)
        t.start()
        assert done.wait(2), "钩子在锁内被调用，反向访问 scope 死锁了"
        t.join(2)

    def test_a_failing_hook_does_not_block_the_rest(self):
        scope, calls = CancelScope(), []
        scope.register(lambda: 1 / 0)
        scope.register(lambda: calls.append("second"))
        scope.cancel()
        assert calls == ["second"]

    def test_unregister_removes_the_hook(self):
        scope, calls = CancelScope(), []
        hook = lambda: calls.append("x")  # noqa: E731
        scope.register(hook)
        scope.unregister(hook)
        scope.unregister(hook)  # 重复注销无害
        scope.cancel()
        assert calls == []

    def test_no_active_scope_is_a_noop(self):
        """CLI/单元测试直接调后端：没有 scope，登记永远成功且什么都不发生"""
        cancel_scope.deactivate()
        assert cancel_scope.register_kill_hook(lambda: None) is True
        cancel_scope.unregister_kill_hook(lambda: None)

    def test_scope_is_thread_local(self):
        cancel_scope.activate(CancelScope())
        seen = []
        t = threading.Thread(target=lambda: seen.append(cancel_scope.current()))
        t.start()
        t.join()
        cancel_scope.deactivate()
        assert seen == [None]


class _Proc:
    """kill_on_cancel 只通过 terminate_process_group 碰进程；这里直接替换它"""
    pid = 1


@pytest.fixture
def killed(monkeypatch):
    calls = []
    import src.killable_proc as kp
    monkeypatch.setattr(kp, "terminate_process_group", lambda proc, grace_sec=5.0: calls.append(proc))
    yield calls
    cancel_scope.deactivate()


class TestKillOnCancel:
    def test_cancel_during_block_terminates_the_process(self, killed):
        scope, proc = CancelScope(), _Proc()
        cancel_scope.activate(scope)
        with kill_on_cancel(proc):
            assert killed == []
            scope.cancel()
        assert killed == [proc]

    def test_scope_already_cancelled_kills_immediately_on_entry(self, killed):
        """取消落在「任务进入 RUNNING」与「Popen 完成」之间的竞态"""
        scope, proc = CancelScope(), _Proc()
        scope.cancel()
        cancel_scope.activate(scope)
        with kill_on_cancel(proc):
            assert killed == [proc]

    def test_hook_is_unregistered_when_the_block_exits(self, killed):
        scope, proc = CancelScope(), _Proc()
        cancel_scope.activate(scope)
        with kill_on_cancel(proc):
            pass
        scope.cancel()  # 子进程早已正常结束：不能再去碰它（pid 可能已被复用）
        assert killed == []

    def test_unregisters_even_if_the_body_raises(self, killed):
        scope, proc = CancelScope(), _Proc()
        cancel_scope.activate(scope)
        with pytest.raises(RuntimeError):
            with kill_on_cancel(proc):
                raise RuntimeError("boom")
        scope.cancel()
        assert killed == []

    def test_without_a_scope_nothing_happens(self, killed):
        cancel_scope.deactivate()
        with kill_on_cancel(_Proc()):
            pass
        assert killed == []


@pytest.fixture
def queue(tmp_path):
    tasks_dir, library_dir = str(tmp_path / "tasks"), str(tmp_path / "library")
    os.makedirs(tasks_dir), os.makedirs(library_dir)
    q = TaskQueue(config={"server": {"cpu_workers": 2}}, tasks_dir=tasks_dir, library_dir=library_dir)
    yield q
    q.stop()


def _wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


class TestQueueCancelRaces:
    def test_hook_registered_during_run_is_called_by_cancel(self, queue, monkeypatch):
        killed, registered = threading.Event(), threading.Event()

        def handler(task, ctx):
            assert cancel_scope.register_kill_hook(killed.set) is True
            registered.set()
            killed.wait(5)

        monkeypatch.setitem(task_queue.TASK_HANDLERS, "parse", handler)
        task = queue.submit("parse", "nv_test")
        queue.start()
        assert registered.wait(3)
        assert queue.cancel(task.id) is True
        assert killed.wait(3), "cancel() 没有调用后端登记的杀进程钩子"
        assert _wait(lambda: queue.get(task.id).state == "cancelled")

    def test_cancel_between_running_and_registration_is_not_lost(self, queue, monkeypatch):
        """回归：令牌以前在 state=RUNNING 之后才创建，落在窗口里的取消返回 True 却什么都没发生。
        现在 scope 先于 RUNNING 创建；后端随后登记钩子时被拒绝，从而自己杀进程。"""
        go, outcome = threading.Event(), {}

        def handler(task, ctx):
            go.wait(5)
            outcome["registered"] = cancel_scope.register_kill_hook(lambda: None)
            outcome["should_cancel"] = ctx.should_cancel()

        monkeypatch.setitem(task_queue.TASK_HANDLERS, "parse", handler)
        task = queue.submit("parse", "nv_test")
        queue.start()
        assert _wait(lambda: queue.get(task.id).state == "running")
        assert queue.cancel(task.id) is True   # handler 还没起子进程
        go.set()
        assert _wait(lambda: queue.get(task.id).state == "cancelled")
        assert outcome == {"registered": False, "should_cancel": True}

    def test_cancel_requested_is_set_and_persisted_without_a_new_state(self, queue, monkeypatch):
        release = threading.Event()
        monkeypatch.setitem(task_queue.TASK_HANDLERS, "parse", lambda t, c: release.wait(5))
        task = queue.submit("parse", "nv_test")
        queue.start()
        assert _wait(lambda: queue.get(task.id).state == "running")
        assert queue.get(task.id).cancel_requested is False
        queue.cancel(task.id)
        t = queue.get(task.id)
        assert t.state == "running" and t.cancel_requested is True  # 仍是 running：状态机不加值
        on_disk = json.load(open(os.path.join(queue.tasks_dir, f"{task.id}.json"), encoding="utf-8"))
        assert on_disk["cancel_requested"] is True
        release.set()
        assert _wait(lambda: queue.get(task.id).state == "cancelled")

    def test_cancel_finished_task_returns_false_and_sets_nothing(self, queue, monkeypatch):
        monkeypatch.setitem(task_queue.TASK_HANDLERS, "parse", lambda t, c: None)
        task = queue.submit("parse", "nv_test")
        queue.start()
        assert _wait(lambda: queue.get(task.id).state == "succeeded")
        assert queue.cancel(task.id) is False
        assert queue.get(task.id).cancel_requested is False

    def test_scope_is_deactivated_after_the_handler(self, queue, monkeypatch):
        monkeypatch.setitem(task_queue.TASK_HANDLERS, "parse", lambda t, c: None)
        task = queue.submit("parse", "nv_test")
        queue.start()
        assert _wait(lambda: queue.get(task.id).state == "succeeded")
        assert queue._cancel_scopes == {}
        # worker 线程上不能残留上一个任务的 scope，否则下一个任务会去注册到已死的 scope
        seen = []
        monkeypatch.setitem(task_queue.TASK_HANDLERS, "parse", lambda t, c: seen.append(cancel_scope.current()))
        t2 = queue.submit("parse", "nv_test")
        assert _wait(lambda: queue.get(t2.id).state == "succeeded")
        assert seen[0] is not None and not seen[0].is_cancelled()

    def test_cancel_storm_at_handler_start_never_loses_the_kill(self, queue, monkeypatch):
        """handler 启动瞬间随机时刻取消（排队期 / 刚置 RUNNING / 登记前 / 登记后）。不变量：
        - cancel() 返回 True ⇔ 任务以 cancelled 收尾；False ⇔ 任务已正常结束
        - handler 运行过且任务被取消 ⇒ 登记被拒（handler 自己收拾）或钩子被调用——
          绝不允许「登记成功、钩子却没人调用」（那就是被丢掉的取消，子进程会白跑到结束）"""
        monkeypatch.setitem(task_queue.TASK_HANDLERS, "parse", lambda t, c: None)
        queue.start()
        rnd = random.Random(7)
        ran_and_cancelled = 0
        for i in range(60):
            started, finished, hooked = threading.Event(), threading.Event(), threading.Event()
            reg = {}

            def handler(task, ctx, started=started, finished=finished, hooked=hooked, reg=reg):
                started.set()
                reg["ok"] = cancel_scope.register_kill_hook(hooked.set)
                if reg["ok"]:
                    hooked.wait(0.3)  # 登记成功：要么有人取消并调用钩子，要么超时放行
                finished.set()

            monkeypatch.setitem(task_queue.TASK_HANDLERS, "parse", handler)
            task = queue.submit("parse", "nv_test")
            time.sleep(rnd.random() * 0.004)
            cancelled = queue.cancel(task.id)
            assert _wait(lambda: queue.get(task.id).state in ("cancelled", "succeeded")), f"第 {i} 轮任务没有收尾"
            state = queue.get(task.id).state
            assert cancelled == (state == "cancelled"), f"第 {i} 轮：cancel()={cancelled} 但最终 {state}"
            if started.is_set():
                assert finished.wait(3), f"第 {i} 轮 handler 没有结束"
                if state == "cancelled":
                    ran_and_cancelled += 1
                    assert reg["ok"] is False or hooked.is_set(), f"第 {i} 轮：登记成功的钩子没有被调用（取消丢了）"
        assert ran_and_cancelled > 0, "随机延迟没有覆盖到「handler 已运行时取消」，这个测试什么都没验证"
