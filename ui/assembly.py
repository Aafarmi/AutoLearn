"""运行装配（P7）。

把「配置 + 模型注册表 + 事件总线」变成一份可直接交给
:class:`~core.orchestrator.Orchestrator` 的 :class:`~core.orchestrator.RunDeps`。

为什么单独一个模块
------------------
装配是**唯一**需要同时认识感知 / 求解 / 执行 / 持久化四层的地方。
放进路由文件会让路由被迫 import 半个工程；放进 ``core/`` 又会让核心层
反向依赖 ``adapters`` 与 ``ui``。放在这里，依赖方向是单向的：
``ui.assembly → {perception, solve, act, adapters, core}``。

无模型配置也能装配
------------------
``build_default_chain`` 在一条可用配置都没有时返回 **MockProvider 单链**，
所以本模块不挑配置 —— 装配这件事永远做得出东西来。

**但「没有模型」现在是硬门槛**：v0.2.0 起页面只能由模型读，所以真正拦住
无模型启动的是 ``ui/runner.py::preflight``（400 + 引导码），不是这里。
把门槛放在 runner 而不是装配层，是为了让单测与 ``scripts/`` 能继续
在无模型的前提下装配依赖、只验装配本身。

目标采集层（P11）
-----------------
装配还要裁决**这次抓什么**，见 :func:`build_target_source`：

- 运行配置里选了目标 → 造一个 :class:`~target.browsers.BrowserTargetSource`，
  附加到用户已经开着的标签页；
- 没选目标 → 返回 ``None``，退回 ``start_url`` 的自启靶场兜底
  （``scripts/`` 下的验收工具与 675 个测试全部走这条路）。

``default_start_url()`` 因此从「起始页的唯一裁决者」降级为**兜底**。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

from adapters.mock_exam.adapter import load_adapter
from core.config import RunConfig
from core.db import init_db
from core.enums import TargetKind, TaskType
from core.orchestrator import RunDeps
from core.ratelimit import ConcurrencyGate
from core.trace import RunLogger
from perception.pipeline import PerceptionPipeline
from perception.vision_probe import VisionProbe
from solve.cache import SolveCache
from solve.providers.factory import build_default_chain
from solve.providers.mock import QuestionBankSource
from solve.solver import Solver
from target.base import TargetSource
from target.browsers import BrowserTargetSource
from target.windows import DesktopWindowSource
from ui.store import db_file, log_root

if TYPE_CHECKING:  # pragma: no cover
    from core.model_registry import ModelRegistry
    from core.trace import EventBus

__all__ = [
    "COURSE_START_URL",
    "DEFAULT_START_URL",
    "build_active_chain",
    "build_run_deps",
    "build_target_source",
    "default_start_url",
    "make_browser_source",
    "make_source",
]

#: 靶场题目页。**P11 起只作测试兜底** —— 产品路径改由「目标采集层」决定抓什么，
#: 见 ``target/``。这个常量保留是因为 ``scripts/`` 下的验收工具与 675 个测试
#: 仍然全部依赖靶场。
DEFAULT_START_URL = "http://127.0.0.1:8899/quiz.html"

#: 靶场网课页（M5 场景）。同样降级为测试兜底。
COURSE_START_URL = "http://127.0.0.1:8899/course.html"


def default_start_url(cfg: RunConfig) -> str:
    """按 ``cfg.task_sequence`` 挑靶场起始页：含 ``video`` 就是网课，否则是题目页。

    **P11 起这是兜底路径**：只有在「没选目标」时才走到这里。产品语义下的起始
    页由用户勾选的目标决定（附加模式下**绝不导航**，用户页面停在哪儿就是哪儿）。

    起始页与任务序列必须一致 —— 跑到网课页上却按题目队列驱动，会一路
    「找不到题目根」；反过来则读不出分集目录。
    """
    if TaskType.VIDEO in cfg.task_sequence:
        return COURSE_START_URL
    return DEFAULT_START_URL


def make_browser_source(cfg: RunConfig) -> BrowserTargetSource:
    """按运行配置造一个浏览器采集源。

    **不要求已经选中目标** —— 「扫描有哪些标签页」这个动作发生在用户选之前。
    """
    return BrowserTargetSource(channel=cfg.browser_channel, port=cfg.browser_debug_port)


def make_source(cfg: RunConfig) -> TargetSource:
    """按 ``cfg.target_kind`` 造采集源。**不要求已选目标**（扫描时用户还没选）。

    这是「抓取」入口的唯一分派点：目标类型决定去枚举标签页还是枚举窗口。
    """
    if cfg.target_kind is TargetKind.DESKTOP_WINDOW:
        return DesktopWindowSource()
    return make_browser_source(cfg)


def build_target_source(cfg: RunConfig) -> TargetSource | None:
    """按运行配置造**已选定目标**的采集源。没选目标时返回 ``None``（退回靶场兜底）。

    返回 ``None`` 而不是抛异常：单测与 ``scripts/`` 的验收工具都是「不给目标
    直接跑」，让这条路继续可用是刻意的（见 ``RunDeps.target_source`` 的说明）。
    """
    if not cfg.target_id:
        return None
    return make_source(cfg)


def _chain_for(
    registry: ModelRegistry,
    profile_id: str | None,
    *,
    cfg: RunConfig,
    backups: list[str] | None = None,
) -> list[Any]:
    """按用户选的``profile_id`` + ``backups`` 造一条 Provider 链。

    2026-09-28 起界面上是四个选择（视觉组 / 视觉备用 / 解题组 / 解题备用），
    所以链的构成是：**主选那一套 → 备用组（按用户勾选的顺序）**。

    - **主选为空、也没有备用** → 整条活动链：保持 P4~P12 的既有语义
      （一条都没有时 ``build_default_chain`` 给出 Mock 单链，无配置也能跑通）。
    - **主选被删 / 被停用** → 只用能用的备用组；一个都没有时才退回整条活动链。
    - **备用组里的死 id**（已被删除）直接忽略 —— 用户勾过它，但它已经不在了，
      不该因此让整条链变成别的模型的降级链。

    为什么主选之后**仍然排备用**（而不是「选了就只用它」）：
    这正是用户要的语义 —— 「备用组可以多个选择」，主用挂了要能顶上。
    而 P13 那条「选了 A 就不许悄悄用 B」的约束仍然成立：B 必须是用户
    **显式勾进备用组**的，不是系统自己塞进来的。
    """
    actives = {p.profile_id: p for p in registry.active_chain()}
    ordered: list[Any] = []
    seen: set[str] = set()
    for pid in [profile_id, *(backups or [])]:
        if not pid or pid in seen:
            continue
        profile = actives.get(pid)
        if profile is None:
            continue
        seen.add(pid)
        ordered.append(profile)
    if not ordered:
        # 什么都没选（或选的都不在了）→ 保持既有降级链语义
        ordered = list(actives.values())
    return build_default_chain(
        ordered,
        credentials=registry.credentials(),
        answer_source=QuestionBankSource.from_default(),
        concurrency=cfg.llm_concurrency,
    )


def build_active_chain(registry: ModelRegistry, cfg: RunConfig) -> list[Any]:
    """按**整条活动链**造 Provider 链（没有任何显式选择）。

    它给「不属于某一次运行」的调用方用 —— 目前只有一个：**训练模式**
    （``ui/routes/training.py``）。训练不读页面，只是把一次失败/成功的留痕
    交给模型总结，所以它不需要视觉组 / 解题组那四个选择，用当前配置的
    并发档位排一条链就够了。
    """
    return _chain_for(registry, None, cfg=cfg)


def build_run_deps(
    *,
    cfg: RunConfig,
    registry: ModelRegistry,
    bus: EventBus,
    run_id: str,
    start_url: str | None = None,
    logs: Path | None = None,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
    target_source: TargetSource | None = None,
) -> RunDeps:
    """按一次运行装配全部依赖。**每次运行装配一套**，不复用跨运行的可变状态。

    不复用是刻意的：``SolveCache`` 跨运行复用会把上一次的作答当成这次的结论；
    ``httpx`` 连接池则由 Provider 自己持有并在收尾时 ``aclose``。

    **四条模型链**（2026-09-28 起是四个选择）：
    **解题组 + 解题备用**，以及 **视觉组 + 视觉备用**。
    视觉组留空时整条视觉链为空 → 编排层自动回落到解题链（保持既有行为）。
    """
    adapter = load_adapter()
    # v0.2.0 题目侧只剩视觉一条探针；媒体探针由流水线自己补（``run_video`` 用），
    # 所以这里不必显式装配 ``MediaProbe``。
    pipeline = PerceptionPipeline([VisionProbe()], cfg)

    # 请求级并发闸（M4-2）：一次运行一个闸，包住整个模型请求生命周期
    gate = ConcurrencyGate(cfg.llm_concurrency)
    providers = _chain_for(
        registry,
        cfg.model_profile_id,
        cfg=cfg,
        backups=cfg.backup_profile_ids,
    )
    solver = Solver(providers, SolveCache(), cfg, bus=bus, gate=gate)

    vision_providers: list[Any] = []
    # 视觉链**只在用户真的另选过视觉组时**单独造；否则让它为空，
    # 编排层自然回落到解题链 —— 「视觉组留空 = 与解题组共用」这条语义靠它成立。
    if cfg.vision_profile_id or cfg.vision_backup_profile_ids:
        vision_providers = _chain_for(
            registry,
            cfg.vision_profile_id,
            cfg=cfg,
            backups=cfg.vision_backup_profile_ids,
        )

    logger = RunLogger(run_id, root=logs if logs is not None else log_root())
    connection = conn if conn is not None else init_db(
        db_path if db_path is not None else db_file()
    )

    return RunDeps(
        adapter=adapter,
        pipeline=pipeline,
        solver=solver,
        cache=solver.cache,
        run_logger=logger,
        bus=bus,
        conn=connection,
        start_url=start_url if start_url is not None else default_start_url(cfg),
        # 视觉读题单独一套模型；为空时编排层自动回落到判题链
        vision_providers=vision_providers or None,
        # P11：给了目标 ID 就附加到用户正在用的目标；否则退回靶场兜底。
        # 显式注入的 target_source 优先（单测用它换掉真实浏览器）。
        target_source=target_source
        if target_source is not None
        else build_target_source(cfg),
        target_id=cfg.target_id,
    )
