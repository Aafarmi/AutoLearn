# AutoLearn 网课模块 · AI 任务书

| 项目 | 内容 |
| --- | --- |
| 版本 | v2.8（2026-10-24：M16 错误矩阵与安全十条） |
| 需求来源 | 根目录 `TASKBOOK.md` |
| 代码基线 | AutoLearn v1.0.4 + 当前工作区改动；fuckZHS v2.6.3（vendor 已内嵌） |
| **代码来源硬规则** | **§1.1：能直接复用 fuckZHS 就复用；AutoLearn 有的就改那块；只有前两级都不成立才新写，且必须写理由** |
| 本文件身份 | ① 任务分解；② **项目进度看板**（每条提示词都要求回写 §4） |
| 实测时间 | 2026-10-24（M16 后全量 `pytest tests -q`：259 passed） |

---

## 0. 当前进度审计（实测，不是估计）

**状态：23 个任务包 → 22 已完成、1 部分完成、0 未开始。完成标记表示任务包的实现与验收已完成，不表示所有真实课程模式和整个产品均已验收。M23 为 M22 后新增的扫码登录与登录后选课流程，静态检查完成，真实扫码及目录返回待用户验收。**

M1-M22 原计划工作量合计 **55 人日**，M23 尚未估算；这些不是实际工时，禁止据此宣称“已投入 55 人日”或产品完成百分比。参数校验、模型档案映射、三模式 bridge 分流、服务路由、安全矩阵、混合列表、课程详情和 M21 回归已完成；M23 的扫码/选课流程待真实账号验收，G2 真实 shared 课程计数对账及 G3 课程浏览器端到端仍待独立验收。

### 0.1 已实现且有自动化验证的任务包

| 任务包 | 当前证据（人工站点验收另记） |
| --- | --- |
| **M1 地基与通道** | protocol/errors/sites、vendor 文件清单和依赖；spawn/iter_jsonl 与 echo_bridge 测试 |
| **M2 后端契约与分发** | meta/task_type/CourseError handler、六处分发与 task_router 测试 |
| **M3 概览合并与做题快照** | 混合列表、做题五接口快照；正常课程概览只读索引的回归测试 |
| **M4 领域与存储** | 状态机、JSON/索引/双事件日志；同根多实例共享锁及交错更新回归 |
| **M5 DTO 与事件出口** | summary/detail/course_block/events；18 键白名单、索引与完整任务的摘要一致性 |
| **M6 生命周期治理** | archive/delete/槽/启动恢复；新旧运行目录清理与删除失败重试回归 |
| **M7 运行时起停与环境** | start/stop/shutdown_all；进程退出、槽释放与启动失败收尾回归。M15 已接入服务；真实站点与浏览器端到端仍未验收 |
| **M8 双通道与事件泵** | stdout 协议校验、内存态/事件落盘、phase 事件、stderr 脱敏与截断；10 万行 stdout（含 100 行坏 JSON）+ 1 万行 stderr 无死锁，protocol_errors=100 |
| **M9 终局裁定** | `CourseRuntime.finalize()`：取消/超时/result/退出码优先级、业务时限缓冲、槽释放、`course_finished` 和 vendor 副本清理；7 类裁定场景 + 实际 watcher 超时用例 |
| **M10 bridge 与进度适配** | 独立 JSONL stdout、stderr 隔离、stub 执行边界与进度事件适配；不调用上游真实课程方法 |
| **M11 执行分流与统计** | shared/hike/ai 显式调上游入口；优先按上游刷新后的课程上下文统计；3 模式 stub 与 time_limit 结果用例 |
| **M12 认证链路** | Cookie 抓取入口、凭据白名单、预检、service.authenticate；已有替身测试，真实智慧树登录及超星抓取回归待人工记录 |
| **M13 运行目录与终端补丁** | 白名单指纹复制、jobs/<id>/run 路径与终端 shim；副本导入和路径隔离测试，真实 AI 流程待验收 |
| **M14 参数校验与档案映射** | `validate_create()` 参数边界；`aimap.build()` 支持的 Chat Completions URL 映射；12 组合法/12 组非法参数及 5 组 URL 用例 |
| **M15 服务与课程路由** | create/start/rerun/settings 与 stop/archive/delete 服务委托；课程创建不写 tasks.sqlite3；四端点成功/失败 stub 与跨源拒绝回归 |
| **M16 错误与安全** | 7 类 CourseError 状态码与安全中文响应；安全十条逐项测试，复用 M8 stderr 脱敏器 |
| **M17 前端新建任务** | 做题/网课表单分流、课程参数和站点能力过滤、左栏类型筛选；请求体不带 `page_url` |
| **M18 前端列表与徽章** | 混合列表显示任务类型/课程阶段/进度；状态动作与删除分流；轮询签名覆盖动态字段 |
| **M19 前端课程详情** | 课程阶段、信息与进度卡、状态操作矩阵；revision 纳入进度，课程详情跳过做题专用请求 |
| **M20 自动化测试补全** | 全量 273 passed；课程 250 passed，覆盖率 80.3%；运行终局和稳定性回归 |
| **M21 界面自检与做题回归** | 用户于 2026-10-24 确认截图、三视口断言、做题接口核对及超星/雨课堂完整链路验收通过 |
| **M22 文档与收尾** | 架构、README、CHANGELOG、版本及任务状态已同步；发布清单见交付回复 |

### 0.2 部分完成

| 任务包 | 当前卡点 |
| --- | --- |
| **M23 登录后选课** | 空课程草稿、fuckZHS 二维码会话、课程目录和选择接口已接通；Python/JS 语法检查通过，真实账号扫码和三类目录返回待用户验收 |

### 0.3 未开始

无。

### 0.4 当前剩余断点（2026-10-24 复核）

> 已接通：课程创建、参数设置、认证校验、启动/重跑、停止/归档/删除服务与路由；M7～M22 任务包已完成（M21 由用户确认本机验收通过）。产品级 G2 真实 shared 课计数对账与 G3 课程完整浏览器链路仍按下文单独跟踪。

1. **真实刷课尚未验收**：M11 三模式和 M15 服务链路有 stub/自动化覆盖，但没有真实 shared 课程与站内进度对账记录。→ 需要有效课程 ID 与登录态后做 G2 验收。
2. **课程详情端到端未验收**：M19 详情页已实现；仍需在浏览器完成课程任务“新建→认证→开始→进度→停止”的真实服务链路及错误请求核验。M21 的界面与做题回归已由用户确认通过；该课程流程属于独立的 G3 产品验收。

**建议顺序（2026-10-24，以依赖和验收证据为准）**

1. **M20、M21、M22 已完成**：M20 自动化测试补全；M21 用户确认界面及两站做题回归通过；M22 发布文档与版本收尾。
2. 下一步是独立补齐 **G2/G3 产品验收证据**：真实 shared 课程站内计数对账，以及课程“新建→认证→开始→进度→停止”浏览器链路。
3. 文件争用记录：M8/M9 顺序改 runtime.py；M11 改 bridge.py；M14/M15 扩展 service.py 与 app.py；M17～M19 修改 static。以上任务包均已完成。

### 0.5 代码来源核对：复用了什么、新写了什么

**复用的（没有重写）**

| 对象 | 证据 |
| --- | --- |
| fuckZHS 全部业务 | `vendor/fuckZHS/fucker.py` 原样复制，`UPSTREAM.md` 记录“本地改动：无”；2026-10-24 比对 9 个 vendor 文件与本地来源逐字节一致。不能以全仓网络库关键词为空证明复用，因为做题域本身也会访问网络 |
| 做题链路 | 原有 TaskStore/worker/适配器继续负责做题；WebUI 扩展元信息、混合概览和课程分发。以源码差异和做题契约快照核对，不用固定增删行数作为验收证据 |
| 路径 / 数据目录 | `core/courses/store.py` 用 `core.paths.data_path`，没有用 cwd 推导 |
| 做题的异常类型 | `task_router` 直接 import `core.tasks` 的 `QueueState` / `TaskTransitionError` |
| Cookie 抓取 | 复用 `core/auth/cookie_manager.py` 的浏览器抓取（M12 只加"外部 adapter 入口"，不复制抓取流程） |
| 智慧树 QR 登录与课程目录 | M23 直接调用 vendor `Fucker.login(use_qr=True)`、`getZhidaoList()`、`getHikeList()`、`getZhidaoAiList()`；Cookie 写回改用 Cookie Manager 的同一序列化与原子配置写入 |
| 适配器契约 | 课程侧 `providers/zhihuishu/auth.py` 照 `core/adapters/chaoxing/auth.py` 的属性契约写 |
| 刷课执行入口 | M11 直接调用 vendor `Fucker.fuckZhidaoCourse()` / `fuckHikeCourse()` / `fuckAiCourse()`；不复制业务请求逻辑、不调用自动猜测的 `fuckCourse()` |
| 模型档案 | M14 通过 `ModelProfileStore.resolve_settings()` 读取既有档案；只在运行配置中调用 `aimap.build()`，不修改档案存储、不把密钥写进 job.json |
| 服务与路由 | M15 复用 `CourseJobStore.create/update/archive/delete`、`CourseRuntime.start/stop`、`aimap.build()` 和 DTO；只扩展既有 service/task_router/app 编排，不造第二套存储或执行器 |
| 前端 | M17 新建任务类型分流、M18 混合列表与状态徽章、M19 课程详情已实现；浏览器验收待 M21 |

**新增层的边界**：课程域的独立状态/JSON 存储、DTO、子进程协议与服务编排；这些不是重写刷课业务。每次新增函数仍需逐项检查上游和 AutoLearn 是否已有等价能力，不能以“属于课程域”为所有新代码自动免责。代码行数不是复用程度或完成度的指标。

**本轮消掉的重复**（原本是新代码抄旧代码）

> 这张表就是 §1.1 的来源：它证明"复制改名"会发生，所以必须写成硬规则。

| 重复项 | 处理 |
| --- | --- |
| 原子写 tmp+replace | 抽到 `core/common/fsio.py`；`courses/store.py` 改为 import，删掉本地实现 |
| 时间戳 `_now/_timestamp/_parse_timestamp` | 抽到 `core/common/clock.py`；`courses/store.py` 改为 import |

**已知残留债（待单独决策，不与功能改动混做）**

1. `core/tasks/store.py` 仍有同名时间戳私有函数（与 `clock.py` 完全一致）——删三个函数 + 加一行 import 即可，但属于做题模块。
2. 全仓另有 6 处历史 tmp+replace（`cookie_manager`、`chaoxing/interaction`、`cjyuketang/crawler`×2、`model_profiles`、`solver`×2），迁移需逐一确认语义差异。
3. 站点中文名有两处来源：`app.py` 的 `_SITE_LABELS`（做题）与 `sites.SITES`（课程）。当前键不重叠，未合并；若将来同一站点同时支持两类任务，必须并到一处。

