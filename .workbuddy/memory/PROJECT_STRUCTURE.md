# AutoLearn 项目结构与阅读地图

> 为第一次阅读代码、接手维护或排查问题的人整理。内容描述当前仓库布局；若本说明与代码不一致，以源码和测试为准。README 保持为快速入门，结构细节集中在本文。

## 1. 项目定位与主流程

AutoLearn 是 Python 页面自动化工具，主要面向在线刷题和网课中断处理。刷题主流程如下：

```text
选择浏览器页面/窗口
   ↓
perception/vision_probe.py 截取视口
   ↓
solve/reader.py + 视觉模型：识别题目与页面状态
   ↓
core/run_plan.py：结合观测确定运行方案
   ↓
solve/solver.py + 解题模型：根据文字题面作答
   ↓
act/actuator.py：按归一化坐标执行点击/提交
   ↓
act/verifier.py：截图差分校验
   ↓
core/orchestrator.py：状态流转、推进、暂停、收尾和留痕
```

视觉模型负责把画面转成结构化信息，解题模型只收文字。程序保留流程控制权，不依据模型自由文本随意切换推进方式。视觉不确定、动作失败或推进结果不匹配时，系统会暂停并留痕，不应静默跳过。

网课流程由媒体探针读取 `<video>` 的播放状态、进度和分集信息；弹题作为任务中断处理。题目本身不通过 DOM、XHR 或页面选择器读取。

## 2. 根目录地图

```text
AutoLearn/
├── core/           领域模型、配置、状态机、数据库、方案裁决与编排
├── perception/     截图视觉探针、媒体状态探针、感知流水线
├── solve/          视觉读取、模型 Provider、解题、投票、缓存、技能与训练
├── act/            坐标换算、点击/提交执行、动作结果校验
├── target/         浏览器标签页附加、窗口目标与输入操作
├── adapters/       站点适配定义（当前重点是媒体适配）
├── ui/             FastAPI 服务、API 路由、任务生命周期及静态界面
│   ├── routes/     按资源组织的 API 路由
│   └── static/     无构建前端：HTML、JavaScript、CSS
├── prompts/        给模型使用的提示词源文件
├── skills/         题型技能和页面推进技能正文
├── mock_site/      本地回归靶场（开发测试专用）
├── scripts/        靶场服务、检查、批量任务和验收脚本
├── tests/          单元测试、接口测试及浏览器集成测试
├── docs/           需求、规划、真实站点经验与模型分工文档
├── state/          运行数据、配置、数据库和浏览器 Profile（本地状态）
├── logs/           任务运行留痕（截图、事件和模型处理结果）
├── .workbuddy/     项目协作资料；memory 内含本结构说明与维护笔记
├── pyproject.toml  项目元数据、格式/类型/pytest 配置
├── requirements.txt      运行依赖
├── requirements-dev.txt  开发与测试依赖
└── run.bat         Windows 源码模式启动入口
```

`state/` 和 `logs/` 属于运行期数据，不是源码模块；不要将个人密钥或真实任务留痕随意提交或分享。查看当前忽略规则时以 `.gitignore` 为准。

## 3. 各模块职责

