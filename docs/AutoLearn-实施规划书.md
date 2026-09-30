# AutoLearn 实施规划书（Rapid Build Plan）

**版本**：v1.0（**2026-09-28 v0.2.0「只使用模型」修订**）
**编制日期**：2026-09-25
**依据文档**：《AutoLearn 项目任务书》v1.0
**文档性质**：任务书 → 施工图。把 WBS 拆成**可并行认领的工作包（Part）**，为每个 Part 钉死**功能边界、接口签名、文件命名、验收口径**。
**读者**：全体开发 + 测试。开工前请先读 §2（契约）与 §3（命名），再查自己的 Part。

> **⚠ 2026-09-28 v0.2.0 修订提要（本文档凡与本节冲突处，以本节为准）**
>
> 产品版本 **0.1.0 → 0.2.0**，架构由「**视觉模型 + DOM 双通道**」改为「**只使用模型（视觉）**」。
>
> | 变化 | 内容 |
> |---|---|
> | **删** | DOM 题目通道（`perception/dom_probe.py`）、网络 XHR 通道（`perception/net_probe.py`）、题目侧回读（`act/readback.py`）、题目锚点 `adapters/mock_exam/selectors.yaml` |
> | **删** | `ProbeOrder` / `ProbeMode` / `PROBE_CHAINS` / `CAPABILITIES` / `modes_for` / `coerce_mode` / `default_probe_mode`、`ErrorCode.TARGET_CHANNEL_UNSUPPORTED`、`core.models.NetworkRecord`、`perception.base.QuestionRootNotFoundError` |
> | **删** | 题目侧六级执行阶梯（`LEVELS`）、`select_option(locator…)` / `apply_answer` / `submit(locator)` / `click(locator)` / `recheck_stem_hash` / `StemMismatchError` |
> | **改** | 读题一律「视口截图 + 模型」：`vision_probe.crop_question(page)`（签名不再收 adapter/ctx）、`solve/reader.py::read_question(png, providers=…)`、`act/actuator.py::select_option(box, size, qtype)` + `norm_box_center(box, size)` |
> | **改** | 题目侧校验只剩**截图差分**：`act/verifier.py::verify_region_changed(box, size, before)`，`VerifyKind.SCREENSHOT_DIFF` |
> | **改** | `load_run_config()` 容忍旧字段、**永不抛异常**，并自愈回写（修掉实测的 HTTP 500） |
> | **保留** | 网课 / 媒体子系统：媒体锚点 `selectors_media.yaml`、读 `<video>` 三态与分集索引、媒体动作阶梯 `MEDIA_LEVELS` / `SEEK_LEVELS` / `PAUSE_LEVELS`、`ActLevel` 六个值、`VerifyKind` 的媒体成员。**这是唯一仍读页面文档结构的地方**，读的是媒体状态而不是题目 |
>
> 破坏性变更的完整清单原先登记在 `docs/CHANGELOG-interface.md` 的 v0.2.0 一节 ——
> **该文件已于 2026-09-29 永久删除**（连同 `channel-bench.md` / `degrade-heatmap.md`），
> 本文档与源码里对它的引用均属**历史记录**，不再是可用路径。
> 现行口径以 `README.md`（维护指南）与 `tests/` 为准。
>
> **⚠ 2026-09-29 增量（v0.3.0 方向）**：
>
> | 变化 | 内容 |
> |---|---|
> | **删** | Tier 分级：`TierUsed.TIER1/TIER2`、`solve.escalated` 事件、`GuardThresholds.tier2_min_votes`、`should_escalate_to_tier2`；`Answer.tier_used` → `Answer.solve_path` |
> | **改** | 求解只剩一种模式；「要不要复算 / 复算几次」改为任务级显式开关（`RunConfig.recalculate` / `sample_n`），**不复算就以第一次答案为准** |
> | **加** | 一屏多题（`ReadBatch`）、固定的推进方式库（`core/advance_library.py` + `prompts/33-推进方式库.md`）、训练模式（`solve/training.py` + `POST /api/training/{run_id}`） |
> | **加** | 任务级缓存：`core.trace.run_dir()` / `purge_run_cache()`；`GET /api/tasks/{run_id}/cache` 预览，删任务级联清缓存 |
> | **改** | 滚动推进：步长降到半步（0.5）、上限放宽到 12、滚过头时**有界回滚补看** |
>
> **本文档保留历史 Part 结构（P0…P9）**：P2 从「DOM + 媒体感知」改成「视觉 + 媒体感知」，
> P3 从「网络通道 + 视觉兜底 + 仲裁」改成「视觉读题 + 单通道流水线」并划归 P2 的延续。
>
> **⚠ 2026-09-30 收口（v0.3.0 方向，细节见 §10）**：
>
> | 变化 | 内容 |
> |---|---|
> | **加** | `core/run_plan.py` —— 开局裁决**一次**：`derive_plan(batch, *, batch_size=0) -> RunPlan` 把推进方式（`card` / `click` / `scroll` / `swipe` 择一，优先级 `advance_library.PLAN_PREFERENCE`）/ 推进几何 / 提交范围 / 总题数一次定死 |
> | **改** | 视觉组只出**观测**：一次回 `{"page": {...}, "questions": [...], "more_below", "note"}`（`core.models.PageView`），`page` 里只报告「有什么」 |
> | **改** | 解题组只吃文本：请求里 `images` 恒为空、选项标号 = **页面标号**（**不再打乱选项**）、输出多一个自报 `confidence` |
> | **改** | 回读判定从布尔改**三态**（`act/verifier.py::region_change_state`：`changed` / `weak` / `none`）；落在墨迹上判 `none` **绝不重点** |
> | **删** | 运行期换招阶梯（点击 → 滚动 → 滑动）、每步重问「下一题在哪」、「滚到顶重读一次」、三份旧的标定 / 收尾确认 / 推进控件提示词（现存 5 份，见 §3.1） |

---

## 0. 怎么用这份文档

| 你是谁 | 你该看哪里 |
|--------|-----------|
| 项目负责人 | §1 分解总览、§6 泳道排期、§7 风险 |
| 开发（任何角色） | §2 接口契约、§3 命名规范、你负责的 Part 章节 |
| 测试 / QA | 每个 Part 的「验收口径」+ §8 交付物清单 |
| 新加入的人 | §0 → §1 → §3 → 你负责的 Part |

**三条硬规则**：

1. **契约先于实现**。§2 的接口清单冻结后，任何人可先提交 `raise NotImplementedError` 的 stub + 类型标注，让下游并行开工。改签名 = 改契约 = 必须同步通知全员。
2. **文件名即契约**。§3 定的路径不得随手更改；确需新增用 §3.6 的命名规则自行落位，并回写本文档。
3. **每位交付人必须同时交付「代码 + 单测 + 对该 Part 验收项的勾选结果」**，缺一不算完成。

---

## 1. 分解总览

### 1.1 从里程碑到工作包（Part）

任务书的 8 个里程碑按「能否并行」重切为 **10 个 Part**。Part 是最小认领单位（1~3 人日），里程碑是验收单位。

| Part | 名称 | 对应里程碑 | 依赖 | 人日 | 可并行对象 |
|------|------|-----------|------|------|-----------|
| **P0** | 契约与骨架冻结 | T0 | 无（阻塞全部） | 0.5 | 无（全员参与，先做） |
| **P1** | 双靶场（题目 + 网课） | M0-1/2 | 无（可与 P0 并行） | 2 | P0、P5 前端骨架 |
| **P2** | 视觉 + 媒体感知（v0.2.0 前为「DOM + 媒体感知」） | M0-3/4/5/6/7 | P0、P1 | 3 | P3、P5 |
| **P3** | 视觉读题 + 单通道流水线（v0.2.0 前为「网络通道 + 视觉兜底 + 仲裁」） | M1 | P2 | 3.5 | P4 骨架、P5 |
| **P4** | 求解层 + 模型 Provider | M2-1~4 | P0、P3(接口级) | 3.5 | P5、P6 骨架 |
| **P5** | UI（配置 / 任务 / 详情 / 日志流） | M2-5~8 | P0（可 mock 数据先行） | 4 | 全部（切面独立） |
| **P6** | 执行层 + dry_run | M3 | P2、P4 | 4.5 | P5 详情页 |
| **P7** | 编排循环 + 限速 + 留痕 | M4 | P6 | 3.5 | P8 预备（任务栈结构） |
| **P8** | 网课场景（中断-恢复） | M5 | P7 | 4.5 | — |
| **P9** | 打包（可选，不污染主线） | M6 | P8 | 2.5 | — |

合计 **31.5 人日**（含 P0）。**关键路径**：`P0 → P1/P2 → P3 → P4（M2 闸门）→ P6 → P7 → P8`。

### 1.2 并行泳道（4 人配置）

```
                    W1      W2       W3      W4      W5      W6
P0 全员         ██
P1 靶场         ██████
P2 感知                ██████
P3 视觉读题                    ██████
P4 求解                                ██████
P5 UI(前端)     ████████████████████  ← 最长的独立车道，从 W1 就开工
P6 执行                                        ████████
P7 编排                                                ██████
P8 网课                                                      ██████
```

| 泳道 | 角色 | 认领 Part | 人日 |
|------|------|----------|------|
| **A 感知** | 浏览器自动化工程师 | P2、P3 | 6.5 |
| **B 求解** | 后端 / AI 工程 | P0(契约)、P4 | 4.0 |
| **C 执行** | 后端 / 自动化 | P0、P6、P7、P8 | 13.0 |
| **D 前端+靶场** | 前端 / 全栈 | P1、P5、P9 | 8.5 |
| **全员** | — | P0 各认领 2 项定义 | 0.5 |

> 3 人配置时：P5 与 P1 合并给 D，P9 顺延；6 人配置时：把 P3 与 P6 各自拆给独立人，P5 拆成「后端 API」+「前端页面」两人。
> **P5 必须最早开工**：它是唯一全程独立车道，且 M2 闸门卡在 UI 可用性上（无配置引导、模型组选择都在 UI）。
> （v0.2.0：这里的「通道优先级切换」已删除。）

### 1.3 每个 Part 的一句话职责

| Part | 一句话 |
|------|--------|
| P0 | 把 8 项定义**变成代码常量和枚举**，让后面所有人不用猜口径 |
| P1 | 造出两个「可控的假网站」，让自动化有靶子可打，且每题知道正确答案 |
| P2 | 让系统**看见页面**：截视口图交给模型读题干、选项与坐标，以及视频的播放三态（媒体侧仍读 `<video>`） |
| P3 | 把「看见」变成一条可靠的流水线：单通道调度、截图重试、读题质量门禁与开局裁决/收尾确认 |
| P4 | 让系统**会做题**：调用模型、多次采样投票、按置信度升级、缓存结果 |
| P5 | 让人能**看见并干预**：配置模型、选用哪套视觉/解题模型、看进度、确认/否决、看日志 |
| P6 | 让系统**真的动手**：点得动、读得回、校验得了，还能干跑不提交 |
| P7 | 让系统**自己跑完 50 题**：循环、限速、断点续跑、全程留痕 |
| P8 | 让系统**自己看完一门课**：播完一集推下一集，被弹题打断能压栈恢复 |
| P9 | 只做分发时的事：复用系统浏览器，不打自带 Chromium |

---

## 2. 接口契约（开工前必须冻结）

> 本节是**并行开发的唯一依据**。P0 阶段的任务就是把这里全部落成代码骨架。
> 标注 `[P0]` 的必须在 P0 当天完成 stub；其余由 Part 负责人在自己 Part 第一天完成 stub 并推主干。

### 2.1 全局枚举与常量

```python
# core/config.py
# v0.2.0：ProbeOrder / ProbeMode 已删除 —— 题目通道只有一条，没有「优先谁 / 只用谁」。
#         旧配置文件里残留的这两个键由 load_run_config() 静默剔除并自愈回写。
class ProbeName(StrEnum):   VISION = "vision";  MEDIA = "media"   # MEDIA 不在题目链里
class SolvePath(StrEnum):   SINGLE = "single";  MOCK = "mock";  CACHE = "cache"
# 2026-09-29：TierUsed 已降级为 SolvePath 的**向后兼容别名**（老库 / 老留痕读得回来），
#            它不再有 tier1 / tier2 取值。
# v0.2.0：active_probe_chain(cfg) 恒返回 [ProbeName.VISION]

# core/states.py
class QuestionState(StrEnum):
    PENDING; PERCEIVED; SOLVED; PENDING_CONFIRM; APPLIED
    SUBMITTED; VERIFIED; FAILED; SKIPPED            # T0-2 九态
class MediaState(StrEnum):
    IDLE; PLAYING; PAUSED; INTERRUPTED; RESUMED; ENDED   # M5-4

# core/tasks.py
class TaskType(StrEnum):    VIDEO = "video";  QUIZ = "quiz"

# core/enums.py — 推进与提交（2026-09-30 收口）
class AdvanceMethod(StrEnum):   CLICK = "click";  CARD = "card";  SWIPE = "swipe"
                                SCROLL = "scroll";  UNKNOWN = "unknown"
# ↑ 五个取值是**冻结契约**（tests/test_t0_definitions.py 卡住）。UNKNOWN 只是裁决前的占位；
#   derive_plan() 一定会给出一个**可执行**的方式，运行期不会拿 UNKNOWN 去推页面。
class SubmitScope(StrEnum):     QUESTION = "question";  PAPER = "paper"
class CompletionState(StrEnum): ALL_DONE = "all_done";  NOT_DONE = "not_done";  UNKNOWN = "unknown"

# core/enums.py — v0.2.0 的校验类型
class VerifyKind(StrEnum):
    SCREENSHOT_DIFF = "screenshot_diff"   # 题目侧唯一判据：点前后选项区域像素
    READBACK; MEDIA_PAUSED; MEDIA_PROGRESS; MEDIA_RESUME; EPISODE_INDEX; SUBMIT_RESULT
```

