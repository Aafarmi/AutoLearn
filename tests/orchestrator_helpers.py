"""P7 编排层的内存替身。

**为什么不用真浏览器**：编排层最该天天跑的几条 —— 状态迁移、断点续跑、
「``submitted`` 不重复提交」、必停分支、限速钩子 —— 与页面无关。
把它们挂在真浏览器上，等于「今天没起靶场就不验了」，而这几条恰恰是最不能
静默不跑的。真浏览器只负责证明「在真页面上确实有效」（见 P6 的验收报告口径）。

v0.2.0 起题目侧只有**视觉**一条路，替身也跟着换了一副面孔：

============================  ==========================================================
``pipeline.run``              直接给出一道**已经读好**的题
``seed_vision_geometry``      补上「那道题的几何」（``_vision_reads``）**并裁决一份运行方案**
                              （``_plan``）—— 没有几何就一个坐标都点不了，
                              没有方案就一条推进逻辑都没有，编排层会正确地停下
``actuator.select_option``    按 ``(box, size)`` 点选项（不再是 ``Locator``）
``actuator.submit`` / ``click``  同上，全走归一化框
``verifier.verify_region_changed``  提交结果的**截图差分**回读
``solver.providers``          一条可用的假视觉链（读图 + 收尾确认）
============================  ==========================================================

最后一行不是装饰：v0.2.0 里**没有模型就完全跑不了**，替身环境若不给视觉链，
每条用例都会以 ``vision_read_failed`` / ``advance_failed`` 停下，
那些「跑到最后干净收工」的断言就全成了假的。

为什么替身要**显式裁决方案**（2026-09-30）
-----------------------------------------
生产链路上，方案由 :func:`core.run_plan.derive_plan` 在开局读图那一次算出来。
替身流水线直接发一道读好的题、**绕过了那一屏读图**，所以它必须自己把方案补上 ——
否则编排层只会得到 ``_plan is None``，按设计**一个坐标都不点**（那是对的）。
这里仍然走**真的** ``derive_plan``（只是喂给它替身造出来的页面观测），
所以「推进方式是怎么裁决出来的」这条逻辑在替身用例里也是**被真跑过的**，
而不是在测试里另写一份判断。
"""

from __future__ import annotations

import json
from typing import Any

from core.enums import (
    ActionKind,
    ActLevel,
    CompletionState,
    ProbeName,
    ProviderName,
    QType,
    SolvePath,
    SubmitScope,
    VerifyKind,
)
from core.models import (
    ActionResult,
    Answer,
    DecisionTrace,
    Episode,
    Option,
    PageCard,
    PageControl,
    PageSubmit,
    PageView,
    PerceptionResult,
    Question,
    ReadBatch,
    ReadOption,
    ReadResult,
    RunPlan,
    VerifyResult,
    VideoState,
)
from core.qid import make_qid
from core.run_plan import derive_plan
from solve.providers.base import LLMResponse, TokenUsage

# --------------------------------------------------------------------------- #
# 构造器
# --------------------------------------------------------------------------- #
DEFAULT_OPTIONS = ["甲说法", "乙说法", "丙说法", "丁说法"]

#: 假截图的尺寸。归一化框 → 像素的期望值全靠它手算，所以刻意取整数。
IMAGE_SIZE = (1000, 600)


def _viewport_png(width: int, height: int) -> bytes:
    """造一张**真的** PNG（探针要给的是能解码的字节，不是占位串）。

    为什么必须真：``Orchestrator._vision_frame`` 会 ``Image.open`` 它取尺寸 ——
    归一化框全靠这个尺寸换算成像素，随手一个 ``b"PNG"`` 会让每条用例都炸在
    「截图解不开」上，而那反映的是替身不真实，不是实现有问题。
    """
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


#: 假探针回的那一屏。与 :data:`IMAGE_SIZE` 同尺寸。
VIEWPORT_PNG = _viewport_png(*IMAGE_SIZE)

#: 假读题结果里的「提交」框：中心 ``(0.945×1000, 0.025×600) = (945, 15)``。
SUBMIT_BOX = (0.89, 0.01, 0.11, 0.03)

#: 假读题结果里的「下一题」框：中心 ``(0.89×1000, 0.955×600) = (890, 573)``。
NEXT_BOX = (0.82, 0.93, 0.14, 0.05)


