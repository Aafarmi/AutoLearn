"""前后端出入参模型（P5）。

红线：**出参一律不含密钥明文**。``ModelProfileOut`` 只回 ``has_api_key``，
不回 ``api_key``、不回 ``api_key_ref`` 指向的值。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from core.config import GuardThresholds, RateLimits
from core.enums import ActLevel, QType, TargetKind, TaskType
from core.models import CapabilityReport, SampleRecord
from target.base import TargetInfo

__all__ = [
    "BulkDeleteIn",
    "BulkDeleteOut",
    "ConfirmIn",
    "GuideCardOut",
    "ItemDetailOut",
    "ModelProfileIn",
    "ModelProfileOut",
    "PresetOut",
    "RunConfigIn",
    "RunConfigOut",
    "ShutdownOut",
    "SkippedTaskOut",
    "SystemPortOut",
    "SystemStatusOut",
    "TargetLaunchOut",
    "TargetListOut",
    "TaskCreateIn",
    "TaskDetailOut",
    "TaskItemOut",
    "TaskListOut",
    "TaskOut",
    "TaskRenameIn",
]


class BulkDeleteIn(BaseModel):
    """``POST /api/tasks/bulk-delete`` 请求体。"""

    model_config = ConfigDict(extra="forbid")

    #: 要删掉的任务 id。空列表是**合法输入**（什么也不做，如实回一个空结果）。
    run_ids: list[str]


class SkippedTaskOut(BaseModel):
    """批量操作里**没做成**的那一条，以及为什么。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    #: 可读原因（「任务正在运行」/「任务不存在」）—— 直接展示，不必再映射。
    reason: str


class BulkDeleteOut(BaseModel):
    """批量删除的结果。

    **逐条如实回报**（``deleted`` + ``skipped``），不做成「全成功 / 全失败」：
    用户勾了 8 个任务、其中 1 个正在跑 —— 那 7 个该删掉，第 8 个该被拒绝并说明原因。
    一次失败就整批放弃，等于逼用户先自己找出哪个在跑，再重来一遍。
    """

    model_config = ConfigDict(extra="forbid")

    deleted: list[str]
    skipped: list[SkippedTaskOut]


class RunConfigIn(BaseModel):
    """``PUT /api/run/config`` 请求体。运行中提交应返回 409。"""

    model_config = ConfigDict(extra="forbid")

    #: 半自动（false，默认）/ 全自动跑批（true）。见 ``core/config.py:RunConfig``
    auto_apply: bool | None = None
    #: **要不要复算**（默认 false = 以第一次答案为准）。
    recalculate: bool | None = None
    #: 复算时的采样次数（含第一次）。只在 ``recalculate=True`` 时生效，至少 2。
    sample_n: int | None = None
    #: 训练模式：成功收尾后是否自动总结经验并写进方式库 / 提示词经验区。
    training: bool | None = None
    task_sequence: list[TaskType] | None = None
    #: **解题组**（作答、投票）。界面上叫「解题组」。
    model_profile_id: str | None = None
    #: **视觉组**（读题 / 看图）；留空 = 与解题组共用一套
    vision_profile_id: str | None = None
    #: **解题备用组**：解题组整体失败时按顺序降级用（可多选，空 = 不降级）
    backup_profile_ids: list[str] | None = None
    #: **视觉备用组**：视觉组整体失败时按顺序降级用（可多选，空 = 不降级）
    vision_backup_profile_ids: list[str] | None = None
    guards: GuardThresholds | None = None
    rate: RateLimits | None = None
    #: 目标选择（P11）。``target_id`` 由 ``GET /api/targets`` 给出
    target_kind: TargetKind | None = None
    target_id: str | None = None


class RunConfigOut(BaseModel):
    """``GET /api/run/config`` 响应体。

    v0.2.0 去掉了 ``probe_order`` / ``probe_mode`` / ``available_modes``：
    页面只经模型的眼睛读，「读题走哪条通道」这个选择本身没有了，
    前端也就没有可渲染的选项 —— 留一组恒为「仅视觉」的字段只会误导。
    """

    model_config = ConfigDict(extra="forbid")

    auto_apply: bool
    #: 要不要复算（false = 每题只调一次模型，以第一次答案为准）
    recalculate: bool = False
    #: 复算时的采样次数（含第一次）；不复算时它不生效
    sample_n: int
    #: 训练模式：成功收尾后是否自动总结经验
    training: bool = False
    task_sequence: list[TaskType]
    #: **解题组**（用户选的那一套；留空 = Mock / 全量降级链）
    model_profile_id: str | None
    #: **视觉组**（留空 = 与解题组共用）
    vision_profile_id: str | None = None
    #: **解题备用组**（按顺序降级）
    backup_profile_ids: list[str] = Field(default_factory=list)
    #: **视觉备用组**（按顺序降级）
    vision_backup_profile_ids: list[str] = Field(default_factory=list)
    guards: GuardThresholds
    rate: RateLimits
    #: 有没有可用的模型配置。v0.2.0 起**没有模型就完全跑不了**（页面只能靠模型读），
    #: 所以 ``false`` 时前端要给出可执行的引导（向导第 5 步的提示条、启动被拦后的
    #: 引导卡），而不是让用户点下去收一个没有解释的 400。
    model_ready: bool
    #: 降级链里是否存在**实测支持视觉**的配置。它只作预告
    #:（「这套模型可能读不了图」），不再用于开关某个通道选择。
    vision_ready: bool = False
    #: 运行中配置只读
    locked: bool = False
    #: 目标（P11）
    target_kind: TargetKind = TargetKind.BROWSER_PAGE
    target_id: str | None = None