| 路径 | 主要职责 | 建议先读 |
|---|---|---|
| `core/` | 全局共享的数据契约、配置、状态和任务流程；不承载浏览器 I/O 的细节 | `models.py`、`enums.py`、`config.py`、`run_plan.py`、`orchestrator.py` |
| `perception/` | 截取页面视口、产出视觉输入；另有读取视频状态的媒体探针 | `vision_probe.py`、`media_probe.py`、`pipeline.py` |
| `solve/` | 调用视觉/解题模型、解析输出、投票、精确缓存、技能注册及训练总结 | `reader.py`、`solver.py`、`prompt_files.py`、`skill_library.py` |
| `act/` | 屏幕坐标与像素区域工具、点击和提交、基于截图的动作回读 | `screen.py`、`actuator.py`、`verifier.py` |
| `target/` | 找到并连接用户指定的浏览器/窗口目标 | `browsers.py`、`windows.py`、`base.py` |
| `adapters/` | 目标站点的适配信息；阅读前先确认它描述的是媒体信息而非题目解析通道 | `adapters/` 下具体配置 |
| `ui/` | FastAPI 应用装配、API、任务运行生命周期和静态页面 | `server.py`、`assembly.py`、`runner.py`、`routes/` |
| `prompts/` | 视觉组、解题组、共享契约、推进技能库与训练总结的提示词真源 | `00-共享契约.md`、`10-视觉组.md`、`20-解题组.md` |
| `skills/` | 注册技能对应的说明文本，包括题型与推进方式 | `README.md`、各技能 Markdown |
| `mock_site/` | 带有边界情况的本地 HTML 靶场，供回归验证 | `README.md`、`static/traps.md` |
| `scripts/` | 手动启动靶场、运行测试验收、开发诊断和批处理 | `serve_mock.py`、`check_mock.py`、`check_ui.py` |
| `tests/` | 保护模块契约与用户可见行为的自动化测试 | 与准备修改的模块同名/相关的测试 |

### 3.1 `core/` 常用文件

- `models.py`：模块间交换的主要数据模型，例如题目、页面观测、动作结果与运行方案。
- `enums.py`：状态、动作类型及其他共享枚举。
- `config.py`：运行配置和安全阈值。
- `states.py`：题目状态转移规则。
- `run_plan.py`：基于开局观测制定推进和提交方案的纯逻辑。
- `advance_library.py`：推进技能和方案相关元数据。
- `orchestrator.py`：主要任务编排循环；改动前要理解它调用的探测、求解、执行和状态持久化路径。
- `db.py`、`trace.py`：SQLite 持久化与任务留痕/缓存清理。
- `events.py`：事件名定义；增加事件通常还需同步 UI 和契约测试。
- `qid.py`、`vid.py`：题目与课程分集标识。

### 3.2 `ui/` 常用文件

- `server.py`：构造 FastAPI 应用并注册路由。
- `assembly.py`：装配 API、任务编排及各层实现。
- `runner.py`：任务生命周期入口；从 API 启动任务时优先从这里理解流程。
- `routes/`：按任务、模型、目标、训练、系统等资源划分 API。
- `static/index.html`、`app.js`、`style.css`：控制台界面；`landing.html` 是入口介绍页。前端无需单独构建。

## 4. 分层与依赖方向

可以先按“领域规则 → 外部能力适配 → 应用装配 → 用户接口”理解：

```text
core/  ← 各实现模块使用共享契约
  ↑        ↑          ↑          ↑
perception/ solve/     act/       target/ adapters/
                 \
                  ui/assembly.py 负责把实现装配到应用中
```

更具体地说：

- `core/` 定义各层共享的数据与规则；不要让它依赖浏览器操作或 UI 路由。
- 感知、求解、执行和目标管理各自实现职责，通过 `core/` 的模型/协议交换数据。
- `ui/assembly.py` 是应用层的集中装配位置。增加实现时先找现有依赖如何组装，避免跨层随处创建依赖。
- 测试按纯逻辑、模块行为、API 和真实浏览器集成逐级覆盖；能用纯逻辑测试验证的规则，优先避免依赖真实浏览器。

实际 import 约束以源码和 `tests/` 为准。对依赖方向不确定时先搜索现有模块导入与装配方式，不要仅根据目录图新增反向依赖。

## 5. 配置、依赖与启动

- Python 版本要求：`pyproject.toml` 声明 `>=3.13`。
- 运行依赖：`requirements.txt`；开发/测试依赖：`requirements-dev.txt`。
- Windows 推荐从根目录运行 `run.bat`；服务端口默认为 `8800`。
- 默认使用系统 Edge；可通过 `AUTOLEARN_BROWSER_CHANNEL=chrome` 选择 Chrome。
- 本地测试靶场由 `scripts/serve_mock.py` 单独启动；它不随产品控制台自动启动。
- 模型配置在控制台模型库里添加。API Key 使用系统凭据管理器；不要写入源码、文档或模型 YAML。

## 6. 运行数据与留痕