def make_question(
    index: int = 1,
    *,
    options: list[str] | None = None,
    qtype: QType = QType.SINGLE,
    trace: list[str] | None = None,
) -> Question:
    texts = options if options is not None else list(DEFAULT_OPTIONS)
    stem = f"第 {index} 题：下列说法正确的是？"
    return Question(
        qid=make_qid(stem, texts),
        stem=stem,
        stem_hash=f"stem-hash-{index}",
        qtype=qtype,
        options=[
            Option(index=i, label=chr(ord("A") + i), text=text, raw=text)
            for i, text in enumerate(texts)
        ],
        source=ProbeName.VISION,
        skill_id={QType.SINGLE: "single_choice", QType.TRUE_FALSE: "true_false"}.get(qtype),
        channel_trace=trace or ["vision:read_from_image"],
    )


def make_perception(
    index: int = 1,
    *,
    question: Question | None = None,
    needs_vision: bool = False,
) -> PerceptionResult:
    q = question if question is not None else make_question(index)
    return PerceptionResult(
        question=q,
        channel_used=ProbeName.VISION,
        trace=DecisionTrace(chosen=ProbeName.VISION, reason="ok", needs_vision=needs_vision),
    )


def make_read_result(question: Question) -> ReadResult:
    """给一道假题造一份「模型读图」的产出（题干 + 每个选项的归一化框）。

    选项框按顺序往下排：``A`` 在 0.36、``B`` 在 0.41 …… 与真实读题结果的形状一致。

    ⚠️ 这里**没有**提交框 / 下一题框 —— 2026-09-30 起它们属于 ``page`` 观测块
    （页面级的事实），不再挂在每一道题上。见 :func:`make_page_view`。
    """
    options = [
        ReadOption(
            label=option.label,
            text=option.text,
            box=(0.05, 0.36 + 0.05 * position, 0.56, 0.03),
        )
        for position, option in enumerate(question.options)
    ]
    return ReadResult(
        stem=question.stem,
        qtype=question.qtype,
        options=options,
        skill_id=question.skill_id or "single_choice",
    )


def make_page_view(
    *,
    completed: CompletionState = CompletionState.UNKNOWN,
    progress: str | None = None,
    total: int | None = None,
    current: int | None = None,
    next_box: tuple[float, float, float, float] | None = None,
    next_label: str = "下一题",
    card: PageCard | None = None,
    submit_box: tuple[float, float, float, float] | None = SUBMIT_BOX,
    submit_scope: SubmitScope | None = None,
    scrolling: bool | None = None,
    reason: str = "",
) -> PageView:
    """造一份**页面观测**（视觉组看到的这一屏：进度 / 控件 / 答题卡 / 提交按钮）。"""
    return PageView(
        progress=progress,
        total=total,
        current=current,
        next_control=PageControl(box=next_box, label=next_label) if next_box else None,
        card=card,
        submit=PageSubmit(box=submit_box, scope=submit_scope),
        completed=completed,
        scrolling=scrolling,
        reason=reason,
    )


def make_read_batch(
    *questions: Question,
    page: PageView | None = None,
    more_below: bool = False,
    note: str = "",
) -> ReadBatch:
    """把若干假题 + 一份页面观测装成一次读图的产出（**与模型输出同构**）。"""
    return ReadBatch(
        questions=[make_read_result(question) for question in questions],
        page=page,
        more_below=more_below,
        note=note,
    )


def seed_vision_geometry(
    orchestrator: Any,
    *questions: Question,
    next_box: tuple[float, float, float, float] | None = None,
    submit_box: tuple[float, float, float, float] | None = SUBMIT_BOX,
    submit_scope: SubmitScope | None = SubmitScope.QUESTION,
    card: PageCard | None = None,
    total: int | None = None,
    current: int | None = None,
    completed: CompletionState = CompletionState.UNKNOWN,
    scrolling: bool | None = None,
) -> RunPlan:
    """把假题的几何塞进 ``_vision_reads``，**并裁决一份运行方案**塞进 ``_plan``。

    为什么必须显式塞：v0.2.0 起**只有**视觉一条作答路径，几何来自「读题那一次」
    （``_read_question_by_vision`` 把 ``ReadResult`` + 图尺寸存进 ``_vision_reads``）。
    替身流水线直接给出一整道 ``Question``、绕过了读题那一步，所以要把那一份几何补上 ——
    否则编排层会**正确地**判定「没有可点的坐标」而停下（它绝不猜坐标）。

    方案走**真的** :func:`core.run_plan.derive_plan`，只是喂给它替身造出来的观测：
    于是「推进方式是怎么裁决出来的」在替身用例里也是被真跑过的。
    两处刻意留给调用方指定（而不是由 ``derive_plan`` 推断）：

    * ``submit_scope``：默认 ``QUESTION`` —— 替身流水线是「一屏一题」的形状，
      也就是靶场与这些用例原本的口径；整卷用例显式传 ``PAPER``。
      不能让它跟着「这次读了几道题」漂移，否则同一条用例的提交行为取决于调用顺序。
    * ``submit_box``：默认 ``SUBMIT_BOX``（中心 ``(945, 15)``），
      与 :data:`IMAGE_SIZE` 配套手算期望像素。

    ``next_box=None``（默认）表示「这张画面上没有『下一题』」→ 裁决成**滚动**推进：
    跑到最后一题时正是如此，于是主循环会走「推不动 → 让视觉组确认收尾」那条正常出口。
    """
    for question in questions:
        orchestrator._vision_reads[question.qid] = (make_read_result(question), IMAGE_SIZE)
    page = make_page_view(
        completed=completed,
        total=total,
        current=current,
        next_box=next_box,
        card=card,
        submit_box=submit_box,
        submit_scope=submit_scope,
        scrolling=scrolling,
    )
    plan = derive_plan(make_read_batch(*questions, page=page), batch_size=len(questions))
    plan.submit_scope = submit_scope
    plan.submit_box = submit_box
    orchestrator._plan = plan
    orchestrator._plan_size = IMAGE_SIZE
    orchestrator._planned = True
    orchestrator._card_number = current if current is not None and current > 0 else 1
    return plan


