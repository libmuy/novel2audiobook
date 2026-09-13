"""任务路由"""
from fastapi import APIRouter, HTTPException
from src.api.deps import get_queue
from src.api.schemas import TaskCreate
from src import library, preflight

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("")
def list_tasks(state: str = None, group_id: str = None, limit: int = 200):
    from dataclasses import asdict
    q = get_queue()
    tasks = q.list(state=state, group_id=group_id, limit=limit)
    return [asdict(t) for t in tasks]


@router.post("")
def submit_task(data: TaskCreate):
    q = get_queue()

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
        task = q.submit(data.type, data.novel_id, chapter_id=chapter_ids[0])
        from dataclasses import asdict
        return {"tasks": [asdict(task)]}
    else:
        group_id, tasks = q.submit_batch(data.type, data.novel_id, chapter_ids)
        from dataclasses import asdict
        return {"group_id": group_id, "tasks": [asdict(t) for t in tasks]}


@router.post("/preflight")
def preflight_check(data: TaskCreate):
    q = get_queue()

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
