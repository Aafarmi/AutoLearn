# AutoLearn 实施规划书（Rapid Build Plan）

**版本**：v1.0
**编制日期**：2026-09-25
**依据文档**：《AutoLearn 项目任务书》v1.0
**文档性质**：任务书 → 施工图。把 WBS 拆成**可并行认领的工作包（Part）**，为每个 Part 钉死**功能边界、接口签名、文件命名、验收口径**。
**读者**：全体开发 + 测试。开工前请先读 §2（契约）与 §3（命名），再查自己的 Part。

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
| **P2** | DOM + 媒体感知 | M0-3/4/5/6/7 | P0、P1 | 3 | P3、P5 |
| **P3** | 网络通道 + 视觉兜底 + 仲裁 | M1 | P2 | 3.5 | P4 骨架、P5 |
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
P3 网络视觉                    ██████
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
> **P5 必须最早开工**：它是唯一全程独立车道，且 M2 闸门卡在 UI 可用性上（无配置引导、通道优先级切换都在 UI）。

### 1.3 每个 Part 的一句话职责

| Part | 一句话 |
|------|--------|
| P0 | 把 8 项定义**变成代码常量和枚举**，让后面所有人不用猜口径 |
| P1 | 造出两个「可控的假网站」，让自动化有靶子可打，且每题知道正确答案 |
| P2 | 让系统**读懂页面**：题干、选项、题型，以及视频的播放三态 |
| P3 | 读不到 DOM 时的备胎：抓 XHR、裁图给模型、并决定信谁 |
| P4 | 让系统**会做题**：调用模型、多次采样投票、按置信度升级、缓存结果 |
| P5 | 让人能**看见并干预**：配置模型、切通道、看进度、确认/否决、看日志 |
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
class ProbeOrder(StrEnum):  DOM_FIRST = "dom-first";  VISION_FIRST = "vision-first"
class ProbeMode(StrEnum):   AUTO = "auto";  DOM_ONLY = "dom-only";  VISION_ONLY = "vision-only"
class ProbeName(StrEnum):   NET = "net";  DOM = "dom";  VISION = "vision"
class TierUsed(StrEnum):    TIER1 = "tier1";  TIER2 = "tier2";  MOCK = "mock"

# core/states.py
class QuestionState(StrEnum):
    PENDING; PERCEIVED; SOLVED; PENDING_CONFIRM; APPLIED
    SUBMITTED; VERIFIED; FAILED; SKIPPED            # T0-2 九态
class MediaState(StrEnum):
    IDLE; PLAYING; PAUSED; INTERRUPTED; RESUMED; ENDED   # M5-4

# core/tasks.py
class TaskType(StrEnum):    VIDEO = "video";  QUIZ = "quiz"
```

### 2.2 Python 接口总表

#### core/config.py `[P0]`

```python
class GuardThresholds(BaseModel):                    # T0-3 / T0-5 / T0-6
    agreement_accept: float = 0.8                    # ≥ 则直接采用，不调 Tier2
    tier2_min_votes: int = 3                         # Tier2 必须投票 ≥3 次
    single_top1_min: float = 0.85                    # M2 闸门
    multi_exact_min: float = 0.75                    # M2 闸门
    valid_ratio_min: float = 0.80                    # 闸门：一致率≥0.8 的题占比
    click_replay_max: int = 3                        # 回读不一致重放上限
    click_replay_gap_ms: tuple[int, int] = (200, 400)

class RateLimits(BaseModel):                         # M4-2
    click_gap_ms: tuple[int, int] = (200, 600)
    submit_gap_s: tuple[int, int] = (5, 15)
    llm_concurrency_free: int = 2
    llm_concurrency_paid: int = 3

class RunConfig(BaseSettings):
    probe_order: ProbeOrder = ProbeOrder.DOM_FIRST
    probe_mode: ProbeMode   = ProbeMode.AUTO
    dry_run: bool           = False
    sample_n: int           = 5                      # 采样数 n ≥ 5
    task_sequence: list[TaskType] = [TaskType.QUIZ]
    model_profile_id: str | None = None
    guards: GuardThresholds = GuardThresholds()
    rate: RateLimits        = RateLimits()
    storage_state_path: Path
    @property
    def is_model_ready(self) -> bool: ...            # 决定「模型优先」是否可点
    @property
    def llm_concurrency(self) -> int: ...

