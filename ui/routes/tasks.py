"""任务与条目路由（P5 建面 / P13 任务化）。

**两个层级，别混**
------------------
- **任务**（``run``）：主页面上一行就是它 —— 有名字、有状态、有进度。
  ``/api/tasks`` 一族。
- **条目**（``task_item``）：任务下面的一道题 / 一集，点开任务才看得到。
  ``/api/items`` 一族。

P5 时代只有「条目」这一层，``/api/tasks`` 指的其实是条目。P13 把「任务」这个词
还给 run 之后，条目级接口迁到 ``/api/items``（``GET /api/tasks/{run_id}`` 与
``GET /api/items/{item_id}`` 从此各指各的，不会互相抢路由）。

两条硬约束不变
-------------
1. ``submitted`` 态条目的确认按钮**置灰**，后端同时返回 409 —— 前端置灰是体验，
   后端 409 才是保障；
2. ``confirm`` / ``reject`` 是**人工决策**，是本模块唯一会写条目状态的动作，
   机器人的状态推进仍归编排层。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse, Response

from core import db as core_db
from core.config import RunConfig
from core.enums import QuestionState
from core.events import Event
from core.states import IllegalTransition, require_transition
from ui import runner
from ui.deps import (
    get_current_run_id,
    get_event_bus,
    get_registry,
    get_run_config,
    get_store,
    is_running,
    set_run_config,
)
from ui.schemas import (
    BulkDeleteIn,
    BulkDeleteOut,
    ConfirmIn,
    ItemDetailOut,
    SkippedTaskOut,
    TaskCreateIn,
    TaskDetailOut,
    TaskItemOut,
    TaskListOut,
    TaskOut,
    TaskRenameIn,
)
from ui.store import ARTIFACT_FILES, db_file

__all__ = ["items_router", "router"]

router = APIRouter(prefix="/api/tasks", tags=["tasks"])
items_router = APIRouter(prefix="/api/items", tags=["items"])

_MIME = {
    ".png": "image/png",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
}

#: 决策 → 目标状态
_DECISION_TARGET = {
    "confirm": QuestionState.APPLIED,
    "reject": QuestionState.SKIPPED,
}


# --------------------------------------------------------------------------- #
# 任务（run）
# --------------------------------------------------------------------------- #
def _task_out(task: TaskOut | None, run_id: str) -> TaskOut:
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "task_not_found", "message": f"任务 {run_id} 不存在"},
        )
    return task


@router.get("", response_model=TaskListOut)
async def list_tasks() -> TaskListOut:
    """任务列表（新的在前）。主页面用。"""
    store = get_store()
    names = runner.model_name_map(get_registry())
    active = get_current_run_id() if is_running() else None
    return TaskListOut(
        tasks=store.list_tasks(model_names=names, active_run_id=active, running=is_running()),
        active_run_id=active,
        running=is_running(),
    )


async def _launch(run_id: str, *, retry: bool = False) -> TaskOut:
    """起任务 —— **``start`` 与 ``retry`` 共用这一份实现**。

    「重试」不是另一条启动路径：``Orchestrator._restore()`` 本来就按 ``qid``
    认领已有条目，``_drain_quiz`` 遇到 ``is_terminal`` 的题**只前进不重做**，
    所以「再起一次」天然就是**断点续跑** —— 已做完的题不重做、提交绝不重放。
    另写一份启动逻辑，只会让两条路慢慢分叉（P13 起就把「两个入口共用一份实现」
    写进了维护指南）。

    用**任务自己的配置快照**跑，不是当前草稿 —— 否则「起一个三天前的任务」会拿着
    今天的配置跑，跟界面上显示的完全不是一回事。
    """
    if is_running():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error_code": "run_active", "message": "已有任务在进行中"},
        )
    store = get_store()
    task = store.task(run_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "task_not_found", "message": f"任务 {run_id} 不存在"},
        )

    conn = core_db.init_db(db_file())
    try:
        run = core_db.get_run(conn, run_id)
    finally:
        conn.close()
    cfg = RunConfig.model_validate((run or {}).get("config") or {})

    try:
        await runner.start_task(run_id=run_id, cfg=cfg)
    except runner.TaskStartError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": exc.code,
                "message": str(exc),
                "next_action": exc.next_action,
            },
        ) from exc

    if retry:
        get_event_bus().emit(Event.RUN_RESUMED, {"run_id": run_id, "retry": True})
    return _task_out(store.task(run_id, active_run_id=run_id, running=True), run_id)


@router.post("", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
async def create_task(payload: TaskCreateIn) -> TaskOut:
    """新建任务。

    **只建不跑** —— 建完由 ``POST /api/tasks/{run_id}/start`` 起。
    这样「新建任务」和「开始跑」是两件事：用户可以先把配置填完整再启动，
    失败时也分得清是「配置不对」还是「跑起来炸了」。
    """
    cfg = get_run_config()
    if payload.config is not None:
        # 用 ``exclude_unset`` 而不是 ``exclude_none``：界面上「两处模型都留空」是一个
        # **明确的决定**（= 走降级链 / Mock），它必须能覆盖掉草稿里以前选过的模型。
        # ``exclude_none`` 会把显式传进来的 ``null`` 一起丢掉，于是任务快照里悄悄
        # 留着上次选的模型 —— 界面上写着「Mock」，跑起来却在调真实模型。
        # ``exclude_unset`` 只丢**压根没传**的字段，显式 ``null`` 照样生效。
        patch = payload.config.model_dump(exclude_unset=True)
        try:
            merged = cfg.model_dump()
            merged.update(patch)
            cfg = RunConfig(**merged)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"error_code": "invalid_config", "message": str(exc)},
            ) from exc
        set_run_config(cfg)

    task = runner.create_task(name=payload.name, config=cfg)
    get_event_bus().emit(
        Event.TASK_CREATED, {"run_id": task["run_id"], "name": task["name"]}
    )
    store = get_store()
    return _task_out(
        store.task(
            str(task["run_id"]),
            model_names=runner.model_name_map(get_registry()),
            active_run_id=get_current_run_id() if is_running() else None,
            running=is_running(),
        ),
        str(task["run_id"]),
    )


@router.get("/{run_id}", response_model=TaskDetailOut)
async def get_task(run_id: str) -> TaskDetailOut:
    """任务详情：任务本身 + 它下面的条目（过程与交互都从这里取）。"""
    store = get_store()
    detail = store.task_detail(
        run_id,
        model_names=runner.model_name_map(get_registry()),
        active_run_id=get_current_run_id() if is_running() else None,
        running=is_running(),
    )
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "task_not_found", "message": f"任务 {run_id} 不存在"},
        )
    return detail


@router.get("/{run_id}/cache")
async def get_task_cache(run_id: str) -> dict[str, Any]:
    """**任务缓存预览**（只读）：这个任务在磁盘上留了什么、有多大。

    它回答的是用户最常问的一句话：「删了这个任务，到底会删掉什么？」
    —— 答案就是这里列出的整个目录（截图 + 逐题留痕 + 操作日志 ``events.jsonl``）。
    列不出来的（``state/autolearn.db`` 里按 qid 全局存的作答、浏览器登录态、
    课程级播放断点）**不属于任何一个任务**，删任务不会碰它们。
    """
    store = get_store()
    if store.task(run_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "task_not_found", "message": f"任务 {run_id} 不存在"},
        )
    return store.run_cache(run_id)


@router.patch("/{run_id}", response_model=TaskOut)
async def rename_task(run_id: str, payload: TaskRenameIn) -> TaskOut:
    """改任务名。"""
    conn = core_db.init_db(db_file())
    try:
        if core_db.get_run(conn, run_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "task_not_found", "message": f"任务 {run_id} 不存在"},
            )
        core_db.rename_run(conn, run_id, payload.name.strip())
    finally:
        conn.close()
    store = get_store()
    return _task_out(
        store.task(
            run_id,
            model_names=runner.model_name_map(get_registry()),
            active_run_id=get_current_run_id() if is_running() else None,
            running=is_running(),
        ),
        run_id,
    )


@router.delete("/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(run_id: str) -> Response:
    """删任务：库里的行 + **它的全部缓存**（截图 / 逐题留痕 / 操作日志）。

    **运行中的任务不许删**。缓存目录是「每个任务一个」的（``logs/<run_id>/``），
    所以这次删除不会波及别的任务；删除范围与边界见 ``GET /api/tasks/{run_id}/cache``
    与 ``core.trace.run_dir`` 的说明。
    """
    if is_running() and get_current_run_id() == run_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error_code": "run_active", "message": "任务正在运行，请先停止再删除"},
        )
    if not get_store().delete_task(run_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "task_not_found", "message": f"任务 {run_id} 不存在"},
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{run_id}/start", response_model=TaskOut)
async def start_task(run_id: str) -> TaskOut:
    """起任务。前置检查不过 → 400 + 错误码（界面据此给引导卡）。"""
    return await _launch(run_id)


@router.post("/{run_id}/retry", response_model=TaskOut)
async def retry_task(run_id: str) -> TaskOut:
    """**重试**：把失败 / 停下的任务从断点续跑。

    ⚠️ 必须定义在 ``/{run_id}/{action}`` **之前** —— 否则 ``/xxx/retry`` 会先被
    那条通配路由吃掉，然后以「未知动作 retry」404 出来（``run.py`` 的
    ``/models/order`` 栽过同一个坑）。

    与 ``start`` **是同一件事**（见 :func:`_launch`）：已经做完的题不会重做、
    提交绝不重放。多一个名字只是为了让界面上的按钮说人话，
    也让「重试」这件事在事件流里认得出来。
    """
    return await _launch(run_id, retry=True)


@router.post("/bulk-delete", response_model=BulkDeleteOut)
async def bulk_delete_tasks(payload: BulkDeleteIn) -> BulkDeleteOut:
    """**批量删除**任务（连同它的条目与留痕）。

    ⚠️ 同样定义在 ``/{run_id}/{action}`` **之前**，理由同上。

    逐条处理、**逐条如实回报**：正在跑的那条跳过并说明原因，其余照删 ——
    一次失败就整批放弃，等于逼用户自己去找出哪个在跑、再勾一遍。
    """
    store = get_store()
    deleted: list[str] = []
    skipped: list[SkippedTaskOut] = []
    # ``dict.fromkeys`` **去重且保序**：重复勾选不该变成删两次。
    for run_id in dict.fromkeys(payload.run_ids):
        if is_running() and get_current_run_id() == run_id:
            skipped.append(SkippedTaskOut(run_id=run_id, reason="任务正在运行，请先停止"))
            continue
        if store.delete_task(run_id):
            deleted.append(run_id)
        else:
            skipped.append(SkippedTaskOut(run_id=run_id, reason="任务不存在"))
    return BulkDeleteOut(deleted=deleted, skipped=skipped)


@router.post("/{run_id}/{action}", response_model=TaskOut)
async def control_task(run_id: str, action: str) -> TaskOut:
    """``pause`` / ``resume`` / ``stop`` —— 只对**当前活跃**任务有效。"""
    if action not in {"pause", "resume", "stop"}:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "unknown_action", "message": f"未知动作 {action}"},
        )
    if not is_running() or get_current_run_id() != run_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error_code": "run_not_active", "message": "该任务当前没有在运行"},
        )
    try:
        await runner.control(action)
    except NotImplementedError as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail={"error_code": "not_implemented", "message": str(exc)},
        ) from exc

    if action == "pause":
        get_event_bus().emit(Event.RUN_PAUSED, {"run_id": run_id})
    elif action == "resume":
        get_event_bus().emit(Event.RUN_RESUMED, {"run_id": run_id})
    else:
        get_event_bus().emit(Event.RUN_FINISHED, {"run_id": run_id, "stopped": True})

    store = get_store()
    return _task_out(
        store.task(
            run_id,
            model_names=runner.model_name_map(get_registry()),
            active_run_id=run_id if is_running() else None,
            running=is_running(),
        ),
        run_id,
    )


# --------------------------------------------------------------------------- #
# 条目（task_item）：题目 / 分集
# --------------------------------------------------------------------------- #
@items_router.get("", response_model=list[TaskItemOut])
async def list_items(
    run_id: str | None = None, type: str | None = None, state: str | None = None
) -> list[TaskItemOut]:
    """条目列表。**默认只看当前（或最近一次）任务** —— 不加过滤会把历次运行的
    条目全倒出来，界面上就是几百条乱七八糟的东西。"""
    store = get_store()
    target = run_id or get_current_run_id() or store.latest_run_id()
    return store.list_items(run_id=target, task_type=type, state=state)


@items_router.get("/{item_id}", response_model=ItemDetailOut)
async def get_item(item_id: str) -> ItemDetailOut:
    """单题 / 单集详情：含 before/after 截图 URL、采样明细、``level_used``。"""
    detail = get_store().item_detail(item_id)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "item_not_found", "message": f"条目 {item_id} 不存在"},
        )
    return detail


@items_router.post("/{item_id}/confirm", response_model=TaskItemOut)
async def confirm_item(item_id: str, payload: ConfirmIn) -> TaskItemOut:
    """人工确认 / 否决。``submitted`` 态返回 409。"""
    store = get_store()
    item = store.get(item_id)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "item_not_found", "message": f"条目 {item_id} 不存在"},
        )

    current = QuestionState(item.state)
    # 危险态必须最先拦：submitted 的出边只能由结果回读触发，人工点按钮也不行
    if current is QuestionState.SUBMITTED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "danger_state",
                "message": "该题已提交，只能回读结果，不能再次决策",
            },
        )

    target = _DECISION_TARGET[payload.decision]
    try:
        require_transition(current, target)
    except IllegalTransition as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "illegal_transition",
                "message": f"当前状态 {current.value} 不接受 {payload.decision}",
            },
        ) from exc

    store.set_state(item_id, target)
    updated = store.get(item_id)
    if updated is None:  # pragma: no cover - 刚写完不可能没有
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
    get_event_bus().emit(
        Event.TASK_STATE_CHANGED,
        {
            "item_id": item_id,
            "from": current.value,
            "to": target.value,
            "decision": payload.decision,
            "note": payload.note,
        },
    )
    return updated


@items_router.get("/{item_id}/artifacts")
async def list_artifacts(item_id: str) -> dict[str, list[str]]:
    """留痕文件清单。"""
    store = get_store()
    run_id = store.run_id_of(item_id)
    if run_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "item_not_found", "message": f"条目 {item_id} 不存在"},
        )
    return {"files": store.artifacts(run_id, item_id)}


@items_router.get("/{item_id}/artifacts/{name}")
async def read_artifact(item_id: str, name: str) -> Response:
    """取单个留痕文件内容（截图 / JSON）。

    ``name`` 走**白名单**而不是黑名单 —— 任何拼接出来以外的名字一律 404，
    这样 ``../`` 之类的路径穿越在第一步就死掉。
    """
    if name not in ARTIFACT_FILES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "artifact_not_found", "message": f"不支持的留痕文件 {name}"},
        )
    store = get_store()
    run_id = store.run_id_of(item_id)
    if run_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "item_not_found", "message": f"条目 {item_id} 不存在"},
        )
    target = store.item_dir(run_id, item_id) / name
    if not target.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "artifact_not_found", "message": f"{name} 尚未生成"},
        )
    media_type = _MIME.get(target.suffix.lower(), "application/octet-stream")
    return FileResponse(target, media_type=media_type)
