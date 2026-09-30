"""运行配置与全局阈值（T0-3 / T0-5 / T0-6 / M4-2）。

8 项 T0 定义中，有 4 项（阈值、重放处置、M2 止损数字、限速）落在本模块，
全部以**具名配置项**形式存在，代码里不得再出现裸数字。

字段语义表（改任何默认值 = 改契约，须同步 ``README.md``）
--------------------------------------------------------------------------
======================  ==================================================
``guards.agreement_accept``         复算多数票一致率 ≥ 此值直接采用，否则标 ⚠复核
``guards.confidence_review_min``    不复算时模型自报 confidence 低于此值 → 标 ⚠复核
``guards.single_top1_min``          T0-6 M2 闸门：单选 Top-1
``guards.multi_exact_min``          T0-6 M2 闸门：多选完全匹配
``guards.valid_ratio_min``          T0-6 M2 闸门：一致率达标题占比
``guards.click_replay_max``         T0-5 回读不一致重放上限
``guards.click_replay_gap_ms``      T0-5 重放间隔区间（随机取值）
``rate.click_gap_ms``               M4-2 动作级限速
``rate.submit_gap_s``               M4-2 提交级限速
``rate.llm_concurrency_free``       M4-2 请求级并发（免费档）
``rate.llm_concurrency_paid``       M4-2 请求级并发（付费档）
``recalculate``                     要不要复算（false → 以第一次答案为准）
``sample_n``                        复算时的采样次数（含第一次；不复算时恒按 1）
``training``                        成功收尾后是否自动跑一次训练模式
``guards.advance_visual_fallback``  P12 「推进下一题」找不到按钮时是否走视觉 / 滑动兜底
``guards.advance_swipe_mode``       P12 手势事件类型（``mouse`` 拖拽 / CDP ``touch``）
``guards.advance_swipe_directions`` P12 滑动方向的尝试顺序（空 = 禁用滑动）
``guards.advance_calibrate``        开局标定：视觉组判「题目总数 + 进入下一题的方式」
``guards.advance_confirm_completion`` 收尾确认：找不到下一题 / 认为做完时再问一次视觉组
``guards.advance_scroll_step_ratio`` 滚动推进的步长（占视口高的比例）
``guards.advance_scroll_max_steps`` 滚动推进的步数上限（防「滚到天荒地老」）
======================  ==================================================
"""

from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.enums import ProbeName, SolvePath, TargetKind, TierUsed
from core.targets import DEFAULT_DEBUG_PORT
from core.tasks import TaskType

__all__ = [
    "DEFAULT_RUN_CONFIG_PATH",
    "MIN_SAMPLE_N",
    "GuardThresholds",
    "ProbeName",
    "RateLimits",
    "RunConfig",
    "SolvePath",
    "TargetKind",
    "TierUsed",
    "active_probe_chain",
    "load_run_config",
    "save_run_config",
]

logger = logging.getLogger(__name__)

#: 采样数下限。
#:
#: **2026-09-28 起是 1**：用户要求「解题的核对次数由至少 5 次改为 1 次」——
#: 每题只调一次模型，最省也最快。
#: 2026-09-29 起它的含义进一步明确：不复算时恒按 1 处理（以第一次答案为准）；
#: 只有 ``RunConfig.recalculate=True`` 时 ``sample_n`` 才生效（作为总采样次数）。
MIN_SAMPLE_N = 1

#: 运行配置文件默认落位
DEFAULT_RUN_CONFIG_PATH = Path("state/run_config.json")

#: 环境变量名，用于覆盖配置文件路径
RUN_CONFIG_ENV = "AUTOLEARN_RUN_CONFIG"