---

### 0.6 本轮缺陷修复与验证（2026-10-24）

| 缺陷 | 修复与回归证据 |
| --- | --- |
| 同根目录多个 CourseJobStore 各持独立锁，交错更新丢字段 | 根路径规范化后共享进程内 RLock；真实双线程交错更新保留标题与计数 |
| 运行目录与删除范围不一致 | 新运行目录统一 jobs/<id>/run；删除同时收纳并清理旧 runs/<id>；删除失败保留记录并支持重试 |
| 启动失败残留站点槽 | _abort 独立释放自己的槽；stdin 写入失败和失败状态落盘异常均验证子进程退出与槽释放 |
| 每轮概览逐任务回读 job.json | 索引补 model_profile_id，旧索引自动重建；复用 dto.summary 序列化索引条目；测试禁止读取 job.json |
| 课程创建参数无校验、模型档案不能传给 fuckZHS | 新增 `service.validate_create()` 与 `aimap.build()`；复用 `ModelProfileStore.resolve_settings()`；12 组合法/12 组非法参数和 5 组 URL 测试 |
| bridge 未按 mode 显式分流、结果未用可信上游统计 | 新增 `bridge.run_course()` 调用三种上游入口，优先读上游返回/context 计数，仅在 AI 进度不可靠时调用上游知识点查询；4 个 mode/time_limit stub 用例通过。真实 shared 课与站内计数需提供课程 ID 后验收 |
| 课程服务与创建入口缺失 | 新增 service 生命周期编排和三条课程路由；`POST /api/tasks` 的 course 分支只写 CourseJobStore，忽略 `page_url`；4 端点各 1 成功/1 失败、homework SQLite 行数不变与跨源拒绝用例通过 |
| 错误响应缺少统一安全消息约束，CourseProtocolError 状态码为 500 | 增加安全文案过滤与固定中文回退；协议错误默认 502；转换/档案/认证错误文案统一中文句号 | 7 类状态与消息矩阵、危险文本回退用例 |
| §5.6 安全十条尚无逐项可执行证据 | 新增 `tests/test_security.py` 十项安全检查，Cookie 仅经子进程 stdin，stderr 复用 M8 脱敏 | 十条检查 + 错误矩阵共 12 项通过 |
| M20 课程自动化缺少集成与稳定性证据 | 新增 stub_fucker 子进程流程、URL 映射用例、五类稳定性回归及 §5.3 契约快照 | 课程测试 250 passed，`core.courses` 覆盖率 80.3%；全量 273 passed |

验证：基础层修复相关回归 **73 passed**；M9 相关回归 **46 passed**；M14 定向用例 **30 passed**（M14 时全量 235 passed）；M15 后全量 **247 passed**；M16 安全测试 **12 passed**，M16 后全量 **259 passed**；M20 后全量 **273 passed**，课程测试 **250 passed / 80.3% 覆盖率**。真实浏览器登录、真实三模式执行和做题双站点完整链路仍未验证，不得据自动化测试标为通过。

---

## 1. 代码来源原则（硬规则，先于所有任务包）

**这一节是整本文档的第一优先级，任何任务卡的提示词与它冲突时，以本节为准。**

### 1.1 三级优先级：能不改就不改，能改就不写

每个改动点在动手前必须按顺序判定，**只有上一级确实不成立才降级**：

| 级别 | 条件 | 做法 | 例子 |
| --- | --- | --- | --- |
| **① 直接复用 fuckZHS** | vendor 里已有完整实现 | **优先直接调用**；允许参数、事件和异常的薄适配，禁止复制函数体后维护“自己的版本” | 共享课/校内课/AI 课：直接 `Fucker.fuckZhidaoCourse / fuckHikeCourse / fuckAiCourse`；进度信号：替换 `fucker.progressBar`；Cookie 结构：照 `Fucker.cookies` setter 的要求喂 |
| **② 复用 AutoLearn 现有代码后修改** | AutoLearn 有同类实现，只是不完全适配 | **改那个函数**（改参数、加分支、抽公共），对外契约尽量不变 | `capture_for_adapter` 抽出 `capture_for_module`；`get_overview` 加课程列表；`renderDetail` 加类型分流；`store.py` 加方法而不是新写一个 store |
| **③ 必须新写** | ①②都不成立（仓库没有等价物，或改它会把语义弄坏） | 新写，且**必须在任务卡「来源」行写明理由** | `dto.py`（课程对象与 TaskRecord 字段集不同）、`aimap.py`（档案格式与上游期望不同）、`bridge.py`（上游没有子进程协议） |

**判断目标是接入改动少、维护成本低，不是机械追求 vendor 零改动。** 当前 vendor 仍原样保留。默认先直接调用，再做薄适配；只有兼容性缺陷无法低成本适配时才比较最小补丁与外围补偿的总代价。若最小补丁更简单，可形成明确的补丁方案、回归用例和 UPSTREAM 记录；不得借机拆分或重写上游业务。本轮没有修改 vendor。

**“部分可复用”应保留可复用部分，只新增缺失的连接逻辑。** 把现有函数复制一份、改两个变量名、挪到新文件——这是**禁止**的。本仓库已经发生过两次（时间戳从 `core/tasks/store.py` 抄到课程存储、原子写实现第 7 份），已在 §0.5 记录并清理，**不要再犯**。

### 1.2 判定的五个问题（答不上来就不许写新代码）

1. 这件事 fuckZHS 有没有现成方法、返回值、context 字段或回调？→ 优先直接利用；接口不同先薄适配，避免为同一能力另建实现。
2. AutoLearn 有没有功能相近的函数/类？→ 有就**进去改**，不要在外面另起一套。
3. 我要新写的这个函数，是不是已有函数的"改名副本"？→ 是就删掉，改成 import + 调用。
4. 我要新建的模块，是不是只是因为"不想动老代码"？→ 是就不是③，是②。
5. 我把改动限制在"改现有函数"里，会不会破坏做题链路？→ 会的话在新文件里**包一层调用**（例如 `task_router` 转发而不是改 `TaskStore`），但仍不复制业务实现。

新增方法包装、统计重算或查询之前，列出“上游方法/字段位置 → 直接复用方式 → 确实缺少的能力”。优先使用上游已有返回值、context 与回调；只包装缺少观测能力的最少方法，不强制把任务卡列出的所有方法都包一遍，不复制上游遍历与请求逻辑。

### 1.3 每个任务包必须标「来源」

§6 每个任务包的元信息下面都有一行 `来源：…`，格式固定为三段的组合：

```text
来源：直接复用 vendor 的 XXX；改 app.py 的 get_overview（加课程列表）；新写 dto.py（理由：字段集不同，无法共用）
```

执行时必须：
- **开工前先复述这一行**（连同你要改的文件清单），确认理解无误再动手；
- 若发现实际可以"少新写、多复用"，**按 §1.1 降级处理并在回写备注里说明**；
- 若发现必须比「来源」行写的新写得更多，**停下来在回复里说明原因**，不要擅自扩大新写范围。

### 1.4 交付时的三行自检（写进回复，不写不算完成）

```text
复用：<直接调用了哪些既有函数/上游方法，带文件:行号>
改写：<改了哪些既有函数，改了什么>
新写：<新增了哪些文件/函数，为什么不复用>
```

评审用下面两条命令抽查"是不是抄的"：

```powershell
# 查找同名工具函数后核对函数体；import 转发/薄包装不是重复实现
rg -n --glob "*.py" "def (_timestamp|_atomic_write|_now)\(" core
# 排查上游业务函数体的复制；同名的纯转发/观测包装需人工判读
rg -n --glob "*.py" --glob "!**/vendor/**" "def (fuck|zhidaoQuery|hikeQuery)" core/courses
```

---

### 1.5 使用规则

1. 默认按任务包推进，避免无关改动混杂；用户明确要求综合修复或文档优化时，可跨包修改必要文件并逐项列证据，不必机械地拆成新会话。
2. **先跑 P0**（§2），让 AI 读到现状与复用清单。
3. **先复述「来源」行再动手**（§1.3）：说不清"复用什么、改什么、为什么新写"就不许写代码。
4. **提示词的「现状」段不许跳过**：它写明哪些已经写好，跳过就会把 `store.py` 的事件系统重写一遍。
5. **回写进度是任务的一部分**，没回写 = 没完成；交付回复必须带 §1.4 的「复用 / 改写 / 新写」三行。
6. 状态四态：`⏸ 未开始`、`◐ 部分完成`、`✅ 完成`、`⚠️ 阻塞`。
7. 默认保持 `core/tasks/**`、`core/adapters/**`、`core/solver/**` 不变；vendor 默认原样保留，例外遵循 §1.1 的最小补丁规则。
8. 不执行 `git commit` / `git push`（用户本人执行）。

---

## 2. P0 全局提示词（每个新会话先跑）

```text
你是 AutoLearn 项目的实施工程师。项目根目录 D:\Ddesktop\AIandCODE\code\AutoLearn，
Windows + PowerShell，Python 3.12 在 .venv（用 .\.venv\Scripts\python.exe）。

本项目正在把 fuckZHS 的网课能力并入 AutoLearn，前半截已经写好了（见 docs/TASKS.md §0 审计）。
本会话只做认知对齐，不改任何文件。

请按顺序读：
1. docs/TASKS.md 的 §1（代码来源原则，硬规则）、§0（现状审计）、§3（复用映射）、§4（进度表）；
2. core/courses/ 下 protocol.py、domain.py、store.py 的公开方法清单；
3. core/webui/task_router.py；
4. docs/architecture.md。

读完后 15 行内回答：
- 课程任务从创建到详情接口，链路上断在哪几处；
- 按 §1.1 的三级优先级，如果要补上这几处，每一处应该"直接复用 / 复用后修改 / 必须新写"哪一类；
- store.py 已提供哪些能力（列方法名），哪些是要新增而不是重写的；
- 进度表里下一个"依赖已满足"的任务包是哪个。
```

---

## 3. 复用映射表（**先查这张表再写代码**）

