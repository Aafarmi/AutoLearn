"""「按方案推进」（``Orchestrator._advance``）：**判定一次，之后全按它走**。

要解决的真问题：旧实现每次推进都要再截一张图、再问一次模型「下一题在哪」
（``find_advance_control``），问不到就换一招（点击 → 滚动 → 滑动）。真机上那条路
的表现是「做到一半停下」，而滚动模式下的换招更是把题号从 3 直接推到 16
（中间 13 道题被静默跳过）。

现在的纪律只有一条：**开局裁决一次（``core/run_plan.derive_plan``），运行期只按它执行**。

* 一种方式推不动 → 不换招，请视觉组确认一次「是不是全部完成了」；
* 确认为完成 → 干净收工（``finished``）；确认不了 → ``advance_failed`` 停下等人；
* 没有方案（**开局读图没成**）→ 一个坐标都不点。

全部用内存替身（页面、执行器、视觉链都是假的），所以这些断言永不 skip、也不起浏览器。
页面一律是 ``FakePage``（**没有** ``evaluate``）：页面指纹取不到 → 编排层必须
「只做一次动作」—— 这正是「不许静默跳题」那条退化路径，用它当默认环境最严。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from core import db
from core.config import GuardThresholds, RunConfig
from core.enums import (
    ActionKind,
    AdvanceMethod,
    AdvanceSkill,
    ProbeName,
    QuestionState,
    SubmitScope,
    TaskType,
)
from core.events import Event
from core.models import (
    ActionResult,
    PageCard,
    PageSwipe,
    PageView,
    PerceptionResult,
    Question,
    TaskItem,
)
from core.orchestrator import Orchestrator, RunContext, RunDeps
from core.trace import RunLogger
from solve.reader import parse_read_batch, to_question
from tests.orchestrator_helpers import (
    IMAGE_SIZE,
    NEXT_BOX,
    FakeActuator,
    FakeAdapter,
    FakePage,
    FakePipeline,
    FakeSolver,
    FakeVerifier,
    FakeVisionProvider,
    RecordingBus,
    make_page_view,
    make_question,
    make_read_batch,
    make_read_result,
    seed_vision_geometry,
)

# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
#: 手算用的答题卡：5 列 × 2 行，格宽 0.1 / 格高 0.2。
CARD_BOX = (0.1, 0.1, 0.5, 0.4)
CARD_COLS, CARD_ROWS = 5, 2

#: 第 2 格（第 1 行第 2 列）的落点，**手算**：中心 ``(0.25 × 1000, 0.2 × 600)``。
EXPECTED_SECOND_CELL = (0.21, 0.12, 0.08, 0.16)
EXPECTED_SECOND_XY = (250.0, 120.0)


def _guards(**overrides: Any) -> GuardThresholds:
    """把「等页面变化」的预算压到几十毫秒 —— 用例不该为了等超时慢上三秒。

    ``advance_settle_ms`` 默认置 0：那是「推进后无条件沉淀」的兜底等待，
    与「等页面指纹变化」是两件事，逐条验的用例里没必要真睡 350ms。
    它自己被 :func:`test_advance_settles_before_the_next_screenshot` 单独钉住。
    """
    base: dict[str, Any] = {
        "advance_change_timeout_ms": 80,
        "advance_change_poll_ms": 20,
        "advance_settle_ms": 0,
    }
    base.update(overrides)
    return GuardThresholds(**base)


def _build(
    tmp_path: Path,
    *,
    vision: FakeVisionProvider | None = None,
    solver: FakeSolver | None = None,
    pipeline: FakePipeline | None = None,
    page: FakePage | None = None,
    actuator: FakeActuator | None = None,
    guards: GuardThresholds | None = None,
    bus: RecordingBus | None = None,
    run_id: str = "run-advance",
) -> tuple[Orchestrator, dict[str, Any]]:
    """装一套「无浏览器」的编排层：本文件验的是**推进决策**，不是坐标换算。"""
    vision = vision if vision is not None else FakeVisionProvider(completed=True)
    solver = solver if solver is not None else FakeSolver(providers=[vision])
    pipeline = pipeline if pipeline is not None else FakePipeline()
    page = page if page is not None else FakePage()
    actuator = actuator if actuator is not None else FakeActuator()
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
        actuator_factory=lambda _item: actuator,
        verifier_factory=lambda _item: FakeVerifier(),
        probe_timeout_s=0.1,
    )
    orchestrator = Orchestrator(ctx, deps=deps)
    return orchestrator, {
        "actuator": actuator,
        "pipeline": pipeline,
        "page": page,
        "conn": conn,
        "bus": bus,
        "vision": vision,
    }


def _card(
    *,
    current_box: tuple[float, float, float, float] | None = (0.11, 0.12, 0.08, 0.16),
    next_box: tuple[float, float, float, float] | None = None,
) -> PageCard:
    """5 列 × 2 行的答题卡；默认「当前格」= 第 1 格（配 ``current=1`` 用）。

    ``current_box=None`` 表示模型**说不出当前是第几题**，``next_box`` 表示它
    直接指出「下一题号格在哪」—— 这两件事在真实卷面上经常只发生一件。
    """
    return PageCard(
        box=CARD_BOX,
        cols=CARD_COLS,
        rows=CARD_ROWS,
        current_box=current_box,
        next_box=next_box,
    )


def _cell_box(index: int) -> tuple[float, float, float, float]:
    """第 ``index`` 格（0 基、行优先）的落点框 —— **手算**，用作模型指出的那一格。"""
    row, col = divmod(index, CARD_COLS)
    step_x, step_y = CARD_BOX[2] / CARD_COLS, CARD_BOX[3] / CARD_ROWS
    center_x = CARD_BOX[0] + (col + 0.5) * step_x
    center_y = CARD_BOX[1] + (row + 0.5) * step_y
    width, height = step_x * 0.8, step_y * 0.8
    return (center_x - width / 2.0, center_y - height / 2.0, width, height)


def _seed_current(orchestrator: Orchestrator, question: Question, *, num_text: str | None = None) -> None:
    """把「当前正在做的那道题」摆好。

    ``_advance`` 拿不到当前条目会**直接返回 False**（连收尾确认都不做），
    所以每条推进用例都要先把这一步摆好 —— 它对应真实运行里「上一轮刚做完一道题」。
    """
    item = TaskItem(
        item_id=f"run-advance-{question.qid}",
        type=TaskType.QUIZ,
        qid=question.qid,
        state=QuestionState.VERIFIED,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    orchestrator.items.append(item)
    orchestrator._current = item
    if num_text is not None:
        orchestrator._vision_reads[question.qid] = (
            make_read_result(question).model_copy(update={"num_text": num_text}),
            IMAGE_SIZE,
        )


def _center_px(
    box: tuple[float, float, float, float], size: tuple[int, int]
) -> tuple[float, float]:
    """归一化框 → 像素中心（**手算期望值**用，不依赖实现）。"""
    return ((box[0] + box[2] / 2.0) * size[0], (box[1] + box[3] / 2.0) * size[1])


def _clicks(actuator: FakeActuator) -> list[tuple[str, tuple[float, float, float, float], tuple[int, int]]]:
    """执行器收到的**坐标点击**（选项点击与提交不算推进）。"""
    return [entry for entry in actuator.boxes if entry[0] == "click"]


def _line_events(bus: RecordingBus) -> list[dict[str, Any]]:
    return [payload for name, payload in bus.events if name == Event.LOG_LINE]


# --------------------------------------------------------------------------- #
# 1. 判定一次，之后全按它走：推进过程再也不问模型
# --------------------------------------------------------------------------- #
async def test_three_advances_ask_the_vision_model_nothing(tmp_path: Path) -> None:
    """**旧实现每次推进都要再问一次「控件在哪」，现在一次都不问。**

    这是本次改造最核心的一条。旧路每次都要多截一张图、多花一次模型调用，而且模型
    每次给的框都可能不同（2026-09-29 真机日志里同一个按钮两次差 0.8 个屏宽）——
    「点得偏」与「一次跳过十几道题」都从这里来。
    """
    vision = FakeVisionProvider(completed=False, next_box=NEXT_BOX, total=20)
    orchestrator, env = _build(tmp_path, vision=vision)
    question = make_question(1)
    seed_vision_geometry(orchestrator, question, next_box=NEXT_BOX, total=20, current=1)
    _seed_current(orchestrator, question)

    for _ in range(3):
        assert await orchestrator._advance(env["page"]) is True

    assert env["actuator"].count("click") == 3, "三次推进 = 三次点击（都点开局那一个框）"
    assert vision.calls == [], "推进过程一次都不该问视觉模型"
    assert env["pipeline"].vision.calls == 0, "也不该再截一张图"


# --------------------------------------------------------------------------- #
# 2. CLICK：点开局观测到的那个控件，配开局那张图的尺寸
# --------------------------------------------------------------------------- #
async def test_click_plan_clicks_the_observed_control_with_the_plan_size(tmp_path: Path) -> None:
    """CLICK 方案点的是**开局观测到的那个框**，配**开局那张图的尺寸**。

    尺寸必须同源：归一化框 → 像素的换算基准一旦换成别的图（视口变了、图被缩放过），
    整体就会偏移 —— 而偏移的表现就是「点歪了」。
    """
    orchestrator, env = _build(tmp_path)
    question = make_question(1)
    plan = seed_vision_geometry(orchestrator, question, next_box=NEXT_BOX)
    _seed_current(orchestrator, question)
    assert plan.method is AdvanceMethod.CLICK

    assert await orchestrator._advance(env["page"]) is True

    clicks = _clicks(env["actuator"])
    assert len(clicks) == 1
    _, box, size = clicks[0]
    assert box == pytest.approx(NEXT_BOX)
    assert size == IMAGE_SIZE
    assert ("click", "next:plan") in env["actuator"].calls, "留痕要能看出这一下是「按方案点的」"


async def test_click_is_done_exactly_once_when_the_page_cannot_be_compared(tmp_path: Path) -> None:
    """页面指纹取不到（题干画在 canvas 上）→ **只点一次**。

    判不了「翻页了没有」时连点两次，一旦第一次其实成功了，就一次跳过一整道题 ——
    跳题在本项目是硬禁区，而且它跳得悄无声息。退化的办法是只做一次动作，
    把结论交给主循环的 ``question_did_not_advance`` 护栏。
    """
    orchestrator, env = _build(tmp_path)
    question = make_question(1)
    seed_vision_geometry(orchestrator, question, next_box=NEXT_BOX)
    _seed_current(orchestrator, question)

    assert await orchestrator._advance(env["page"]) is True

    assert env["actuator"].count("click") == 1, "指纹判不了就不许连点"
    assert len(_clicks(env["actuator"])) == 1


# --------------------------------------------------------------------------- #
# 3. CARD：点「算出来的那一格」，并把目标题号记成预期
# --------------------------------------------------------------------------- #
async def test_card_plan_clicks_the_computed_cell(tmp_path: Path) -> None:
    """CARD 方案点的是**开局算好的那一格**（纯算术，不再问模型）。

    这里用手算出来的期望像素断言：题号 2 = 第 1 行第 2 列，中心 ``(250, 120)``。
    算成「点第一格」或「偏一格」在真机上都是**点错了题**。
    """
    orchestrator, env = _build(tmp_path)
    question = make_question(1)
    plan = seed_vision_geometry(orchestrator, question, card=_card(), current=1)
    _seed_current(orchestrator, question)
    assert plan.method is AdvanceMethod.CARD

    assert await orchestrator._advance(env["page"]) is True

    clicks = _clicks(env["actuator"])
    assert len(clicks) == 1
    _, box, size = clicks[0]
    assert box == pytest.approx(EXPECTED_SECOND_CELL)
    assert size == IMAGE_SIZE
    assert _center_px(box, size) == pytest.approx(EXPECTED_SECOND_XY)
    assert orchestrator._card_number == 2, "题号链每成功一步 +1"
    assert orchestrator._expected_num == 2, "下一轮读到的应当就是第 2 题"


@pytest.mark.parametrize(
    ("current", "expected", "got"),
    [
        pytest.param(1, 2, 3, id="from-the-first-question"),
        pytest.param(16, 17, 20, id="resumed-from-16"),
    ],
)
async def test_card_advance_pauses_when_the_read_back_number_disagrees(
    tmp_path: Path, current: int, expected: int, got: int
) -> None:
    """**题号可信**时，推进之后读回来的题号与预期不符 → **停下**（``advance_failed``）。

    这是防静默跳题的最后一道：坐标算歪、卡片内部滚过、页面重排，都会表现为
    「点完读到的是另一道题」。继续做下去，被跳过的那几道不会被任何人发现。
    两组数字都取「续做」的形状（第 16 题 → 预期第 17 题），因为断点续做正是
    这条校验最容易被绕过的场景。
    """
    bus = RecordingBus()
    orchestrator, env = _build(tmp_path, bus=bus)
    first = make_question(1)
    plan = seed_vision_geometry(orchestrator, first, card=_card(), current=current)
    _seed_current(orchestrator, first)
    assert plan.numbers_known is True, "题号可信时才谈得上校验"

    assert await orchestrator._advance(env["page"]) is True
    assert orchestrator._expected_num == expected

    # 下一轮读到的却是别的题 —— 说明刚才那一下没有落在该点的那一格上
    second = make_question(2)
    _seed_current(orchestrator, second, num_text=f"{got}.")

    assert orchestrator._expected_number_ok(second) is False
    assert orchestrator.paused_by == "advance_failed"

    wrong = [p for p in _line_events(bus) if p.get("advance") == "wrong_question"]
    assert len(wrong) == 1
    assert wrong[0]["expected"] == expected and wrong[0]["got"] == got
    assert wrong[0]["qid"] == second.qid, "留痕要指出是哪一道题让校验失败的"


async def test_card_advance_without_a_known_number_skips_the_number_check(tmp_path: Path) -> None:
    """题号不可信时**不做题号校验** —— 拿一个猜出来的题号去比对，会把正常推进判成跳题。

    为什么这是真风险：模型经常给不出「当前第几题」（题干画在 canvas 上、题库不给编号），
    而 ``next_box`` 仍然指得准。旧行为在这种情况下会设一个「从第 1 题之后起算」的预期
    题号，下一屏读到的真实题号（比如第 17 题）与它不符，于是**每一次正常推进都被判成
    跳题并停下** —— 用户看到的就是「做到一半停下」。
    所以这里钉死两条：落点照样准（点的是模型指出的那一格），以及
    ``_expected_num`` 必须是 ``None``（＝不校验）。
    """
    bus = RecordingBus()
    orchestrator, env = _build(tmp_path, bus=bus)
    first = make_question(1)
    plan = seed_vision_geometry(
        orchestrator, first, card=_card(current_box=None, next_box=_cell_box(4)), current=None
    )
    _seed_current(orchestrator, first, num_text="3.")

    assert plan.method is AdvanceMethod.CARD, "题号缺失不影响 CARD 成立"
    assert plan.numbers_known is False

    assert await orchestrator._advance(env["page"]) is True

    clicks = _clicks(env["actuator"])
    assert len(clicks) == 1
    assert clicks[0][1] == pytest.approx(_cell_box(4)), "落点仍钉在模型指出的那一格"
    assert orchestrator._card_number == 2
    assert orchestrator._expected_num is None, "题号不可信 → 不许设预期题号"

    # 下一屏读到的是第 17 题（与臆测的「第 2 题」完全不符）—— 依旧不许停
    later = make_question(2)
    _seed_current(orchestrator, later, num_text="17.")

    assert orchestrator._expected_number_ok(later) is True
    assert orchestrator.paused_by is None, "题号缺失时读到任意题号都不该停下"
    assert not [p for p in _line_events(bus) if p.get("advance") == "wrong_question"]


async def test_unreadable_number_after_advance_does_not_pause(tmp_path: Path) -> None:
    """读不出题号时**不判** —— 拿「没读到」当「跳题了」会把正常页面全拦下来。

    页面上本来就可能没有印题号（题干画在 canvas 上、题库不给编号），
    这条误判的代价是**每次推进都停下**，比漏判一次大得多。
    """
    orchestrator, _env = _build(tmp_path)
    question = make_question(1)
    seed_vision_geometry(orchestrator, question, card=_card(), current=1)
    _seed_current(orchestrator, question, num_text=None)  # 读题结果里没有题号
    orchestrator._expected_num = 6

    assert orchestrator._expected_number_ok(question) is True

    assert orchestrator.paused_by is None
    assert orchestrator._expected_num is None, "判过一次就清掉，不该影响下一轮"


async def test_card_that_cannot_compute_the_next_cell_clicks_nothing(tmp_path: Path) -> None:
    """答题卡「下一格」算不出来（已经在最后一格）→ **一个坐标都不点**，进收尾确认。

    算不出来时唯一正确的动作是停下。硬拿一个编出来的坐标去点，就是一次真实的误点
    （点到空白，甚至点到别的题或提交按钮上）。
    """
    bus = RecordingBus()
    vision = FakeVisionProvider(completed=False)
    orchestrator, env = _build(tmp_path, vision=vision, bus=bus)
    question = make_question(1)
    plan = seed_vision_geometry(orchestrator, question, card=_card(), current=1)
    _seed_current(orchestrator, question)
    # 题号链走到最后一格：10 格卡片里第 10 格之后再没有格子
    orchestrator._card_number = CARD_COLS * CARD_ROWS
    assert plan.card_target(orchestrator._card_number + 1) is None

    assert await orchestrator._advance(env["page"]) is False

    assert env["actuator"].boxes == [], "落点算不出来时一个坐标都不许点"
    assert any(p.get("advance_card") == "no_target" for p in _line_events(bus))
    assert vision.calls == ["read"], "推不动 → 请视觉组确认一次"
    assert orchestrator.paused_by == "advance_failed"


# --------------------------------------------------------------------------- #
# 4. SCROLL：滚一步 → 读一屏 → 新题真的进来了才算到位
# --------------------------------------------------------------------------- #
class _WheelMouse:
    """只实现 ``wheel`` 的鼠标替身（滚动推进唯一的动作口）。"""

    def __init__(self) -> None:
        self.wheels: list[float] = []
        self.page: _ScrollPage | None = None

    async def wheel(self, dx: float, dy: float, **kwargs: Any) -> None:
        self.wheels.append(float(dy))
        if self.page is not None:
            self.page.scroll_by(float(dy))


class _ScrollPage(FakePage):
    """可滚动的假页面：``_scroll_once`` 要的三个口子（读位置 / 滚一下 / 复读）都实现。

    ``FakePage`` 没有 ``evaluate``，代表「滚不动」的页面；这个子类是「长卷页面」，
    也是滚动推进唯一能被验成的环境。
    """

    def __init__(self, *, height: int = 800, limit: float = 4000.0) -> None:
        super().__init__()
        self.height = height
        self.limit = limit
        self.scroll_y = 0.0
        self.mouse = _WheelMouse()
        self.mouse.page = self

    def scroll_by(self, dy: float) -> None:
        self.scroll_y = max(0.0, min(self.limit, self.scroll_y + dy))

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if "innerHeight" in script:  # _scroll_once 读位置
            return {"h": self.height, "y": self.scroll_y}
        if "scrollBy" in script:  # _scroll_once 的脚本回退路径
            self.scroll_by(float(arg or 0))
            return None
        if "scrollY" in script:  # _scroll_once 复读位置
            return self.scroll_y
        return None


class _ScriptedVision(FakeVisionProvider):
    """按脚本逐屏改口的视觉 provider。

    滚动推进的判据正是「**这一屏读到了什么**」（还是做过的那道 → 滚少了；
    新题 → 到位），一份固定答复验不出这条判据。
    """

    def __init__(self, screens: list[list[dict[str, Any]]]) -> None:
        super().__init__(completed=True)
        self.screens = list(screens)

    def reply(self) -> dict[str, Any]:
        if self.screens:
            self.questions = self.screens.pop(0)
        return super().reply()


def _read_payload(num_text: str, stem: str) -> dict[str, Any]:
    """一屏一道题的读图回复（与 ``prompts/10-视觉组.md`` 的固定格式同构）。"""
    return {
        "index": 1,
        "num_text": num_text,
        "qtype": "single",
        "stem": stem,
        "options": [{"label": "A", "text": "甲", "box": [0.05, 0.36, 0.56, 0.03]}],
    }


def _qid_of(payload: dict[str, Any]) -> str:
    """算出这份读图回复里那道题的 ``qid``（用来构造「**做过的**题」）。"""
    batch = parse_read_batch(json.dumps({"questions": [payload]}, ensure_ascii=False))
    assert batch is not None and batch.questions, "夹具本身要能解析出题目"
    return str(to_question(batch.questions[0]).qid)


def _crop_only_pipeline() -> FakePipeline:
    """只出图、不识别（真实 ``arbiter`` 在题目侧的产出形状）。"""
    return FakePipeline(
        [
            PerceptionResult(
                question=None, channel_used=ProbeName.VISION, warnings=["vision:crop_only"]
            )
        ]
    )


async def test_scroll_counts_as_arrived_only_when_a_new_question_shows_up(tmp_path: Path) -> None:
    """滚一步后读到的**还是做过的题** → 不算到位，再滚一步；读到新题才算到位。

    判据从「滚了多少」换成「这一屏有没有新题」是刻意的：旧实现把滚动量当判据，
    真机上从第 3 题一路滚到第 16 题（中间 13 道全跳过，日志里还看不出来）。
    """
    done = _read_payload("1.", "第 1 题：做过的题")
    fresh = _read_payload("2.", "第 2 题：滚出来的新题")
    vision = _ScriptedVision([[done], [fresh]])
    orchestrator, env = _build(tmp_path, vision=vision, page=_ScrollPage())
    question = make_question(1)
    plan = seed_vision_geometry(orchestrator, question, submit_scope=SubmitScope.QUESTION)
    _seed_current(orchestrator, question, num_text="1.")
    assert plan.method is AdvanceMethod.SCROLL
    orchestrator._visited.add(_qid_of(done))

    assert await orchestrator._advance(env["page"]) is True

    page = env["page"]
    assert len(page.mouse.wheels) == 2, "第一次滚少了（还是做过的题）→ 必须再滚一步"
    assert page.scroll_y > 0
    assert env["actuator"].boxes == [], "滚动推进不点任何坐标"
    assert [q.qid for q in orchestrator._pending_reads] == [_qid_of(fresh)], (
        "滚出来的题必须入队 —— 主循环下一轮直接复用，不再读一次图"
    )
    assert vision.calls == ["read", "read"], "两次读屏：一次判「滚少了」、一次确认到位"


@pytest.mark.parametrize(
    ("completed", "paused_by"),
    [
        pytest.param(False, "advance_failed", id="vision-says-not-done"),
        pytest.param(True, None, id="vision-confirms-done"),
    ],
)
async def test_scroll_that_cannot_move_goes_to_the_end_check_without_switching_methods(
    tmp_path: Path, completed: bool, paused_by: str | None
) -> None:
    """滚不动（这页没有滚动条）→ 进收尾确认；**不换招**去点控件或滑动。

    旧实现在滚动模式下照样会先去点按钮、找不着就继续往下滚，一路滚到
    ``0.8 屏 × 6 = 4.8 屏`` —— 真机题号从 3 跳到 16。现在推不动就停下，
    请视觉组确认一次「是不是全部完成了」。
    """
    vision = FakeVisionProvider(completed=completed)
    orchestrator, env = _build(tmp_path, vision=vision)  # 默认 FakePage：没有 evaluate → 滚不动
    question = make_question(1)
    plan = seed_vision_geometry(orchestrator, question)
    _seed_current(orchestrator, question)
    assert plan.method is AdvanceMethod.SCROLL

    assert await orchestrator._advance(env["page"]) is False

    assert env["actuator"].boxes == [], "滚不动也不许改点坐标"
    assert env["actuator"].count("swipe") == 0, "更不许改滑动"
    assert vision.calls == ["read"], "推不动时问一次视觉组：是不是全部完成了"
    assert orchestrator.paused_by == paused_by


# --------------------------------------------------------------------------- #
# 5. SWIPE：不可滚动时的最后一招，一下只滑一次
# --------------------------------------------------------------------------- #
class _SwipeActuator(FakeActuator):
    """能记下「让滑了几下、哪些方向」的执行器替身。

    真实手势要页面与 CDP 会话，这里只验**编排层让不让滑、试几个方向** ——
    滑几下是「一次跳过一整道题」的直接原因。
    """

    def __init__(self) -> None:
        super().__init__()
        self.swipes: list[str] = []
        #: 每次手势带的幅度（``distance_ratio``）。``None`` = 没给，用配置里的固定距离 ——
        #: 「幅度由视觉组给还是由配置给」是 2026-10-01 那次修复的核心，必须看得见。
        self.ratios: list[float | None] = []

    async def swipe(self, direction: str = "left", **kwargs: Any) -> ActionResult:
        self.swipes.append(direction)
        self.ratios.append(kwargs.get("distance_ratio"))
        return self._result(ActionKind.SWIPE, f"gesture:swipe={direction}", True)


async def test_swipe_plan_swipes_once_and_stops(tmp_path: Path) -> None:
    """``scrolling=False`` 的方案只滑一下、只试一个方向。

    连滑是「一次跳过一整道题」的成因，而它跳得悄无声息：判不了「翻没翻」时把
    「没变」当结论再滑一下，第一次其实成功了就跨过一整题。
    """
    actuator = _SwipeActuator()
    orchestrator, env = _build(tmp_path, actuator=actuator)
    question = make_question(1)
    plan = seed_vision_geometry(orchestrator, question, scrolling=False)
    _seed_current(orchestrator, question)
    assert plan.method is AdvanceMethod.SWIPE

    assert await orchestrator._advance(env["page"]) is True

    assert actuator.swipes == ["left"], "第一下就成功 → 不许再滑第二个方向"
    assert actuator.boxes == [], "滑动手势不是坐标点击"


async def test_swipe_that_is_unavailable_does_not_switch_to_another_method(tmp_path: Path) -> None:
    """滑动机制不可用（拿不到视口 / 建不起 CDP 会话）→ 停下确认，**不换招**去点坐标。

    ``FakeActuator`` 没有页面时 ``swipe`` 如实报失败，正是这种情形。
    换个方向还是同一套机制，再试没有意义。
    """
    vision = FakeVisionProvider(completed=True)
    orchestrator, env = _build(tmp_path, vision=vision)
    question = make_question(1)
    plan = seed_vision_geometry(orchestrator, question, scrolling=False)
    _seed_current(orchestrator, question)
    assert plan.method is AdvanceMethod.SWIPE

    assert await orchestrator._advance(env["page"]) is False

    assert env["actuator"].count("swipe") == 1, "机制不可用 → 只试一次"
    assert env["actuator"].boxes == [], "不许换成点坐标"
    assert orchestrator.paused_by is None, "视觉组确认完成 → 干净收工"


# --------------------------------------------------------------------------- #
# 6. 没有方案 / 兜底被关掉：一个坐标都不点
# --------------------------------------------------------------------------- #
async def test_without_a_plan_nothing_is_clicked_at_all(tmp_path: Path) -> None:
    """``_plan is None``（开局读图没成）→ **一个坐标都不点**，直接走收尾确认。

    「没有逻辑就不动手」是硬纪律：没有方案时的任何点击都是拿一个编出来的坐标去点
    用户的页面。旧实现在这种情况下会走「点击 → 滚动 → 滑动」的默认阶梯。
    """
    bus = RecordingBus()
    vision = FakeVisionProvider(completed=True)
    orchestrator, env = _build(tmp_path, vision=vision, bus=bus)
    question = make_question(1)
    seed_vision_geometry(orchestrator, question, next_box=NEXT_BOX)
    _seed_current(orchestrator, question)
    orchestrator._plan = None  # 开局那一次读图没成留下的状态

    assert await orchestrator._advance(env["page"]) is False

    assert env["actuator"].boxes == [], "没有方案 = 一个坐标都不点"
    assert env["actuator"].count("swipe") == 0, "也不许滑动"
    assert any(p.get("advance") == "no_plan" for p in _line_events(bus))
    assert orchestrator.paused_by is None, "确认完成 → 干净收工"


@pytest.mark.parametrize(
    ("completed", "paused_by"),
    [
        pytest.param(True, None, id="vision-confirms-done"),
        pytest.param(False, "advance_failed", id="vision-says-not-done"),
    ],
)
async def test_visual_fallback_off_never_clicks_a_control(
    tmp_path: Path, completed: bool, paused_by: str | None
) -> None:
    """``guards.advance_visual_fallback=False`` → CLICK 方案**不点**，直接进收尾确认。

    这个开关是用户对「按坐标点击」的显式否决；被关掉之后还去点，等于绕过用户的决定。
    """
    bus = RecordingBus()
    vision = FakeVisionProvider(completed=completed)
    orchestrator, env = _build(
        tmp_path, vision=vision, guards=_guards(advance_visual_fallback=False), bus=bus
    )
    question = make_question(1)
    plan = seed_vision_geometry(orchestrator, question, next_box=NEXT_BOX)
    _seed_current(orchestrator, question)
    assert plan.method is AdvanceMethod.CLICK

    assert await orchestrator._advance(env["page"]) is False

    assert env["actuator"].boxes == [], "兜底关掉就不许点"
    assert any(p.get("advance") == "fallback_off" for p in _line_events(bus))
    assert orchestrator.paused_by == paused_by


async def test_visual_fallback_off_also_blocks_the_card(tmp_path: Path) -> None:
    """兜底开关同样管住 CARD —— 它也是「按坐标点击」，不能只挡 CLICK。"""
    vision = FakeVisionProvider(completed=True)
    orchestrator, env = _build(
        tmp_path, vision=vision, guards=_guards(advance_visual_fallback=False)
    )
    question = make_question(1)
    plan = seed_vision_geometry(orchestrator, question, card=_card(), current=1)
    _seed_current(orchestrator, question)
    assert plan.method is AdvanceMethod.CARD

    assert await orchestrator._advance(env["page"]) is False

    assert env["actuator"].boxes == []
    assert orchestrator.paused_by is None
    assert orchestrator._card_number == 1, "没点成就不许推进题号链"


# --------------------------------------------------------------------------- #
# 7. 两道硬闸门：总数到齐先确认，收尾确认才算收工
# --------------------------------------------------------------------------- #
async def test_reaching_the_total_asks_first_and_finishes_cleanly(tmp_path: Path) -> None:
    """已完成数 = 总数 → **先确认再收工**，确认之前一个坐标都不点。

    最后一题之后有些站点会把页面跳到成绩页，那一步跨过去就回不来了。
    """
    bus = RecordingBus()
    vision = FakeVisionProvider(completed=True, next_box=NEXT_BOX)
    orchestrator, env = _build(tmp_path, vision=vision, bus=bus)
    question = make_question(1)
    seed_vision_geometry(orchestrator, question, next_box=NEXT_BOX, total=2, current=1)
    _seed_current(orchestrator, question)

    assert await orchestrator._advance(env["page"], 2) is False

    checks = [p for name, p in bus.events if name == Event.ADVANCE_COMPLETION_CHECK]
    assert len(checks) == 1
    assert checks[0]["trigger"] == "reached_total"
    assert checks[0]["completed"] is True
    assert checks[0]["observed"] == "all_done"
    assert env["actuator"].boxes == [], "确认完成之前不许再推一步"
    assert orchestrator.paused_by is None


async def test_total_that_the_model_denies_keeps_pushing_the_planned_way(tmp_path: Path) -> None:
    """总数可能读错：视觉组说「还没做完」时**不停下**，继续按方案推进。

    把「到齐了」当停止条件会掩盖一个更常见的事实 —— 总数是从进度文字上看来的，
    它读错的时候（比如把 20 看成 2），后面所有题都会被静默跳过。
    """
    bus = RecordingBus()
    vision = FakeVisionProvider(completed=False, next_box=NEXT_BOX)
    orchestrator, env = _build(tmp_path, vision=vision, bus=bus)
    question = make_question(1)
    plan = seed_vision_geometry(orchestrator, question, next_box=NEXT_BOX, total=2, current=1)
    _seed_current(orchestrator, question)

    assert await orchestrator._advance(env["page"], 2) is True

    triggers = [p["trigger"] for name, p in bus.events if name == Event.ADVANCE_COMPLETION_CHECK]
    assert triggers == ["reached_total"]
    assert plan.method is AdvanceMethod.CLICK
    assert env["actuator"].count("click") == 1, "说没做完就继续用方案里的那一招"
    assert vision.calls == ["read"], "只问了一次（收尾确认），推进本身不问模型"


async def test_end_check_event_carries_completed_and_observed(tmp_path: Path) -> None:
    """收尾确认的事件必须同时给出 ``completed``（判定）与 ``observed``（观测原值）。

    用户要能看懂「它凭什么说做完了」：``completed`` 是程序的结论，``observed`` 是
    模型原话 —— 结论错了的时候，唯一的线索就是这两者。
    """
    bus = RecordingBus()
    vision = FakeVisionProvider(completed=True)
    orchestrator, env = _build(tmp_path, vision=vision, bus=bus)
    question = make_question(1)
    seed_vision_geometry(orchestrator, question, scrolling=False)
    _seed_current(orchestrator, question)

    assert await orchestrator._advance(env["page"]) is False

    checks = [p for name, p in bus.events if name == Event.ADVANCE_COMPLETION_CHECK]
    assert checks and checks[0]["completed"] is True
    assert checks[0]["observed"] == "all_done"
    assert checks[0]["trigger"] == "stuck"


@pytest.mark.parametrize(
    ("completed", "expected_status"),
    [
        pytest.param(True, "finished", id="confirmed-finished"),
        pytest.param(False, "paused:advance_failed", id="not-confirmed-pauses"),
    ],
)
async def test_run_status_follows_the_end_check(
    tmp_path: Path, completed: bool, expected_status: str
) -> None:
    """收尾闸门决定 ``run`` 表的终态：确认完成 → ``finished``；确认不了 → ``paused:advance_failed``。

    界面上这两者的提示完全不同（一个「跑完了」、一个「停下等你处理页面」）。
    把「停下等人」记成 ``finished`` 会让用户以为后面所有题都做完了。
    """
    vision = FakeVisionProvider(completed=completed)
    orchestrator, env = _build(tmp_path, vision=vision, pipeline=FakePipeline(generate=1))
    seed_vision_geometry(orchestrator, make_question(1), scrolling=False)

    await orchestrator.run()

    row = env["conn"].execute("SELECT status FROM run").fetchone()
    assert row["status"] == expected_status
    assert orchestrator.paused_by == ("advance_failed" if not completed else None)


# --------------------------------------------------------------------------- #
# 8. 开局判定：读一屏、裁决一次（旧标定测试的替代）
# --------------------------------------------------------------------------- #
async def test_plan_run_reads_one_screen_and_decides_once(tmp_path: Path) -> None:
    """``_plan_run`` 用**同一次**读图既拿方案又拿首屏题：读一屏 → 裁决 → 发事件。

    这是整条运行「判断逻辑从哪来」的唯一入口。旧实现还要单独发一次「标定」请求
    （另一套提示词、另一张图），既贵、又可能与读题看到的画面不一致。
    """
    bus = RecordingBus()
    vision = FakeVisionProvider(
        next_box=NEXT_BOX,
        total=20,
        current=3,
        submit_scope=SubmitScope.QUESTION,
        questions=[_read_payload("3.", "第 3 题：下列说法正确的是？")],
    )
    orchestrator, env = _build(
        tmp_path, vision=vision, pipeline=_crop_only_pipeline(), bus=bus
    )

    await orchestrator._plan_run(env["page"])

    batch = orchestrator._last_batch
    assert batch is not None and batch.page is not None, "开局那一次读图的观测必须留下来"
    assert len(batch.questions) == 1, "首屏的题也来自同一次读图（不再多花一次调用）"

    plan = orchestrator._plan
    assert plan is not None
    assert plan.method is AdvanceMethod.CLICK
    assert plan.total == 20 and plan.current == 3
    assert plan.control_box == pytest.approx(NEXT_BOX)
    assert plan.submit_scope is SubmitScope.QUESTION
    assert orchestrator._plan_size == IMAGE_SIZE, "落点要用**开局那张图**的尺寸换算"
    assert orchestrator._card_number == 3, "题号链从开局观测到的当前题号起算"

    calibrated = [p for name, p in bus.events if name == Event.ADVANCE_CALIBRATED]
    assert len(calibrated) == 1
    payload = calibrated[0]
    assert payload["method"] == "click"
    assert payload["total"] == 20
    assert payload["current"] == 3
    assert payload["scope"] == "question"
    assert "点控件" in payload["summary"] and "20" in payload["summary"]
    assert payload["reason"], "裁决依据要进事件流，用户要看得懂它凭什么这么走"
    assert vision.calls == ["read"], "开局只读一次图"


async def test_plan_run_happens_only_once(tmp_path: Path) -> None:
    """开局判定只发生一次：第二次调用既不读图也不发事件。

    每题都判一次既贵又没意义，更要紧的是「判第二次」意味着运行中途换逻辑 ——
    那正是旧实现「一次跳过十几道题」的机制。
    """
    bus = RecordingBus()
    vision = FakeVisionProvider(
        next_box=NEXT_BOX, total=20, current=1, questions=[_read_payload("1.", "第 1 题")]
    )
    orchestrator, env = _build(
        tmp_path, vision=vision, pipeline=_crop_only_pipeline(), bus=bus
    )

    await orchestrator._plan_run(env["page"])
    await orchestrator._plan_run(env["page"])

    assert vision.calls == ["read"]
    calibrated = [name for name, _ in bus.events if name == Event.ADVANCE_CALIBRATED]
    assert len(calibrated) == 1


async def test_no_calibration_means_no_plan_and_no_screen_read(tmp_path: Path) -> None:
    """``advance_calibrate=False`` → ``_plan_run`` **不产生方案、不读图**。

    「守卫关掉」的语义是「别在开局花那一次调用」，而不是「随便挑一个方式凑合」：
    没有方案时推进按「一个坐标都不点」处理（见下面两条断言），
    这比拿一个猜出来的方式去推安全得多。
    """
    bus = RecordingBus()
    vision = FakeVisionProvider(completed=True, next_box=NEXT_BOX)
    orchestrator, env = _build(
        tmp_path, vision=vision, guards=_guards(advance_calibrate=False), bus=bus
    )
    _seed_current(orchestrator, make_question(1))

    await orchestrator._plan_run(env["page"])

    assert orchestrator._plan is None
    assert orchestrator._planned is True, "「试过了」必须记下来，否则每轮都会再试一次"
    assert vision.calls == [], "守卫关掉 → 连图都不读"
    assert env["pipeline"].vision.calls == 0
    assert not [name for name, _ in bus.events if name == Event.ADVANCE_CALIBRATED]

    # 没有方案 → 推进时一个坐标都不点，直接进收尾确认
    assert await orchestrator._advance(env["page"]) is False
    assert env["actuator"].boxes == []
    assert any(p.get("advance") == "no_plan" for p in _line_events(bus))
    assert orchestrator.paused_by is None, "视觉组确认完成 → 干净收工"


async def test_confirm_completion_off_means_the_end_check_never_confirms(tmp_path: Path) -> None:
    """``advance_confirm_completion=False`` → 收尾确认永远是「未确认」→ 必然 ``advance_failed``。

    这个开关挡的是「拿一次模型调用换一个收工许可」。关掉之后程序**只能**停下等人 ——
    它也绝不因此把「没确认」当成「做完了」。
    """
    bus = RecordingBus()
    vision = FakeVisionProvider(completed=True)
    orchestrator, env = _build(
        tmp_path,
        vision=vision,
        guards=_guards(advance_confirm_completion=False),
        bus=bus,
    )
    question = make_question(1)
    seed_vision_geometry(orchestrator, question, scrolling=False)
    _seed_current(orchestrator, question)

    assert await orchestrator._advance(env["page"]) is False

    assert orchestrator.paused_by == "advance_failed"
    assert vision.calls == [], "确认被关掉 → 连图都不该去读"
    assert not [name for name, _ in bus.events if name == Event.ADVANCE_COMPLETION_CHECK]


# --------------------------------------------------------------------------- #
# 3. 推进落点取「本次读图刚给的观测」（2026-10-01 加）
#
#    真机日志 ``logs/9abc4ed8ef60``：``advance_card`` 连着两次点偏，读回来的
#    题号与预期不符（expected 27 got 26 / expected 29 got 28）。根因是拿**开局那一屏**
#    算好的几何去点**后来已经变化**的画面 —— 答题卡的行列数、页面滚动位置、
#    浏览器缩放都会让旧框失效。修法：每一步都优先用**刚读到的那一屏**给的落点。
# --------------------------------------------------------------------------- #
def _seed_fresh_read(
    orchestrator: Orchestrator,
    *questions: Question,
    page: PageView | None,
) -> None:
    """把「最近一次读图」的产出摆好 —— 推进逻辑的新鲜落点就来自它。"""
    orchestrator._last_batch = make_read_batch(*questions, page=page)
    orchestrator._last_size = IMAGE_SIZE


async def test_control_advance_prefers_the_freshest_observed_box(tmp_path: Path) -> None:
    """点控件时用**本次读图**给的框，而不是开局算好的那一个。"""
    orchestrator, env = _build(tmp_path)
    first = make_question(1)
    stale = (0.10, 0.90, 0.10, 0.05)
    fresh = (0.80, 0.93, 0.14, 0.05)
    plan = seed_vision_geometry(orchestrator, first, next_box=stale, current=1)
    _seed_current(orchestrator, first)
    assert plan.method is AdvanceMethod.CLICK, "有「下一题」控件 → 裁决成 CLICK"
    _seed_fresh_read(orchestrator, first, page=make_page_view(next_box=fresh))

    assert await orchestrator._advance(env["page"]) is True

    clicks = _clicks(env["actuator"])
    assert len(clicks) == 1
    assert clicks[0][1] == pytest.approx(fresh), "落点必须是刚读到的那一屏给的框"


async def test_card_target_counts_same_screen_queued_questions(tmp_path: Path) -> None:
    """一屏多题时，**同屏消费掉的题也要算进题号**（真机停摆的根因）。

    现场 ``logs/9abc4ed8ef60``：一屏读到 26 / 27 两道（``batch 2``），
    ``page.current=26``。第 2 道（#27）是 ``_take_queued_question`` 从队列里
    直接取走的 —— 那一步**不经过答题卡推进**，所以 ``_card_number`` 链停在 26。
    等两道都做完再推进时，链算出的目标又回到了 **27 = 刚才那道题**，
    点下去页面纹丝不动，随后被判成「推进没生效」停下等人。

    正解：目标以「**刚做完那道题**的题号 + 1」为准（题号可信时）。
    """
    orchestrator, env = _build(tmp_path)
    first = make_question(1)
    second = make_question(2)
    seed_vision_geometry(orchestrator, first, card=_card(), current=26)
    assert orchestrator._card_number == 26, "开局链的起点是观测到的当前题号"
    # 走**真实路径**：同屏第 2 题从队列里取走（这一步不经过推进）。
    orchestrator._pending_reads.append(second)
    assert orchestrator._take_queued_question() is second
    _seed_current(orchestrator, second, num_text="27.")  # 刚做完的是队列里的 #27

    assert await orchestrator._advance(env["page"]) is True

    clicks = _clicks(env["actuator"])
    assert len(clicks) == 1
    # 网格下标：当前格（26）是第 0 格 → 第 28 题 = 第 2 格。
    assert clicks[0][1] == pytest.approx(_cell_box(2)), (
        "目标必须是第 28 题那一格；点第 1 格（=27）就是又点了刚做完那道"
    )
    assert clicks[0][1] != pytest.approx(_cell_box(1)), "绝不能落在刚做完的 #27 上"
    assert orchestrator._card_number == 28
    assert orchestrator._expected_num == 28, "预期待校验的题号也要跟着实际点击走"


async def test_stale_screen_card_pointer_is_not_used_after_queue_consumption(
    tmp_path: Path,
) -> None:
    """**本屏已消费过题目** → 这一屏给的 ``card.next_box`` 已过期，不许再用。

    独立验证者找出的旁路（比我原先改的那一处更靠前）：``_last_batch`` 里存的正是
    读出 26/27 那一屏的观测，而 ``core/run_plan.py`` 在开局把锚点重钉成
    ``card_target(current + 1) == next_box`` —— 所以模型那个 ``next_box``
    **按构造就是 #27 那一格**，与 ``current_box``（#26 格）恰差 1 格，
    正好被「相邻」护栏收下。于是哪怕题号算对了（28），**落点仍然点在 #27 上**，
    现场症状（点一下、页面不动、停下）原样保留。

    这条用例把「新鲜指针」也一起摆出来，专门钉住这个旁路。
    """
    bus = RecordingBus()
    orchestrator, env = _build(tmp_path, bus=bus)
    first = make_question(1)
    second = make_question(2)
    seed_vision_geometry(orchestrator, first, card=_card(), current=26)
    orchestrator._pending_reads.append(second)
    assert orchestrator._take_queued_question() is second
    _seed_current(orchestrator, second, num_text="27.")
    # 最近一次读图就是那一屏：next_box 指的是 #27 那一格（= 刚做完的那道）。
    _seed_fresh_read(
        orchestrator,
        first,
        page=make_page_view(current=26, card=_card(next_box=_cell_box(1))),
    )

    assert await orchestrator._advance(env["page"]) is True

    clicks = _clicks(env["actuator"])
    assert len(clicks) == 1
    assert clicks[0][1] == pytest.approx(_cell_box(2)), (
        "过期的「下一格」(#27) 必须被拒，落点要按实际题号算成 #28"
    )
    stale = [p for p in _line_events(bus) if p.get("advance_card") == "next_box_stale_screen"]
    assert stale, "过期指针必须留痕，否则事后查不出「为什么没用模型给的框」"


async def test_card_number_correction_is_bounded(tmp_path: Path) -> None:
    """题号修正**有界**：读到离谱的题号时退回链目标，绝不静默跨过中间几道。

    链是「每成功一步 +1」的，它自己永远不会跳过题；而 ``num_text`` 是模型抄的，
    可能把 26 抄成 30。修正窗口的右边就是「我们确实比链多做了几道」
    （:attr:`_queued_consumed`），超出窗口即不可信。
    """
    orchestrator, env = _build(tmp_path)
    first = make_question(1)
    seed_vision_geometry(orchestrator, first, card=_card(), current=26)
    _seed_current(orchestrator, first, num_text="30.")  # 把 26 抄成了 30
    assert orchestrator._queued_consumed == 0, "没有同屏消费 → 修正窗口右边就是链目标"

    assert await orchestrator._advance(env["page"]) is True

    clicks = _clicks(env["actuator"])
    assert clicks[0][1] == pytest.approx(_cell_box(1)), (
        "离谱题号必须被拒 → 退回链目标 #27；绝不顺着它跳到 #31（那会静默跨过 #28~#30）"
    )
    assert orchestrator._expected_num == 27


async def test_card_target_keeps_the_chain_when_numbers_are_unknown(tmp_path: Path) -> None:
    """**题号不可信**时仍走链 —— 那时的题号基底是合成的，不能用页面真实题号去喂。

    ``card_start_number`` 会退回 1、``card_anchor`` 由模型指出的 ``next_box`` 反推，
    所以「真实题号 + 1」在那套基底里会解析到完全不同的格子。
    """
    orchestrator, env = _build(tmp_path)
    first = make_question(1)
    seed_vision_geometry(
        orchestrator, first, card=_card(current_box=None, next_box=_cell_box(4)), current=None
    )
    _seed_current(orchestrator, first, num_text="3.")  # 页面上有题号，但开局没观测到

    assert await orchestrator._advance(env["page"]) is True

    clicks = _clicks(env["actuator"])
    assert clicks[0][1] == pytest.approx(_cell_box(4)), (
        "题号不可信 → 目标仍在方案自己的基底里（沿用开局钉好的那一格）"
    )
    assert orchestrator._expected_num is None, "题号不可信 → 不设预期题号"


async def test_card_advance_prefers_the_freshly_pointed_cell(tmp_path: Path) -> None:
    """答题卡推进用**本次读图**直接指出的「下一格」（与当前格相邻的那一格）。"""
    orchestrator, env = _build(tmp_path)
    first = make_question(1)
    fresh_cell = _cell_box(1)  # 当前格是第 0 格，第 1 格才是「下一格」
    seed_vision_geometry(orchestrator, first, card=_card(), current=1)
    _seed_current(orchestrator, first)
    _seed_fresh_read(
        orchestrator,
        first,
        page=make_page_view(current=1, card=_card(next_box=fresh_cell)),
    )

    assert await orchestrator._advance(env["page"]) is True

    clicks = _clicks(env["actuator"])
    assert len(clicks) == 1
    assert clicks[0][1] == pytest.approx(fresh_cell), "用模型指出的那一格，不用旧算术"
    assert orchestrator._card_number == 2, "题号链照旧每成功一步 +1"


async def test_card_advance_rejects_a_next_box_far_from_the_current_cell(tmp_path: Path) -> None:
    """「下一格」离当前格好几格远 → **不采信**，退回有界的算术落点。

    答题卡里一格就是一个题号：点一个隔着好几格的坐标，等于**一次跨过好几道题**。
    而跳题在本项目是静默事故（2026-09-28 真机滚动模式下题号从 3 直接跳到 16）。
    这是独立验证者实测出来的越界路径：``numbers_known=False`` 时模型给的框原本零校验。
    """
    bus = RecordingBus()
    orchestrator, env = _build(tmp_path, bus=bus)
    first = make_question(1)
    seed_vision_geometry(orchestrator, first, card=_card(), current=1)
    _seed_current(orchestrator, first)
    _seed_fresh_read(
        orchestrator,
        first,
        page=make_page_view(current=1, card=_card(next_box=_cell_box(6))),
    )

    assert await orchestrator._advance(env["page"]) is True

    clicks = _clicks(env["actuator"])
    assert len(clicks) == 1
    assert clicks[0][1] == pytest.approx(EXPECTED_SECOND_CELL), (
        "隔了 6 格 → 必须退回算术落点（第 2 格），不能顺着模型点过去"
    )
    far = [p for p in _line_events(bus) if p.get("advance_card") == "next_box_too_far"]
    assert far, "越界必须留痕，否则事后看不出「为什么没用模型给的框」"


async def test_card_advance_without_a_current_cell_falls_back_to_arithmetic(
    tmp_path: Path,
) -> None:
    """模型没给「当前题号格」→ 相邻性无从校验 → 退回有界的算术落点。

    宁可走旧路（有界），也不拿一个可能越过好几题的坐标去点用户的页面。
    """
    bus = RecordingBus()
    orchestrator, env = _build(tmp_path, bus=bus)
    first = make_question(1)
    seed_vision_geometry(orchestrator, first, card=_card(), current=1)
    _seed_current(orchestrator, first)
    _seed_fresh_read(
        orchestrator,
        first,
        page=make_page_view(
            current=1,
            card=_card(current_box=None, next_box=_cell_box(6)),
        ),
    )

    assert await orchestrator._advance(env["page"]) is True

    clicks = _clicks(env["actuator"])
    assert clicks[0][1] == pytest.approx(EXPECTED_SECOND_CELL), "退回算术落点"
    assert [p for p in _line_events(bus) if p.get("advance_card") == "next_box_unverifiable"]


async def test_card_advance_ignores_a_next_box_that_is_the_current_cell(tmp_path: Path) -> None:
    """模型把「当前格」填成「下一格」时**不采信** —— 照着点等于原地不动。

    画面上看不见下一格时模型有相当比例会这样填，而那一下点下去不产生任何变化，
    随后被判成「推进后题号不符」—— 正是用户看到的「推进与实际不符」。
    这种情况必须退回开局算好的算术落点。
    """
    orchestrator, env = _build(tmp_path)
    first = make_question(1)
    current_cell = _cell_box(0)
    seed_vision_geometry(orchestrator, first, card=_card(), current=1)
    _seed_current(orchestrator, first)
    _seed_fresh_read(
        orchestrator,
        first,
        page=make_page_view(current=1, card=_card(current_box=current_cell, next_box=current_cell)),
    )

    assert await orchestrator._advance(env["page"]) is True

    clicks = _clicks(env["actuator"])
    assert len(clicks) == 1
    assert clicks[0][1] == pytest.approx(EXPECTED_SECOND_CELL), "重叠 → 退回算术落点"
    assert clicks[0][1] != pytest.approx(current_cell), "绝不在「当前格」上原地再点一次"


async def test_control_advance_falls_back_to_the_plan_when_no_fresh_read(tmp_path: Path) -> None:
    """没有新鲜观测（还没读到这一屏 / 模型没给框）→ 照旧用开局几何，不是不点。"""
    orchestrator, env = _build(tmp_path)
    first = make_question(1)
    seed_vision_geometry(orchestrator, first, next_box=NEXT_BOX, current=1)
    _seed_current(orchestrator, first)
    orchestrator._last_batch = None

    assert await orchestrator._advance(env["page"]) is True

    clicks = _clicks(env["actuator"])
    assert len(clicks) == 1
    assert clicks[0][1] == pytest.approx(NEXT_BOX)


async def test_advance_settles_before_the_next_screenshot(tmp_path: Path) -> None:
    """推进成功后**必须等待一段沉淀**才让下一轮截屏。

    页面切题是异步的：进度条先变、题面随后才换。动作一返回就截屏，截到的往往是
    换到一半的旧画面 —— 那会把一次成功的推进读成「推进没生效」并停下等人。
    """
    import time as _time

    orchestrator, env = _build(tmp_path, guards=_guards(advance_settle_ms=300))
    first = make_question(1)
    seed_vision_geometry(orchestrator, first, next_box=NEXT_BOX, current=1)
    _seed_current(orchestrator, first)

    started = _time.monotonic()
    assert await orchestrator._advance(env["page"]) is True
    elapsed = _time.monotonic() - started

    assert elapsed >= 0.25, f"推进后没有沉淀就返回了（用时 {elapsed:.3f}s）"


def test_settle_budget_rejects_negative_values() -> None:
    """负的沉淀预算是配置错误，必须在装载时就炸出来，而不是悄悄当成 0。"""
    with pytest.raises(ValueError):
        GuardThresholds(advance_settle_ms=-1)


# --------------------------------------------------------------------------- #
# 4. 「这一下没生效」与「落到了别的题」必须分开（2026-10-01 加）
# --------------------------------------------------------------------------- #
async def test_advance_with_no_effect_is_reported_as_no_effect(tmp_path: Path) -> None:
    """读回来**仍是推进前那道题** → ``advance=no_effect``，不是 ``wrong_question``。

    两者要采取的动作完全不同：前者是「页面没动」（手动点一下 / 滑动幅度不够），
    后者是「坐标算错、落到别的题上」。旧实现把前者也报成 ``wrong_question``，
    用户只能去怀疑坐标，而真相是这一步没生效。
    """
    bus = RecordingBus()
    orchestrator, _env = _build(tmp_path, bus=bus)
    first = make_question(1)
    seed_vision_geometry(orchestrator, first, card=_card(), current=1)
    _seed_current(orchestrator, first, num_text="26.")

    # 摆成「刚点完第 27 格」的状态：预期读回 27，实际仍读到 26（推进前那道）
    orchestrator._card_number = 26
    orchestrator._expected_num = 27
    orchestrator._num_before_advance = 26
    same = make_question(2)
    _seed_current(orchestrator, same, num_text="26.")

    assert orchestrator._expected_number_ok(same) is False
    assert orchestrator.paused_by == "advance_failed"
    events = _line_events(bus)
    assert [p for p in events if p.get("advance") == "no_effect"], "要报「推进未生效」"
    assert not [p for p in events if p.get("advance") == "wrong_question"]


async def test_reread_covers_a_frame_older_than_the_previous_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """补读的判据是「**任何**数字不符」，不只是「读到了上一题」。

    独立验证者指出的盲区：旧判据写的是 ``got == 上一题`` 才补读，而真机现场是
    ``expected=27 / 上一题=27 / got=26`` —— 截屏抢跑时读到的可能是**比上一题还旧**
    的一帧，于是补读永远不会发生，等于没修。这里钉住这条形状。
    """
    bus = RecordingBus()
    orchestrator, env = _build(tmp_path, bus=bus)
    first = make_question(1)
    seed_vision_geometry(orchestrator, first, card=_card(), current=27)
    stale = make_question(2)
    _seed_current(orchestrator, stale, num_text="26.")  # 读回来的是更旧的那一帧
    orchestrator._expected_num = 27
    orchestrator._num_before_advance = 27  # 「上一题」就是 27 —— 旧判据在这里短路

    fresh = make_question(3)

    async def _reread() -> Question:
        orchestrator._vision_reads[fresh.qid] = (
            make_read_result(fresh).model_copy(update={"num_text": "27."}),
            IMAGE_SIZE,
        )
        return fresh

    monkeypatch.setattr(orchestrator, "_read_question_by_vision", _reread)

    resolved = await orchestrator._reconcile_advance_read(env["page"], stale)

    assert resolved is fresh, "更旧的一帧也要补读，否则现场那种形状永远修不好"
    assert orchestrator.paused_by is None
    assert any(p.get("advance") == "reread_stale" for p in _line_events(bus))


async def test_stale_read_is_reread_before_reporting_no_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """读到「上一题」时**先补读一屏**：补读到别的题就照常继续，不当故障停下。

    这是真机那次误停的直接修复：页面切题是异步的，读题紧跟着动作截屏，截到的
    正是换到一半的旧画面。旧实现直接判 ``wrong_question`` 停下等人，
    而页面上其实什么都没坏 —— 补读一次就能拿到正确的那一题。
    """
    bus = RecordingBus()
    orchestrator, env = _build(tmp_path, bus=bus)
    first = make_question(1)
    seed_vision_geometry(orchestrator, first, card=_card(), current=1)
    stale = make_question(2)
    _seed_current(orchestrator, stale, num_text="26.")
    orchestrator._expected_num = 27
    orchestrator._num_before_advance = 26

    fresh = make_question(3)

    async def _reread() -> Question:
        orchestrator._vision_reads[fresh.qid] = (
            make_read_result(fresh).model_copy(update={"num_text": "27."}),
            IMAGE_SIZE,
        )
        return fresh

    monkeypatch.setattr(orchestrator, "_read_question_by_vision", _reread)

    resolved = await orchestrator._reconcile_advance_read(env["page"], stale)

    assert resolved is fresh, "补读到的才是这一轮该做的那道题"
    assert orchestrator.paused_by is None, "补读到了正确的题 → 不许停下"
    assert any(p.get("advance") == "reread_stale" for p in _line_events(bus)), "补读要留痕"
    assert not [p for p in _line_events(bus) if p.get("advance") == "no_effect"]


async def test_stale_read_that_stays_stale_reports_no_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """补读之后**仍是同一道题** → 这一下确实没生效，按 ``no_effect`` 停下并说清原因。"""
    bus = RecordingBus()
    orchestrator, env = _build(tmp_path, bus=bus)
    first = make_question(1)
    seed_vision_geometry(orchestrator, first, card=_card(), current=1)
    stale = make_question(2)
    _seed_current(orchestrator, stale, num_text="26.")
    orchestrator._expected_num = 27
    orchestrator._num_before_advance = 26

    calls: list[str] = []

    async def _reread() -> Question:
        calls.append("read")
        return stale

    monkeypatch.setattr(orchestrator, "_read_question_by_vision", _reread)

    assert await orchestrator._reconcile_advance_read(env["page"], stale) is None

    assert calls == ["read"], "只补读一次 —— 不许拿重读当重试往下滚"
    assert orchestrator.paused_by == "advance_failed"
    events = _line_events(bus)
    assert [p for p in events if p.get("advance") == "no_effect"], "理由要指向「没生效」"
    assert not [p for p in events if p.get("advance") == "wrong_question"], (
        "页面根本没动，不是「落到别的题上」"
    )


# --------------------------------------------------------------------------- #
# 5. 滑动推进：方向与**幅度**由视觉组给（2026-10-01 加）
# --------------------------------------------------------------------------- #
async def test_swipe_advance_uses_the_vision_amplitude(tmp_path: Path) -> None:
    """``advance_swipe`` 技能给出的方向与幅度才是落点；配置里的固定距离只是兜底。"""
    actuator = _SwipeActuator()
    orchestrator, env = _build(tmp_path, actuator=actuator, guards=_guards())
    first = make_question(1)
    seed_vision_geometry(orchestrator, first, scrolling=False, submit_scope=SubmitScope.QUESTION)
    _seed_current(orchestrator, first)
    _seed_fresh_read(
        orchestrator,
        first,
        page=PageView(
            scrolling=False,
            advance_skill_id=AdvanceSkill.SWIPE.value,
            swipe=PageSwipe(direction="up", amplitude=0.42, reason="下一题在下方约半屏"),
        ),
    )

    assert await orchestrator._advance(env["page"]) is True

    assert actuator.swipes == ["up"], "用视觉组给的方向，不是配置里的方向序"
    assert actuator.ratios == [pytest.approx(0.42)], "按视觉组给的幅度滑动，不是配置固定值"


async def test_swipe_observation_is_ignored_when_another_skill_was_selected(tmp_path: Path) -> None:
    """报了幅度却没选 ``advance_swipe`` → **不采信**，退回配置里的方向序。

    模型自己都没把这一步判成滑动时，拿它顺手填的幅度去滑是不负责的
    —— 滑多了就是静默跳题。
    """
    actuator = _SwipeActuator()
    orchestrator, env = _build(tmp_path, actuator=actuator, guards=_guards())
    first = make_question(1)
    seed_vision_geometry(orchestrator, first, scrolling=False, submit_scope=SubmitScope.QUESTION)
    _seed_current(orchestrator, first)
    _seed_fresh_read(
        orchestrator,
        first,
        page=PageView(
            scrolling=False,
            advance_skill_id=AdvanceSkill.CLICK.value,
            swipe=PageSwipe(direction="up", amplitude=0.42),
        ),
    )

    await orchestrator._advance(env["page"])

    assert actuator.ratios and all(ratio is None for ratio in actuator.ratios), (
        "没选滑动技能 → 不许用它的幅度，退回配置距离"
    )