class GuardThresholds(BaseModel):
    """质量闸门与处置阈值（T0-3 / T0-5 / T0-6）。"""

    model_config = ConfigDict(extra="forbid")

    # -- T0-3 复核门限 ------------------------------------------------------ #
    #: 复算之后多数票一致率**低于**此值 → 标 ``⚠复核`` 并必停等人。
    #:
    #: Tier 分级删除后，它不再触发「升级到 Tier2」，只用来判断
    #: 「复算出来的结果分歧是不是大到该让人看一眼」。
    agreement_accept: float = 0.8

    #: **不复算**（单次作答）时，模型自报的 ``confidence`` 低于此值 → 标 ``⚠复核`` 必停。
    #:
    #: 为什么需要它：单样本的一致率恒为 1，那个 1.0 里没有信息。模型自报的把握
    #: （见 ``prompts/20-解题组.md`` 的输出契约）才携带信息：题面完整、它也能给出
    #: 一个答案，但它自己说「只有三成把握」—— 这种**安静的错答案**在旧口径下
    #: 会一路被当成确定结论点出去。
    #:
    #: 只在模型**真的报了**这个字段时生效；没报（``None``）时保持旧语义
    #: （= 一致率，单样本即 1.0），**不新增暂停**。
    confidence_review_min: float = 0.5

    # -- T0-6 M2 止损数字（唯一强制闸门） ---------------------------------- #
    single_top1_min: float = 0.85
    multi_exact_min: float = 0.75
    valid_ratio_min: float = 0.80

    # -- T0-5 回读不一致处置 ------------------------------------------------ #
    click_replay_max: int = 3
    click_replay_gap_ms: tuple[int, int] = (200, 400)

    # -- 「推进下一题」的兜底（P12 增量） ----------------------------------- #
    #:
    #: 背景：真实站点的「下一题」有两种实现 —— **点按钮** 与 **滑动翻页**。
    #: 找不到按钮时是否允许走视觉 / 滑动兜底。
    #:
    #: .. danger::
    #:    默认 ``True`` 是给「无锚点站点」用的；有锚点的站点（靶场那类）里
    #:    ``selectors.next`` 就是权威契约，**没有按钮就是干净跑完**，
    #:    所以调用方要用 ``_visual_fallback_enabled()`` 把这道边界关掉。
    advance_visual_fallback: bool = True
    #: 滑动用哪种事件：``mouse`` = 鼠标按下-移动-抬起（兼容面最广，
    #: 绝大多数现代库认它）；``touch`` = CDP ``Input.dispatchTouchEvent``
    #: （只认 TouchEvent 的老式移动端页面才需要）。
    advance_swipe_mode: str = "mouse"
    #: 滑动方向的尝试顺序。第一下就成功就停 —— **不会连着滑**，避免一次跳过好几题。
    #: 空元组 = 禁用滑动（此时找不到按钮就按 ``advance_failed`` 停下）。
    advance_swipe_directions: tuple[str, ...] = ("left", "up")
    #: 滑动距离（占视口宽 / 高的比例）与手势时长（毫秒）
    advance_swipe_distance: float = 0.6
    advance_swipe_duration_ms: int = 240
    #: 判定「页面真的翻页了」的轮询预算。翻页有过渡动画，读早了会误判成没动。
    advance_change_timeout_ms: int = 1500
    advance_change_poll_ms: int = 150
    #: 推进动作**成功之后、下一次截图之前**的固定沉淀时间（毫秒，2026-10-01 加）。
    #:
    #: 为什么非有不可：真机日志 ``logs/9abc4ed8ef60`` 里出现
    #: ``advance_card clicked number=27`` 之后重新读图仍然读到 **26 题**
    #: （同一个 qid），于是被判成 ``wrong_question`` 停下等人。
    #: 页面切题是异步的：进度条与题号先变、题面随后才换，而读题紧接着就截屏，
    #: 截到的正是**换到一半的旧画面**。
    #:
    #: 与 ``advance_change_*`` 的分工：那一组靠**页面指纹**判「变了没有」，
    #: 而指纹取不到时（题干画在 canvas 上、``evaluate`` 被禁）会整条跳过 ——
    #: 这一条是**无条件**的兜底沉淀，保证「动作之后绝不零等待截屏」。
    advance_settle_ms: int = 350

    # -- 起始标定 + 收尾确认（2026-09-28 取代 P14 的末题判定） --------------- #
    #:
    #: 旧做法（P14）是「找不到按钮时靠一堆启发式证据猜这是不是最后一题」，
    #: 用户实测判定为**严重逻辑错误**，整套推倒。新做法简单得多：
    #:
    #: 1. **任务开始**时让视觉组模型看一眼，标定出「题目总数」与
    #:    「进入下一题的方式」（点击按钮 / 滑动翻页 / 向下滚动）；
    #: 2. 逐题解题时按标定出的方式推进；
    #: 3. **找不到下一题**、或**已完成数达到标定总数**时，
    #:    再由视觉组**确认一次**「是否全部完成」——确认了才干净收工。
    #:
    #: 这样「是否做完」由模型看画面回答，而不是靠正则与文案猜。
    #: 开局标定一次（每题不重复调用）
    advance_calibrate: bool = True
    #: 收尾前再问一次视觉组「是否全部完成」
    advance_confirm_completion: bool = True
    #: 每步滚动多少（占视口高的比例），以及最多滚几步。
    #: **必须有限**：否则「找不到按钮」会退化成「滚到天荒地老」。
    #: 由「推进方式 = 向下滚动」以及「点击按钮但要滚动才出现」两种情况共用。
    #:
    #: ⚠️ **2026-09-29 从 0.8 降到 0.5**：步长越大，「上一屏底部还没露全、
    #: 下一屏已经从更靠下的题开始」这种**夹缝题**越容易被整屏跨过去 ——
    #: 这正是「滚动过多、跳过题目」的成因。半步滚动配合「读一屏确认新题进来了」
    #: 的重叠校验，夹缝里的题一定会先被读到一次。
    advance_scroll_step_ratio: float = 0.5
    #: 滚动步数上限。半步滚动比整屏滚动需要的步数更多，所以上限同步放宽。
    advance_scroll_max_steps: int = 12
    #: 向前恢复：发现「滚过头」时最多往回滚几次去找漏掉的题。
    #: 只在**两端题号都是数字**且跨度 > 1 时才触发，且总量有界。
    advance_scroll_recover_max: int = 2

    @model_validator(mode="after")
    def _check(self) -> GuardThresholds:
        for name in (
            "agreement_accept",
            "confidence_review_min",
            "single_top1_min",
            "multi_exact_min",
            "valid_ratio_min",
        ):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} 必须是 0~1 的比例，得到 {value}")
        if self.click_replay_max < 0:
            raise ValueError("click_replay_max 不得为负")
        low, high = self.click_replay_gap_ms
        if low < 0 or low > high:
            raise ValueError(f"click_replay_gap_ms 区间非法：{self.click_replay_gap_ms}")
        if self.advance_swipe_mode not in {"mouse", "touch"}:
            raise ValueError(f"advance_swipe_mode 只能是 mouse / touch：{self.advance_swipe_mode}")
        known = {"left", "right", "up", "down"}
        unknown = set(self.advance_swipe_directions) - known
        if unknown:
            raise ValueError(f"advance_swipe_directions 含未知方向 {sorted(unknown)}")
        if not 0.05 <= self.advance_swipe_distance <= 0.9:
            raise ValueError("advance_swipe_distance 必须在 0.05~0.9 之间")
        if self.advance_swipe_duration_ms <= 0:
            raise ValueError("advance_swipe_duration_ms 必须为正")
        if self.advance_change_timeout_ms <= 0 or self.advance_change_poll_ms <= 0:
            raise ValueError("推进判定的轮询预算必须为正")
        if self.advance_settle_ms < 0:
            raise ValueError("advance_settle_ms 不得为负")
        if self.advance_scroll_max_steps < 0:
            raise ValueError("advance_scroll_max_steps 不得为负")
        if self.advance_scroll_recover_max < 0:
            raise ValueError("advance_scroll_recover_max 不得为负")
        if not 0.1 <= self.advance_scroll_step_ratio <= 1.0:
            raise ValueError("advance_scroll_step_ratio 必须在 0.1~1.0 之间")
        return self


