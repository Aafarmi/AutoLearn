# AutoLearn 前端任务计划（历史方案归档）

> 当前状态（2026-10-17 后续）：用户已决定只做本机 localhost WebUI，不再构建或分发 Windows EXE。当前入口为 `python -m core.webui`，使用 FastAPI + Uvicorn + 单个纯 Python worker；旧 Qt/PySide6、`QThread`、Nuitka、standalone 和 NSIS 方案均已删除。  
> 本文第 2 节及后续关于 PySide6/Qt Widgets、`QThread`、Windows EXE、`pyside6-deploy` 和安装包的内容保留为历史决策记录，不再作为实现依据。当前运行、安全、数据和依赖约定以根目录 `AGENTS.md`、`README.md` 和 `docs/architecture.md` 为准。  
> 已完成：任务 SQLite 域、纯 Python 串行 runtime、localhost API、本机静态界面、模型档案、答案复核、爬取/解题/交互队列、一次性提交确认、可写用户数据路径、DPAPI URL 保护和旧数据迁移代码。未新增自动化测试；真实站点、模型请求、浏览器组件和提交仍需分别获得明确授权后验收。

## 1. 产品目标

桌面应用采用类似 GPT 交互产品的简洁工作区风格，但不实现通用浏览器功能。应用启动后打开首页；顶部提供页面导航和设置入口。首页负责创建和管理任务，打开任务后进入应用内的新页面或任务标签，在任务详情中按步骤完成抓取、解题、复核、填写和提交。

应用支持多个任务保存、切换和恢复。任务可在不同页面中同时打开，但抓取、解题、网页交互按队列逐个执行，同一时间只运行一个任务。应用退出或崩溃后不得自动重试提交；处于提交不确定状态的任务必须先由用户检查。

## 2. 已确认的产品决策

- 最终交付目标为 Windows 桌面 EXE。
- 视觉风格参考 GPT 交互页面：简洁、留白、轻量边框、清晰状态提示；使用 AutoLearn 自有名称和样式，不复制品牌标识。
- 顶部放置菜单/导航。首页负责任务管理，设置页有固定入口。
- 首页打开时显示任务面板。新建任务后立即创建草稿，并在同一应用窗口打开新的任务页。
- 任务不能手动输入站点 ID，必须从当前适配器列表中选择站点。
- 作业 URL 由用户为任务填写；每个任务保存自己的 URL 上下文。
- Cookie 不在任务中手动编辑。任务页通过按钮调用 Cookie Manager，并只显示安全状态，不显示 Cookie 值。
- 任务页只选择模型；模型参数集中放在设置页。
- 多个任务可保存、切换和恢复；实际执行按队列串行。
- 首页任务列表和筛选使用 SQLite `TaskStore` 作为唯一任务索引；UI 不维护第二份持久索引。
- 解题后可编辑答案。编辑后的答案必须经过 Schema 与题目/选项/空位引用校验。
- 最终提交确认放在桌面前端。后端完成页面预检、填写和回显验证后等待用户确认；只有收到明确确认事件才调用网站提交。
- “前端已确认”不等于“网站已提交成功”；仍要等待页面证据并记录 confirmed、failed 或 unconfirmed。
- 发送题目和图片到模型服务前，必须向用户展示服务目的地、模型、题目数量和图片数量，并由用户确认。

### 2.1 架构调研结论（核心方案已确认）

- 当前桌面壳层采用 PySide6/Qt Widgets；SQLite `TaskStore`、进程内 Python API + 单个 `QThread` 串行 worker、独立 Scrapling 可见浏览器均已获用户确认并接入。Windows EXE 打包兼容性仍未验证。
- 任务元数据、配置、`runs/` 和日志建议保存在 `%LOCALAPPDATA%\AutoLearn`；题包继续沿用现有 `runs/<run_id>/` JSON。安装包资源与可写用户数据分开。
- 第一版将任务页和最终确认放在 AutoLearn 主窗口，完整网站页面留在独立的 Scrapling 可见浏览器窗口；该方案已获用户批准，不假设 `QWebEngineView` 能共享 Scrapling 的 Playwright 页面/上下文。浏览器启动和交互仍未做运行验收。
- Windows 发布策略已确定：Windows 11 25H2 或更新版本、x64；使用 `pyside6-deploy --mode standalone` 生成应用目录，再封装成无需管理员权限的 per-user 安装包。安装在 `%LOCALAPPDATA%\Programs\AutoLearn`，用户数据留在 `%LOCALAPPDATA%\AutoLearn`，卸载默认保留用户数据。Chrome Stable x64 作为 Cookie Manager `real_chrome=True` 的系统前置条件，不随包分发；Scrapling 浏览器依赖首次按用户操作安装，版本随应用锁定。SQLite 内任务 URL 使用 DPAPI CurrentUser 加密。路径迁移代码和资源收集清单已实现；迁移、Windows 构建与安装仍待验收。

## 3. 视觉与页面结构

### 3.1 应用框架

建议使用顶部导航与内部标签页的组合：

    ┌ AutoLearn  [首页] [雨课堂作业 A ×] [超星作业 B ×]              [设置]
    ├─────────────────────────────────────────────────────────────────
    │ 当前页面内容
    └─────────────────────────────────────────────────────────────────

- 首页标签固定在最前。
- 打开的任务详情在首页之后形成任务标签；任务运行中关闭前须确认停止，等待 worker 确认停止并释放队列后再关闭；关闭标签不删除任务记录或题包。
- 设置页从顶部固定入口打开，可采用固定导航项或内部标签。
- 不提供地址栏、前进/后退、书签等通用浏览器控件。
- 当任务标签过多时，使用横向滚动或“更多标签”菜单，不压缩顶部主要操作。
- 登录、题目交互和提交确认尽量在同一应用窗口的任务工作区完成。若现有浏览器组件不能嵌入，技术调研需说明最小必要的受控窗口方案，不得悄悄改成无人工确认的提交。

### 3.2 首页：任务管理面板

首页以桌面宽屏的列表/表格为主，兼顾空状态和小窗口布局。

建议组成：

- 标题区：任务中心、未完成任务数量。
- 主操作：新建任务。
- 筛选区：搜索名称、站点适配器、任务状态、更新时间。
- 任务列表：名称、站点、当前步骤、状态、最近更新时间、更多操作。
- 空状态：简短说明与醒目的“新建任务”按钮。
- 执行队列提示：明确标出当前运行任务和等待中的任务。

基本操作：

- 单击任务行/卡片：打开任务详情。
- 新建任务：创建草稿并打开新的任务页，不先弹复杂设置对话框。
- 更多菜单：编辑、复制为新任务、归档、删除；永久删除任务和题包须说明影响并二次确认。
- 草稿可直接删除；有 run_id 或运行结果的任务默认归档。永久删除题包须在二次确认中说明影响。
- 运行中的任务不可直接删除；关闭标签须经确认并停止后台阶段，停止失败时保留标签。
- 已完成和未确认任务保留运行记录，不能被“继续任务”静默重跑。

### 3.3 任务详情：向导工作区

任务页包括：

1. 任务标题、适配器、状态和保存提示。
2. 水平步骤条，显示已完成、当前、等待和失败步骤。
3. 当前步骤内容区：表单、抓取统计、题目预览、答案复核或交互状态。
4. 固定底部操作栏：上一步、保存草稿、当前步骤主操作。
5. 必要时提供安全提示区域，说明等待用户授权、浏览器状态或失败原因。

建议步骤：

1. **任务设置**：适配器选择、作业 URL、模型选择、Cookie 状态。
2. **抓取题目**：执行对应站点爬虫，显示安全进度、题数和附件数。
3. **题包检查**：展示题目数量、题型统计和附件是否齐全。
4. **模型授权与解题**：显示模型服务目的地及题目/图片数量，确认后调用解题器。
5. **答案复核**：按题目展示答案，允许编辑；保存后即时校验。
6. **填写与提交确认**：后端完成页面唯一性预检、填写和回显核对后，桌面页显示最终提交确认。
7. **结果**：展示网站确认结果和逐题安全状态，支持返回查看题目与答案。