| 要做的事 | 已经有实现 | 怎么用 |
| --- | --- | --- |
| 原子写文件（JSON / 文本） | `core/common/fsio.py` 的 `atomic_write_json()` / `atomic_write_text()` | 直接 import。全仓历史上散落 7 份 tmp+replace，**新代码一份都不许再写** |
| 记录时间戳（UTC、毫秒、`Z`） | `core/common/clock.py` 的 `timestamp()` / `parse_timestamp()` / `utc_now()` | 直接 import；`core/tasks/store.py` 里还有一份同名私有实现，迁移是独立改动 |
| 拉起子进程 / 逐行切 JSONL | `core/courses/runtime.py` 的 `spawn()` / `iter_jsonl()` | 直接调，别重写 subprocess 参数 |
| 事件字段校验、数值夹取、大小限制 | `core/courses/protocol.py` 的 `validate_event()` / `MAX_*` | 直接调，别自己判字段 |
| 状态机与迁移合法性 | `core/courses/domain.py` 的 `ALLOWED_TRANSITIONS` / `ensure_transition()` | 迁移前必调 |
| 原子写、索引、事件落盘、进度合并节流 | `core/courses/store.py` 的 `_atomic_write` / `_rebuild_index` / `append_runtime_event` / `_should_merge_progress` | **全部已存在**，只加新方法 |
| 任务 ID（`course-` + 32 hex） | `domain.job_id()`；路由侧校验见 `task_router.COURSE_ID_PATTERN` | 不要另造 ID 格式 |
| 任务分发（前缀判定 / 取 job / 六转发） | `core/webui/task_router.py` | 已接好；dto 已落地，只需补它转发的动作方法（`stop` 等） |
| 摘要字段与脱敏 | `app.py` 的 `_task_json()` / `_safe_title()` / `_URL_IN_TITLE` | DTO 对齐字段契约，复用已有序列化/脱敏能力，禁止复制同类业务 |
| 业务事件列表格式 | `app.py` 的 `_task_events_json()` | DTO 对齐它 |
| 浏览器抓 Cookie（可见浏览器 + 临时 profile） | `core/auth/cookie_manager.py` 的 `capture_for_adapter()` / `capture_for_module()` | 外部入口已落地（M12），抓取逻辑全复用 |
| 适配器认证契约样板 | `core/adapters/chaoxing/auth.py` | 课程侧 `zhihuishu/auth.py` 已按它写好 |
| Cookie 读取与解析 | `core/credentials.py` 的 `LocalCredentialProvider` | 加一行白名单 |
| 用户数据 / 资源路径 | `core/paths.py` 的 `data_path()` / `resource_path()` | 禁止用 cwd 推导 |
| config.json 原子更新 | `cookie_manager._atomic_write_config()` | 别自己写 json.dump |
| 刷课 / 校内课 / AI 课业务 | `vendor/fuckZHS/fucker.py` 的 `fuckZhidaoCourse` / `fuckHikeCourse` / `fuckAiCourse` | **直接调用**，禁止重写或拆分 |
| 播放进度信号 | `vendor` 里 `fucker.py` 的模块级 `progressBar`（上游 `from utils import progressBar`） | 替换 `fucker.progressBar` |
| Cookie 结构要求 | `Fucker.cookies` setter（要 `CASLOGC` 且能解出 uuid） | 启动前预检 |
| 前端卡片/徽章/事实行/按钮/事件卡/步骤条 | `app.js` 的 `card()` / `facts()` / `statusBadge()` / `actionRow()` / `renderEventCard()` / `renderStepper()` | 复用结构，别新造组件 |
| 前端轮询 | `app.js` 的 `refreshOverview()` / `refreshDetail()`（1.6s） | 只改字段，不动间隔 |
| 错误 → HTTP 状态码 | `app.py` 的 `@app.exception_handler(...)` | 课程统一走 `CourseError` |

---

## 4. 进度表

> 人工维护的进度看板；状态须有源码和可重现验收证据支持。每次实施/审计同步该行与小结，事实源优先级见 §7。

**进度：22 完成 · 1 部分 · 0 未开始 · 0 阻塞 · 最后更新 2026-10-24（M21/M22 完成；M23 静态检查通过，真实扫码目录待验收）**

| ID | 任务包（合并后） | 含原子任务 | 依赖 | 人日 | 状态 | 备注 |
| --- | --- | --- | --- | --- | --- | --- |
| M1 | 地基与通道验证 | T0-01～05 | — | 3.5 | ✅ 完成 | protocol/errors/sites/vendor/依赖/spawn+iter_jsonl 落地 |
| M2 | 后端契约与分发 | T1-01/02/04/05 | M1 | 3.75 | ✅ 完成 | meta+task_type+CourseError+6 处分发+task_router |
| M3 | 概览合并与做题快照 | T1-03/06 | M2, M5 | 1.5 | ✅ 完成 | 混合概览倒序；五接口快照；正常轮询只读索引 |
| M4 | 领域与存储 | T2-01/02 | M1 | 2.5 | ✅ 完成 | 领域/原子写/索引/事件；多实例交错更新回归 |
| M5 | DTO 与事件出口 | T2-03/04 | M4 | 2.25 | ✅ 完成 | dto.py 落地，详情接口 200，18 键校验通过 |
| M6 | 生命周期治理 | T2-05/06/07 | M4 | 1.75 | ✅ 完成 | 归档/新旧运行目录清理/槽/恢复及失败重试 |
| M7 | 运行时起停与环境 | T3-01/02/06/07 | M6 | 3.25 | ✅ 完成 | 起停/关机清理；启动失败释放槽含异常回归 |
| M8 | 双通道与事件泵 | T3-03/04 | M7 | 2.25 | ✅ 完成 | 2026-10-24：stdout/stderr 双泵、脱敏、排空与截断；10 万行验收，protocol_errors=100 |
| M9 | 终局裁定 | T3-05 | M8 | 1.0 | ✅ 完成 | 7 类终局裁定+watcher 超时；槽释放与事件收尾 |
| M10 | bridge 与进度适配 | T3-08/09 | M8, M13 | 2.75 | ✅ 完成 | 2026-10-24：stdout JSONL、可注入执行边界与 stub 进度事件通过 |
| M11 | 执行分流与统计 | T3-10 | M10, M14 | 1.0 | ✅ 完成 | 2026-10-24：shared/hike/ai 显式分流；上游可靠进度优先、time_limit 分类；3 模式 stub 通过；真实 shared 站点计数待课程 ID 验收 |
| M12 | 认证链路 | T4-02/03/04/05 | M2 | 3.25 | ✅ 完成 | 抓取入口/凭据白名单/预检/service 认证落地，409 语义齐 |
| M13 | 运行目录与终端补丁 | T4-01/08 | M1 | 1.25 | ✅ 完成 | vendor 指纹复制/终端 shim/任务内运行目录 |
| M14 | 参数校验与档案映射 | T4-06/07 | M4 | 2.0 | ✅ 完成 | 2026-10-24：Chat Completions 档案映射；12 组合法/12 组非法参数 + 5 组 URL 用例 |
| M15 | 服务与课程路由 | T5-01/02 | M9, M11, M12, M14 | 2.0 | ✅ 完成 | 2026-10-24：生命周期服务/三条路由/创建分流落地；4 端点各成功与失败用例、tasks.sqlite3 行数不变、跨源 POST 拒绝；全量 247 项通过 |
| M16 | 错误与安全收口 | T5-03/04 | M15 | 1.5 | ✅ 完成 | 7 类错误状态/安全消息矩阵 + 安全十条逐项测试；`tests/test_security.py` 12 passed；全量 259 passed |
| M17 | 前端新建任务 | T6-01～05 | M2 | 3.75 | ✅ 完成 | 2026-10-24：新建弹窗支持做题/网课分流、课程参数、站点能力过滤与左栏类型筛选；课程请求不带 page_url |
| M18 | 前端列表与徽章 | T7-01/02/03 | M17 | 2.25 | ✅ 完成 | 2026-10-24：混合列表显示类型/课程阶段/进度；课程状态与删除运行记录分支，签名包含动态字段 |
| M19 | 前端课程详情 | T7-04～08 | M18, M5, M11, M15, M16 | 4.0 | ✅ 完成 | 2026-10-24：课程详情阶段/信息/进度/状态操作；刷新签名覆盖课程进度字段，跳过做题专用请求 |
| M20 | 自动化测试补全 | T8-01～05 | M5, M9, M11, M14, M16 | 5.25 | ✅ 完成 | 2026-10-24：全量 273 passed；课程 250 passed，覆盖率 80.3%；8 类运行终局 + 5 类稳定性 |
| M21 | 界面自检与做题回归 | T8-06/07 | M19, M20 | 2.25 | ✅ 完成 | 2026-10-24：用户确认界面截图/断言、契约核对与超星、雨课堂链路全部通过；截图不入库。见回归记录 |
| M22 | 文档与收尾 | T9-01～04 | M21 | 2.0 | ✅ 完成 | 2026-10-24：架构、README、变更记录、版本与任务书状态收尾；版本 v1.1.0 |
| M23 | 登录后选课与上游二维码 | 新增流程调整 | M12, M13, M15, M17, M19 | 待估 | ◐ 部分完成 | 2026-10-24：空草稿/二维码/目录选择已接通；静态检查通过，待真实扫码验收 |

---

## 5. 契约（权威定义在代码，这里只列不许改的部分）

### 5.1 摘要字段（18 键，一多一少都算破坏契约）

> 2026-10-23 校正：旧文写「19 键」是笔误，`dto.SUMMARY_FIELDS` 实测 18 个，与下面枚举、`app._task_json()` 一致。

`task_id / task_type / site_id / title / authentication_hint / model_profile_id / run_id / current_step / status / created_at / updated_at / last_safe_error_category / last_operation / interrupted_operation / pending_operation / interaction_retry_authorized / queue_state / queue_position`

课程固定值：`task_type="course"`、`run_id=null`（运行标识放 `course.run_id`）、`queue_state=null`、`queue_position=null`、`interaction_retry_authorized=false`。

### 5.2 详情外壳

`{"task": {...}, "cookie_status": {"captured": bool, "configured": bool}, "queue": null, "events": [...], "course": {...}}`

`course`：`mode / course_id / class_id / course_name / phase / current_title / progress_percent / completed_count / total_count / failed_count / run_id / options{speed,threshold,time_limit_minutes,process_exam}`

### 5.3 事件

以 `core/courses/protocol.py` 为准（六种 type、`MAX_LINE_BYTES=64KB`、`MAX_EVENTS_PER_JOB=500`、`PROGRESS_MIN_INTERVAL_MS=500`）。**没有合法 `result`：退出码 0 → `failed(no_result)`，退出码非 0 → `failed(process_exit)`；取消/超时优先级见 M9。`stats_mismatch` 暂只记父进程内部诊断，不是现有 result 白名单字段，不得假定它能到达前端。**

### 5.4 状态

以 `core/courses/domain.py` 的 `CourseStatus` / `ALLOWED_TRANSITIONS` 为准，不得新增状态；阶段 `setup → authentication → catalog → learning → exam → result`（`exam` 仅 AI 课程且 `process_exam=true`）。

### 5.5 接口