def make_answer(
    question: Question,
    *,
    labels: list[str] | None = None,
    review_flag: bool = False,
) -> Answer:
    chosen = labels if labels is not None else ["A"]
    texts = [
        question.options[ord(label) - ord("A")].text
        for label in chosen
        if 0 <= ord(label) - ord("A") < len(question.options)
    ]
    return Answer(
        qid=question.qid,
        chosen_labels=chosen,
        chosen_texts=texts,
        confidence=0.95,
        solve_path=SolvePath.MOCK,
        review_flag=review_flag,
        model_name="mock",
        samples=5,
        stem_hash=question.stem_hash,
    )


# --------------------------------------------------------------------------- #
# 页面 / 适配器
# --------------------------------------------------------------------------- #
class FakePage:
    """只够编排层用的假页面：能截图、能读滚动位置。

    **刻意不实现 ``evaluate``**：真实站点上页面指纹可能取不到可判定的内容
    （题干画在 canvas 上那类），那时编排层必须退化成「只做一次动作」。
    少一个口子正好把那条退化路径一起覆盖上。
    """

    def __init__(self, *, screenshot: bytes | None = b"\x89PNG-before") -> None:
        self.shots: list[bytes] = []
        self._screenshot = screenshot
        self.url = "https://real.example/homework"

    async def screenshot(self, **kwargs: Any) -> bytes:
        self.shots.append(self._screenshot or b"")
        return self._screenshot or b""


class FakeAdapter:
    """站点适配器替身 —— v0.2.0 起适配器**只剩媒体侧**。

    题目锚点（``anchors`` / ``selectors`` / ``locator()`` / ``question_scope()``）
    已随 DOM 通道整体删除，所以这里一个都不提供：编排层若还去摸它们，
    会以 ``AttributeError`` 立刻炸出来，而不是静默走回老路。
    """

    site = "mock_exam"

    def __init__(self) -> None:
        from types import SimpleNamespace

        # -- 媒体锚点：名字与 ``selectors_media.yaml`` 对齐 -------------------- #
        self.media_anchors = SimpleNamespace(
            video='[data-media="video"]',
            episode_list='[data-media="episode-list"]',
            next='[data-media="next"]',
            interrupt='[data-media="interrupt"]',
            play_button='[data-media="play-button"]',
            progress='[data-media="progress"]',
        )
        self.media_selectors = {"episode_item": '[data-media="episode"]'}
        self.media_assertions = SimpleNamespace(ended_epsilon_s=0.35)
        self.interrupt_detection = SimpleNamespace(observer_interval_ms=120, poll_interval_ms=80)

    async def matches(self, page: Any) -> bool:
        return True

    def field(self, name: str, *, media: bool = False) -> str | None:
        del media  # v0.2.0 起只剩媒体属性约定
        return {"body_site_attr": "data-site"}.get(name)

    def media_locator(self, page: Any, anchor: str, **kwargs: Any) -> Any:
        selector = getattr(self.media_anchors, anchor, anchor)
        return _CountedLocator(selector)


class _CountedLocator:
    """媒体路径只需要 ``count()`` / ``first``（媒体读口）。"""

    def __init__(self, selector: str, count: int = 1) -> None:
        self.selector = selector
        self._count = count

    @property
    def first(self) -> _CountedLocator:
        return self

    async def count(self) -> int:
        return self._count