class TargetListOut(BaseModel):
    """``GET /api/targets`` 响应体：当前可抓的目标列表。

    ``alive=false`` 不是错误，而是「浏览器没带调试端口启动」这个**最常见的
    初始状态**。此时 ``hint`` 给出可操作的下一步，前端必须展示它，
    不能只显示一个空列表让用户猜。
    """

    model_config = ConfigDict(extra="forbid")

    kind: TargetKind
    #: CDP 端点地址（``http://127.0.0.1:9222``）。**桌面窗口目标为 ``None``** ——
    #: 原生程序没有「端点」这个概念，硬填一个假地址只会误导排障。
    endpoint: str | None = None
    #: 该目标类型下有没有可用的连接（浏览器 = 调试端口活着；桌面 = 能枚举窗口）
    alive: bool = False
    targets: list[TargetInfo] = Field(default_factory=list)
    #: 需要用户做什么才能拿到目标。``None`` = 无需操作
    hint: str | None = None


class TargetLaunchOut(TargetListOut):
    """``POST /api/targets/launch`` 响应体：接管启动后的同一份列表。"""

    #: 是否真的由本次调用拉起了浏览器进程
    launched: bool = False


class ModelProfileIn(BaseModel):
    """新增 / 编辑模型配置。``api_key`` 留空表示不改动既有密钥。

    **一套配置一个模型**（2026-09-28 起）：不再分 tier1 / tier2。
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    base_url: str
    #: 模型名（唯一）。Tier 语义仍在，但复算用的是同一个模型。
    model: str
    temperature: float = 0.2
    timeout_s: int = 60
    concurrency: int = 2
    api_key: str | None = None
    enabled: bool = True


class ModelProfileOut(BaseModel):
    """模型配置出参。**不含密钥明文、不含 api_key_ref**。"""

    model_config = ConfigDict(extra="forbid")

    profile_id: str
    name: str
    base_url: str
    model: str
    temperature: float
    timeout_s: int
    concurrency: int
    enabled: bool
    order: int
    has_api_key: bool
    capabilities: CapabilityReport | None = None


class PresetOut(BaseModel):
    """表单预设模板（``GET /api/providers/presets``）。"""

    model_config = ConfigDict(extra="forbid")

    preset_id: str
    label: str
    base_url: str
    model: str
    docs_url: str | None = None
    free_quota_note: str | None = None


class TaskItemOut(BaseModel):
    """任务列表项。题目与分集混排。"""

    model_config = ConfigDict(extra="forbid")

    item_id: str
    type: TaskType
    qid: str | None = None
    vid: str | None = None
    state: str
    attempts: int
    suspended: bool
    title: str | None = None
    #: 分集专用：当前集 / 总集数
    episode_index: int | None = None
    episode_total: int | None = None
    created_at: datetime
    updated_at: datetime


class ItemDetailOut(BaseModel):
    """**单题/单集详情**（原 ``TaskDetailOut``）。

    含 before/after 截图 URL、采样明细、``level_used``。
    改名是为了把「任务」这个词让给 run 级（用户界面上的一条任务），
    见 :class:`TaskOut` / :class:`TaskDetailOut`。
    """

    model_config = ConfigDict(extra="forbid")

    item: TaskItemOut
    stem: str | None = None
    qtype: QType | None = None
    options: list[str] = Field(default_factory=list)
    chosen_labels: list[str] = Field(default_factory=list)
    confidence: float | None = None
    #: 这次作答走的路径（``single`` / ``mock`` / ``cache``）。**只有一种求解模式** ——
    #: 原先的 ``tier1`` / ``tier2`` 分级已删除。
    solve_path: str | None = None
    review_flag: bool = False
    samples: list[SampleRecord] = Field(default_factory=list)
    level_used: ActLevel | None = None
    readback_mismatch: bool = False
    before_screenshot_url: str | None = None
    after_screenshot_url: str | None = None
    #: ``submitted`` 态必须为 ``true``，前端据此置灰确认按钮
    is_danger_state: bool = False


class TaskOut(BaseModel):
    """**任务**（= 一次运行 ``run``）。主页面上一条就是它。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    name: str
    #: 展示态 key：``created`` / ``running`` / ``paused`` / ``finished`` / ``stopped`` / ``error``
    #: —— 前端按它选样式，不要去解析 ``status_raw``
    status: str
    #: 展示态的人话（「进行中」「已完成」「已中断（进程已退出）」…）
    status_label: str
    #: 库里存的原始 status（``running`` / ``paused:needs_confirm`` / …），排障用
    status_raw: str
    #: 是不是**当前正在跑**的那个任务
    active: bool
    created_at: datetime
    finished_at: datetime | None = None
    #: 进度
    total: int = 0
    done: int = 0
    failed: int = 0
    pending_confirm: int = 0
    current_item_id: str | None = None
    #: 目标与选项摘要（主页面要一眼看清「这个任务在干什么」）
    target_kind: TargetKind = TargetKind.BROWSER_PAGE
    target_id: str | None = None
    target_title: str | None = None
    task_sequence: list[TaskType] = Field(default_factory=list)
    auto_apply: bool = False
    judge_model: str | None = None
    vision_model: str | None = None


