"""核心数据模型（P0 契约层）。

本模块是全部 Pydantic 模型的唯一归属处。其中 ``QType`` / ``ActionKind`` /
``ActLevel`` / ``VerifyKind`` 定义在 :mod:`core.enums`，此处再导出，保证契约
要求的导入路径（``from core.models import ActionKind`` 等）有效。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from core.enums import (
    ActionKind,
    ActLevel,
    AdvanceMethod,
    CompletionState,
    MediaState,
    ProbeName,
    QType,
    QuestionState,
    SolvePath,
    SubmitScope,
    TaskType,
    VerifyKind,
)

__all__ = [
    "ActLevel",
    "ActionKind",
    "ActionResult",
    "AdvanceMethod",
    "Answer",
    "CapabilityReport",
    "CompletionCheck",
    "CompletionState",
    "DecisionTrace",
    "Episode",
    "MediaState",
    "Option",
    "PageCard",
    "PageControl",
    "PageSubmit",
    "PageView",
    "PerceptionResult",
    "QType",
    "Question",
    "QuestionState",
    "ReadBatch",
    "ReadOption",
    "ReadResult",
    "RunPlan",
    "SampleRecord",
    "SolvePath",
    "SubmitScope",
    "TaskItem",
    "TaskType",
    "VerifyKind",
    "VerifyResult",
    "VideoState",
    "VideoTask",
    "VoteResult",
]


class _Base(BaseModel):
    """统一模型基类：禁止未声明字段，便于尽早暴露契约漂移。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# --------------------------------------------------------------------------- #
# 题目
# --------------------------------------------------------------------------- #
class Option(_Base):
    """单个选项。

    ``index`` 是**打乱后**在页面上的呈现序号；``label`` 是 ``A``/``B``/``C`` …；
    ``text`` 是 NFKC 归一化后的选项正文；``raw`` 保留页面上抓到的原始串。
    """

    index: int
    label: str
    text: str
    raw: str


class Question(_Base):
    """结构化读出的一道题。``qid`` 不含答案（T0-1）。"""

    qid: str
    stem: str
    stem_hash: str
    qtype: QType
    options: list[Option]
    source: ProbeName
    #: 与题型一一匹配的已注册技能；供解题组加载专门规则。
    skill_id: str | None = None
    #: 读题时发现题型无技能、或 skill_id 缺失/不匹配；仅用于安全跳过。
    skill_error: str | None = None
    #: 模型原始报告的 skill_id，便于诊断无效 ID。
    reported_skill_id: str | None = None
    #: 视觉组报告的原始题型（未知题型落成 SINGLE 占位时保留）。
    reported_qtype: str | None = None
    channel_trace: list[str] = Field(default_factory=list)

    @property
    def option_texts(self) -> list[str]:
        """按页面呈现顺序返回选项正文。"""
        return [opt.text for opt in self.options]

    @property
    def option_labels(self) -> list[str]:
        return [opt.label for opt in self.options]


# --------------------------------------------------------------------------- #
# 作答
# --------------------------------------------------------------------------- #
class Answer(_Base):
    """一次作答结果（M2-3 / M3-7）。"""

    qid: str
    chosen_labels: list[str]
    chosen_texts: list[str]
    confidence: float
    #: 这次作答走的是哪条路径（正常 / Mock / 缓存）。**只有一种求解模式** ——
    #: 原先的 ``tier1`` / ``tier2`` 分级已删除，见 :class:`core.enums.SolvePath`。
    solve_path: SolvePath = SolvePath.SINGLE
    review_flag: bool
    model_name: str | None = None
    samples: int = 0
    #: M3-7 执行前重校验用。``sha1(_norm(题干正文))``，由 Solver 回填。
    stem_hash: str | None = None


class SampleRecord(_Base):
    """单次采样的原始记录，用于「每题记录采样明细」（M2 验收）。"""

    sample_index: int
    chosen_labels: list[str]
    chosen_texts: list[str]
    latency_ms: int
    error_code: str | None = None
    raw: str | None = None


class VoteResult(_Base):
    """一致性投票结果（M2-2）。计票按**选项内容**而非字母。"""

    chosen_labels: list[str]
    majority_ratio: float
    distribution: dict[str, int] = Field(default_factory=dict)
    n_samples: int = 0
    samples: list[SampleRecord] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 执行与校验