| 方法 | 路径 | 归属 |
| --- | --- | --- |
| GET | `/api/meta`（已含 `task_types`） | M2 ✅ |
| GET | `/api/overview`（需合并课程） | M3 |
| GET | `/api/tasks/{id}`、`/api/tasks/{id}/runtime-events` | M2/M5 分发与读取 |
| POST | `/api/tasks/{id}/authenticate`、`/stop`、`/archive` | M2 分发；动作见 M6/M7/M12/M15 |
| GET | `/api/tasks/{id}/course/authentication`、`/course/catalog`；POST `/course/select` | M23 二维码状态、目录抓取与选课 |
| DELETE | `/api/tasks/{id}` | M2/M6 分发与删除 |
| POST | `/api/tasks`（`task_type` 缺省 homework） | M15 |
| POST | `/api/tasks/{id}/course/start`、`/course/rerun` | M15 |
| PUT | `/api/tasks/{id}/course-settings` | M15 |

### 5.6 十条硬约束（违反必返工）

1. 智慧树不得进 `core/adapters/`（`_discover_site_ids` 会把站点拉进做题流程）。
2. 课程不得写 `tasks.sqlite3`。
3. 进度变化必须刷新 `job.updated_at`（前端 revision 依赖它）。
4. bridge 必须 `dup(1)` 当事件通道、`dup2(2,1)` 把 stdout 导向 stderr（上游满屏 `print`）。
5. 必须把 vendor 复制到 `run/upstream` 再 import（`logger.py` 导入时按 `utils.py` 目录建 `./logs`）。
6. `fuckAiCourse()` 的 `os.get_terminal_size()` 无兜底（fucker.py:1279），bridge 必须补丁。
7. `fuckZhidaoCourse` / `_traverse` 可能吞异常后正常 return，退出码不是业务成功依据；优先复用可靠返回值/context/已有统计，只补缺失的结果归并。
8. `TimeLimitExceeded` 是 `TimeoutError` 子类 → 映射 `partial`。
9. `Fucker.cookies` setter 要求 `CASLOGC`，缺失抛 `ValueError("Cookies invalid")`。
10. 上游拼 `{baseUrl}/v1/chat/completions`，档案 `base_url` 是完整 endpoint → 必须推导 `api_base`。

---

## 6. 任务包

> 已完成包的任务卡保留原始实施上下文，不得把历史“缺少某文件/方法”当作当前事实。开工以 §0、§4 和源码为准；已有能力只复核和修补，不重新实现。当前测试基线见 §0.6，旧行数/测试条数不作为验收门槛。

### M1 地基与通道验证 ✅

`3.5 人日 · 已由 protocol.py(215) / errors.py(61) / sites.py(71) / vendor 9 文件 / requirements 5 包 / runtime.spawn+iter_jsonl 落地`

`来源：本包已完成——protocol/errors/sites 为既有实现，vendor 为上游原样复制；不新写任何业务。`

```text
【M1 复核（已完成，仅在怀疑有偏差时运行）】
复核 core/courses/protocol.py、errors.py、sites.py 是否满足 docs/TASKS.md §5.3/§5.4 与 §5.6 第 1 条：
- EVENT_TYPES 是否六种、validate_event 对畸形输入返回 None 而不抛异常；
- sites.SITES 是否是课程站点的唯一硬编码点（全仓 grep "zhihuishu" 只应出现在 sites/auth/vendor 三处）；
- requirements.txt 与 pyproject.toml 的依赖集合是否一致。
只读不改，输出三条结论 + 有偏差时给出具体行号。

回写进度：无偏差则保持 M1 为 ✅（备注加"复核通过 YYYY-MM-DD"）；有偏差改 ⚠️ 并写明行号。
```

### M2 后端契约与分发 ✅

`3.75 人日 · 已由 app.py（meta/task_type/CourseError/6 处分发）+ task_router.py(284) + test_task_router(362) 落地`

`来源：本包已完成——改 app.py（加分支与字段）、新写 task_router.py（仓库无分发层）；复用 TaskStore 异常类型。`

```text
【M2 复核（已完成，仅在怀疑有偏差时运行）】
复核：① GET /api/meta 是否返回 task_types 与 adapters[].task_types，且智慧树不在做题站点；
② 六个通用接口是否都有 has_course_prefix 分支且做题分支未被改动；
③ task_router 的 course- 校验是否同时校验 32 位 hex 形状（防伪造）。
只读不改，输出结论；顺带列出当前未接通动作（目前 stop 尚未接入 runtime；archive/delete/authenticate 已有实现，必须以源码核实）。

回写进度：保持 M2 ✅ 或按结论改 ⚠️。
```

### M3 概览合并与做题快照 ✅

`1.5 人日 · 角色 BE+QA · 依赖 M2、M5 · 涉及 app.py、tests/snapshots/`

`来源：改 app.py 的 get_overview（复用 _task_json 字段与脱敏）；复用 dto.summary、TaskStore.list_tasks；新写仅快照测试。`

```text
【M3 概览合并与做题快照】
原始实施前现状（当前实现见 §0/§4）：GET /api/overview（app.py:427）只返回 TaskStore 的做题任务，课程任务进不了前端列表。
      core/courses/store.py 已有 list_summaries(include_archived)；dto 由 M5 提供。

做两件事：
1. 合并列表：get_overview() 取做题摘要 + [dto.summary(job) for job in CourseJobStore().list_summaries(include_archived)]，
   合并后按 updated_at 倒序（同格式字符串可直接比较）。queue 只保留做题队列，active_task_id 仍只表示做题 worker。
   课程存储读取整体 try/except：出错返回空列表并记 warning，绝不能让做题列表一起 500。
2. 做题契约快照：用隔离 LOCALAPPDATA（指向 %TEMP%，不要指工作区，SQLite 会只读报错）起服务，造 2 条做题草稿，
   抓 /api/meta、/api/overview、/api/tasks、/api/tasks/{id}、/runtime-events 响应，脱敏后存 tests/snapshots/homework_*.json；
   写 tests/test_api_contract.py 断言除 task_type 外结构与值不变（时间/UUID 归一化）。

复用：dto.summary 用 M5 的；摘要统一复用 dto.summary 的白名单与脱敏，不另写一套。
禁：不改 TaskStore；不给课程加队列表；快照里不得出现 cookie / api_key / 完整 URL。
验收：pytest tests/test_api_contract.py -q 全绿；curl /api/overview 同时看到两类任务且倒序正确；
     把 courses 目录临时改名后接口仍 200。

回写进度：§4 的 M3 → ✅ / 日期 / 一行摘要；未过则 ⚠️ 写卡点。同时更新表头「进度：x 完成 · y 部分 · z 未开始」。
```

### M4 领域与存储 ✅

`2.5 人日 · 已由 domain.py(153) + store.py(699) + test_domain(161) + test_event_log(213) 落地`

`来源：本包已完成——课程域新写（做题域是 SQLite，无等价物）；原子写/时间戳已改回复用 core.common。`

```text
【M4 复核（已完成，仅在怀疑有偏差时运行）】
复核：① domain.ALLOWED_TRANSITIONS 是否覆盖 §5.4 全部状态且无自环泄漏；
② store 原子写是否 tmp + os.replace + fsync；③ 事件文件是否限 500 行、sequence 连续、
   同 stage 的 progress 是否 500ms 合并；④ index.json 损坏时能否重建。
只读不改，输出四条结论。

回写进度：保持 M4 ✅ 或改 ⚠️。
```

### M5 DTO 与事件出口 ✅

`2.25 人日（事件日志已完成，剩 DTO）· 角色 BE · 依赖 M4 · 新增 core/courses/dto.py`

`来源：复用 app.py 的 _task_json/_task_events_json/_safe_title 与 store.read_business_events；新写 dto.py（理由：CourseJob 与 TaskRecord 字段集不同，无法共用序列化）。`

```text
【M5 课程 DTO（补完最后一块）】
原始实施前现状（当前实现见 §0/§4）：双事件日志已在 store.py 完整实现（append_business_event / append_runtime_event / read_business_events /
      read_runtime_events / _should_merge_progress），不要再动。
      缺的是 core/courses/dto.py —— task_router._course_dto() 已按模块名 core.courses.dto 延迟导入，
      文件一落地，GET /api/tasks/{course-id} 立刻从 501 变 200。

新建 core/courses/dto.py，只做序列化，不读写存储：
- summary(job) -> dict：严格 §5.1 的 18 键；task_type="course"、run_id=None、queue_state/queue_position=None、
  interaction_retry_authorized=False、title 走脱敏（http(s)://… → "网址已隐藏"）、
  authentication_hint 取 sites.auth_hint(job.site_id)
- detail(job, cookie_status=None, events=None) -> dict：{"task","cookie_status","queue":None,"events","course"}
- course_block(job) -> dict：§5.2 字段，options 只放 speed/threshold/time_limit_minutes/process_exam
- events(job, limit=100) -> list：event_id/task_id/occurred_at/event_type/from_status/to_status/operation/safe_error_category
- 常量 SUMMARY_KEYS：frozenset，测试拿它比对

复用：对齐 app.py 的字段和安全契约，优先复用已有 dto 序列化，不复制同类业务；job 字段名照 domain.CourseJob。
禁：不返回 cookie / api_key / 完整 URL；不用 asdict 直接丢；不改 store.py。
验收：造 1 条 job，用 python -c 打印 sorted(dto.summary(job).keys()) 与 SUMMARY_KEYS 一致；
     起服务 GET /api/tasks/course-<32hex> 返回 200 且含 course 块。

回写进度：§4 的 M5 → ✅ / 日期 / 摘要（"dto.py 落地，详情 200"）；更新表头计数。
```

### M6 生命周期治理 ✅

`1.75 人日 · 角色 BE · 依赖 M4 · 涉及 store.py、app.py 的 lifespan`

`来源：改 store.py 加 4 个方法，复用 _atomic_write/_rebuild_index/ensure_transition；删除回滚照 tasks/store.py 的 rename→rmtree 做法改。`

