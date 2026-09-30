"""运行配置与生命周期路由（P5 建面 / P7 接真）。

REST 契约见规划书 §2.3。三条硬约束在此落地：

1. **运行中配置只读** —— ``PUT /api/run/config`` 返回 409；
2. **一条模型配置都没有** —— ``POST /api/run/start`` 返回 400 + 引导码，
   不是静默失败。v0.2.0 起页面只由模型读，所以这条从「只有模型优先才要求」
   变成了**任何一次运行都要求**；
3. 配置里的 ``target_kind`` 一换，``target_id`` 必须清掉（见 ``put_config``）。

启动即真跑（P7）：``start`` 走 :func:`ui.assembly.build_run_deps` 装配
感知 / 求解 / 执行 / 持久化，再由编排层在后台跑完；暂停 / 恢复 / 停止
调用的是编排层的真方法，不再返回 501。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, status

from core.config import RunConfig
from core.enums import TargetKind
from core.events import Event
from ui import APP_VERSION, BOOT_REV, code_rev, runner
from ui.deps import (
    get_current_run_id,
    get_event_bus,
    get_registry,
    get_run_config,
    get_store,
    is_running,
    set_orchestrator,
    set_run_config,
)
from ui.guide import guide_for
from ui.schemas import GuideCardOut, RunConfigIn, RunConfigOut

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["run"])

#: 本进程的启动时刻。供 ``/api/health`` 暴露 —— 判断「服务是不是旧进程」用。
_STARTED_AT = datetime.now(UTC).isoformat()


def _config_out() -> RunConfigOut:
    cfg = get_run_config()
    chain = get_registry().active_chain()
    return RunConfigOut(
        auto_apply=cfg.auto_apply,
        # ⚠️ 凡新增 RunConfig 字段，**必须**在这里回显 —— 漏传的症状是
        # 「PUT 存得下、GET 回空」，用户「选完刷新一下就变空白」，而盘上明明有。
        recalculate=cfg.recalculate,
        sample_n=cfg.sample_n,
        training=cfg.training,
        task_sequence=cfg.task_sequence,
        model_profile_id=cfg.model_profile_id,
        vision_profile_id=cfg.vision_profile_id,
        # 两个**备用组**必须一起回显（2026-09-28）。
        # 漏传的症状与当年 ``vision_profile_id`` 漏传一模一样：PUT 存得下、
        # ``GET`` 却永远回空 —— 用户「选了备用、刷新一下就变空白」，
        # 而落盘文件里明明有。**凡新增配置字段，都要同时问一句「GET 回显了吗」。**
        backup_profile_ids=cfg.backup_profile_ids,
        vision_backup_profile_ids=cfg.vision_backup_profile_ids,
        guards=cfg.guards,
        rate=cfg.rate,
        model_ready=bool(chain),
        vision_ready=any(
            p.capabilities is not None and p.capabilities.supports_vision for p in chain
        ),
        locked=is_running(),
        target_kind=cfg.target_kind,
        target_id=cfg.target_id,
    )


def _merged_config(cfg: RunConfig, patch: dict[str, Any]) -> RunConfig:
    """合并补丁后**重新过一遍校验**（``model_copy`` 不跑 validator）。"""
    merged = cfg.model_dump()
    merged.update(patch)
    return RunConfig(**merged)


@router.get("/health")
async def health() -> dict[str, object]:
    """探活。

    刻意带上 ``pid`` / ``started_at`` / ``code_rev`` / ``boot_rev`` 四项：
    **用来识别「旧进程」**。改了代码界面却没变化时，第一个要排除的就是
    「控制台连着一个改动之前启动的旧服务」。配套脚本 ``scripts/check_server.py``。

    .. warning::
       ``code_rev`` 是**请求时现读磁盘**算的，所以它查不出「改完代码没重启」——
       在活着的进程里它永远等于磁盘现状。真正用来判陈旧的是 ``boot_rev``
       （进程导入时记下的指纹）。两个都给出，是为了让人一眼看清
       「进程加载的代码」与「磁盘现在的代码」是不是同一份。
    """
    return {
        "ok": True,
        "version": APP_VERSION,
        #: **磁盘现在的**代码指纹（请求时现算）。人看的，别拿它判陈旧。
        "code_rev": code_rev(),
        #: **本进程启动时**的代码指纹 —— 与磁盘现算值不一致 = 该重启了
        "boot_rev": BOOT_REV,
        "pid": os.getpid(),
        "started_at": _STARTED_AT,
        "running": is_running(),
        "run_id": get_current_run_id(),
        "models": len(get_registry().active_chain()),
    }


@router.get("/run/config", response_model=RunConfigOut)
async def get_config() -> RunConfigOut:
    return _config_out()


@router.put("/run/config", response_model=RunConfigOut)
async def put_config(payload: RunConfigIn) -> RunConfigOut:
    """运行中提交返回 409。"""
    if is_running():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error_code": "run_locked", "message": "运行中配置只读，请先停止"},
        )
    try:
        patch = payload.model_dump(exclude_none=True)
        current = get_run_config()
        # 换目标类型 = 换一套目标的「身份证」：CDP target id 与窗口句柄互不通用。
        # 不清掉的话，用户切到「应用程序」后启动，会拿着一个标签页 ID 去当窗口句柄，
        # 报出来的错离真正原因十万八千里。**这条规则必须放在服务端** ——
        # 放在前端就只是「界面约定」，别的调用方一样会踩。
        if "target_kind" in patch and TargetKind(patch["target_kind"]) is not current.target_kind:
            patch["target_id"] = None
        updated = _merged_config(current, patch)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error_code": "invalid_config", "message": str(exc)},
        ) from exc
    set_run_config(updated)
    return _config_out()


@router.post("/run/start")
async def start_run(payload: dict[str, Any] | None = None) -> dict[str, str]:
    """**兼容入口**：建一个任务并立刻起跑，返回 ``{run_id}``。

    P13 起，产品路径是「``POST /api/tasks`` 建 → ``POST /api/tasks/{id}/start`` 起」；
    这条保留是因为 ``scripts/`` 下的验收工具与大量测试都在用它，
    语义与「新建任务 + 立刻启动」完全一致（任务名自动取「任务N」）。
    """
    if is_running():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error_code": "run_active", "message": "已有运行在进行中"},
        )

    cfg = get_run_config()
    if payload and payload.get("task_sequence"):
        cfg = _merged_config(cfg, {"task_sequence": payload["task_sequence"]})

    task = runner.create_task(config=cfg)
    run_id = str(task["run_id"])
    bus = get_event_bus()
    try:
        await runner.start_task(run_id=run_id, cfg=cfg, bus=bus)
    except runner.TaskStartError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": exc.code,
                "message": str(exc),
                "next_action": exc.next_action,
            },
        ) from exc
    return {"run_id": run_id}


async def _invoke(action: str) -> None:
    """调用编排器方法；未落实现时返回 501 而不是 500。"""
    try:
        await runner.control(action)
    except NotImplementedError as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail={"error_code": "not_implemented", "message": str(exc)},
        ) from exc


@router.post("/run/pause")
async def pause_run() -> dict[str, bool]:
    await _invoke("pause")
    get_event_bus().emit(Event.RUN_PAUSED, {"run_id": get_current_run_id()})
    return {"ok": True}


@router.post("/run/resume")
async def resume_run() -> dict[str, bool]:
    await _invoke("resume")
    get_event_bus().emit(Event.RUN_RESUMED, {"run_id": get_current_run_id()})
    return {"ok": True}


@router.post("/run/stop")
async def stop_run() -> dict[str, bool]:
    await _invoke("stop")
    get_event_bus().emit(Event.RUN_FINISHED, {"run_id": get_current_run_id(), "stopped": True})
    set_orchestrator(None)
    return {"ok": True}

@router.get("/run/progress")
async def progress() -> dict[str, Any]:
    """``{total, done, by_state, current_item_id}``。SSE 重连后对账用。"""
    run_id = get_current_run_id() or get_store().latest_run_id()
    payload = get_store().progress(run_id)
    payload["running"] = is_running()
    return payload


@router.get("/guide", response_model=GuideCardOut | None)
async def guide(code: str | None = None) -> GuideCardOut | None:
    """按错误码取引导卡；未登记的码返回 ``null``，前端不得吞掉。"""
    return guide_for(code)