# --------------------------------------------------------------------------- #
class ActionResult(_Base):
    """一次动作执行的结果（M3-2 / M3-4）。"""

    kind: ActionKind
    target: str
    level_used: ActLevel
    ok: bool
    readback: str | None = None
    #: 这条 ``readback`` 是否**无需复核**（2026-10-01 加）。
    #:
    #: 为什么不能靠 ``readback`` 文本判断：成功动作写的是人类可读串
    #: （``region_mad=5.32 state=changed aim=ink_centroid`` / ``weak_changed:1.2 …``），
    #: 而期望值在 ``act.verifier`` 里是另一句话（``region_mad≥2.0``）——
    #: 拿两者做字符串比较会**恒不相等**，于是每一次成功点击都在详情页显示
    #: 「动作回读不一致」（用户实测报的就是这一条）。
    #:
    #: 语义只有一个：``True`` = 一致或该动作本来就没有可校验的回读
    #: （``skipped:`` / 手势 / 无候选点时的正常收工）；``False`` = 明确**没验成**
    #: （重放耗尽、提交超时这类必须复核的失败）。
    readback_ok: bool = True
    elapsed_ms: int = 0
    error: str | None = None
    #: P6 增量（见 README.md §P6）：阶梯到顶 / 提交超时时留档的截图引用。
    #: 「失败一律暂停 + 截图 + 留档，禁止静默跳过」这条硬约束要求失败结果**自带证据**，
    #: 而不是靠调用方记得去截图。
    screenshot_ref: str | None = None


class VerifyResult(_Base):
    """一次校验的结果（M3-5）。"""

    ok: bool
    kind: VerifyKind
    expected: str
    actual: str
    level_used: ActLevel | None = None
    screenshot_ref: str | None = None


# --------------------------------------------------------------------------- #
# 媒体
# --------------------------------------------------------------------------- #
class VideoState(_Base):
    """媒体状态。**一律读 ``<video>`` 属性，不得用元素可见性代替**（任务书 §2.1）。

    这是本项目**唯一**还读文档结构的读口，而且它读的不是题目：
    网课任务要判断「在播 / 暂停 / 放到第几秒 / 第几集」，
    这些量在页面上没有别的可靠来源。题目侧已经全部走模型。
    """

    paused: bool
    ended: bool
    current_time: float
    duration: float
    episode_index: int
    episode_total: int
    src: str | None = None


class VideoTask(_Base):
    """一个视频（分集）任务（T0-7 / M5-1）。"""

    vid: str
    course_id: str
    episode_index: int
    title: str
    resume_at: float = 0.0
    state: MediaState = MediaState.IDLE


class Episode(_Base):
    """课程分集目录里的一项（M5-1）。

    它只是**页面事实**（分集表），不是队列条目：队列条目是 :class:`TaskItem`
    （``type=video``），由 :func:`core.tasks.build_task_sequence` 按目录组装。
    ``vid`` 由站点在分集元素上直接给出（``data-vid``），因此这里**不重算**
    —— 算一遍只会多一处可能与页面漂移的第二真相。"""

    episode_index: int
    title: str
    vid: str
    duration: float = 0.0


# --------------------------------------------------------------------------- #
# 感知
# --------------------------------------------------------------------------- #
class DecisionTrace(_Base):
    """一次通道决策的来龙去脉，落进 ``perception.json``（M1-3）。

    名义归属是 ``core/arbiter.py``，但 :class:`PerceptionResult` 要引用它，
    放 arbiter 会形成 ``arbiter → models → arbiter`` 环，故在此定义并由
    ``core/arbiter.py`` 原样 re-export（见 README.md）。
    """

    chosen: ProbeName
    reason: str = ""
    conflicts: list[str] = Field(default_factory=list)
    #: 即便当前通道读到了，是否仍需按 T0-3 升级 Tier2（Canvas / 带图 / 截断）
    needs_vision: bool = False
    #: 仲裁「双失败」→ **暂停留档，绝不静默跳过**
    paused_for_dump: bool = False


class PerceptionResult(_Base):
    """一次感知的整体产出（M0-4 / M0-7）。"""

    question: Question | None = None
    video_state: VideoState | None = None
    channel_used: ProbeName
    warnings: list[str] = Field(default_factory=list)
    screenshot_ref: str | None = None
    #: 通道间不一致 / 双失败时置真，交人工复核（M1-3）
    review_required: bool = False
    #: 本次仲裁的决策留痕（M1-3）
    trace: DecisionTrace | None = None