def load_run_config() -> RunConfig
def save_run_config(cfg: RunConfig) -> None
def active_probe_chain(cfg: RunConfig) -> list[ProbeName]   # 按 probe_order 展开探针顺序
```

#### core/models.py `[P0]`

```python
class Option(BaseModel):        index: int; label: str; text: str; raw: str
class Question(BaseModel):      qid: str; stem: str; stem_hash: str; qtype: QType
                                options: list[Option]; source: ProbeName; channel_trace: list[str]
class Answer(BaseModel):        qid: str; chosen_labels: list[str]; chosen_texts: list[str]
                                confidence: float; tier_used: TierUsed; review_flag: bool
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
class VoteResult(BaseModel):    chosen_labels: list[str]; majority_ratio: float
                                distribution: dict[str, int]; n_samples: int
                                samples: list[SampleRecord]
class VerifyResult(BaseModel):  ok: bool; kind: VerifyKind; expected: str; actual: str
                                level_used: ActLevel | None; screenshot_ref: str | None
class TaskItem(BaseModel):      item_id: str; type: TaskType; qid: str | None; vid: str | None
                                state: QuestionState | MediaState; attempts: int
                                suspended: bool = False; created_at: datetime; updated_at: datetime
class CapabilityReport(BaseModel):                          # M2-6
                                auth_ok: bool; tier1_ok: bool; tier2_ok: bool
                                supports_vision: bool; supports_structured_output: bool
                                image_payload: str | None                    # "base64" | "url" | None
                                max_qps: float; latency_ms: int
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
                                tier1_model: str; tier2_model: str | None
                                temperature: float; timeout_s: int; concurrency: int
                                api_key_ref: str; capabilities: CapabilityReport | None
                                enabled: bool = True; order: int = 0
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

# core/orchestrator.py — M4-1
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

# core/events.py — SSE 事件常量
class Event:  RUN_STARTED="run.started"  RUN_PAUSED="run.paused"  RUN_RESUMED="run.resumed"
              RUN_FINISHED="run.finished"  RUN_ERROR="run.error"
              TASK_CREATED="task.created"  TASK_UPDATED="task.updated"
              TASK_STATE_CHANGED="task.state_changed"  TASK_NEEDS_CONFIRM="task.needs_confirm"
              PERCEPTION_DONE="perception.done"
              SOLVE_VOTE="solve.vote"  SOLVE_DONE="solve.done"
              SOLVE_ESCALATED="solve.escalated"  SOLVE_REVIEW_REQUIRED="solve.review_required"
              ACT_LEVEL_USED="act.level_used"  ACT_READBACK_MISMATCH="act.readback_mismatch"
              ACT_SUBMIT_TIMEOUT="act.submit_timeout"
              MEDIA_STATE_CHANGED="media.state_changed"  MEDIA_INTERRUPT_DETECTED="media.interrupt_detected"
              STACK_PUSHED="stack.pushed"  STACK_POPPED="stack.popped"  LOG_LINE="log.line"

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

# perception/dom_probe.py — M0-4 / M0-5
class DomProbe(BaseProbe):
    async def probe(...) -> PerceptionResult
async def ensure_ready(page: Page, locator: Locator) -> None          # 三重就绪断言
def is_ready_visible(locator: Locator) -> bool                        # visible 且文本长度 > 0
async def scroll_into_view(locator: Locator) -> None
async def canvas_ink_ratio(page: Page, locator: Locator) -> float     # 非背景像素占比
async def read_stem(page, adapter) -> str
async def read_options(page, adapter) -> list[Option]
async def read_qtype(page, adapter) -> QType

# perception/media_probe.py — M0-7 / M5-3 / M5-4
class MediaProbe(BaseProbe):
    async def attach(self, page: Page) -> None                        # 注册 timeupdate/ended/pause 监听