class TaskListOut(BaseModel):
    """``GET /api/tasks``：任务列表 + 当前是否有活跃运行。"""

    model_config = ConfigDict(extra="forbid")

    tasks: list[TaskOut] = Field(default_factory=list)
    active_run_id: str | None = None
    running: bool = False


class TaskDetailOut(BaseModel):
    """``GET /api/tasks/{run_id}``：任务详情（过程 + 条目）。"""

    model_config = ConfigDict(extra="forbid")

    task: TaskOut
    items: list[TaskItemOut] = Field(default_factory=list)
    #: 最近一次视觉技能映射诊断摘要，任务详情用于排查被跳过的题。
    skill_diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    #: 各状态条目数，前端画进度条用
    by_state: dict[str, int] = Field(default_factory=dict)
    stack_depth: int = 0


class TaskCreateIn(BaseModel):
    """``POST /api/tasks``：新建任务。

    ``name`` 留空 → 自动取名「任务N」（N 由已有任务名推算）。
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    #: 建任务时一并提交的运行配置（可选）。**会存进任务自己的快照**
    config: RunConfigIn | None = None


class TaskRenameIn(BaseModel):
    """``PATCH /api/tasks/{run_id}``：改任务名。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60)


class ConfirmIn(BaseModel):
    """``POST /api/tasks/{item_id}/confirm`` 请求体。"""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["confirm", "reject"]
    note: str | None = None


class GuideCardOut(BaseModel):
    """引导卡片。四类情形各自独立文案 + 明确的下一步动作。"""

    model_config = ConfigDict(extra="forbid")

    title: str
    body_md: str
    next_action: str
    doc_url: str | None = None


class SystemPortOut(BaseModel):
    """一个「本程序相关端口」的当前占用情况。"""

    model_config = ConfigDict(extra="forbid")

    port: int
    pid: int
    #: ``self`` 本进程 / ``sibling`` 同一解释器起的兄弟进程（会被关闭）/
    #: ``foreign`` 判不出归属或不是本项目 —— **不会被碰**
    owner: Literal["self", "sibling", "foreign"]
    exe: str | None = None


class SystemStatusOut(BaseModel):
    """``GET /api/system/status``：关机**会做什么**的预览。只读，不改任何状态。

    界面直接渲染 ``preview`` 与 ``ports``、不自己拼文案 —— 文案与判定**同源**，
    才不会出现「界面说会关、实际没关」这种最伤信任的不一致。
    """

    model_config = ConfigDict(extra="forbid")

    ports: list[SystemPortOut]
    #: 本程序「接管启动」的浏览器进程数（关机会关闭它们）
    browsers: int
    #: 是否有任务正在运行（关机会中断它）
    running: bool
    #: 逐条说明「关闭会做什么」，含「哪些不归我们管」
    preview: list[str]


class ShutdownOut(BaseModel):
    """``POST /api/system/shutdown`` 的回执。

    注意它**先发出去、再动手**：拿到这份回执只说明关机已被排上，
    不代表此刻已经关完 —— 前端应据此显示「正在关闭」，再用 ``/api/health``
    探活确认（响应本身可能因为进程退出而被截断）。
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    #: 已被安排关闭的东西
    stopped: list[str]
    #: 明确**不动**的占用者（别人的服务）
    skipped: list[str]
    preview: list[str]