```text
【M6 生命周期治理：单实例槽 / 归档删除 / 崩溃恢复】
原始实施前现状（当前实现见 §0/§4）：store.py 已有 _atomic_write、_ensure_runtime_file、runtime_path、_rebuild_index 这套底座，
      但没有 archive / delete / 槽 / 恢复方法；task_router 已经会转发 archive/delete（方法不存在时报 501）。

在 CourseJobStore 补四个方法（全部复用现有 _atomic_write 与索引逻辑）：
1. archive(job_id)：ensure_transition(status, archived) → 写 job.json + business event "course_archived" → 索引标 archived。
2. delete(job_id)：running/stopping 抛 CourseBusy；否则 jobs/<id> → jobs/.trash-<ts>-<id> → rmtree，
   失败 rename 回来再抛；完成后 _rebuild_index()。
3. acquire_slot(site_id, job_id, pid) / release_slot(site_id)：读写 runtime.json（已有 _ensure_runtime_file），
   槽存 job_id/pid/since；pid 已死（os.kill(pid,0) 或 psutil，不引第三方）→ 覆盖并记 "stale_slot_reclaimed"。
4. recover_on_start()：running/stopping/authenticating → failed + "process_exit"，记 "recovered_after_restart"，
   清空 runtime.json 的 active；index 与 job.json 不一致时以 job.json 为准。
然后在 app.py 的 _lifespan 里、TaskStore 之后调用 recover_on_start()，异常只记日志不能让服务起不来。

复用：事件写法照 store.append_business_event；迁移必调 ensure_transition；pid 判定用标准库。
禁：不动做题侧恢复逻辑；不在 running 状态强删；不加数据库。
验收：pytest tests/courses -q 仍全绿，并新增 3 条测试（archive 后 overview 不返回 / running 删除被拒 /
     running 的 job 重启后变 failed 且有恢复事件）。

回写进度：§4 的 M6 → ✅ / 日期 / 摘要；更新表头计数。
```

### M7 运行时起停与环境 ✅

`3.25 人日 · 角色 BR · 依赖 M6 · 涉及 runtime.py`

`来源：改 runtime.py 的既有 spawn/iter_jsonl；复用 domain 状态与 store.update；进程管理用标准库，不另造执行器。`

```text
【M7 CourseRuntime：起停、环境、停止、关机清理】
原始实施前现状（当前实现见 §0/§4）：runtime.py 只有 spawn() 与 iter_jsonl()（65 行），别改它们的签名——后续都会直接调。

在 runtime.py 增加 CourseRuntime（复用 spawn/iter_jsonl）：
- class CourseRun：job_id/site_id/proc/run_id/started_at/threads/cancel_event/event_seq/counts
- start(job, params)：acquire_slot 失败抛 CourseBusy；生成 run_id；状态 running、phase setup；写 job.json +
  business event "course_started"；spawn 时固定：cwd=data_path("courses","jobs",job_id,"run")，
  env={**os.environ, PYTHONPATH=<run>/upstream, PYTHONUTF8=1, PYTHONIOENCODING=utf-8, COLUMNS=80, LINES=24}，
  argv=[sys.executable,"-X","utf8",bridge]，CREATE_NO_WINDOW。
- stop(job_id) -> bool：running→stopping（写盘 + 事件）→ terminate → 等 5s → kill → 再等 2s；
  只有确认退出才 True；仍未退出则 failed("process_kill_failed")。
- shutdown_all(timeout_seconds=8)：逐个走 stop 流程，终态 cancelled + "service_shutdown"；单个失败不影响其它。
- 在 app.py 的 lifespan finally 里、TaskStore 关闭**之前**调 shutdown_all，异常只记日志。

复用：domain 的状态枚举、store.update 写盘、spawn 起进程（不要自己拼 subprocess 参数）。
禁：不在事件循环里阻塞等进程（用线程）；Cookie 不进环境变量或命令行（只走 stdin）。
验收：用 echo_bridge 起 1 个 run，stop 后 <8s 变 cancelled 且无残留进程；threading.active_count() 回基线；
     连点两次 start 第二次抛 CourseBusy。

回写进度：§4 的 M7 → ✅ / 日期 / 摘要；更新表头计数。
```

### M8 双通道与事件泵 ✅

`2.25 人日 · 角色 BR · 依赖 M7 · 涉及 runtime.py · 2026-10-24 已完成`

`来源：扩展既有 runtime 的事件接线、队列消费和 stderr 脱敏；复用 iter_jsonl/protocol.validate_event/store.append_runtime_event，不另建协议、存储或业务执行器。`

```text
【M8 双通道 + 事件泵】
现状：iter_jsonl 已能把 stdout 切成 dict；protocol.validate_event 已能判违约；store.append_runtime_event 已含节流。
      缺的是把两者串起来的常驻消费者，以及 stderr 脱敏通道。

在 runtime.py 补：
1. stdout 泵：复用 iter_jsonl → validate_event。iter_jsonl 当前静默丢弃坏 JSON，允许加可选 on_invalid 回调（默认行为不变）报告非法行长度；解析失败与字段违约统一计入 protocol_errors，不保存原文；
   合法 → 更新内存态（phase/current_title/counts/percent）→ store.append_runtime_event（内部已 500ms 合并）
   → **每次刷新 job.updated_at**（§5.6 第 3 条）→ phase 变化补一条 "course_phase_changed"。
   计数为空时沿用上一条，不归零。
2. stderr 泵：逐行 → 脱敏 → 追加 jobs/<id>/diagnostics.log（≤2MB，超限截断写 "log truncated"）。
   脱敏至少覆盖 CASLOGC、cookie=…、Authorization、Bearer <token>、≥32 位 base64/hex 串、http(s) URL 的 query。
3. stdout/stderr 两个 daemon 读线程只入有界队列；单独消费者统一更新内存与落盘；扩展既有 _watch 等待退出、排空与限时 join，不在 WebUI 事件循环中阻塞，不丢终局 result。

复用：不重写节流（_should_merge_progress）；不自己判字段（validate_event）。
禁：stderr 任何一行都不许进前端事件；不在读线程里落盘（交给队列消费者）。
验收：echo_bridge 灌 10 万行 stdout（含 100 行坏 JSON）+ 1 万行含假 Cookie 的 stderr：
     无死锁、protocol_errors≈100、diagnostics.log 里 Cookie 已脱敏、runtime-events 无畸形项。

回写进度：§4 的 M8 → ✅ / 日期 / 摘要；更新表头计数。
```

### M9 终局裁定 ✅

`1.0 人日 · 角色 BE · 依赖 M8 · 涉及 runtime.py`

`来源：改 runtime.py 加 finalize；复用 domain.ensure_transition 与 store.update；无新模块。`

```text
【M9 终局裁定】
现状：上游吞异常、退出码恒为 0（§5.6 第 7 条），终态必须父进程裁定。

在 runtime.py 实现 finalize(run, exit_code, had_result, result_payload)：
| result=completed → completed / null
| result=partial → partial / result 分类或 "time_limit"
| result=failed → failed / result 分类
| 无 result 且 exit 0 → failed / "no_result"
| 无 result 且 exit≠0 → failed / "process_exit"
| cancel_event 已置位 → cancelled / null
| 等进程超时（limit+10 分钟缓冲） → failed / "process_timeout"
裁定优先级：用户取消 > 父进程超时 > 最后一个合法 result > 退出码兜底；time_limit_minutes=0 表示无业务时限，不得在 10 分钟后误杀无限时任务。
收尾顺序固定：进程确认退出 → 读完剩余输出并限时 join 读线程 → 消费队列至空并 join 消费者 → release_slot → 写 job.json（status/phase=result/counts/updated_at）
→ business event "course_finished"（带 from/to）→ 清理 run/upstream（保留 diagnostics 与事件文件）。

复用：domain.ensure_transition 兜非法迁移；store.update 写盘。
禁：有 result 时不得用退出码覆盖；cancelled 不得记成 failed。
验收：7 种情形各一条 stub 用例（tests/courses/test_runtime_integration.py），断言终态 + 分类 + 槽已释放；连同 M8 的畸形事件用例形成 G2 所需的 8 类证据，不推迟到 M20 才首次实现。

回写进度：§4 的 M9 → ✅ / 日期 / 摘要；更新表头计数。
```

### M10 bridge 与进度适配 ✅

`2.75 人日 · 角色 BR · 依赖 M8、M13 · 涉及 providers/zhihuishu/{bridge,progress}.py`

`来源：直接复用 vendor——替换 fucker.progressBar、包装 Fucker 实例方法；bridge/progress 仅作胶水，禁止复制上游函数体。`

```text
【M10 bridge 骨架 + 进度适配】
现状：vendor 里 fucker.py 原样保留；provider 下已有 auth.py/preflight.py，没有 bridge.py/progress.py。
      progressBar 是 fucker 模块级名字（上游 `from utils import progressBar`），替换即可。

A. bridge.py（启动顺序是硬要求）：
1. sys.stdout.flush() → events=os.fdopen(os.dup(1),"w",encoding="utf-8",buffering=1) → os.dup2(2,1)
   （此后 print 流向 stderr，只有 events.write 进协议通道）
2. stdin 读一行 JSON：{job_id, run_id, mode, course_id, class_id, speed, threshold, time_limit_minutes,
   process_exam, ai_config, cookies}
3. sys.path.insert(0, run/upstream)；import fucker；logger.setLevel("WARNING")
4. 发 hello{"version":1,"mode":...,"upstream_version":<meta.json>}
5. 执行边界先做成可注入 callable，用 stub 验证；M11 再接上游 mode 分流，不要求 M10 调尚不存在的方法；未捕获异常 → 发 result{failed} + exit 1
6. finally events.flush()

B. progress.py：
- install(events, state)：替换 fucker.progressBar。签名必须兼容上游调用
  progressBar(s, e, prefix=..., suffix="done", progressbar_view=...)（含 progressbar_view 关键字）→ 发 progress 事件；
  按 prefix 判阶段（"answering quiz"/"fucking exam" → exam，其余 learning）
- 先核查返回值/context/回调能否提供所需信号；确实不足才包装最少的实例方法（不改类、不复制函数体）。候选：fuckZhidaoVideo / fuckHikeVideo / fuckAiVideo / _traverse。
  进入前发 current{stage,title}（标题从 context 取：共享课 context[RAC_id].videos[id].name、校内课 node.name、
  AI 课 knowledge.knowledgeName；取不到退化成 id，不要编），退出 completed+=1，异常 failed+=1（只记异常类型名）
- 同 title 不重复发；每秒最多 1 条 current

复用：spawn/读取用 runtime.py；不复制上游业务代码。
禁：bridge 不 import FastAPI、WebUI 或父进程存储/服务；可导入同目录的薄适配与 M13 的终端补丁；复用 preflight 时须显式加入可导入 core 的只读代码路径（不改变 run 工作目录、不依赖 cwd），不能复制终端 shim。子进程继承既有 Python 依赖；不能误写成“只有标准库”。cookies 不进任何日志。
验收：echo 一行参数给 bridge（给不存在的 course_id），先 hello 再 failed result，stdout 严格是 JSONL；
     用 stub fucker 跑假流程能看到 current/progress 递增。

回写进度：§4 的 M10 → ✅ / 日期 / 摘要；更新表头计数。
```

### M11 执行分流与统计 ✅

`1.0 人日 · 角色 BR · 依赖 M10、M14 · 涉及 bridge.py`

`来源：直接调用 vendor 的 fuckZhidaoCourse/fuckHikeCourse/fuckAiCourse 与 Fucker.context；新写只有 mode 分流与统计重算（上游无对外统计）。`

