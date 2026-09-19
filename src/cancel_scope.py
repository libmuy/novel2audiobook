"""
任务取消作用域：让「取消」能真正打断正在跑的推理子进程。

以前 TaskQueue 只给每个运行中的任务一个 threading.Event，后端在管线深处才构造、
起子进程，队列拿不到子进程句柄，取消只能等整批合成跑完（IndexTTS 一次子进程处理
整章，可能几十分钟）。这里换成 CancelScope：

- 队列在任务开始运行**之前**创建 scope，并在执行任务的线程里 activate；
- 后端在 `Popen` 之后**立刻**通过 `register_kill_hook` 注册一个「杀这个子进程」的钩子；
- `TaskQueue.cancel()` 调 `scope.cancel()`：置位并调用当前登记的全部钩子。

竞态：取消可能落在「任务已进入 RUNNING」与「后端 Popen 完成」之间。此时 scope 已被
取消，`register` 返回 False——后端据此自己杀掉刚起的子进程，而不是傻等它跑完。
没有活动 scope（CLI 直接调用、单元测试）时 `register_kill_hook` 是空操作并返回 True。
"""
import logging
import threading

logger = logging.getLogger(__name__)


class CancelScope:
    def __init__(self):
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._hooks = []

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        """置位并调用已登记的钩子。钩子在**锁外**调用：钩子里（杀进程、收尸，可能
        耗时数秒）如果反过来碰 scope（如 unregister）不会死锁。重复 cancel 是空操作。"""
        with self._lock:
            if self._event.is_set():
                return
            self._event.set()
            hooks = list(self._hooks)
            self._hooks.clear()
        for hook in hooks:
            try:
                hook()
            except Exception:  # 一个钩子失败不能挡住其余钩子
                logger.exception("取消钩子执行失败")

    def register(self, hook) -> bool:
        """登记钩子。scope 已被取消则**不登记**并返回 False——调用方必须自己收拾
        （杀掉刚起的子进程）；这是关掉「注册晚于取消」竞态的唯一手段。"""
        with self._lock:
            if self._event.is_set():
                return False
            self._hooks.append(hook)
            return True

    def unregister(self, hook) -> None:
        with self._lock:
            try:
                self._hooks.remove(hook)
            except ValueError:
                pass


_local = threading.local()


def activate(scope: CancelScope) -> None:
    _local.scope = scope


def deactivate() -> None:
    _local.scope = None


def current():
    return getattr(_local, "scope", None)


def register_kill_hook(hook) -> bool:
    scope = current()
    if scope is None:
        return True
    return scope.register(hook)


def unregister_kill_hook(hook) -> None:
    scope = current()
    if scope is not None:
        scope.unregister(hook)