async def read_video_state(page, adapter) -> VideoState
async def wait_for_playback(page, adapter, window_s: float = 3.0, min_delta: float = 1.0) -> bool
async def wait_for_ended(page, adapter, timeout_s: float, poll_s: float = 0.5) -> bool  # 事件 + 轮询兜底
async def wait_for_interrupt(page, adapter, timeout_s: float) -> bool                   # MutationObserver + 轮询双保险
async def read_episode_index(page, adapter) -> tuple[int, int]

# perception/net_probe.py — M1-1（仅被动监听）
class NetProbe(BaseProbe):
    async def attach(self, page: Page) -> None
    async def wait_for_question_payload(self, url_keyword: str, timeout_s: float) -> dict | None
    def parse_xhr_question(self, payload: dict) -> Question
    def snapshot(self) -> list[NetworkRecord]

# perception/vision_probe.py — M1-2
class VisionProbe(BaseProbe):
    async def probe(...) -> PerceptionResult
    async def crop_question(self, page: Page, adapter) -> bytes       # bounding_box 裁切，禁 full_page
    async def shot_element(self, locator: Locator) -> bytes           # iframe 用它绕开坐标换算

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

# solve/voting.py — M2-2
class VotingEngine:
    def build_index_map(self, shuffled: list[str], original: list[str]) -> dict[int, int]
    def vote(self, samples: list[LLMResponse], qtype: QType) -> VoteResult   # 按内容比对，不按字母
    @staticmethod
    def majority_ratio(vr: VoteResult) -> float

# solve/solver.py — M2-3 / T0-3 / T0-4
SELF_REF_PATTERN: re.Pattern                      # 自指选项白名单正则
def is_self_referential(text: str) -> bool
def should_escalate_to_tier2(question: Question, first: VoteResult, cfg: RunConfig) -> bool
class Solver:
    def __init__(self, providers: list[LLMProvider], cache: SolveCache, cfg: RunConfig) -> None
    async def solve(self, question: Question) -> Answer
    def build_sampling_batch(self, q: Question) -> list[list[str]]   # 命中自指则不打乱
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
class ReadbackProbe(NamedTuple):  name: str; attr: str | None; pattern: str | None
READBACK_PROBES: tuple[ReadbackProbe, ...]        # checked → aria-checked → aria-pressed → class
async def read_state(locator: Locator) -> str
def is_selected(state: str) -> bool

# act/actuator.py — M3-2 / M3-3
class ActionKind(StrEnum):  CLICK="click"; SELECT_OPTION="select_option"; SUBMIT="submit"
                            PLAY_MEDIA="play_media"; PAUSE_MEDIA="pause_media"
                            SEEK_MEDIA="seek_media"; NEXT_EPISODE="next_episode"
class ActLevel(StrEnum):    L1_LOCATOR="l1_locator"; L2_FORCE="l2_force"
                            L3_SCROLL="l3_scroll"; L4_FOCUS_KEYS="l4_focus_keys"
                            L5_BBOX="l5_bbox"; L6_VISION_XY="l6_vision_xy"
LEVELS: tuple[ActLevel, ...]                       # 六级阶梯
MEDIA_LEVELS: tuple[ActLevel, ...]                 # 媒体动作：可信手势优先，evaluate 靠后
class Actuator:
    def __init__(self, page: Page, cfg: RunConfig) -> None
    async def select_option(self, locator: Locator, qtype: QType) -> ActionResult
    async def click(self, locator: Locator) -> ActionResult
    async def submit(self, locator: Locator) -> ActionResult        # 不重试、不重放
    async def play_media(self, adapter) -> ActionResult
    async def pause_media(self, adapter) -> ActionResult
    async def seek_media(self, adapter, seconds: float) -> ActionResult
    async def next_episode(self, adapter) -> ActionResult
    async def _attempt(self, level: ActLevel, target: Locator) -> ActionResult
    async def _restore_scroll(self) -> None

# act/verifier.py — M3-5 / M5 量化断言
class VerifyKind(StrEnum):  READBACK="readback"; MEDIA_PAUSED="media_paused"
                            MEDIA_PROGRESS="media_progress"; MEDIA_RESUME="media_resume"
                            EPISODE_INDEX="episode_index"; SUBMIT_RESULT="submit_result"
                            SCREENSHOT_DIFF="screenshot_diff"
