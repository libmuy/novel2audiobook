"""FastAPI 应用入口"""
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from src.utils import resolve_path
from src.api.deps import get_config, get_queue, set_queue
from src.api.routers import novels, chapters, segments, roles, tasks, system, events


@asynccontextmanager
async def lifespan(app):
    # 启动
    from tools.gpu_arbiter import recover_orphaned_suspension
    config = get_config()
    recover_orphaned_suspension(config)

    queue = get_queue()
    queue.recover_on_startup()
    queue.start()
    set_queue(queue)

    yield

    # 关闭
    queue.stop(wait=True)
    recover_orphaned_suspension(config)


def create_app():
    app = FastAPI(title="小说有声书生成流水线", lifespan=lifespan)

    # 注册路由
    app.include_router(novels.router, prefix="/api")
    app.include_router(chapters.router, prefix="/api")
    app.include_router(segments.router, prefix="/api")
    app.include_router(roles.router, prefix="/api")
    app.include_router(tasks.router, prefix="/api")
    app.include_router(system.router, prefix="/api")
    app.include_router(events.router, prefix="/api")

    # 静态文件（所有 /api 路由之后）；resolve_path 动态读取 PROJECT_ROOT，
    # 见 src/api/routers/chapters.py 里的详细注释
    static_dir = resolve_path(os.path.join("web", "static"))
    os.makedirs(static_dir, exist_ok=True)
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

    return app