class RateLimits(BaseModel):
    """三档限速（M4-2）。**一律带随机抖动**，区间取值。"""

    model_config = ConfigDict(extra="forbid")

    click_gap_ms: tuple[int, int] = (200, 600)
    submit_gap_s: tuple[int, int] = (5, 15)
    llm_concurrency_free: int = 2
    llm_concurrency_paid: int = 3

    @model_validator(mode="after")
    def _check(self) -> RateLimits:
        for name in ("click_gap_ms", "submit_gap_s"):
            low, high = getattr(self, name)
            if low < 0 or low > high:
                raise ValueError(f"{name} 区间非法：{(low, high)}")
        if self.llm_concurrency_free < 1 or self.llm_concurrency_paid < 1:
            raise ValueError("模型并发上限至少为 1")
        return self


class RunConfig(BaseSettings):
    """一次运行的完整配置。启动前设定，运行中只读（UI 侧返回 409）。"""

    model_config = SettingsConfigDict(
        extra="ignore",
        arbitrary_types_allowed=True,
        validate_default=True,
        env_prefix="AUTOLEARN_",
    )

    # -- 感知 / 执行 ------------------------------------------------------- #
    #:
    #: v0.2.0：``probe_order`` / ``probe_mode`` 已删除 —— 页面只经模型的眼睛读，
    #: 没有第二条通道可选，也就不需要「优先谁 / 只用谁」这两个开关。
    #: 旧配置里残留的这些键由 :func:`load_run_config` 静默忽略。
    auto_apply: bool = False

    # -- 复算（2026-09-29 加；取代原 Tier 分级） ----------------------------- #
    #:
    #: 用户在建任务时可以**显式选择**要不要复算、复算几次：
    #:
    #: * ``recalculate=False``（默认）→ **以第一次答案为准**，每题只调一次模型；
    #: * ``recalculate=True`` → 按 :attr:`sample_n` 次取样后按**内容**投票，
    #:   多数解为最终答案；一致率低于 ``guards.agreement_accept`` 时标 ``⚠复核`` 必停。
    #:
    #: ⚠️ 这里的语义与「Tier」时代最大的不同：**复算不是自动升级**，
    #: 而是用户在创建任务时的一次明确决定。所以「没开复算却多花了钱」
    #: 这种事不应该再发生。
    recalculate: bool = False
    #: 采样次数（**含第一次**）。只在 ``recalculate=True`` 时生效，此时至少为 2；
    #: 不复算时恒按 1 处理 —— 「不复算就以第一次答案为准」这句靠它落地。
    sample_n: int = MIN_SAMPLE_N

    # -- 任务序列 ----------------------------------------------------------- #
    task_sequence: list[TaskType] = Field(default_factory=lambda: [TaskType.QUIZ])

    # -- 模型（**四组**，2026-09-28 起） ------------------------------------ #
    #:
    #: 界面上是四个选择：**视觉组 / 视觉备用（可多选）** 与
    #: **解题组 / 解题备用（可多选）**。
    #: 字段名沿用 ``model_profile_id``（历史契约；改名字会让已存盘的
    #: ``state/run_config.json`` 失效），只是界面标签改成「解题组」。
    #: 解题组：作答用哪一套模型配置。
    model_profile_id: str | None = None
    #: 视觉组：读题 / 看图用哪一套。留空 = 与解题组共用。
    vision_profile_id: str | None = None
    #: 解题备用组：解题组整体失败时**按顺序**降级用（空 = 不降级）。
    backup_profile_ids: list[str] = Field(default_factory=list)
    #: 视觉备用组：视觉组整体失败时**按顺序**降级用（空 = 不降级）。
    vision_backup_profile_ids: list[str] = Field(default_factory=list)

    #: 训练模式：任务**成功收尾**后自动总结这次的经验并写进方式库 / 提示词经验区。
    #:
    #: 默认关。它是一个会**改磁盘上提示词文件**的动作，不该在用户没要求时发生；
    #: 想临时跑一次用 ``POST /api/training/{run_id}``。
    training: bool = False

    # -- 阈值（具名，禁止裸数字） ------------------------------------------- #
    guards: GuardThresholds = Field(default_factory=GuardThresholds)
    rate: RateLimits = Field(default_factory=RateLimits)

    # -- 目标与浏览器 ------------------------------------------------------- #
    storage_state_path: Path = Path("state/storage_state.json")
    target_kind: TargetKind = TargetKind.BROWSER_PAGE
    target_id: str | None = None
    browser_channel: str = "chrome"
    browser_debug_port: int = DEFAULT_DEBUG_PORT

    @model_validator(mode="after")
    def _check(self) -> RunConfig:
        if self.sample_n < MIN_SAMPLE_N:
            raise ValueError(f"sample_n 不得小于 {MIN_SAMPLE_N}")
        if self.recalculate and self.sample_n < 2:
            raise ValueError("开启复算时 sample_n 至少为 2（1 次等于不复算）")
        if not self.task_sequence:
            raise ValueError("task_sequence 不得为空")
        return self

    @property
    def is_model_ready(self) -> bool:
        """是否**真的选了模型**。

        它是「这一次运行是不是在用真模型」的唯一判据：``None`` 表示没选
        （退回 Mock / 全量降级链），选了就表示用户明确指定了一套配置。
        界面上「模型优先」能不能点、并发档位取哪一档，都读它。
        """
        return self.model_profile_id is not None

    @property
    def llm_concurrency(self) -> int:
        """请求级并发上限（M4-2）。

        档位判据就是 :attr:`is_model_ready`：**配了模型 = 用付费档，
        没配（Mock / 空跑）= 免费档**。P4 讨论过要不要为此加一个显式字段，
        结论是**不加** —— 再开一个字段等于给「同一件事」两个真相来源，
        两者不一致时就没人说得清该听谁的。见 ``README.md`` §P4。
        """
        if self.is_model_ready:
            return self.rate.llm_concurrency_paid
        return self.rate.llm_concurrency_free


