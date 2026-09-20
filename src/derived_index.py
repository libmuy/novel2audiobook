"""
角色引用统计索引（计划 005）

统计每个角色被多少小说、多少分块引用。用一个可随时删除、删了自动重建的派生索引来做，
不引入数据库。
"""
import glob
import hashlib
import json
import os

from src.utils import PROJECT_ROOT

ROLE_REFS_PATH = os.path.join(PROJECT_ROOT, ".cache", "index", "role_refs.json")


def compute_fingerprint(library_dir: str = None) -> str:
    """把所有 library/*/chapters/*/script_final.json 的
    (相对路径, mtime, size) 三元组排序后拼起来算 md5。
    只 stat 不读内容——几千个文件也是毫秒级。"""
    if library_dir is None:
        from src.library import library_root  # 延迟导入：library 依赖 status_tracker，避免循环
        library_dir = library_root()

    pattern = os.path.join(library_dir, "*", "chapters", "*", "script_final.json")
    files = sorted(glob.glob(pattern))

    fingerprints = []
    for f in files:
        rel_path = os.path.relpath(f, library_dir)
        stat = os.stat(f)
        fingerprints.append(f"{rel_path}:{stat.st_mtime}:{stat.st_size}")

    if not fingerprints:
        return ""

    content = "|".join(fingerprints)
    return hashlib.md5(content.encode()).hexdigest()


def build_role_refs(library_dir: str = None) -> dict:
    """全量扫描，统计每个 role_id 的引用情况。只统计 script_final.json，
    script_draft.json 是未定稿的草稿不算数。返回：
    {"fingerprint": "...",
     "roles": {"su_yan": {"novels": ["nv_xianni"], "segment_count": 87,
                          "by_novel": {"nv_xianni": 87}}}}"""
    if library_dir is None:
        from src.library import library_root  # 延迟导入：library 依赖 status_tracker，避免循环
        library_dir = library_root()

    fingerprint = compute_fingerprint(library_dir)
    roles = {}

    pattern = os.path.join(library_dir, "*", "chapters", "*", "script_final.json")
    for script_path in sorted(glob.glob(pattern)):
        # novel_id = 库目录下的第一级目录名。基于相对路径取，不依赖库目录本身叫什么
        # （server.library_root 可配，且路径里恰好还有别的 "library" 段时旧写法会取错）
        novel_id = os.path.relpath(script_path, library_dir).split(os.sep)[0]

        try:
            with open(script_path, "r", encoding="utf-8") as f:
                segments = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue

        for seg in segments:
            speaker = seg.get("speaker")
            if not speaker:
                continue  # 未绑定角色不计入

            if speaker not in roles:
                roles[speaker] = {"novels": set(), "segment_count": 0, "by_novel": {}}

            roles[speaker]["novels"].add(novel_id)
            roles[speaker]["segment_count"] += 1
            roles[speaker]["by_novel"][novel_id] = roles[speaker]["by_novel"].get(novel_id, 0) + 1

    # 转换 set 为 list 以便 JSON 序列化
    result_roles = {}
    for role_id, info in roles.items():
        result_roles[role_id] = {
            "novels": sorted(info["novels"]),
            "segment_count": info["segment_count"],
            "by_novel": info["by_novel"],
        }

    return {"fingerprint": fingerprint, "roles": result_roles}


def get_role_refs(library_dir: str = None) -> dict:
    """读缓存文件，比对 fingerprint；不一致或文件不存在就调 build_role_refs 重建并落盘。"""
    current_fp = compute_fingerprint(library_dir)

    if os.path.exists(ROLE_REFS_PATH):
        try:
            with open(ROLE_REFS_PATH, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("fingerprint") == current_fp:
                return cached
        except (OSError, json.JSONDecodeError):
            pass

    # 重建
    refs = build_role_refs(library_dir)
    invalidate()
    os.makedirs(os.path.dirname(ROLE_REFS_PATH), exist_ok=True)
    tmp_path = ROLE_REFS_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(refs, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, ROLE_REFS_PATH)
    return refs


def invalidate():
    """直接删掉缓存文件。所有写 script_final.json 的 API 路径都要调这个。"""
    if os.path.exists(ROLE_REFS_PATH):
        try:
            os.remove(ROLE_REFS_PATH)
        except OSError:
            pass
