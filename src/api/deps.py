"""单例依赖"""
from src.utils import load_global_config
from src.task_queue import TaskQueue

_config = None
_queue = None


def get_config() -> dict:
    global _config
    if _config is None:
        _config = load_global_config()
    return _config


def get_queue() -> TaskQueue:
    global _queue
    if _queue is None:
        _queue = TaskQueue(config=get_config())
    return _queue


def set_queue(q: TaskQueue):
    global _queue
    _queue = q


def set_config(c: dict):
    global _config
    _config = c
