"""提交范围（``submit_scope``）：**开局定死一次，全程照它走**（2026-09-30 收口）。

**真实故障 I（2026-09-28）**：作业页 44 题纵向排列、右上角**唯一**一个「交卷」按钮，
而程序按「每题提交」的思路在做 —— 第 1 题作答完就去点了交卷。留痕
``logs/de615cf6d625`` 显示：

    {"kind":"submit", "ok":true, "elapsed_ms":70}          ← 点击确实发出去了
    verify.json: "region_mad=0.06"（阈值 ≥2）              ← 画面纹丝不动

点得**准**（``ink_centroid`` 落在按钮里）却毫无反应 —— 因为 ``0/44题`` 时交卷
按钮是被禁用的。题目于是以 ``submit_timeout`` 停下。所以问题不在「点哪儿」，
在**点得不是时候**：提交是不可逆动作，整卷页面上它必须推迟到全部做完。

**真实故障 II（2026-09-29，``logs/10ca5ee8c89e``）**：修好「什么时候提交」之后
又出了这一条 —— 旧版每读一屏就重新判一次范围，而「这一屏没有提交按钮」在整卷
页面上是**正常形态**（交卷按钮常常只在最后一屏才露出来）。于是程序做到一半就
按「找不到提交按钮」停下等人。现在范围只在开局裁决一次，与某一屏有没有按钮**无关**。

本文件钉住四件事：

1. 观测里的范围能正确解析；**含糊说法一律不猜**（猜错的方向是提前交卷）；
2. 范围来自**开局方案**（``RunPlan.submit_scope``）：``PAPER`` 中途一次都不交、
   收尾才交一次；``QUESTION`` 每题交一次；
3. 整卷页面**不会**因为某一屏没有提交按钮而中途暂停 —— 收尾那一屏读到的框能兜住；
4. 没有方案（开局读图失败）时才走**结构性兜底**：一屏读到 ≥2 道题 → 整卷。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core import db
from core.config import RunConfig
from core.enums import QuestionState, SubmitScope, TaskType
from core.events import Event
from core.models import ReadResult, RunPlan
from core.orchestrator import Orchestrator, RunContext, RunDeps
from core.trace import RunLogger
from solve.reader import parse_read_batch
from tests.act_helpers import RecordingBus
from tests.orchestrator_helpers import (
    NEXT_BOX,
    SUBMIT_BOX,
    FakeActuator,
    FakeAdapter,
    FakePage,
    FakePipeline,
    FakeSolver,
    FakeVerifier,
    FakeVisionProvider,
    make_perception,
    make_question,
    seed_vision_geometry,
)

#: 观测里的提交框（与 :data:`tests.orchestrator_helpers.SUBMIT_BOX` 同一份）。
#: 中心 ``(945, 15)`` —— 断言「提交到底发出去没有」时看得到那个坐标。
BOX = list(SUBMIT_BOX)

#: 一份最小的题目回复（``page`` 观测 + 一道题）。
_QUESTION = {
    "index": 1,
    "num_text": "1.",
    "stem": "下列说法正确的是？",
    "qtype": "single",
    "options": [{"label": "A", "text": "甲", "box": [0.05, 0.36, 0.10, 0.03]}],
}


def _screen_text(*, scope: object = None, with_submit: bool = True) -> str:
    """造一份读图回复；``with_submit=False`` 表示画面上**根本没有**提交按钮。

    ``scope=None`` 且 ``with_submit=True`` 表示模型给了按钮、但没说范围 ——
    这是「不知道」，与「本题提交」**不是一回事**。
    """
    page: dict[str, Any] = {"completed": "not_done", "scrolling": True}
    if with_submit:
        submit: dict[str, Any] = {"box": BOX}
        if scope is not None:
            submit["scope"] = scope
        page["submit"] = submit
    return json.dumps({"page": page, "questions": [_QUESTION]}, ensure_ascii=False)


def _observed_scope(scope: object) -> SubmitScope | None:
    """从一份回复里读出**观测到的**提交范围（没有提交块时先断言它真的没有）。"""
    batch = parse_read_batch(_screen_text(scope=scope))
    assert batch is not None and batch.page is not None and batch.page.submit is not None
    return batch.page.submit.scope


# --------------------------------------------------------------------------- #
# 一、解析：认得准，且**拿不准就不猜**
# --------------------------------------------------------------------------- #
def test_named_scopes_are_parsed() -> None:
    """``paper`` / ``question`` 两个取值必须对得上 —— 整卷还是每题，差别不可逆。"""
    assert _observed_scope("paper") is SubmitScope.PAPER
    assert _observed_scope("question") is SubmitScope.QUESTION


def test_scope_is_case_insensitive_and_accepts_chinese() -> None:
    """模型可能写 ``"Paper"``、``"整卷"`` 这类形态 —— 能对上就给对应值。"""
    assert _observed_scope("Paper") is SubmitScope.PAPER
    assert _observed_scope("整卷") is SubmitScope.PAPER
    assert _observed_scope("本题") is SubmitScope.QUESTION


def test_absent_scope_stays_none() -> None:
    """模型没给范围 → ``None``，**不是** ``QUESTION``（拿不准不许当「本题提交」）。"""
    assert _observed_scope(None) is None
    assert _observed_scope("") is None


def test_ambiguous_wording_is_not_guessed() -> None:
    """含糊说法一律 ``None``。

    ``"submit"`` 既可能是「提交本题」也可能是「交卷」—— 猜成 ``QUESTION``
    会在整卷页面上**每做一题交一次卷**。拿不准就交回上层走结构性兜底。
    """
    for vague in ("submit", "确认", "yes", "unknown", 123, ["paper"]):
        assert _observed_scope(vague) is None, vague


def test_no_submit_button_observation_stays_none() -> None:
    """画面上**没有**提交按钮 → ``page.submit`` 为 ``None``。

    这一条是「整卷页面中途不应暂停」的解析层前提：没有按钮与「本题提交」必须
    区分开 —— 否则整卷页面每一屏都会被判成「这题提交不了」。
    """
    batch = parse_read_batch(_screen_text(with_submit=False))
    assert batch is not None and batch.page is not None
    assert batch.page.submit is None


# --------------------------------------------------------------------------- #
# 二、判定：范围来自**开局方案**，没有方案才走结构性兜底
# --------------------------------------------------------------------------- #
def _scope_of(plan: RunPlan | None, batch_max: int) -> SubmitScope:
    """直接问那一个判据 —— 它只读两个属性，不必真起一次运行。"""
    orchestrator = object.__new__(Orchestrator)
    orchestrator._plan = plan
    orchestrator._batch_max = batch_max
    return orchestrator._submit_scope_of_run()


def test_plan_scope_wins_over_structure() -> None:
    """方案里写死了范围 → 它就是整轮的唯一依据，与读到过几道题无关。"""
    assert _scope_of(RunPlan(submit_scope=SubmitScope.PAPER), 1) is SubmitScope.PAPER
    assert _scope_of(RunPlan(submit_scope=SubmitScope.QUESTION), 5) is SubmitScope.QUESTION


def test_plan_without_a_scope_falls_back_to_structure() -> None:
    """方案在、但观测没给范围 → 退回结构性兜底（而不是默认「每题提交」）。"""
    assert _scope_of(RunPlan(submit_scope=None), 2) is SubmitScope.PAPER
    assert _scope_of(RunPlan(submit_scope=None), 1) is SubmitScope.QUESTION


def test_without_a_plan_multi_question_screen_implies_paper() -> None:
    """**结构性兜底**：一屏读到过 ≥2 道题 → 整卷。

    题目能在同一屏里连续排列，说明它们共享一个卷面 —— 那种页面上
    **不存在**「每题独立提交」的按钮。这条判据不看模型脸色，是硬事实。
    """
    assert _scope_of(None, 2) is SubmitScope.PAPER
    assert _scope_of(None, 5) is SubmitScope.PAPER


def test_without_a_plan_single_question_screen_keeps_the_old_behaviour() -> None:
    """开局读图失败 + 一屏一题 → ``QUESTION``（**靶场的既有行为，不能变**）。"""
    assert _scope_of(None, 1) is SubmitScope.QUESTION
    assert _scope_of(None, 0) is SubmitScope.QUESTION


def test_recording_the_batch_never_moves_the_scope() -> None:
    """``_note_read_scope`` 只记「一屏最多读到过几道」，**不碰方案**。

    旧版每读一屏就据此改一次范围，于是同一份卷子在不同屏上可能判出不同范围 ——
    那正是「做到一半去交卷」与「整卷页面中途暂停」两条事故的共同根因。
    """
    orchestrator = object.__new__(Orchestrator)
    orchestrator._batch_max = 0
    orchestrator._plan = RunPlan(submit_scope=SubmitScope.QUESTION)

    plain = ReadResult(stem="题", qtype="single", options=[])  # type: ignore[arg-type]
    orchestrator._note_read_scope(plain, 3)
    assert orchestrator._batch_max == 3

    # 后来某次只读到 1 道（重读），不该把「读到过 3 道」这件事抹掉
    orchestrator._note_read_scope(plain, 1)
    assert orchestrator._batch_max == 3

    # 而且范围仍然只听方案的（哪怕刚刚读到过 3 道题）
    assert orchestrator._submit_scope_of_run() is SubmitScope.QUESTION


# --------------------------------------------------------------------------- #
# 三、真跑一轮：范围定死之后，提交次数与时机
# --------------------------------------------------------------------------- #
def _build(
    tmp_path: Path,
    *,
    total: int,
    submit_scope: SubmitScope | None,
    submit_box: tuple[float, float, float, float] | None,
    vision_box: tuple[float, float, float, float] | None = SUBMIT_BOX,
    completed: bool = True,
    next_box: tuple[float, float, float, float] | None = NEXT_BOX,
) -> tuple[Orchestrator, FakeActuator, RecordingBus, Any]:
    """装配一条「假流水线 + 假视觉组」的运行。

    假流水线直接给出读好的题，所以这里验的是**提交时机**：它与「模型怎么读图」
    无关，只与开局裁决出来的方案有关。收尾那一屏的观测由 ``FakeVisionProvider``
    提供 —— 它是整卷提交的闸门（``completed`` 决定「能不能交」）。
    """
    pipeline = FakePipeline([make_perception(index) for index in range(1, total + 1)])
    vision = FakeVisionProvider(
        completed=completed, submit_box=vision_box, submit_scope=SubmitScope.PAPER
    )
    solver = FakeSolver(providers=[vision])
    actuator = FakeActuator()
    verifier = FakeVerifier()
    bus = RecordingBus()
    conn = db.init_db(tmp_path / "autolearn.db")
    cfg = RunConfig(auto_apply=True, task_sequence=[TaskType.QUIZ])
    ctx = RunContext(run_id="run-scope", cfg=cfg, started_at=datetime.now(UTC))
    deps = RunDeps(
        page=FakePage(),
        adapter=FakeAdapter(),
        pipeline=pipeline,  # type: ignore[arg-type]
        solver=solver,  # type: ignore[arg-type]
        run_logger=RunLogger("run-scope", root=tmp_path / "logs"),
        bus=bus,
        conn=conn,
        probe_timeout_s=0.1,
        actuator_factory=lambda _item: actuator,
        verifier_factory=lambda _item: verifier,
    )
    orchestrator = Orchestrator(ctx, deps=deps)
    # 方案走**真的** ``derive_plan``（只是喂给它替身造出来的观测），
    # 再把两个必须由调用方指定的字段钉死 —— 见 ``seed_vision_geometry`` 的说明。
    seed_vision_geometry(
        orchestrator,
        *(make_question(index) for index in range(1, total + 1)),
        next_box=next_box,
        submit_scope=submit_scope,
        submit_box=submit_box,
        total=total,
    )
    return orchestrator, actuator, bus, conn


def _quiz_actions(actuator: FakeActuator) -> list[str]:
    """只取作答类动作（选项 / 提交）；推进用的 ``click`` 不属于提交范围问题。"""
    return [name for name in actuator.order() if name in {"select_option", "submit"}]


def _states(conn: Any) -> list[str]:
    return [row["state"] for row in conn.execute("SELECT state FROM task_item").fetchall()]


async def test_paper_scope_submits_once_at_the_end(tmp_path: Path) -> None:
    """``PAPER``：两题都做完之前**一次都不提交**，收尾只交一次。

    顺序就是证据：``submit`` 出现在两个 ``select_option`` 之后，
    而那之间隔着整个推进与收尾确认。
    """
    orchestrator, actuator, _bus, conn = _build(
        tmp_path, total=2, submit_scope=SubmitScope.PAPER, submit_box=SUBMIT_BOX
    )

    await orchestrator.run()

    assert _quiz_actions(actuator) == ["select_option", "select_option", "submit"], (
        f"整卷两题做完才交一次，实际 {actuator.order()}"
    )
    assert actuator.count("submit") == 1
    # 整卷只有**一个**提交动作，所以结果回读也只有一次 —— 它挂在那道用来定位
    # 「交卷」的题上（最后作答的那道），其余题如实停在「已选未交」。
    # 这两条与「提交次数」正是本用例要的全部：**中途一次都没交**。
    assert set(_states(conn)) <= {QuestionState.APPLIED.value, QuestionState.VERIFIED.value}
    assert QuestionState.VERIFIED.value in _states(conn), "收尾那次提交必须回读成功"
    assert orchestrator._paper_submitted is True
    assert orchestrator.paused_by is None


async def test_question_scope_submits_each_question(tmp_path: Path) -> None:
    """``QUESTION``：每题作答完就提交一次（靶场形态，行为不变）。"""
    orchestrator, actuator, _bus, conn = _build(
        tmp_path, total=2, submit_scope=SubmitScope.QUESTION, submit_box=SUBMIT_BOX
    )

    await orchestrator.run()

    assert _quiz_actions(actuator) == [
        "select_option",
        "submit",
        "select_option",
        "submit",
    ], f"每题提交，实际 {actuator.order()}"
    assert actuator.count("submit") == 2
    assert _states(conn) == [QuestionState.VERIFIED.value] * 2


async def test_paper_screen_without_a_submit_button_never_pauses_mid_run(
    tmp_path: Path,
) -> None:
    """**整卷页面不会因为「开局那屏没有提交按钮」而中途暂停**（用户报的 bug）。

    构造正是真机 ``logs/10ca5ee8c89e`` 的形状：范围内的提交框在开局那屏**看不到**
    （``plan.submit_box is None``），只有收尾那一屏才露出来。

    提交按钮的框有三个来源，按**范围**排序（2026-09-30）：
    ``PAPER → [方案, 收尾那一屏 ``_end_submit``, 最近一次读图 ``_last_submit``]``；
    ``QUESTION → [方案, 最近一次读图, 收尾那一屏]``。
    三来源的优先级由 ``tests/test_advance_calibration.py`` 逐条钉着，这里不重复；
    本用例只钉它对外的那句承诺：**缺按钮不等于停下**，收尾仍交得成。
    """
    orchestrator, actuator, bus, conn = _build(
        tmp_path,
        total=1,
        submit_scope=SubmitScope.PAPER,
        submit_box=None,  # 开局那屏没有提交按钮 → 方案里没有框
        vision_box=SUBMIT_BOX,  # 收尾那一屏读到了 → ``_end_submit`` 兜住
        completed=True,
    )

    await orchestrator.run()

    assert orchestrator.paused_by is None, (
        f"这一屏没有提交按钮不该让整卷页面中途停下，实际 {orchestrator.paused_by!r}"
    )
    assert [p["reason"] for p in bus.payloads(Event.RUN_PAUSED)] == []
    assert orchestrator._end_submit is not None, "收尾那一屏读到的框必须被记下来"
    assert actuator.count("submit") == 1, f"收尾仍要交一次，实际 {actuator.order()}"
    assert _states(conn) == [QuestionState.VERIFIED.value]
    row = conn.execute("SELECT status FROM run").fetchone()
    assert row["status"] == "finished"


async def test_paper_scope_submits_nothing_when_completion_is_not_confirmed(
    tmp_path: Path,
) -> None:
    """收尾确认答「没做完」→ 即使方案里有提交框，也**一次都不交**。

    交卷不可逆：宁可让人自己按那一下，也不能由程序替他在「可能还差几题」时按下去。
    这一条与上一条一起把「不因为缺按钮而停」与「不该交时绝不交」两端都钉住。

    ``next_box=None``（方案=滚动）是刻意的：滚动在这个页面替身上一定推不动，
    于是走到「推不动 → 收尾确认 → 说没做完 → 停下等人」那条正常出口；
    若给一个「下一题」控件，替身页面取不到指纹会一直判「推成功了」，
    反而测不到这条出口。
    """
    orchestrator, actuator, _bus, conn = _build(
        tmp_path,
        total=1,
        submit_scope=SubmitScope.PAPER,
        submit_box=SUBMIT_BOX,
        completed=False,
        next_box=None,
    )

    await orchestrator.run()

    assert actuator.count("submit") == 0, f"没确认做完就不许交卷，实际 {actuator.order()}"
    assert orchestrator._completion_confirmed is False
    assert orchestrator.paused_by == "advance_failed"
    assert _states(conn) == [QuestionState.APPLIED.value], "答案留在「已选未交」，人补完还能交"