> **v0.2.0 读题契约（本节新增，冻结）**：`core/models.py::ReadResult` 携带
> `clipped` / `uncertain` / `more_below` / `note` / `unsupported_qtype`，
> 由 `solve/reader.py::gate_read_result(result) -> (放行?, 原因码)` 裁决：**非空就拦**
> （`clipped` → `vision_incomplete`，`uncertain` → `vision_uncertain`）并带原因暂停问人。
> 它是 0.2.0 唯一的质量出口 —— 读题是全链路唯一「错了也不报错」的环节。

### 2.2 Python 接口总表

#### core/config.py `[P0]`

```python
class GuardThresholds(BaseModel):                    # T0-3 / T0-5 / T0-6
    agreement_accept: float = 0.8                    # 复算后多数票一致率 < 此值 → ⚠复核
    confidence_review_min: float = 0.5               # 2026-09-30：**不复算**时自报 confidence < 此值 → ⚠复核
    single_top1_min: float = 0.85                    # M2 闸门
    multi_exact_min: float = 0.75                    # M2 闸门
    valid_ratio_min: float = 0.80                    # 闸门：一致率≥0.8 的题占比
    click_replay_max: int = 3                        # 几何兜底落点的候选点上限（≤3 次）
    click_replay_gap_ms: tuple[int, int] = (200, 400)
# 2026-09-29：tier2_min_votes 随 Tier 分级删除（复算改由 RunConfig.recalculate 显式决定）

class RateLimits(BaseModel):                         # M4-2
    click_gap_ms: tuple[int, int] = (200, 600)
    submit_gap_s: tuple[int, int] = (5, 15)
    llm_concurrency_free: int = 2
    llm_concurrency_paid: int = 3

class RunConfig(BaseSettings):
    # v0.2.0：probe_order / probe_mode 已删除 —— 题目通道只有一条，没有可选项。
    #         盘上残留的这两个键由 load_run_config() 静默剔除并自愈回写。
    dry_run: bool           = False
    sample_n: int           = 1                      # 每题只核对一次（原来 n ≥ 5）
    task_sequence: list[TaskType] = [TaskType.QUIZ]
    model_profile_id: str | None = None              # 解题组
    vision_profile_id: str | None = None             # 视觉组
    backup_profile_ids: list[str] = []               # 解题备用（多选，按顺序降级）
    vision_backup_profile_ids: list[str] = []        # 视觉备用（多选，按顺序降级）
    guards: GuardThresholds = GuardThresholds()
    rate: RateLimits        = RateLimits()
    storage_path / target_kind / target_id / browser_channel / browser_debug_port
    @property
    def is_model_ready(self) -> bool: ...            # 是否真的选了模型（决定并发档位）
    @property
    def llm_concurrency(self) -> int: ...

def load_run_config() -> RunConfig                   # **永不抛异常**；容忍并剔除旧字段，自愈回写
def save_run_config(cfg: RunConfig) -> None
def active_probe_chain(cfg: RunConfig) -> list[ProbeName]   # v0.2.0 恒返回 [ProbeName.VISION]
```

#### core/models.py `[P0]`

```python
class Option(BaseModel):        index: int; label: str; text: str; raw: str
class Question(BaseModel):      qid: str; stem: str; stem_hash: str; qtype: QType
                                options: list[Option]; source: ProbeName; channel_trace: list[str]
class Answer(BaseModel):        qid: str; chosen_labels: list[str]; chosen_texts: list[str]
                                confidence: float; solve_path: SolvePath; review_flag: bool
                                model_name: str | None; samples: int
class ActionResult(BaseModel):  kind: ActionKind; target: str; level_used: ActLevel
                                ok: bool; readback: str | None; elapsed_ms: int; error: str | None
class VideoState(BaseModel):    paused: bool; ended: bool; current_time: float; duration: float
                                episode_index: int; episode_total: int; src: str | None
class VideoTask(BaseModel):     vid: str; course_id: str; episode_index: int; title: str
                                resume_at: float = 0.0; state: MediaState = MediaState.IDLE
class PerceptionResult(BaseModel):
                                question: Question | None; video_state: VideoState | None
                                channel_used: ProbeName; warnings: list[str]; screenshot_ref: str | None
# ---- 读题观测 → 开局方案（2026-09-30）-------------------------------------- #
class ReadResult(BaseModel):    stem: str; qtype: QType; options: list[ReadOption]
                                index: int = 0; num_text: str | None = None
                                clipped: list[str]; uncertain: list[str]
                                more_below: bool = False; note: str = ""
                                unsupported_qtype: str | None = None
class PageControl(BaseModel):   box: NormBox; label: str | None = None
class PageCard(BaseModel):      box: NormBox; cols: int = 0; rows: int = 0
                                current_box: NormBox | None; next_box: NormBox | None
class PageSubmit(BaseModel):    box: NormBox | None; scope: SubmitScope | None
class PageView(BaseModel):      progress: str | None; total: int | None; current: int | None
                                next_control: PageControl | None; card: PageCard | None
                                submit: PageSubmit | None
                                completed: CompletionState = CompletionState.UNKNOWN
                                scrolling: bool | None; reason: str = ""
class ReadBatch(BaseModel):     questions: list[ReadResult]; page: PageView | None
                                more_below: bool = False; note: str = ""; raw: str = ""
class RunPlan(BaseModel):       method: AdvanceMethod; total: int | None; current: int | None
                                control_box: NormBox | None; control_label: str | None
                                card: PageCard | None; card_step: tuple[float, float] | None
                                card_origin: tuple[float, float] | None; card_anchor: int = 1
                                submit_scope: SubmitScope | None; submit_box: NormBox | None
                                reason: str = ""; raw: str | None = None
# ⚠️ ReadResult **没有** submit_box / next_box / submit_scope（2026-09-30）——
#    提交范围与落点属于 PageView（由 solve/reader.parse_page_view 解析）与 RunPlan。
class VoteResult(BaseModel):    chosen_labels: list[str]; majority_ratio: float
                                distribution: dict[str, int]; n_samples: int
                                samples: list[SampleRecord]
class VerifyResult(BaseModel):  ok: bool; kind: VerifyKind; expected: str; actual: str
                                level_used: ActLevel | None; screenshot_ref: str | None
class TaskItem(BaseModel):      item_id: str; type: TaskType; qid: str | None; vid: str | None
                                state: QuestionState | MediaState; attempts: int
                                suspended: bool = False; created_at: datetime; updated_at: datetime
class CapabilityReport(BaseModel):                          # M2-6
                                auth_ok: bool; model_ok: bool
                                supports_vision: bool; supports_structured_output: bool
                                image_payload: str | None                    # "base64" | "url" | None
                                max_qps: float; latency_ms: int
                                vision_model: str | None; vision_evidence: str | None
                                error_code: str | None                       # 见 §2.4
```

#### core/qid.py · core/vid.py · core/states.py · core/tasks.py `[P0]`

```python
# core/qid.py — T0-1
def normalize_text(s: str) -> str                                  # NFKC 归一化
def make_qid(stem: str, option_texts: Sequence[str]) -> str         # sha1(...)[:16]
def make_stem_hash(stem: str) -> str                                # M3-7 执行前重校验

# core/vid.py — T0-7
def make_vid(course_id: str, episode_index: int, title: str) -> str # sha1(...)[:16]

# core/states.py — T0-2
TRANSITIONS: dict[QuestionState, frozenset[QuestionState]]
DANGER_STATES: frozenset[QuestionState] = frozenset({QuestionState.SUBMITTED})
def can_transition(src: QuestionState, dst: QuestionState) -> bool
def require_transition(src: QuestionState, dst: QuestionState) -> None     # 违约抛 IllegalTransition
def resume_entry(state: QuestionState) -> QuestionState                   # submitted → 只回读

# core/tasks.py — T0-8 / M5-1 / M5-2
class SuspendFrame(BaseModel):  frame_id: str; parent_item_id: str; child_item_id: str
                               media_state_at_suspend: VideoState; reason: str; created_at: datetime
class TaskStack:                # 落盘 SQLite
    def push(self, frame: SuspendFrame) -> None
    def pop(self) -> SuspendFrame | None
    def peek(self) -> SuspendFrame | None
    @property
    def depth(self) -> int
    def snapshot(self) -> list[SuspendFrame]
    def restore(self, frames: list[SuspendFrame]) -> None
def build_task_sequence(cfg: RunConfig) -> list[TaskItem]
```

#### core/router 与持久层

```python
# core/model_registry.py — M2-6 / M2-7
class ModelProfile(BaseModel):  profile_id: str; name: str; base_url: str
                                model: str          # **一套配置只有一个模型**（2026-09-28）
                                temperature: float; timeout_s: int; concurrency: int
                                api_key_ref: str; capabilities: CapabilityReport | None
                                enabled: bool = True; order: int = 0
# 老 models.yaml 里的双模型字段由 migrate_profile_payload() 自动迁移（取第一个），用户无需重配
class ModelRegistry:
    def load(self) -> None;  def save(self) -> None
    def list(self) -> list[ModelProfile]
    def add(self, profile: ModelProfile) -> ModelProfile
    def update(self, profile_id: str, patch: dict) -> ModelProfile
    def remove(self, profile_id: str) -> None
    def reorder(self, profile_ids: list[str]) -> None          # 排序即降级链
    def active_chain(self) -> list[ModelProfile]
    def mark_disabled(self, profile_id: str, reason: str) -> None
class CredentialStore:                                          # keyring → WinCred
    def put(self, ref: str, secret: SecretStr) -> None
    def get(self, ref: str) -> SecretStr | None
    def delete(self, ref: str) -> None
async def test_connection(profile: ModelProfile) -> CapabilityReport     # 实测，不接受手填

# core/arbiter.py — M1-3
def decide_channel(cfg: RunConfig, available: list[ProbeName]) -> ProbeName
def arbitrate(results: list[PerceptionResult], cfg: RunConfig) -> PerceptionResult
class DecisionTrace(BaseModel):  chosen: ProbeName; reason: str; conflicts: list[str]

# core/orchestrator.py — M4-1 / 2026-09-30 收口
class RunContext(BaseModel):    run_id: str; cfg: RunConfig; started_at: datetime
class Orchestrator:
    async def run(self) -> None
    async def pause(self) -> None
    async def resume(self) -> None
    async def stop(self) -> None
    async def _step_quiz(self, item: TaskItem) -> None
    async def _step_video(self, item: TaskItem) -> None
    async def _on_interrupt(self, item: TaskItem) -> None    # M5-2 压栈
    def _persist(self) -> None;  def _restore(self) -> None
    # ---- 开局裁决 → 只按方案推进（2026-09-30）------------------------------ #
    async def _plan_run(self, page) -> None                  # 开局读一屏 → derive_plan() → self._plan
    async def _advance(self, page, position=None) -> bool     # 只按 plan.method 推，**不换招**
    async def _run_advance_strategy(self, plan: RunPlan, ...) -> bool   # 第一个参数是 RunPlan
    async def _advance_by_card(self, plan: RunPlan, ...) -> bool        # 答题卡题号（几何 + 算术）
    async def _advance_by_control(self, plan: RunPlan, ...) -> bool     # 固定控件
    async def _settle_end(self, page) -> bool                # 推不动 → 收尾确认 → 否则 advance_failed
    def _expected_number_ok(self, question) -> bool          # 推进后题号与预期不符 → advance_failed
    # v0.2.0 / 2026-09-30 已删除：开局标定 / 记录推进方式 / 每步找下一题控件 /
    #                             视觉点击推进 / 滚到顶重读 那几个私有方法，以及 _submit_scope 字段

# core/events.py — SSE 事件常量
class Event:  RUN_STARTED="run.started"  RUN_PAUSED="run.paused"  RUN_RESUMED="run.resumed"
              RUN_FINISHED="run.finished"  RUN_ERROR="run.error"
              TASK_CREATED="task.created"  TASK_UPDATED="task.updated"
              TASK_STATE_CHANGED="task.state_changed"  TASK_NEEDS_CONFIRM="task.needs_confirm"
              PERCEPTION_DONE="perception.done"
              SOLVE_VOTE="solve.vote"  SOLVE_DONE="solve.done"
              SOLVE_REVIEW_REQUIRED="solve.review_required"
              # solve.escalated 已于 2026-09-29 随 Tier 分级删除，不许回流
              ACT_LEVEL_USED="act.level_used"  ACT_READBACK_MISMATCH="act.readback_mismatch"
              ACT_SUBMIT_TIMEOUT="act.submit_timeout"
              MEDIA_STATE_CHANGED="media.state_changed"  MEDIA_INTERRUPT_DETECTED="media.interrupt_detected"
              STACK_PUSHED="stack.pushed"  STACK_POPPED="stack.popped"  LOG_LINE="log.line"
              ADVANCE_CALIBRATED="advance.calibrated"          # 开局裁决完成（2026-09-30 起给出 RunPlan）
              ADVANCE_COMPLETION_CHECK="advance.completion_check"   # 收尾确认
              TRAINING_DONE="training.done"                    # 训练模式（2026-09-29 加）
# ALL_EVENTS 共 28 条（tests/test_t0_definitions.py::EVENT_COUNT 卡住）

# core/trace.py — M4-3 留痕
class RunLogger:
    def __init__(self, run_id: str, root: Path = Path("logs")) -> None
    def item_dir(self, item_id: str) -> Path
    def save_screenshot(self, item_id: str, stage: str, data: bytes) -> str
    def save_json(self, item_id: str, kind: str, payload: BaseModel | dict) -> str
    def save_model_raw(self, item_id: str, raw: str) -> str      # 模型原始响应全文必须落盘
class EventBus:
    def emit(self, event: str, payload: dict) -> None
    def subscribe(self) -> AsyncIterator[tuple[str, dict]]       # SSE 源
```

