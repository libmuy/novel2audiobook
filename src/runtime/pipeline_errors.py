"""流水线通用异常定义"""


class TaskCancelled(Exception):
    """用户请求取消任务时抛出"""
    pass
