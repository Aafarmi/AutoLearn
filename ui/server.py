"""FastAPI 装配入口（P5）。

启动::

    python -m uvicorn ui.server:create_app --factory --port 8800

静态前端（``ui/static``）零构建，直接由本应用挂载，不用额外的 dev server。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from ui import APP_VERSION
from ui.deps import OrchestratorNotRunningError
from ui.routes import artifacts, events, models, run, system, targets, tasks, training

__all__ = ["STATIC_DIR", "create_app"]

#: 静态资源目录（相对本文件）
STATIC_DIR = Path(__file__).resolve().parent / "static"

ROUTERS = (run, tasks, models, events, artifacts, targets, system, training)


def create_app() -> FastAPI:
    """装配全部路由、SSE 与静态资源。"""
    app = FastAPI(
        title="AutoLearn",
        version=APP_VERSION,
        description="只使用模型（视觉）读页面的自动化系统",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    @app.exception_handler(OrchestratorNotRunningError)
    async def _not_running(_request: Request, exc: OrchestratorNotRunningError) -> JSONResponse:
        # 「没有活跃运行」是状态问题不是错误，统一 409，别让界面收到 500
        return JSONResponse(
            status_code=409,
            content={"detail": {"error_code": "run_not_active", "message": str(exc)}},
        )

    for module in ROUTERS:
        app.include_router(module.router)

    # 条目级接口（``/api/items``）与任务级（``/api/tasks``）同属 tasks 模块，
    # 但前缀不同 —— FastAPI 的一个 APIRouter 只能有一个 prefix，所以分两个注册。
    app.include_router(tasks.items_router)

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> Response:
        """单页入口。静态目录缺失时给一句可执行的提示，而不是 404 白屏。"""
        entry = STATIC_DIR / "index.html"
        if not entry.is_file():
            return JSONResponse(
                status_code=503,
                content={
                    "detail": {
                        "error_code": "ui_not_built",
                        "message": "ui/static/index.html 不存在（P5 前端产物）",
                    }
                },
            )
        return FileResponse(entry)

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    return app