具体路径受运行配置影响，默认数据根目录为项目中的 `state/` 和 `logs/`。典型内容：

- `state/`：SQLite 任务数据库、模型非敏感配置、任务配置草稿、浏览器 Profile。
- `logs/<run_id>/`：某次运行的事件日志和按题目组织的截图、视觉读取、求解、动作和校验留痕。
- 删除任务可能级联删除该次运行的留痕。执行删除前从界面确认目标任务；不要用宽泛通配符清理。

运行留痕可能含题目截图、站点信息或模型回复，应按敏感数据处理。测试请用隔离配置，不要让测试污染个人任务数据。

## 7. 测试和质量检查

在虚拟环境中运行：

```powershell
$env:AUTOLEARN_SECRET_BACKEND = "memory"
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy
```

涉及 UI、真实浏览器目标或靶场时，还要按改动范围运行相应脚本，例如：

```powershell
.venv\Scripts\python.exe scripts/serve_mock.py   # 在单独终端保持运行
.venv\Scripts\python.exe scripts/check_mock.py --all
.venv\Scripts\python.exe scripts/check_ui.py
.venv\Scripts\python.exe scripts/check_target.py
.venv\Scripts\python.exe scripts/check_server.py
```

先确认测试是否真实执行了所需的浏览器路径；靶场未启动可能导致相关浏览器用例跳过。看测试结果也要同时检查跳过数量和诊断报告。

## 8. 常见改动应该从哪里入手

| 改动目标 | 先看 | 同步检查 |
|---|---|---|
| 更改模型提示词或输出格式 | `prompts/`、`solve/prompt_files.py`、`solve/reader.py` / `solve/solver.py` | 解析测试、`tests/test_prompt_files.py`、`tests/test_reader.py`、重启服务验证 |
| 更改运行配置 | `core/config.py` | `ui/schemas.py`、`ui/routes/run.py` 配置回显、前端、配置测试 |
| 更改开局方案/推进 | `core/run_plan.py`、`core/advance_library.py`、`core/orchestrator.py` | `tests/test_run_plan.py`、`tests/test_advance_next.py`、推进技能测试 |
| 更改坐标点击或回读 | `act/screen.py`、`act/actuator.py`、`act/verifier.py` | 坐标、差分、失败暂停相关测试；优先在靶场验证 |
| 更改任务状态/API | `core/states.py`、`core/db.py`、`ui/runner.py`、对应 `ui/routes/` | 状态迁移、DB 迁移、API、恢复/重试测试 |
| 更改前端页面 | `ui/static/`、对应 API 路由 | `scripts/check_ui.py` 及相关 API 测试 |
| 更改模型密钥/凭据 | `core/model_registry.py`、模型 API 路由 | 确认密钥不落入普通配置、日志或测试快照 |
| 更改测试靶场 | `mock_site/`、生成脚本 | 靶场自检、相关浏览器集成测试；同步生成文档（如适用） |

任何涉及“是否提交”“失败是否重试”“坐标何时更新”“暂停是否可恢复”的改动，先阅读 `.workbuddy/memory/MEMORY.md` 的硬约束和 `REFERENCE.md` 对应章节，再读具体实现与测试。历史记录可能已过时，不能替代当前源码。

## 9. 文档地图

- `README.md`：面向新用户/贡献者的快速入口和启动说明。
- `docs/双模型分工-视觉组与解题组.md`：视觉模型和解题模型之间的数据契约及设计背景。
- `docs/真实站点作业做题手册.md`：真实站点观察结论与使用注意事项。
- `docs/AutoLearn-项目任务书.md`、`docs/AutoLearn-实施规划书.md`：需求和规划，其中部分章节属于历史方案，阅读时应与现状交叉核对。
- `prompts/*.md`：模型运行时提示词真源。
- `.workbuddy/memory/MEMORY.md`：维护时需遵守的长期约束和常用命令。
- `.workbuddy/memory/REFERENCE.md`：按模块索引的细节与历史陷阱。
- `.workbuddy/memory/YYYY-MM-DD.md`：按日期记录的工作过程。
