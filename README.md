# AutoLearn

> **只使用模型（视觉）** 的自动化做题/网课助手。
> 它**不导航、不新开标签页**：只附加到**你正在用的**那个页面或窗口上，
> 截图 → 让视觉模型读题 → 让解题模型作答 → 按归一化坐标点击 → 截图差分校验。

当前版本 **0.3.0**（见 `pyproject.toml`）。

---

## 目录

- [它是什么、代价是什么](#它是什么代价是什么)
- [快速开始](#快速开始)
- [项目结构](#项目结构)
- [界面（信息架构）](#界面信息架构)
- [接口地图](#接口地图)
- [测试与质量门槛](#测试与质量门槛)
- [维护指南：改 X 要看 Y](#维护指南改-x-要看-y)
- [硬约束（违反即出事故）](#硬约束违反即出事故)
- [2026-09-30 收口：四条修复](#2026-09-30-收口四条修复)
- [文档体系](#文档体系)

---

## 它是什么、代价是什么

一条题目链路上**只有一条通道**：**截图 → 模型**。程序不解析题目的 DOM、不抓 XHR、
也没有「通道优先级」这种选择。题目侧唯一还读页面结构的地方是**网课媒体态**
（`<video>` 的 `paused` / `ended` / `currentTime` / 分集索引）—— 那读的不是题目。

两条纪律来自真机事故，别改回去：

| 纪律 | 为什么 |
|---|---|
| **用一种方式推进，但落点必须来自最新画面；推不动就停下** | 开局那一次读图裁决出**唯一**一种推进方式（`core/run_plan.py::derive_plan` → `RunPlan`），旧版「点击 → 滚动 → 滑动」的**换招阶梯已整条删除** —— 它正是 `scroll` 模式下照样去找按钮、找不到就继续滚（0.8 屏 × 6 = 4.8 屏）、题号从 3 跳到 16、中间 13 题全被跳过的成因（2026-09-28 实机）。<br>**2026-10-01 修正**：方式可以只用一种，但**落点不能只用开局那一次的**。真机 `logs/9abc4ed8ef60` 里 `advance_card` 连着两次点偏（`expected 27 got 26`、`expected 29 got 28`），根因是拿开局那一屏算好的坐标去点**后来已经变化**的画面。现在每一步都优先用**刚读到的那一屏**给的落点（`page.next_control.box` / `page.card.next_box` / `page.swipe`），开局几何只作兜底；推不动就请视觉组确认一次「是否全部完成」，确认不了按 `advance_failed` **停下等人，绝不静默跳过** |
| **整卷提交只在「视觉组确认全部完成」之后** | 44 题的作业页上做完第 1 题就点了「交卷」（落点是对的，但按钮此时禁用），以 `submit_timeout` 停下。站点若允许交卷，那一按就是 43 题作废。**提交范围现在也在开局定死**（`RunPlan.submit_scope`）：不再随「这一屏读到了什么」逐屏漂移。旧版整卷页面会因为某一屏没看到「交卷」而被判成「每题提交」，真机 `logs/10ca5ee8c89e` 的表现就是**做到一半停在 `vision_no_submit_box`**；现在整卷范围从开局就是 `paper`，**这一屏没有提交按钮不再是异常**（`vision_no_submit_box` 只在「方案与收尾那一屏都拿不到按钮框」时才触发，且提示直接告诉你手动点一次交卷） |

### 代价一：每道题都是一次模型调用

读题**本身就是发图**，没有「锚点站点零调用」那条便宜路。

针对这一点做了三件事：

1. **一屏多题**：视觉组一次读**一屏**，把这一屏里所有完整题目抄进 `questions` 数组，
   同屏其余题直接复用几何，不再截图、不再调模型（`_pending_reads` 队列）；
2. **复算可选**：默认**每题只调一次模型，以第一次答案为准**；需要更稳时在创建任务时
   显式勾「复算」并给次数，程序才按内容投票；
3. **推进不额外读图**：推进方式在**开局那一次读图**里裁决好（`RunPlan`），此后每一步的落点由**紧邻的那一次读图**顺带给出（`page.advance_skill_id` + `next_control.box` / `card.next_box` / `swipe`）—— 不给推进单独多发一次模型调用。拿不到新鲜落点时才退回开局的纯算术（`grid_geometry` + `card_cell_box`）；滚动推进则是「滚一步 → 读一屏 → 确认新题进来了」，读到即入队。

### 代价二：读错题在下游是**不可见的**（所以才有门禁）

读题是全链路唯一「错了也不报错」的环节：模型抄出来的题面少一个负号、漏一个指数、
被视口切掉半行，输出**看起来完全正常**，下游会算出一个看起来正常的错答案，
再照着坐标点到用户的真实页面上。

所以视觉组被要求**自己声明**哪里残缺（`clipped`）或拿不准（`uncertain`）；
只要非空，`gate_read_result()` 就**拦住不下传**，带原因码（`vision_incomplete` /
`vision_uncertain`）暂停问人。一屏三题里坏了一道，只跳过那一道。

**校验的诚实边界**：题目侧校验已经从「能确认选中内容」退化成
「能确认点到了东西」——`verify_region_changed()` 只证明「那块像素变了」。
这是删掉 DOM 通道必然付出的代价，也是门禁必需的原因。
2026-09-29 起它的判据是**三态**（`region_change_state()`）：`changed`（`mad ≥ 2.0`）/
`weak`（`0.05 ≤ mad < 2.0`）/ `none`，而 `ok = state in {"changed", "weak"}` ——
真实站点的选中态常常只是 1px 描边或一个小圆点（整块平均差 0.1~1.5），
只认 2.0 会把**点对了**判成「没点中」，执行层就去框里换点乱点
（2026-09-28 真机 `region_mad=0.00` 点在行尾空白那次事故的修复延伸）。

---

## 快速开始

```bash
# 1. 建虚拟环境（Python 3.13）
python -m venv .venv
.venv/Scripts/activate          # Windows；Linux/macOS 用 source .venv/bin/activate

# 2. 装依赖
pip install -r requirements.txt
pip install -r requirements-dev.txt     # 跑测试 / lint / 类型检查

# 3. 起控制台
.venv/Scripts/python -m uvicorn ui.server:create_app --factory --port 8800
#   → 控制台     http://127.0.0.1:8800
#   → API 文档   http://127.0.0.1:8800/api/docs
```

Playwright 使用**系统浏览器**（默认 `msedge`，用 `AUTOLEARN_BROWSER_CHANNEL=chrome` 切换），
不需要 `playwright install chromium`。

首次使用：**模型库 → 添加模型**（接入点 + 模型名 + API Key，密钥只进系统凭据管理器）
→ **新建任务** → 向导五步（任务名 / 目标 / 两组模型 / 运行选项 / 确认）→ 启动。

> ⚠️ **没有模型就完全起不来**：页面只能由模型读，预检会直接拦下并给引导卡。
> 预检还会检查「模型是否支持视觉」，但只拦**明确知道都不支持**的（没测过的放行）。

### 靶场（仅测试基础设施）

```bash
.venv/Scripts/python scripts/serve_mock.py    # 8899 主站 + 8900 跨域 frame，须常驻
```

靶场**不再是产品入口**（UI 入口已摘），只服务于单测与验收脚本；不选目标时后端会退回它。

### 打包

```bash
run.bat                                  # 一键启动器：建 venv → 装依赖 → 起控制台
.venv/Scripts/python scripts/package.py  # 源码分发包 zip → dist/
```

---

## 项目结构

```
AutoLearn/
├── core/           契约与领域模型（纯逻辑，谁都不依赖）
├── perception/     感知层：视觉探针（唯一题目通道）+ 媒体探针 + 流水线
├── solve/          求解层：Provider / 投票 / 缓存 / 读题 / 技能注册库 / 训练模式
├── skills/         技能正文：题型技能（单选/判断）+ 推进技能（点控件/滑动/答题卡）
├── act/            执行层：按坐标点击、提交、截图差分校验、媒体阶梯、dry_run
├── target/         目标采集层：附加浏览器标签页 / 枚举原生窗口 / 系统级输入
├── adapters/       站点适配：媒体锚点（题目侧已无锚点）
├── ui/             FastAPI 后端（8 路由 + SSE）+ 零构建静态前端 + 任务生命周期
├── mock_site/      测试靶场：22 题 8 类坑 + 6 集网课 + 跨域 frame（**仅供测试**）
├── scripts/        自检 / 验收 / 批量跑 / 打包工具
├── tests/          单测 + 集成测试
├── prompts/        提示词（**唯一真源**，共 5 份）：共享契约 + 视觉组 + 解题组 + 方式库 + 训练总结
├── docs/           任务书 / 实施规划书 / 双模型分工 / 真实站点手册
├── assets/         图标等静态资源
├── state/          运行期落盘：配置草稿、模型库、凭据清单、浏览器 Profile（gitignored）
├── logs/           留痕：**每个任务一个专属目录**（gitignored，见下）
├── run.bat         一键启动器（**必须 ASCII/GBK，不能存成 UTF-8**）
├── launcher.py     启动器实现（venv → 依赖 → 起服务 → 开浏览器）
└── AutoLearn.spec  PyInstaller 打包描述
```

### 依赖方向（单向，反向依赖一律拒绝）

```
core/  ──┬──▶ perception/  solve/  act/  target/  adapters/
         │
   core/orchestrator.py  ──▶  ui/（FastAPI + 静态前端 + runner）
```

- 只有 `ui/assembly.py` 同时认识四层 —— 装配是**唯一**允许跨层的地方；
- `core/` 不得 import `target/`；目标常量在 `core/targets.py`，实现住在 `target/`；
- `core/enums.py` 是全部枚举的唯一定义处，用来切断 core 内部循环导入。

### 运行期落盘（都在 `.gitignore` 里）

| 位置 | 内容 | 删任务会删吗 |
|---|---|---|
| `logs/<run_id>/<item_id>/` | `before.png` / `after.png` / `after_submit.png` / `perception.json` / `vision_read.json` / `solve.json` / `solve_raw.txt` / `action.json` / `verify.json` | ✅ 会 |
| `logs/<run_id>/events.jsonl` | 该运行的操作日志（按行追加） | ✅ 会 |
| `state/autolearn.db` | 任务状态、挂起栈、降级统计、**按 `qid` 全局**存的作答 | 部分（该 run 的条目 / 统计删除；`answer` 是这道题的事实，跨运行共享，不动） |
| `state/models.yaml` / `state/run_config.json` | 模型库（零敏感字段）/ 配置草稿 | ❌ 不删 |
| `state/browser_profile/` | 接管启动浏览器的专用登录态 | ❌ 不删 |

**任务缓存是「每个任务一个专属目录」**：`core.trace.run_dir()` 是它的唯一定义点，
`GET /api/tasks/{run_id}/cache` 可预览（列出条目与体积），
`DELETE /api/tasks/{run_id}` 会把该目录**整个删掉**（截图 + 逐题留痕 + 事件流）。

### 分层职责

| 目录 | 关键文件 | 职责 |
|---|---|---|
| `core/` | `config.py` | `RunConfig` / `GuardThresholds` / `RateLimits`；加字段必须同步 `ui/schemas.py` 与 `ui/routes/run.py::_config_out()`。`load_run_config()` **永不抛异常** |
| | `enums.py` | 全部枚举。`ProbeName` 只剩 `VISION`/`MEDIA`；`SolvePath` = `single`/`mock`/`cache`（Tier 分级已删）；`AdvanceMethod` = `card`/`click`/`scroll`/`swipe`（`unknown` 只是裁决前的占位，不是运行期可用的方式）；`CompletionState` = `all_done`/`not_done`/`unknown` |
| | `states.py` / `qid.py` / `vid.py` | 九态状态机（`pending` 没有入边）/ 题目指纹（不含答案）/ 分集标识 |
| | `models.py` | 跨层数据模型（`Question` / `Answer` / `ReadResult` / `ReadBatch` / `PageView` / **`RunPlan`**）。`RunPlan` 取代了旧的**标定模型**：后者只说得出「怎么推进」而**给不出落点** |
| | `run_plan.py` | **开局裁决**：`derive_plan(batch, *, batch_size=0) -> RunPlan`。读图那一次就把四件事定死 —— ①推进方式 ②推进几何 ③提交范围 ④总题数；`grid_geometry` / `card_cell_box` 是答题卡的纯算术落点（算不出来返回 `None` → **不点**） |
| | `events.py` | 事件名常量（`<domain>.<object>.<action>`）。**新增事件必须同步 `ui/static/app.js` 与 `tests/test_t0_definitions.py::EVENT_COUNT`** |
| | `trace.py` | `EventBus` + `RunLogger`（留痕目录 + `events.jsonl`）+ 缓存清理 |
| | `db.py` | SQLite schema / 迁移 / 读写。加列必须补 `_migrate()` |
| | `model_registry.py` | 模型配置与凭据后端 |
| | `arbiter.py` / `orchestrator.py` | 单通道仲裁 / **编排循环**（最大的文件） |
| | `advance_library.py` | **固定的推进方式库**：方式 → 实现与元数据。`PLAN_PREFERENCE = (CARD, CLICK, SCROLL, SWIPE)` 现在是**开局裁决的优先级**，不是运行期的尝试阶梯 |
| `perception/` | `vision_probe.py` | 唯一的题目通道：**只出图，不调模型**（`full_page` 禁用，一律视口截图） |
| | `media_probe.py` | 媒体态 + 弹题探测 + 分集目录（唯一仍读页面结构的地方） |
| `solve/` | `solver.py` | 取样 →（开了复算才）投票 → 缓存 → 复核标记；**不复算时用模型自报的 `confidence`**（低于 `guards.confidence_review_min` → 标 ⚠复核必停），复算时用多数票占比 |
| | `reader.py` | 一份提示词（`READ_SYSTEM_PROMPT` = `00+10`）+ 各 `parse_*`：`parse_page_view` / `parse_read_batch` / `parse_read_payload`，`gate_read_result` 门禁，`read_question` / `read_questions`。**不再有**开局标定 / 收尾确认 / 找推进控件那三套独立契约 |
| | `voting.py` / `cache.py` | 按**内容**计票 / 只缓存干净作答 |
| | `prompt_files.py` | 提示词装载（导入时读一次，**缺文件大声失败**） |
| | `training.py` | **训练模式**：把成功任务总结成经验，写进 md 的自动标记区 |
| `act/` | `actuator.py` | 一律按归一化坐标点；媒体阶梯仍在。`select_option` 的落点处置按**判定表**分档：墨迹落点 `changed`/`weak` 收工、`none` **绝不重点**（`ok=True` + `no_change_on_ink:...`）；几何兜底落点才允许换点，全部候选都 `none` → `_exhausted` + 暂停 |
| | `verifier.py` | 题目侧只有**截图差分**，且判据是三态（`region_change_state`：`changed`/`weak`/`none`）；媒体侧时间窗断言 |
| | `screen.py` | `norm_box_center()` —— **坐标换算全项目唯一一处** |
| `target/` | `browsers.py` / `windows.py` | 附加浏览器（CDP）/ 枚举原生窗口；自管浏览器登记表供「关机」收尾 |
| `ui/` | `runner.py` | 任务生命周期**唯一实现**（`start` 与 `retry` 共用） |
| | `assembly.py` | 唯一跨层装配点 |
| | `system.py` | 关机 / 端口清理（判定与 I/O 全部可注入） |
| | `static/` | 零构建前端三文件：`index.html` / `app.js` / `style.css` |

---

## 界面（信息架构）

- **主页**只有两块：**任务**（主角，一任务一张卡）+ **模型库**；点任务卡进**任务详情**。
- **任务详情**顺序固定：头部信息 → 运行态条 → 「需要你决定」→ 任务过程 → 题目/分集 → **日志最后**。
- **新建任务五步向导**：任务名 → 目标 → **两组模型选择** → 运行选项 → 确认。每步一个 `.why` 说明块。
- **向导第 3 步的四组模型**：视觉组 / 视觉备用 / 解题组 / 解题备用。
  **两个备用组是勾选列表**（不是原生 multi-select —— 后者要按住 Ctrl，
  用户点第二下会取消第一个，看起来就是「勾选没生效」）；**勾选先后 = 降级顺序**。
- **向导第 4 步**：复算开关 + 复算次数（关掉时次数置灰）、任务序列、提交模式、干跑、训练模式。
- **前端两条纪律**：① 模型只能从 `/api/models` 选（下拉不许手打 `profile_id`）
  ② 每个空状态 / 置灰都要有下一步出口。

---

## 接口地图

| 层 | 路径 |
|---|---|
| 任务（run） | `GET/POST /api/tasks`、`GET/PATCH/DELETE /api/tasks/{run_id}`、`GET /api/tasks/{run_id}/cache`、`POST /api/tasks/{run_id}/{start,pause,resume,stop,retry}`、`POST /api/tasks/bulk-delete` |
| 条目（item） | `GET /api/items`、`GET /api/items/{item_id}`、`POST /api/items/{item_id}/confirm`、`GET /api/items/{item_id}/artifacts[/name]` |
| 运行配置 | `GET/PUT /api/run/config`、`GET /api/run/progress`、`POST /api/run/{start,pause,resume,stop}`（兼容入口） |
| 模型 | `GET/POST /api/models`、`PATCH/DELETE /api/models/{profile_id}`、`POST /api/models/{profile_id}/test`、`GET /api/providers/presets` |
| 目标 | `GET /api/targets`、`POST /api/targets/launch` |
| 训练 | `GET /api/training`（只读：写到哪几份文件、现在有哪些经验）、`POST /api/training/{run_id}`（跑一次总结） |
| 系统 | `GET /api/health`、`GET /api/system/status`、`POST /api/system/shutdown` |
| 事件 | `GET /api/events`（SSE） |
| 引导 | `GET /api/guide?code=` |

- `item_id = f"{run_id}-{qid}"`（运行内标识；`task_item.item_id` 是全局主键）。
- `TaskOut.status` 是**展示态 key**（`created`/`running`/`paused`/`finished`/`stopped`/`error`），
  前端按 key 选样式，**不要解析 `status_raw`**。
- `POST /api/tasks` 的配置补丁用 `exclude_unset`（显式 `null` = 明确清空）；
  `PUT /api/run/config` 用 `exclude_none`。

---

## 测试与质量门槛

```bash
export AUTOLEARN_SECRET_BACKEND=memory        # ⚠️ 不设它跑测试会往真实 Windows 凭据库写
export AUTOLEARN_BROWSER_CHANNEL=chrome       # 默认 msedge

.venv/Scripts/python scripts/serve_mock.py &  # 靶场须先在后台起，否则真浏览器用例整批 skip
.venv/Scripts/python -m pytest -q --junitxml=.pytest-tmp/report.xml -p no:cacheprovider
.venv/Scripts/python -m ruff check .
.venv/Scripts/python -m mypy
.venv/Scripts/python scripts/check_ui.py      # 前端验收（真 Chrome，DOM id 是契约）
.venv/Scripts/python scripts/check_mock.py --all
.venv/Scripts/python scripts/check_target.py
.venv/Scripts/python scripts/check_server.py  # 判控制台是否连着旧进程
```

三条经验，每条都踩过：

1. **靶场不起 → 真浏览器用例静默 skip，pytest 全绿 ≠ 跑过**；
2. pytest 的 terminal summary 可能被沙箱吃掉 → 用 `--junitxml` 读权威结果，`--basetemp` 放项目内；
3. **跑测试期间不要改** `core/ui/target/act/perception/solve/adapters/scripts` 下的
   `.py/.js/.html/.css/.md` —— `code_rev()` 变化会让 `test_health_exposes_boot_rev_and_why_it_matters` 变红
   （那是**真断言**，不是 flaky）。

---

## 维护指南：改 X 要看 Y

| 要动什么 | 先看哪 |
|---|---|
| 提示词 | `prompts/*.md`（**唯一真源**，导入时读一次 → **改完必须重启服务**） |
| 新增事件 / 加表 | `core/events.py` + `ui/static/app.js` + `tests/test_t0_definitions.py` 的 `EVENT_COUNT` / `DB_TABLE_COUNT` |
| 新增 `RunConfig` 字段 | `ui/schemas.py`（In/Out）+ `ui/routes/run.py::_config_out()` 回显（症状：「存得下、读回是空」）+ 向导前端 |
| 推进 / 滚动 | **方式**：`core/run_plan.py`（开局裁决）+ `core/advance_library.py`（`PLAN_PREFERENCE`）+ `prompts/33-推进方式库.md`。**当前这一步的落点**：`prompts/10-视觉组.md`「推进目标」一节 + `core/enums.py::AdvanceSkill` + `solve/skill_library.py::ADVANCE_SKILL_SPECS` + `skills/advance_*.md` + `solve/reader.py::parse_page_view` + `core/orchestrator.py::_fresh_advance_target` / `_vision_swipe` / `_settle_after_advance` / `_reconcile_advance_read` → `tests/test_advance_next.py` + `tests/test_advance_skills.py` |
| 提交时机 | `core/run_plan.py::derive_plan`（`submit_scope` 开局定死）→ `core/orchestrator.py::_submit_scope_of_run` / `_submit_paper_once` + `prompts/10-视觉组.md` 的 `submit.scope` 一节 |
| 置信度 / 复核口径 | `solve/solver.py::_confidence_of` / `_review_reason_of`（`SAMPLE_RETRY_MAX` = 每条采样项最多 3 次请求）+ `solve/prompts.py::parse_confidence` + `guards.confidence_review_min` |
| 点哪儿 | `act/screen.py::norm_box_center()`（唯一换算点）+ `actuator.py::candidate_points`（框内**内容质心**，不是几何中心） |
| 任务列表界面 | `ui/routes/tasks.py`（`/retry`、`/bulk-delete`、`/cache` **必须排在 `/{run_id}/{action}` 之前**）→ `app.js::renderTasks` → `scripts/check_ui.py` |
| 留痕 / 事件流 | `core/trace.py::RunLogger` → `orchestrator._emit`（一条事件既进总线、又落盘） |
| 关机 / 端口 | `ui/system.py` + `target/browsers.py`（`MANAGED_PORTS = 8800/8899/8900`） |
| 训练模式 | `solve/training.py`（`TARGETS` / 经验区界标）+ `ui/routes/training.py` |
| 打包 / 桌面化 | `AutoLearn.spec` + `launcher.py` |

### 踩过的坑（别重复踩）

- **界面没变化先查旧进程**：`/api/health` 回 `boot_rev` / `code_rev` / `pid`；判陈旧比 `boot_rev`。
- **回环地址不能用环境代理**：httpx 一律 `trust_env=False`。
- **`.bat` 必须纯 ASCII/GBK**。
- **不要用 PowerShell 管道改文本文件**（会静默跳行并把坏内容写回）。
- **改动前先备份 `__pycache__`**：`.pyc` 能被 `SourcelessFileLoader` 直接加载，是误删后唯一的线索。

---

## 硬约束（违反即出事故）

1. **坐标只乘一次**：一律 `scale="css"` 截图；换算点只有 `act/screen.py::norm_box_center()`，
   **只乘、不除 DPR**（把除法加回来 → 每次点击偏一半）。
2. **一律视口截图，禁 `full_page=True`**（有守门单测）。
3. **门禁不许绕过**：`clipped` / `uncertain` 非空 → 不下传，带原因码暂停问人。
4. `submitted` 是**唯一危险态**：续跑只回读，**绝不重新点击 / 重放提交**。
5. 提交**不重试、不重放**，超时暂停等人。
6. **禁 `networkidle`**；媒体态必须读 `<video>` 属性，不能拿元素可见性代替。
7. 失败一律「暂停 + 截图 + 留档」；**禁静默跳过、禁用 assert 做流程控制**。
8. `qid` 不含答案；选项排序后参与哈希。**选项顺序一律按页面顺序，从不打乱** ——
   解题组看到的标号必须逐字等于页面标号；自指选项（以上/上述/都正确…）的检测仍保留，
   但已降级为**风险标注**（`allows_shuffle`），不改变任何呈现顺序。
9. 投票按**内容**比对；`MockProvider` 读地面真值，**不得作闸门依据**。
   解题组**只吃文本**：请求里 `images` 恒为空（它看不到截图、坐标或任何图片）。
10. 密钥只进 OS 凭据管理器；`models.yaml` 零敏感字段（只存 `api_key_ref`）。
11. 删文件：**只删显式列出的路径**、先校验在仓库根之下、**绝不与其它命令同行**。

---

## 2026-09-30 收口：四条修复

用户实测之后的四条判词，各自对应一处设计改动。**判词本身是需求，别只当成 bug 报告**。

### ① 视觉组只翻译，解题组只看文本（判词：「视觉组模型的任务就是解读照片，将照片翻译成固定格式，解题组模型严禁拿到非文本输入」）

- 视觉组（`prompts/00-共享契约.md` + `10-视觉组.md` → `solve/reader.py::READ_SYSTEM_PROMPT`）
  **只做观测**：所有视觉请求回**同一份固定格式** ——
  `{"page": {...页面观测...}, "questions": [...], "more_below", "note"}`。
  `page` 里是「有什么」+「这一步从哪进下一题」：`progress` / `total` / `current` /
  `next_control` / `card` / `submit` / `completed` / `scrolling` / `reason`，
  外加 **`advance_skill_id`**（从注册的推进技能里选一个）与 **`swipe`**（选滑动时给方向与幅度）。
  **它不判提交时机**，也不决定「用哪种推进方式推进整条运行」——
  那仍在开局由程序裁决；视觉组只回答「**这一步**按当前画面该点哪儿、该滑多远」，
  而这个答案**每一屏都要重新给**（这是修复「推进后与实际不符」的关键，
  见 `prompts/10-视觉组.md` 的「推进目标」一节）。
- 解题组（`prompts/20-解题组.md`、`solve/solver.py`）**只吃文本**：请求里 `images` 恒为空；解题策略由视觉组选择的注册技能 ID（`single_choice` / `true_false`）确定，缺少匹配技能的题目记录为 `SKIPPED`。技能正文位于 `skills/`，注册表位于 `solve/skill_library.py`。
  选项标号就是**页面标号**（可能不连续，如 `A、B、D`），**不再打乱选项**。
- **交接物就是那一份固定格式本身** —— 视觉组的 JSON 就是解题组的输入文本，
  中间没有「再翻译一次」的环节。
- 为什么要切这么干净：**控制流**留在程序手里（用哪条路推进、什么时候提交、什么时候算做完），
  视觉组只回答「这一步的画面里，那个入口在哪 / 该滑多远」。
  这条边界 2026-10-01 重新划过一次：旧版把**落点**也锁死在开局那一屏，
  于是页面一变（滚动、答题卡重构、缩放）就点偏 ——
  真机 `logs/9abc4ed8ef60` 连续两次 `advance_card` 点偏后停下等人。
  现在的口径是「**方式的控制权归程序，落点的新鲜度归视觉组**」；
  视觉组给的落点必须来自**当前这一屏**，且程序侧的判据（题号校验、到达总数、收尾确认）
  一条都没有放松。

### ② 开局判定一次，之后全按它走（判词：「程序开始运行时需要有一套判断逻辑，一旦判定好后后续的全部按照这套逻辑」）

- 新增 `core/run_plan.py::derive_plan(batch, *, batch_size=0) -> RunPlan`（`RunPlan` 在 `core/models.py`）。
  它在**开局读图那一次**就把四件事定死：①推进方式（`CARD`/`CLICK`/`SCROLL`/`SWIPE`，**恰好一种**）
  ②推进几何（`control_box` 或答题卡的 `card_origin`/`card_step`/`card_anchor`）
  ③提交范围（`PAPER`/`QUESTION`）④总题数。裁决优先级在
  `core/advance_library.PLAN_PREFERENCE = (CARD, CLICK, SCROLL, SWIPE)`。
- **此后不再**：**换招**（点击→滚动→滑动那套阶梯已整条删除）、
  「滚到顶部重读一次」、以及因为「这一屏没有提交按钮」而中途暂停。
  ⚠️ 与旧版的差别：「不再每题问模型『下一题在哪』」**已经作废**（2026-10-01）——
  现在每一步都按**紧邻的那一次读图**取落点；但它不是额外的一次调用，
  而是复用「读这一屏的题」那次回复里顺带给出的 `advance_skill_id` / `next_control` /
  `card.next_box` / `swipe`。**程序侧不再自己算答题卡落点，除非视觉组没给。**
- 推不动就**停下**：`_settle_end` → 请视觉组确认「是不是全部完成了」→ 确认才收工，
  确认不了就 `advance_failed` 停下等人（**绝不静默跳过**）。
- 答题卡落点：**优先用视觉组本屏直接指出的 `card.next_box`**；它与 `card.current_box`
  重叠 > 50% 时视为模型把「当前格」填成了「下一格」→ **不采信**，退回开局纯算术
  （`grid_geometry` + `card_cell_box`，容差 `CARD_GRID_TOLERANCE = 0.10`）；
  算术也算不出来返回 `None` → **不点**（宁可停下，也不拿编出来的坐标点用户的页面）。
- 推进动作成功后**无条件沉淀** `guards.advance_settle_ms`（默认 350ms）再让下一轮截屏：
  页面切题是异步的，动作一返回就截屏会截到**换到一半的旧画面**。
- 推进之后读到的题号分两种处置（防静默跳题的判据一条没松）：
  - 读到的是**推进前那道题** → 先沉淀再**补读一屏**；补读到别的题就照常继续，
    补读仍是同一道 → `advance=no_effect`，`advance_failed` 停下；
  - 读到的是**第三个**题号 → `advance=wrong_question`，`advance_failed` 停下。
  两者的提示语不同 —— 前者是「这一步没生效」，后者才是「坐标算错了」。

### ③ 置信度只在「不开复算」时才有意义（判词：「如果不使用复算系统则不存在置信度系统。目前不开启复算时存在模型无法解题的情况」）

- 解题组**自报 `confidence`**（`solve/prompts.py::parse_confidence` 认 `0.8` / `"0.8"` / `"80%"`，
  越界**夹到** `[0,1]` 而不是丢弃）；`prompts/20-解题组.md` 把它写进输出契约，**不许省略**，空作答写 `0`。
- **不开复算**（`sample_n == 1`）：用自报值；低于 `guards.confidence_review_min`（默认 0.5）
  → `review_flag=True`（理由「自报置信度低于门限」）→ 停下请人复核；模型没报 → 回到一致率（单样本即 1.0）。
- **开复算**（`sample_n > 1`）：用**多数票占比**，自报值不参与。`recalculated` 仍表示「是否开了复算」（`n > 1`）。
- **不可用的单次回复**（无 content / 解析不出 / 标号越界 / provider 报错）是**请求失败**，
  走**有界重发**（`solve/solver.py::SAMPLE_RETRY_MAX = 2`，即每条采样项最多 3 次请求），
  **不是**复算；**显式空作答是模型的结论**，不重发。
- 为什么：不开复算时只有一次采样，那个 1.0 的一致率不含任何信息；
  一次网络抖动或一次截断会让 `chosen_labels=[]`，用户看到的是「模型解不了这道题」，
  真实原因却只是那一次回复坏了 —— 开复算时第二个样本能顺手救回来，所以这个故障**只在不复算时暴露**。

### ④ 动作回读是三态（判词：「动作回读不一致判断逻辑存在问题，存在点击正确却判断错误导致胡乱操作」）

- `act/verifier.py::region_change_state(mad)` 三态：`changed`（`mad >= REGION_CHANGE_MIN_MAD = 2.0`）/
  `weak`（`>= REGION_CHANGE_WEAK_MAD = 0.05`）/ `none`；
  `verify_region_changed` 的 `ok = state in {"changed", "weak"}`，`actual` 形如 `region_mad=0.31 state=weak`。
- `select_option` 的处置表按**落点**分档：`ink_centroid` 落点 → `changed`/`weak` 成功收工；
  `none` → **绝不重点**（`ok=True`，readback `no_change_on_ink:...`）；
  几何兜底落点 `none` → 换下一个候选点（换点前先重测，已是 `changed`/`weak` 就收工）；
  全部候选都 `none` → `_exhausted`（`ok=False` + 截图 + pause）。
- 为什么：画面本来就没什么可变的（例如纯文字行、或这道题**本来就已经选中**）时，
  旧判据会判「没点中」，于是反复换点、乱点一气；多选/复选上那一下会把刚选上的勾**取消**。
  重复点正是用户报的「胡乱操作」本身，收益为负。

---

## 2026-10-01 收口：推进落点 + 回读误报

用户实测报了两件事，各自对应一处**真缺陷**（不是配置问题）。现场证据都在
`logs/9abc4ed8ef60/`。

### ① 推进之后读到的题与实际不符（症状：`expected 29 got 28`）

- 现场：`advance_card clicked number=27` 之后重新读图仍读到 **26 题**（同一个 qid），
  触发 `{"advance":"wrong_question","expected":27,"got":26}` 并暂停；下一次点 29 号格
  读到 28，同样停下。而页面本身没有坏。
- **真正的根因在下面 ①-b**（答题卡目标题号没跟着「同屏队列里做完的题」走）。
  这一条列的是另外两处**同样真实、但单独不足以解释现场**的缺陷：
  1. **落点只认开局那一屏**：答题卡落点原先由开局那次观测**纯算术**推出来，
     而页面一滚、答题卡重构、浏览器一缩放，旧框就失效。现在每一步优先用
     **紧邻那一次读图**给出的 `page.next_control.box` / `page.card.next_box`，
     开局几何只作兜底。⚠️ 独立验证指出：**单靠这一条修不好现场**
     （现场那一屏 `plan.card_target(27)` 与模型的 `next_box` 恰好是同一格），
     它解决的是「页面变过之后落点漂移」这一类。
     「模型把当前格填成下一格」（重叠 > 50%）、
     「下一格离当前格超过 1 格（网格下标差）」、「模型没给当前格」
     这三种一律**不采信**，退回有界算术（`_fresh_advance_target`）。
  2. **截屏抢跑**：页面切题是异步的（进度条先变、题面后换），动作一返回就截屏，
     截到的正是换到一半的旧画面。现在推进成功后**无条件沉淀**
     `guards.advance_settle_ms`（默认 350ms）再进入下一轮读图
     （`_settle_after_advance`；与靠页面指纹判断的 `_page_changed` 分工不同，
     后者取不到指纹时会整条跳过，前者是无条件兜底）。
     ⚠️ 把 `advance_settle_ms` 设成 0 是合法配置，但会让这条兜底失效 ——
     独立验证者实测：设 0 时现场故障可原样复现。
- 「这一下没生效」与「落到了别的题」现在**分开报**：
  读回来仍是**推进前那道题** → `_reconcile_advance_read` 先沉淀再**补读一屏**
  （补读到别的题就照常继续）；补读仍是同一道才按 `advance=no_effect` 停下。
  读到**第三个**题号才是 `advance=wrong_question`。
  ⚠️ 补读**只读不点** —— 绝不重试点击，重点一下就可能真的一次跨过两道题。
  ⚠️ 补读的判据是「**任何**数字不符」，不是「正好读到上一题」：截屏抢跑时读到的
  可能是**比上一题还旧**的一帧（现场 `expected=27 / 上一题=27 / got=26` 就是这样），
  按「等于上一题」筛选会整条漏掉。

### ①-b 答题卡目标题号必须**跟着实际做完的题走**（独立验证者找出的真根因）

- 上面两个修复之外还有一个更靠前的缺陷：**一屏多题时，同屏第 2、3 题是从
  `_pending_reads` 队列直接消费的**（不截图、不调模型），那几步**不经过答题卡推进**，
  所以 `_card_number` 链不会 +1 —— 两道都做完再推进时，目标又回到了**刚做完那道题**，
  点下去页面不动。现场 `logs/9abc4ed8ef60` 正是如此：一屏读到 26 / 27，
  做完之后点答题卡的 **27 号格**（＝刚才那道）。
- 现在 `_card_advance_target()` ：**题号可信**（开局观测到了「当前第几题」）时，
  目标 = **刚做完那道题**的题号 + 1；题号不可信时仍走链 ——
  那时 `card_start_number` 退回 1、`card_anchor` 由模型指出的 `next_box` 反推，
  题号基底是**合成的**，拿页面真实题号去喂 `card_target` 会解析到别的格子。
- ⚠️ **只改题号还不够**（第二版修的东西，独立复验的第二个反例）：
  `_advance_by_card` 会**先**取「最近一次读图」给的 `card.next_box`。而
  `core/run_plan.py` 在开局把锚点重钉成 `card_target(current + 1) == next_box`，
  所以模型那个 `next_box` **按构造就是「开局时那道题的下一题」那一格** ——
  本屏已经消费过 #27 之后，它指的正是**刚做完的 #27**，且与 `current_box` 恰差 1 格，
  刚好越过「相邻」护栏。结果：题号算对了（28），**落点仍点在 #27 上**，现场症状原样保留。
  修法：记 :attr:`_queued_consumed`（本屏从队列消费掉的题数），
  **只要它 > 0，这一屏给的 `card.next_box` 一律判为过期**（事件
  `advance_card=next_box_stale_screen`），退回按实际题号算出的落点。
  同屏消费过的题是「不经过推进」被做掉的，这正是那个指针落后的原因。
- 题号修正**有界**：只允许在 `[链目标, 链目标 + 已消费题数]` 窗口内向前修正。
  超出窗口说明 `num_text` 本身不可信（把 26 抄成 30 之类）→ **退回链目标**：
  链每成功一步只 +1，自己**永远不会跳过题**；宁可少走一步（会被题号校验抓到并停下），
  也绝不静默跨过中间几道（独立复验构造过「一次跨过 #28~#30 且校验抓不到」的反例）。
- 同一轮还收紧了「模型给的下一格」的采信条件（`_fresh_advance_target`）：
  `card.current_box` 缺失 → 相邻性无从校验 → **不采信**（退回有界算术）；
  下一格与当前格的**网格下标差 > 1** → 不采信（点它等于一次跨过好几道题）；
  网格信息不全（缺 `cols`/`rows`/外框）导致下标差**算不出来** → 同样不采信
  （「无从校验」与「太远」同处置）。
  用下标差而不是几何距离：答题卡按行换行，行末那一步几何上横跨整个卡片宽度，
  却仍然是「下一格」；而斜对角用几何距离只有 √2、会被误当成相邻。

### 已知未修风险（独立验证者报出、本轮有意不做）

- **滚动推进的「到位」判据可以被题号缺失绕过**：`_advance_by_scroll` 靠
  `_scroll_gap` 判断「新题进来了没有」，而它**任一端 `num_text` 读不出来就返回
  `None`**，于是被当成「arrived」—— 题号 3 → 16 这种大幅跳跃在 `num_text` 缺失时
  不会触发 `advance_scroll=jumped` 告警，中间 #4~#15 从未入队，运行继续做 #16。
  触发条件是「视觉组没抄到题号」（`num_text` 是可选字段，门禁不要求它）。
  **本轮不修的理由**：用户报的是答题卡推进 + 回读误报，与滚动路不是同一条；
  改动落在滚动重读逻辑上、回归面较大。修法方向：`num_text` 缺失时**不许**判
  `arrived`，改为「读不到题号 → 不确认到位，按推不动停下」。

### ② 「进入下一题」成为视觉组可选的**推进技能**

- 新增三个注册技能（`skills/advance_*.md`，注册表在 `solve/skill_library.py`，
  ID 常量在 `core/enums.py::AdvanceSkill`）：`advance_click`（点「下一题」控件）、
  `advance_swipe`（滑动，**必须**给方向与幅度）、`advance_card`（点答题卡题号格）。
- 视觉组在**每一次**读图的 `page.advance_skill_id` 里选一个，滑动时另给
  `page.swipe = {direction, amplitude, reason}`；`amplitude` 是相对视口宽/高的比例，
  执行前夹到 `0.05~0.9`（1.0 会让手势从屏幕边缘起手，有相当比例被系统丢弃；
  0 幅度等于没滑却会被记成成功）。**没选 `advance_swipe` 时它给的幅度一律不采信**
  （模型自己都没判成滑动，就不该拿它顺手填的数去滑）。
- 解析纪律与题型技能同款：未注册的 ID、非法的 `direction`、不可转或越界的 `amplitude`
  一律落成 `None` + `warning` 留痕，**不修不猜**；`advance_swipe` 缺合法 `swipe` 载荷时
  连技能选择一起降级，避免执行层拿到「没有幅度的滑一下」。

### ③ 详情页「动作回读不一致」误报（症状：选对了却显示不一致）

- `ui/store.py` 的旧判据是 `readback != expected` 的**文本比较**：`expected` 是阈值表达式
  `region_mad≥2.0`，而成功动作写的是人话 `region_mad=6.02 state=changed aim=ink_centroid`
  —— **恒不相等**，于是每一次成功点击都在详情页显示「动作回读不一致」。
  现场四个真实 `action.json` 全部中招。
- 现在由**结构化事实**判定：`ActionResult.readback_ok`（`core/models.py`，默认 `True`）
  → `ui/store.py::readback_mismatch()`。老留痕缺这个字段时退回同一条留痕里的 `ok`
  （`ok is False` 才算不一致），**任何分支都不再比较文本**，历史任务不会被翻成一片红。
- ⚠️ 纪律：`readback_ok` 默认 `True`，所以**新增的失败构造点必须显式填 `False`**
  （`act/actuator.py::_exhausted` 已填，并有守门用例）。

### 收口时删掉的东西（别再照旧名字找它们）

- 提示词只剩 **5 份**：`00-共享契约.md`、`10-视觉组.md`、`20-解题组.md`、`33-推进方式库.md`、
  `34-训练总结.md`。原先那三份**控制流**提示词（`30` / `31` / `32`：开局标定、收尾确认、
  找推进控件各一份）已删除 —— 它们各自是一份独立输出契约，而现在是
  「**所有视觉请求回同一份固定格式**」，不同时机只是用到的字段不同。
- `solve/reader.py` 现在**只有一个提示词**（`READ_SYSTEM_PROMPT` = `00` + `10`）与一组解析函数
  （`parse_page_view` / `parse_read_batch` / `parse_read_payload`、`gate_read_result`、
  `read_question` / `read_questions`）。开局标定 / 收尾确认 / 找推进控件那三套 `*_SYSTEM_PROMPT`
  与它们各自的解析函数已随之删除。
- `core/models.py` 里没有「标定」这个模型了（见上，`RunPlan` 取代它；也没有任何「推进控件观测」之类的中间模型）；
  `ReadResult` 不再携带提交框 / 下一题框 / 提交范围 —— **它们属于 `PageView`**，
  是「这一屏长什么样」的观测，不是「一道题」的属性。
- 编排层不再有「开局标定」「查找下一题控件」「按视觉点击推进」「滚到顶部重读」这几条路径
  （`_plan_run` 裁决一次 + `_run_advance_strategy` 按方案执行，是唯一的推进入口）；
  「提交范围」也不再是编排层上的一个可变字段，而是 `RunPlan.submit_scope`。
- 术语也换了：文档里说「开局**判定**」（旧词是「开局标定」）——
  旧词暗示「问模型要一份标定结果」，而现在是**程序按观测裁决**。

---

## 文档体系

| 文档 | 内容 |
|---|---|
| `README.md` | 本文件：定位、用法、结构、约定 |
| `docs/AutoLearn-项目任务书.md` | 需求与验收口径 |
| `docs/AutoLearn-实施规划书.md` | 分阶段实施规划 |
| `docs/双模型分工-视觉组与解题组.md` | 视觉组 / 解题组的交接契约 |
| `docs/真实站点作业做题手册.md` | 真实站点（超星学习通）实战结论 |
| `prompts/*.md` | **提示词唯一真源**，共 5 份：`00` 共享契约 / `10` 视觉组 / `20` 解题组 / `33` 推进方式库 / `34` 训练模式（`30`~`32` 三份控制流提示词**已删除**） |
| `mock_site/static/traps.md` | 靶场坑位清单（由 `scripts/gen_traps_md.py` 生成，勿手改） |

### 训练模式怎么用

```bash
curl -X POST http://127.0.0.1:8800/api/training/<run_id>
```

它会读该任务的留痕（`events.jsonl` + 逐题留痕），让模型总结出几条**可复用的判读经验**，
追加到 `prompts/10-视觉组.md` / `20-解题组.md` / `33-推进方式库.md` 的
`<!-- AUTOTRAIN:BEGIN -->…<!-- AUTOTRAIN:END -->` 区块里（**人工内容一个字不动**）。

- 只有**跑成功**（`finished`）的任务允许训练：失败记录里混着走不通的路径；
- 也可以在向导第 4 步勾「训练模式」，任务成功后自动跑一次；
- 训练改的是 `prompts/` → **改完记得重启服务**才生效。
