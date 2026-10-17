# 更新记录

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