#### perception/*

```python
# perception/base.py — M0-4
class BaseProbe(ABC):
    name: ProbeName
    async def attach(self, page: Page) -> None
    async def is_available(self, page: Page, adapter: BaseAdapter) -> bool
    async def probe(self, page: Page, adapter: BaseAdapter, ctx: PerceptionContext) -> PerceptionResult
# v0.2.0：QuestionRootNotFoundError 已删除

# perception/dom_probe.py — M0-4 / M0-5
# **整个文件已删除（v0.2.0）**。题目侧不再解析页面文档结构。
# 下面这些能力随之删除：read_stem / read_options / read_qtype、三重就绪断言与
# canvas_ink_ratio（后者仍被靶场自检 scripts/check_mock.py 之类使用，不在产品链路里）。

# perception/media_probe.py — M0-7 / M5-3 / M5-4   ★ 保留
class MediaProbe(BaseProbe):
    async def attach(self, page: Page) -> None                        # 注册 timeupdate/ended/pause 监听
async def read_video_state(page, adapter) -> VideoState
async def wait_for_playback(page, adapter, window_s: float = 3.0, min_delta: float = 1.0) -> bool
async def wait_for_ended(page, adapter, timeout_s: float, poll_s: float = 0.5) -> bool  # 事件 + 轮询兜底
async def wait_for_interrupt(page, adapter, timeout_s: float) -> bool                   # MutationObserver + 轮询双保险
async def read_episode_index(page, adapter) -> tuple[int, int]

# perception/net_probe.py — M1-1（仅被动监听）
# **整个文件已删除（v0.2.0）**，core.models.NetworkRecord 一并删除。
# XHR 不再是被动抓取的对象，而是「模型要能看懂的一种页面形态」。
# 连带的 PerceptionContext.url_keyword 与 pipeline._enrich 也已删除。

# perception/vision_probe.py — M1-2（v0.2.0：唯一的题目通道）
class VisionProbe(BaseProbe):
    async def probe(page, adapter, ctx) -> PerceptionResult            # 返回 screenshot_ref + vision:crop_ok
    async def crop_question(self, page: Page) -> bytes                # **签名变了**：不再收 adapter/ctx。
                                                                      # 名字保留只为让调用点不改；语义 = 抓一张视口图
    @staticmethod
    async def shot_viewport(page: Page) -> bytes                      # 视口截图，**禁用 full_page**

# solve/reader.py — v0.2.0 的读题契约（唯一题目通道的消费方）
# 导出**以本文件的 __all__ 为准**（2026-09-30 收口：只剩一个提示词、一次读一屏）：
READ_SYSTEM_PROMPT                                            # 唯一的视觉提示词（= prompts/10-视觉组.md）
READ_TEMPERATURE = 0.0
def build_read_messages(image_png: bytes) -> tuple[str, list[dict]]
def parse_page_view(payload: dict) -> PageView | None         # 只认结构、不认散文（page 观测块）
def parse_read_batch(text: str) -> ReadBatch | None           # 一屏多题 + page 观测
def parse_read_payload(text: str) -> ReadResult | None        # 单题（历史形态，仍认）
def gate_read_result(result: ReadResult) -> tuple[bool, str | None]   # clipped / uncertain 非空就拦
async def read_questions(...) -> ReadBatch | None             # 一次读一屏
async def read_question(png: bytes, *, providers=...) -> ReadResult | None
# 另有 stem_fingerprint(stem)（qid / 缓存 / 续跑的身份指纹）仍在模块里，但未进 __all__
# 已删除（2026-09-30）：除 READ_SYSTEM_PROMPT 之外的三个旧系统提示词常量、
#                      「标定 / 收尾确认 / 找推进控件」三组函数与它们的载荷解析函数

# core/run_plan.py — 2026-09-30 新增（开局裁决：观测 → 唯一一份运行方案）
def derive_plan(batch: ReadBatch, *, batch_size: int = 0) -> RunPlan
def grid_geometry(card: PageCard) -> tuple[tuple[float, float], tuple[float, float]] | None
def card_cell_box(plan: RunPlan, number: int) -> NormBox | None      # 算不出来返回 None → **绝不点**
def all_done(page_view: object | None) -> bool                       # 只认 CompletionState.ALL_DONE
def plan_summary(plan: RunPlan) -> str                               # 一句话进事件流
CARD_GRID_TOLERANCE = 0.10                                           # 格子超出答题卡可见区域的容差

# perception/pipeline.py — M0-4
class PerceptionPipeline:
    def __init__(self, probes: list[BaseProbe], cfg: RunConfig) -> None
    async def run(self, page, adapter, ctx) -> PerceptionResult       # 题目流程
    async def run_video(self, page, adapter, ctx) -> VideoState       # 视频流程
    def chain(self) -> list[ProbeName]
```

#### solve/*

```python
# solve/providers/base.py — M2-1
class LLMRequest(BaseModel):   model: str; system: str; user: str
                               images: list[bytes] = []; temperature: float
                               force_json: bool = True; max_tokens: int = 2048
class LLMResponse(BaseModel):  text: str; parsed: dict | None; usage: TokenUsage
                               latency_ms: int; raw: str; error_code: str | None
class LLMProvider(ABC):
    name: ProviderName
    async def complete(self, req: LLMRequest) -> LLMResponse
    async def aclose(self) -> None
class ProviderError(Exception):     code: str
class AuthError(ProviderError)          # code="auth_failed"
class RateLimitError(ProviderError)     # code="rate_limited"
class ModelNotFoundError(ProviderError) # code="model_not_found"
class VisionNotSupportedError(ProviderError)  # code="vision_unsupported"
class StructuredNotSupportedError(ProviderError)  # code="structured_unsupported"

# solve/providers/openai_compat.py
class OpenAICompatProvider(LLMProvider):     # 换厂商只改 base_url + 模型名
    def __init__(self, profile: ModelProfile, api_key: SecretStr, concurrency: int) -> None

# solve/providers/mock.py
class MockProvider(LLMProvider):             # 读靶场 data-answer，注入错误率
    def __init__(self, answer_source: AnswerSource, error_rate: float = 0.0) -> None

# solve/providers/factory.py
def build_provider(profile: ModelProfile) -> LLMProvider
def build_provider_chain(profiles: list[ModelProfile]) -> list[LLMProvider]
def provider_name_of(profile: ModelProfile) -> ProviderName

# solve/voting.py — M2-2（导出以 __all__ 为准：EXPLICIT_EMPTY_CODE / SampleOutcome / VotingEngine …）
class VotingEngine:
    def build_index_map(self, shuffled: list[str], original: list[str]) -> dict[int, int]
    # ↑ **2026-09-30 起选项不再打乱**，「呈现序号 ↔ 原始序号」只剩归并历史留痕的用途。
    def vote(self, samples: list[LLMResponse], qtype: QType) -> VoteResult   # 按内容比对，不按字母
    @staticmethod
    def majority_ratio(vr: VoteResult) -> float

# solve/prompts.py — 解题组提示词与答复解析（导出以 __all__ 为准）
PROMPT_VERSION / SYSTEM_PROMPT                    # 解题组系统提示词（= prompts/20-解题组.md）
CONFIDENCE_KEY = "confidence"                     # 2026-09-30：解题组必须自报（空作答写 0）
def parse_confidence(raw: Any) -> float | None    # 0.8 / "0.8" / "80%" 都认；越界夹到 [0,1]
def parse_answer_payload(text: str) -> dict | None    # None = 回复**不可用**；explicit_empty = 模型的结论
def render_options(...) / option_pairs(...) / label_for_index(...) / build_messages(...)

# solve/solver.py — M2-3 / T0-3 / T0-4（导出以 __all__ 为准）
SELF_REF_PATTERN: re.Pattern                      # 自指选项白名单正则
SAMPLE_RETRY_MAX = 2                              # **不可用回复**的有界重发上限（每条采样项最多 3 次请求）
DEFAULT_TEMPERATURE / LOW_TEMPERATURE / SOLVE_MAX_TOKENS
def is_self_referential(text: str) -> bool
def question_has_self_ref(question: Question) -> bool
def allows_shuffle(question: Question) -> bool    # 只剩**风险标注**：呈现顺序一次都不打乱
class Solver:
    def __init__(self, providers: list[LLMProvider], cache: SolveCache, cfg: RunConfig) -> None
    async def solve(self, question: Question) -> Answer
    # 取样批次一律**页面顺序**；置信度：不复算用自报 confidence，复算用多数票占比
    # 已删除（2026-09-29）：should_escalate_to_tier2（复算改由 cfg.recalculate 显式决定）
    def mark_review(self, answer: Answer, reason: str) -> Answer

# solve/cache.py — M2-4
class SolveCache:
    def get(self, qid: str) -> Answer | None
    def put(self, answer: Answer) -> None
    def size(self) -> int
```

#### act/*