```text
【M11 按 mode 分流 + 结果统计重算】
现状：上游有 fuckCourse() 的正则自动判断，但我们不用——前端显式传 mode，避免 course_id 格式变化误分发。

在 bridge.py 实现 run_course（Fucker 构造：cookies 注入、speed、end_thre=threshold、limit=time_limit_minutes、
tree_view=False、progressbar_view=True；ai 模式先装 M13 的终端补丁）：
- shared → fucker.fuckZhidaoCourse(course_id)
- hike   → fucker.fuckHikeCourse(course_id)
- ai     → fucker.fuckAiCourse(int(course_id), int(class_id), ai_config, no_exam=not process_exam)
跑完优先读取上游返回值/context/已有统计，不数 progressBar 次数；下列是需要核对上游字段的候选规则，不照抄上游遍历/请求实现：
- shared：total=len(ctx.videos)，completed=sum(watchState==1)
- hike：递归 ctx.root 数 dataType==3 的文件，completed=studyTime>=totalTime*end_thre
- ai：先检查本次返回值/context 是否已有最新 knowledgeList；只有缺少可靠进度时才调用上游 getAiKnowlegePoints 补查，不自行实现请求
- 与桥接内部计数不一致：以可靠上游进度为准，failed 取桥接计数；stats_mismatch 仅作为内部诊断，不向现有 result 白名单塞新字段
兜底裁定：全完成→completed；有完成有失败→partial；全失败→failed；TimeLimitExceeded→partial+"time_limit"。

复用：上游方法直接调；Cookie 校验已在父进程 M12 做过，不重复。
禁：不调 fuckCourse()/fuckWhatever()/main.py；不在 bridge 写 job.json。
验收：三种 mode 各用 stub 跑一次断言 result；真实 shared 课跑一节，completed_count 与站内一致。

回写进度：§4 的 M11 → ✅ / 日期 / 摘要；更新表头计数。
```

### M12 认证链路 ✅

`3.25 人日（auth.py 已完成，剩 3 块）· 角色 BE+BR · 依赖 M2 · 涉及 cookie_manager.py、credentials.py、preflight.py、service.py`

`来源：改 cookie_manager（capture_for_adapter 函数体原样搬进 capture_for_module）+ credentials 加一行；zhihuishu/auth.py 已按 chaoxing 契约写好（复用）；新写仅 check_cookies 与 service.authenticate。`

```text
【M12 认证链路补完】
原始实施前现状（当前实现见 §0/§4）：core/courses/providers/zhihuishu/auth.py 已按 core/adapters/chaoxing/auth.py 的样板写好
      （SITE_ID/CONFIG_SECTION/URL_FIELD/COOKIE_FIELD/COOKIE_VALUE_TYPE/PROFILE_NAME/ALLOWED_COOKIE_DOMAINS/
       CAPTURE_START_URL/CAPTURE_HINT/is_authenticated/serialize_cookies），不要再改它。
      还差：抓取入口、凭据白名单、预检、服务编排。

1. cookie_manager.py 重构（做题行为零变化）：
   - 新增 capture_for_module(adapter, page_url=None, *, status_callback, cancel_event)：把 capture_for_adapter
     的函数体搬进来，adapter 由调用方传入；抽出 _validate_module(adapter) 做原来那 7 个必需属性 +
     可选 CAPTURE_START_URL 校验
   - capture_for_adapter(site_id, ...) 变成 _load_adapter(site_id) → capture_for_module(...)
   - 保留 _adapter_config 的严格语义（缺 section 仍报错，文案不变），不在这里自动建段
2. credentials.py：_SITE_COOKIE_FIELDS 增 "zhihuishu": ("zhihuishu", "cookie")，解析逻辑一行不改。
3. providers/zhihuishu/preflight.py 新增 check_cookies(cookies)：缺 CASLOGC 或解不出 uuid → CourseAuthRequired，
   消息里不带 cookie 值。
4. courses/service.py 新增 authenticate(job_id)：确保 config.json 有 zhihuishu 段（缺则用 cookie_manager 的原子写
   合并空对象，保留其它键）→ capture_for_module(zhihuishu auth, page_url="https://www.zhihuishu.com/")
   → authenticating → ready；失败回 draft + "auth_failed"。
5. task_router._COURSE_METHODS["authenticate"] 现在指向 store；改成指向 service（推荐），
   让 CourseJobStore 不承担认证职责。

复用：抓取、profile 建删、config 原子写全部复用 cookie_manager，别重写浏览器代码。
禁：不让智慧树进 core/adapters；auth.py 里不写 config.json；不放宽既有 cookie 校验。
验收：① 真实浏览器抓一次智慧树 Cookie 写入 config.json；② 超星抓 Cookie 仍可用；
     ③ 清空 cookie 后 authenticate 返回 409 且提示重新登录；④ pytest tests/courses -q 全绿。

回写进度：§4 的 M12 → ✅ / 日期 / 摘要；更新表头计数。
```

### M13 运行目录与终端补丁 ✅

`1.25 人日 · 角色 BR · 依赖 M1 · 涉及 preflight.py`

`来源：新写 preflight 两个函数（理由：上游无此能力且 vendor 只读）；复制策略照 logger.py 的路径行为设计，不照抄代码。`

```text
【M13 运行目录复制 + 终端补丁】
原始实施前现状（当前实现见 §0/§4）：vendor 在 core/courses/providers/zhihuishu/vendor/fuckZHS（9 文件 + UPSTREAM.md），只读。

1. preflight.materialize_vendor(run_dir)：按 (size, mtime_ns) 指纹比对，从 vendor 复制白名单文件到
   run_dir/upstream，排 __pycache__，写 .vendor_fingerprint。
   白名单：fucker/ObjDict/zd_utils/sign/utils/logger/push/meta.json/LICENSE/UPSTREAM.md。
2. preflight.patch_terminal_size()：上游 fuckAiCourse 里 os.get_terminal_size() 无 try（fucker.py:1279），
   PIPE 下必抛 OSError。做法：import fucker 之后设 fucker.os = shim（__getattr__ 转发 os，
   get_terminal_size 返回 (80,24)），**不要**改全局 os.get_terminal_size。
3. preflight.prepare_run_dir(job_id)：建 run 目录（清上次残留）→ materialize_vendor。

复用：core.paths 的 data_path/resource_path；不要在 vendor 目录里 import（会生成 logs/）。
禁：不复制 config.json / cookies.json / execution.json / main.py / images / decrypt。
验收：连续两次 materialize 第二次 0 复制；AI 课程在 PIPE 下能进到知识点遍历；
     跑完 vendor 目录里没有新增文件。

回写进度：§4 的 M13 → ✅ / 日期 / 摘要；更新表头计数。
```

### M14 参数校验与档案映射 ✅

`2.0 人日 · 角色 BE · 依赖 M4 · 涉及 service.py、providers/zhihuishu/aimap.py`

`来源：新写 aimap/validate（理由：档案格式与上游 Openai 期望不同，映射层必须存在）；复用 protocol 常量与 domain 枚举。`

```text
【M14 参数校验 + 模型档案映射】
实现：`service.validate_create()` 校验课程创建字段；`aimap.build()` 把本地模型档案映射为上游配置。模型档案读取只走 `ModelProfileStore.resolve_settings()`。

1. `aimap.build(profile_settings) -> dict`：
   - 只接受 path 以 /chat/completions 结尾的档案（/responses、/messages → CourseUnsupported）
   - api_base 推导：去掉尾部 /chat/completions，若以 /v1 结尾再去掉 /v1，保留其它前缀
     （https://x.com/openai/v1/chat/completions → https://x.com/openai）
   - 返回 {"enabled":True,"use_zhidao_ai":False,"openai":{"api_base","api_key","model_name"},
     "ppt_processing":{"provide_to_ai":False,"moonShot":{"base_url":"","api_key":""}},"use_stream":True}
   - api_key 为空 → CourseUnsupported("该模型档案没有配置 API 密钥")
2. `service.validate_create(site_id, mode, course_id, class_id, options)`：shared 的 course_id 含字母且 1-128、
   hike 纯数字、ai 纯数字且 class_id/model_profile_id 必填；speed∈[0.1,500]、threshold∈(0,1]、
   time_limit_minutes 整数∈[0,14400]、process_exam 是 bool；非法 → CourseInvalid（中文、带字段名）。

复用：档案读 core/settings/model_profiles 的 resolve_settings；不改 ModelProfileStore。
禁：不做类型正则猜测；不把 api_key 写进 job.json。
验收：5 组 URL 用例（api.openai.com/v1/chat/completions → https://api.openai.com 等）+ 12 合法/12 非法参数用例进测试。

完成记录（2026-10-24）：新增 `aimap.py`、`validate_create()` 和 5 组 URL + 12 组合法/12 组非法参数回归；全量 235 passed。复用 `ModelProfileStore.resolve_settings()`，密钥仅进入运行时映射，不写 job.json。
```

### M15 服务与课程路由 ✅

`2.0 人日 · 角色 BE · 依赖 M9、M11、M12、M14 · 扩展既有 service.py/task_router.py，改 app.py；确有必要才拆 router.py`

`来源：扩展既有 service.py 与 task_router.py 的编排/转发，改 app.py.create_task；复用 store/runtime/aimap/dto，不另造第二套服务或分发器。`

```text
【M15 课程服务 + 路由 + 建任务分发】
现状：task_router 已把 6 个通用接口接到课程分支，但 start/rerun/settings 还没有；
      POST /api/tasks 未改（app.py:476 只做 homework）。CourseRuntime（M7）、认证（M12）、校验映射（M14）
      就绪后本包只是编排，不要重写它们。

1. service.py：create_job / start / rerun / update_options / stop / archive / delete
   - start：正常状态必须 ready；draft 即使已有 Cookie 也不得直接跳 ready，应先通过认证/凭据校验编排的 draft→authenticating→ready 合法链路。再读 LocalCredentialProvider 的 cookie →
     preflight.check_cookies；ai 模式这里调 aimap.build；再 Runtime.start
   - create_job：validate_create → domain.job_id() → store.create
2. 优先在既有 app.py/task_router.py 接 POST /course/start、/course/rerun、PUT /course-settings，全走既有 service；只有确能减少复杂度才拆 router.py，错误统一 CourseError。
3. app.py：create_task() 先读 task_type（缺省 homework）；homework 分支一字不改；
   course 分支只接受 site_id/title/course{...}，忽略 page_url，返回 dto.summary(job)。
   确认 course 分支零调用 TaskStore。

复用：只编排已有能力；store/runtime/aimap 一律不重写。
禁：不给 course 建草稿再转换；不放宽写请求的 loopback/Host/Origin 校验。
验收：建 1 条课程任务后 tasks.sqlite3 行数不变；四个端点成功各一次 + 失败各一次；跨源 POST 仍 403。

回写进度：§4 的 M15 → ✅ / 日期 / 摘要；更新表头计数。
完成记录（2026-10-24）：create_job/start/rerun/update_options/stop/archive/delete 落地；POST `/api/tasks` 的 course 分支零调用 TaskStore，新增 start/rerun/settings 路由。4 端点成功/失败各一条，数据库行数不变，跨源拒绝 403；M15 定向 38 passed，全量 247 passed。
```