class Verifier:
    def __init__(self, page: Page, cfg: RunConfig) -> None
    async def verify_readback(self, locator, expected: str) -> VerifyResult
    async def verify_submit(self, adapter) -> VerifyResult
    async def verify_playing(self, page, adapter, window_s=3.0, min_delta=1.0) -> VerifyResult
    async def verify_paused(self, page, adapter, window_s=3.0, max_delta=0.2) -> VerifyResult
    async def verify_resume_continuous(self, page, adapter, suspend_time: float) -> VerifyResult
    async def verify_episode_advance(self, page, adapter, before_index: int) -> VerifyResult
    def should_escalate(self, result: VerifyResult) -> bool
```

#### adapters/*

```python
# adapters/base.py — M0-3（抽象必须在 M0 一次定完）
class AnchorSet(BaseModel):       stem: str; options: str; submit: str; result: str; qtype: str
class MediaAnchorSet(BaseModel):  video: str; episode_list: str; next: str; interrupt: str
                                  play_button: str; progress: str
class BaseAdapter(ABC):
    site: str
    anchors: AnchorSet
    media_anchors: MediaAnchorSet
    @classmethod
    def from_yaml(cls, path: Path) -> "BaseAdapter"
    async def matches(self, page: Page) -> bool
    def locator(self, page: Page, anchor: str, **kwargs) -> Locator
    def media_locator(self, page: Page, anchor: str, **kwargs) -> Locator

# adapters/mock_exam/adapter.py
class MockExamAdapter(BaseAdapter):   site = "mock_exam"
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
| GET | `/api/run/config` | — | `RunConfigOut` | 含 `probe_order`、`model_ready` |
| PUT | `/api/run/config` | `RunConfigIn` | `RunConfigOut` | 运行前设定；运行中只读（返回 409） |
| POST | `/api/run/start` | `{task_sequence?}` | `{run_id}` | `model_ready=false` 且 `probe_order=vision_first` → 400 + 引导码 |
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
| `vision_unsupported` | 模型不支持图片 | 「模型优先」置灰 | `GUIDE_EMPTY` |
| `structured_unsupported` | 不支持 JSON Schema | 降级为文本解析 | — |
| `readback_mismatch` | 回读不一致 | 详情页高亮 | — |
| `submit_timeout` | 提交后无结果 | 暂停 + 留档 | — |
| `stack_restored` | 续跑恢复栈 | 日志提示 | — |

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
├─ docs/                           # 本规划书、任务书、接口变更记录
│   ├─ AutoLearn-项目任务书.md
│   ├─ AutoLearn-实施规划书.md
│   └─ CHANGELOG-interface.md      # 接口变更流水（改签名必写）
├─ core/
│   ├─ config.py  models.py  qid.py  vid.py  states.py  tasks.py
│   ├─ model_registry.py  arbiter.py  orchestrator.py
│   ├─ events.py  trace.py  ratelimit.py
│   └─ db.py                       # SQLite 连接与建表
├─ perception/
│   ├─ base.py  dom_probe.py  media_probe.py  net_probe.py
│   ├─ vision_probe.py  pipeline.py
├─ solve/
│   ├─ solver.py  voting.py  cache.py  prompts.py
│   └─ providers/{base.py, openai_compat.py, mock.py, factory.py}
├─ act/
│   ├─ actuator.py  readback.py  verifier.py
├─ adapters/
│   ├─ base.py
│   └─ mock_exam/
│       ├─ adapter.py
│       ├─ selectors.yaml          # 题目五锚点
│       └─ selectors_media.yaml    # 媒体锚点组
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
│   ├─ probe_bench.py              # M1-4a 三通道量化
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
| 适配器锚点 | `adapters/<site>/selectors.yaml` | 媒体用 `selectors_media.yaml` |
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
| 枚举值 | 字符串小写连字符，与 UI 文案解耦：`"dom-first"` |

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
core/config.py       # RunConfig / GuardThresholds / RateLimits / ProbeOrder / ProbeMode
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