```python
# act/readback.py — M3-1
# **整个文件已删除（v0.2.0）**：题目侧已无任何 DOM 回读调用方。
# 随之删除的还有 ReadbackProbe / READBACK_PROBES / read_state() / is_selected()
# 以及 tests/test_readback.py。

# act/actuator.py — M3-2 / M3-3
class ActionKind(StrEnum):  CLICK="click"; SELECT_OPTION="select_option"; SUBMIT="submit"
                            PLAY_MEDIA="play_media"; PAUSE_MEDIA="pause_media"
                            SEEK_MEDIA="seek_media"; NEXT_EPISODE="next_episode"
                            SWIPE="swipe"
class ActLevel(StrEnum):    L1_LOCATOR="l1_locator"; L2_FORCE="l2_force"
                            L3_SCROLL="l3_scroll"; L4_FOCUS_KEYS="l4_focus_keys"
                            L5_BBOX="l5_bbox"; L6_VISION_XY="l6_vision_xy"
# v0.2.0：题目侧的 LEVELS（六级阶梯）已删除，六个 ActLevel 值仍由**媒体阶梯**使用。
MEDIA_LEVELS: tuple[ActLevel, ...]                 # 媒体动作：可信手势优先，evaluate 靠后
SEEK_LEVELS / PAUSE_LEVELS: tuple[ActLevel, ...]   # 另两条媒体阶梯

#: 归一化包围框 (x, y, w, h)，0..1，相对整张截图左上角
NormBox = tuple[float, float, float, float]
ImageSize = tuple[int, int]
def norm_box_center(box: NormBox, size: ImageSize) -> tuple[float, float]
# ↑ 视口 CSS 像素中心。截图一律 scale="css"（图像像素 == 视口 CSS 像素），
#   **只乘一次，不除 devicePixelRatio**。

class Actuator:
    def __init__(self, page: Page, cfg: RunConfig) -> None
    async def select_option(self, box: NormBox, size: ImageSize, qtype: QType,
                            *, target_label: str | None = None) -> ActionResult   # 点坐标 + 截图差分
    # ↑ 处置表**按落点分**（2026-09-29/30）：
    #   ink_centroid 落点 → changed / weak 都成功收工；判 none → **绝不重点**
    #                        （ok=True，readback = "no_change_on_ink:region_mad=…"）
    #   几何兜底落点判 none → 换下一个候选点（candidate_points，换点前**先重测**）
    #   全部候选都 none → _exhausted（ok=False + 截图 + pause）
    async def click(self, box: NormBox, size: ImageSize, *, kind: ActionKind = ActionKind.CLICK,
                    target_label: str | None = None) -> ActionResult              # 下一题 / 关弹窗
    async def submit(self, box: NormBox, size: ImageSize,
                     *, target_label: str | None = None) -> ActionResult          # 不重试、不重放；
                                                                                  # 失败 → 截图 + ACT_SUBMIT_TIMEOUT + pause
    async def play_media(self, adapter) -> ActionResult                            # 媒体侧签名不变
    async def pause_media(self, adapter) -> ActionResult
    async def seek_media(self, adapter, seconds: float) -> ActionResult
    async def next_episode(self, adapter) -> ActionResult
    async def ensure_media_locatable(self, adapter) -> ActionResult
    async def swipe(self, ...) -> ActionResult
# v0.2.0 已删除：select_option(locator…) / apply_answer / submit(locator) / click(locator) /
#               recheck_stem_hash / StemMismatchError / 题目用途的 LEVELS

# act/verifier.py — M3-5 / M5 量化断言
class VerifyKind(StrEnum):  SCREENSHOT_DIFF="screenshot_diff"   # 题目侧唯一判据
                            READBACK="readback"; MEDIA_PAUSED="media_paused"
                            MEDIA_PROGRESS="media_progress"; MEDIA_RESUME="media_resume"
                            EPISODE_INDEX="episode_index"; SUBMIT_RESULT="submit_result"
class Verifier:
    def __init__(self, page: Page, cfg: RunConfig) -> None
    async def verify_region_changed(self, box: NormBox, size: ImageSize, before: bytes) -> VerifyResult
    # ↑ 对选项区域做前后像素差分。**三态**（2026-09-29/30）：
    #   ok = state in {"changed", "weak"}；actual = "region_mad=0.31 state=weak"
    #   它只证明「那块像素变了」，**不证明「选中了正确的那一项」**。
    async def verify_playing(self, page, adapter, window_s=3.0, min_delta=1.0) -> VerifyResult
    async def verify_paused(self, page, adapter, window_s=3.0, max_delta=0.2) -> VerifyResult
    async def verify_resume_continuous(self, page, adapter, suspend_time: float) -> VerifyResult
    async def verify_episode_advance(self, page, adapter, before_index: int) -> VerifyResult
    async def verify_media_flag(self, adapter, *, paused=None, ended=None,
                                current_time=None, tolerance=1.0) -> VerifyResult
    async def read_media(self, adapter) -> VideoState | None
    def should_escalate(self, result: VerifyResult) -> bool
def region_change_state(mad: float) -> str
# ↑ 三态判定的**唯一定义点**：mad ≥ REGION_CHANGE_MIN_MAD(2.0) → "changed"；
#   ≥ REGION_CHANGE_WEAK_MAD(0.05) → "weak"；否则 "none"。别在调用方重新划线。
# v0.2.0 已删除：verify_readback(locator, expected) / verify_submit(locator)（题目 DOM 断言）；
#               verify_episode_advance 里用 adapter.anchors.result 的题目结果断言段也已删除。
```

#### adapters/*

```python
# adapters/base.py — M0-3（抽象必须在 M0 一次定完）
class BaseAdapter(ABC):
    site: str
    # v0.2.0：只剩**媒体侧**。题目侧锚点（AnchorSet / anchors / selectors / fields /
    #         locator() / question_scope() / variants / readiness …）已整体删除。
    media_anchors: ...            # 媒体锚点（来自 selectors_media.yaml）
    media_selectors / media_fields / media_assertions / interrupt_detection
    @classmethod
    def from_yaml(cls, media_path: Path) -> "BaseAdapter"
    async def matches(self, page: Page) -> bool
    def media_locator(self, page: Page, anchor: str, **kwargs) -> Locator
    def field(self, name: str, *, media: bool = False) -> str

# adapters/mock_exam/adapter.py
class MockExamAdapter(BaseAdapter):
    site = "mock_exam"
    @classmethod
    def load(cls, media: ... = None) -> "MockExamAdapter"    # qtype_of_input 已删除
# adapters/mock_exam/selectors.yaml 已删除；selectors_media.yaml 保留并新增 fields.body_site_attr
```

#### ui/*

```python
# ui/schemas.py
class RunConfigIn(BaseModel) / RunConfigOut(BaseModel)
class TaskItemOut(BaseModel) / TaskDetailOut(BaseModel)
class ModelProfileIn(BaseModel) / ModelProfileOut(BaseModel)   # 出参不含密钥明文
class ConfirmIn(BaseModel):  decision: Literal["confirm", "reject"]; note: str | None
class GuideCardOut(BaseModel): title: str; body_md: str; next_action: str; doc_url: str | None

# ui/guide.py — M2-8（四类情形独立文案，禁止共用泛化错误）
GUIDE_FIRST_RUN: GuideCardOut
GUIDE_EMPTY: GuideCardOut
GUIDE_AUTH_FAILED: GuideCardOut
GUIDE_MODEL_NOT_FOUND: GuideCardOut
GUIDE_RATE_LIMITED: GuideCardOut
def guide_for(error_code: str | None) -> GuideCardOut

# ui/deps.py
def get_registry() -> ModelRegistry
def get_orchestrator() -> Orchestrator
def get_event_bus() -> EventBus
def get_run_config() -> RunConfig
```

### 2.3 REST API 契约表（前端按此对接，可先行 mock）

| 方法 | 路径 | 请求 | 响应 | 说明 |
|------|------|------|------|------|
| GET | `/api/health` | — | `{ok, version}` | 探活 |
| GET | `/api/run/config` | — | `RunConfigOut` | 含 `model_ready`。**v0.2.0 去掉 `probe_order` / `probe_mode` / `available_modes`**；旧文件里的这些键由加载器静默剔除并自愈 |
| PUT | `/api/run/config` | `RunConfigIn` | `RunConfigOut` | 运行前设定；运行中只读（返回 409） |
| POST | `/api/run/start` | `{task_sequence?}` | `{run_id}` | `model_ready=false` → 400 + 引导码。**v0.2.0 收紧**：原来是「`probe_order=vision_first` 才强制」，现在**任何**运行都必须有支持视觉的模型（没有模型完全跑不了） |
| POST | `/api/run/pause` | — | `{ok}` | — |
| POST | `/api/run/resume` | — | `{ok}` | — |
| POST | `/api/run/stop` | — | `{ok}` | — |
| GET | `/api/run/progress` | — | `{total, done, by_state, current_item_id}` | SSE 重连后对账用 |
| GET | `/api/tasks` | `?type=&state=` | `TaskItemOut[]` | 题目 + 分集混合 |
| GET | `/api/tasks/{item_id}` | — | `TaskDetailOut` | 含 before/after 截图 URL、采样明细、`level_used` |
| POST | `/api/tasks/{item_id}/confirm` | `ConfirmIn` | `TaskItemOut` | `submitted` 态返回 409 并置灰 |
| GET | `/api/tasks/{item_id}/artifacts` | — | `{files: []}` | 留痕文件清单 |
| GET | `/api/models` | — | `ModelProfileOut[]` | 不含密钥 |
| POST | `/api/models` | `ModelProfileIn` | `ModelProfileOut` | 密钥写入 `CredentialStore` |
| PUT | `/api/models/{profile_id}` | `ModelProfileIn` | `ModelProfileOut` | 密钥留空表示不改 |
| DELETE | `/api/models/{profile_id}` | — | `{ok}` | 同步删凭据 |
| PUT | `/api/models/order` | `{profile_ids: []}` | `{ok}` | 排序即降级链 |
| POST | `/api/models/{profile_id}/test` | — | `CapabilityReport` | 实测并回写 |
| GET | `/api/providers/presets` | — | `PresetOut[]` | 表单预设模板 |
| GET | `/api/guide` | `?code=` | `GuideCardOut` | 四类情形独立文案 |
| GET | `/api/events` | — | `text/event-stream` | SSE，见 §2.5 |
| GET | `/api/logs/{run_id}/{item_id}/{kind}` | — | `text/plain` | `kind` ∈ `perception/solve/action` |

### 2.4 错误码字典（前后端 + 日志共用）

| `error_code` | 触发场景 | UI 表现 | 引导卡 |
|--------------|---------|---------|--------|
| `no_config` | 无任何模型配置 | 不报错，展示引导卡 | `GUIDE_FIRST_RUN` |
| `auth_failed` | 401 / 403 | 配置项标红 + 下一步动作 | `GUIDE_AUTH_FAILED` |
| `model_not_found` | 404 / 模型名无效 | 标红 + 建议换名 | `GUIDE_MODEL_NOT_FOUND` |
| `rate_limited` | 429 | 降速提示 + 重试倒计时 | `GUIDE_RATE_LIMITED` |
| `vision_unsupported` | 模型不支持图片 | 该模型不能作为视觉组；**任何运行都必须有支持视觉的模型** | `GUIDE_EMPTY` |
| `structured_unsupported` | 不支持 JSON Schema | 降级为文本解析 | — |
| `readback_mismatch` | 回读不一致 | 详情页高亮 | — |
| `submit_timeout` | 提交后无结果 | 暂停 + 留档 | — |
| `stack_restored` | 续跑恢复栈 | 日志提示 | — |
| `advance_failed` | 开局裁决的那**一种**推进方式推不动（不换招），或推进后读到的题号与方案预期不符，且让视觉组再确认「是否全部完成」也没确认 | 停下并显示卡在哪一题 | — |
| `action_ladder_exhausted` | 执行阶梯走到顶仍拿不到期望状态 | 暂停 + 截图 + 记 failed | — |
| `target_unavailable` | 目标不存在 / 已关闭 / 附加失败 | 回到目标选择 | — |
| `target_channel_unsupported` | ~~通道不受目标类型支持~~ | **已删除（v0.2.0）**：题目通道只有一条，不存在「目标不支持该通道」。目标接不上改记 `target_unavailable` | — |

> **v0.2.0 的读题门禁原因码**（**不是** `ErrorCode` 成员，是 `gate_read_result()` 的返回值，
> 由编排层作为暂停原因记入留痕）：`vision_incomplete`（模型声明题面被截断）、
> `vision_uncertain`（模型声明拿不准）。放行的结果是交给解题组；被拦的结果一律不放行。

### 2.5 SSE 约定

- 三个必备处理：`X-Accel-Buffering: no`、每 15s 发注释行心跳、重连后主动拉 `/api/run/progress` 全量对账
- 事件格式：`event: <domain>.<object>.<action>\ndata: {...}\n\n`
- 事件名清单见 §2.2 `core/events.py`；新增事件必须同步本文档

### 2.6 SQLite 表结构（P0 冻结，P7 落实现）

| 表 | 关键列 |
|----|--------|
| `run` | `run_id, started_at, finished_at, status, config_json` |
| `task_item` | `item_id, run_id, type, qid, vid, state, attempts, suspended, created_at, updated_at` |
| `answer` | `qid, chosen_labels_json, confidence, tier_used, review_flag, model_name, stem_hash` |
| `suspend_frame` | `frame_id, run_id, parent_item_id, child_item_id, media_state_json, reason, created_at`（M5-2 栈落盘，栈顶优先） |
| `level_stat` | `item_id, kind, level_used, ok, elapsed_ms`（M4-4 热力图数据源） |
| `media_position` | `vid, episode_index, last_position, updated_at`（M5-5 断点续跑） |

---

## 3. 命名规范

### 3.1 目录与文件

```
autolearn/
├─ run.bat                         # 源码模式一键启动（venv + uvicorn）
├─ requirements.txt                # 依赖锁定
├─ pyproject.toml                  # ruff / pytest 配置
├─ .env.example                    # 只提交示例，.env 入 .gitignore
├─ README.md
├─ docs/                           # 本规划书、任务书
│   ├─ AutoLearn-项目任务书.md
│   ├─ AutoLearn-实施规划书.md
│   └─ 双模型分工-视觉组与解题组.md 真实站点作业做题手册.md
│   # CHANGELOG-interface.md / channel-bench.md / degrade-heatmap.md 已于 2026-09-29 删除
├─ core/
│   ├─ config.py  models.py  qid.py  vid.py  states.py  tasks.py
│   ├─ model_registry.py  arbiter.py  orchestrator.py
│   ├─ advance_library.py  run_plan.py   # 推进方式库 / 开局裁决（2026-09-30）
│   ├─ events.py  trace.py  ratelimit.py
│   └─ db.py                       # SQLite 连接与建表（6 张表）
├─ perception/
│   ├─ base.py  media_probe.py  pipeline.py  vision_probe.py   # dom_probe.py / net_probe.py 已删除（v0.2.0）
├─ prompts/                        # 2026-09-30 起只有 5 份
│   ├─ 00-共享契约.md  10-视觉组.md  20-解题组.md
│   ├─ 33-推进方式库.md  34-训练总结.md
│   └─ # 标定 / 收尾确认 / 推进控件三份旧提示词已删除，不许回流
├─ solve/
│   ├─ solver.py  voting.py  cache.py  prompts.py  reader.py  training.py
│   └─ providers/{base.py, openai_compat.py, mock.py, factory.py}
├─ act/
│   ├─ actuator.py  verifier.py            # readback.py 已删除（v0.2.0）
├─ adapters/
│   ├─ base.py
│   └─ mock_exam/
│       ├─ adapter.py
│       └─ selectors_media.yaml    # 媒体锚点组（selectors.yaml 已删除，v0.2.0）
├─ mock_site/
│   ├─ quiz.html                   # 50 题，6 类坑，每题 data-answer
│   ├─ course.html                 # 分集 + video + 可配置弹题
│   └─ static/{mock.css, quiz_runtime.js, course_runtime.js, questions.json, traps.md}
├─ ui/
│   ├─ server.py                   # FastAPI 装配入口
│   ├─ deps.py  schemas.py  guide.py
│   ├─ routes/{run.py, tasks.py, models.py, events.py, artifacts.py}
│   └─ static/{index.html, app.js, style.css}
├─ scripts/
│   ├─ serve_mock.py               # 起靶场静态服务
│   ├─ probe_bench.py              # M1-4a 单通道（视觉）量化
│   ├─ export_heatmap.py           # M4-4 降级热力图
│   └─ reset_state.py
├─ state/                          # autolearn.db / models.yaml / storage_state.json
├─ logs/<run_id>/<item_id>/        # before.png after.png perception.json solve.json action.json verify.json
└─ tests/
```

