# 更新记录

## 未发布

### 变更

- WebUI 界面精简：去掉每个步骤卡片内的说明段落、英文眉标（`TASK QUEUE` / `NEW TASK` / `LOCAL SETTINGS` 等）以及重复的状态区，改为「步骤条即页面标题」+ 一行状态点 + 结构化事实行的表达方式，只保留操作必需的文字。
- 任务详情页头部只保留一处状态徽章 + 站点 + 更新时间；原「当前串行操作」卡片降级为一行队列提示，事件记录折叠为「事件记录 · N」抽屉，归档 / 删除改为页脚文字按钮。
- 右侧内容区限宽 940px 并居中，左栏与顶部栏收紧；服务状态改为圆点 + 短词（已连接 / 未连接 / 已关闭）。
- 题目卡片不再为空附件渲染占位网格，选项行间距随之收窄；模型授权、提交确认弹窗只保留授权判断所需的信息行。

## v1.0.4 - 2026-10-18

### 新增

- 雨课堂适配器支持 UEditor 富文本答题区（考试页主观题）：抓取时识别 `iframe#ueditor_<n>` 并记录 `frame_selector`，填写时进入该 iframe 的可编辑元素写入、回读并派发 `input`/`change`/`blur` 事件；交互映射 schema 增加可选 `frame_selector`。
- 抓 Cookie 时浏览器改为先打开雨课堂首页（适配器新增可选 `CAPTURE_START_URL`，当前为 `https://changjiang.yuketang.cn/web`）：任务 URL 不变，未登录状态下打开作业/考试深链容易失败的问题由此规避，由用户登录后手动进入目标页。
- 适配器可声明 `CAPTURE_HINT`，WebUI 在点击“抓取 Cookie / 更新登录验证”时弹窗展示该提示；任务接口新增 `authentication_hint` 字段。
- 抓取题目失败且任务尚未生成题包时，允许在“登录验证”页刷新 Cookie：状态机新增 `failed(抓取) → authenticating → draft` 这一条窄路径（要求 `retry_failed=true` 且 `run_id` 为空），解题/填写失败的任务仍然禁止。
- `/api/tasks/{task_id}/authenticate` 支持可选 `retry_failed` 字段。

### 修复

- 抓取找不到答题控件时按实际页面结构给出明确原因（UEditor iframe 缺失、Quill/TinyMCE/wangEditor 等不支持的编辑器），不再统一报“未找到可记录的答题控件”。
- `setup.bat` 重写 Python 3.12 探测：`py -3.12` 不可用时自动查找 uv 管理的 3.12 解释器和常见安装目录，并支持用 `AUTOLEARN_PYTHON312` 指定；找不到 3.12 时明确报错，不再回退到默认 Python。
- `setup.bat` 识别随项目移动而失效的 `.venv`（脚本启动器指向旧路径）并自动重建，避免 `pip.exe` / `scrapling.exe` 静默失效。
- 抓取遇到考试页单页面限制时给出准确原因：识别页面被跳转到 `/warn`（“请勿同时开启多个考试页面”），不再笼统报“静态和浏览器加载后都没有识别到题目容器”；浏览器渲染抛异常时也会二次探测页面，命中该限制同样返回明确提示。
- 登录验证结束后间隔不足约 30 秒再抓取时，先等待再请求页面（新增进度状态 `waiting_exam_session`），避免抓 Cookie 打开过的考试页仍被服务端算作在线而触发 `/warn`。
- 抓取失败原因写入 `%LOCALAPPDATA%\AutoLearn\logs\crawl.log`（URL 与 Cookie 字段脱敏），解决失败只有 `operation_failed` 分类、无法定位的问题。
- 修复 `cjyuketang` 爬虫独立命令在失败时抛 `NameError: DataMigrationError`（漏 import），真实抓取错误不再被掩盖。
- 客观题试卷题干被判为空：部分试卷（如 37 题的客观题卷）题干放在 `h4` 之外的 `.item-body` 直接子节点里，`h4` 本身为空。题干定位新增「`.item-body` 直接子节点、排除选项列表」兜底，并在报错里附上题干容器的标签 / 类名指纹，便于定位未适配的结构。
- 选项里带图片时点击会落在图片上、选项没有被选中：选择动作改为对内层 `input[type=radio|checkbox]` 做 DOM 点击（不受图片遮挡与其预览 / 缩放处理器影响），点击后最多等 1 秒确认选中状态；失败时报出具体题号与选项。填写中断时，状态面板与任务结果里也会带上这条具体原因。

## v1.0.2 - 2026-10-17

### 新增

- 模型档案页新增“测试连接”：保存前可用当前填写的 URL、模型和密钥发送一条极小请求，返回连通状态、响应耗时和简短回复；失败按网络、密钥、模型不存在等分类提示，且不写入任务记录。

## v1.0.1 - 2026-10-17

### 变更

- Cookie 抓取每次调用空 profile，不再实现 Cookie 持久化：每次抓取都在系统临时目录新建空白 Chrome profile，浏览器以未登录状态启动，抓取结束后删除该目录，不复用任何历史登录状态。
- `%LOCALAPPDATA%\AutoLearn\ChromeProfiles\` 不再写入；旧版本遗留的目录可删除。

## v1.0.0 - 2026-10-17

AutoLearn 首个正式发布版本。

### 新增

- 本机 localhost WebUI，串联题目抓取、模型解题、答案复核和网页填写。
- 固定解题核心与可插拔站点适配器架构。
- 当前内置两个站点适配器，可按相同结构继续扩展更多网页。
- 任务队列、模型档案、答案复核、一次性提交确认和本机数据迁移。
- 基于 Scrapling 的网页抓取、浏览器渲染和 Cookie 管理。
- Windows 一键安装并启动脚本 `setup.bat`。

### 安装

- Windows 用户会话
- Python 3.12 x64
- Google Chrome Stable x64
- 执行 `.\setup.bat`

### 开源组件

- Scrapling（BSD 3-Clause License）
- FastAPI（MIT License）
- Uvicorn（BSD 3-Clause License）