### P2 — DOM + 媒体感知（3 人日）

**目标功能**：读懂页面。题干 / 选项 / 题型结构化读出；视频播放三态 + 弹题打断探测。

**交付文件**
```
perception/base.py  perception/dom_probe.py  perception/media_probe.py  perception/pipeline.py
adapters/base.py  adapters/mock_exam/adapter.py
adapters/mock_exam/selectors.yaml  adapters/mock_exam/selectors_media.yaml
core/qid.py（补实现）  core/vid.py（补实现）
tests/test_dom_probe.py  tests/test_media_probe.py  tests/test_pipeline.py
```

**接口**：见 §2.2 `perception/*`、`adapters/*`

**验收口径**
- [ ] 五锚点 + 媒体锚点组抽象完成，靶场适配器由 `selectors.yaml` 驱动，**`dom_probe` 内零硬编码靶场 DOM**
- [ ] 三重就绪断言生效：visible 且文本长度 > 0；懒加载先 `scroll_into_view_if_needed()`；Canvas 非背景像素占比 > 5%
- [ ] **全局禁用 `networkidle`**（加 lint 规则或 grep 单测卡住）
- [ ] DOM 探针正确读出题干、选项、题型
- [ ] 媒体探针能正确读出播放 / 暂停 / 结束三态
- [ ] `wait_for_interrupt()` 在弹题出现 1s 内触发，且**不误报**（无弹题场景连续 60s 零触发）
- [ ] 50 题全部被 DOM 通道正确结构化读出
- [ ] **零模型调用**（用 mock patch 断言未构造任何 Provider）

**坑**
- 媒体态必须走 `paused`/`ended`/`currentTime`/`duration`，**不要用元素可见性代替**。
- 弹题不会改 `paused`，所以 `wait_for_interrupt` 得用 MutationObserver + 定时轮询双保险。
- P2 交付时 `adapters/base.py` 的锚点集合要一次定完，**M5 不再回头改抽象**。

---

### P3 — 网络通道 + 视觉兜底 + 仲裁（3.5 人日）

**目标功能**：DOM 读不到时的两条退路，以及「信谁」的裁决。

**交付文件**
```
perception/net_probe.py  perception/vision_probe.py
core/arbiter.py
scripts/probe_bench.py          # M1-4a 三通道量化骨架
docs/channel-bench.md           # 三通道耗时/token 对比结论
tests/test_net_probe.py  tests/test_arbiter.py  tests/test_vision_probe.py
```

**验收口径**
- [ ] NetProbe **仅被动监听**（不主动构造请求），产出 XHR 题型的结构化数据
- [ ] 视觉兜底：只用 `bounding_box()` 裁题目区域、**全局禁 `full_page=True`**；截图前先 `scroll_into_view_if_needed()`；iframe 题用 `element.screenshot()`
- [ ] 仲裁四分支全覆盖：一致→用 DOM；不一致→DOM + 告警标记人工复核；DOM 失败→走视觉；双失败→**暂停留档，绝不静默跳过**
- [ ] `probe_bench.py` 产出结构化记录（耗时 + token），**零密钥可验收**
- [ ] 用户添加模型配置后补齐真实 token 数据（可回填至 P4 期间）
- [ ] class 名混淆时按配置降级，不中断
- [ ] 「模型优先」链路由 `active_probe_chain()` 驱动，顺序正确

**坑**
- 跨域 iframe 的兜底只能验「触发路径」，识别能力要等用户配了支持视觉的模型才能判。
- 仲裁的「双失败」分支最容易被写成静默跳过 —— 必须显式暂停 + 留档。

---

### P4 — 求解层 + 模型 Provider（3.5 人日）