任务设置页面的字段：

- 适配器：从已注册适配器列表选择。
- 作业 URL：用户输入，作为该任务上下文保存。
- Cookie：不显示值；显示“未配置/可用/需更新/获取中/失败”等状态，并提供调用 Cookie Manager 的按钮。
- 模型：从设置页已配置模型中选择；不在任务页配置服务地址或 API 密钥。
- 任务名称：可选；默认按站点和创建时间生成。

编辑规则建议：

- 抓取前可更改适配器、URL 和模型。
- 已抓取后，URL 或适配器变化应创建新任务，避免旧题包与新页面错配。
- 更换模型只影响尚未生成答案的任务；已有答案需明确选择重新解题并遵守覆盖规则。
- 填写后若用户修改答案，必须重新填写和验证所有目标控件。

### 3.4 设置页

设置页集中管理：

- 命名模型配置：服务提供方、完整请求 URL、模型、密钥、温度、输出限制和超时等。密钥始终掩码显示。
- 当前适配器列表与认证状态。
- Cookie Manager 的站点级登录状态及“更新登录”入口。
- 本地数据目录和版本信息（是否可更改由技术调研决定）。

任务只保存模型配置标识，不复制密钥。设置变更不得悄悄改变已创建任务的 URL 或模型选择。

### 3.5 GPT 风格交互细节

- 优先使用少量稳定的颜色和图标，状态文案明确，避免仅用颜色表达成功或失败。
- 长任务操作在主视图显示进度和最近事件；不要弹出连续阻塞对话框。
- 破坏性操作、模型数据发送、最终提交使用明确确认框，并写出影响对象。
- 错误消息展示可处理原因和下一步；日志与界面不展示 Cookie、API 密钥、题目全文、答案全文或带签名的完整 URL。
- 任务详情顶部保留返回首页/切换任务的路径；运行中关闭标签会记录停止请求，关闭本身不删除任务记录或题包。

## 4. 任务数据与状态设计

### 4.1 已实现的任务记录

`core/tasks/models.py` 与 `TaskStore` 当前记录/暴露：

- 稳定 `task_id`、`site_id`、显示标题、`model_profile_id`、可选 `run_id`、阶段/状态和创建/更新时间。
- 完整 URL 存在独立 `task_context` 表；只由任务详情 API 返回，`TaskRecord` 的对象表示隐藏 URL，首页 `TaskSummary` 不含 URL 或确认 token。
- 当前/中断操作、安全错误分类、待执行操作、队列状态/序号，以及一次性提交确认状态和恢复检查所需字段。
- 任务表不保存 Cookie 或 API key；答案正文继续在 `runs/<run_id>/answers.json`，SQLite 不保存答案题包。

任务库、配置、题包、日志和 Chrome profile 统一定位到 `%LOCALAPPDATA%\AutoLearn` 用户数据目录；只读适配器、Schema、README 和 prompt 通过 `resource_path()` 定位。旧配置、`runs/` 和 `logs/` 在首次启动时先校验并复制到暂存区，校验通过后原子迁入；在 `backups/legacy-v1/` 留档并保留旧来源。若目标已有不同内容则停止，不合并或覆盖；复制或校验失败时移除本次新建的目标和备份。SQLite 版本 1 升级使用 SQLite backup API 保存到数据库旁的 `backups/`，版本与 URL 更新在事务中完成；Windows 当前用户通过 DPAPI CurrentUser 保护活动数据库及数据库备份中的 URL。数据库损坏自动恢复尚未实现。

### 4.2 已实现的应用任务状态

`TaskStatus` 当前包括 `draft`、`queued`、`authenticating`、`crawling`、`package_review`、`waiting_model_authorization`、`solving`、`answer_review`、`ready_for_interaction`、`filling`、`awaiting_submit_confirmation`、`submitting`、`cancelling`、`filled_unsubmitted`、`confirmed`、`failed`、`unconfirmed`、`cancelled`、`paused`、`needs_inspection` 和 `archived`。UI 将这些内部值映射为中文阶段和状态文案；队列另有 queued/active/paused/blocked 状态。

### 4.3 安全状态转换

    draft → queued → authenticating (optional) → crawling → package_review
          → waiting_model_authorization → solving → answer_review
          → ready_for_interaction → filling → awaiting_submit_confirmation
          → submitting → confirmed / failed / unconfirmed

- 队列任一时刻最多运行一个需要浏览器或外部服务的任务。
- 用户取消解题授权时不发送模型请求，任务停留在可继续状态。
- 等待提交确认时关闭应用或浏览器，不得自动触发提交。
- 在向 UI 发出提交确认等待事件前，先持久化 `awaiting_submit_confirmation` 和一次性确认 ID/有效期；启动恢复时不重放旧确认事件。
- 可能发生自动保存或提交但没有明确结果时，标为未确认，要求用户检查页面。
- filled_unsubmitted 是已填写但未提交的结果，不得在界面标成已完成提交。
- 启动恢复时先核对状态和结果文件；不得只凭最后一次前端状态重试提交。

## 5. 前后端契约要求

### 5.1 本机任务调用接口

当前实现使用 PySide 进程内 Python API：主线程管理 UI、队列和 SQLite 状态，单个 `QThread` worker 串行执行阻塞的爬取、解题和交互阶段；不启动本地 HTTP 服务，也不解析终端文本。worker 通过 Qt signal 向 UI 发布结构化事件；授权/确认/取消等 UI 输入通过线程安全队列或事件对象返回 worker。接口覆盖：

- 创建、读取、更新、排队、取消和恢复任务。
- 启动抓取、解题和交互阶段；传入 task_id、run_id、page_url、model_profile_id 等必要上下文。
- 返回结构化状态变化、进度、安全错误分类和生成文件路径。
- 支持等待用户模型授权、答案修改保存、等待前端提交确认等双向交互。
- 事件包含 `task_id`、`run_id`、单调序号、阶段、状态、必要题数/附件数和安全错误分类；不含完整 URL、Cookie、密钥、题目或答案内容。
- 定义长阶段和网络请求的安全取消点；应用关闭、中断与恢复均需从 SQLite 和运行包结果核实状态。
- 避免将带签名 URL、Cookie、API 密钥、题目全文或答案全文写入日志、命令行回显、普通事件载荷或任务列表索引。

### 5.2 URL 与适配器调用

桌面调用路径现已向抓取、解题、交互和认证阶段传递任务上下文，包括 `task_id`、`run_id`、私有 `page_url`、`model_profile_id` 和后端凭证提供器；不会为切换任务覆盖全局站点 URL。任务 URL 从 SQLite 私有上下文读取，不进入任务列表或一般事件；Windows 上使用 DPAPI CurrentUser，适配器仍负责站点 HTTPS/域名校验。桌面与独立 CLI 已切到统一资源/数据路径及迁移入口。

### 5.3 模型配置与解题授权

设置页和任务页已接入版本化模型档案与解题授权流程：

- 稳定的 `model_profile_id` 和配置版本行为；档案被修改后，旧任务在解题前须明确提示将使用更新后的配置并取得确认。
- 任务如何引用配置；密钥只在本机设置中保存并掩码显示。
- 解题器如何使用所选模型，而不把密钥暴露给 UI 日志或任务元数据。
- 发送前授权摘要的字段及取消行为。
- 不得静默改变已发出授权摘要中的模型或目标服务；必要时记录授权时的档案版本或摘要。

solver 的桌面路径按 `model_profile_id` 解析档案，模型请求前展示脱敏服务目的地、模型及题目/图片数量并等待授权，授权后再次核对档案版本。旧 `model`/`model1` 字段继续作为迁移与 CLI 兼容数据；配置路径使用用户数据目录。授权版本是否需要长期保留为审计信息仍待产品确认。