def active_probe_chain(cfg: RunConfig) -> list[ProbeName]:
    """题目探针调用顺序。

    v0.2.0 起**恒为一条视觉通道**。函数保留而不是就地内联成字面量，
    是为了让「题目通道只有一条」这个事实只有一个定义点 ——
    将来若真恢复第二条通道，改动仍集中在这里。
    """
    del cfg  # 顺序不再取决于配置；入参保留是为了调用点不必改签名
    return [ProbeName.VISION]


def _config_path() -> Path:
    override = os.environ.get(RUN_CONFIG_ENV)
    if override:
        return Path(override)
    return DEFAULT_RUN_CONFIG_PATH


def _strip_extra_fields(data: Any, exc: ValidationError) -> tuple[Any, list[str]]:
    """按 ``extra_forbidden`` 报错逐条剔除未知字段。

    返回 ``(剔完的数据, 被剔除的字段路径)``。只认这两样的组合：
    ``type == "extra_forbidden"`` 且路径能在数据里走通 —— 其余报错（类型不对、
    取值越界）**原样留给调用方**，绝不在这里悄悄吞掉。

    为什么必须做这一步：``state/run_config.json`` 是**跨版本存盘**的，
    嵌套模型却一律 ``extra="forbid"``。删掉一个 guard 字段之后，老文件里
    残留的那几个键就会让校验整体失败 —— 而 ``load_run_config()`` 的异常
    会一路冒到路由，变成每次打开控制台都是 **HTTP 500**。
    实测事故（2026-09-28）：P14 时代的 7 个 ``guards.end_*`` / ``advance_scroll_*``
    键留在盘上，``/api/run/config`` 与 ``/api/targets`` 全挂，界面直接打不开。
    """
    pruned = copy.deepcopy(data)
    dropped: list[str] = []
    for error in exc.errors():
        if error.get("type") != "extra_forbidden":
            continue
        loc = error.get("loc") or ()
        if not loc:
            continue
        parent = pruned
        for part in loc[:-1]:
            if isinstance(parent, dict):
                parent = parent.get(part)
            elif isinstance(parent, list) and isinstance(part, int) and part < len(parent):
                parent = parent[part]
            else:
                parent = None
                break
        if isinstance(parent, dict) and loc[-1] in parent:
            parent.pop(loc[-1])
            dropped.append(".".join(str(part) for part in loc))
    return pruned, dropped


