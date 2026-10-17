# 更新记录

## 未发布

### 新增

- 雨课堂适配器支持 UEditor 富文本答题区（考试页主观题）：抓取时识别 `iframe#ueditor_<n>` 并记录 `frame_selector`，填写时进入该 iframe 的可编辑元素写入、回读并派发 `input`/`change`/`blur` 事件；交互映射 schema 增加可选 `frame_selector`。

### 修复

- 抓取找不到答题控件时按实际页面结构给出明确原因（UEditor iframe 缺失、Quill/TinyMCE/wangEditor 等不支持的编辑器），不再统一报“未找到可记录的答题控件”。
- `setup.bat` 重写 Python 3.12 探测：`py -3.12` 不可用时自动查找 uv 管理的 3.12 解释器和常见安装目录，并支持用 `AUTOLEARN_PYTHON312` 指定；找不到 3.12 时明确报错，不再回退到默认 Python。
- `setup.bat` 识别随项目移动而失效的 `.venv`（脚本启动器指向旧路径）并自动重建，避免 `pip.exe` / `scrapling.exe` 静默失效。

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