### 3.2 文件命名规则

| 类型 | 规则 | 示例 |
|------|------|------|
| Python 模块 | `snake_case.py`，一名一职 | `media_probe.py` |
| 测试 | `tests/test_<被测模块>.py` | `tests/test_voting.py` |
| 用例 | `test_<场景>_<期望>` | `test_shuffled_options_qid_unchanged` |
| 靶场页 | `mock_site/<场景>.html` | `quiz.html` / `course.html` |
| 适配器锚点 | `adapters/<site>/selectors.yaml` | **v0.2.0 起题目侧没有选择器了**；媒体仍用 `selectors_media.yaml` |
| 静态资源 | 同目录 `static/`，`<页名>_runtime.js` | `course_runtime.js` |
| 日志 | `logs/<run_id>/<item_id>/<stage>.<ext>` | `logs/r_01/a1b2c3/before.png` |
| 分支 | `<type>/<part>-<短描述>` | `feat/p2-media-probe` |
| 事项 | `[<Part>] <交付物>` | `[P2] 媒体感知探针` |

### 3.3 标识符命名

| 元素 | 规则 |
|------|------|
| 类 | `PascalCase`，后缀即语义：`*Probe` 感知 / `*Adapter` 站点 / `*Provider` 模型厂商 / `*Engine` 纯计算 / `*Cache`·`*Registry`·`*Store` 持久化 / `*Verifier` 校验 / `*Orchestrator` 编排 |
| 函数 | `snake_case`，动词前缀表意：`read_*` 同步纯读（无副作用）／`wait_for_*` 等待条件／`make_*` 构造标识／`build_*` 组装结构／`parse_*` 原始→对象／`verify_*` 断言并返回 `VerifyResult`／`is_*` 布尔判定／`normalize_*` 归一化 |
| 私有 | 单下划线前缀 `_attempt`，`_` 前缀不得跨模块调用 |
| 常量 | `UPPER_SNAKE`，集中在 `core/` 对应模块顶部 |
| 字段 | `snake_case`；布尔用 `is_`/`has_`/`can_`；时间统一 `*_ms`(int) 或 `*_at`(datetime)，**禁止裸 `timestamp`** |
| 枚举值 | 字符串小写连字符，与 UI 文案解耦：`"dom-first"`（v0.2.0 起该枚举已删除，示例仅示形制） |

### 3.4 事件与接口命名

| 类型 | 规则 | 示例 |
|------|------|------|
| SSE 事件 | `<domain>.<object>.<action>` 全小写点分 | `media.interrupt_detected` |
| REST 路径 | `/api/<resource>` 复数；动作走 `POST .../<id>/<verb>` | `POST /api/tasks/{id}/confirm` |
| REST 字段 | `snake_case`，与 Python 一致，不做驼峰转换 | `probe_order` |
| 错误码 | `snake_case` 短语，进 §2.4 字典才准用 | `readback_mismatch` |
| 配置键 | `snake_case`；嵌套不超过 3 层 | `guards.click_replay_max` |

### 3.5 前端命名（零构建单页）

- DOM id：`kebab-case` + 区域前缀 `zone-`，五区固定为 `zone-run-config` / `zone-models` / `zone-tasks` / `zone-detail` / `zone-logs`
- JS 函数：`camelCase`，渲染函数统一 `render*`、请求函数统一 `api*`、订阅统一 `on*`
- 不引入构建工具；`index.html` + `app.js` + `style.css` 三文件封顶，超出则拆 `views/<view>.js`

### 3.6 新增文件怎么放

按语义就近落位：**感知 → `perception/`；求解 → `solve/`；动手 → `act/`；站点差异 → `adapters/`；跨模块契约 → `core/`**。
判不准时问自己：换一个网站它要不要改？要改 → `adapters/`。换一个模型厂商要不要改？要改 → `solve/providers/`。都不用改 → `core/`。

---

## 4. Part 详述

> 每个 Part 给：**目标功能 / 交付文件 / 接口 / 验收口径 / 依赖 / 坑**。
> 验收口径直接对应任务书验收标准，逐条勾选后才算 Part 完成。

### P0 — 契约与骨架冻结（0.5 人日，阻塞全部）

**目标功能**：把任务书 §4 的 8 项定义变成代码，让后面所有人不用猜。**不写业务逻辑，只写常量、枚举、类型、转移表。**

**交付文件**
```
core/config.py       # RunConfig / GuardThresholds / RateLimits（v0.2.0：无 ProbeOrder / ProbeMode）
core/states.py       # 九态 + TRANSITIONS 完整转移表 + DANGER_STATES
core/qid.py          # normalize_text / make_qid / make_stem_hash
core/vid.py          # make_vid
core/tasks.py        # TaskType / TaskItem / SuspendFrame / TaskStack 骨架
core/models.py       # 全部 Pydantic 模型
core/events.py       # 事件常量
core/db.py           # 6 张表建表 SQL
docs/CHANGELOG-interface.md   # 新建，空表头
```

**分工**（半天内并行完成）
| 认领人 | 定义项 |
|--------|--------|
| C | T0-1 qid、T0-2 状态机 |
| B | T0-3 升级阈值、T0-6 M2 止损数字 |
| A | T0-4 自指白名单正则、T0-5 回读处置常量 |
| D | T0-7 vid、T0-8 任务类型与中断模型 |

**验收口径**
- [ ] 8 项全部落为**代码常量或配置项**，无散落魔法数字
- [ ] `make_qid()` 通过「打乱选项后 qid 不变」单测
- [ ] 状态机转移表通过「`submitted` 态续跑不重复提交」单测
- [ ] 自指检测通过「含自指选项的题不被乱序」单测
- [ ] `TaskStack` 通过「压栈→弹栈→恢复」单测（用内存实现即可，落盘留给 P8）
- [ ] §2.2 全部签名提交为 `raise NotImplementedError` stub，`ruff` + `mypy` 通过

**坑**
- `submitted` 是唯一危险态：`TRANSITIONS` 里必须体现「`submitted` → `verified`/`failed` 只能由结果回读触发」。
- `qid` **不含答案**；选项排序后参与哈希，别把原始顺序写进去。

---

### P1 — 双靶场（2 人日，可与 P0 并行）

**目标功能**：造两个可控假网站。这是全项目的**地面真值来源**，靶场质量直接决定后面能不能验收。

**交付文件**
```
mock_site/quiz.html
mock_site/course.html
mock_site/static/mock.css
mock_site/static/quiz_runtime.js
mock_site/static/course_runtime.js
mock_site/static/questions.json      # XHR 拉题的题型
mock_site/static/traps.md            # 6 类坑分布表（哪题埋哪个坑）
scripts/serve_mock.py
```

**题目靶场要求**
- 50 题，**每题埋 `data-answer`**（ground truth），含 1 个 XHR 拉题题型
- 6 类坑各至少 3 题：SPA 动态渲染 / 懒加载 / Canvas 题干 / iframe 嵌套 / class 名混淆 / 弹窗遮罩
- `traps.md` 用表格记录：题号 · 坑类型 · 触发条件 · 期望的降级层级

**网课靶场要求**
- 分集列表（≥5 集）+ `<video>` + 「下一集」按钮
- **可配置触发弹题**：URL 参数如 `?interrupt_at=30`，即第 30s 弹出；另提供 `?interrupt_at=end`（与 `ended` 同刻到达，用于验优先级）
- 每集埋 `data-vid` 与 `data-duration`
- 弹题弹窗**不得使 `video.paused` 变真**（用于验证「弹题不是媒体态」这一判断）

**验收口径**
- [ ] 题目靶场含全部 6 类坑，每题埋 `data-answer`，含 1 个 XHR 拉题题型
- [ ] 网课靶场含分集列表、可触发弹题打断、播放结束事件、下一集按钮
- [ ] `python scripts/serve_mock.py` 一条命令起服务，`http://127.0.0.1:8899/quiz.html` 可访问
- [ ] `traps.md` 覆盖 50 题，无遗漏

**坑**
- 靶场 duration 别设太长，用 20~60s；长视频会拖慢 M5 全部验收。
- iframe 题要真跨域才有效（`srcdoc` 不算），否则测不出视觉兜底路径。

---

### P2 — 视觉 + 媒体感知（3 人日）

> **v0.2.0 重写**：本 Part 原为「**DOM** + 媒体感知」——「读懂页面」靠解析文档结构。
> 现在题目侧的读题在 P3 交付（截图 + 模型），P2 只剩**媒体感知**与感知层的公共骨架。
> 因此 P2 与 P3 的边界也随之调整：凡是「题目怎么读」的内容都不再属于 P2。

**目标功能**：视频播放三态 + 弹题打断探测；感知层公共契约（探针基类、流水线调度、感知结果）。

**交付文件**
```
perception/base.py  perception/media_probe.py  perception/pipeline.py
adapters/base.py  adapters/mock_exam/adapter.py
adapters/mock_exam/selectors_media.yaml
core/qid.py（补实现）  core/vid.py（补实现）
tests/test_media_probe.py  tests/test_pipeline.py
# 已删除（v0.2.0）：perception/dom_probe.py、adapters/mock_exam/selectors.yaml、tests/test_dom_probe.py
```

**接口**：见 §2.2 `perception/*`、`adapters/*`

**验收口径**
- [ ] 媒体锚点组抽象完成，靶场适配器由 `selectors_media.yaml` 驱动，**`media_probe` 内零硬编码靶场 DOM**
- [ ] 媒体侧三重就绪断言仍生效：visible 且文本长度 > 0；懒加载先 `scroll_into_view_if_needed()`；Canvas 非背景像素占比 > 5%
- [ ] **全局禁用 `networkidle`**（加 lint 规则或 grep 单测卡住）
- [ ] 媒体探针能正确读出播放 / 暂停 / 结束三态
- [ ] `wait_for_interrupt()` 在弹题出现 1s 内触发，且**不误报**（无弹题场景连续 60s 零触发）
- [ ] 感知层只调度**一条**题目通道（`active_probe_chain()` 恒为视觉）+ 媒体独立调度
- [ ] **零模型调用**（用 mock patch 断言未构造任何 Provider；媒体态全部读 `<video>` 属性）

**坑**
- 媒体态必须走 `paused`/`ended`/`currentTime`/`duration`，**不要用元素可见性代替**。
- 弹题不会改 `paused`，所以 `wait_for_interrupt` 得用 MutationObserver + 定时轮询双保险。
- `adapters/base.py` **只剩媒体侧**：题目侧的 `anchors` / `selectors` / `locator()` /
  `question_scope()` 已在 v0.2.0 删除，别照着旧签名实现。

---

### P3 — 视觉读题 + 单通道流水线（3.5 人日）

> **v0.2.0 重写**：本 Part 原为「网络通道 + 视觉兜底 + 仲裁」—— 视觉只是 DOM 读不到时的备胎。
> 现在**视觉就是主路、也是唯一的路**：网络通道删除，仲裁收敛为单通道，
> 「三通道量化」改为「单通道量化」。本节还接管了原属 P2 的「题目怎么读」。

**目标功能**：截视口图 → 模型读出题干 / 选项 / 归一化坐标**与这一屏的观测**；读题质量门禁；开局裁决与收尾确认。