### M16 错误与安全收口 ✅

`1.5 人日 · 角色 BE · 依赖 M15 · 涉及 app.py、errors.py、dto.py、runtime.py`

`来源：复用既有 CourseError handler 与 M8 脱敏；新写仅错误矩阵映射与安全测试。`

```text
【M16 错误矩阵 + 安全十条】
现状：CourseError 的全局 handler 已注册，CourseError(message, status_code) 已支持自带状态码。
      缺错误的状态码约定，以及把 §5.6 的十条变成可执行检查。

1. 错误矩阵：CourseInvalid→422、CourseTransitionError→409、CourseBusy→409、CourseNotFound→404、
   CourseAuthRequired→409、CourseUnsupported→422、CourseProtocolError→502（当前实现为 500，M16 修改并验证）；CourseInvalid/CourseUnsupported 已由 M14 定义，M16 需把 handler 状态码和消息矩阵补齐并验证。
   所有 message 中文、以句号结尾、不含 Traceback / 盘符 / 临时路径 / cookie / api_key。
2. 安全十条逐条落 tests/test_security.py：响应白名单 / 无完整 URL / Cookie 只走 stdin / stderr 脱敏 /
   日志级别 WARNING / 运行数据只在 data_path("courses") / 删除清理运行目录 / 写请求三重校验无豁免 /
   dto 不含 api_key / 单站点单实例。

复用：脱敏函数若已在 M8 存在就直接用；错误风格照现有 handler。
禁：不为过测试放宽校验；不把异常栈写进响应。
验收：7 类错误各一次（状态码 + 消息）；pytest tests/test_security.py 十条全绿。

回写进度：§4 的 M16 → ✅ / 日期 / 摘要；更新表头计数。
```

完成记录（2026-10-24）：CourseProtocolError 默认状态码改为 502；7 类 CourseError 经全局 handler 返回安全中文句号文案，危险异常文本回退固定提示；新增安全十条测试（响应白名单、URL、Cookie stdin、stderr 脱敏、日志级别、数据路径、删除清理、三重写保护、DTO 密钥白名单、单站点槽）。`tests/test_security.py` 12 passed，全量 `pytest tests -q` 259 passed。

### M17 前端新建任务 ✅

`3.75 人日 · 角色 FE · 依赖 M2 · 涉及 index.html、app.js、styles.css`

`来源：改 static 三文件；复用现有表单控件样式、api()、option 构造；不新造组件。`

```text
【M17 新建任务：类型分流 + 网课参数】
实施前现状：新建弹窗与提交逻辑只有做题语义；本任务完成后 static 三文件已支持前端类型分流。

1. index.html：弹窗改五段——类型分段（隐藏 input task_type）、站点 select、名称、
   .form-section[data-section=homework]（作业网址 + 模型档案）、
   .form-section[data-section=course]（课程类型 shared/hike/ai、课程 ID、班级 ID、播放速度 1.5、
   完成阈值 0.91、运行时限 0、处理考试、模型档案）。班级 ID/处理考试/模型档案仅 ai 显示，page_url 仅 homework 必填。
2. app.js：新增 applyTaskType()（显隐 + 必填 + 站点过滤）；renderMeta 按 task_types 过滤站点；
   openNewTask 默认做题；submitNewTask 按类型构造 payload（网课 {task_type,site_id,title,course:{...}}，不带 page_url）；
   editTaskId 仅对做题生效；左栏加 #typeFilter（全部/做题/网课）并参与 renderTaskList 过滤与签名。
3. styles.css：.type-switch / .form-section / .course-grid（≤560px 变一列）；复用 34px 控件高、6px 圆角、
   #F8FAFC/#FFFFFF/#2563EB 调色板，不引入新色值。

复用：卡片/按钮/表单控件样式、option 构造、api() 封装全部沿用现有代码。
禁：不改做题提交请求体；不在前端硬编码 "zhihuishu"/"智慧树"（label 从 meta 取）；不加第二个"新建"入口。
验收记录：已检查 JS 语法与 diff 空白；网课站点按 meta.task_types 过滤，课程 ID 设置 required 并在提交逻辑再次拦截。
后端 `POST /api/tasks` 已由 M15 按 task_type 分流；端到端抓取成功的网课创建请求及三宽度截图待 M21 联调确认。

回写进度：§4 的 M17 → ✅ / 2026-10-24 / 完成新建任务类型分流与网课参数表单；端到端请求依赖 M15。
```

### M18 前端列表与徽章 ✅

`2.25 人日 · 角色 FE · 依赖 M17 · 涉及 app.js`

`来源：改 app.js；复用 statusBadge/card 结构、.selected 逻辑、STATUS_LABELS；只加分支与常量。`

```text
【M18 混合列表 + 状态徽章 + 任务菜单】
现状：renderTaskList（180-229）用 WIZARD_LABELS[wizardStepForTask(task)] 显示阶段，课程状态会退化成「登录验证」；
      statusLabel（64-72）先看 queue_state；openTaskMenu/openDeleteTaskDialog（265-299 / 1057-1087）
      用 Boolean(task.run_id) 判断「删除任务 vs 删除任务与题包」——课程没有题包，文案必然错。

1. 常量：STATUS_LABELS 增 ready/running/stopping/completed/partial（cancelled/failed/archived 已有）；
   STATUS_TONES 补色；新增 COURSE_STEPS 与 COURSE_STEP_LABELS；**WIZARD_STEPS 一字不改**。
2. renderTaskList：标题前加 [做题]/[网课] 类型标签；课程阶段走 COURSE_STEP_LABELS[current_step]；
   有 progress_percent 时 meta 行显示百分比；签名追加 task_type / current_step / progress_percent。
3. statusLabel/statusBadge：课程不看 queue_state，直接 STATUS_LABELS[status]；isTaskBusy 追加 running/stopping。
4. openTaskMenu：课程删除项文案「删除运行记录」；openDeleteTaskDialog：课程恒 packageMode=false，
   请求 {mode:"record"}，不要求输入 DELETE，警示语改「课程进度、事件记录与运行目录会一并移除」。

复用：徽章/卡片/菜单结构与 .selected 逻辑不动，只看 task_type 分支。
禁：不做课程专用卡片；不改做题删除分支。
验收：混合列表截图；课程删除 Network 里是 {mode:"record"}；做题删除流程与改前一致；轮询 10 秒不闪烁。

回写进度：§4 的 M18 → ✅ / 2026-10-24 / 混合列表区分类型、课程阶段和进度；课程状态与删除文案/请求正确分流；签名覆盖轮询动态字段。`node --check` 与 `git diff --check` 通过。本轮本机 WebUI 未能连接，截图、Network 请求及 10 秒轮询观测待界面联调。
```

### M19 前端课程详情 ✅

`4.0 人日 · 角色 FE · 依赖 M18、M5、M11、M15、M16 · 涉及 app.js`

`来源：改 app.js；复用 card/facts/statusLine/actionRow/renderEventCard/renderStepper 与 refreshDetail 轮询；不新造组件。`

```text
【M19 课程详情页】
现状：renderDetail（470-501）只有做题七步向导；renderStepper（556-568）遍历全局 WIZARD_STEPS；
      refreshDetail（353-411）的 revision 不含课程进度，且会对课程任务发 /review、/model-authorization、
      /submit-confirmation 三个做题专用请求（课程上必然 404）。

1. renderDetail 分流：task_type === "course" → renderCourseDetail(task)，做题分支整体不动。
2. renderCourseStepper(task)：用 COURSE_STEPS；mode!=="ai" 或 !process_exam 时过滤「考试」；
   复用 .stepper/.step 的 done/reached/current 类。
3. 课程信息卡（card()+facts()）：类型/课程 ID/班级 ID/课程名/速度/阈值/时限；
   学习进度卡：当前项目、已完成/总数、百分比、失败数；有 percent 用 .progress-track，无则不确定进度条；
   事件抽屉直接复用 renderEventCard()。
4. 按钮矩阵：draft→登录；ready→开始+重新登录+编辑参数；running→停止学习(danger)；stopping→只读；
   completed→刷新+重跑+归档；partial/failed→重跑+归档（当前状态机不允许 failed→authenticating，不显示必定被拒绝的登录按钮；凭据失效提示新建任务并登录。若要原任务重新登录，先单独修订状态机、服务与测试）；
   cancelled→重跑+归档；archived→删除。
5. refreshDetail：revision 追加 course.phase/progress_percent/current_title/completed_count；
   课程跳过 /review、/model-authorization、/submit-confirmation；不调 syncWizardStep；
   切任务清课程态；轮询间隔不动。

复用：card/facts/statusLine/actionRow/renderEventCard 全部复用，不新造组件。
禁：课程页不出现模型授权/提交确认/填写答案；不在前端插值进度。
验收：4 种进度 + 9 种状态截图；进度变化时文字不跳位；课程页无 404 请求；做题详情逐屏与改前一致。

回写进度：§4 的 M19 → ✅ / 2026-10-24 / 完成课程详情阶段、信息、进度和状态操作；刷新跳过做题专用接口并纳入课程进度 revision。JS 语法与 diff 空白检查通过；截图/网络请求人工验收留待 M21。
```

### M20 自动化测试补全 ✅

`5.25 人日 · 角色 QA · 依赖 M5、M9、M11、M14、M16`

`来源：复用 tests/conftest 与 echo_bridge 思路；新写仅为缺失用例。`

```text
【M20 测试补全】
历史基线：tests/ 曾有 124 条通过（当前条数见 §0.6）——test_domain(161 行) / test_event_log(213) / test_task_router(379) /
      test_dto(195) / test_auth(333) / test_lifecycle(277) / test_bridge_smoke(89) + conftest + fixtures/echo_bridge。
      不要重写这些。

补齐三块（放 tests/courses/，用 tmp_path 做数据目录，绝不写真实 LOCALAPPDATA）：
1. ~~test_dto.py~~（已完成：summary 键集合 === dto.SUMMARY_KEYS 18 键、detail 四外壳键齐全、序列化后无 cookie/api_key/http）。
2. test_aimap.py：5 组 URL（/v1/chat/completions、自定义前缀、无 v1、/responses、loopback）+ 缺 key。
3. test_runtime_integration.py：stub fucker（tests/fixtures/stub_fucker.py）覆盖正常/部分失败/时限/非 0 退出/
   无 result/用户 stop/畸形事件/超时共 8 条，断言终态 + 分类 + 槽释放 + 无残留进程。
4. test_stability.py：强杀子进程、断网、Cookie 过期、连点两次开始、关服务打断 5 条；附 §5.3 事件契约快照。

复用：已有测试的 tmp_path/独立 LOCALAPPDATA 隔离方式与 echo_bridge；tests/conftest.py 当前只负责 import 路径，没有可直接复用的数据隔离 fixture，不得假定其存在。
禁：不为过测试改业务代码；不 sleep 真实时间（注入 clock 或 monkeypatch）。
验收：pytest tests -q 全绿，tests/courses 覆盖率 ≥80%（--cov=core.courses）。

回写进度：§4 的 M20 → ✅ / 日期 / 摘要（用例数 + 覆盖率）；更新表头计数。
```

