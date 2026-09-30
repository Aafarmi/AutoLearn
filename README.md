# AutoLearn

AutoLearn 是一个用视觉模型读取页面、再自动完成页面操作的 Python 项目。这个 README 面向第一次接触项目的开发者：先说明它能做什么、怎样运行和从哪里开始读；完整目录结构与模块职责见 [`.workbuddy/memory/PROJECT_STRUCTURE.md`](.workbuddy/memory/PROJECT_STRUCTURE.md)。

> **重要：**程序会在你选定的页面上点击，某些任务还会提交答案。请先在可控页面或测试靶场中验证，不要直接对重要、计分或不可撤销的任务启用自动操作。使用前确认你有权使用目标站点，并遵守其规则。

## 先了解它做什么

一次刷题任务的大致流程是：

1. 连接用户选定的浏览器标签页或窗口，并截取当前视口。
2. **视觉模型**把画面转成题目和页面状态；它不确定或发现内容被截断时，程序会暂停，而不是猜测。
3. **解题模型**只接收识别后的文本，不接收截图。
4. 程序按页面坐标执行操作，并用后续截图检查页面是否发生变化。
5. 运行过程写入任务状态、事件和调试留痕，供界面查看。

网课场景还会读取视频播放状态并处理弹题。项目不通过题目 DOM 或网络请求读取题目；媒体状态是例外。由于题面识别和点击都存在模型误差，截图差分也不等于确认答案正确，自动化结果仍需要人工监督。

## 运行项目（Windows）

### 环境要求

- Python **3.13 或更高版本**。
- 本机安装 Edge（默认）或 Chrome。
- 至少配置一个支持图像输入的模型，用于视觉识别；首次运行后在「模型库」中填写服务地址、模型名称和 API Key。密钥由系统凭据管理器保存，不写入普通模型配置文件。

### 启动

在项目根目录双击 `run.bat`。它会创建 `.venv`、安装运行依赖并启动本地控制台：

- 控制台：<http://127.0.0.1:8800>
- API 文档：<http://127.0.0.1:8800/api/docs>

也可以在项目根目录执行：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m uvicorn ui.server:create_app --factory --port 8800
```

手动方式启动后，若依赖缺失请先安装；若 8800 端口已被占用，先在已有控制台使用「关机」或确认占用进程后再启动。不要同时启动多个实例。

### 第一次使用

1. 打开控制台，在「模型库」添加视觉模型和解题模型（可配置为同一模型）。
2. 新建任务，选择要操作的浏览器页面或窗口、任务类型和运行选项。
3. 检查目标页面和任务设置后再启动；运行中留意暂停原因和人工确认提示。
4. 任务结束后，从任务详情查看过程、截图和日志。使用控制台右上角的关机入口停止本程序。

视觉模型是读取题目的必需项；没有可用的视觉模型时，启动预检会阻止任务开始。模型 API 调用可能产生费用，具体取决于所配置的服务商和模型。

## 开发与测试

开发依赖包括 pytest、ruff 和 mypy：

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:AUTOLEARN_SECRET_BACKEND = "memory"
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy
```

设置 `AUTOLEARN_SECRET_BACKEND=memory` 是为了让测试使用内存凭据后端，避免把测试密钥写入 Windows 凭据库。

测试靶场不是产品运行时自动启动的一部分，需要时另开终端运行：

```powershell
.venv\Scripts\python.exe scripts/serve_mock.py
```

靶场服务地址为 <http://127.0.0.1:8899>（跨域 frame 使用 8900 端口）。需要跑浏览器验收时，让靶场保持运行；可进一步运行 `scripts/check_mock.py --all`、`scripts/check_ui.py` 和 `scripts/check_target.py`。部分浏览器集成测试在靶场未运行时会跳过，因此单看 pytest 通过数不能确认这些用例已执行。

## 接下来读什么

第一次准备改代码时，先打开 [项目结构与阅读地图](.workbuddy/memory/PROJECT_STRUCTURE.md)，其中集中整理了目录职责、主流程、模块依赖、数据落盘位置和按改动类型的阅读路线。提示词的唯一真源是 `prompts/*.md`；提示词在服务启动时加载，修改后重启服务再验证。

## 当前边界

- 项目重点是基于截图的页面读取与选项点击；不要假设任意题型、任意站点或桌面应用都已适配。
- 自动化不保证模型识别或作答正确。识别门禁、暂停和留痕是安全措施，不是正确性证明。
- 测试靶场只用于开发和回归，不代表真实站点适配完成。

## 文档入口

- 目录、模块职责与维护路线：[`PROJECT_STRUCTURE.md`](.workbuddy/memory/PROJECT_STRUCTURE.md)
- 视觉组与解题组的交接契约：[`docs/双模型分工-视觉组与解题组.md`](docs/双模型分工-视觉组与解题组.md)
- 真实站点实测手册：[`docs/真实站点作业做题手册.md`](docs/真实站点作业做题手册.md)
- 需求与规划：[`docs/AutoLearn-项目任务书.md`](docs/AutoLearn-项目任务书.md)、[`docs/AutoLearn-实施规划书.md`](docs/AutoLearn-实施规划书.md)
- 面向维护者的长期约束与历史记录：`.workbuddy/memory/` 下的 `MEMORY.md`、`REFERENCE.md` 与日期记录。它们包含历史快照；如与当前源码冲突，以当前源码和测试为准。

## 项目版本

当前版本以 [`pyproject.toml`](pyproject.toml) 中的 `version` 为准。