class ReadOption(_Base):
    """视觉「读题」从图里读出的一个选项，**带几何**。"""

    #: 呈现标号（A / B / C…）。模型若没给就用序号补。
    label: str
    text: str
    #: 归一化包围框 ``(x, y, w, h)``，取值 ``0..1``，相对**整张图**左上角。
    #:
    #: 用归一化而不是像素：模型看到的图可能被它自己缩放过，
    #: 像素坐标会随分辨率漂移；归一化后由调用方按「图的实际尺寸 / 视口尺寸」
    #: 一次性换算，换算点只有一个。
    box: tuple[float, float, float, float]


class ReadResult(_Base):
    """视觉「读题」的产出：题干、选项、技能选择与几何。"""

    stem: str
    qtype: QType
    options: list[ReadOption]
    #: 根据 qtype 从本地技能注册表解析出的技能 ID；不要求视觉组重复生成。
    skill_id: str | None = None
    #: 技能缺失 / 不匹配 / 题型不支持的安全处置原因。
    skill_error: str | None = None
    #: 视觉组原始返回的 skill_id（排查模型偏离约定时使用）。
    reported_skill_id: str | None = None
    #: 视觉组报告的原始题型（未知题型占位时保留）。
    reported_qtype: str | None = None
    #: 模型报告的原始题型，或当前技能库不支持的题型。
    unsupported_qtype: str | None = None
    #: 这一屏里从上往下第几道题（1 起）。模型给的 ``index``，读不到就是 0。
    #:
    #: 一屏多题之后它才有意义：``index`` 与 :attr:`num_text` 一起用于判断
    #: 「推进之后是不是真的换了题」，以及「有没有跳过中间某道」。
    index: int = 0
    #: 页面上**印的**题号（如 ``"1."``）；页面上没有题号就是 ``None``。
    #:
    #: 与 :attr:`index` 是两回事：``index`` 是「在我给你的这张图里排第几」，
    #: ``num_text`` 是「卷子上写的是第几题」。题库编号可能不连续，两者对不上是正常的。
    num_text: str | None = None

    # ---- 质量字段（2026-09-28 加，门禁的判据）-------------------------------- #
    #:
    #: 为什么要有这一组：读题是**唯一一个「错了也不报错」的环节**。
    #: 视觉模型抄出来的题面如果少了一个负号、漏了一个指数、被视口截掉半行，
    #: 输出看起来**完全正常** —— 下游会拿它去解题，得到一个看起来正常的错误答案，
    #: 然后照着坐标点到用户的页面上。这三个字段是唯一能让那种错误**浮出水面**的东西。

    #: 被画面边缘切掉、或没能读出的部分。词汇统一为 ``"stem"`` / ``"option:D"`` /
    #: ``"stem.formula.1"``。**非空 = 这份题面残缺**。
    #:
    #: （``clipped`` 与「模型没提」是两件事：模型没提就是空列表，代表它认为抄全了。）
    clipped: list[str] = Field(default_factory=list)
    #: 模型自己声明「拿不准」的项，词汇同 :attr:`clipped`。**非空 = 需要复核**。
    #:
    #: 视觉模型的置信度校准很差，"报一个分数"没有意义；有用的是让它**点名**
    #: 哪几处没把握 —— 那些位置要么重新裁图复核，要么整题停下来问人。
    uncertain: list[str] = Field(default_factory=list)
    #: 画面下方是否还有内容被截断（多题同页 / 长题干）。
    #: 只进留痕与门禁，**不单独作为拦截理由**（它只是"可能还有"）。
    more_below: bool = False
    #: 模型给的一句话依据或异常说明。只进留痕，**不参与任何判定** ——
    #: 它没有结构，拿自然语言做判断等于把散文当开关。
    note: str = ""