**交付文件**
```
perception/vision_probe.py      # shot_viewport() / crop_question(page)（本层只出图）
solve/reader.py                 # READ_SYSTEM_PROMPT / build_read_messages / parse_page_view /
                                # parse_read_batch / parse_read_payload / gate_read_result /
                                # read_question / read_questions（**导出以 __all__ 为准**）
core/run_plan.py                # 开局裁决：derive_plan / grid_geometry / card_cell_box /
                                # all_done / plan_summary
core/advance_library.py         # 推进方式库：PLAN_PREFERENCE / ADVANCE_LIBRARY / strategy_for
core/arbiter.py                 # 单通道
scripts/probe_bench.py          # M1-4a 单通道量化骨架
docs/channel-bench.md           # 单通道耗时/图像字节结论
tests/test_arbiter.py  tests/test_vision_probe.py  tests/test_reader.py
tests/test_run_plan.py  tests/test_advance_library.py
# 已删除（v0.2.0）：perception/net_probe.py、tests/test_net_probe.py
# 已删除（2026-09-30）：reader 里的「标定 / 收尾确认 / 找推进控件」三组函数
#                      与标定 / 收尾确认 / 推进控件三份旧提示词
```

**验收口径**
- [ ] 视觉读题：不加任何题目选择器也能读出题干/选项/坐标（有单测卡住）
- [ ] 视觉组**只做观测**：一次回 `{"page": {...}, "questions": [...], …}`，`page` 里只有 `progress` / `total` / `current` / `next_control` / `card` / `submit` / `completed` / `scrolling` / `reason`，**不含任何「该怎么做」的字段**
- [ ] 截图**只用视口**、**全局禁 `full_page=True`**；截图失败有重试
- [ ] 归一化框换算只有一处（`norm_box_center`）：`scale="css"` 下**只乘一次，不除 `devicePixelRatio`**
- [ ] 读题门禁：`clipped` 非空 → `vision_incomplete`；`uncertain` 非空 → `vision_uncertain`；**拦下就带原因暂停，不放行**。`more_below` 与「这一屏没有提交按钮」都**不归门禁管**
- [ ] 开局裁决产出**唯一一份** `RunPlan`（推进方式**恰好一种** + 推进几何 + 提交范围 + 总题数），此后不换招、不再问模型「下一题在哪」（`core/run_plan.py::derive_plan`；有 `tests/test_run_plan.py` 卡住）
- [ ] 答题卡落点是**纯算术**（`grid_geometry` / `card_cell_box`，网格按行换行、容差 `CARD_GRID_TOLERANCE = 0.10`）；算不出来返回 `None` → **绝不点**
- [ ] 推不动就停：`_settle_end` 看视觉组对 `page.completed` 的观测，只有 `all_done` 才收工；确认不了 / 推进后题号与方案不符 → `advance_failed` 停下，**绝不静默跳过**
- [ ] 单通道仲裁：读失败 → **暂停留档，绝不静默跳过**（原「四分支仲裁」已收敛）
- [ ] `probe_bench.py` 产出结构化记录（图像字节 + 耗时），**零密钥可验收**；旧三通道结论作废
- [ ] 用户添加模型配置后补齐真实读题用量（可回填至 P4 期间）

**坑**
- 跨域 iframe 的能力只能验「读题链路本身」，识别质量要等用户配了支持视觉的模型才能判。
- **推进方式只能有一种**（2026-09-30）：换招阶梯（点击 → 滚动 → 滑动）正是「一次跳过十几道题」的成因 —— 旧实现在滚动模式下照样先点按钮，找不着就一路滚到上限（0.8 屏 × 6 = **4.8 屏**），真机题号从第 3 题**跳到第 16 题**。现在推不动就停下等人，**不要为了「多走一步」再加回兜底**。
- **0.2.0 最需要防的失效模式不是「读不到」，而是「读错了却看不出来」**：模型返回结构完好的
  JSON、没有错误信号。所以门禁的「非空就拦」不许放宽成「轻微的放过去」，
  拦截的代价只是停下来问人，放行的代价是一次静默的错误作答。
- 坐标不准的排查顺序：截图是不是 `scale="css"` → 有没有多除/少乘一次 → 模型给的框是不是归一化的。
- 提问「该怎么推进」的字段**不许加回 `page`**：那等于把控制流交给一个看不见程序的模型，而它每次的说法都可能不一样。裁决规则写在 `derive_plan()` 里，改规则就改那一处。

---

### P4 — 求解层 + 模型 Provider（3.5 人日）

**目标功能**：让系统会做题。多厂商统一接入、**只吃文本**（选项标号 = 页面标号，不打乱）、不复算时用模型自报的置信度、开了复算才投票、精确缓存。

**交付文件**
```
solve/providers/base.py  solve/providers/openai_compat.py
solve/providers/mock.py  solve/providers/factory.py
solve/voting.py  solve/solver.py  solve/cache.py  solve/prompts.py
core/model_registry.py           # 与 P5 分工：本 Part 出注册表与凭据层，P5 出面板
scripts/probe_bench.py           # 回填真实 token 数据
tests/test_voting.py  tests/test_solver.py  tests/test_cache.py  tests/test_mock_provider.py
```

**验收口径**
- [ ] **解题组只吃文本**（2026-09-30）：请求里 `images` 恒为空；选项标号**逐字等于页面标号**（可能不连续，如 A、B、D），**一次也不打乱呈现顺序**（有「呈现顺序 = 页面顺序」的测试用例）
- [ ] 投票**按内容比对**（多选题按 `frozenset` 整体集合记票）；历史（打乱时代）的留痕仍归并得回来
- [ ] 默认**不复算**（`recalculate=False` → 每题只调一次模型，**取第一次答案**）；开了复算时按 `sample_n`（≥2）取样并用多数票占比；每题记录采样明细、置信度、决策
- [ ] 置信度口径（2026-09-30）：**不复算** → 用模型**自报**的 `confidence`（`0.8` / `"0.8"` / `"80%"` 都认，越界夹到 `[0,1]`；空作答写 0），低于 `guards.confidence_review_min`（默认 0.5）→ 标 `⚠复核`（理由「自报置信度低于门限」）并停下等人；模型**没报** → 回到一致率（单样本恒 1.0，**不新增暂停**）
- [ ] **开复算**时用多数票占比，**自报值不参与**；一致率低于 `agreement_accept` → `⚠复核` 必停
- [ ] **不可用的单次回复**（无 content / 解析不出 / 标号越界 / provider 报错）= **请求失败** → `SAMPLE_RETRY_MAX = 2` 有界重发（每条采样项最多 3 次请求），**不是复算**；**显式空作答是模型的结论**，不重发
- [ ] 自指选项（`allows_shuffle` / `question_has_self_ref`）只剩**风险标注**：呈现顺序不因它改变，温度也不再分档（一律 `DEFAULT_TEMPERATURE`）
- [ ] `MockProvider` 读靶场 `data-answer`，可按注入错误率验证投票逻辑正确性
- [ ] 精确缓存命中 qid 直接返回并计入采样明细（标记 `solve_path=single/mock/cache` 语义正确；库里的列名仍是冻结的 `tier_used`）
- [ ] `test_connection()` 实测项齐全：鉴权 / 模型名可用性 / `supports_vision` / `supports_structured_output` / `image_payload` / 真实 QPS，**不留手填字段**
- [ ] 密钥走 `api_key_ref`，`models.yaml` 无敏感字段

**坑**
- `MockProvider` 直接读答案，**结果不得作为 M2 闸门依据**，只能验投票逻辑。
- **别再加回打乱**：标号错位会让「模型答 `D`、程序按位置落回 `C`」这种错答**从输出上看不出来**（真机 `logs/10ca5ee8c89e/.../solve.json` 的现场）。计票仍按**内容**比对。
- **别把「回复不可用」当成复算**：不可用是请求失败，走有界重发；复算是「用户显式要求多算几次」。两者混在一起会让「不开复算就没法解题」这个 bug 复发。
- 用户未配模型时：M2 闸门判定**顺延**，期间只验 MockProvider 全链路 + UI 引导。

---

### P5 — UI（4 人日，最早开工、独立车道）

**目标功能**：五区单页 + SSE 实时流 + 模型配置独立面板 + 四类空/错状态引导。

**交付文件**
```
ui/server.py  ui/deps.py  ui/schemas.py  ui/guide.py
ui/routes/{run.py, tasks.py, models.py, events.py, artifacts.py}
ui/static/index.html  ui/static/app.js  ui/static/style.css
tests/test_api_run.py  tests/test_api_models.py  tests/test_sse.py
```

**五区布局（DOM id 固定，前端不得改名）**
| 区 | id | 内容 |
|----|----|------|
| ① | `zone-run-config` | dry_run 开关、任务序列、核对次数、提交模式、启动/暂停/停止。**v0.2.0 删掉了「通道优先级 / 通道模式」单选** —— 题目通道只有视觉一条，没得选 |
| ② | `zone-models` | 独立面板：多套配置新增/编辑/删除/排序 + [测试连接] + 能力实测结果 |
| ③ | `zone-tasks` | 题目 + 分集混合列表，分集显示进度、弹题显示状态 |
| ④ | `zone-detail` | before/after 截图 + 采样明细 + `level_used` + 确认/否决按钮 |
| ⑤ | `zone-logs` | SSE 日志流 + 重连对账提示 |

**验收口径**
- [ ] 五区布局可交互；`app.js` 零构建依赖
- [ ] SSE 三项处理齐全：`X-Accel-Buffering: no`、15s 注释行心跳、重连后拉 `/api/run/progress` 对账
- [ ] 四类文案各自独立：空状态 / 鉴权失败 / 模型名不存在 / 限流，**不共用一个泛化错误提示**
- [ ] 首次运行（无配置）展示**引导卡片**而非报错，不阻塞运行
- [ ] 多套配置可新增 / 编辑 / 删除 / 排序；排序结果即降级链
- [ ] 密钥保存后不回显明文，且 `input.value` 已清空
- [ ] `submitted` 态任务确认按钮**置灰**，后端同时返回 409
- [ ] ~~通道优先级可切换；无模型配置时「模型优先」置灰并给出引导提示~~ → **已作废（v0.2.0）**：通道选择整体删除，取而代之的硬约束是「**任何**运行都必须有支持视觉的模型」，未选时预检拒绝 + 引导码
- [ ] W1 起可用 mock 数据独立开发，不被后端阻塞

**坑**
- 模型缺视觉能力时**不是置灰而是硬拦截**（v0.2.0）：引导卡必须给下一步动作，但不存在「换个通道也能跑」这种安慰。
- 排序即降级链，顺序变了 `active_chain()` 必须立刻反映。

---

### P6 — 执行层 + dry_run（4.5 人日）

**目标功能**：真的动手且能自证。**按模型给的归一化坐标作答**、媒体动作（顺序相反）、提交不重试、**截图差分三态校验与按落点处置**。

**交付文件**
```
act/actuator.py  act/verifier.py
core/trace.py（截图/JSON 落盘部分）
tests/test_actuator.py  tests/test_verifier.py  tests/test_region_change.py
# 已删除（v0.2.0）：act/readback.py、tests/test_readback.py
```

**验收口径**
- [ ] 单题全自动闭环跑通（感知 → 求解 → 坐标执行 → 截图差分 → 提交 → 结果确认）
- [ ] 题目侧**按坐标作答落准**，`level_used` / 坐标来源 / 落点（`aim`）正确记录并可导出（**题目侧已无六级阶梯**）
- [ ] 坐标换算只有一处：`norm_box_center()`，`scale="css"` 下**只乘一次、不除 `devicePixelRatio`**
- [ ] 点击回读按**三态**处置（`region_change_state`）：`changed` / `weak`(≥0.05) 都算点到；落在墨迹（`ink_centroid`）上判 `none` → **绝不重点**（照旧收工，`readback` 记 `no_change_on_ink:…`）；几何兜底落点判 `none` → 换下一个候选点（换点前**先重测**）；候选全 `none` → 暂停 + 截图 + 记 `failed`；**禁用 `assert`，禁止静默继续**
- [ ] **截图差分的能力边界写进文档**：它只证明「选项区域像素变了」，**不证明「选中了正确的那一项」**
- [ ] 媒体动作阶梯**顺序与元素点击相反**：优先真实点击播放器区域或空格键，`evaluate("video.play()")` 排后面；启动参数含 `--autoplay-policy=no-user-gesture-required`
- [ ] 提交动作**不重试、不重放**，超时暂停等人
- [ ] ~~题目身份重校验：`stem_hash` 不一致 → 拒绝执行~~ → **已删除（v0.2.0）**：`recheck_stem_hash` / `StemMismatchError` 依赖重读 DOM 题干，随 DOM 通道删除；`stem_hash` 仍用于 qid / 缓存 / 续跑的身份匹配
- [ ] `dry_run=True`：题目执行到「选项已选好」即停；**视频停在 `PlayMedia` 前**，只校验播放器可定位
- [ ] 失败题留截图 + 暂停，不静默跳过

**坑**
- `evaluate("video.play()")` 属不可信手势，会被抛 `NotAllowedError`，所以媒体动作顺序必须反过来。
- **坐标换算别照 0.1.0 的写法**：那时 L6 用设备像素截图、需要 `css = image / devicePixelRatio`；
  现在一律 `scale="css"`，多除一次就是「每次点击都偏一半」。