**目标功能**：让系统会做题。多厂商统一接入、多次采样投票、按置信度升级 Tier2、命中自指不打乱、精确缓存。

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
- [ ] 投票**按内容比对**（有「打乱后同一内容应判为一致」的测试用例），多选题按 `frozenset` 整体集合记票
- [ ] `n ≥ 5`，用多数票占比而非全体一致；每题记录采样明细、置信度、决策
- [ ] Tier 路由符合 T0-3：`agreement ≥ 0.8` 直接采用；`< 0.8` 交 Tier2；带图 / Canvas / 题干截断直接 Tier2；**Tier2 必须投票 ≥3 次**
- [ ] Tier1 与 Tier2 多数解不同 → 标 `⚠复核` 且必停；两者均低于门限 → `⚠复核` + 留截图 + 暂停
- [ ] 自指选项**禁止打乱**，退化为多次低温度采样（有测试用例）
- [ ] `MockProvider` 读靶场 `data-answer`，可按注入错误率验证投票逻辑正确性
- [ ] 精确缓存命中 qid 直接返回并计入采样明细（标记 `tier_used=mock/cache` 语义正确）
- [ ] `test_connection()` 实测项齐全：鉴权 / Tier1 模型名 / Tier2 模型名 / `supports_vision` / `supports_structured_output` / `image_payload` / 真实 QPS，**不留手填字段**
- [ ] 密钥走 `api_key_ref`，`models.yaml` 无敏感字段

**坑**
- `MockProvider` 直接读答案，**结果不得作为 M2 闸门依据**，只能验投票逻辑。
- 采样打乱后要建「打乱序号 ↔ 原始序号」双向映射，否则计票会串位。
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
| ① | `zone-run-config` | 通道优先级单选（DOM 优先默认 / 模型优先置灰+引导）、dry_run 开关、任务序列、启动/暂停/停止 |
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
- [ ] 通道优先级可切换；无模型配置时「模型优先」置灰并给出引导提示
- [ ] W1 起可用 mock 数据独立开发，不被后端阻塞

**坑**
- 「模型优先」置灰是**引导**不是静默失败 —— 必须给下一步动作。
- 排序即降级链，顺序变了 `active_chain()` 必须立刻反映。

---

### P6 — 执行层 + dry_run（4.5 人日）

**目标功能**：真的动手且能自证。状态读取适配、六级升级阶梯、媒体动作（顺序相反）、提交不重试、回读重放。

**交付文件**
```
act/readback.py  act/actuator.py  act/verifier.py
core/trace.py（截图/JSON 落盘部分）
tests/test_readback.py  tests/test_actuator.py  tests/test_verifier.py  tests/test_dry_run.py
```

**验收口径**
- [ ] 单题全自动闭环跑通（感知 → 求解 → 执行 → 回读 → 提交 → 结果确认）
- [ ] 六级阶梯逐级生效，`level_used` 正确记录并可导出
- [ ] `read_state()` 按 `checked → aria-checked → aria-pressed → class` 依次探测，**临点前一刻读**（TOCTOU），逻辑集中在 `readback.py`，业务代码零散落
- [ ] 回读不一致：重放 click ≤3 次、间隔 200~400ms 随机 → 仍不一致上移一级阶梯 → 到顶暂停 + 截图 + 记 `failed`；**禁用 `assert`，禁止静默继续**
- [ ] 媒体动作阶梯**顺序与元素点击相反**：优先真实点击播放器区域或空格键，`evaluate("video.play()")` 排后面；启动参数含 `--autoplay-policy=no-user-gesture-required`
- [ ] 提交动作**不重试、不重放**，超时暂停等人
- [ ] 题目身份重校验：`stem_hash` 不一致 → 拒绝执行、回「待复算」
- [ ] `dry_run=True`：题目执行到「选项已选好」即停；**视频停在 `PlayMedia` 前**，只校验播放器可定位
- [ ] 失败题留截图 + 暂停，不静默跳过

