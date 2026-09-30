"""AutoLearn 全局枚举（P0 契约层，零依赖）。

本模块是**唯一**的枚举定义处，集中放置所有跨模块复用的枚举，用于切断
``core.config`` / ``core.models`` / ``core.tasks`` / ``core.states`` 之间的循环导入：

    core.config  ──▶ core.tasks ──▶ core.models ──▶ core.enums
    core.states  ──▶ core.enums

契约说明
--------
枚举的**逻辑归属**仍按《AutoLearn 实施规划书》§2.1 执行——各归属模块以
``from core.enums import X`` 的形式再导出，因此下列导入路径全部有效：

    from core.config import ProbeName, TierUsed
    from core.states import QuestionState, MediaState
    from core.tasks  import TaskType
    from core.models import QType, ActionKind, ActLevel, VerifyKind
    from act.actuator import ActionKind, ActLevel
    from act.verifier import VerifyKind
    from solve.providers.base import ProviderName

v0.2.0：本程序**只使用模型**读页面。原先的「DOM / 网络 / 视觉」三通道与
「DOM 优先 / 模型优先」选择一并删除，``ProbeName`` 只剩视觉一条题目通道，
外加不在题目链里的媒体态读口 ``MEDIA``。

禁止在本模块 import 任何其他 core 模块。
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "ActLevel",
    "ActionKind",
    "AdvanceMethod",
    "CompletionState",
    "ErrorCode",
    "MediaState",
    "ProbeName",
    "ProviderName",
    "QType",
    "QuestionState",
    "SolvePath",
    "SubmitScope",
    "TargetKind",
    "TaskType",
    "TierUsed",
    "VerifyKind",
]


class ProbeName(StrEnum):
    """可用探针标识（v0.2.0 起只剩两条）。

    ============  ==========================================================
    ``VISION``    截当前视口，交给模型读题（**唯一**的题目通道）
    ``MEDIA``     读 ``<video>`` 三态，**不在题目探针链里**（见 ``run_video``）
    ============  ==========================================================

    「DOM 通道 / 网络通道」与随之而来的「DOM 优先 / 模型优先」选择已整体删除 ——
    页面只经模型的眼睛读，不再解析文档结构、也不再被动抓 XHR 响应。
    """

    VISION = "vision"
    MEDIA = "media"


class SolvePath(StrEnum):
    """本次作答走的是哪条路径（2026-09-29：**Tier 分级整体删除**）。

    ============  ==========================================================
    ``SINGLE``    正常作答 —— 一次取样；开了「复算」时按配置次数取样投票
    ``MOCK``      没配模型，由 ``MockProvider`` 直接读地面真值（**仅供靶场自检**）
    ``CACHE``     精确缓存命中：不调模型，直接返回既有 ``Answer``
    ============  ==========================================================

    原先的 ``tier1`` / ``tier2`` 两档（以及「一致率不足就升级复算」那套路由）
    已随「只使用模型」的收敛一并删除：**只剩一种求解模式**，
    「要不要复算、复算几次」现在由运行配置 ``RunConfig.recalculate`` 显式决定，
    而不是由一致率触发的隐式升级。

    老库 / 老留痕里的 ``"tier1"`` / ``"tier2"`` 由 :meth:`parse` 兼容读回 ``SINGLE``。
    """

    SINGLE = "single"
    MOCK = "mock"
    CACHE = "cache"

    @classmethod
    def parse(cls, raw: object) -> SolvePath:
        """把任意来源的取值规整成本枚举。**认不出来一律按 ``SINGLE``**。

        兼容口径刻意的：历史 ``answer.tier_used`` 列里存着 ``"tier1"`` / ``"tier2"``，
        续跑要能读回来；把它当错误抛出去只会让一条旧库彻底打不开。
        """
        token = str(raw or "").strip().lower()
        if token in {"mock"}:
            return cls.MOCK
        if token in {"cache"}:
            return cls.CACHE
        return cls.SINGLE


#: 向后兼容别名：旧代码 / 旧测试仍按 ``TierUsed`` 导入。
#:
#: ⚠️ 只保留 ``MOCK`` / ``CACHE`` 与新的 ``SINGLE``；``TIER1`` / ``TIER2``
#: **已删除**，引用它们会在导入期就是一个 ``AttributeError`` —— 这是刻意的，
#: 免得某处还在按档位分派却没人发现。
TierUsed = SolvePath


class QuestionState(StrEnum):
    """题目九态（T0-2）。``SUBMITTED`` 是唯一危险态。"""

    PENDING = "pending"
    PERCEIVED = "perceived"
    SOLVED = "solved"
    PENDING_CONFIRM = "pending_confirm"
    APPLIED = "applied"
    SUBMITTED = "submitted"
    VERIFIED = "verified"
    FAILED = "failed"
    SKIPPED = "skipped"


class MediaState(StrEnum):
    """媒体任务六态（M5-4），与题目九态并存。"""

    IDLE = "idle"
    PLAYING = "playing"
    PAUSED = "paused"
    INTERRUPTED = "interrupted"
    RESUMED = "resumed"
    ENDED = "ended"


class TaskType(StrEnum):
    """任务类型（T0-8 / M5-1）。"""

    VIDEO = "video"
    QUIZ = "quiz"


class TargetKind(StrEnum):
    """目标类型（P11）。

    它决定「哪些东西可抓」。v0.2.0 起两条题目通道都只剩视觉一条，
    所以目标类型不再影响通道选择 —— 只影响**怎么拿到画面**：
    浏览器页走 CDP 附加，原生窗口走 Win32 截图。
    """

    #: 浏览器里的一个页面（附加到用户已开的标签页）
    BROWSER_PAGE = "browser_page"
    #: 原生桌面窗口（刷题 / 网课客户端）
    DESKTOP_WINDOW = "desktop_window"


class QType(StrEnum):
    """支持识别的题型；单选、多选与判断分别绑定技能。"""

    SINGLE = "single"
    MULTIPLE = "multiple"
    TRUE_FALSE = "true_false"


class CompletionState(StrEnum):
    """视觉组对「整卷是不是已经全部答完」的**观测**（2026-09-30 加）。

    它是收尾闸门的观测口径，与「推进方式」无关。取值刻意只有三种，
    而且**默认方向是「不知道」**：

    ================  ==========================================================
    ``ALL_DONE``      画面上有明确证据说明整卷做完了（进度 20/20、只差交卷…）
    ``NOT_DONE``      画面上还有未完成的题（进度 3/20、还有可用的「下一题」…）
    ``UNKNOWN``       看不出来（**默认值**）
    ================  ==========================================================

    判错的代价不对称：把「还没做完」说成 ``ALL_DONE`` 会让后面所有题都不被作答，
    而说成 ``NOT_DONE`` / ``UNKNOWN`` 最坏只是停下来等人看一眼。
    所以裁决层（``core.run_plan``）只在 ``ALL_DONE`` 时允许收工。
    """

    ALL_DONE = "all_done"
    NOT_DONE = "not_done"
    UNKNOWN = "unknown"


class SubmitScope(StrEnum):
    """提交按钮的**作用范围**（2026-09-28 加；定义见 ``prompts/10-视觉组.md``）。

    页面上的提交类按钮不是一回事，程序必须知道它管多大范围，才能决定**什么时候点**：

    =================  ==========================================================
    ``QUESTION``       只提交**当前这道题**（点完这题结束、出现下一题）
    ``PAPER``          提交**整份试卷 / 作业**，一按就结束整场
    =================  ==========================================================

    ``None``（不是本枚举的取值）代表「模型没说」或「页面上没有提交按钮」——
    与 ``QUESTION`` **不是一回事**，别把「不知道」当成「本题提交」。

    为什么非分不可：真实作业页上「交卷」是全页唯一的按钮，而程序原先**每题**都点
    一次 ``submit_box`` —— 做完第 1 题就去交卷。2026-09-28 真机留痕
    ``region_mad=0.06``（按钮此时被禁用，点了毫无反应），题目以 ``submit_timeout``
    停下。整卷场景下这个值必须是 ``PAPER``：提交推迟到全部题做完的收尾阶段。
    """

    QUESTION = "question"
    PAPER = "paper"


class AdvanceMethod(StrEnum):
    """「怎么进入下一题」—— **开局判定一次，之后全程只用它**（2026-09-30 收口）。

    这一版与上一版最大的差别是**谁做决定**：

    * 视觉组只做**观测**（固定格式：这一屏有哪些控件、有没有题号答题卡、
      进度文字写的是什么），它**不回答**「该怎么推进」；
    * 程序按固定规则从观测里**裁决**出唯一一种方式（见 :mod:`core.run_plan`），
      裁决结果就是整条运行里唯一的推进逻辑 —— 不再有「这一步点按钮、
      下一步去滚动」的换招，也不再有每步重新问模型的「找下一题」调用。

    ``UNKNOWN`` 只在**裁决之前**出现（表示「还没定」）。裁决一定会给出一个
    可执行的方式，所以运行期不会拿 ``UNKNOWN`` 去推页面 ——
    「不知道」不等于「四招都试一遍」。
    """

    CLICK = "click"
    """点画面上的**固定控件**（「下一题 / 下一页 / 继续」）。含「要滚动才出现」的情况。"""

    CARD = "card"
    """点**答题卡上的题号格**（左侧/右侧那个题号网格）。

    真实作业页最常见的一种：一屏一题的整卷页面，**没有**「下一题」按钮，
    切题靠点答题卡里的题号。旧的标定只能说 ``click`` 而给不出落点，
    于是做题做到一半就停在「找不到提交按钮 / 找不到下一题」。
    """

    SWIPE = "swipe"
    """滑动翻页（整屏一张答题卡那类）。"""

    SCROLL = "scroll"
    """向下滚动即进入下一题（下一题就在下面那个区块）。"""

    UNKNOWN = "unknown"
    """**未裁决**的占位值，不是运行期可用的推进方式。"""


class AdvanceSkill(StrEnum):
    """**推进技能 ID**：视觉组选一个，程序照它执行（2026-10-01 加）。

    与 :class:`AdvanceMethod` 的关系：``AdvanceMethod`` 是**程序侧**的执行方式，
    由 :func:`core.run_plan.derive_plan` 从观测里裁决；本枚举是**视觉组侧**的技能
    ID，由模型在读图时直接选。两者取值一一对应（``click``/``card``/``swipe``），
    但**来源不同**，所以不能合并成一个枚举 —— 合并之后就分不清
    「这个落点是模型看画面给的，还是开局算出来的」了，而两者的新鲜度完全不同。

    为什么要有它：用户实测「推进之后读到的题与实际不符」，根因之一是拿**开局那一屏**
    算好的坐标去点**后来已经变化**的画面。把推进方式做成技能之后，视觉组**每一步**
    都会按当前画面重新给一次落点（按钮框 / 下一格 / 滑动幅度），
    程序优先用这份**新鲜观测**，开局几何只作兜底。

    只注册三个：点控件、点答题卡、滑动。滚动（``scroll``）不进技能表 ——
    「滚一步读一屏确认新题进来了」本身就是程序侧的循环，不需要模型给落点。
    """

    CLICK = "advance_click"
    """点画面上的「下一题 / 下一页 / 继续」控件；落点由视觉组给出 ``next_control.box``。"""

    CARD = "advance_card"
    """点题号答题卡里「下一题号」那一格；落点由视觉组给出 ``card.next_box``。"""

    SWIPE = "advance_swipe"
    """手势推进（题目同屏、没有可点的下一题按钮）；**必须**给出方向与滑动幅度。"""


class ActionKind(StrEnum):
    """执行层动作类型（M3-2 / M3-3）。

    ``SWIPE`` 是 P12 增量：「下一题」在有些页面里**没有按钮可点**，只能滑动。
    v0.2.0 起题目侧的点击一律走**模型给出的坐标**（:attr:`ActLevel.L6_VISION_XY`），
    媒体侧（播放 / 暂停 / 定位 / 下一集）仍走媒体锚点阶梯。
    """

    CLICK = "click"
    SELECT_OPTION = "select_option"
    SUBMIT = "submit"
    PLAY_MEDIA = "play_media"
    PAUSE_MEDIA = "pause_media"
    SEEK_MEDIA = "seek_media"
    NEXT_EPISODE = "next_episode"
    #: 滑动（下一页 / 下一题）。**手势，不是点击**：见 ``act.actuator.Actuator.swipe``
    SWIPE = "swipe"


class ActLevel(StrEnum):
    """执行阶梯（M3-2）。

    v0.2.0 起题目侧**只用** :attr:`L6_VISION_XY`（模型给坐标）；
    L1~L5 仍由**媒体控件**使用（播放 / 暂停 / 定位 / 下一集），
    它们的锚点来自 ``selectors_media.yaml``，与题目无关。
    """

    L1_LOCATOR = "l1_locator"
    L2_FORCE = "l2_force"
    L3_SCROLL = "l3_scroll"
    L4_FOCUS_KEYS = "l4_focus_keys"
    L5_BBOX = "l5_bbox"
    L6_VISION_XY = "l6_vision_xy"


class VerifyKind(StrEnum):
    """校验类型（M3-5 / M5 进度断言）。

    v0.2.0 起题目侧**只有** :attr:`SCREENSHOT_DIFF`：不再读 DOM 属性，
    改为比对点击前后选项区域像素。媒体侧三条断言照旧（媒体态本身就是
    ``<video>`` 的属性，不属于「题目通道」）。
    """

    #: 选项区域点击前后的像素差分（题目侧唯一判据）
    SCREENSHOT_DIFF = "screenshot_diff"
    #: 媒体态标志（``paused`` 是否等于期望值）
    READBACK = "readback"
    MEDIA_PAUSED = "media_paused"
    MEDIA_PROGRESS = "media_progress"
    MEDIA_RESUME = "media_resume"
    EPISODE_INDEX = "episode_index"
    SUBMIT_RESULT = "submit_result"


class ProviderName(StrEnum):
    """模型 Provider 实现标识（M2-1）。"""

    OPENAI_COMPAT = "openai_compat"
    MOCK = "mock"


class ErrorCode(StrEnum):
    """错误码字典（规划书 §2.4），前后端与日志共用。进字典才准用。

    P6 增量补了 3 个**执行层**错误码：
    六级阶梯到顶、题干身份不符、媒体动作失败都必须有名字，
    否则执行层只能把它们混成一个「动作失败」，UI 与留痕就分不出该怎么处置。
    """

    NO_CONFIG = "no_config"
    AUTH_FAILED = "auth_failed"
    MODEL_NOT_FOUND = "model_not_found"
    RATE_LIMITED = "rate_limited"
    VISION_UNSUPPORTED = "vision_unsupported"
    STRUCTURED_UNSUPPORTED = "structured_unsupported"
    READBACK_MISMATCH = "readback_mismatch"
    SUBMIT_TIMEOUT = "submit_timeout"
    STACK_RESTORED = "stack_restored"

    # -- P6 执行层 ------------------------------------------------------------ #
    #: 执行阶梯走到顶仍拿不到期望状态 → 暂停 + 截图 + 记 failed（T0-5）
    ACTION_LADDER_EXHAUSTED = "action_ladder_exhausted"
    #: 执行前重校验发现题干与落库答案对不上 → 拒绝执行、回「待复算」（M3-7）
    STEM_HASH_MISMATCH = "stem_hash_mismatch"
    #: 媒体动作（播放 / 暂停 / 定位 / 下一集）未能达成期望态
    MEDIA_ACTION_FAILED = "media_action_failed"

    # -- P11 目标采集层 ------------------------------------------------------- #
    #: 目标不存在 / 已关闭 / 附加失败。与「模型读不到」区分开：
    #: 读不到可能是页面形态问题，目标不可用是**根本没接上**。
    TARGET_UNAVAILABLE = "target_unavailable"
    #: 浏览器不是带调试端口启动的，CDP 附加不上去（P11 最高频的失败）
    BROWSER_NO_DEBUG_PORT = "browser_no_debug_port"

    # -- 推进失败（2026-09-28 起**唯一**的收尾失败码） ------------------------ #
    #: 「下一题」找不到，而且让视觉组再看一次也**没能确认「已全部完成」**。
    #:
    #: P14 曾为「判不出是不是最后一题」单独开过一个码（``last_question_uncertain``）
    #: 并让界面弹两个按钮问人；用户实测反馈那套判定**逻辑有严重问题**，
    #: 已整套推倒 —— 现在改成「让视觉组再确认一次」：
    #: 确认完成 → 干净收工；确认**没完成**或看不出来 → 就用这个码停下。
    #: **绝不静默跳过**：停下时人能看到「卡在哪一题、为什么」。
    ADVANCE_FAILED = "advance_failed"