class FakeVisionProbe:
    """假视觉探针：只实现 ``crop_question``（v0.2.0 唯一的截图口）。

    为什么替身也要给探针：真实链路上「模型看到的那一屏」一律由它给出
    （``Orchestrator._vision_frame`` → ``pipeline.probe(VISION).crop_question``），
    读题与**收尾确认**都走它。替身原来一律回 ``None``，那时只有读题会用到它、
    而读题又被替身流水线绕过了；现在收尾确认也要读一屏，
    再回 ``None`` 就等于「这条环境里永远确认不了完成」，跑批必然停在半路。
    """

    def __init__(self, png: bytes = VIEWPORT_PNG) -> None:
        self.png = png
        self.calls = 0

    async def is_available(self, page: Any, adapter: Any) -> bool:
        return True

    async def crop_question(self, page: Any) -> bytes:
        self.calls += 1
        return self.png


class FakePipeline:
    """按脚本逐题返回感知结果；脚本用尽后返回「没有题目」。

    ``generate`` 用于「只关心条数、不关心内容」的用例：自动生成 N 道题，
    并让 ``total`` 成为跑批的自然长度。
    """

    def __init__(
        self,
        results: list[PerceptionResult] | None = None,
        *,
        generate: int = 0,
    ) -> None:
        self.results = list(results or [])
        self.generate = generate
        #: 本次运行一共会经过多少道题（跑批长度）
        self.total = len(self.results) + generate
        self.served = 0
        self.calls = 0
        self.vision = FakeVisionProbe()

    def script(self, *results: PerceptionResult) -> FakePipeline:
        self.results.extend(results)
        self.total += len(results)
        return self

    async def run(self, page: Any, adapter: Any, ctx: Any) -> PerceptionResult:
        self.calls += 1
        if self.results:
            self.served += 1
            return self.results.pop(0)
        if self.served < self.total:
            self.served += 1
            return make_perception(self.served)
        return PerceptionResult(
            question=None,
            channel_used=ProbeName.VISION,
            warnings=["pipeline:exhausted"],
        )

    def probe(self, name: ProbeName) -> Any:
        # 题目侧只有视觉一个探针（媒体探针在网课用例里另有替身，见 ``install_media``）。
        return self.vision if name is ProbeName.VISION else None


# --------------------------------------------------------------------------- #
# 网课场景（P8）替身
# --------------------------------------------------------------------------- #
def make_episode(index: int, *, vid: str | None = None, duration: float = 30.0) -> Episode:
    return Episode(
        episode_index=index,
        title=f"第 {index} 讲",
        vid=vid or f"vid{index:02d}",
        duration=duration,
    )


class FakeMediaWorld:
    """按脚本驱动的媒体世界。

    编排层只通过 ``perception.media_probe`` 的四个函数看世界，所以替身就把这四个
    函数实现掉（见 :func:`install_media`），而不是去伪造一个假 ``<video>`` DOM。

    - ``outcomes``：每次 ``wait_for_episode_outcome`` 依次吐一个；用尽后一律
      ``ended``（对应「正常播完」）；
    - ``finish_after``：第 N 次等结局时把视频置为已结束 —— 用来演
      ``?interrupt_at=end``（弹题与 ``ended`` 同刻）的竞态。
    """

    def __init__(
        self,
        episodes: list[Episode],
        *,
        outcomes: list[str] | None = None,
        index: int | None = None,
        current_time: float = 0.0,
        finish_after: int | None = None,
    ) -> None:
        self.episodes = list(episodes)
        first = self.episodes[0] if self.episodes else None
        self.current_index = index or (first.episode_index if first else 0)
        self.current_time = current_time
        self.duration = float(first.duration) if first else 0.0
        self.paused = True
        self.ended = False
        self.outcomes = list(outcomes or [])
        self._finish_after = finish_after
        #: 观测计数
        self.catalog_calls = 0
        self.outcome_calls = 0
        self.state_reads = 0

    # -- 编排层看到的口子 -------------------------------------------------- #
    async def read_catalog(self, page: Any, adapter: Any) -> list[Episode]:
        self.catalog_calls += 1
        return list(self.episodes)

    async def read_index(self, page: Any, adapter: Any) -> tuple[int, int]:
        return self.current_index, len(self.episodes)

    async def read_state(self, page: Any, adapter: Any) -> VideoState:
        self.state_reads += 1
        return VideoState(
            paused=self.paused,
            ended=self.ended,
            current_time=self.current_time,
            duration=self.duration,
            episode_index=self.current_index,
            episode_total=len(self.episodes),
            src=f"/media/ep{self.current_index}.wav",
        )

    async def wait_outcome(
        self, page: Any, adapter: Any, timeout_s: float, *, poll_s: float | None = None
    ) -> str | None:
        from perception.media_probe import EPISODE_OUTCOME_ENDED

        self.outcome_calls += 1
        if self._finish_after is not None and self.outcome_calls >= self._finish_after:
            self.finish()
        if self.outcomes:
            return self.outcomes.pop(0)
        return EPISODE_OUTCOME_ENDED

    # -- 供 actuator 改变世界 ---------------------------------------------- #
    def set_playing(self) -> None:
        self.paused = False
        self.ended = False

    def set_paused(self) -> None:
        self.paused = True

    def seek(self, seconds: float) -> None:
        self.current_time = float(seconds)
        self.ended = False

    def advance(self) -> None:
        self.current_index += 1
        self.current_time = 0.0
        self.paused = True
        self.ended = False

    def finish(self) -> None:
        self.ended = True
        self.paused = True
        self.current_time = self.duration


