"""
config/global_config.yaml 的写入口。

PATCH /api/config 以前用 yaml.safe_dump 整文件回写，会把文件里所有注释（含素材引擎
选型的决策记录）永久抹掉——第一次在设置页点保存就不可逆。这里改用 ruamel round-trip：
只改被更新的键，其余注释、引号、缩进、键顺序原样保留（对真实的 config/global_config.yaml
做未改动的往返是字节级一致的，有测试）。
"""
import io
import os
import threading

from ruamel.yaml.comments import CommentedMap

from src.pipeline.asset_specs_store import round_trip_yaml

CONFIG_LOCK = threading.Lock()


def round_trip_update(path: str, updates: dict) -> None:
    """updates: {"section.sub.key": value}，按点分路径逐层写入，缺失的层级自动创建。
    原子写（tmp + os.replace），进程内串行。"""
    with CONFIG_LOCK:
        y = round_trip_yaml()
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                doc = y.load(f) or CommentedMap()
        else:
            doc = CommentedMap()

        for dotted, value in updates.items():
            *parents, leaf = dotted.split(".")
            node = doc
            for part in parents:
                if not isinstance(node.get(part), dict):
                    node[part] = CommentedMap()
                node = node[part]
            node[leaf] = value

        buf = io.StringIO()
        y.dump(doc, buf)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(buf.getvalue())
        os.replace(tmp, path)
