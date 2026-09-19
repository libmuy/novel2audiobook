"""
推理子进程的终止工具（TTS 与素材生成共用同一套）。

`os.killpg` 只有在子进程是用 `start_new_session=True` 起的（自己是新进程组的组长）
时才安全。如果子进程其实跟服务器同属一个进程组，`killpg` 会把 API 服务器自己也杀掉——
所以这里在动手前先核对子进程的进程组 id 不等于当前进程的，不满足就退化成只终止这一个
进程，宁可杀不干净也不误杀自己。
"""
import contextlib
import logging
import os
import signal
import subprocess

from src import cancel_scope

logger = logging.getLogger(__name__)


def in_own_process_group(proc) -> bool:
    """子进程是否在独立于当前进程的进程组里（才可以对整个组 killpg）"""
    try:
        return os.getpgid(proc.pid) != os.getpgrp()
    except (ProcessLookupError, PermissionError):
        return False


def terminate_process_group(proc, grace_sec: float = 5.0) -> None:
    """SIGTERM → 等 grace_sec 秒 → SIGKILL，并保证最后 wait() 收尸（不留僵尸进程）。
    各 infer 脚本都没装信号处理器，SIGTERM 会立即终止解释器、显存由驱动回收，
    所以宽限期只是保险。已经退出的进程直接返回。"""
    if proc is None or proc.poll() is not None:
        return
    grouped = in_own_process_group(proc)

    def _send(sig):
        try:
            if grouped:
                os.killpg(os.getpgid(proc.pid), sig)
            elif sig == signal.SIGKILL:
                proc.kill()
            else:
                proc.terminate()
        except (ProcessLookupError, PermissionError):
            pass

    if not grouped:
        logger.warning("子进程 %s 不在独立进程组里，只终止该进程本身（不能 killpg，否则会连服务器一起杀）", proc.pid)
    _send(signal.SIGTERM)
    try:
        proc.wait(timeout=grace_sec)
        return
    except subprocess.TimeoutExpired:
        pass
    _send(signal.SIGKILL)
    try:
        proc.wait(timeout=grace_sec)
    except subprocess.TimeoutExpired:
        logger.error("子进程 %s 在 SIGKILL 后 %.0f 秒仍未退出", proc.pid, grace_sec)


@contextlib.contextmanager
def kill_on_cancel(proc):
    """在 with 块内，任务被取消时终止 proc（整个进程组）。

    后端在 Popen 之后立刻进入这个块。如果取消恰好发生在任务进入 RUNNING 与这里之间，
    scope 已取消、注册会失败——此时就地终止刚起的子进程，调用方随后的 communicate()
    会很快返回，再由调用方按「取消」处理（不要写占位音）。

    **os.killpg 只有在子进程用 start_new_session=True 起的时候才安全**（见
    terminate_process_group 的护栏）：给任何新的子进程后端接这个钩子之前，先确认它是
    独立进程组，否则取消会把 API 服务器自己也杀掉。"""
    def hook():
        terminate_process_group(proc)

    if not cancel_scope.register_kill_hook(hook):
        terminate_process_group(proc)
    try:
        yield
    finally:
        cancel_scope.unregister_kill_hook(hook)