#: 剔除未知字段的最大往返轮数。嵌套模型最深 2 层，3 轮已是余量。
_LENIENT_ROUNDS = 3


def _validate_tolerating_stale_fields(data: Any) -> tuple[RunConfig, list[str]]:
    """校验运行配置，**容忍**旧版本残留的未知字段。

    剔除未知字段后重新校验，最多往返 ``_LENIENT_ROUNDS`` 轮（嵌套层级有限，
    一轮剔一层就够；留几轮余量是防止同一层有多个兄弟字段时漏剔）。
    仍有非 ``extra_forbidden`` 的报错时**原样抛出** —— 那是真错误，不是陈旧。
    """
    attempt = data
    dropped: list[str] = []
    last: ValidationError | None = None
    for _ in range(_LENIENT_ROUNDS):
        try:
            return RunConfig.model_validate(attempt), dropped
        except ValidationError as exc:
            last = exc
            attempt, removed = _strip_extra_fields(attempt, exc)
            if not removed:
                raise
            dropped.extend(removed)
    assert last is not None  # pragma: no cover - 循环至少跑一轮才会到这里
    raise last


def load_run_config() -> RunConfig:
    """读取运行配置。

    **本函数永不因磁盘上的配置内容抛异常** —— 它被路由直接调用，抛出去就是
    整个控制台 HTTP 500。三种降级，任何一种都只记一条警告：

    1. 文件不存在           → 全默认值（首次运行，正常路径）；
    2. 含旧版本残留字段     → 丢掉那些字段，**其余照用**（用户的其他设置保住），
       并把清理结果回写，让盘上的文件自愈；
    3. 坏 JSON / 取值非法   → 全默认值。宁可退回默认，也不能让界面打不开。
    """
    path = _config_path()
    if not path.exists():
        return RunConfig()

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("运行配置 %s 读不出来（%s），改用默认值", path, exc)
        return RunConfig()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("运行配置 %s 不是合法 JSON（%s），改用默认值", path, exc)
        return RunConfig()

    if not isinstance(data, dict):
        logger.warning("运行配置 %s 顶层应是对象，得到 %s，改用默认值", path, type(data).__name__)
        return RunConfig()

    try:
        cfg, dropped = _validate_tolerating_stale_fields(data)
    except ValidationError as exc:
        logger.warning("运行配置 %s 校验失败（%s），改用默认值", path, exc)
        return RunConfig()

    if dropped:
        logger.warning(
            "运行配置 %s 含已废弃字段，已忽略：%s", path, ", ".join(sorted(dropped))
        )
        # 自愈：把剔干净的结果写回盘上，下次启动不再报同一条警告。
        try:
            save_run_config(cfg)
        except OSError as exc:  # pragma: no cover - 只读介质 / 权限不足
            logger.warning("运行配置 %s 清理结果回写失败：%s", path, exc)
    return cfg


def save_run_config(cfg: RunConfig) -> None:
    """落盘运行配置。只写非敏感字段——密钥一律走 ``CredentialStore``。"""
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(cfg.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