### 5.4 答案编辑契约

- answers.json 是后续填写使用的权威答案文件。
- 前端编辑通过结构化接口保存，不直接拼装任意文件路径。
- 保存前和保存后均按适配器 answers Schema 及 solver.py 跨文件引用校验。
- 正常答案形状没有变化时可复用现有 Schema；只有确实增加字段时才扩 Schema 并同步版本与文档。
- 编辑失败应保留先前有效答案；不得记录答案全文。

### 5.5 前端最终确认与交互契约

桌面一次性确认协议已在公共 runner、两个适配器、Schema、README 和 UI 中接线：

1. 后端完成整页预检、填写并验证回显后，先持久化 `awaiting_submit_confirmation`，并发出绑定 `task_id`、`run_id` 的等待事件。
2. 等待事件包含一次性 `confirmation_id` 和有效期；后端保持可安全继续交互的页面/会话。AutoLearn 前端展示任务、站点、题数和最终影响摘要。
3. 前端向后端发送绑定当前任务的 confirm 或 cancel 事件；事件重复、过期、任务不匹配或应用失联时安全拒绝/中止，不得提交。
4. 只有收到有效 confirm 后，后端才重新查询并核验唯一、可见、启用且属于当前页面的提交控件，随后触发网站提交。
5. 后端必须继续等待网站成功/失败证据，并写 execution_result.json。前端收到“确认已发送”不能直接显示“网站提交成功”。
6. cancel、窗口中断、自动保存和无明确回执分别映射为正确的 skipped / unconfirmed 等状态。
7. Chaoxing、长江雨课堂两种适配器、公共交互 runner、结果 Schema、适配器 README 和相关开发契约需一起更新。

### 5.6 用户数据、安装资源与本地路径

- 发布目标是：安装包中的后端模块、适配器 Schema、README 和 prompt 按只读资源定位；配置、SQLite 任务库、`runs/` 和日志从 `%LOCALAPPDATA%\AutoLearn` 读写，不依赖源码仓库路径。
- 配置、凭证、SQLite、`runs/`、solver/interaction 日志和 Chrome profile 均使用用户数据目录；适配器代码、Schema、README 和 prompt 通过只读资源路径定位。旧源码/安装目录中的配置、题包和日志由启动迁移入口备份后迁入，冲突时不覆盖。
- 任务索引仅存恢复和列表所需元数据；完整任务 URL 使用私有存储通道，排除在列表、普通日志和结构化事件之外。Windows URL 和数据库迁移备份中的 URL 使用 DPAPI CurrentUser；schema 升级有 SQLite backup API 快照和事务回滚。

### 5.7 Cookie Manager 的桌面调用

- 桌面入口已通过后端 API 绑定 `site_id` 与当前任务 URL，支持取消和安全认证状态回调，不使用终端 `input()`。独立 CLI 入口从用户数据目录读取配置。
- Cookie 值仅留在后端认证流程中；UI 只接收未配置、可用、需更新、进行中或失败等安全状态，不返回 Cookie 字段或值。
- 必须继续遵守适配器 HTTPS/域名白名单。桌面端关闭等待或取消时，不得误报认证成功。

## 6. 执行顺序与子任务

以下子任务按依赖顺序规划。多个 agent 可并行处理无共享文件的 UI 工作；修改公共交互契约、适配器和同一组文件的任务应由一个负责人协调，避免冲突。

### 子任务 0：桌面技术与后端接口只读调研

依赖：无。  
交付：桌面框架建议、浏览器内嵌可行性、任务存储建议、本机接口方案、现有 CLI/模块需调整的接口清单。不得写代码。  
状态（2026-10-17，技术调研阶段）：只读调研已完成；PySide6、SQLite、进程内 Python API + 单个 `QThread` worker、独立 Scrapling 可见浏览器方案已获用户批准。当时 URL DPAPI 保护和 Windows 发布形态尚未确认；后续决策见第 8 节。

**调研结论与方案比较**

- **推荐方向：PySide6 + Qt Widgets + 单个串行 `QThread` worker + SQLite。** 现有后端为 Python，可由 UI 进程直接调用；主线程负责界面、任务队列和数据库状态，worker 执行阻塞阶段并通过 Qt signal 汇报进度/状态。授权、取消和提交确认由线程安全队列或事件对象传回 worker；不启动本地 HTTP 服务，也不依赖终端文本协议。
- **任务存储：SQLite + 现有题包格式。** Python 自带 `sqlite3`；任务元数据和队列状态建议放在 `%LOCALAPPDATA%\AutoLearn`，题目/答案/交互题包继续使用 `runs/<run_id>/` JSON。完整任务 URL 另存于私有存储通道，不出现在任务列表、普通日志和事件。技术调研阶段 DPAPI 尚未确定；当前发布决定见第 8 节。
- **浏览器：第一版保留 Scrapling 可见浏览器窗口。** 任务工作区和最终提交确认在 AutoLearn 主窗口，登录及网页操作由 Scrapling 浏览器窗口承载。交互适配器的 `page_action` 回调持有当前页面，可在预检、填写和回显校验后暂停等待 UI 事件；等待期间必须保留页面与 worker 上下文，收到确认后重新查询提交控件。Qt `QWebEngineView` 不会自动共享 Scrapling 的 Playwright 页面/上下文；嵌入网页需要重做浏览器控制层或验证 CDP，作为后续方案。
- **发布：** 技术调研阶段将 PySide `pyside6-deploy` 列为候选，并把单文件/应用目录/安装包、MSVC 工具链、Scrapling 浏览器组件和系统 Chrome 要求留待确认；后续已确定的发布政策见第 8 节。安装资源与 `%LOCALAPPDATA%` 可写数据目录必须分离。

| 方案 | 优点 | 代价 |
| --- | --- | --- |
| PySide6（推荐） | Python UI 直接复用现有模块；`QThread` 信号适合进度和等待确认 | 新增 Qt 依赖和 Windows 打包流程；视觉用 Qt Widgets/QSS 或 Qt Quick |
| Tauri + Web 前端 + Python sidecar | Web UI 灵活，能将 Python 后端作为 sidecar | 增加 Rust、前端工具链及 sidecar 生命周期管理；Windows 需确定 WebView2 运行时策略 |
| Electron + Web 前端 + Python sidecar | Web UI 和子进程模式成熟 | 自带 Chromium/Node.js，包体较大；需严格隔离网页与本机权限并验证 IPC 来源 |

**后端现状与契约影响（调研时基线及当前状态）**

- 调研时 solver、interaction 与 Cookie Manager 依赖全局配置/终端输入；后续子任务已为桌面路径增加显式任务上下文、模型档案和 Cookie Manager 状态 API。旧 CLI 默认行为仍保留，因此不能据此认定发布路径已解耦。
- 调研时两个交互适配器在浏览器内等待提交确认；后续子任务 8 已将确认点移至桌面，并同步公共 runner、两适配器、Schema 和 README。运行结果仍必须由页面证据决定。
- 当前结构化事件包含任务/运行标识、序号、阶段、状态、必要计数和安全错误分类；不包含 URL、Cookie、密钥或答案正文。取消与重启恢复设有保护，但动态浏览器的实际停止时序尚未运行验证。
- AppData 路径已通过 `resource_path()` / `data_path()` 统一；安装资源与可写数据分离，迁移代码已有备份、冲突保护和失败回滚。
- `requirements.txt` 固定 PySide6 与 Scrapling fetchers 的直接运行依赖，`requirements-build.txt` 固定 Nuitka 构建工具；SQLite 使用 Python 标准库。完整传递依赖锁与 MSVC 构建链仍未生成/验证。雨课堂动态抓取可能使用无头回退；交互阶段使用可见浏览器。只读调研没有运行爬取、解题、交互、模型请求或网站提交。

