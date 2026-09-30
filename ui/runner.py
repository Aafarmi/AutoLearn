"""任务生命周期服务（P13）。

把「建任务 / 起任务 / 控任务」从路由里抽出来，因为现在有**两个入口**都要用它：

- 用户界面：``POST /api/tasks`` 建任务，``POST /api/tasks/{id}/start`` 起任务；
- 兼容入口：``POST /api/run/start``（P7 起就有，测试与 ``scripts/`` 都在用）。

两边各写一份「起任务」必然漂移 —— 一份改了启动前检查、另一份没改，
症状就是「界面上拦住了，用脚本却能起一个注定跑不动的运行」。

它跟 ``ui/assembly`` 的分工
--------------------------
``assembly`` 只管**造依赖**（谁实现感知 / 求解 / 执行）；
这里管**流程与前置检查**（什么时候允许起、起了之后谁负责收尾）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from core import db as core_db
from core.config import RunConfig
from core.enums import ErrorCode, TargetKind
from core.events import Event
from core.orchestrator import Orchestrator, RunContext
from core.trace import EventBus
from target.base import TargetUnavailableError
from ui.deps import get_event_bus, get_registry, get_run_deps_factory
from ui.guide import guide_for
from ui.store import db_file

__all__ = [
    "TaskStartError",
    "control",
    "create_task",
    "drive",
    "model_name_map",
    "preflight",
    "start_task",
]

logger = logging.getLogger(__name__)

#: 后台驱动任务。持引用，避免被 GC 提前回收。
_tasks: set[asyncio.Task[None]] = set()


class TaskStartError(RuntimeError):
    """启动前检查没过。``code`` 进 §2.4 错误码字典，供界面分流引导卡。"""

    def __init__(self, code: ErrorCode | str, message: str, *, next_action: str | None = None):
        self.code = str(code)
        self.next_action = next_action
        super().__init__(message)


def model_name_map(registry: Any) -> dict[str, str]:
    """``profile_id → 用户起的名字``。任务列表要显示「用了哪个模型」。"""
    return {p.profile_id: p.name for p in registry.list()}


def _conn():
    return core_db.init_db(db_file())


def create_task(
    *,
    name: str | None = None,
    config: RunConfig | None = None,
    conn: Any | None = None,
) -> dict[str, Any]:
    """建一条任务（``run`` 行），并返回它的原始字段。

    ``config`` 留空则用当前运行配置（``state/run_config.json`` 里的草稿）。
    **配置会被快照进任务**：用户之后改全局配置，历史任务显示的还是当时那一套。
    """
    from ui.deps import get_run_config

    cfg = config if config is not None else get_run_config()
    own = conn is None
    connection = conn if conn is not None else _conn()
    try:
        run_id = uuid.uuid4().hex[:12]
        task_name = (name or "").strip() or core_db.next_task_name(connection)
        core_db.create_run(
            connection,
            run_id=run_id,
            name=task_name,
            config=cfg,
            started_at=datetime.now(UTC),
            status="created",
        )
        task = core_db.get_run(connection, run_id)
    finally:
        if own:
            connection.close()
    if task is None:  # pragma: no cover - 刚写进去的行
        raise RuntimeError(f"任务 {run_id} 建好后读不回来，库可能不可写")
    return task


def preflight(cfg: RunConfig, registry: Any) -> None:
    """启动前检查。**能拦的都拦在这里**，不要返回一个 run_id 再在后台默默失败。

    顺序是有讲究的：**先看目标，再看模型**。目标类型不支持时，配多少模型都没用，
    所以必须先把那条说出来 —— 反过来先报「没有模型」，用户会去配一套模型、
    再点一次，才被告知「这个目标根本跑不了」，白忙一场。
    """
    if cfg.target_id and cfg.target_kind is TargetKind.DESKTOP_WINDOW:
        # 桌面窗口的**采集**已可用（枚举 / 截图 / 系统级点击），但「图片 → 模型 →
        # 题干与选项坐标」这条识别链路还没接上，现在启动必然跑不出东西。
        raise TaskStartError(
            ErrorCode.TARGET_UNAVAILABLE,
            (
                "应用程序窗口目前只支持「抓取 + 截图」：窗口枚举、画面截取与系统级点击"
                "都已可用，但视觉识别链路（截图→模型→题干与选项坐标）尚未接入，"
                "因此还不能启动运行。请暂时选「网页」目标。"
            ),
        )

    # v0.2.0：页面只由模型读（截图 → 模型 → 题干与坐标），**没有第二条通道**，
    # 所以「没有可用模型链」不再是「只有模型优先才要管」的事 —— 任何一次运行
    # 都起不来。不再退回 Mock 起跑：那样只会返回一个 run_id，然后在后台
    # 一路上报「读不到题」，用户看到的是「跑起来了但什么都没干」。
    if not registry.active_chain():
        card = guide_for("no_config")
        raise TaskStartError(
            ErrorCode.NO_CONFIG,
            (
                "还没有可用的模型配置，无法启动：v0.2.0 起页面只能由模型读"
                "（截图 → 模型 → 题干与选项坐标），没有模型就完全跑不动。"
                "请先在「模型配置」里新增一套配置，点「测试连接」确认它支持视觉。"
            ),
            next_action=card.next_action if card else None,
        )

    # 「有一条配置」还不够 —— 那条配置得**支持视觉**。读题就是**发图**给模型，
    # 纯文本模型收不了图，跑起来只会一路停在「读不到题目」。
    #
    # 这条检查原本是缺的（2026-09-28 实测踩到）：引导卡片 ``GUIDE_VISION_UNSUPPORTED``
    # 早就写好了，却**没有任何调用方** —— 于是预检放行、运行照起，
    # 直到读题才失败，界面上只有一句笼统的失败理由，
    # 排查方向被引到"通道读不到"上，而真正的原因是**模型根本收不了图**。
    # （2026-09-30 起那条理由已经是具体的 ``vision_read_failed``，
    # 但"启动前就拦住"仍然比"跑到读题才失败"好得多：那时连题目都还没读到。）
    if not _vision_capable(registry, cfg):
        card = guide_for(ErrorCode.VISION_UNSUPPORTED.value)
        raise TaskStartError(
            ErrorCode.VISION_UNSUPPORTED,
            (
                "选中的模型实测不支持图片输入，无法读题：v0.2.0 起读题就是"
                "「截图 → 模型 → 题干与选项坐标」，纯文本模型拿到图也读不出题面。"
                "请换一套支持视觉的配置，或者把它选成「解题组」、"
                "另选一套多模态模型做「视觉组」。"
            ),
            next_action=card.next_action if card else None,
        )


def _vision_capable(registry: Any, cfg: RunConfig) -> bool:
    """这一次运行**读题时实际会用的**模型，是否支持视觉。

    判据只认**实测**（``capabilities.supports_vision``，由「测试连接」写进去）：
    ``capabilities is None``（还没测过）**不算不支持** —— 否则会把没测过的用户
    拦在门外，而他们的配置很可能本来就是好的。也就是说这个检查只拦
    「**明确知道**都不支持」的情形。

    视觉组留空 = 与解题组共用同一条链（v0.2.0 的既有语义），
    所以这时检查的是整条活动链。
    """
    by_id = {p.profile_id: p for p in registry.active_chain()}

    def usable(profile: Any) -> bool:
        caps = profile.capabilities
        return caps is None or bool(caps.supports_vision)

    if cfg.vision_profile_id:
        picked = [
            by_id.get(cfg.vision_profile_id),
            *(by_id.get(pid) for pid in cfg.vision_backup_profile_ids),
        ]
        chosen = [p for p in picked if p is not None]
        if chosen:
            return any(usable(p) for p in chosen)
    return any(usable(p) for p in registry.active_chain())


async def _ensure_target_ready(cfg: RunConfig) -> None:
    """选定目标时确认它真的连得上（浏览器要求带着调试端口）。"""
    if not cfg.target_id:
        return
    from ui.assembly import make_browser_source

    source = make_browser_source(cfg)
    try:
        await source.ensure_ready()
    except TargetUnavailableError as exc:
        raise TaskStartError(ErrorCode.BROWSER_NO_DEBUG_PORT, str(exc)) from exc


async def start_task(
    *,
    run_id: str,
    cfg: RunConfig,
    bus: EventBus | None = None,
    registry: Any | None = None,
) -> None:
    """起一个已建好的任务。前置检查不过就抛 :class:`TaskStartError`。"""
    bus = bus if bus is not None else get_event_bus()
    registry = registry if registry is not None else get_registry()

    preflight(cfg, registry)
    await _ensure_target_ready(cfg)

    ctx = RunContext(run_id=run_id, cfg=cfg, started_at=datetime.now(UTC))
    try:
        deps = get_run_deps_factory()(cfg=cfg, registry=registry, bus=bus, run_id=run_id)
    except Exception as exc:
        raise TaskStartError(
            "assembly_failed", f"装配失败：{type(exc).__name__}: {exc}"
        ) from exc

    orchestrator = Orchestrator(ctx, deps=deps)
    from ui import deps as ui_deps

    ui_deps.set_orchestrator(orchestrator)
    ui_deps.set_current_run_id(run_id)
    ui_deps.set_run_config(cfg)

    conn = core_db.init_db(db_file())
    try:
        core_db.set_run_status(conn, run_id, "running")
    finally:
        conn.close()

    bus.emit(Event.RUN_STARTED, {"run_id": run_id, "cfg": cfg.model_dump(mode="json")})
    task = asyncio.create_task(drive(orchestrator, bus, run_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def drive(orchestrator: Orchestrator, bus: EventBus, run_id: str) -> None:
    """跑编排循环，并把结果翻译成事件与**任务状态**。"""
    try:
        await orchestrator.run()
        bus.emit(Event.RUN_FINISHED, {"run_id": run_id, "stopped": orchestrator.stopped})
        await _maybe_train(orchestrator, bus, run_id)
    except asyncio.CancelledError:
        raise
    except NotImplementedError as exc:
        _mark_error(run_id, "not_implemented")
        bus.emit(
            Event.RUN_ERROR,
            {"run_id": run_id, "error_code": "not_implemented", "message": str(exc)},
        )
    except Exception as exc:
        _mark_error(run_id, "run_failed")
        bus.emit(
            Event.RUN_ERROR,
            {
                "run_id": run_id,
                "error_code": "run_failed",
                "message": f"{type(exc).__name__}: {exc}",
            },
        )
    finally:
        from ui import deps as ui_deps

        ui_deps.set_orchestrator(None)


async def _maybe_train(orchestrator: Orchestrator, bus: EventBus, run_id: str) -> None:
    """任务**干净跑完**且开了训练模式时，顺手跑一次训练总结。

    三条闸门，缺一不做：

    * ``cfg.training`` 为真 —— 默认关。它会**改磁盘上的提示词文件**，
      不该在用户没要求时发生；
    * 这次运行**没暂停也没被停** —— 半途而废的记录里混着走不通的路径，
      拿来当经验只会让下一次更保守；
    * 该运行的 Provider 链非空。

    失败**只记一条日志**，绝不把训练的问题算到任务头上（任务已经跑完了）。
    """
    cfg = orchestrator.ctx.cfg
    if not bool(getattr(cfg, "training", False)):
        return
    if orchestrator.paused or orchestrator.stopped:
        return
    solver = getattr(orchestrator.deps, "solver", None)
    providers = list(getattr(solver, "providers", None) or []) if solver else []
    if not providers:
        return
    from solve.prompt_files import prompts_dir
    from solve.training import train_run
    from ui.store import log_root

    try:
        result = await train_run(
            run_id,
            log_root=log_root(),
            prompt_dir=prompts_dir(),
            providers=providers,
        )
    except Exception as exc:  # 训练是后处理，炸掉不该影响任务状态
        logger.warning("训练模式失败（run=%s）：%s", run_id, exc)
        return
    if result.ok:
        bus.emit(
            Event.TRAINING_DONE,
            {
                "run_id": run_id,
                "written": result.written,
                "files": result.files,
                "skipped": result.skipped,
            },
        )
    else:
        logger.info("训练模式未产出经验（run=%s）：%s", run_id, result.error)


def _mark_error(run_id: str, code: str) -> None:
    """把「运行炸了」写进任务状态 —— 否则任务永远停在「进行中」。"""
    conn = core_db.init_db(db_file())
    try:
        core_db.set_run_status(conn, run_id, f"error:{code}")
    finally:
        conn.close()


async def control(action: str) -> None:
    """``pause`` / ``resume`` / ``stop``。没有活跃编排器时抛 ``KeyError``。"""
    from ui.deps import get_orchestrator

    orchestrator = get_orchestrator()
    await getattr(orchestrator, action)()