def install_media(monkeypatch: Any, world: FakeMediaWorld) -> None:
    """把媒体世界的四个读口换成替身。

    编排层是**函数内** ``from perception.media_probe import ...`` 的，所以
    patch 模块属性即可生效。
    """
    from perception import media_probe

    monkeypatch.setattr(media_probe, "read_episode_catalog", world.read_catalog)
    monkeypatch.setattr(media_probe, "read_episode_index", world.read_index)
    monkeypatch.setattr(media_probe, "read_video_state", world.read_state)
    monkeypatch.setattr(media_probe, "wait_for_episode_outcome", world.wait_outcome)


class RecordingBus:
    """记录事件顺序的假事件总线（编排层只调 ``emit``）。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, event: str, payload: dict[str, Any]) -> None:
        self.events.append((event, payload))

    def names(self) -> list[str]:
        return [name for name, _ in self.events]

    def index_of(self, name: str, *, to: str | None = None) -> int | None:
        for position, (event, payload) in enumerate(self.events):
            if event != name:
                continue
            if to is not None and payload.get("to") != to:
                continue
            return position
        return None


# --------------------------------------------------------------------------- #
# 执行 / 校验
# --------------------------------------------------------------------------- #
def _box_center(
    box: tuple[float, float, float, float], size: tuple[int, int]
) -> tuple[float, float]:
    """归一化框 → 像素中心。**替身刻意自己算**：不依赖正在并行改写的执行层实现。"""
    return ((box[0] + box[2] / 2) * size[0], (box[1] + box[3] / 2) * size[1])


def _swipe_path(
    direction: str, width: float, height: float, ratio: float = 0.6
) -> tuple[tuple[float, float], tuple[float, float]]:
    """手势的起终点。方向语义与真实 :meth:`Actuator.swipe` 一致：**手指的移动方向**。"""
    cx, cy = width / 2.0, height / 2.0
    if direction in {"left", "right"}:
        dx = max(1.0, width * ratio / 2.0)
        if direction == "left":
            return (cx + dx, cy), (cx - dx, cy)
        return (cx - dx, cy), (cx + dx, cy)
    dy = max(1.0, height * ratio / 2.0)
    if direction == "up":
        return (cx, cy + dy), (cx, cy - dy)
    return (cx, cy - dy), (cx, cy + dy)


class FakeActuator:
    """记录每一次动作。``fail_at`` 用来模拟阶梯到顶 / 提交失败。

    v0.2.0 的题目侧动作只有一种形态：**按归一化框点坐标**
    （``select_option`` / ``submit`` / ``click`` 收 ``(box, size)``，不收 ``Locator``）。
    每次调用都把框记进 :attr:`boxes`，这样「编排层到底把哪个框递下来了」可断言。

    ``page`` 给上时动作会**真的落到页面上**（``page.mouse.click`` 与滑动手势），
    于是「点了画面上那个按钮，页面才翻过去」这类断言才有东西可验；
    不给就只记账（媒体用例与「只看动作顺序」的用例不需要页面）。

    ``world`` 给上时，媒体动作会**真的改变媒体世界**（播放 → 不暂停、
    暂停 → 暂停、seek → 改位置、下一集 → 索引 +1），这样「挂起期间必须显式暂停」
    这类断言才有东西可验。
    """

    def __init__(
        self,
        *,
        fail_at: str | None = None,
        exit_ok: bool = True,
        world: FakeMediaWorld | None = None,
        page: Any | None = None,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        #: 每次调用收到的 ``(动作名, 归一化框, 图尺寸)``
        self.boxes: list[tuple[str, tuple[float, float, float, float], tuple[int, int]]] = []
        self.fail_at = fail_at
        self.exit_ok = exit_ok
        self.world = world
        self.page = page

    # -- 记账 --------------------------------------------------------------- #
    def _result(
        self,
        kind: ActionKind,
        target: str,
        ok: bool,
        *,
        level: ActLevel = ActLevel.L6_VISION_XY,
        error: str | None = None,
    ) -> ActionResult:
        return ActionResult(
            kind=kind, target=target, level_used=level, ok=ok, elapsed_ms=3, error=error
        )

    def _note(
        self, name: str, box: tuple[float, float, float, float], size: tuple[int, int]
    ) -> None:
        self.boxes.append((name, box, size))

    async def _click(self, box: tuple[float, float, float, float], size: tuple[int, int]) -> None:
        """把一次点击真的落到页面上（没有页面就什么也不做）。"""
        mouse = getattr(self.page, "mouse", None)
        if mouse is None:
            return
        x, y = _box_center(box, size)
        await mouse.click(x, y)

    # -- 题目侧（v0.2.0：全是坐标） ----------------------------------------- #
    async def select_option(
        self,
        box: tuple[float, float, float, float],
        size: tuple[int, int],
        qtype: QType,
        *,
        target_label: str | None = None,
    ) -> ActionResult:
        label = target_label or ""
        self.calls.append(("select_option", label))
        self._note("select_option", box, size)
        if self.fail_at == "apply":
            return self._result(ActionKind.SELECT_OPTION, label, False)
        await self._click(box, size)
        return self._result(ActionKind.SELECT_OPTION, label, True)

    async def submit(
        self,
        box: tuple[float, float, float, float],
        size: tuple[int, int],
        *,
        target_label: str | None = None,
    ) -> ActionResult:
        label = target_label or "submit"
        self.calls.append(("submit", label))
        self._note("submit", box, size)
        if self.fail_at == "submit":
            return self._result(ActionKind.SUBMIT, label, False)
        await self._click(box, size)
        return self._result(ActionKind.SUBMIT, label, True)

    async def click(
        self,
        box: tuple[float, float, float, float],
        size: tuple[int, int],
        *,
        kind: ActionKind = ActionKind.CLICK,
        target_label: str | None = None,
    ) -> ActionResult:
        label = target_label or "click"
        self.calls.append(("click", label))
        self._note("click", box, size)
        if not self.exit_ok:
            # 「下一题」点不动：用来演「运行没有出口」的那类页面。
            return self._result(kind, label, False, error="fake:exit_blocked")
        await self._click(box, size)
        return self._result(kind, label, True)

    # -- 手势 --------------------------------------------------------------- #
    async def swipe(self, direction: str = "left", **kwargs: Any) -> ActionResult:
        """滑动手势替身：**形状与真实实现一致**（按下 → 若干中间点 → 抬起）。

        方向由首尾两点算出，所以「方向不对就不该算翻页」这条语义仍由页面替身决定。
        没有页面时如实报失败 —— 假装滑了一下会让「推不动就必须停下」的用例失真。
        """
        self.calls.append(("swipe", direction))
        mouse = getattr(self.page, "mouse", None)
        if mouse is None:
            return self._result(
                ActionKind.SWIPE,
                f"gesture:swipe={direction}",
                False,
                error="fake:no_page",
            )
        size = await self._viewport()
        start, end = _swipe_path(direction, size[0], size[1])
        await mouse.move(start[0], start[1])
        await mouse.down()
        try:
            for step in range(1, 5):
                await mouse.move(
                    start[0] + (end[0] - start[0]) * step / 4,
                    start[1] + (end[1] - start[1]) * step / 4,
                )
        finally:
            await mouse.up()
        return self._result(ActionKind.SWIPE, f"gesture:swipe={direction}", True)

    async def _viewport(self) -> tuple[float, float]:
        """视口尺寸：优先属性，其次回退到页内读（CDP 附加来的页面常常没有前者）。"""
        page = self.page
        if page is None:
            return (0.0, 0.0)
        size = getattr(page, "viewport_size", None)
        if isinstance(size, dict) and size.get("width") and size.get("height"):
            return float(size["width"]), float(size["height"])
        value = await page.evaluate(
            "() => [window.innerWidth || 0, window.innerHeight || 0]"
        )
        return float(value[0]), float(value[1])

    # -- 媒体（P8） --------------------------------------------------------- #
    async def play_media(self, adapter: Any, **kwargs: Any) -> ActionResult:
        self.calls.append(("play_media", "media:play"))
        if self.fail_at == "play":
            return self._result(
                ActionKind.PLAY_MEDIA, "media:play", False, level=ActLevel.L1_LOCATOR
            )
        if self.world is not None:
            self.world.set_playing()
        return self._result(ActionKind.PLAY_MEDIA, "media:play", True, level=ActLevel.L1_LOCATOR)

    async def pause_media(self, adapter: Any, **kwargs: Any) -> ActionResult:
        self.calls.append(("pause_media", "media:pause"))
        if self.fail_at == "pause":
            return self._result(
                ActionKind.PAUSE_MEDIA, "media:pause", False, level=ActLevel.L1_LOCATOR
            )
        if self.world is not None:
            self.world.set_paused()
        return self._result(ActionKind.PAUSE_MEDIA, "media:pause", True, level=ActLevel.L1_LOCATOR)

    async def seek_media(self, adapter: Any, seconds: float, **kwargs: Any) -> ActionResult:
        self.calls.append(("seek_media", f"media:seek={seconds:g}"))
        if self.fail_at == "seek":
            return self._result(
                ActionKind.SEEK_MEDIA, "media:seek", False, level=ActLevel.L1_LOCATOR
            )
        if self.world is not None:
            self.world.seek(seconds)
        return self._result(
            ActionKind.SEEK_MEDIA, f"media:seek={seconds:g}", True, level=ActLevel.L1_LOCATOR
        )

    async def next_episode(self, adapter: Any, **kwargs: Any) -> ActionResult:
        self.calls.append(("next_episode", "media:next"))
        if self.fail_at == "next":
            return self._result(
                ActionKind.NEXT_EPISODE, "media:next", False, level=ActLevel.L1_LOCATOR
            )
        if self.world is not None:
            self.world.advance()
        return self._result(
            ActionKind.NEXT_EPISODE, "media:next", True, level=ActLevel.L1_LOCATOR
        )

    def order(self) -> list[str]:
        """动作调用顺序（只取动作名），用于「谁先谁后」的断言。"""
        return [name for name, _ in self.calls]

    def count(self, name: str) -> int:
        return sum(1 for call, _ in self.calls if call == name)


class FakeVerifier:
    """提交差分回读 + 媒体断言的替身。

    ``ok`` 管提交结果（``verify_region_changed`` 的截图差分）；三条媒体断言各有
    独立开关，便于分别制造「点了但没播起来」（``flag_ok``）与「播了但进度没动」
    （``playing_ok``）。
    """

    def __init__(
        self,
        *,
        ok: bool = True,
        raise_once: bool = False,
        flag_ok: bool = True,
        playing_ok: bool = True,
        resume_ok: bool = True,
    ) -> None:
        self.ok = ok
        self.raise_once = raise_once
        self.flag_ok = flag_ok
        self.playing_ok = playing_ok
        self.resume_ok = resume_ok
        self.calls = 0
        self.media_calls: list[str] = []

    async def verify_region_changed(
        self,
        box: tuple[float, float, float, float],
        size: tuple[int, int],
        before: bytes,
    ) -> VerifyResult:
        del box, size, before  # 替身不真的比像素
        self.calls += 1
        if self.raise_once and self.calls == 1:
            raise RuntimeError("模拟进程在提交后被杀死")
        return VerifyResult(
            ok=self.ok,
            kind=VerifyKind.SCREENSHOT_DIFF,
            expected="region_changed",
            actual="changed" if self.ok else "unchanged",
        )

    # -- 媒体（P8） --------------------------------------------------------- #
    async def verify_media_flag(self, adapter: Any, **kwargs: Any) -> VerifyResult:
        self.media_calls.append("flag")
        return VerifyResult(
            ok=self.flag_ok,
            kind=VerifyKind.MEDIA_PROGRESS,
            expected="media_flag",
            actual="ok" if self.flag_ok else "mismatch",
        )

    async def verify_playing(self, page: Any, adapter: Any, *args: Any, **kwargs: Any) -> VerifyResult:
        self.media_calls.append("playing")
        return VerifyResult(
            ok=self.playing_ok,
            kind=VerifyKind.MEDIA_PROGRESS,
            expected="progress",
            actual="Δ=2.00s" if self.playing_ok else "Δ=0.00s",
        )

    async def verify_resume_continuous(
        self, page: Any, adapter: Any, suspend_time: float, *args: Any, **kwargs: Any
    ) -> VerifyResult:
        self.media_calls.append("resume")
        return VerifyResult(
            ok=self.resume_ok,
            kind=VerifyKind.MEDIA_RESUME,
            expected="|Δt|<=2s",
            actual=f"suspend={suspend_time:.2f}" if self.resume_ok else "Δt=9.00s",
        )


# --------------------------------------------------------------------------- #
# 求解 / 视觉链
# --------------------------------------------------------------------------- #
def _text(content: str) -> LLMResponse:
    return LLMResponse(text=content, parsed=None, usage=TokenUsage(), latency_ms=1, raw="")


def _payload(payload: dict[str, Any]) -> LLMResponse:
    return _text(json.dumps(payload, ensure_ascii=False))


class FakeVisionProvider:
    """假视觉 provider：按**那一份固定格式**回答读图（题目 + 页面观测）。

    2026-09-30 起读图**只有一个契约**（``prompts/10-视觉组.md``）：模型一次回
    题目 JSON + 页面观测 JSON。所以这里也只有一条应答路径，不再是「按用途分成
    找控件 / 收尾确认 / 开局标定三套提示词」——那三套提示词已经删掉了。

    ``completed``（默认 ``True``）是这份应答里最要紧的字段：它决定收尾闸门
    「视觉组说做完了没有」。替身用例跑到「推不动」时，正是靠它干净收工。

    其余字段按需要给：``next_box`` / ``card`` / ``total`` / ``current`` /
    ``submit_scope`` 都会被**真实的** ``derive_plan`` 拿去裁决推进方式 ——
    于是「给一个『下一题』框就点它」这类结论在替身里也是真算出来的。
    """

    name = ProviderName.MOCK

    def __init__(
        self,
        *,
        completed: bool = True,
        next_box: tuple[float, float, float, float] | None = None,
        next_label: str = "下一题",
        card: PageCard | None = None,
        total: int | None = None,
        current: int | None = None,
        submit_scope: SubmitScope | None = None,
        submit_box: tuple[float, float, float, float] | None = None,
        scrolling: bool | None = None,
        questions: list[dict[str, Any]] | None = None,
    ) -> None:
        self.completed = completed
        self.next_box = next_box
        self.next_label = next_label
        self.card = card
        self.total = total
        self.current = current
        self.submit_scope = submit_scope
        self.submit_box = submit_box
        self.scrolling = scrolling
        self.questions = list(questions or [])
        self.calls: list[str] = []

    def model_for(self) -> str:
        return "fake-vlm"

    def reply(self) -> dict[str, Any]:
        """这一份固定格式的应答（读图只有这一个契约）。"""
        state = CompletionState.ALL_DONE if self.completed else CompletionState.NOT_DONE
        page: dict[str, Any] = {
            "completed": state.value,
            "total": self.total,
            "current": self.current,
            "scrolling": self.scrolling,
            "reason": "fake",
        }
        if self.next_box is not None:
            page["next_control"] = {"box": list(self.next_box), "label": self.next_label}
        if self.card is not None:
            page["card"] = self.card.model_dump(mode="json")
        if self.submit_box is not None or self.submit_scope is not None:
            submit: dict[str, Any] = {}
            if self.submit_box is not None:
                submit["box"] = list(self.submit_box)
            if self.submit_scope is not None:
                submit["scope"] = self.submit_scope.value
            page["submit"] = submit
        return {"page": page, "questions": self.questions, "more_below": False, "note": "fake"}

    async def complete(self, req: Any) -> LLMResponse:
        self.calls.append("read")
        if req.images:
            return _payload(self.reply())
        # 不带图 = 解题请求（同一个 provider 兼任判题链时的兜底）：回一份空作答，
        # 让调用方按「模型明确空作答」处理，而不是抛一个替身看不懂的错。
        return _payload({"chosen_labels": [], "confidence": 0.0})

    async def aclose(self) -> None:  # pragma: no cover - 替身
        return


class FakeSolver:
    """按题目返回固定作答；``review`` 用来触发 T0-3 的必停。

    ``providers`` 默认带一条可用的假视觉链：编排层的开局标定 / 找推进控件 /
    收尾确认都要问模型，一条都没有时跑批只会停在 ``advance_failed``。
    """

    def __init__(
        self,
        *,
        review: bool = False,
        labels: list[str] | None = None,
        providers: list[Any] | None = None,
        completed: bool = True,
    ) -> None:
        self.review = review
        self.labels = labels
        self.providers = (
            list(providers)
            if providers is not None
            else [FakeVisionProvider(completed=completed)]
        )
        self.calls: list[str] = []

    async def solve(self, question: Question, **kwargs: Any) -> Answer:
        self.calls.append(question.qid)
        return make_answer(question, labels=self.labels, review_flag=self.review)

    def last_vote(self) -> None:
        return None

    async def aclose(self) -> None:
        return


# --------------------------------------------------------------------------- #
# 组装
# --------------------------------------------------------------------------- #
class RecordingPacer:
    """限速钩子替身：记录被调用次数，不真的睡 5 秒。"""

    def __init__(self) -> None:
        self.clicks = 0
        self.submits = 0

    async def click(self) -> None:
        self.clicks += 1

    async def submit(self) -> None:
        self.submits += 1


def provider_name_for_tests() -> ProviderName:
    return ProviderName.MOCK
