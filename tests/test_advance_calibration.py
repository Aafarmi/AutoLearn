"""开局判定与收尾确认（``Orchestrator._plan_run`` / ``_confirm_completion``）。

这一层管的是**两个时刻**，两者都只有一次、都由程序裁决而不是模型说了算：

* **开局**：读一屏 → 把视觉组的观测裁决成唯一一份运行方案（``core.run_plan``）。
  失败允许（没有 provider / 截图失败 / 模型答不出来）——**不暂停、不抛异常**，
  只留一条日志；主循环随后会用同一套读题逻辑给出更准的原因。
* **收尾**：读一屏 → 看 ``page.completed``。只有 ``ALL_DONE`` 才允许收工；
  问不成（没 provider / 截图失败 / 没有观测块）一律按**未确认**处理。
  这是全流程唯一防跳题的闸门：把它当成「做完了」，后面所有题都不会被作答，
  而且从界面上完全看不出来。

还有一条几何纪律：``_end_submit`` 里的**框与尺寸必须来自同一次截图**。
2026-09-29 的「点歪了」事故就是两个来源混用造成的 —— 框来自上一屏、尺寸来自这一屏，
换算出来的像素位置整体偏移。这条在这里被钉死。

全部用内存替身，所以永不 skip。
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from core import db
from core.config import GuardThresholds, RunConfig
from core.enums import AdvanceMethod, ProbeName, QuestionState, SubmitScope, TaskType
from core.events import Event
from core.models import Question, RunPlan, TaskItem
from core.orchestrator import Orchestrator, RunContext, RunDeps
from core.trace import RunLogger
from solve.providers.base import RateLimitError
from tests.orchestrator_helpers import (
    IMAGE_SIZE,
    NEXT_BOX,
    SUBMIT_BOX,
    FakeActuator,
    FakeAdapter,
    FakePage,
    FakePipeline,
    FakeSolver,
    FakeVerifier,
    FakeVisionProvider,
    RecordingBus,
    make_question,
    make_read_result,
)

#: 第二次收尾确认那一屏的提交框（与 :data:`SUBMIT_BOX` 明确不同，位置差半个屏）。
SECOND_SUBMIT_BOX = (0.10, 0.20, 0.30, 0.10)


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def _guards(**overrides: Any) -> GuardThresholds:
    """把「等页面变化」的预算压到几十毫秒 —— 用例不该为了等超时慢上三秒。"""
    base: dict[str, Any] = {"advance_change_timeout_ms": 80, "advance_change_poll_ms": 20}
    base.update(overrides)
    return GuardThresholds(**base)


def _build(
    tmp_path: Path,
    *,
    vision: FakeVisionProvider | None = None,
    solver: FakeSolver | None = None,
    pipeline: FakePipeline | None = None,
    guards: GuardThresholds | None = None,
    bus: RecordingBus | None = None,
    actuator: FakeActuator | None = None,
    run_id: str = "run-calibration",
) -> tuple[Orchestrator, dict[str, Any]]:
    """装一套「无浏览器」的编排层：截图走假探针，视觉链走假 provider。

    ``actuator`` 传进来时**所有条目共用同一个替身**（默认是每次新建一个）——
    提交框那样「点了哪里」的断言要看记账，必须拿到同一个实例。
    提交级限速也在这里短路掉：真实实现是 5~15s 的 sleep，用例不该真的等它。
    """

    async def _no_gap() -> None:
        return None

    vision = vision if vision is not None else FakeVisionProvider(completed=True)
    solver = solver if solver is not None else FakeSolver(providers=[vision])
    pipeline = pipeline if pipeline is not None else FakePipeline()
    page = FakePage()
    conn = db.init_db(tmp_path / "autolearn.db")
    cfg = RunConfig(
        auto_apply=True,
        task_sequence=[TaskType.QUIZ],
        guards=guards if guards is not None else _guards(),
    )
    ctx = RunContext(run_id=run_id, cfg=cfg, started_at=datetime.now(UTC))
    deps = RunDeps(
        page=page,
        adapter=FakeAdapter(),
        pipeline=pipeline,
        solver=solver,
        run_logger=RunLogger(run_id, root=tmp_path / "logs"),
        bus=bus,
        conn=conn,
        actuator_factory=(
            (lambda _item: actuator) if actuator is not None else (lambda _item: FakeActuator())
        ),
        verifier_factory=lambda _item: FakeVerifier(),
        submit_gap=_no_gap,
        probe_timeout_s=0.1,
    )
    return Orchestrator(ctx, deps=deps), {
        "pipeline": pipeline,
        "page": page,
        "conn": conn,
        "bus": bus,
        "vision": vision,
    }


def _raw_png(width: int, height: int) -> bytes:
    """造一张**真的** PNG —— 探针给的尺寸由图片本身决定，占位串会让截图这一步炸掉。"""
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _read_payload(num_text: str, *, index: int = 1) -> dict[str, Any]:
    """一屏一道题的读图回复（与 ``prompts/10-视觉组.md`` 的固定格式同构）。"""
    return {
        "index": index,
        "num_text": num_text,
        "qtype": "single",
        "stem": f"第 {num_text} 题：下列说法正确的是？",
        "options": [{"label": "A", "text": "甲", "box": [0.05, 0.36, 0.56, 0.03]}],
    }


def _seed_current(orchestrator: Orchestrator, question: Question) -> None:
    """摆好「当前正在做的那道题」—— 收尾确认的缓存键就是它的 ``qid``。"""
    item = TaskItem(
        item_id=f"run-calibration-{question.qid}",
        type=TaskType.QUIZ,
        qid=question.qid,
        state=QuestionState.VERIFIED,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    orchestrator.items.append(item)
    orchestrator._current = item
    orchestrator._vision_reads[question.qid] = (make_read_result(question), IMAGE_SIZE)


class _NoProbePipeline(FakePipeline):
    """探针取不到 → ``_vision_frame`` 直接回 ``None``（截图这一路失败）。"""

    def probe(self, name: ProbeName) -> Any:
        return None


class _GarbageVision(FakeVisionProvider):
    """答非所问的视觉 provider（模型没回固定格式）。"""

    def reply(self) -> dict[str, Any]:
        return {}


class _RawPageVision(FakeVisionProvider):
    """原样回一份 ``page`` 观测块。

    为什么需要它：``FakeVisionProvider`` 的 ``completed`` 只有布尔两态，
    给不出 ``UNKNOWN``（「看不出来」）与「根本没有 page 块」这两种**真实**观测，
    而收尾闸门对它们的处置正是最要紧的那条（未确认）。
    """

    def __init__(self, *, page: dict[str, Any] | None, questions: list[dict[str, Any]] | None = None) -> None:
        super().__init__(questions=questions)
        self._page = page

    def reply(self) -> dict[str, Any]:
        payload = super().reply()
        if self._page is None:
            payload.pop("page", None)
        else:
            payload["page"] = dict(self._page)
        return payload


def _log_events(bus: RecordingBus) -> list[dict[str, Any]]:
    return [payload for name, payload in bus.events if name == Event.LOG_LINE]


def _completion_events(bus: RecordingBus) -> list[dict[str, Any]]:
    return [payload for name, payload in bus.events if name == Event.ADVANCE_COMPLETION_CHECK]


# --------------------------------------------------------------------------- #
# 1. 开局判定：四条路
# --------------------------------------------------------------------------- #
async def test_plan_run_derives_a_plan_from_one_screen(tmp_path: Path) -> None:
    """成功那条路：一屏观测 → 一份方案 + 一个事件（用户要看得懂它打算怎么走）。

    这一次读图**同时**产出方案与首屏题，所以它不能白花：观测留在 ``_last_batch``
    （收尾确认还要用它），尺寸留在 ``_plan_size``（落点换算的基准）。
    """
    bus = RecordingBus()
    vision = FakeVisionProvider(
        next_box=NEXT_BOX,
        total=20,
        current=3,
        submit_scope=SubmitScope.QUESTION,
        questions=[_read_payload("3.")],
    )
    orchestrator, env = _build(tmp_path, vision=vision, bus=bus)

    await orchestrator._plan_run(env["page"])

    batch = orchestrator._last_batch
    assert batch is not None and batch.page is not None
    assert orchestrator._plan is not None
    assert orchestrator._plan.method.value == "click"
    assert orchestrator._plan.total == 20
    assert orchestrator._plan_size == IMAGE_SIZE
    assert orchestrator._planned is True

    calibrated = [p for name, p in bus.events if name == Event.ADVANCE_CALIBRATED]
    assert len(calibrated) == 1
    assert calibrated[0]["total"] == 20 and calibrated[0]["current"] == 3
    assert vision.calls == ["read"]


async def test_a_page_only_opening_screen_still_yields_a_plan(tmp_path: Path) -> None:
    """开局那一屏只有 ``page`` 观测、**没有一道完整题目**时，仍然要裁决出方案。

    这条曾经是反的（当时的实现把 ``_last_batch`` 记在「至少一道题过门禁」之后），
    后果是：**读到了页面却给不出方案** —— 「只有进度条 / 答题卡、题目被画面切掉」
    的开局屏会让整条运行永远在 ``no_plan`` 上打转，每个坐标都不点，
    最后停在收尾确认等人。而 ``page`` 是**页面级**的事实，
    和「这一屏有没有读到题」本来就无关（2026-09-30 修）。

    所以这里钉住的是**解耦**：观测照样留下、方案照样裁决、
    只是这次的 ``questions`` 为空（那由主循环照常按 ``vision_read_empty`` 处置）。
    """
    bus = RecordingBus()
    vision = FakeVisionProvider(next_box=NEXT_BOX, total=20, current=3, questions=[])
    orchestrator, env = _build(tmp_path, vision=vision, bus=bus)

    await orchestrator._plan_run(env["page"])

    assert orchestrator._plan is not None, "只有观测、没有题目，也要给得出方案"
    assert orchestrator._plan.method.value == "click"
    assert orchestrator._plan.total == 20 and orchestrator._plan.current == 3
    assert orchestrator._plan_size == IMAGE_SIZE, "落点换算要用这一屏的尺寸"
    assert orchestrator._card_number == orchestrator._plan.card_start_number == 3, (
        "题号链从观测到的当前题号起算"
    )
    batch = orchestrator._last_batch
    assert batch is not None and batch.page is not None, "观测块必须留下来"
    assert batch.questions == []
    calibrated = [p for name, p in bus.events if name == Event.ADVANCE_CALIBRATED]
    assert len(calibrated) == 1
    assert vision.calls == ["read"], "图读了、模型也答了，只是这一批里没有完整题目"
    assert orchestrator.paused_by is None, "开局判定本身不暂停"


async def test_the_opening_read_keeps_every_question_of_that_screen(tmp_path: Path) -> None:
    """开局那一次读图的首屏第 1 题**也要留在队列里**，不许被丢掉。

    ``_read_question_by_vision`` 的约定是：把同屏**其余**题入队、把第一道**返回**给调用方。
    主循环先取队列（``_take_queued_question``），所以**不接返回值的调用方必须自己把它放回队列** ——
    否则「一屏多题」时首屏第 1 题会被整轮**静默跳过**，而单题屏则会被**再读一遍**
    （白花一次模型调用）。这条与 ``_scroll_reveal_next`` 早就有的那句是同一个约定。
    """
    bus = RecordingBus()
    vision = FakeVisionProvider(
        next_box=NEXT_BOX,
        total=20,
        current=3,
        questions=[_read_payload("3.", index=1), _read_payload("4.", index=2)],
    )
    orchestrator, env = _build(tmp_path, vision=vision, bus=bus)

    await orchestrator._plan_run(env["page"])

    queued = orchestrator._pending_reads
    assert len(queued) == 2, "一屏两道题 → 两道都在队列里（含返回的那一道）"
    assert len({item.qid for item in queued}) == 2, "两道是不同的题"
    assert all(item.skill_id == "single_choice" for item in queued)
    assert all(item.skill_error is None for item in queued)
    for item in queued:
        assert item.qid in orchestrator._vision_reads, "队列里的几何必须已经缓存好"
    assert vision.calls == ["read"], "只读了一次图：首题不该被当成「队列为空」再读一遍"


async def test_plan_run_without_a_frame_stays_alive(tmp_path: Path) -> None:
    """截图拿不到（探针取不到 / 页面正在导航）→ **不暂停、不抛异常**，只留日志。

    开局判定失败**不许终止任务**：没有方案时推进按「一个坐标都不点」处理，
    而用户看到的暂停理由由主循环给出（``vision_read_failed``），比这里笼统地停下准确。
    """
    bus = RecordingBus()
    vision = FakeVisionProvider(completed=True)
    orchestrator, env = _build(
        tmp_path, vision=vision, pipeline=_NoProbePipeline(), bus=bus
    )

    await orchestrator._plan_run(env["page"])

    assert orchestrator._plan is None
    assert orchestrator.paused_by is None and orchestrator.paused is False
    assert vision.calls == [], "连图都没有，就不该去问模型"
    skip = [p for p in _log_events(bus) if p.get("advance_calibrate") == "failed"]
    assert len(skip) == 1
    assert skip[0]["reason"], "失败也要说清原因（排障的第一手线索）"
    assert skip[0]["read_error"] == "no_frame"


async def test_plan_run_exposes_real_rate_limit_error_for_operator(tmp_path: Path) -> None:
    class RateLimitedVision(FakeVisionProvider):
        async def complete(self, req: Any) -> Any:
            if req.images:
                raise RateLimitError("temporary 429")
            return await super().complete(req)

    bus = RecordingBus()
    vision = RateLimitedVision()
    orchestrator, env = _build(tmp_path, vision=vision, bus=bus)

    await orchestrator._plan_run(env["page"])

    failed = [p for p in _log_events(bus) if p.get("advance_calibrate") == "failed"]
    assert failed and failed[0]["read_error"] == "rate_limited"
    assert "检查额度" in failed[0]["hint"]


async def test_plan_run_with_an_unparsable_reply_stays_alive(tmp_path: Path) -> None:
    """模型答非所问 → 同样只是「没有方案」，不暂停、不抛异常。

    读图失败是**正常形态**（模型没答好、图太糊），把它当致命错误会让整批题都跑不了。
    """
    bus = RecordingBus()
    vision = _GarbageVision()
    orchestrator, env = _build(tmp_path, vision=vision, bus=bus)

    await orchestrator._plan_run(env["page"])

    assert orchestrator._plan is None
    assert orchestrator.paused_by is None
    assert vision.calls == ["read"], "问了（这次确实问了），只是答复用不了"
    failed = [p for p in _log_events(bus) if p.get("advance_calibrate") == "failed"]
    assert failed
    assert failed[0]["read_error"] == "read_parse_failed"
    assert failed[0]["hint"]


async def test_plan_run_without_any_vision_provider_only_logs(tmp_path: Path) -> None:
    """一条视觉 provider 都没有（用户没配模型）→ 只发一条日志，``_plan is None``。

    这是**配置问题**，不是运行期可以靠重试解决的事：说清「没有办法确认 / 读取」，
    让用户去设置里选模型。绝不默认收工，也绝不在这里抛异常。
    """
    bus = RecordingBus()
    orchestrator, env = _build(tmp_path, solver=FakeSolver(providers=[]), bus=bus)

    await orchestrator._plan_run(env["page"])

    assert orchestrator._plan is None
    assert orchestrator.paused_by is None and orchestrator.paused is False
    skipped = [p for p in _log_events(bus) if p.get("advance_calibrate") == "skipped"]
    assert len(skipped) == 1
    assert "视觉模型" in skipped[0]["reason"], "要说清是「没有可用的视觉模型」"

    # 同一条环境里收尾确认也必须如实报「问不成」，而不是收工
    assert await orchestrator._confirm_completion(env["page"], "stuck") is False
    checks = _completion_events(bus)
    assert checks and checks[0]["completed"] is False
    assert "视觉模型" in checks[0]["reason"]


async def test_plan_run_can_be_switched_off(tmp_path: Path) -> None:
    """``advance_calibrate=False`` → 既不读图也不产生方案（守卫是「别花那次调用」）。"""
    vision = FakeVisionProvider(completed=True, next_box=NEXT_BOX)
    orchestrator, env = _build(tmp_path, vision=vision, guards=_guards(advance_calibrate=False))

    await orchestrator._plan_run(env["page"])

    assert orchestrator._plan is None
    assert vision.calls == []
    assert env["pipeline"].vision.calls == 0, "守卫关掉 → 连截图都不做"


# --------------------------------------------------------------------------- #
# 2. 收尾确认：只有 ALL_DONE 才放行
# --------------------------------------------------------------------------- #
async def test_confirm_completion_records_the_submit_box_of_that_screen(tmp_path: Path) -> None:
    """``ALL_DONE`` → True，并把**这一屏**的提交框与尺寸记进 ``_end_submit``。

    为什么要顺手记：整卷的「交卷」按钮常常只在收尾那一屏露出来（开局那屏它在页脚之外），
    而收尾本来就要读一张图 —— 不记下来就永远拿不到它的坐标。
    """
    bus = RecordingBus()
    vision = FakeVisionProvider(
        completed=True, submit_box=SUBMIT_BOX, submit_scope=SubmitScope.PAPER
    )
    orchestrator, env = _build(tmp_path, vision=vision, bus=bus)
    _seed_current(orchestrator, make_question(1))

    assert await orchestrator._confirm_completion(env["page"], "stuck") is True

    assert orchestrator._end_submit == (SUBMIT_BOX, IMAGE_SIZE)
    assert orchestrator._completion_confirmed is True
    checks = _completion_events(bus)
    assert len(checks) == 1
    assert checks[0]["trigger"] == "stuck"
    assert checks[0]["completed"] is True
    assert checks[0]["observed"] == "all_done"


@pytest.mark.parametrize(
    ("vision_factory", "observed"),
    [
        pytest.param(lambda: FakeVisionProvider(completed=False), "not_done", id="not-done"),
        pytest.param(
            lambda: _RawPageVision(
                page={"completed": "unknown", "submit": {"box": list(SUBMIT_BOX)}, "reason": "看不出来"}
            ),
            "unknown",
            id="unknown",
        ),
    ],
)
async def test_confirm_completion_is_false_unless_all_done(
    tmp_path: Path, vision_factory: Any, observed: str
) -> None:
    """``NOT_DONE`` 与 ``UNKNOWN`` 都不许收工 —— 默认方向必须是「不放行」。

    两侧代价不对称：把「还没做完」当成做完 = 后面所有题静默不作答；
    把「做完了」当成没做完，最坏只是停下来让人看一眼。
    """
    bus = RecordingBus()
    orchestrator, env = _build(tmp_path, vision=vision_factory(), bus=bus)
    _seed_current(orchestrator, make_question(1))

    assert await orchestrator._confirm_completion(env["page"], "stuck") is False

    assert orchestrator._completion_confirmed is False
    checks = _completion_events(bus)
    assert len(checks) == 1
    assert checks[0]["completed"] is False
    assert checks[0]["observed"] == observed


async def test_confirm_completion_counts_a_failed_screenshot_as_unconfirmed(tmp_path: Path) -> None:
    """截图失败 → **未确认**（连问都没法问）→ 停下等人，不许当成做完了。

    这条判据存在的理由只有一个：收工判据必须来自**看过画面**。
    拿「问不成」当「做完了」，代价是后面所有题都不作答。
    """
    bus = RecordingBus()
    vision = FakeVisionProvider(completed=True)
    orchestrator, env = _build(
        tmp_path, vision=vision, pipeline=_NoProbePipeline(), bus=bus
    )
    _seed_current(orchestrator, make_question(1))

    assert await orchestrator._confirm_completion(env["page"], "stuck") is False

    assert vision.calls == [], "连截图都拿不到，就不该去问"
    checks = _completion_events(bus)
    assert len(checks) == 1
    assert checks[0]["completed"] is False
    assert "截图" in checks[0]["reason"], "理由要说清是「没看成」而不是「没做完」"


async def test_confirm_completion_without_a_page_block_is_unconfirmed(tmp_path: Path) -> None:
    """答复里**没有 page 观测块** → 未确认（并说清原因）。

    收尾判据住在 ``page`` 里；没有这一块就等于这次答复回答不了「做完了吗」，
    哪怕它抄回来了一整道题。
    """
    bus = RecordingBus()
    vision = _RawPageVision(page=None, questions=[_read_payload("9.")])
    orchestrator, env = _build(tmp_path, vision=vision, bus=bus)
    _seed_current(orchestrator, make_question(1))

    assert await orchestrator._confirm_completion(env["page"], "stuck") is False

    checks = _completion_events(bus)
    assert len(checks) == 1
    assert checks[0]["completed"] is False
    assert "no_page_observation" in checks[0]["reason"]
    assert orchestrator._end_submit is None, "没有观测块就没有可记的提交框"


async def test_the_same_trigger_is_only_asked_once(tmp_path: Path) -> None:
    """同一题 + 同一触发点**只问一次**（缓存）。

    没有这条纪律就会出现「推不动 → 问 → 说没完成 → 再推 → 再问」的绕圈，
    每绕一圈都是一次真实的模型调用与一次截图。
    """
    bus = RecordingBus()
    vision = FakeVisionProvider(completed=False)
    orchestrator, env = _build(tmp_path, vision=vision, bus=bus)
    _seed_current(orchestrator, make_question(1))

    assert await orchestrator._confirm_completion(env["page"], "stuck") is False
    assert await orchestrator._confirm_completion(env["page"], "stuck") is False

    assert vision.calls == ["read"], "第二次必须命中缓存，不再花钱"
    assert len(_completion_events(bus)) == 1, "缓存命中也不该再发一条事件"


async def test_end_submit_box_and_size_come_from_the_same_frame(tmp_path: Path) -> None:
    """``_end_submit`` 的**框与尺寸必须来自同一次截图**。

    2026-09-29 的「点歪了」事故就是这么来的：框留着上一屏的、尺寸换了这一屏的，
    归一化 → 像素的换算整体偏移，点下去的位置与按钮差半个屏。
    这条用例造两屏尺寸不同、观测到的框也不同的截图，断言记下的是**第二屏自己的**那一对。
    """
    vision = FakeVisionProvider(
        completed=True, submit_box=SUBMIT_BOX, submit_scope=SubmitScope.PAPER
    )
    orchestrator, env = _build(tmp_path, vision=vision)
    _seed_current(orchestrator, make_question(1))
    pipeline = env["pipeline"]

    assert await orchestrator._confirm_completion(env["page"], "stuck") is True
    assert orchestrator._end_submit == (SUBMIT_BOX, IMAGE_SIZE)

    # 第二屏：图尺寸变了，**同一屏里**观测到的提交框也变了
    pipeline.vision.png = _raw_png(800, 640)
    vision.submit_box = SECOND_SUBMIT_BOX
    assert await orchestrator._confirm_completion(env["page"], "reached_total") is True

    recorded = orchestrator._end_submit
    assert recorded is not None
    box, size = recorded
    assert size == (800, 640), "尺寸必须是**第二屏**的，不是上一屏的"
    assert box == pytest.approx(SECOND_SUBMIT_BOX), "框必须与尺寸来自同一次截图"
    assert size != IMAGE_SIZE and box != pytest.approx(SUBMIT_BOX)
    assert vision.calls == ["read", "read"], "两个触发点各问一次"


# --------------------------------------------------------------------------- #
# 4. 提交按钮的框：三个来源，按范围排序
# --------------------------------------------------------------------------- #
#: 「最近一次读图」看到的提交框（每题范围要用的就是它）。
LAST_BOX = (0.20, 0.80, 0.12, 0.05)
#: 那一屏的尺寸 —— 与 :data:`SUBMIT_BOX` 那屏**不同**，专门用来验「框与尺寸同源」。
LAST_SIZE = (800, 640)


def _seed_plan(orchestrator: Orchestrator, scope: SubmitScope, *, box: Any = None) -> TaskItem:
    """摆好「开局方案 + 当前这道题（选项已点、待提交）」，并返回那个条目。

    状态必须是 ``APPLIED``：``VERIFIED → SUBMITTED`` 是**非法迁移**
    （状态机不允许跳过「已作答」），提交这条路本来就只在 ``APPLIED`` 之后走。
    """
    question = make_question(1)
    _seed_current(orchestrator, question)
    orchestrator._plan = RunPlan(method=AdvanceMethod.CLICK, submit_scope=scope, submit_box=box)
    orchestrator._plan_size = IMAGE_SIZE
    item = orchestrator._current
    assert item is not None
    item.state = QuestionState.APPLIED
    return item


async def test_question_scope_prefers_the_last_read_submit_box(tmp_path: Path) -> None:
    """每题范围的框取**最近一次读图**看到的那个，而不是收尾那一屏的。

    为什么：``QUESTION`` 交的是**当前这道题**，所以按钮必须来自「当前这道题
    所在那一屏」。收尾那一屏的框属于「全部做完之后」那个语境，
    拿它去交一道题是**语义错位**（真机上就是「点歪了」或者点了个禁用的按钮）。
    """
    actuator = FakeActuator()
    orchestrator, _env = _build(tmp_path, actuator=actuator)
    item = _seed_plan(orchestrator, SubmitScope.QUESTION)
    # 三个来源里：方案没有框，最近一次读图有，收尾那一屏也有（不该被采用）
    orchestrator._last_submit = (LAST_BOX, LAST_SIZE)
    orchestrator._end_submit = (SECOND_SUBMIT_BOX, IMAGE_SIZE)

    await orchestrator._submit_and_confirm(item)

    assert orchestrator.paused_by is None, "有框就不该停在 vision_no_submit_box"
    submits = [entry for entry in actuator.boxes if entry[0] == "submit"]
    assert len(submits) == 1, "提交不重放：一次动作只发一次"
    assert submits[0][1] == pytest.approx(LAST_BOX)
    assert submits[0][2] == LAST_SIZE, "框与尺寸必须同源（那一屏自己的尺寸）"


async def test_paper_scope_prefers_the_end_screen_submit_box(tmp_path: Path) -> None:
    """整卷范围的框优先取**收尾那一屏**（「交卷」常常只在最后才露出来）。

    这条正是用户报的那个故障的解药：44 题的整卷页面做到第 16 题时，
    中间几屏根本没有「交卷」按钮 —— 那时**不该暂停**，收尾那一屏会给出来。
    """
    actuator = FakeActuator()
    orchestrator, _env = _build(tmp_path, actuator=actuator)
    item = _seed_plan(orchestrator, SubmitScope.PAPER)
    orchestrator._last_submit = (LAST_BOX, LAST_SIZE)
    orchestrator._end_submit = (SECOND_SUBMIT_BOX, IMAGE_SIZE)

    await orchestrator._submit_and_confirm(item)

    assert orchestrator.paused_by is None
    submits = [entry for entry in actuator.boxes if entry[0] == "submit"]
    assert len(submits) == 1
    assert submits[0][1] == pytest.approx(SECOND_SUBMIT_BOX), "整卷要的是收尾那一屏的「交卷」"
    assert submits[0][2] == IMAGE_SIZE


async def test_the_plan_submit_box_outranks_both_other_sources(tmp_path: Path) -> None:
    """方案里的框（开局定死的那一个）**优先于**另外两个来源。

    它是「程序开局判定」的产物，比运行期临时记下来的更可控：
    同一份方案在整个运行里给的是同一个框，落点不会漂移。
    """
    actuator = FakeActuator()
    orchestrator, _env = _build(tmp_path, actuator=actuator)
    item = _seed_plan(orchestrator, SubmitScope.QUESTION, box=SUBMIT_BOX)
    orchestrator._last_submit = (LAST_BOX, LAST_SIZE)
    orchestrator._end_submit = (SECOND_SUBMIT_BOX, IMAGE_SIZE)

    await orchestrator._submit_and_confirm(item)

    submits = [entry for entry in actuator.boxes if entry[0] == "submit"]
    assert len(submits) == 1
    assert submits[0][1] == pytest.approx(SUBMIT_BOX)
    assert submits[0][2] == IMAGE_SIZE, "方案那一次的框配**方案那一次**的尺寸"


async def test_no_submit_box_anywhere_pauses_with_a_scope_specific_hint(
    tmp_path: Path,
) -> None:
    """三个来源都没有 → **不猜坐标**，停下并说清「你要做什么」。

    猜一个坐标就是一次真实的误点（可能点到别的按钮），而这里缺的只是
    最后一下人工动作 —— 停下来问人的成本远低于点错。
    """
    bus = RecordingBus()
    actuator = FakeActuator()
    orchestrator, _env = _build(tmp_path, bus=bus, actuator=actuator)
    item = _seed_plan(orchestrator, SubmitScope.PAPER)

    await orchestrator._submit_and_confirm(item)

    assert orchestrator.paused_by == "vision_no_submit_box"
    assert not [entry for entry in actuator.boxes if entry[0] == "submit"], "一个坐标都不许点"
    no_box = [p for p in _log_events(bus) if p.get("vision_submit") == "no_box"]
    assert len(no_box) == 1
    assert no_box[0]["scope"] == SubmitScope.PAPER.value
    assert "手动" in no_box[0]["hint"], "整卷范围缺的只是最后一下人工动作，提示要说清"


# --------------------------------------------------------------------------- #
# 5. 整卷提交之后：这一轮已作答的题一起进终态
# --------------------------------------------------------------------------- #
def _applied_items(orchestrator: Orchestrator, count: int) -> list[TaskItem]:
    """造 ``count`` 条「已作答、待确认」的条目（整卷提交前的真实状态）。"""
    made: list[TaskItem] = []
    for index in range(1, count + 1):
        question = make_question(index)
        item = TaskItem(
            item_id=f"run-calibration-{question.qid}",
            type=TaskType.QUIZ,
            qid=question.qid,
            state=QuestionState.APPLIED,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        orchestrator.items.append(item)
        orchestrator._vision_reads[question.qid] = (make_read_result(question), IMAGE_SIZE)
        made.append(item)
    return made


def _seed_paper_submit(orchestrator: Orchestrator, items: list[TaskItem]) -> TaskItem:
    """摆好「整卷 + 已完成确认 + 最后作答的那一条」这套提交前提。"""
    orchestrator._plan = RunPlan(
        method=AdvanceMethod.CLICK,
        submit_scope=SubmitScope.PAPER,
        submit_box=SUBMIT_BOX,
    )
    orchestrator._plan_size = IMAGE_SIZE
    orchestrator._completion_confirmed = True
    last = items[-1]
    orchestrator._last_applied_item_id = last.item_id
    orchestrator._current = last
    return last


async def test_a_confirmed_paper_submit_settles_the_whole_batch(tmp_path: Path) -> None:
    """整卷提交**确认生效**之后，这一轮其余已作答的题也要进终态。

    为什么：整卷只有**一次**提交动作，回读只挂在最后作答的那道题上。其余题若停在
    ``applied``，续跑时 ``is_terminal("applied")`` 为假 → 它们会被**重做一遍** ——
    多选题上重新点一遍选项就是把刚选上的勾**取消**（用户报的「胡乱操作」的一种）。
    """
    bus = RecordingBus()
    actuator = FakeActuator()
    orchestrator, env = _build(tmp_path, bus=bus, actuator=actuator)
    first, _second = _applied_items(orchestrator, 2)
    last = _seed_paper_submit(orchestrator, [first, _second])

    await orchestrator._submit_paper_once(env["page"])

    assert orchestrator.paused_by is None
    assert QuestionState(first.state) is QuestionState.VERIFIED, "先作答的那道也要进终态"
    assert QuestionState(last.state) is QuestionState.VERIFIED
    settled = [p for p in _log_events(bus) if p.get("submit") == "paper_batch_settled"]
    assert len(settled) == 1
    assert settled[0]["items"] == 1, "只统计**其余**那些（最后那道由回读自己收尾）"
    states = [
        p["to"] for p in (payload for name, payload in bus.events if name == Event.TASK_STATE_CHANGED)
    ]
    assert "submitted" in states and "verified" in states, (
        "状态机里 applied 没有直达 verified 的边，必须分两步走"
    )


async def test_a_failed_paper_submit_leaves_the_other_questions_alone(tmp_path: Path) -> None:
    """提交**没成功**时不许settle：这一卷结果未知，保持原状才是诚实的。

    把「未确认」写成「已验证」比少标一条严重得多 —— 它会让人以为答案已经交上去了。
    """
    bus = RecordingBus()
    actuator = FakeActuator(fail_at="submit")
    orchestrator, env = _build(tmp_path, bus=bus, actuator=actuator)
    first, _second = _applied_items(orchestrator, 2)
    _seed_paper_submit(orchestrator, [first, _second])

    await orchestrator._submit_paper_once(env["page"])

    assert orchestrator.paused_by == "submit_failed"
    assert QuestionState(first.state) is QuestionState.APPLIED, "结果未知 → 保持原样"
    assert not [p for p in _log_events(bus) if p.get("submit") == "paper_batch_settled"]