class ReadBatch(_Base):
    """**一屏**读到的全部题目（视觉组一次模型调用的完整产出）。

    2026-09-28 起视觉组一次读一屏、把**完整的题目都抄出来**（见
    ``prompts/10-视觉组.md``），所以下游拿到的是一批而不是一道。

    这一条是「减少消耗」的落点：长页面上常见一屏两三道题，
    以前每道题都要**重新截一张图 + 调一次模型**（题干、选项、坐标全再算一遍）；
    现在一次调用拿回整屏，后面的题直接复用。

    为什么被切掉半截的题**不进** ``questions``：下游拿到的每一道都必须能独立作答 ——
    残缺题面看起来完全正常，却少了一半，那种错误在下游是**看不见的**。
    """

    questions: list[ReadResult] = Field(default_factory=list)
    #: **这一屏的页面观测**（2026-09-30 加）。视觉组的固定格式里除了题目，
    #: 还有一份「这一屏长什么样」的**观测**：进度文字、有没有「下一题」控件、
    #: 有没有题号答题卡、提交按钮管多大范围、是不是已经全部答完。
    #:
    #: ⚠️ 它是**观测**，不是判断：视觉组只如实报告「画面上有什么」，
    #: 「该怎么推进 / 什么时候提交」由 :func:`core.run_plan.derive_plan` 在
    #: **开局裁决一次**。两者分开是刻意的 —— 让视觉组回答「怎么进下一题」
    #: 等于把控制流交给一个看不见程序的模型，而它每次的说法都可能不一样。
    page: PageView | None = None
    #: 画面下方是否还有内容被截断（这批之后还有题）。
    more_below: bool = False
    #: 一句话说明（本屏几道题 / 有没有异常）。
    note: str = ""
    #: 模型这次的**原始回复**（截断到 2000 字）。排障用。
    #:
    #: 为什么要有它：2026-09-28 的 `region_mad=0.00` 事故里，最关键的证据
    #: （模型到底给了多大的 box）**因为没留痕而无法直接看到**，只能靠截图反推。
    #: 读题的原始回复必须留下来 —— 它是「坐标为什么是这个值」的唯一出处。
    raw: str = ""


class PageControl(_Base):
    """画面上一个**固定的推进控件**（「下一题 / 下一页 / 继续」）的观测。"""

    #: 归一化包围框 ``(x, y, w, h)``，相对**整张截图**。
    box: tuple[float, float, float, float]
    #: 控件上的文字（只有图标就是 ``None``）。只进留痕，不参与判定。
    label: str | None = None


class PageCard(_Base):
    """**题号答题卡**（题号网格）的观测。

    为什么它值得单独一组字段：真实作业页（一屏一题、44 题）**没有**「下一题」按钮，
    切题只能点这个网格里的题号。旧的标定只说得出一句 ``click`` 而给不出落点，
    于是做到一半就卡住。有了 ``box`` + ``cols`` / ``rows`` + 当前格，
    程序可以**按题号推算**每一格的落点（见 :func:`core.run_plan.grid_geometry`）。
    """

    #: 答题卡整体区域（可见部分）的归一化框。
    box: tuple[float, float, float, float]
    #: 网格的列数与行数（看得见的部分）。任一为 0 表示「看不出网格」→ 不能推算。
    cols: int = 0
    rows: int = 0
    #: **当前题号**那一格（用来校准网格原点：算出来的格子与它对上才算准）。
    current_box: tuple[float, float, float, float] | None = None
    #: **下一题号**那一格（画面上看得见就给；这是最直接的落点）。
    next_box: tuple[float, float, float, float] | None = None


class PageSwipe(_Base):
    """**滑动推进**的观测（题面同屏时用「滑多远」切到下一题）。

    为什么滑动需要模型给幅度：题目排在一张长页 / 一个横向卡片流里时，没有可点的
    「下一题」按钮，切题靠手势 —— 而**滑多了就是静默跳题**（2026-09-28 真机：
    题号从 3 直接跳到 16）。幅度只能由看见画面的人回答：这一屏的题占多高、
    下一题在哪个方向、要移动多少屏才刚好把它带进来。

    ``amplitude`` 是**归一化比例**（相对视口宽或高），与 ``page`` 的坐标同一口径：
    调用方拿这次截图的尺寸换算成像素，换算点只有一个。
    """

    #: 滑动方向：``"up"``（向下滚动看下一题）/ ``"left"``（横向卡片流往左翻）。
    direction: str
    #: 移动幅度：视口宽 / 高的比例。必须 ``> 0``，且不允许超过一整屏。
    amplitude: float
    #: 一句话依据（只进留痕）。
    reason: str = ""


class PageSubmit(_Base):
    """提交类按钮的观测（按钮本身 + 它管的范围）。"""

    box: tuple[float, float, float, float] | None = None
    #: 这个按钮提交的范围。``None`` = 画面上没有提交按钮 / 模型没说。
    scope: SubmitScope | None = None