- **别把「区域没变」直接当成「没点中」**（2026-09-29/30）：真实站点上的选中态常常只是 1px 描边或一个小圆点（整块区域 `region_mad` 只有 0.1~1.5）。判错一次就会在**同一个框里**再点 1~3 个候选点 —— 多选上那是把刚选上的勾取消。两次真机误点（`region_mad=0.00` 点在行尾空白、`region_mad=0.06` 点了被禁用的交卷按钮）都是这么来的。

---

### P7 — 编排循环 + 限速 + 留痕（3.5 人日）

**目标功能**：50 题全自动跑完并可中断续跑，全程可追溯。

**交付文件**
```
core/orchestrator.py  core/ratelimit.py  core/db.py（落实现）
scripts/export_heatmap.py
docs/degrade-heatmap.md
tests/test_orchestrator.py  tests/test_resume.py  tests/test_ratelimit.py
```

**验收口径**
- [ ] 批量跑完 50 题
- [ ] 每步推进**只按开局裁决的那一种方式**（`RunPlan`），日志里能看出 `advance_library=<method>` / `advance=stuck` / `advance=failed`；中途**不换招、不重问模型「下一题在哪」**
- [ ] 中断后可断点续跑（SQLite 恢复状态）
- [ ] **`submitted` 状态题续跑时不重复提交**（专项演练：提交后杀进程，再续跑）
- [ ] 限速三档全带随机抖动：动作级 200~600ms、提交级 5~15s、请求级并发 ≤2（免费档）/ ≤3（付费档）用 `asyncio.Semaphore` 包住；`httpx.AsyncClient` 复用连接池，**禁止裸 gather**
- [ ] 留痕目录齐全：`logs/<run_id>/<item_id>/{before.png, after.png, perception.json, solve.json, action.json, verify.json}`，**`solve.json` 含模型原始响应全文**
- [ ] 降级热力图产出，定位最易降级的控件类型

**坑**
- 状态机严格按 T0-2，别在编排层绕过 `require_transition()`。
- `asyncio.Semaphore` 要包住**整个请求生命周期**（含流式读取），否则并发限制形同虚设。

---

### P8 — 网课场景（4.5 人日）

**目标功能**：分集自动推进 + 弹题嵌套中断的压栈/弹栈恢复。

**交付文件**
```
core/tasks.py（落实现栈落盘）
perception/media_probe.py（补 wait_for_ended / read_episode_index 兜底）
act/actuator.py（补 play/pause/seek/next 的媒体阶梯）
act/verifier.py（补四条量化断言）
adapters/mock_exam/selectors_media.yaml（定稿）
ui/routes/tasks.py（补分集进度呈现）
docs/suspend-resume-spec.md      # 栈语义规格（对齐任务书 M5-2）
tests/test_task_stack.py  tests/test_media_flow.py  tests/test_interrupt_resume.py
```

**恢复语义（照抄任务书 M5-2，实现不得偏离）**
- 弹题到来 → **显式暂停媒体** → 压栈 → 处理弹题 → 弹栈 → 回读 `currentTime` 未越界 → 恢复播放
- 栈落盘 SQLite；进程重启后**栈顶优先**
- 被挂起的视频以 `paused` 态重建、**不自动续播**
- **优先级**：`ended` 与弹题同刻到达时 `ended` 优先 —— 该集视为已完成，弹题处理完直接推进下一集，**不恢复播放**

**进度断言量化（Verifier 实现依据）**

| 断言 | 判据 |
|------|------|
| 播放态推进 | 采样窗口 3s 内 `ΔcurrentTime ≥ 1.0s` |
| 暂停态静止 | 采样窗口 3s 内 `ΔcurrentTime ≤ 0.2s` |
| 恢复位置连续 | `|currentTime − 挂起时 currentTime| ≤ 2s` 且 `currentTime < duration` |
| 分集索引变化 | 点击下一集后，索引严格 +1 |

**验收口径**
- [ ] 靶场中连续播完全部分集，自动推进无人工干预
- [ ] 弹题打断时**视频被显式暂停**，处理完成后恢复且位置连续
- [ ] 播放结束事件丢失时，轮询兜底仍能推进下一集
- [ ] `ended` 与弹题同刻到达时按优先级正确推进，不重复播放该集
- [ ] 弹题处理中途杀进程，续跑后**栈顶优先恢复、视频以暂停态重建**
- [ ] 中断后可断点续跑，不重播已完成的集
- [ ] 全部动作留痕，`level_used` 与媒体态切换有记录

**坑**
- 挂起期间若只「不操作」而不显式 pause，视频会继续播，恢复时位置校验必然失败。
- `ended` 与弹题的竞态必须在靶场留一个 `?interrupt_at=end` 用例专测。

---

### P9 — 打包（可选，2.5 人日，单独排期）

**目标功能**：需要分发时才做。

- 复用系统浏览器：`browser_type.launch(channel="msedge")`（或 `"chrome"`），**不打包自带 chromium**
- T0–P8 全部按源码运行（`venv` + `run.bat`），**不引入 PyInstaller**
- **单独排期，不污染主线**

**验收口径**
- [ ] `run.bat` 在干净 Windows 上可启动（含依赖安装提示）
- [ ] 无内置 chromium 依赖，启动走系统浏览器

---

## 5. 任务分配矩阵

| Part | 泳道 A 感知 | 泳道 B 求解 | 泳道 C 执行 | 泳道 D 前端+靶场 | 人日 |
|------|:---:|:---:|:---:|:---:|:---:|
| P0 | 契约(T0-4/5) | 契约(T0-3/6) | 契约(T0-1/2) | 契约(T0-7/8) | 0.5 |
| P1 | — | — | — | **主责** | 2 |
| P2 | **主责** | 评审 | 评审 | 靶场接口配合 | 3 |
| P3 | **主责** | 评审 | — | — | 3.5 |
| P4 | — | **主责** | — | 配置面板对接 | 3.5 |
| P5 | SSE 消费确认 | 引导文案 | — | **主责** | 4 |
| P6 | 媒体动作评审 | 提交校验评审 | **主责** | 详情页对接 | 4.5 |
| P7 | — | — | **主责** | 热力图可视化 | 3.5 |
| P8 | 媒体探针配合 | — | **主责** | 分集进度 UI | 4.5 |
| P9 | — | — | — | **主责** | 2.5 |

**每个 Part 完成的 Definition of Done（全员统一）**
1. 代码合入主干，`ruff` + 类型检查通过
2. 单测覆盖该 Part 的**验收口径每一条**（至少要有一条测试直接对应）
3. 该 Part 验收清单勾选完毕，勾选结果贴进对应事项
4. 若改了 §2 任何签名 → 更新 `docs/CHANGELOG-interface.md` 并通知全员
5. 该 Part 的交付物路径已写入事项描述

---

## 6. 推进节奏

| 波次 | 做什么 | 出口判据 |
|------|--------|---------|
| **W0**（半天） | P0 契约冻结 + 骨架 stub 合入 | §2 全部签名可 import；5 项单测通过 |
| **W1** | P0 完成 → P1 靶场 + P2 感知 起跑；P5 UI 骨架起跑 | 靶场两页可访问；UI 五区空壳可交互 |
| **W2** | P2 收尾 + P3 起跑；P5 接真实 API | MediaProbe 三态正确；视口截图稳定（`full_page` 禁用） |
| **W3** | P3 收尾 + P4 起跑；P5 模型面板可用 | 读题质量门禁 + 开局裁决/收尾确认覆盖；单通道指标骨架产出 |
| **W4** | P4 收尾 → **M2 闸门判定** | 三项指标达标（须真实模型跑出），否则停 |
| **W5** | P6 执行层 | 单题全自动闭环；坐标作答落准、截图差分生效 |
| **W6** | P7 编排 + 留痕 | 50 题跑完；断点续跑不重复提交 |
| **W7** | P8 网课场景 | 分集自动推进；弹题中断-恢复位置连续 |
| **W8+** | P9 打包（可选） | 单独排期 |

**M2 闸门是唯一强制止损点**：单选 Top-1 ≥ 0.85、多选完全匹配 ≥ 0.75、一致率 ≥0.8 的题占比 ≥ 0.80，**三项均须真实模型 Provider 跑出**。不达标 → 停，不进入 P6。

---

## 7. 风险与阻塞点

| # | 风险 | 影响 | 处置 |
|---|------|------|------|
| 1 | **用户未添加模型配置** | M2 闸门无法判定，P6+ 无法启动 | UI 引导卡做到位（P5）；闸门顺延期间只验 MockProvider 全链路 + UI，**不判闸门不推进** |
| 2 | 靶场质量不足 | 后面全部验收失去真值 | P1 交付前逐条核对 6 类坑覆盖 + `traps.md` 完整 |
| 3 | 锚点抽象定晚了 | M5 回头改抽象，返工 2~3 人日 | M0 一次定完**媒体锚点组**，**P2 之后不再改**（v0.2.0：题目侧锚点已整体删除，这个风险面缩小到媒体一条线） |
| 4 | 媒体动作阶梯顺序写反 | 播放永远抛 `NotAllowedError` | 媒体阶梯独立于元素阶梯，代码里显式注释顺序理由 |
| 5 | `submitted` 态被重复提交 | 重复提交，最严重的业务事故 | `TRANSITIONS` 硬约束 + 专项演练（P7 杀进程续跑） |
| 6 | `networkidle` / `full_page` 被误用 | 隐式不稳定、性能塌方 | 加 grep 单测卡死这两个字符串 |
| 7 | 密钥泄漏 | 安全事故 | 全链路 `SecretStr`；`models.yaml` 零敏感字段；UI 保存后清空 `input.value` |
| 8 | P5 UI 开工太晚 | M2 闸门卡在 UI 上 | P5 从 W1 起独立车道，用 mock 数据先行 |
| 9 | 早于契约冻结就动手 | M3/M4 冒出错答与重复提交类缺陷 | P0 半天不可压缩，8 项定义不冻结不开工 |
| 10 | **（v0.2.0 新增）读错题却判不出来** | 模型返回结构完好的 JSON、没有错误信号；少一个负号/漏一个指数/被视口截半行都会算出一个「看起来正常」的错答案，并按坐标点到用户的真实页面上；事后从日志里看不出发生过什么 | **读题质量门禁不得放宽**（`clipped` / `uncertain` 非空就拦、带原因暂停问人）+ 开局裁决 + 收尾确认；`prompts/10-视觉组.md` 的三条纪律（只读不答 / 坐标归一化 / 找不到就留空）不得删改 |
| 11 | **（v0.2.0 新增）把截图差分当成「选中正确」的证据** | 校验通过但选错项，错答流到用户页面上 | 文档与代码注释都必须写明：它只证明「选项区域像素变了」；真正的把关靠读题门禁 |
| 12 | **（v0.2.0 新增）坐标换算口径被改回设备像素** | 每次点击都偏一半，全量失败 | 截图固定 `scale="css"`；换算只有 `norm_box_center()` 一处；注释写明「不除 `devicePixelRatio`」 |
| 13 | **（2026-09-30 新增）推进逻辑被改回「换招」或运行期重问模型** | 一次跳过十几道题，且事后从日志里分不清漏了哪几道 | 开局裁决的 `RunPlan` 是**唯一**推进依据；`page` 观测里**不许**出现「该怎么推进」的字段；`tests/test_run_plan.py` / `test_advance_library.py` 卡住；推不动就停（`advance_failed`） |
| 14 | **（2026-09-30 新增）解题组被塞回图片，或选项又被「打乱」** | 标号错位：模型答 `D`、程序按位置落回 `C`，错答**从输出上看不出来** | 请求 `images` 恒为空；标号逐字等于页面标号；`prompts/20-解题组.md` 的输出契约（含 `confidence`）不得删改；有「呈现顺序 = 页面顺序」单测 |
| 15 | **（2026-09-30 新增）把「回复不可用」当成复算，或把「区域没变」当成没点中** | 要么「不开复算就不能解题」，要么在同一个框里补点 1~3 下（多选上等于取消刚选的项） | 不可用 = 请求失败 → `SAMPLE_RETRY_MAX` 有界重发；空作答不重发；回读三态 `changed` / `weak` / `none`，墨迹落点判 `none` **绝不重点** |

> **v0.2.0 的取舍说明**：第 10 项这条风险是本次重构**换来的**，不是遗留缺陷。
> 0.1.0 用 DOM 读题能在结构层挡住一部分错误（读不出就换通道、`checked` 能直接断言），
> 代价是真实站点上那条路根本走不通。0.2.0 选择「让模型读，但逼它承认不确定」——
> 所以门禁与开局裁决/收尾确认不是附加功能，而是**这套设计成立的前提**。

---

## 8. 交付物清单（可直接建事项）

