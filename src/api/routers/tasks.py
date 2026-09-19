"""任务路由"""
from fastapi import APIRouter, HTTPException
from src.api.deps import get_queue
from src.api.schemas import TaskCreate
from src import library, preflight, task_queue as tq

router = APIRouter(prefix="/tasks", tags=["tasks"])

# 不属于任何小说/章节的全局任务：不走 scope→chapter_ids 展开，直接 submit_global。
# （precompute_embedding 早就注册了 handler，但因为 POST /tasks 强制要求非空
# chapter_ids，一直没法通过 API 提交——跟 asset_gen 同一个坑，一起修。）
CHAPTERLESS_TYPES = {tq.TYPE_ASSET_GEN, tq.TYPE_PRECOMPUTE_EMBEDDING}

# 每种任务类型允许的 params 键；未知键 400，避免 params 变成没人管的垃圾桶
_ALLOWED_PARAMS = {
    tq.TYPE_MIX: {"with_assets", "output_stem"},
    tq.TYPE_ASSET_GEN: {"kinds", "only", "force"},
    tq.TYPE_PRECOMPUTE_EMBEDDING: {"role_id"},
}


def _validate_params(task_type: str, params: dict):
    if not params:
        return
    allowed = _ALLOWED_PARAMS.get(task_type, set())
    unknown = set(params) - allowed
    if unknown:
        raise HTTPException(400, f"任务类型 {task_type} 不支持参数: {', '.join(sorted(unknown))}")
    if task_type == tq.TYPE_ASSET_GEN:
        kinds = params.get("kinds")
        if kinds is not None:
            from src.asset_gen import VALID_KINDS
            if not isinstance(kinds, list) or any(k not in VALID_KINDS for k in kinds):
                raise HTTPException(400, f"kinds 必须是 {list(VALID_KINDS)} 的子集")
        only = params.get("only")
        if only is not None and not (isinstance(only, list) and all(isinstance(n, str) for n in only)):
            raise HTTPException(400, "only 必须是素材名字符串数组")


def _require_novel_id(data: TaskCreate):
    if not data.novel_id:
        raise HTTPException(400, f"任务类型 {data.type} 必须提供 novel_id")


@router.get("")
def list_tasks(state: str = None, group_id: str = None, limit: int = 200):
    from dataclasses import asdict
    q = get_queue()
    tasks = q.list(state=state, group_id=group_id, limit=limit)
    return [asdict(t) for t in tasks]


@router.post("")
def submit_task(data: TaskCreate):
    q = get_queue()

    if not tq.is_supported_type(data.type):
        raise HTTPException(400, f"不支持的任务类型: {data.type}")
    _validate_params(data.type, data.params)

    if data.type in CHAPTERLESS_TYPES:
        task = q.submit_global(data.type, params=data.params)
        from dataclasses import asdict
        return {"tasks": [asdict(task)]}
    _require_novel_id(data)

    # Determine chapter IDs from scope
    scope = data.scope or {}
    node_id = scope.get("node_id")
    chapter_ids = scope.get("chapter_ids")

    if not chapter_ids:
        # 按 node_id（整本/整部/整卷）展开为章节 ID 列表；
        # iter_chapters 本身返回的就是 chapter_id 字符串列表，不需要再取字段
        try:
            novel = library.load_novel(data.novel_id)
        except FileNotFoundError:
            raise HTTPException(404, f"小说 {data.novel_id} 不存在")

        try:
            chapter_ids = library.iter_chapters(novel, node_id)
        except ValueError:
            raise HTTPException(404, f"节点 {node_id} 不存在")

    if not chapter_ids:
        raise HTTPException(400, "未找到可处理的章节")

    if len(chapter_ids) == 1:
        task = q.submit(data.type, data.novel_id, chapter_id=chapter_ids[0], params=data.params)
        from dataclasses import asdict
        return {"tasks": [asdict(task)]}
    else:
        group_id, tasks = q.submit_batch(data.type, data.novel_id, chapter_ids, params=data.params)
        from dataclasses import asdict
        return {"group_id": group_id, "tasks": [asdict(t) for t in tasks]}


@router.post("/preflight")
def preflight_check(data: TaskCreate):
    q = get_queue()

    if not tq.is_supported_type(data.type):
        raise HTTPException(400, f"不支持的任务类型: {data.type}")
    _validate_params(data.type, data.params)

    if data.type == tq.TYPE_ASSET_GEN:
        return preflight.preflight_assets(data.params)
    if data.type in CHAPTERLESS_TYPES:
        return {"chapters": [], "summary": {"create": 1, "overwrite": 0, "skip": 0},
                "invalidated_cache_count": 0, "estimated_gpu_minutes": None}
    _require_novel_id(data)

    scope = data.scope or {}
    node_id = scope.get("node_id")
    chapter_ids = scope.get("chapter_ids")

    if not chapter_ids:
        try:
            novel = library.load_novel(data.novel_id)
        except FileNotFoundError:
            raise HTTPException(404, f"小说 {data.novel_id} 不存在")

        try:
            chapter_ids = library.iter_chapters(novel, node_id)
        except ValueError:
            raise HTTPException(404, f"节点 {node_id} 不存在")

    return preflight.preflight(data.type, data.novel_id, chapter_ids)


@router.get("/{task_id}")
def get_task(task_id: str):
    from dataclasses import asdict
    q = get_queue()
    task = q.get(task_id)
    if not task:
        raise HTTPException(404, f"任务 {task_id} 不存在")
    return asdict(task)


@router.delete("/{task_id}")
def cancel_task(task_id: str):
    q = get_queue()
    ok = q.cancel(task_id)
    if not ok:
        raise HTTPException(404, f"任务 {task_id} 不存在或无法取消")
    return {"ok": True}


@router.get("/{task_id}/log")
def get_task_log(task_id: str, offset: int = 0):
    q = get_queue()
    text, next_offset = q.read_log(task_id, offset)
    return {"text": text, "next_offset": next_offset}