class PageView(_Base):
    """视觉组对**这一屏页面**的固定格式观测（不含任何判断）。

    它是「视觉组只解读照片、把照片翻译成固定格式」这条分工的落点：
    所有视觉请求（读题 / 开局 / 收尾）都返回**同一份 schema**，
    只是不同时机用到的字段不同。程序据此裁决推进方式、提交范围与是否收工。
    """

    #: 页面上印的进度文字，**原样抄**（如 ``"16 / 44 题"``、``"第 3 题 / 共 20 题"``）。
    progress: str | None = None
    #: 从进度文字看得出**总题数**就填，看不出来写 ``null``（**不要猜**）。
    total: int | None = None
    #: 当前是第几题；看不出来写 ``null``。
    current: int | None = None
    #: 画面上那个固定的「下一题 / 下一页 / 继续」控件；没有就 ``null``。
    next_control: PageControl | None = None
    #: 题号答题卡；没有就 ``null``。
    card: PageCard | None = None
    #: 提交类按钮（含它管的范围）；没有就 ``null``。
    submit: PageSubmit | None = None
    #: 整卷是不是已经**全部答完**（收尾闸门用的观测）。
    completed: CompletionState = CompletionState.UNKNOWN
    #: 页面能否向下滚动（辅助裁决：能不能用滚动推进）。看不出写 ``null``。
    scrolling: bool | None = None
    #: 视觉组选中的**推进技能 ID**（``advance_click`` / ``advance_swipe`` /
    #: ``advance_card``）。它回答的是「这一步该怎么进下一题」——
    #: 与题型技能同一套注册表纪律：只认注册过的 ID，读不出来就 ``None``
    #: （程序退回开局裁决的那一套，绝不瞎猜）。
    advance_skill_id: str | None = None
    #: 滑动推进的观测（``advance_swipe`` 时才给）。
    swipe: PageSwipe | None = None
    #: 一句话依据。**只进留痕**，不参与判定（拿散文当开关是没有结构可言的）。
    reason: str = ""


# --------------------------------------------------------------------------- #
# 开局方案（**整条推进与提交逻辑的唯一依据**）
# --------------------------------------------------------------------------- #
class RunPlan(_Base):
    """**开局裁决**的产物：整条运行只按它推进与提交（2026-09-30 加）。

    它取代了旧的 :class:`TaskCalibration`（只有 ``total`` + ``method``）——
    旧版说得出「怎么推进」，但**给不出落点**：``method=click`` 之后，
    每一步还得再截一张图、再问一次模型「那个按钮在哪」（``find_advance_control``），
    或者退回去滚动、滑动地换招试。真机上那条路的表现是「做到一半就停下」。

    现在的纪律只有一条：**开局裁决一次，之后全程照它执行**。

    * 裁决在 :func:`core.run_plan.derive_plan`，输入是视觉组的观测
      （:class:`PageView`）+ 本次读到的题数，输出就是本模型；
    * 运行期**不再**问模型「怎么进下一题」，也**不再**有换招阶梯；
    * 几何（控件框 / 答题卡步距）在开局算好，之后按题号推算。

    推不动时**不换招**：如实停下并请视觉组确认「是不是全部完成了」——
    「确认不了就停下等人」永远比「静默跳过十几道题」好。
    """

    #: 裁决出的推进方式（**一定是可执行的那个**，不会是 ``UNKNOWN``）。
    method: AdvanceMethod = AdvanceMethod.UNKNOWN
    #: 题目总数（观测到的）。``None`` = 不知道 —— 此时 ``reached_total()`` 恒为 False。
    total: int | None = None
    #: 开局时是第几题（观测到的）。
    current: int | None = None

    # -- CLICK：固定控件 -- #
    control_box: tuple[float, float, float, float] | None = None
    control_label: str | None = None

    # -- CARD：题号答题卡 -- #
    card: PageCard | None = None
    #: 格子中心的归一化步距 ``(dx, dy)``（往下一题号走一格）。
    card_step: tuple[float, float] | None = None
    #: 题号 1 所在那一格的**中心**（归一化）。``None`` = 网格推算不出来。
    card_origin: tuple[float, float] | None = None
    #: 开局时那一格的题号（``current``）。缺省当 1。
    card_anchor: int = 1

    # -- 提交 -- #
    #: 本次运行的提交范围。``None`` = 观测没给出（由裁决从结构证据推断）。
    submit_scope: SubmitScope | None = None
    #: 提交按钮的归一化框（观测到的）。
    submit_box: tuple[float, float, float, float] | None = None

    #: 裁决依据（人话），直接进事件流给用户看。
    reason: str = ""
    #: 观测原文（排障用；裁决看起来不对时先看它）。
    raw: str | None = None

    @property
    def knows_total(self) -> bool:
        """题目总数是否可用（0 与负数都是**无效**结果，按不知道处理）。"""
        return self.total is not None and self.total > 0

    @property
    def numbers_known(self) -> bool:
        """**题号**是否可信（观测到了「当前第几题」）。

        它管的是答题卡那一路：题号是算术落点的自变量，
        而落点算完之后还要用它校验「读回来的题是不是预期的那道」。
        观测没给出当前题号时，落点仍然可以由「模型直接指出的下一题号格」
        （``card.next_box``）推出来 —— 所以 CARD **照样可用**，
        但**不能**做那个校验：拿一个猜出来的题号去比对，会把正常推进判成跳题。
        """
        return self.current is not None and self.current > 0

    @property
    def card_start_number(self) -> int:
        """答题卡推进的**起始题号**：观测到的当前题号，没有就按 1（题号从 1 开始）。

        单独开一个属性而不是让调用方自己判 ``current``：调用方各写一遍
        「``not None`` 且 ``> 0``」，迟早有一处漏掉一半（``current=0`` 或负数），
        那会让 ``card_target`` 算出一个负号题号的格子 —— 点在答题卡之外。
        """
        current = self.current
        if current is None or current <= 0:
            return 1
        return current

    def reached_total(self, done: int) -> bool:
        """已完成 ``done`` 题时，是否已达到总数。

        不知道总数就**永远不算到** —— 不能拿「不知道」当「做完了」。
        """
        return self.knows_total and done >= (self.total or 0)

    @property
    def uses_card(self) -> bool:
        """答题卡几何是否可用（没有步距就只能退回 CLICK）。"""
        return self.card is not None and self.card_step is not None

    def card_target(self, number: int) -> tuple[float, float, float, float] | None:
        """题号 ``number`` 那一格的归一化框；推算不出来返回 ``None``。

        ``None`` 是**必须被尊重**的结果：宁可停下，也不能拿一个编出来的格子
        去点用户的页面（那是一次真实的误点）。
        """
        from core.run_plan import card_cell_box

        return card_cell_box(self, number)