| 事项名 | 交付物路径 | 负责泳道 | 验收依据 |
|--------|-----------|---------|---------|
| `[P0] 契约与骨架冻结` | `core/{config,states,qid,vid,tasks,models,events,db}.py` | 全员 | P0 验收清单 |
| `[P1] 双靶场` | `mock_site/{quiz,course}.html`、`static/traps.md` | D | P1 验收清单 |
| `[P2] 感知层 视觉+媒体` | `perception/{base,media_probe,pipeline}.py`、`adapters/**` | A | P2 验收清单 |
| `[P3] 视觉读题+单通道` | `perception/vision_probe.py`、`solve/reader.py`、`core/arbiter.py`、`scripts/probe_bench.py` | A | P3 验收清单 |
| `[P4] 求解层+Provider` | `solve/**`、`core/model_registry.py` | B | P4 验收清单 + M2 闸门 |
| `[P5] UI 五区+引导` | `ui/**` | D | P5 验收清单 |
| `[P6] 执行层+dry_run` | `act/**`、`core/trace.py` | C | P6 验收清单 |
| `[P7] 编排+限速+留痕` | `core/{orchestrator,ratelimit}.py`、`docs/degrade-heatmap.md` | C | P7 验收清单 |
| `[P8] 网课中断恢复` | `core/tasks.py`、`docs/suspend-resume-spec.md` | C | P8 验收清单 |
| `[P9] 打包（可选）` | `run.bat`、`requirements.txt` | D | P9 验收清单 |
| `[v0.2.0] 单通道重构` | 见 `docs/CHANGELOG-interface.md` 的 v0.2.0 一节；删 DOM/网络通道与通道选择、题目侧改坐标作答 + 截图差分、配置加载器加固 | 全员 | v0.2.0 节的「影响面与迁移动作」表（**该文件已于 2026-09-29 删除，此行为历史记录**） |
| `[2026-09-30] 推进与置信度收口` | `core/run_plan.py`、`core/advance_library.py`、`core/models.py`（`PageView` / `RunPlan`）、`solve/reader.py`、`solve/solver.py`、`solve/prompts.py`、`act/verifier.py`、`act/actuator.py`、`prompts/{00,10,20}.md` | 全员 | §10 的四条 + P3/P4/P6 更新的验收项 |

---

## 9. 配套动作（流转提醒）

- **归档**：本规划书与任务书一并留在项目资料库，形成统一上下文
- **建事项**：按 §8 建 10 个事项，标题用 `[P?]` 前缀，描述里贴交付物路径 + 该 Part 验收清单
- **关注人**：`[P4] 求解层` 与 `[P5] UI` 两个事项必须加关注人（M2 闸门判定在此闭环）
- **子任务**：`[P6]`/`[P7]`/`[P8]` 建后拆子任务给具体开发，附对应 Part 章节链接
- **接口变更**：任何签名改动同步写 `docs/CHANGELOG-interface.md`，并在例会同步
- **待确认**：M2 闸门阈值（任务书 §4 第 6 项）已给初值，**跑完基线后校准，不得事后才定**

---

## 10. 2026-09-30 收口（推进 / 观测分工 / 置信度 / 回读）

用户对上一版给了**四条实测判词**，本节是这四条落到施工图上的口径。改动都是**定点**的：
不新增 Part、不重排 P0…P9，只把「谁做决定」「交接什么」这两件事钉死。

### 10.1 「下一题处理仍然存在严重问题 → 开局要有一套判断逻辑，判定好后后续全部按它执行」

**新契约**：`core/run_plan.py::derive_plan(batch, *, batch_size=0) -> RunPlan`。
开局读图那**一次**就把四件事定死：

| 定死的东西 | 落在哪 |
|---|---|
| 推进方式（**恰好一种**） | `RunPlan.method ∈ {card, click, scroll, swipe}`，按 `advance_library.PLAN_PREFERENCE = (CARD, CLICK, SCROLL, SWIPE)` 裁决 |
| 推进几何 | `control_box`（CLICK）或 `card_origin` / `card_step` / `card_anchor`（CARD） |
| 提交范围 | `submit_scope ∈ {PAPER, QUESTION}`（观测没给就按结构证据推断，默认往「整卷」靠） |
| 总题数 | `RunPlan.total`（`None` = 不知道，`reached_total()` 恒假） |

**运行期纪律**：`Orchestrator._plan_run()` 只跑一次并把结果放进 `self._plan`；
`_advance()` → `_run_advance_strategy(plan, …)` 只按 `plan.method` 执行。
**禁止**：每题重问模型「下一题在哪」、「点击 → 滚动 → 滑动」的换招阶梯（已整条删除）、
「滚到顶重读一次」、因为「这一屏没有提交按钮」中途暂停（提交范围开局已定死）。

**推不动就停**（`_settle_end`）：请视觉组确认「是不是全部完成了」（看同一份固定格式里的
`page.completed`）→ 只有 `all_done` 才干净收工，否则 `ErrorCode.ADVANCE_FAILED` 停下等人。
推进后读到的题号与方案预期不符（`_expected_number_ok`）同样按 `advance_failed` 停下。

**为什么**：滚动推进曾一路滚到上限（0.8 屏 × 6 = **4.8 屏**），真机题号从第 3 题
**跳到第 16 题**；真机 `logs/10ca5ee8c89e` 里 44 题的卷子做到第 16 题，因「这一屏没有
提交按钮」停在 `vision_no_submit_box`。两处都不是「读不到」，而是**运行期还在做判断**。
判定权收进开局一次之后，事后从日志里一眼能看出「它打算怎么走」与「停在第几题」。
答题卡落点是纯算术（`grid_geometry` / `card_cell_box`，网格**按行换行**、容差
`CARD_GRID_TOLERANCE = 0.10`），算不出来返回 `None` → **绝不点**（宁可停下）。

### 10.2 「视觉组只解读照片、翻译成固定格式；解题组严禁拿到非文本输入」

**视觉组只做观测**：`prompts/00-共享契约.md` + `prompts/10-视觉组.md` →
`solve/reader.READ_SYSTEM_PROMPT`（**唯一的视觉提示词**）。一次回两块：

```json
{"page": {"progress": …, "total": …, "current": …, "next_control": {"box": …, "label": …},
          "card": {"box": …, "cols": …, "rows": …, "current_box": …, "next_box": …},
          "submit": {"box": …, "scope": …}, "completed": "all_done|not_done|unknown",
          "scrolling": …, "reason": …},
 "questions": [ … ], "more_below": …, "note": …}
```

`page` 里**只报告「有什么」**，不报告「该怎么办」——「该怎么办」由 `derive_plan()` 裁决。

**解题组只吃文本**：`prompts/20-解题组.md` / `solve/solver.py`。请求里 `images` 恒为空；
选项标号就是**页面标号**（可能不连续，如 A、B、D），**不再打乱选项**；输出多一个
`confidence`（空作答写 0，不许省略）。交接物 = 那一份固定格式本身。

**为什么**：读题时代「打乱选项」把标号与页面的对应关系错位了一次又一次 ——
模型答 `D`、程序按位置映射落回 `C`，而错误**从输出上看不出来**（真机
`logs/10ca5ee8c89e/.../solve.json` 现场；视觉组给的非连续标号 `A, B, D` 更会把位置字母
彻底打乱）。不复算时打乱也没有任何收益：抗位置偏置靠「同一题看到不同排列再比一致性」，
而默认路径只有一次采样。自指检测因此降级为**风险标注**（`allows_shuffle`，只进留痕）。

### 10.3 「不使用复算则不存在置信度系统；不开启复算时存在模型无法解题的情况」

**自报置信度**：`solve/prompts.py::parse_confidence()` 认 `0.8` / `"0.8"` / `"80%"`，
**越界夹到** `[0,1]`（夹到 1.0 只是不额外触发复核，夹到 0.0 只会多停一次）。

| 场景 | `confidence` 取哪个数 |
|---|---|
| **不复算**（`sample_n == 1`）且模型报了 | 自报值；低于 `guards.confidence_review_min`（默认 0.5）→ 标 `⚠复核`（理由「自报置信度低于门限」）并停下请人复核 |
| 不复算且模型**没报** | 回到一致率（单样本恒 **1.0**，**不新增暂停**） |
| **开复算**（`sample_n > 1`） | 多数票占比；**自报值不参与** |
| **显式空作答** | `0`（这是**模型的结论**，不重发） |

**两种「没有答案」必须分清**（`solve/prompts.py::parse_answer_payload`）：

- **不可用**（无 content / 解析不出 / 标号越界 / provider 报错）= **请求失败** →
  `solve/solver.SAMPLE_RETRY_MAX = 2` 的**有界重发**（每条采样项最多 3 次请求）；**不是复算**；
- **显式空作答** = 模型的结论 → 不重发，按 `empty_answer` 停下问人。

`recalculated` 字段仍表示「**是否开了复算**」，与重发无关。

**为什么**：旧口径下「不可用」被当成「再多算几次」，于是**不开复算时模型干脆没法解题**；
而单样本的一致率恒为 1.0，那个 1 里没有信息 —— 模型自报的把握才是唯一携带信息的信号。

### 10.4 「动作回读不一致判断逻辑有问题：点击正确却判断错误，导致胡乱操作」

**三态判定**（`act/verifier.py`，唯一定义点是 `region_change_state(mad)`）：

| `mad` | 状态 | 含义 |
|---|---|---|
| `≥ REGION_CHANGE_MIN_MAD`（2.0） | `changed` | 明显变了（靶场选中态实测 14.3） |
| `≥ REGION_CHANGE_WEAK_MAD`（0.05） | `weak` | 确实动过，但很轻（1px 描边 / 小圆点） |
| `< 0.05` | `none` | 一个像素都没动（PNG 无损截图噪声地板实测 0.00） |

`verify_region_changed` 的 `ok = state in {"changed", "weak"}`，
`actual` 形如 `region_mad=0.31 state=weak`。

**`act/actuator.py::select_option` 的处置表（按落点分）**：

| 落点 | 判 `none` 时怎么做 |
|---|---|
| `ink_centroid`（模型给出的墨迹质心） | **绝不重点**：`ok=True` 收工，`readback` 记 `no_change_on_ink:region_mad=… aim=…` |
| 几何兜底候选点 | 换下一个候选点（`candidate_points`，上限 `click_replay_max + 1`；**换点前先重测**） |
| 全部候选都 `none` | `_exhausted`：`ok=False` + 截图 + `pause` |

**为什么**：真实站点上的「已选中」常常只是 1px 描边或一个小圆点，整块区域 `region_mad`
只有 0.1~1.5。旧实现判成「没变」后会在**同一个框里**再点 1~3 个候选点：多选上那是把刚选好的
勾**取消**，框略偏时还会点到别处 —— 真机上 `region_mad=0.00` 点在行尾空白、
`region_mad=0.06` 点了被禁用的交卷按钮，都是这条误判链的产物。

### 10.5 与 Part 的对应

| Part | 本节带来的改动 |
|---|---|
| P3 | 交付 `core/run_plan.py` / `core/advance_library.py`；`solve/reader.py` 只剩 `READ_SYSTEM_PROMPT` 一个提示词；验收项改为「开局裁决产出唯一一份 `RunPlan`」「推不动就停」 |
| P4 | 解题组只吃文本、不打乱、自报 `confidence`；验收项按 §10.3 的三场景 + 重发规则改 |
| P6 | 回读三态与按落点处置写进验收与坑 |
| P7 | 每步推进只按 `RunPlan`，日志能看出 `advance_library=<method>` / `advance=stuck` / `advance=failed` |
| 契约冻结 | 「坐标只乘一次 / `scale="css"` / 禁用 `full_page` / 禁用 DOM·XHR 读题 / `gate_read_result` 不许绕过 / `submitted` 只回读不重提 / `qid` 不含答案 / 密钥只进 OS 凭据管理器 / 失败一律「暂停 + 截图 + 留痕」绝不静默跳过」**一字未改** |

**同一批里顺手补掉的四个真 bug**（都由测试逼出来）：

- `_plan_run` 曾经丢掉开局那一次读图的**首屏第 1 题**（`_read_question_by_vision` 只把其余题入队、
  第一道是返回值）：主循环先取队列 → 「一屏多题」时首题静默跳过、单题屏被读第二遍。
  现在 `_plan_run` 把它 `insert(0, …)` 放回队首（与 `_scroll_reveal_next` 既有约定一致）。
- 整卷提交**确认生效**后，`_submit_paper_once` 调 `_settle_paper_batch()` 把这一轮其余
  `applied` 的题按 `applied → submitted → verified` 一并推终态 —— 否则它们不是终态，
  续跑会**重做**（多选题上重新点选项 = 取消已选）。提交失败/暂停时**不**推。
- `_last_batch` / `_last_size` / `_last_submit` 改在**解析成功**时就记（原先记在「至少一题过门禁」后）：
  「只有 `page` 观测、没有完整题目」的开局屏不再把观测整份丢掉。
- 提交框新增第三个来源 `_last_submit`（最近一次读图那屏的框），按范围排序
  `PAPER → [方案, 收尾, 最近]`、`QUESTION → [方案, 最近, 收尾]`；**框与尺寸永远同源**。

---

**编制**：Rapid Prototyper
**状态**：可开工。P0 优先，P1/P5 可即刻并行起跑。
**下一步**：确认 4 条泳道人员 → 建 10 个事项 → 开 P0 半天冻结会