参考资料： [PySide `QThread`](https://doc.qt.io/qtforpython-6/PySide6/QtCore/QThread.html)、[PySide 部署](https://doc.qt.io/qtforpython-6/deployment/deployment-pyside6-deploy.html)、[Python `sqlite3`](https://docs.python.org/3.11/library/sqlite3.html)、[Scrapling 动态抓取](https://scrapling.readthedocs.io/en/latest/fetching/dynamic.html)、[Qt `QWebEngineView`](https://doc.qt.io/qt-6.8/qwebengineview.html)、[Tauri shell](https://v2.tauri.app/reference/javascript/shell/)、[Electron 文档](https://www.electronjs.org/docs/latest/)。

**原始只读调研提示词（已完成）**

> 你负责 AutoLearn 前端实现前的只读架构调研。先阅读项目根目录 AGENTS.md、structure.md 以及 core 下的入口、适配器、解题器、交互器和 Cookie Manager。结合 Windows EXE 目标、现有 Python 后端、Scrapling 可见浏览器、用户要求的应用内任务页面和最终前端提交确认，比较可行的桌面 GUI 技术、本机调用方式、任务元数据存储方式和浏览器显示方案。给出推荐方案、主要限制、依赖和需要调整的后端契约。不要修改文件、安装依赖、运行爬虫/解题/交互、访问模型服务或提交网站。结论应区分已确认需求和待用户确认假设。

### 子任务 1：任务模型、持久化和串行队列

依赖：子任务 0 的架构选择。  
交付：task_id/run_id 关系、状态迁移、任务持久化接口、单活跃任务队列、应用重启恢复规则。

**Agent 提示词**

> 你负责 AutoLearn 桌面应用的任务域与本地状态层。先确认用户已采纳 PySide/SQLite 推荐方案；如未确认，不要把建议当成已批准技术决策。方案获批后，用 Python `sqlite3` 实现 task 记录、状态迁移、`task_id/run_id` 关联、持久恢复和单 worker 串行队列；默认数据根目录为 `%LOCALAPPDATA%\AutoLearn`，题包仍保存在 `runs/<run_id>/`。任务保存 `model_profile_id`，不得保存 Cookie/API key；完整 URL 用私有存储通道与列表、日志、事件隔离，签名参数是否用 DPAPI 保护仍需产品确认。对运行中断、等待提交确认和 `unconfirmed` 设计恢复保护；恢复时核对运行包结果，绝不自动重试提交。不要碰首页组件、适配器提交逻辑或 EXE 打包文件；除非另行要求，不添加或运行测试。

**实现状态（2026-10-17；静态代码审阅，未运行测试）**

- 已在 `core/tasks/` 实现 SQLite 任务记录、状态迁移、`task_id` 与 `run_id` 关联、持久化恢复，以及 FIFO 单活跃任务队列；并加入本机实例锁和数据库层的单活跃任务约束。
- URL 保存在独立任务上下文中，不进入任务列表、一般事件或对象表示；任务记录只保存 `model_profile_id`，不保存 Cookie 或 API key。SQLite 中的 URL 当前未加密；发布策略已确认用 Windows DPAPI CurrentUser，代码和明文 URL 迁移尚未实现。
- 恢复逻辑会使等待中的提交确认失效，并对中断、`unconfirmed` 等需要核查页面的状态设保护，绝不自动重试提交。恢复时仅按关联 `run_id` 检查已有结果文件，不读取题包内容。
- `TaskStore` 已被首页、任务详情与 `TaskRuntimeController` 使用；SQLite、worker 和独立浏览器架构已获批准。数据库、`runs/`、配置和日志已统一到用户数据目录；SQLite schema 迁移先备份并事务更新，Windows URL 使用 DPAPI CurrentUser。数据库损坏恢复与迁移运行验收仍待完成。
- 未添加或运行测试，也未运行爬虫、解题、交互或访问模型服务。该实现状态描述代码结构，不代表运行环境或重启恢复行为已验收。

### 子任务 2：桌面应用框架与 GPT 风格视觉系统

依赖：PySide6/Qt Widgets 作为当前桌面壳层方向已获用户确认；EXE 打包兼容性由子任务 9 后续验证。  
交付：窗口框架、顶部导航、首页/任务/设置页面容器、基础组件和视觉变量。  
状态（2026-10-17）：已完成应用入口 `desktop.py`、顶部工作区导航和页面容器 `ui/main_window.py`、首页/设置/任务占位页 `ui/pages.py`、共享视觉组件 `ui/components.py` 及样式表 `ui/theme.py`。启动显示 Home；Home 固定在首位，任务页以可关闭标签打开；壳层最初只移除视图，当前关闭行为由子任务 3 按任务队列状态处理。没有地址栏或浏览器导航控件。已实现按钮、输入框、标签、状态提示、步骤条、通用确认对话框和空状态样式。此阶段不包含任务数据、后端调用、答案编辑或提交行为。Python AST 静态语法检查通过；已按用户授权在项目 `.venv` 安装 `PySide6==6.12.0`，新增 `requirements-desktop.txt` 并将 `.venv/` 加入 `.gitignore`。用隔离环境启动 `desktop.py` 后确认 AutoLearn 主窗口出现且 Windows 报告其响应正常；未进行控件交互/完整视觉验收，未添加或运行测试。Windows EXE 打包及后端完整依赖清单尚未验证/整理。

**实现范围记录**

> 你负责 AutoLearn 桌面前端的应用框架和视觉基础。使用已确认的 PySide6/Qt Widgets 方向制作 Windows 工作区：启动进入首页，顶部放 Home、打开中的任务标签和设置入口；页面简洁、有留白、状态清晰，不做通用浏览器，不加地址栏或浏览器导航键。建立统一的按钮、输入框、标签、步骤条、状态提示、对话框和空状态样式。只处理壳层和基础视觉组件；不得接任务存储、`QThread` 后端 worker、答案编辑或提交行为。用户已明确授权在项目隔离环境安装 PySide6 并完成启动验收；Windows EXE 打包兼容性留给发布任务验证。除非另有要求，不添加或运行测试。

### 子任务 3：首页任务管理与创建

依赖：子任务 1、2。  
交付：首页任务列表、筛选/搜索、新建草稿入口和安全的任务管理操作。

状态（2026-10-17）：SQLite 方案经用户确认后，首页直接调用 `TaskStore` 新建草稿、按名称/站点搜索、按适配器/状态筛选及列出任务；列表只消费 `TaskSummary`，不读取/展示完整任务 URL，也不建立第二份持久索引。任务可从列表打开为应用内标签。后续按用户要求调整关闭行为：运行中任务经确认后请求协作式停止，worker 确认停止并释放队列后才关闭标签；排队/暂停任务经确认后取消当前排队操作再关闭；关闭标签不删除任务记录或题包。草稿删除前展示影响说明并确认；已有 `run_id` 或非草稿任务默认提供归档，也可通过明确的“永久删除任务和题包”入口删除。永久删除前说明题目、映射、附件、答案、执行结果和任务记录均会丢失并二次确认；任务库拒绝删除仍有排队/活动操作的任务，并将题包路径限制在配置的 `runs/` 目录。当前关闭接口已接入子任务 5 worker；任务详情页整合后的活动停止端到端验收仍待完成。未运行爬虫、解题、交互或测试。

**Agent 提示词**

> 你负责 AutoLearn 首页任务面板。SQLite 方案已获用户确认；通过 `core.tasks.TaskStore` 提供新建任务、搜索、适配器/状态筛选、任务列表和任务标签打开行为，不要在 UI 另建第二份任务索引。新建后创建草稿并在应用内打开任务页；列表显示名称、适配器、当前步骤、状态和更新时间，不显示完整签名 URL。草稿可删除，有 run_id/结果的任务默认优先归档；同时提供明确标注的永久删除任务和题包入口，说明删除任务记录、题目、映射、附件、答案及执行结果的影响，并在用户确认后才执行。任何删除都拒绝排队或活动中的任务。关闭运行中标签时先确认，再请求 worker 协作式停止；只有收到停止完成且任务队列已释放的回执后才关闭，缺少停止处理器或停止失败时保留标签。排队/暂停任务先确认并取消排队操作后关闭；关闭标签本身不删除任务记录或题包。不得用 `QThread.terminate()` 强杀 worker。未经授权不运行爬虫/模型/交互；除非另有要求，不添加或运行测试。

### 子任务 4：设置页、模型配置档案和适配器认证状态

依赖：子任务 0、1、2。  
交付：模型配置管理、任务可选模型清单、密钥掩码、站点认证状态区。

**实现状态（2026-10-17；档案、设置页和任务页已接线，未做端到端验收）**

- 已核对当前 `config.json` 仍使用旧 `model`、`model1` 对象及 solver 所需字段。`ModelProfileStore` 将旧对象映射为确定性 UUID 档案 ID；新档案使用 UUID，配置修改递增档案版本，同时保留旧字段并把默认档案同步到兼容字段。
- 设置页支持档案选择、创建、编辑、默认项和版本显示。API key 输入为密码模式，不回显已保存值，拦截复制/剪切及拖拽导出；错误文案不附带密钥或配置值。Solver 可按 `model_profile_id` 读取配置，并在 profile 变更与任务创建时间比较后要求确认；确认版本与实际读取版本不一致时会拒绝继续。
- Cookie Manager 提供传入当前任务 `site_id`/URL、取消事件及状态回调的 UI 接口。设置页只接收固定状态码；带状态回调时后端不使用终端 `input()`，不向 UI 返回 Cookie 值。
- `TaskDetailPage` 已提供任务 URL/模型档案选择，并连接到模型档案变更确认、模型授权和按当前任务 URL 调用 Cookie Manager 的路径。Cookie UI 只接收安全状态；凭证值由后端管理。
- 未运行 Cookie Manager、解题器、外部请求或测试；API key 掩码、复制保护和错误脱敏均为静态代码审阅结论，尚未做 UI 安全验收。模型配置当前默认写用户数据目录 `config.json`；旧配置迁移代码已实现，尚未运行验收。

**Agent 提示词**

> 你负责 AutoLearn 设置页。先核对当前 `config.json` 结构与 solver 配置加载方式，再实现具稳定 ID/配置版本的模型档案；任务只保存 `model_profile_id`。档案被修改后，旧任务在解题前须提示并确认是否使用更新配置。API key 只在设置页掩码管理，不回显到日志、错误或普通剪贴板操作。Cookie Manager 的 UI 入口按当前任务 `site_id` 和 URL 发起，不通过终端 `input()`；UI 只接收安全认证状态，Cookie 值始终留在后端。保留旧配置字段与迁移能力，不复制真实凭证到源码/示例。不得运行 Cookie Manager 或外部请求；除非另有要求，不添加或运行测试。

### 子任务 5：本机后端桥接、任务 URL 和模型档案传递

依赖：子任务 0、1、4。  
交付：进程内结构化调用接口和单 worker 生命周期；按任务传 URL/模型选择；进度、取消、等待确认与安全错误状态。

**实现状态（2026-10-17；按用户批准的进程内 Python API + 单个 `QThread` 实现，任务详情页已接线，未做运行验收）**

- `ui/task_runtime.py` 的主线程控制器负责轮询/领取 `TaskStore` 队列、模型授权、提交确认、事件序号和结果结算；唯一 `QThread` 中的 worker 串行调用阶段，并通过 Qt signal 回传固定状态、阶段、计数和安全错误分类。结构化事件不包含 URL、Cookie、密钥、题目或答案全文；没有本机 HTTP 服务、子进程或终端输出解析。
- 阶段调用传入任务上下文和 `LocalCredentialProvider`；抓取、解题与交互均按任务 URL、`task_id`、`run_id`、`model_profile_id` 操作，不覆盖全局 URL。Cookie Manager 从当前任务读取站点和 URL，状态回调只向 UI 返回固定状态码，Cookie 值只在后端捕获并保存。
- 解题前先核对任务所选档案 ID/版本和题包题数、图片数，等待用户确认服务主机、提供方、模型和发送范围；用户确认后再次核对档案版本。恢复被暂停的解题队列也必须重新授权；其他明确暂停的非交互操作只能由用户显式恢复。
- 最终提交前由 `TaskStore.await_submit_confirmation()` 持久化一次性确认 ID 和过期时间，UI 只回传匹配的任务、run 和确认 ID。取消/过期不提交；两个适配器在确认后重新预检提交控件，并在最后提交点检查协作取消。提交请求结果不明时按网页证据记录 `unconfirmed`，不自动重试。
- 关闭标签或应用时先持久化 `request_cancel_active_operation()`，再设置 worker 取消事件；worker 结束后主线程调用 `finish_operation()` 记录结果并释放队列，最后通知 `MainWindow.complete_active_task_stop()`。UI 重读任务并确认队列不再活动后才关闭标签。重启时由 `TaskStore` 暂停未开始的排队工作、核对解题/交互结果并将不确定交互置为需检查状态，不重放确认或自动恢复提交。抓取 `run_id` 目前在题包成功后才关联到任务；进程在抓取中断时可能遗留未关联的部分目录，但任务会保持暂停且不会自动重跑，目录恢复/清理由后续策略处理。
- `TaskDetailPage` 和 `MainWindow` 已接入抓取、认证、解题授权、交互、暂停恢复、实时安全事件和停止处理入口。模型请求摘要与一次性提交确认都在 UI 侧经结构化事件处理。
- 动态浏览器和模型网络调用只能在模块提供的停止点响应取消，停止可能等待当前阻塞调用或用户关闭保留的浏览器窗口。未运行任务、浏览器、Cookie Manager、模型服务、网站提交或测试；worker 跨阶段和窗口关闭流程尚无端到端验收证据。

**Agent 提示词**

> 你负责 AutoLearn UI 与 Python 模块之间的本机桥接。SQLite 任务库方案已获用户批准；实施前仍需确认用户是否批准进程内 Python API 和单个 `QThread` worker。获批后不启动 HTTP 服务或子进程，也不解析终端输出。主线程负责 UI、队列和 SQLite；worker 串行执行阶段并通过 Qt signal 发结构化事件，确认/取消等输入通过线程安全队列或事件对象返回。所有阶段显式接收 `task_id`、`run_id`、私有任务 URL、`model_profile_id` 和后端凭证提供器，不覆盖全局 URL。事件包含序号、阶段、状态、必要计数和安全错误分类，不含 URL、Cookie、密钥、题目或答案全文。实现安全取消点、进度和恢复；解题前等待模型授权；Cookie Manager 按任务 URL 调用并只回传认证状态。接入 `MainWindow` 的活动任务停止处理器：收到关闭请求后使用可协作取消的信号/事件停止当前 worker，不得调用 `QThread.terminate()`；先通过 `TaskStore.request_cancel_active_operation()` 持久化停止请求，worker 到达安全停止点后调用 `finish_operation()` 记录结果、释放队列，再通过 `MainWindow.complete_active_task_stop()` 回报。只有 UI 重新读取任务并确认活动队列已释放后才能关闭标签。交互取消应按网页证据记录 `skipped`、`filled_unsubmitted` 或 `unconfirmed`；已发出提交请求但结果不明时不得声称已撤销或自动重试。不要运行真实任务；除非另有要求，不添加或运行测试。

### 子任务 6：任务详情向导与队列状态

依赖：子任务 1、2、3、5。  
交付：任务设置页、阶段步骤条、当前步骤面板、固定操作栏、排队/恢复状态显示。

**实现状态（2026-10-17；任务向导和主窗口接线已完成，未运行任务或测试）**

- `ui/pages.py` 已用 `TaskDetailPage` 替换任务占位页：按设置、抓取、题包检查、模型授权、解题、答案复核、填写/提交和结果展示阶段；任务详情只读取 `TaskStore.get_task()`，任务列表仍由首页使用不含 URL 的 `TaskSummary`。
- 任务设置写回 SQLite，站点和 URL 在抓取前可编辑，任务只存模型档案 ID；抓取前检查题包时显示逐题本地预览，解题后可按稳定题目 ID 编辑答案。抓取、认证、解题和网页填写均需用户从任务页明确发起；模型请求摘要显示去除 userinfo、query 和 fragment 的服务端点、提供方/模型、题数和图片数并等待单独授权。
- 任务页显示全局串行队列、操作阶段、进度计数和安全错误分类；暂停任务仅显式恢复，解题恢复会重新经过模型授权。不确定/中断/已填写未提交的交互必须由用户确认已检查网页状态并持久化恢复授权，随后再次确认开始填写；不自动重试提交。
- 提交按钮仅由 `SubmitConfirmationRequest` 路由到对应任务页；页面校验 SQLite 中仍为 `awaiting_submit_confirmation`、活动交互、相同 `run_id`、未过期的 `confirmation_id` 后才显示一次性确认。`ui/task_runtime.py` 屏蔽持久化前的同名进度回调，只在 `TaskStore.await_submit_confirmation()` 成功后发 `awaiting_submit_confirmation` 结构化事件和请求。页面确认只回传 task/run/confirmation ID 与决定，不操作网站控件。
- 任务页文案和确认面板按已获批准的“独立 Scrapling 可见浏览器 + PySide 主窗口”方案接线；没有嵌入 `QWebEngineView` 或假定共享 Playwright 上下文。本次没有启动或验证浏览器。中断恢复仍以 SQLite 状态为准，启动时不自动重放交互。
- `TaskRecord` 现提供持久化 `interaction_retry_authorized` 详情字段，让用户已检查网页后授权的恢复步骤可跨任务页重开显示；首页 `TaskSummary` 不暴露该字段。
- **尚未端到端验收：** 未启动任务、浏览器、模型服务或 Cookie Manager；未访问真实站点、提交网站或运行测试。逐题题包预览/答案编辑已由子任务 7 实现；Windows EXE 打包仍属于子任务 9。

**Agent 提示词**

> 你负责 AutoLearn 的任务详情向导界面。按顺序呈现适配器/URL/模型、抓取、题包检查、模型授权、解题、答案复核、填写与前端提交确认、结果。若用户确认推荐浏览器方案，任务页和提交确认留在 PySide 主窗口，网页本身由独立 Scrapling 可见浏览器承载；不得假定 `QWebEngineView` 能共享 Playwright 上下文。根据用户批准的任务库/worker 结构化状态展示阶段、串行队列和安全恢复入口。模型请求前展示服务目的地、模型、题数和图片数并等待授权。仅当后端持久化并报告 `awaiting_submit_confirmation` 后显示确认，且确认事件绑定任务与一次性有效 `confirmation_id`。UI 不操作网站控件；未经授权不访问真实站点或模型服务；除非另有要求，不添加或运行测试。

### 子任务 7：题包预览与可编辑答案复核

依赖：子任务 6、5。  
交付：题目预览、图片附件显示、答案编辑器、保存与校验错误提示。

**实现状态（2026-10-17；已接入本地题包预览与答案保存，未运行测试）**

- `core/solver/answer_review.py` 提供只针对本地任务题包的结构化加载/保存接口；以 SQLite 当前任务的 `site_id`/`run_id` 定位 `runs/<run_id>/`，校验 questions/answers Schema、适配器题目校验器、solver 答案跨文件引用和题包本地附件路径。
- 答案通过同目录临时文件、`fsync` 和 `os.replace()` 原子替换 `answers.json`；验证或写入失败时不覆盖原文件，并以固定安全分类返回错误。SQLite 不保存答案全文，接口不调用模型或网络服务。
- `TaskDetailPage` 在题包检查阶段按 `question_id` 显示题干、选项、题型、空位及本地图片附件；答案复核阶段提供单选、多选、填空和简答编辑，编辑须通过后端校验并保存后才能继续。
- 对填写后的任务，仅在用户先检查网页并授权交互恢复后开放答案编辑；保存后要求重新开始填写并由后端再次验证页面回显。模型解题、网页交互及提交流程没有在本子任务中运行。
- 附件预览按包内相对路径加载本地图片，不渲染题包 HTML 或外部资源；答案编辑内容不写日志，也不写入任务库。
- **未运行测试或端到端任务。**

**Agent 提示词**

> 你负责 AutoLearn 题包检查和答案复核界面。按稳定 `question_id` 展示题干、选项、题型、空位及本地附件；允许编辑答案。编辑通过后端结构化保存接口，按适配器 answers Schema 和 solver.py 跨文件引用规则校验，并安全原子写入 `runs/<run_id>/answers.json`。答案题包仍以现有 JSON 文件为准，SQLite 不存答案全文。保存失败保留先前有效答案，不写答案日志；填写后再次编辑须重新填写并验证回显。不要请求模型；除非另有要求，不添加或运行测试。

### 子任务 8：前端最终确认协议与两个适配器交互契约

依赖：子任务 0、5、6。  
交付：等待确认/确认/取消双向协议；Chaoxing、长江雨课堂和公共 runner 的一致实现；Schema、README 和契约文档同步。

**实现状态（2026-10-17；契约与代码已同步，未访问站点或提交）**

- 公共 interaction runner、两个站点适配器及其 `execution_result` Schema 已实现桌面等待/确认/取消协议；等待点先持久化 `awaiting_submit_confirmation`，并使用绑定 `task_id`、`run_id`、一次性 `confirmation_id` 和过期时间的事件。
- UI 只允许匹配且未过期的决定；确认后适配器重新读取页面并验证提交控件唯一、可见、启用及表单归属。取消、超时、重复/错配事件和应用失联均不提交，结果仍按网页证据确定。README 与根目录开发约定已同步。
- 只做静态审阅；未访问真实作业页、未触发网站提交，未运行测试。浏览器上下文保持、适配器页面证据分类和真实站点差异尚未运行验收。

**Agent 提示词**

> 你负责将最终提交确认从浏览器内面板迁移到桌面前端，并同步公共 interaction runner、两个适配器、execution_result Schema、README 和开发契约。实施前确认前端/浏览器承载方案已获用户批准。全页预检、填写及回显验证后，先持久化 `awaiting_submit_confirmation` 并向 UI 发含 `task_id`、`run_id`、一次性 `confirmation_id`、过期时间的结构化事件；暂停时保持 Scrapling 页面/worker 上下文。UI 只发送匹配且未过期的 confirm/cancel。confirm 后适配器重新查询并核验唯一、可见、启用且属于当前页面的提交控件，再决定是否点击；cancel、重复/过期/错配事件、应用失联均不得提交。网站结果仍由后端依据页面证据记录 `confirmed`、`failed`、`unconfirmed`、`skipped` 或 `filled_unsubmitted`。不得访问真实作业页或提交；除非另有要求，不添加或运行测试。

### 子任务 9：Windows EXE 打包与本地运行体验

依赖：子任务 0 至 8 的技术和接口稳定。  
交付：可重复的 Windows 发布方案、运行数据目录约定、依赖/Chrome 提示、配置迁移和升级说明。

**方案调研状态（2026-10-17；本轮构建预检未通过，未安装依赖或运行应用测试）**

- 项目固定 `PySide6==6.12.0`、`scrapling[fetchers]==0.4.15`；当前 `.venv` 使用 Python 3.12.14，但没有 Scrapling、Playwright、Patchright、Nuitka、ordered-set 或 zstandard。`requirements-build.txt` 固定构建依赖；当前机器为 x64、Windows 11 25H2、build 26200，但没有 `cl.exe`、`dumpbin.exe`、`msbuild.exe`、Windows SDK x64 库或 `makensis.exe`，因此不能生成本机发布产物。完整传递依赖锁仍须在隔离发布环境生成。
- 推荐以 `pyside6-deploy` 的 standalone 目录作为构建产物，并用 per-user 安装包安装到 `%LOCALAPPDATA%\Programs\AutoLearn`。目录分发适合便携 ZIP 和排错；onefile 适合单文件分发，但会在启动时解包到临时目录，包含浏览器二进制时启动/体积负担更大。正式安装应保留 `%LOCALAPPDATA%\AutoLearn`，不把用户数据放进安装目录。
- PySide/Qt 6.12 官方支持 Windows 10 1809+ x86_64 和 Windows 11 x86_64，支持 MSVC 2022 或 MinGW-w64；Qt 6.12 是最后一个支持 Windows 10 的版本。考虑到 Windows 10 已于 2025-10-14 结束支持，新发布默认建议 Windows 11 x86_64；是否额外兼容 Windows 10 和最低版本仍需产品确认。
- 只读安装资源包括应用后端模块、动态适配器模块、Schema、适配器 README、solver prompt、Qt/Python 运行库，以及 Scrapling/Playwright/Patchright Python 包数据。Scrapling 浏览器二进制不进入安装包，按既定策略首次由用户发起安装。可写用户数据包括 `config.json`、`tasks.sqlite3`、`runs/`、`logs/` 和 `ChromeProfiles/`；配置中的 API key/Cookie、任务 URL、题包及任务记录均不进入 EXE 或安装资源。
- 路径代码已提供只读 `resource_path()` 和可写 `data_path()`。`pyproject.toml` 已配置动态适配器代码、各 Schema/README、solver prompt 及 Scrapling/Playwright/Patchright 包数据的 standalone 收集；浏览器二进制按既定策略不打包。系统 Chrome 只供 Cookie Manager `DynamicSession(real_chrome=True)` 使用，其他浏览器抓取依赖 Scrapling 首次安装的浏览器。
- 首次启动自动从旧源码/安装目录迁移 `config.json`、`runs/` 和 `logs/`：校验配置 JSON 与已知字段类型，备份并校验复制内容，原子安装并保留旧来源；冲突时安全失败，不覆盖已有用户数据。SQLite 已有数据默认就在用户数据目录，schema 升级通过 SQLite backup API 备份并用事务迁移；Windows URL 在主数据库和迁移备份中均使用 DPAPI CurrentUser。迁移从不输出凭证、Cookie、题目或签名 URL。
- 本轮运行 `pyside6-deploy --help` 成功；`pyside6-deploy -c pysidedeploy.spec --dry-run --no-install` 因当前缺少 Nuitka 4.1.1 而退出，并提示未找到 `dumpbin`。新增 `scripts/package_windows.ps1` 作为构建/资源排除预检入口、`installer/AutoLearn.nsi` 作为 per-user 安装定义，以及 `docs/windows-release.md` 作为升级/卸载验收清单。PowerShell 脚本已做语法解析；未编译 NSIS、未构建 standalone、未安装应用或执行升级/卸载验证。
- 安装定义固定 `%LOCALAPPDATA%\Programs\AutoLearn`，要求 Windows 11 25H2+ x64，以普通当前用户权限运行并只写 HKCU；更新前运行旧版卸载程序，按本版 standalone 生成的逐文件清单删除程序文件，不递归删除目录；卸载不访问 `%LOCALAPPDATA%\AutoLearn`。旧源码目录不能由新安装位置自动发现，源码迁移须在仍能访问旧目录的版本中先完成。
- 下一阶段需在隔离发布环境生成并审阅完整依赖锁，补齐 MSVC 2022 Build Tools/Windows SDK、Nuitka 和 NSIS，执行 standalone/安装器构建，再使用独立 Windows 测试用户完成启动、升级、卸载和 AppData 保留验收。不得使用本机现有配置/题包执行迁移或安装验收。

**Agent 提示词**

> 你负责 AutoLearn Windows EXE 发布与打包方案。以 PySide6/Qt Widgets 和 `pyside6-deploy` 为候选；比较应用目录、单文件和安装包形式，并核实 MSVC 工具链及 Scrapling 浏览器组件。安装资源（后端模块、Schema、README、prompt）与 `%LOCALAPPDATA%\AutoLearn` 可写数据（配置、SQLite、runs、日志）分离；凭证和任务数据不得嵌入 EXE。说明首次运行是否要求系统 Chrome、Cookie Manager 的 `real_chrome` 配置、迁移和更新方式。不得构建/发布带真实密钥的包或安装未经批准的依赖；除非另有要求，不添加或运行测试。

## 前端整体进度（静态审阅，2026-10-17）

| 子任务 | 当前结论 | 未完成/未验证 |
| --- | --- | --- |
| 0. 架构调研 | 已完成；PySide6、SQLite、进程内 Python API + 单 `QThread`、独立 Scrapling 浏览器已获批准；AppData 路径已统一。 | Windows 发布兼容性与安装包验收。 |
| 1. 任务域与持久化 | SQLite task 状态、schema 版本、run 关联、FIFO 单活跃队列和恢复保护已实现；Windows URL 使用 DPAPI CurrentUser，schema 升级先备份并事务迁移。 | DB 损坏恢复策略未实现；未运行恢复验收。 |
| 2. 桌面壳层 | `desktop.py`、主窗口导航、标签、基础控件与主题已实现；此前计划记录过隔离环境启动成功。 | 本轮未重启应用；完整交互/视觉验收和 EXE 兼容性。 |
| 3. 首页任务面板 | SQLite 列表、筛选、新建、打开、归档/删除和队列提示已接线。 | 关闭活动任务后的 worker/队列端到端验收。 |
| 4. 设置与模型档案 | 稳定 profile ID/版本、旧配置兼容、密钥掩码和 Cookie Manager 安全状态入口已实现并与任务页接线；配置使用用户数据目录。 | UI 安全行为未实测。 |
| 5. 本机桥接 | 单 worker、结构化事件、任务上下文、模型授权、协作取消和提交确认桥接已实现。 | 浏览器/网络阶段取消和窗口停止流程未运行验收。 |
| 6. 任务向导 | 任务设置、阶段、串行队列、授权/恢复/提交确认已接线。 | 未运行任务或浏览器；无端到端验收。 |
| 7. 题包与答案复核 | 本地预览、附件显示、结构化答案保存、Schema/引用校验和原子替换已实现。 | 未运行验证；答案编辑控件和图片视觉效果未实测。 |
| 8. 最终提交协议 | 公共 runner、两适配器、结果 Schema、README 和开发契约已同步为桌面一次性确认。 | 未访问真实站点或触发提交；仅完成静态审阅。 |
| 9. EXE 发布 | 发布策略、资源清单、Windows 构建预检脚本和 per-user NSIS 安装定义已配置；Chrome 仅作为 Cookie Manager `real_chrome=True` 的系统前置项。 | 当前机缺少 Scrapling 浏览器 Python 包、Nuitka、MSVC/SDK 和 NSIS，未生成 standalone/安装包；完整传递依赖锁、独立用户的启动/升级/卸载验收、应用内 Scrapling 浏览器首次安装流程、迁移与 DPAPI 故障回滚仍未验收。 |

**横向发布阻塞项：** `resource_path()` / `data_path()`、旧配置/题包/日志迁移与 SQLite URL DPAPI 已完成静态实现；依赖直接版本和打包资源收集规则已配置。尚未生成完整传递依赖锁，也未实际运行迁移、故障回滚或 Windows 构建验收。

## 下一阶段工作建议（分小任务推进）

建议按依赖顺序分阶段完成发布准备；路径与迁移代码已完成，接下来锁定依赖并构建安装包。各阶段交付应可单独审阅。

| 顺序 | 工作项 | 交付与完成条件 | 前置决策/依赖 |
| --- | --- | --- | --- |
| 1 | 发布策略确认（已完成） | Windows 11 25H2+ x64；`pyside6-deploy --mode standalone` + per-user 安装包；系统 Chrome Stable x64 仅为 Cookie Manager 前置条件；Scrapling 浏览器依赖按需首次安装；SQLite URL 采用 DPAPI CurrentUser。 | 已确认；路径代码已完成，后续补齐依赖清单和 Windows 构建。 |
| 2 | 数据和资源路径统一（代码已实现） | 只读 `resource_path()`、可写 `data_path()`；配置、凭证、`runs/`、日志、SQLite 和 Chrome profile 统一到用户数据目录；旧配置/题包/日志备份迁移与冲突保护；SQLite backup API、事务升级和 DPAPI URL 保护。 | 第 1 项；仍需授权后安排本机迁移/故障回滚验收。 |
| 3 | 依赖与打包资源清单（规则已配置） | 直接运行/构建依赖版本已固定；`pyproject.toml` 配置动态适配器、Schema、README、prompt 和 Scrapling/Playwright/Patchright Python 包数据收集。浏览器二进制由用户首次主动安装；系统 Chrome 仅为 Cookie Manager `real_chrome=True` 的前置要求。 | 第 1、2 项；完整传递依赖锁、应用内浏览器安装/进度流程和构建收集结果须在隔离 Windows 发布环境验证；不携带配置、凭证、题包、日志或用户数据。 |
| 4 | Windows 构建与安装 | 在确定的 Windows 构建机上验证 MSVC/SDK 和 `pyside6-deploy`，生成 standalone 构建产物及 per-user 安装包；检查升级、卸载和 AppData 保留约定。 | 第 1–3 项；使用隔离、无真实用户数据的发布环境。 |
| 5 | 分层验收 | 先检查干净环境安装、启动、数据目录、迁移和不含敏感文件的产物；再单独安排授权后的 Scrapling 浏览器、模型与真实站点流程验收。 | 第 4 项；模型请求、真实网站访问和提交仍需各自明确授权。 |

`ui/pages.py`、`ui/task_runtime.py` 和 `core/tasks/store.py` 的职责拆分作为可选维护性工作，放在路径/发布契约稳定之后；不是构建 EXE 的前置条件。当前项目约定下不默认添加或运行测试，验收范围需按后续明确要求执行。

## 7. 子任务依赖总览

| 子任务 | 依赖 |
| --- | --- |
| 0. 技术调研 | 无 |
| 1. 任务域与持久化 | 0 |
| 2. 桌面框架与视觉 | 0 |
| 3. 首页任务管理 | 1、2 |
| 4. 设置/模型配置 UI | 0、1、2 |
| 5. 本机后端桥接与任务上下文 | 0、1、4 |
| 6. 任务详情向导 | 1、2、3、5 |
| 7. 题目预览与答案编辑 | 5、6 |
| 8. 最终确认协议与契约同步 | 0、5、6 |
| 9. Windows EXE 打包 | 0–8 |

实际顺序需按子任务提示中的依赖调整；前后端契约和适配器交互改动应先确定接口，再并行实现互不冲突的 UI 页面。

## 8. 已确认事项与待确认假设

### 已确认

1. PySide6/Qt Widgets 作为桌面壳层；Windows EXE 是最终交付目标。
2. SQLite `TaskStore` 保存任务元数据和串行队列；题目、答案、交互映射及执行结果继续保留在 `runs/<run_id>/` JSON/附件中。
3. 主线程负责 UI、队列和 SQLite；进程内 Python API 由单个 `QThread` worker 串行执行，Qt signal 传递结构化状态；不启动本地 HTTP 服务或后端子进程。
4. AutoLearn 主窗口承载任务页与最终提交确认；网站页面由独立 Scrapling 可见浏览器承载。不得假设 `QWebEngineView` 与 Playwright 上下文共享。
5. 任务仅存 `model_profile_id`；API key/Cookie 只留在后端配置/凭证层。profile 更新时，旧任务在解题前须明确确认。
6. 永久删除任务和题包需明确确认；提交结果不确定时先检查页面，永不自动重试。
7. **Windows 发布策略（2026-10-17）：**
   - 最低目标为 Windows 11 25H2、x64；新版本 Windows 11 在构建验收中覆盖。Windows 10 和 ARM64 不在首版目标范围内。若正式发布晚于 2027-10-13，重新检查 25H2 Home/Pro 的支持状态并调整最低版本。
   - 用 `pyside6-deploy --mode standalone` 构建目录，再封装无需管理员权限的 per-user 安装包；不采用 onefile。应用安装至 `%LOCALAPPDATA%\Programs\AutoLearn`，数据位于 `%LOCALAPPDATA%\AutoLearn`，卸载默认保留数据。
   - Cookie Manager 当前通过 Scrapling `DynamicSession(real_chrome=True)` 使用本机安装的 Google Chrome Stable x64；不随 AutoLearn 安装 Chrome，不改写 Chrome 企业策略。Cookie Manager 使用按站点隔离的独立 profile；缺少 Chrome 或策略阻止启动时给出可操作错误。
   - 其余 Scrapling 浏览器 fetcher 所需的浏览器依赖不放进应用安装包。首次需要浏览器的操作前，由用户发起下载/安装，落在当前用户缓存；依赖版本与应用锁定版本一致。当前 `scrapling install` 会下载全部浏览器和相关依赖，首次安装需要网络和数百 MiB 级磁盘空间；界面应先说明并显示进度。离线或被策略阻止时应安全停止并给出说明，不得静默绕过策略。
   - 发布版 SQLite 中保存的完整任务 URL 使用 Windows DPAPI CurrentUser 加密，不使用 LocalMachine 范围。现有明文 URL 需在数据库迁移时加密；加/解密失败时安全失败，不得回退明文。DPAPI 数据通常绑定当前 Windows 用户和设备，不能承诺跨用户/设备恢复。

   参考依据： [Windows 11 Home/Pro 生命周期](https://learn.microsoft.com/en-us/lifecycle/products/windows-11-home-and-pro)、[Playwright Python 安装与系统要求](https://playwright.dev/python/docs/intro)、[Playwright 浏览器安装与缓存](https://playwright.dev/python/docs/browsers)、[Scrapling 官方安装说明](https://scrapling.readthedocs.io/en/latest/)、[PySide6 部署](https://doc.qt.io/qtforpython-6/deployment/deployment-pyside6-deploy.html)、[Microsoft DPAPI `CryptProtectData`](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata)。

### 待完成事项或待产品决策

1. **迁移验收：** 路径迁移、SQLite schema/DPAPI 迁移及回滚逻辑已有代码，尚未在含旧数据的隔离副本上运行。SQLite 损坏恢复策略仍待实现。
2. **模型授权版本：** 当前实现会在档案更新后要求旧任务确认使用新配置；是否还需把授权时档案版本/摘要持久化用于审计，可由产品确认。

## 9. 全局实现约束

以下约束适用于全部 agent 提示词和后续实现：

- 先阅读项目根目录 AGENTS.md、structure.md 及相关适配器 README；遵守 Scrapling、域名、Cookie、Schema、日志和人工确认要求。
- 实现按用户明确授权的范围进行；桌面壳层、项目隔离环境中的 PySide6 安装及启动验收已获授权并完成。该授权不覆盖其他依赖安装、运行爬虫/解题/交互、访问模型服务或提交网站；这些仍须获得对应明确授权。
- 不读取或输出 config.json 的凭证值，不把题目/答案全文、Cookie、API key、访问令牌、签名 URL 放进日志、任务列表或普通事件载荷。
- 不通过 UI 确认绕过页面结构预检；提交依然需要后端验证实时提交控件和页面结果证据。
- 不因多任务设计而并发控制多个浏览器/提交操作。队列串行执行。
- 不自动覆盖已有 answers.json 或 execution_result.json；只有任务明确进入受控的重新解题/交互流程时才按对应覆盖约定处理。
- 未经用户针对具体 run_id、题数、图片数和目标服务的明确授权，不发送真实题包到模型服务。
- 未经用户明确确认，不对真实网站提交作业。
- 除非用户另行要求，不新增或运行测试；各实现任务应记录尚未验证的运行环境、验收证据和集成依赖。