# --------------------------------------------------------------------------- #
# 任务与能力
# --------------------------------------------------------------------------- #
class TaskItem(_Base):
    """队列中的一条任务。题目态与媒体态共用此结构。"""

    item_id: str
    type: TaskType
    qid: str | None = None
    vid: str | None = None
    state: QuestionState | MediaState
    attempts: int = 0
    suspended: bool = False
    created_at: datetime
    updated_at: datetime


class CompletionCheck(_Base):
    """**收尾确认**：再问视觉组一次「是不是全部做完了」。

    触发时机只有两个（都在编排层）：
    1. 找不到「下一题」（推进失败）；
    2. 已完成数达到标定总数。

    纪律：``completed=False`` 时**绝不允许静默收工** —— 必须按推进失败停下，
    让人看到卡在哪一题。这是整个流程里唯一防「跳题」的闸门。
    """

    completed: bool
    #: 模型给的一句话理由。
    reason: str = ""
    #: 模型原文（排障用）。
    raw: str | None = None


class CapabilityReport(_Base):
    """[测试连接] 实测结果（M2-6）。**不接受手填**。"""

    auth_ok: bool
    #: 模型名可用且能正常回包。**2026-09-28 起是单模型**（原先分 tier1_ok / tier2_ok，
    #: 模型库改成「一套配置一个模型」后这两个勾必然一样，就合成一个）。
    model_ok: bool
    supports_vision: bool
    supports_structured_output: bool
    image_payload: str | None = None  # "base64" | "url" | None
    max_qps: float = 0.0
    latency_ms: int = 0
    error_code: str | None = None  # 见规划书 §2.4

    #: 视觉能力**实际探的是哪个模型**（单模型后 = 这套配置的模型）。
    vision_model: str | None = None

    #: 视觉判定的证据强度。**这是「测试连接」能不能信的关键**：
    #:
    #: ======================  ====================================================
    #: ``read_digit``          探针图内容已知，模型答对了 → **强证据**
    #: ``accepted``            服务端收下了图（HTTP 2xx）但没答对/没答完 → 弱证据
    #: ``None``                HTTP 4xx 拒绝，或未探测
    #: ======================  ====================================================
    #:
    #: 之所以要分级：只看 HTTP 状态码是不够的 —— 有的厂商会 200 但把图片丢掉，
    #: 于是「支持视觉」这句结论其实没有依据。界面上「弱证据」要如实标出来。
    vision_evidence: str | None = None
