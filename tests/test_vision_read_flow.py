"""视觉读题 → 按坐标作答的**接线**回归（P11 增量）。

这一层专治一类「每一步都成功、结果却失败」的 bug，实测踩到过：

    19:40:26 log vision_read=ok qid=v6421a401ac81f9c qtype=single options=4
    19:40:26 task.created ...
    19:40:26 task.state_changed from=pending to=failed
    19:40:26 run.paused reason=perception_failed      ← 看上去像「没读到」

日志里明明读到了题，任务却在下一行被判失败，暂停理由还是 ``perception_failed``。
根因是读题结果只落在**局部变量**里，而下游（``_step_quiz`` / 留痕）只认
``perception.question``（仍是 ``None``）。所以本文件的核心断言是：

    **读题成功后，这条任务不许以 ``perception_failed`` 收场。**

v0.2.0 起题目侧只有视觉一条路，于是这里连**真**执行层一起验：
``Actuator.select_option`` / ``submit`` 收的是归一化框，坐标换算（``norm_box_center``）、
选项区域差分、提交结果差分全都在真实实现里跑 —— 所以下面的坐标期望值
（选项 330/225、提交 945/15）是「模型给的框真的落到了那个像素上」的证据，
不是替身的自说自话。页面是内存替身，所以永不 skip。

2026-09-30 的契约变化（本文件跟着改的地方）
-------------------------------------------
* 读图只有**一份** system prompt、**一种**回复格式：``page`` 观测块 + ``questions``。
  「怎么推进 / 什么时候提交 / 是不是做完了」不再由模型回答，而是由
  :func:`core.run_plan.derive_plan` 在**开局读图那一次**裁决，之后全程照它走。
* 题上不再有 ``submit_box`` / ``next_box`` / ``submit_scope``：它们上移到 ``page``
  （页面上那个按钮管多大范围是**页面级**事实，不属于某一道题）。
* 「这一屏没有提交按钮」**不再是读题事件、也不再中途暂停** —— 整卷页面上它本来就是
  正常形态，而提交时机由开局方案定（真机 ``logs/10ca5ee8c89e`` 就是停在这儿）。
* 于是本文件删掉了两类用例：「找不到提交框 → 中途暂停 ``vision_no_submit_box``」
  与「第一次没有提交框 → 滚到顶重读一次（``retry_after_scroll``）」——
  被测的那套行为已被**有意删除**，留着它们只会把已删的逻辑重新钉回来。
* **开局那次读图产出的首题不再被丢掉**（2026-09-30 修）：它插回队首、由主循环取用
  （``vision_read: reused``），所以「一屏多题」整轮**只读一次图**，而首屏的每一道题
  都会被作答。夹具因此按「开局屏 + 收尾屏」两份写 —— 断言的是**行为**
  （读几次、按什么顺序做），不是「恰好读了几次图」，以后又省一次调用也不会红一片。
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw

from core import db
from core.config import RunConfig
from core.enums import AdvanceMethod, ProbeName, ProviderName, QuestionState, TaskType
from core.events import Event
from core.models import DecisionTrace, PerceptionResult
from core.orchestrator import Orchestrator, RunContext, RunDeps
from core.trace import EventBus, RunLogger
from solve.providers.base import LLMResponse, TokenUsage
from tests.orchestrator_helpers import FakeActuator, FakeAdapter, make_answer

#: 图尺寸刻意取整，好把「归一化框 → 像素」的期望值手算出来
IMG_W, IMG_H = 1000, 600

#: 提交框的像素范围（用来把「选项那一下」与「提交那一下」分开）
SUBMIT_RECT = (890.0, 6.0, 1000.0, 24.0)
#: 提交框的归一化坐标：中心 ``(0.945×1000, 0.025×600) = (945, 15)``
SUBMIT_BOX = [0.89, 0.01, 0.11, 0.03]

#: 两道题的题干。**唯一性靠它** —— ``qid`` 由题干哈希而来，两道题不能同题干，
#: 否则「有没有重复处理同一道题」的判据会自己撞上自己。
STEM_ONE = "设 f(x) 为随机变量的概率密度，则其必满足的性质是"
STEM_TWO = "第 2 题：哪个选择器匹配页面上所有元素？"
#: 整卷页面（作业页）第一题的题干 —— 几个页面构造共用同一句，免得对不上号。
STEM_PAPER_ONE = "第 1 题：哪个 HTML 元素会应用选择器 p.note？"


# --------------------------------------------------------------------------- #
# 读题回复的构造（2026-09-30 的固定格式）
# --------------------------------------------------------------------------- #
def _option(label: str, text: str, top: float) -> dict[str, Any]:
    """一个选项：行高 0.03 → 图上 18px；``top=0.36`` 的中心正好是 y=225。"""
    return {"label": label, "text": text, "box": [0.05, top, 0.56, 0.03]}


def _question(index: int, stem: str, top: float, **overrides: Any) -> dict[str, Any]:
    """一道题的回复形状（选项 A/B 落在 ``top`` / ``top+0.05`` 两行）。"""
    entry: dict[str, Any] = {
        "index": index,
        "num_text": f"{index}.",
        "stem": stem,
        "qtype": "single",
        "skill_id": "single_choice",
        "options": [_option("A", "甲说法", top), _option("B", "乙说法", top + 0.05)],
        "clipped": [],
        "uncertain": [],
        "note": "",
    }
    entry.update(overrides)
    return entry


def _screen(
    *questions: dict[str, Any],
    completed: str = "not_done",
    total: int | None = None,
    current: int | None = None,
    next_control: dict[str, Any] | None = None,
    submit_scope: str | None = "question",
    note: str = "",
) -> dict[str, Any]:
    """一屏的固定格式回复：``page`` 观测 + ``questions``（``page`` 每次都要给）。

    默认**不带** ``next_control``、``scrolling=True`` → 裁决成 ``SCROLL``。
    这不是随手选的：``_Page`` 的 ``evaluate`` 只为记录脚本、取不到滚动位置，
    所以滚动一定「推不动」→ 正好走「推不动 → 请视觉组确认收尾」那条正常出口。
    """
    page: dict[str, Any] = {
        "progress": None,
        "total": total,
        "current": current,
        "next_control": next_control,
        "card": None,
        "submit": {"box": list(SUBMIT_BOX), "scope": submit_scope} if submit_scope else None,
        "completed": completed,
        "scrolling": True,
        "reason": "本屏观测",
    }
    return {"page": page, "questions": list(questions), "more_below": False, "note": note}


#: 一屏一道题（提交范围=每题，靶场形态）。
READ_PAYLOAD = _screen(_question(1, STEM_ONE, 0.36), total=1, current=1)

#: 收尾那一屏：``questions: []`` + ``page.completed: all_done`` —— 收尾闸门唯一的放行形状。
END_PAYLOAD = _screen(completed="all_done", total=1, current=1, submit_scope="paper", note="已全部答完")

#: 负对照：同样读不出题，但观测说**还没做完** → 不许收工。
NOT_DONE_END_PAYLOAD = _screen(
    completed="not_done", total=1, current=1, submit_scope="paper", note="还有题没做"
)

#: 一屏两道题（同一份卷面）。用来钉住「第二道不再调模型」。
TWO_QUESTION_SCREEN = _screen(
    _question(1, STEM_PAPER_ONE, 0.36),
    _question(2, STEM_TWO, 0.41),
    total=2,
    current=1,
)

#: 一次正常跑完的读图脚本 —— **整轮只读两次图**。
#:
#: 第一次（开局）同时给出方案与首屏题目；首屏的题会入队、由主循环取用
#: （事件 ``vision_read: reused``），所以**没有**「主循环再读一次」那一格 ——
#: 多写一格反而会让用例在「又省下一次调用」时莫名其妙地红。
#: 末位固定放收尾观测：脚本用尽后重复最后一条，于是任何一次收尾确认都读得到「做完了」，
#: 而收尾要读几次（``reached_total`` / ``stuck`` 两个触发点各自缓存）不必写进断言。
DEFAULT_RUN: tuple[dict[str, Any], ...] = (READ_PAYLOAD, END_PAYLOAD)


def _png(
    *,
    option_clicks: list[tuple[float, float]] | None = None,
    submits: int = 0,
) -> bytes:
    """按页面状态渲染一张图。

    **必须真的会跟着动作变**：v0.2.0 的题目侧判据就是像素差分 ——
    一张恒定的图会让 ``select_option`` / ``submit`` 一路重放到阶梯到顶
    （判「点了没反应」），于是本该通过的用例变成「动作失败」，
    而那反映的是替身不真实，不是实现有问题。

    两处刻意做成**累积**的，因为真实页面就是累积的：

    * 每点一个选项，那一点上**留下一个标记**（点第二道题的选项也得看出变化，
      不能靠一个全局开关 —— 那会让「第二个选项」永远测出「没变」）；
    * 每次点提交，结果区**多一行**（一屏多题、每题提交时会有第二次提交，
      用一个「已提交」布尔量画同一块面板，第二次提交就永远看不出变化）。
    """
    image = Image.new("RGB", (IMG_W, IMG_H), "white")
    draw = ImageDraw.Draw(image)
    for x, y in option_clicks or []:
        # 标记画在那一下**实际点到的地方**（不是照抄框的位置）——
        # 差分只能证明「那块像素变了」，所以要让「点到哪儿」与「哪儿变了」对得上。
        draw.rectangle([x - 8, y - 6, x + 8, y + 6], fill="black")
    for index in range(submits):
        top = 40 + index * 24
        draw.rectangle([100, top, 900, top + 18], fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class _Mouse:
    def __init__(self, page: _Page) -> None:
        self.page = page

    async def click(self, x: float, y: float) -> None:
        self.page.clicks.append((x, y))
        left, top, right, bottom = SUBMIT_RECT
        if left <= x <= right and top <= y <= bottom:
            self.page.submits += 1
        else:
            self.page.option_clicks.append((x, y))


class _Page:
    """只提供视觉路径与执行层真正用到的那几个口子。

    ``screenshot`` 随页面状态变化：点过的地方留下标记、提交过就多一行结果。
    差分判据正是拿它工作的 —— 所以它必须记得住**每一次**动作，而不是一个「已作答」开关。
    """

    def __init__(self) -> None:
        self.clicks: list[tuple[float, float]] = []
        self.mouse = _Mouse(self)
        self.url = "https://real.example/homework"
        self.scrolls: list[str] = []
        self.option_clicks: list[tuple[float, float]] = []
        self.submits = 0

    async def screenshot(self, **kwargs: Any) -> bytes:
        return _png(option_clicks=self.option_clicks, submits=self.submits)

    async def evaluate(self, script: str) -> None:
        # 只看滚动位置的脚本：记一笔即可（本文件不验滚动，验的是坐标与差分）。
        # **返回 None 是有意的**：取不到滚动位置 → 方案里那一招「推不动」→
        # 干净地走收尾确认，而不是留在半路抛异常。
        self.scrolls.append(script)


class _VisionProbe:
    """整屏视觉探针：v0.2.0 的 ``crop_question(page)`` **只返回 PNG 字节**。"""

    async def is_available(self, page: Any, adapter: Any) -> bool:
        return True

    async def crop_question(self, page: Any) -> bytes:
        return await page.screenshot()


class _Pipeline:
    """感知流水线替身：**只出图、不识别** —— 这正是真实 ``arbiter`` 的产出形状。"""

    def __init__(self) -> None:
        self._probe = _VisionProbe()

    def probe(self, name: ProbeName) -> Any:
        return self._probe if name is ProbeName.VISION else None

    async def run(self, page: Any, adapter: Any, ctx: Any) -> PerceptionResult:
        png = await page.screenshot()
        return PerceptionResult(
            question=None,
            video_state=None,
            channel_used=ProbeName.VISION,
            warnings=[
                "vision:crop_only",
                "vision:crop_ok",
                "vision:viewport",
                f"vision:bytes={len(png)}",
                "vision:requires_model_config",
                "arbiter:vision_only",
            ],
            trace=DecisionTrace(
                chosen=ProbeName.VISION, reason="vision_only", needs_vision=True
            ),
        )


def _resp(payload: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        text=json.dumps(payload, ensure_ascii=False),
        parsed=None,
        usage=TokenUsage(),
        latency_ms=1,
        raw="",
    )


def _plain(text: str) -> LLMResponse:
    return LLMResponse(text=text, parsed=None, usage=TokenUsage(), latency_ms=1, raw="")


class _ScriptedProvider:
    """脚本化假视觉 provider：**只按「带图 = 读图请求」这一条判断**。

    2026-09-30 起读图只有一份契约（``prompts/10-视觉组.md``），所以这里不再按
    ``req.system`` 分流成「开局标定 / 找推进控件 / 收尾确认」三套应答 ——
    那种替身会替实现决定「什么时候该问什么」，而真实实现只认图：
    同一个页面在任何时机都只会得到同一份固定格式的回答。

    ``responses`` 按**读图调用序号**逐条吐出：``dict`` 序列化成 JSON，``str``
    原样返回（用来演「答非所问」）；用完之后重复最后一条 —— 大多数场景里
    「模型每次看到的都是同一屏」正是真相。

    ``calls`` 只数**读图**调用：本文件要证明「同屏的第二道题不再调模型」，
    所以这个计数必须干净到能直接当证据用（见 :class:`_RecordingBus`）。
    """

    name = ProviderName.MOCK

    def __init__(self, *responses: dict[str, Any] | str) -> None:
        assert responses, "至少给一条应答 —— 否则替身在测一个它没有的行为"
        self.responses = list(responses)
        self.calls = 0

    def model_for(self) -> str:
        return "fake-vlm"

    async def complete(self, req: Any) -> LLMResponse:
        if not req.images:
            # 不带图 = 解题请求（同一个 provider 兼任判题链时）→ 回一份**空作答**，
            # 让调用方按「模型明确空作答」处理，而不是撞上一个替身看不懂的错。
            return _resp({"chosen_labels": [], "confidence": 0.0})
        self.calls += 1
        entry = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        return _plain(entry) if isinstance(entry, str) else _resp(entry)

    async def aclose(self) -> None:  # pragma: no cover - 替身
        return


class _RecordingBus:
    """只记不发的总线：每条事件顺带记下「发它的时候模型被读图调用过几次」。

    为什么要把两件事绑在一起：本文件要钉住「一屏多题里第二道不再调模型」。
    只看 ``vision_read=reused`` 那条事件说明不了问题 —— 它只证明队列被取用了；
    必须能断言「从 ``ok`` 到 ``reused`` 之间 ``calls`` 一次都没涨」，
    否则「复用几何」这件事随时可能悄悄退化成「再问一次模型」。
    """

    def __init__(self, provider: _ScriptedProvider) -> None:
        self.provider = provider
        self.events: list[tuple[str, dict[str, Any], int]] = []

    def emit(self, event: str, payload: dict[str, Any]) -> None:
        self.events.append((event, payload, self.provider.calls))

    def vision_logs(self, kind: str) -> list[tuple[dict[str, Any], int]]:
        """``vision_read=<kind>`` 的日志行，连同当时的读图调用次数。"""
        return [
            (payload, calls)
            for name, payload, calls in self.events
            if name == Event.LOG_LINE and payload.get("vision_read") == kind
        ]

    def of(self, event: str) -> list[tuple[dict[str, Any], int]]:
        return [(payload, calls) for name, payload, calls in self.events if name == event]


class _Solver:
    """假解题组：只记下「哪些题被下传了」（这就是本题材要害的那条接线）。"""

    providers: list[Any]

    def __init__(self, provider: _ScriptedProvider, *, labels: list[str] | None = None) -> None:
        self.providers = [provider]
        self.labels = labels if labels is not None else ["A"]
        self.seen: list[Any] = []

    async def solve(self, question: Any, **kwargs: Any) -> Any:
        self.seen.append(question)
        return make_answer(question, labels=self.labels)


def _build(
    tmp_path: Path, *responses: dict[str, Any] | str
) -> tuple[Orchestrator, _Page, _ScriptedProvider, _RecordingBus, Any]:
    """装配一条视觉读题链路（**执行层与校验层用真的**）。

    执行层与校验层不能换替身：坐标换算与像素差分都在这条链路上，
    换成替身就等于把本文件要证明的东西（「框真的落到了那个像素上」）验没了。
    """
    provider = _ScriptedProvider(*responses)
    solver = _Solver(provider)
    bus = _RecordingBus(provider)
    page = _Page()
    conn = db.init_db(tmp_path / "autolearn.db")
    cfg = RunConfig(auto_apply=True, task_sequence=[TaskType.QUIZ])
    ctx = RunContext(run_id="run-vision", cfg=cfg, started_at=datetime.now(UTC))
    deps = RunDeps(
        page=page,
        adapter=FakeAdapter(),
        pipeline=_Pipeline(),  # type: ignore[arg-type]
        solver=solver,  # type: ignore[arg-type]
        run_logger=RunLogger("run-vision", root=tmp_path / "logs"),
        bus=bus,
        conn=conn,
        probe_timeout_s=0.1,
    )
    return Orchestrator(ctx, deps=deps), page, provider, bus, conn


def _states(conn: Any) -> list[str]:
    """按**作答顺序**取题目状态。

    必须显式 ``ORDER BY rowid``：``task_item`` 上有一条 ``(run_id, state)`` 索引，
    裸 ``SELECT state`` 会被那条索引覆盖，于是返回的是**按状态字母序**——
    一屏两题时 ``['failed', 'verified']`` 看着像「第一道失败了」，其实顺序整个反着，
    会让断言完全指错方向。（真踩到过：同一份结果按两种读法给出两种顺序。）
    """
    rows = conn.execute("SELECT state FROM task_item ORDER BY rowid").fetchall()
    return [row["state"] for row in rows]


def _run_status(conn: Any) -> str:
    return str(conn.execute("SELECT status FROM run").fetchone()["status"])


# --------------------------------------------------------------------------- #
# 核心回归：读题成功之后，任务不许以 perception_failed 收场
# --------------------------------------------------------------------------- #
async def test_vision_read_result_reaches_downstream(tmp_path: Path) -> None:
    """**回归守卫**：读题结果必须并入 ``perception``。

    只放在局部变量里的话，``_step_quiz`` 看到的仍是 ``perception.question is None``
    → 立刻判 ``failed`` + 以 ``perception_failed`` 暂停。日志上看起来像「没读到」，
    其实读到且完全正确 —— 这种「每一步都对、结果却失败」最难查。
    """
    orchestrator, _page, _provider, bus, conn = _build(tmp_path, *DEFAULT_RUN)

    await orchestrator.run()

    ok = bus.vision_logs("ok")
    assert ok, "读题成功必须留下 vision_read=ok 这条证据（留痕是排查的唯一入口）"
    assert ok[0][0]["qid"], "ok 事件必须带上 qid —— 否则「读到了哪道题」无从对账"
    assert ok[0][0]["skill_id"] == "single_choice"
    # 读题成功就该走到作答结束。v0.2.0 的提交结果回读是**截图差分**，
    # 所以终态是 ``verified``（差分看得见画面变了）—— 不再是「只提交、不校验」。
    assert _states(conn) == [QuestionState.VERIFIED.value], f"实际状态 {_states(conn)}"
    # 收尾时视觉组确认「全部做完」→ **干净收工**，既不暂停也不该是 perception_failed。
    assert orchestrator.paused_by is None, (
        f"读到了题、也答完了，不该停下（理由 {orchestrator.paused_by!r}）"
    )


async def test_one_read_yields_both_the_plan_and_the_first_questions(tmp_path: Path) -> None:
    """**开局那次读图同时产出方案与首屏题目**，不为方案多花一次调用。

    旧版是「先标定一次、再读题一次」——同一个页面被问两遍，而且两次的说法
    可能不一样（那正是「下一题处理仍然存在严重问题」的成因之一）。
    现在裁决所用的一切都来自这一次回复的 ``page`` 块。

    断言到「当时模型被调用过几次」这一步是必须的：只看有没有
    ``advance.calibrated`` 事件，区分不了「同一次读图兼职」与「另外问了一次」。
    """
    orchestrator, _page, provider, bus, _conn = _build(tmp_path, *DEFAULT_RUN)

    await orchestrator.run()

    ok_payload, ok_calls = bus.vision_logs("ok")[0]
    assert ok_calls == 1, "首题的读图就是第 1 次调用"
    # 读图那条日志的字段是**固定**的：这正是事后回答「它当时看到了什么」的全部依据。
    # 用集合相等而不是逐个 get：多出 ``has_submit_box`` 这类已删字段（题级提交框的残留）
    # 与少掉 ``page``（没有观测留痕）都能被这一条抓住。
    assert set(ok_payload) == {
        "vision_read",
        "qid",
        "qtype",
        "skill_id",
        "reported_qtype",
        "reported_skill_id",
        "skill_error",
        "options",
        "num_text",
        "batch",
        "queued",
        "more_below",
        "page",
    }, f"读图日志字段变了：{sorted(ok_payload)}"
    assert ok_payload["qtype"] == "single"
    assert ok_payload["options"] == 2
    assert ok_payload["num_text"] == "1."
    assert ok_payload["batch"] == 1, "这一屏只有一道题"
    assert ok_payload["queued"] == 0
    assert ok_payload["more_below"] is False
    assert isinstance(ok_payload["page"], dict), "页面观测必须留痕（方案就是照它裁的）"
    assert ok_payload["page"]["completed"] == "not_done"
    assert ok_payload["page"]["submit_scope"] == "question"

    calibrated = bus.of(Event.ADVANCE_CALIBRATED)
    assert calibrated, "开局必须发一条 advance.calibrated（方案要进事件流，用户看得懂它打算怎么走）"
    plan_payload, plan_calls = calibrated[0]
    assert plan_calls == 1, "方案与题目来自**同一次**读图，不该为方案多问一次模型"
    assert plan_payload["method"] == AdvanceMethod.SCROLL.value
    assert plan_payload["scope"] == "question"
    assert "推进方式" in plan_payload["summary"]
    assert orchestrator._plan is not None
    assert orchestrator._plan.method is AdvanceMethod.SCROLL

    # **首题走队列、不再读第二次图**（2026-09-30 修掉「开局读图把首题丢掉」之后）：
    # 开局那次读图的返回值会插回队首，主循环第一轮就把它取走。
    reused = bus.vision_logs("reused")
    assert [p["qid"] for p, _ in reused] == [ok_payload["qid"]], (
        "首题必须从队列里取（reused），而且就是开局读到的那一道"
    )
    assert {calls for _, calls in reused} == {1}, "取用队列里的题**不再调模型**"
    assert provider.calls == 2, (
        "整轮只读两次图：开局那次（方案 + 首题）与收尾确认那次。"
        f"多出来就说明同一屏又被问了一遍，实际 {provider.calls}"
    )


async def test_vision_path_clicks_options_and_submit_by_coordinates(tmp_path: Path) -> None:
    """按坐标作答：点选项 → 点提交，坐标由归一化框换算而来。

    期望值手算（图 1000×600）：
    A 的中心 ``(0.05 + 0.56/2) × 1000 = 330``、``(0.36 + 0.03/2) × 600 = 225``；
    提交框中心 ``(0.89 + 0.11/2) × 1000 = 945``、``(0.01 + 0.03/2) × 600 = 15``。

    「只有两下」本身就是判据：推进方式是**滚动**，滚动一个坐标都不点 ——
    如果这里多出第三下，说明推进又退回了「按坐标点下一题」那条老路。
    """
    orchestrator, page, _provider, _bus, _conn = _build(tmp_path, *DEFAULT_RUN)

    await orchestrator.run()

    assert len(page.clicks) == 2, f"该点两下（选项 + 提交），实际 {page.clicks}"
    assert page.clicks[0] == pytest.approx((330.0, 225.0)), "选项 A 的中心"
    assert page.clicks[1] == pytest.approx((945.0, 15.0)), "提交框的中心"


async def test_vision_path_records_l6_ladder(tmp_path: Path) -> None:
    """按坐标的动作要落在 ``L6_VISION_XY`` 阶梯上 —— 那正是这个阶梯的语义。

    v0.2.0 起题目侧的动作**全在 L6**（再无 L1~L5 的题目用途），
    热力图 / 降级统计靠它把「题目」与「媒体」分开看（媒体仍走媒体锚点阶梯）。
    """
    orchestrator, _page, _provider, _bus, _conn = _build(tmp_path, *DEFAULT_RUN)

    await orchestrator.run()

    # 动作结果落在 action.json 里（RunLogger），阶梯字段必须在
    actions = list((tmp_path / "logs" / "run-vision").rglob("action.json"))
    assert actions, "按坐标的动作也必须留痕"
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in actions]
    levels = {p.get("level_used") for p in payloads if p}
    assert "l6_vision_xy" in levels, f"阶梯应为 L6，实际 {levels}"


# --------------------------------------------------------------------------- #
# 一屏多题：一次读图拿回整屏，后面的题**不再调模型**
# --------------------------------------------------------------------------- #
async def test_one_screen_of_two_questions_answers_both_without_extra_model_calls(
    tmp_path: Path,
) -> None:
    """一屏两题：**两道都作答**，而且从开局到做完**一次模型调用都没多花**。

    这是「一屏多题」省下的东西 —— 几何在上一次读图时已经连同框一起算好，
    只要画面没动就依然有效；再问一次模型既费钱又可能给出**不一样的框**
    （2026-09-29 真机日志里同一个按钮两次给的框差了 0.8 个屏宽）。

    两个方向都要钉住，缺一个就会出现「看着很省、其实白读」或「省了调用、漏了题」：

    * **不漏题**：两道都按**页面顺序**作答（主循环先把队列取空，队列没空就不推进）；
    * **不重读**：两次取用（``reused``）都发生在同一次模型调用之内，
      整轮总读图次数 = 开局那次 + 收尾确认那次。

    推进之后队列必须清空：画面一动，那些几何指向的就是**别的题**了，
    留着它们等于拿旧坐标去点新画面。
    """
    orchestrator, _page, provider, bus, conn = _build(tmp_path, TWO_QUESTION_SCREEN, END_PAYLOAD)

    await orchestrator.run()

    ok_payload, ok_calls = bus.vision_logs("ok")[0]
    assert ok_payload["batch"] == 2, "一屏读到两道题"
    assert ok_payload["queued"] == 1, "其中一道留给后面用"

    reused = bus.vision_logs("reused")
    assert len(reused) == 2, f"两道题都该走「复用几何」那条路，实际 {reused}"
    assert reused[0][0]["qid"] == ok_payload["qid"], (
        "开局读到的那道题必须**先做**（2026-09-30 之前它被 `_plan_run` 丢掉了）"
    )
    assert reused[1][0]["qid"] != ok_payload["qid"], "第二道是屏幕上的另一道题"
    assert {calls for _, calls in reused} == {ok_calls}, (
        "两次取用都发生在同一次模型调用之内 —— 这正是「第二道不再调模型」的全部含义"
    )
    solver: Any = orchestrator.deps.solver
    assert [question.stem for question in solver.seen] == [STEM_PAPER_ONE, STEM_TWO], (
        "两道题都要作答，且按页面顺序"
    )
    assert _states(conn) == [QuestionState.VERIFIED.value] * 2, "两道都作答并回读成功"
    assert provider.calls == 2, (
        f"整轮只该读两次图（开局 + 收尾确认），实际 {provider.calls}"
    )
    assert orchestrator._pending_reads == [], "推进之后同屏几何必须作废，队列不许留东西"


# --------------------------------------------------------------------------- #
# 门禁：题面残缺 / 模型拿不准 → **不下传**（2026-09-28 加）
# --------------------------------------------------------------------------- #
async def test_clipped_read_is_gated_before_the_actuator(tmp_path: Path) -> None:
    """模型自己声明题面被切掉 → **一个坐标都不点**，且暂停原因要具体。

    门禁的存在理由：读题是本链路**唯一「错了也不报错」**的环节。
    抄漏一个选项、少一个负号，输出看起来完全正常 —— 放行它，就等于把一次
    静默的错答一路推到用户的真实页面上，而日志里什么都看不出来。
    在「校验退化成截图差分」（只证明有东西变了）之后，这道门禁更重要了。
    """
    payload = _screen(_question(1, STEM_ONE, 0.36, clipped=["option:D"]), total=1, current=1)
    orchestrator, page, _provider, _bus, conn = _build(tmp_path, payload)

    await orchestrator.run()

    assert page.clicks == [], "题面残缺时一个坐标都不该点"
    assert orchestrator.paused is True
    assert orchestrator._paused_by == "vision_incomplete", (
        f"暂停原因要具体到门禁（笼统的 perception_failed 会把排查方向带偏），"
        f"实际 {orchestrator._paused_by!r}"
    )
    assert _states(conn) == [], "被门禁拦下时不该建任务"


async def test_uncertain_read_pauses_with_its_own_reason(tmp_path: Path) -> None:
    """「拿不准」与「残缺」必须给出**不同**的暂停原因 —— 下一步动作不同。"""
    payload = _screen(
        _question(1, STEM_ONE, 0.36, uncertain=["stem.formula.1"]), total=1, current=1
    )
    orchestrator, page, _provider, _bus, conn = _build(tmp_path, payload)

    await orchestrator.run()

    assert page.clicks == []
    assert orchestrator._paused_by == "vision_uncertain"
    assert _states(conn) == []


async def test_a_broken_question_does_not_drag_down_the_good_one(tmp_path: Path) -> None:
    """一屏两题、**坏了一道**：另一道照常作答，不陪着一起停。

    为什么必须如此：门禁拦的是「这一道抄得不全」，而它是**逐题**的事实。
    旧行为是「有一道残缺就整批作废」，在长页面上代价很大 ——
    好题被坏题拖着停下，用户看到的却是「读题失败」，无从下手。

    夹具里**只有**「开局那一屏 + 收尾那一屏」两次读图：好题在开局就入队，
    做完之后滚动推不动 → 收尾确认读到最后那一格（脚本用尽重复最后一条）。
    """
    payload = _screen(
        _question(1, "第 1 题：这道题的下半截被视口切掉了", 0.36, clipped=["stem"]),
        _question(2, STEM_TWO, 0.41),
        total=2,
        current=1,
    )
    orchestrator, page, _provider, bus, conn = _build(tmp_path, payload, END_PAYLOAD)

    await orchestrator.run()

    solver: Any = orchestrator.deps.solver
    assert [question.stem for question in solver.seen] == [STEM_TWO], (
        "只该把**好的那一道**下传解题"
    )
    assert len(page.clicks) == 2, f"好题照常作答（选项 + 提交），实际 {page.clicks}"
    assert _states(conn) == [QuestionState.VERIFIED.value]
    assert orchestrator.paused_by is None, "坏题只是被跳过，不该让整轮停下"
    gated = bus.vision_logs("gated")
    assert gated, "坏题被拦的留痕仍要发出来（拦了却查不出为什么，用户只能把门禁关掉）"
    assert {p["reason"] for p, _ in gated} == {"vision_incomplete"}


async def test_gate_leaves_an_auditable_event(tmp_path: Path) -> None:
    """拦下来必须**留痕说清拦在哪**：只说「失败了」等于没分流。

    （门禁最怕的不是拦错，是拦了却查不出为什么 —— 那用户只能把它关掉。）
    """
    payload = _screen(
        _question(1, STEM_ONE, 0.36, clipped=["stem"], note="题干上半被视口截断"),
        total=1,
        current=1,
    )
    orchestrator, _page, _provider, bus, _conn = _build(tmp_path, payload)

    await orchestrator.run()

    gated = bus.vision_logs("gated")
    assert gated, "应当发出一条 vision_read=gated 的日志"
    entry = gated[0][0]
    assert entry["reason"] == "vision_incomplete"
    assert entry["clipped"] == ["stem"]
    assert entry["note"] == "题干上半被视口截断"


async def test_unsupported_qtype_is_persisted_as_skipped_and_run_continues(
    tmp_path: Path,
) -> None:
    """填空题无匹配技能：建 SKIPPED 任务并留档，不暂停也不误点。"""
    payload = _screen(_question(1, STEM_ONE, 0.36, qtype="text", skill_id=None), total=1, current=1)
    orchestrator, page, _provider, bus, conn = _build(tmp_path, payload, END_PAYLOAD)

    await orchestrator.run()

    assert page.clicks == [], "无匹配技能时一个坐标都不该点"
    assert orchestrator.paused is False, "跳过不支持题型后应继续正常收尾"
    assert _states(conn) == [QuestionState.SKIPPED.value]
    skipped = bus.vision_logs("skipped_no_skill")
    assert skipped and skipped[0][0]["reported_qtype"] == "text"
    assert skipped[0][0]["action_taken"] is False
    assert conn.execute("SELECT COUNT(*) FROM task_item").fetchone()[0] == 1
    task_row = conn.execute("SELECT item_id FROM task_item").fetchone()
    assert task_row is not None
    evidence = json.loads(
        (tmp_path / "logs" / "run-vision" / task_row["item_id"] / "skipped.json").read_text(
            encoding="utf-8"
        )
    )
    assert evidence["reason"] == "unsupported_question_type"
    assert evidence["action_taken"] is False


# --------------------------------------------------------------------------- #
# 答不出来 / 这一屏没有题：两种理由必须分开
# --------------------------------------------------------------------------- #
async def test_unparseable_reply_pauses_as_read_failed_and_saves_the_raw_reply(
    tmp_path: Path,
) -> None:
    """模型答非所问（回不了 JSON）→ ``vision_read_failed``，**原始回复必须落盘**。

    为什么非落盘不可：抽出来的正文可能是空串（推理模型把内容写在别的字段里），
    那时「模型到底回了什么」只剩原文能回答。两次真机事故都因为没留痕，
    只能靠截图反推，绕一大圈。
    """
    orchestrator, page, _provider, bus, _conn = _build(
        tmp_path, "这不是 JSON。我只是随便说了一句。"
    )

    await orchestrator.run()

    assert page.clicks == [], "读不出题时一个坐标都不该点"
    assert orchestrator.paused_by == "vision_read_failed", (
        f"理由要指向「模型回复用不了」，实际 {orchestrator.paused_by!r}"
    )
    failed = bus.vision_logs("failed")
    assert failed and failed[0][0]["error"] == "read_parse_failed"
    raw = tmp_path / "logs" / "run-vision" / "vision_read_raw.txt"
    assert raw.is_file(), "解析失败时原始回复必须落盘（否则又一次「失败无证据」）"
    assert "随便说了一句" in raw.read_text(encoding="utf-8")


async def test_screen_without_questions_but_with_an_observation_pauses_as_read_empty(
    tmp_path: Path,
) -> None:
    """一屏一道完整题目都没有 → ``vision_read_empty``（**不是** ``vision_read_failed``）。

    两者的下一步动作不同：一个是「这一屏没题（可能是收尾屏、也可能被切了）」，
    另一个是「模型回答用不了」。它**不等于**「没有题了」—— 那是收尾确认要回答的，
    所以这里如实停下，而不是顺手收工。
    """
    payload = _screen(completed="unknown", total=1, current=1, note="这一屏里没有一道完整的题")
    orchestrator, page, _provider, bus, conn = _build(tmp_path, payload)

    await orchestrator.run()

    empty = bus.vision_logs("empty")
    assert empty, "必须发一条 vision_read=empty 的日志（说明模型看到了画面、只是没有题）"
    assert empty[0][0]["note"] == "这一屏里没有一道完整的题"
    assert orchestrator.paused_by == "vision_read_empty"
    assert page.clicks == []
    assert _states(conn) == []


async def test_screen_without_questions_or_observation_is_a_read_failure(
    tmp_path: Path,
) -> None:
    """既没有题、也**没有 ``page`` 块** → 解析层判为「这次读图没成」。

    这条钉住的是**解析层的既有口径**（``solve.reader.parse_read_batch``：
    没题且没观测 → ``None``）：报告里那句「一道题都没有、也没有 page 块 →
    ``vision_read_empty``」在当前实现下是**做不到**的 —— 那种回复与「答非所问」
    在解析层长得一模一样，都只能报 ``vision_read_failed``。
    要区分它们得动解析层或编排层（本次不允许改），所以这里如实断言现状，
    免得下次有人照着报告去写一个必然失败的用例。
    """
    payload: dict[str, Any] = {"questions": [], "note": "这一屏里没有一道完整的题"}
    orchestrator, page, _provider, bus, _conn = _build(tmp_path, payload)

    await orchestrator.run()

    assert bus.vision_logs("empty") == [], "没有 page 块时连「空的观测」都不成立"
    assert bus.vision_logs("failed"), "只能按「读图没成」记"
    assert orchestrator.paused_by == "vision_read_failed"
    assert page.clicks == []


# --------------------------------------------------------------------------- #
# 收尾闸门：``questions: []`` + ``page.completed: all_done`` 才算收工
# --------------------------------------------------------------------------- #
async def test_end_screen_is_accepted_as_completion_and_the_run_finishes(
    tmp_path: Path,
) -> None:
    """收尾那一屏（没题 + ``all_done``）→ 确认完成 → **干净收工**。

    这是收尾闸门**唯一**的放行路径：推进推不动时读一屏，观测明确说「整卷做完了」
    才允许把这次运行记成 ``finished``。它同时是「最后一题做完后没有下一题可点」
    这条正常尾路的出口。
    """
    orchestrator, _page, provider, bus, conn = _build(tmp_path, *DEFAULT_RUN)

    await orchestrator.run()

    checks = bus.of(Event.ADVANCE_COMPLETION_CHECK)
    assert checks, "收尾必须发一条 advance.completion_check（用户要能看懂它凭什么收工）"
    payload, _calls = checks[-1]
    assert payload["completed"] is True
    assert payload["observed"] == "all_done"
    assert orchestrator._completion_confirmed is True
    assert orchestrator.paused is False
    assert orchestrator.paused_by is None
    assert provider.calls == 2, (
        f"整轮两次读图：开局那次（方案 + 首题）与收尾确认那次，实际 {provider.calls}"
    )
    assert _run_status(conn) == "finished", "确认完成后这次的运行状态必须是 finished"


async def test_not_done_end_screen_stops_instead_of_finishing(tmp_path: Path) -> None:
    """同样的「推不动」，但观测说**没做完** → 停，绝不收工。

    这是全流程唯一防「静默跳掉后面所有题」的闸门：把「不知道 / 没做完」
    当成「做完了」的代价是后面所有题都不再作答，而且事后看不出来。

    收尾那一屏刻意**每次都**回答「没做完」（脚本用尽后重复最后一条）：
    无论收尾确认问几次（``reached_total`` 与 ``stuck`` 是两个触发点），
    闸门都必须保持关闭 —— 这样断言的是**行为**，不是「恰好读了几次图」。
    """
    script = (READ_PAYLOAD, NOT_DONE_END_PAYLOAD)
    orchestrator, _page, _provider, bus, conn = _build(tmp_path, *script)

    await orchestrator.run()

    checks = bus.of(Event.ADVANCE_COMPLETION_CHECK)
    assert checks and checks[-1][0]["completed"] is False
    assert orchestrator._completion_confirmed is False
    assert orchestrator.paused_by == "advance_failed"
    assert _run_status(conn) != "finished"


# --------------------------------------------------------------------------- #
# 提交时机：范围来自**开局方案**，与「这一屏有没有提交按钮」无关
# （2026-09-28 加；2026-09-30 改成开局定死）
# --------------------------------------------------------------------------- #
#: 整卷页面（一屏一题、右上角唯一一个「交卷」）—— 复刻 2026-09-28 真实作业页的形状。
#:
#: 实测日志：44 题纵向排列、右上角唯一一个「交卷」，程序在第 1 题作答完 1 秒内就
#: 走到「已提交」，随即以 ``submit_timeout`` 停下（``region_mad=0.06``：``0/44题``
#: 时那个按钮被禁用，点了毫无反应）。危险不在这次停下 —— 而在**站点若允许交卷**，
#: 那一按就是用户剩余 43 道题的作业当场作废，且不可撤销。
PAPER_SCREEN = _screen(
    _question(1, STEM_PAPER_ONE, 0.36),
    total=2,
    current=1,
    submit_scope="paper",
    note="整卷页面：画面上只有右上角一个「交卷」",
)

#: 整卷页面、**两道题同屏** —— 用来验「一次提交之后，这一轮其余答案一起入终态」。
#: 同屏两题会被主循环一次做完（队列没空就不推进），于是两道都停在 ``applied``，
#: 而整卷只有一次提交动作：不做收尾清算的话，先做的那道会一直不是终态。
PAPER_SCREEN_TWO = _screen(
    _question(1, STEM_PAPER_ONE, 0.36),
    _question(2, STEM_TWO, 0.41),
    total=2,
    current=1,
    submit_scope="paper",
    note="整卷页面：两道题同屏",
)


class _PaperPage(_Page):
    """在 ``_Page`` 上补两样 ``_advance`` 会用到的：视口尺寸与页面指纹。"""

    def __init__(self) -> None:
        super().__init__()
        self.viewport_size = {"width": IMG_W, "height": IMG_H}

    async def evaluate(self, script: str) -> Any:
        if "scrollY" in script:
            # 滚不动的页面（本区不验滚动）—— 如实报「位置没变」。
            return {"h": IMG_H, "y": 0}
        if "innerWidth" in script:
            return [IMG_W, IMG_H]
        # 页面指纹：同一页 → 同一串文本 → 「翻页了没有」判得出来（答案：没翻）。
        return ["https://real.example/homework", "第 1 题 第 2 题"]


def _build_paper(
    tmp_path: Path, *responses: dict[str, Any] | str
) -> tuple[Orchestrator, _PaperPage, FakeActuator, Any]:
    """带**假执行层**的装配 —— 本区验「什么时候提交」，不验坐标与像素。

    为什么换成假执行层：真 ``Actuator`` 每次点击都要用截图差分自证「点到了」，
    而它的页面替身只建模了「某一个选项被标记」，第二次点击必然测出「没变」→
    重放 → 动作失败。本区关心的是**提交次数与顺序**，假执行层把动作按序列下来即可
    （``order()`` / ``count()``）。
    点击仍真的落到 ``_PaperPage`` 上（``FakeActuator(page=...)``），
    所以收尾那次提交的差分回读走的仍是**真 Verifier**。
    """
    provider = _ScriptedProvider(*responses)
    solver = _Solver(provider)
    page = _PaperPage()
    conn = db.init_db(tmp_path / "autolearn.db")
    cfg = RunConfig(auto_apply=True, task_sequence=[TaskType.QUIZ])
    ctx = RunContext(run_id="run-paper", cfg=cfg, started_at=datetime.now(UTC))
    actuator = FakeActuator(page=page)
    deps = RunDeps(
        page=page,
        adapter=FakeAdapter(),
        pipeline=_Pipeline(),  # type: ignore[arg-type]
        solver=solver,  # type: ignore[arg-type]
        run_logger=RunLogger("run-paper", root=tmp_path / "logs"),
        bus=EventBus(),
        conn=conn,
        probe_timeout_s=0.1,
        actuator_factory=lambda _item: actuator,
    )
    return Orchestrator(ctx, deps=deps), page, actuator, conn


def _quiz_actions(actuator: FakeActuator) -> list[str]:
    """只取**作答类**动作（选项 / 提交）。

    推进用的 ``click`` / ``swipe`` 与本区无关：试了几个方向推进是 ``_advance``
    的内部自由度，把它的具体动作写进断言，只会让用例在调整推进顺序时莫名其妙地红。
    """
    return [name for name in actuator.order() if name in {"select_option", "submit"}]


async def test_paper_scope_defers_submit_until_the_whole_paper_is_done(
    tmp_path: Path,
) -> None:
    """**整卷页面：做一题不许交一次。** 提交推迟到收尾确认之后。

    断言的是**顺序**：选项那一下之后并不是提交，而是推进 → 收尾确认；
    ``submit`` 只在最后出现一次，那就是「没有提前交卷」的证据。

    夹具只有「开局那一屏 + 收尾那一屏」：开局把题入队（主循环取用，不再读图），
    滚动推不动之后由收尾确认那一屏给出 ``all_done`` —— 这条尾路与真机一致。
    """
    orchestrator, _page, actuator, conn = _build_paper(tmp_path, PAPER_SCREEN, END_PAYLOAD)

    await orchestrator.run()

    assert _quiz_actions(actuator) == ["select_option", "submit"], (
        f"作答 → 收尾确认 → 才交一次卷，实际 {actuator.order()}"
    )
    assert actuator.count("submit") == 1, "整卷只交一次"
    assert orchestrator._paper_submitted is True
    assert _states(conn) == [QuestionState.VERIFIED.value]
    assert _run_status(conn) == "finished"


async def test_paper_scope_never_submits_when_completion_is_not_confirmed(
    tmp_path: Path,
) -> None:
    """**没确认做完 → 一次都不交。** 本区最重要的一条。

    收尾确认答「没完成」时程序按 ``advance_failed`` 停下等人：页面上的答案已经
    都在了，但**交卷不可逆** —— 宁可让人自己按那一下，也不能由程序替他在
    「可能还差几题」的时候按下去。

    收尾那一屏**每次都**答「没做完」（脚本用尽后重复最后一条），
    所以断言的是「一次都不交」这个行为，而不是「恰好问了几次」。
    """
    orchestrator, _page, actuator, conn = _build_paper(
        tmp_path, PAPER_SCREEN, NOT_DONE_END_PAYLOAD
    )

    await orchestrator.run()

    assert actuator.count("submit") == 0, f"没确认做完就不许交卷，实际 {actuator.order()}"
    assert actuator.count("select_option") == 1, "但选项照点（选答案是无害的）"
    assert orchestrator.paused_by == "advance_failed"
    assert set(_states(conn)) == {QuestionState.APPLIED.value}, (
        f"答案留在「已选未交」，人补完还能接着交，实际 {_states(conn)}"
    )


async def test_paper_scope_does_not_pause_just_because_the_screen_has_no_submit_box(
    tmp_path: Path,
) -> None:
    """**整卷页面不会因为「这一屏没有提交按钮」而中途暂停。**

    这是用户报的那个 bug（真机 ``logs/10ca5ee8c89e``）：整卷页面上「交卷」往往只在
    最后一屏才露出来，而旧逻辑每一屏都要检查提交按钮，看不见就暂停 ——
    表现是「题目一直没读完就停住」。现在提交时机由**开局方案**定死，
    提交按钮的框则按**范围**在三个来源里挑（2026-09-30）：
    ``PAPER → [方案, 收尾那一屏, 最近一次读图]``，``QUESTION → [方案, 最近一次读图, 收尾那一屏]``。
    本用例只钉「缺按钮不等于停下」，三来源的优先级在
    ``tests/test_advance_calibration.py`` 里逐条钉着。
    """
    # 开局那一屏没有提交按钮（``page.submit`` 为 ``None``）→ 方案里没有框；
    # 收尾那一屏才有（``END_PAYLOAD`` 的 ``page.submit.box``）→ ``_end_submit`` 兜住。
    no_submit_screen = _screen(
        _question(1, STEM_PAPER_ONE, 0.36),
        total=2,
        current=1,
        submit_scope=None,
        note="这一屏看不到交卷按钮（在页脚之外）",
    )
    orchestrator, _page, actuator, conn = _build_paper(
        tmp_path, no_submit_screen, END_PAYLOAD
    )

    await orchestrator.run()

    assert actuator.count("submit") == 1, (
        f"提交范围是整卷时，中途缺提交按钮不该影响收尾那一次交卷，实际 {actuator.order()}"
    )
    assert orchestrator.paused_by is None, (
        f"整卷页面不该因为某一屏没有提交按钮而暂停，实际 {orchestrator.paused_by!r}"
    )
    assert orchestrator._end_submit is not None, "收尾那一屏读到的框必须被记下来"
    assert _run_status(conn) == "finished"


async def test_paper_submit_settles_every_answer_of_this_run(tmp_path: Path) -> None:
    """整卷提交**被回读确认**之后，这一轮其余答案也要一起进终态。

    为什么必须如此：整卷只有一次提交动作，而结果回读只挂在「最后作答的那道题」上，
    其余题会一直停在 ``applied``。而 ``applied`` **不是终态** ——
    续跑时那些题会被重做一遍，多选题上就是**把刚选上的勾取消掉**。

    夹具刻意用**同屏两道题**：主循环会把队列取空再推进，于是两道都停在 ``applied``，
    正好逼出「先做的那道会不会被落下」这个问题（改动前它会被落下）。
    """
    orchestrator, _page, actuator, conn = _build_paper(
        tmp_path, PAPER_SCREEN_TWO, END_PAYLOAD
    )

    await orchestrator.run()

    assert _quiz_actions(actuator) == ["select_option", "select_option", "submit"], (
        f"两道都作答、只交一次卷，实际 {actuator.order()}"
    )
    assert _states(conn) == [QuestionState.VERIFIED.value] * 2, (
        f"两道题都必须是终态（否则续跑会重做、把选中的答案点掉），实际 {_states(conn)}"
    )
    assert orchestrator.paused_by is None
    assert _run_status(conn) == "finished"


async def test_question_scope_still_submits_each_question(tmp_path: Path) -> None:
    """``question`` 范围（靶场形态）**行为不变**：每题作答完就提交。

    这条守的是既有回归基础设施：靶场每屏一题、每题一个提交按钮 ——
    改提交时机绝不能把它一起改掉。

    判据取的是「收尾确认说**没做完**时，这一题仍然已经提交过了」：
    每题提交**不依赖**「全部完成」这个前提，与整卷范围正好相反（见上一条）。
    """
    script = (READ_PAYLOAD, NOT_DONE_END_PAYLOAD)
    orchestrator, _page, actuator, conn = _build_paper(tmp_path, *script)

    await orchestrator.run()

    assert _quiz_actions(actuator) == ["select_option", "submit"], (
        f"每题答完就交，实际 {actuator.order()}"
    )
    assert actuator.count("submit") == 1
    assert _states(conn) == [QuestionState.VERIFIED.value]
    assert orchestrator.paused_by == "advance_failed", "推不动且没做完 → 停下等人"
