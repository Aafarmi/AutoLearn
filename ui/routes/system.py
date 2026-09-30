"""系统级路由：关机（P16）。

两条路由的**副作用等级不同**，刻意分开 —— 与 ``/api/targets`` 的分法一致：

``GET /api/system/status``
    纯只读。界面在弹出确认框前调它，用来列「会关闭什么」。可以反复调。
``POST /api/system/shutdown``
    不可逆 —— 它会让整个控制台进程退场。所以有两道保护：

    1. **有任务在跑时默认拒绝**（409 ``run_active``），必须显式 ``force=true``。
       静默中断一个跑了半小时的任务是很坏的失败模式；
    2. **先回执、后动手**：收尾动作挂在 ``BackgroundTasks`` 上，响应先发出去。
       抢在响应之前杀进程，用户看到的是一次莫名其妙的连接重置 ——
       而其实关机成功了。

为什么不做成 ``POST /api/run/stop`` 的扩展
------------------------------------------
那不是同一件事。停止运行只让编排循环停下来，控制台还在、靶场还在、端口还占着；
关机是**整个程序退场**：控制台 + 靶场 + 本程序接管启动的浏览器。
把两者合并，会让「我只想停下这一轮」变成「我顺手把控制台也关了」。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status

from ui import runner, system
from ui.deps import is_running, set_orchestrator
from ui.schemas import ShutdownOut, SystemPortOut, SystemStatusOut

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/system", tags=["system"])


def _status() -> SystemStatusOut:
    """当前状态 + 「关闭会做什么」的预览。**只查询，不改任何东西**。"""
    plan = system.build_plan(running=is_running())
    return SystemStatusOut(
        ports=[
            SystemPortOut(port=o.port, pid=o.pid, owner=o.owner, exe=o.exe)
            for o in plan.occupants
        ],
        browsers=plan.browser_count,
        running=plan.running,
        preview=plan.reasons(),
    )


@router.get("/status", response_model=SystemStatusOut)
async def system_status() -> SystemStatusOut:
    """关机预览：哪些端口会被释放、哪些不归我们管、有几个自管浏览器。**只读**。"""
    return _status()


@router.post("/shutdown", response_model=ShutdownOut)
async def shutdown(
    background: BackgroundTasks,
    force: bool = Query(default=False, description="有任务正在跑时也要关闭"),
) -> ShutdownOut:
    """关闭控制台并释放本程序占用的全部端口。

    返回体是**回执而不是结果**（见模块头第 2 条）：拿到它就代表关机已经排上，
    实际收尾在响应发出后立刻开始，最后会 ``os._exit`` 掉整个进程。
    """
    running = is_running()
    if running and not force:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "run_active",
                "message": "有任务正在运行，关闭控制台会中断它。",
                "next_action": "确认要中断就再点一次「关机」；想留着就先回任务里点「停止」。",
            },
        )

    if running:
        # 停编排器是**瞬时**的（设标志 + 释放 gate，见 ``Orchestrator.stop``），
        # 放在响应之前做，让这一轮有个干净的收尾点。真收不完也没关系 ——
        # 下面的 ``os._exit`` 是兜底，任务的陈旧状态由 ``display_status`` 兜住。
        try:
            await runner.control("stop")
            set_orchestrator(None)
        except Exception as exc:  # 收尾失败不该让用户关不掉程序
            logger.warning("关机前停止编排器失败：%s", exc)

    plan = system.build_plan(running=running)
    # 收尾挂后台：先让响应到达前端，再动手（``apply_shutdown`` 自带 0.35s 宽限）。
    background.add_task(system.apply_shutdown, plan)

    return ShutdownOut(
        ok=True,
        stopped=[
            f"port:{o.port}(pid {o.pid})" for o in plan.siblings
        ] + ([f"browsers:{plan.browser_count}"] if plan.browser_count else []),
        skipped=[f"port:{o.port}(pid {o.pid})" for o in plan.foreign],
        preview=plan.reasons(),
    )