### M21 界面自检与做题回归 ✅

`2.25 人日 · 角色 QA · 依赖 M19、M20 · 产出 docs/shots/ + docs/regression/`

`来源：复用 .workbuddy/cdp-*.js 脚本与截图流程；不新写框架。`

```text
【M21 界面自检 + 做题回归】
现状：项目已有无头 Chrome + CDP 验证手法（.workbuddy/cdp-shots.js、cdp-menu.js）。
      两个坑：132% DPI 下截图会缩、Chrome 会缓存 app.js（要 Network.setCacheDisabled、deviceScaleFactor=2）。

1. 界面自检：隔离实例（LOCALAPPDATA → %TEMP%）造 4 类任务（做题草稿、网课 ready、网课 running 带进度、
   网课 partial），截：混合列表、类型筛选、新建弹窗两态、课程详情四进度、9 状态按钮、课程删除弹窗。
   断言：控制台无 JS 异常；关键元素在 1440/1024/360 下与内容列对齐（getBoundingClientRect）。
2. 做题回归：真实账号跑超星与雨课堂各一条完整链路；核对 /api/meta 只多 task_types、八个做题接口结构与错误码不变
   （对照 M3 快照）、七步向导与题包/答案界面视觉零变化、抓 Cookie 弹窗文案不变。

复用：沿用现有 CDP 脚本与 .workbuddy 目录，别新写框架。
禁：不为截图好看改业务代码；截图不提交进 git。
验收：截图集齐备、断言输出贴记录；两条做题链路全通且四类核对项"一致"。

回写进度：§4 的 M21 → ✅ / 日期 / 摘要；更新表头计数。
```

### M22 文档与收尾 ✅

`2.0 人日 · 角色 DOC · 依赖 M21`

`来源：改 README/CHANGELOG/architecture，沿用既有写法。`

```text
【M22 文档与收尾】
1. docs/architecture.md 增「网课模块」章节（目录职责、两套任务域关系、子进程+JSONL 数据流、与做题模块边界），
   并更新目录树与「架构梳理日期」。
2. README.md：功能加「智慧树网课（共享课/校内课/AI 课程）」；补依赖说明；新增「网课任务」小节
   （创建→登录→开始→停止/重跑，三种 course_id 来源）；常见问题（Cookie 过期、为什么显示部分完成、
   能否同时跑两门课、诊断日志在哪）。
3. CHANGELOG.md 在「未发布」下补新增/变更/修复三条，不编造未发生的修复。
4. 版本与状态：pyproject.toml + README 顶部版本号同步；TASKBOOK.md 状态改「已实施」；本文件 §4 改 22/22 并更新表头。
5. 输出发布清单给用户（改动文件 + 建议 commit 信息 + 需要用户执行的命令），**不自己提交**。

复用：沿用 README/CHANGELOG 既有写法。
禁：不执行 git add/commit/push/tag；不在 README 写状态机全表（放 architecture.md）。
验收：三处版本号一致；进度 22/22；发布清单与 git status 对照无遗漏。

回写进度：§4 的 M22 → ✅ / 日期 / 摘要，并把表头更新为最终计数。
```

### M23 登录后选课与上游二维码 ◐

`待估 · 角色 BE+FE · 依赖 M12、M13、M15、M17、M19`

`来源：直接复用 vendor 的 Fucker.login(use_qr=True)、getZhidaoList()/getHikeList()/getZhidaoAiList()；修改 CourseJob/domain/store/service、Cookie Manager 配置持久化、app/task_router 与课程详情 UI；新写 auth_bridge.py 和 qr_sessions.py（现有认证调用是阻塞浏览器抓取，没有能把上游 QR 返回页面并跨请求保留登录会话的桥接能力）。`

```text
【M23 登录后选课与上游二维码】
1. 创建网课任务只提交站点与任务名，不要求课程类型、课程 ID 或班级 ID。
2. 详情页登录只提供智慧树二维码；二维码来自上游 Fucker.login(use_qr=True)，认证完成后复用 Cookie Manager 写回配置。
3. 登录后按钮调用上游共享课/校内课/AI 课程目录方法，列表显示全部课程；服务端只接受本次抓取目录中的选择。
4. 验收真实 QR 扫码、CASLOGC 持久化预检、三类目录字段映射、AI 班级与档案选择、选择前阻止启动；回归原做题链路。

复用：按上文直接调用上游登录和目录方法；复用 Cookie Manager 配置原子写入。
禁：不修改 vendor；不在创建阶段要求手工填写课程标识；不接受目录外的课程选择。
当前：修复二维码重绘/状态误报与同名 Cookie 保存；共享课/校内课字段对齐 fuckZHS `--fetch`，AI 单独隔离；当前执行环境探测为 ProxyError，用户端真实目录待验收。
```

---

## 7. 回写规范

**常规任务包完成后更新 §4 的三处**（用户要求审计修订/跨包缺陷修复时，可以同步修订相关卡片、契约与 §0，须说明原因，不能继续保留已知错误）：

1. 该行「状态」→ `✅ 完成` / `◐ 部分完成` / `⚠️ 阻塞`；
2. 该行「备注」→ ≤30 字，写做了什么；阻塞就写卡点（哪一步、下一步怎么试）；
3. 表头 → `**进度：x 完成 · y 部分 · z 未开始 · 最后更新 YYYY-MM-DD**`。

示例：

```markdown
| M5 | DTO 与事件出口 | T2-03/04 | M4 | 2.25 | ✅ 完成 | dto.py 落地，详情接口 200，18 键校验通过 |
```

**禁止**：未通过相应验收就把 ◐/⚠️ 写成 ✅；无关地改别人备注或表格列结构。测试通过不等于真实站点验收通过；新增发现应登记并回归，不隐藏已知缺陷。

**事实源优先级**：源码与可重现验证证据 → §4 状态及验收记录 → §0 摘要 → 历史任务提示词。每次涉及状态/缺陷审计时同步 §0、§4，不能只改计数而保留相矛盾的摘要。

**交付回复必须带三行**（§1.4 的口径，缺一行视为未完成）：

```text
复用：<直接调用了哪些既有函数/上游方法，带 文件:行号>
改写：<改了哪些既有函数，改了什么>
新写：<新增了哪些文件/函数，为什么不复用>
```

没回写 = 任务未完成。若提示词本身有问题（读不到文件、口径矛盾），在备注写清并在回复里指出，不要硬做。

---

## 8. 质量门

> 每个门通过前，先跑 §1.4 的两条抽查命令与三行自检；命中"重复实现"即视为不通过。

| 门 | 条件 | 通过标准 |
| --- | --- | --- |
| **G1** 后端可读 | M3、M5、M6 全 ✅ | 混合任务列表正确；课程详情 200；非法迁移/运行中删除被拒；崩溃后无「学习中」 |
| **G2** 桥接闭环 | M7～M11、M14 全 ✅ | stub 8 类终局全绿；无残留进程；真实共享课跑通且计数与站内一致 |
| **G3** 端到端 | G2；M12～M19、M23 全 ✅ | 浏览器完成「新建→二维码登录→抓取目录→选课→开始→看到进度→停止」；错误矩阵与安全十条通过 |
| **G4** 全量 | G3；M20、M21 全 ✅ | 三模式 + 做题双站点回归 + 界面自检全通过；覆盖率 ≥80% |
| **G5** 可发布 | G4；M22 全 ✅ | 版本一致、README 可复现、发布清单齐备 |

准备工作和使用 stub 的前端开发不等于验收。M19 与 G3 的最终验收依赖 G2 及服务接口；M21 完成才有 G4 所需的真实回归证据，不能把质量门定义成等待自己下游才能满足的环。真实站点凭据或环境不可用时保留“待验收”，不以 stub 替代。

---

## 附录 A 排期（含已完成部分）

| 段 | 任务包 | 累计人日 | 里程碑 |
| --- | --- | --- | --- |
| 已标完成 | M1、M2、M3、M4、M5、M6、M7、M8、M9、M10、M11、M12、M13、M14、M15、M16、M17、M18、M19 | 45.5 | 参数/桥接基础、服务路由、安全收口、终局裁定与课程详情前端；G1 证据按现有测试复核 |
| 2 | M19 | 45.5 | G3 |
| 3 | M20 → M21 | 53 | G4；M20 已有部分实现 |
| 4 | M22 | 55 | G5 |

以上为计划权重累计，不代表实际用时。未完成状态包的名义预算合计 9.5 人日，包含部分完成包的全部预算；实际剩余工作量和工期仍需单独估算并考虑真实站点风险。

## 附录 B vendor 清单

已就位（`core/courses/providers/zhihuishu/vendor/fuckZHS/`）：`fucker.py`、`ObjDict.py`、`zd_utils.py`、`sign.py`、`utils.py`、`logger.py`、`push.py`、`meta.json`、`LICENSE`、`UPSTREAM.md`。
刻意不含：`main.py`（全局配置与 `exit()`）、`config.json`/`cookies.json`/`execution.json`（凭据）、`images/`、`decrypt/`、`logs/`。
**vendor 默认保持原样**；优先直接调用和最少薄适配（progressBar、终端 shim 等），必要补丁遵循 §1.1；不得把 monkeypatch 当作必须增加的工作。

## 附录 C 数据布局

```text
%LOCALAPPDATA%\AutoLearn\
├── config.json                 【改】新增 zhihuishu: { cookie }
├── tasks.sqlite3               做题任务（课程不得写入）
├── courses\                    【新，由 store.py 管理】
│   ├── index.json              overview 只读它
│   ├── runtime.json            站点单实例槽（M6）
│   └── jobs\<job_id>\
│       ├── job.json            原子写
│       ├── events.jsonl        business events（详情用）
│       ├── runtime-events.jsonl 轮询事件（≤500 行）
│       ├── diagnostics.log     脱敏 stderr（≤2MB，M8）
│       └── run\upstream\       每次运行复制的 vendor（M13）
└── logs\                       做题侧日志（不动）
```