**坑**
- `evaluate("video.play()")` 属不可信手势，会被抛 `NotAllowedError`，所以媒体动作顺序必须反过来。
- 坐标级降级要 `css = image / devicePixelRatio` 换算，别忘。

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
| **W1** | P0 完成 → P1 靶场 + P2 感知 起跑；P5 UI 骨架起跑 | 靶场两页可访问；UI 五区空壳可切通道 |
| **W2** | P2 收尾 + P3 起跑；P5 接真实 API | 50 题 DOM 读出；MediaProbe 三态正确 |
| **W3** | P3 收尾 + P4 起跑；P5 模型面板可用 | 仲裁四分支覆盖；三通道指标骨架产出 |
| **W4** | P4 收尾 → **M2 闸门判定** | 三项指标达标（须真实模型跑出），否则停 |
| **W5** | P6 执行层 | 单题全自动闭环；六级阶梯生效 |
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
| 3 | 锚点抽象定晚了 | M5 回头改抽象，返工 2~3 人日 | M0 一次定完五锚点 + 媒体锚点组，**P2 之后不再改** |
| 4 | 媒体动作阶梯顺序写反 | 播放永远抛 `NotAllowedError` | 媒体阶梯独立于元素阶梯，代码里显式注释顺序理由 |
| 5 | `submitted` 态被重复提交 | 重复提交，最严重的业务事故 | `TRANSITIONS` 硬约束 + 专项演练（P7 杀进程续跑） |
| 6 | `networkidle` / `full_page` 被误用 | 隐式不稳定、性能塌方 | 加 grep 单测卡死这两个字符串 |
| 7 | 密钥泄漏 | 安全事故 | 全链路 `SecretStr`；`models.yaml` 零敏感字段；UI 保存后清空 `input.value` |
| 8 | P5 UI 开工太晚 | M2 闸门卡在 UI 上 | P5 从 W1 起独立车道，用 mock 数据先行 |
| 9 | 早于契约冻结就动手 | M3/M4 冒出错答与重复提交类缺陷 | P0 半天不可压缩，8 项定义不冻结不开工 |

---

## 8. 交付物清单（可直接建事项）

| 事项名 | 交付物路径 | 负责泳道 | 验收依据 |
|--------|-----------|---------|---------|
| `[P0] 契约与骨架冻结` | `core/{config,states,qid,vid,tasks,models,events,db}.py` | 全员 | P0 验收清单 |
| `[P1] 双靶场` | `mock_site/{quiz,course}.html`、`static/traps.md` | D | P1 验收清单 |
| `[P2] 感知层 DOM+媒体` | `perception/{base,dom_probe,media_probe,pipeline}.py`、`adapters/**` | A | P2 验收清单 |
| `[P3] 网络+视觉+仲裁` | `perception/{net_probe,vision_probe}.py`、`core/arbiter.py`、`scripts/probe_bench.py` | A | P3 验收清单 |
| `[P4] 求解层+Provider` | `solve/**`、`core/model_registry.py` | B | P4 验收清单 + M2 闸门 |
| `[P5] UI 五区+引导` | `ui/**` | D | P5 验收清单 |
| `[P6] 执行层+dry_run` | `act/**`、`core/trace.py` | C | P6 验收清单 |
| `[P7] 编排+限速+留痕` | `core/{orchestrator,ratelimit}.py`、`docs/degrade-heatmap.md` | C | P7 验收清单 |
| `[P8] 网课中断恢复` | `core/tasks.py`、`docs/suspend-resume-spec.md` | C | P8 验收清单 |
| `[P9] 打包（可选）` | `run.bat`、`requirements.txt` | D | P9 验收清单 |

---

## 9. 配套动作（流转提醒）

- **归档**：本规划书与任务书一并留在项目资料库，形成统一上下文
- **建事项**：按 §8 建 10 个事项，标题用 `[P?]` 前缀，描述里贴交付物路径 + 该 Part 验收清单
- **关注人**：`[P4] 求解层` 与 `[P5] UI` 两个事项必须加关注人（M2 闸门判定在此闭环）
- **子任务**：`[P6]`/`[P7]`/`[P8]` 建后拆子任务给具体开发，附对应 Part 章节链接
- **接口变更**：任何签名改动同步写 `docs/CHANGELOG-interface.md`，并在例会同步
- **待确认**：M2 闸门阈值（任务书 §4 第 6 项）已给初值，**跑完基线后校准，不得事后才定**

---

**编制**：Rapid Prototyper
**状态**：可开工。P0 优先，P1/P5 可即刻并行起跑。
**下一步**：确认 4 条泳道人员 → 建 10 个事项 → 开 P0 半天冻结会
