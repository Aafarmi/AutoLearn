# AutoLearn

AutoLearn 是一个运行在本机的课程作业工作区，用任务队列串联题目抓取、模型解题、答案复核和网页填写。当前版本提供 localhost WebUI：浏览器显示页面，Python 服务负责本地数据、任务队列和站点操作。

> 当前版本：**v1.0.1** · [更新记录](CHANGELOG.md)

## 功能

- **固定解题核心**：题目抓取后的解题、答案复核、任务队列和 WebUI 流程由统一模块负责，站点差异不会进入解题核心。
- **可扩展适配器架构**：不同网站的认证、抓取和网页交互封装在独立适配器中；当前已内置两个适配器，可以继续新增适配器，对更多网页进行适配。
- 抓取题目、选项、答案空位和图片附件，并按适配器 Schema 校验题包。
- 保存模型档案；发送题目和图片到模型服务前由用户在任务页授权。
- 在本机任务页复核和修改答案。
- 交互前检查题目、选择器和控件映射，填写后核对回显。
- 仅在本机任务页通过一次性确认发起正常提交；提交结果按网站页面证据记录。

## 适配器扩展

核心解题流程保持固定，站点相关代码隔离在 `core/adapters/<adapter_id>/`。每个适配器提供认证、抓取、交互和对应 Schema；要支持新的网页，只需要新增一个适配器并接入现有任务流程，不需要修改解题模块和 WebUI 主流程。

详细模块和数据流见[架构说明](docs/architecture.md)，适配器契约见各适配器目录中的 README。

## 开源项目声明

AutoLearn 使用了开源项目 **Scrapling** 完成网页抓取、浏览器渲染和 Cookie/网页交互。

- 项目：Scrapling
- 作者：Karim Shoair 及其贡献者
- 源码：<https://github.com/D4Vinci/Scrapling>
- 许可证：BSD 3-Clause License
- AutoLearn 中锁定的版本：`scrapling[fetchers]==0.4.15`

本仓库不包含 Scrapling 源代码，Scrapling 由 `pip` 按 [`requirements.txt`](requirements.txt) 安装。Scrapling 的版权和许可证归其作者所有，使用和再分发时请遵守其 BSD 3-Clause License。感谢 Scrapling 项目及其贡献者。

项目还使用 FastAPI（MIT License）和 Uvicorn（BSD 3-Clause License）作为本机 Web 服务依赖，具体版本见 [`requirements.txt`](requirements.txt) 和 [`pyproject.toml`](pyproject.toml)。

## 环境要求

- Windows 用户会话和 Python 3.12 x64。
- 使用 Cookie Manager 时需要本机安装 Google Chrome Stable x64。
- 安装 Python 依赖和 Scrapling 浏览器组件时需要网络连接。
- 项目直接依赖锁定在 `requirements.txt` 和 `pyproject.toml`：FastAPI、Uvicorn、Scrapling。

## 一键安装并运行（推荐）

如果是从 GitHub 浏览到本项目，先在 PowerShell 中克隆并进入项目目录：

```powershell
git clone https://github.com/Aafarmi/AutoLearn
cd AutoLearn
```

然后在项目根目录执行：

```powershell
.\setup.bat
```

如果本机没有安装 Git，也可以在 GitHub 页面点击 **Code** → **Download ZIP**，解压后进入项目目录，再执行上面的 `.\setup.bat`。

`setup.bat` 会自动：

- 使用 Python 3.12 创建 `.venv`
- 安装或升级 `pip`
- 安装 `requirements.txt` 中的依赖
- 执行 `scrapling install` 安装浏览器组件
- 启动 AutoLearn WebUI

以后启动只需要执行：

```powershell
.\run.bat
```

如果只想安装、不立即启动：

```powershell
.\setup.bat --install-only
```

首次执行需要网络，`scrapling install` 需要数百 MiB 级磁盘空间。

## 手动安装教程

以下命令均在项目根目录的 PowerShell 中执行。

### 1. 获取项目

如果本机还没有项目源码，先克隆或下载项目，然后进入项目目录：

```powershell
git clone <你的仓库地址>
cd <项目目录>
```

如果已经拿到项目目录，直接在该目录打开 PowerShell。

### 2. 创建虚拟环境

```powershell
py -3.12 -m venv .venv
```

如果 `.venv` 已经存在，可以跳过这一步。不要使用其他 Python 版本替代 3.12 x64。

### 3. 安装 Python 依赖

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

也可以使用项目配置文件安装：

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

### 4. 安装 Scrapling 浏览器组件

需要浏览器抓取或网页交互时，必须使用同一个虚拟环境安装浏览器组件：

```powershell
.\.venv\Scripts\scrapling.exe install
```

首次安装需要网络和数百 MiB 级磁盘空间。离线、网络受限或被浏览器策略阻止时，安装会失败；AutoLearn 在服务启动时不会静默下载浏览器组件。

Cookie Manager 的 `real_chrome=True` 路径还要求本机安装 Google Chrome Stable x64。每次抓取 Cookie 都会新建一个临时 Chrome 配置目录，浏览器以未登录状态启动，抓取结束后该目录被删除：登录状态不会在本机留存，也不操作用户默认 profile；Chrome 不随项目分发。

### 5. 检查安装

确认 Python 依赖已经安装：

```powershell
.\.venv\Scripts\python.exe -c "import fastapi, uvicorn, scrapling; print('AutoLearn 依赖安装完成')"
```

## 启动 WebUI

```powershell
.\.venv\Scripts\python.exe -m core.webui --open-browser
```

也可以直接双击 `run.bat` 启动。`rerun.bat` 会先结束占用默认端口的 AutoLearn 服务，再重新启动；脚本只会在确认监听进程属于 `core.webui` 后结束它。

默认地址为 **http://127.0.0.1:8877**。也可以指定其他本机端口：

```powershell
.\.venv\Scripts\python.exe -m core.webui --port 9000
```

端口范围为 `1024–65535`。服务只绑定 `127.0.0.1`；不要改为 `0.0.0.0`，也不要通过代理或端口转发开放给局域网或公网。可点击页面右上角“关闭服务”结束进程并释放端口，也可以在启动终端按 `Ctrl+C`。

## 使用教程

按下面顺序完成第一次任务。页面底部的“上一步”和“下一步”会按当前阶段开放，只有满足条件后才能继续。

### 1. 配置模型档案

打开页面右上角的“模型与设置”，新建或编辑模型档案，填写：

- 名称：用于在任务中选择的档案名称。
- 完整请求 URL：需要包含 API 路径，例如 `https://.../v1/chat/completions`。
- 模型：服务商要求的模型名称。
- API 密钥：由本机后端保存和读取，不会返回给浏览器。

保存后可以设为默认档案。模型服务地址、密钥和调用目标需要由你确认后再使用。

### 2. 新建任务

点击“新建任务”，选择站点并填写作业页面地址：

- 站点：选择已安装的适配器。
- 作业网址：填写对应站点的 HTTPS 作业页面地址。
- 模型档案：选择已保存的模型档案，也可以稍后在授权页面修改。

创建后会生成任务草稿并进入“登录验证”阶段。

### 3. 完成登录验证

在“登录验证”页面：

1. 点击“抓取 Cookie”。
2. 根据提示在弹出的浏览器中完成登录，并确认页面已经进入目标作业。该浏览器每次都是全新启动，不带任何历史登录状态，所以每次抓取都需要重新登录。
3. 返回任务页面，确认 Cookie 状态已经更新。

需要更换账号或登录状态时，点击“更新登录验证”。抓取完成后才能进入“抓取题目”。

### 4. 抓取并检查题目

进入“抓取题目”页面后点击“抓取题目”，系统会生成题包。随后在“检查题包”页面核对题目、选项、答案空位和图片附件；有需要时按页面提示返回上一步处理登录或任务设置。

### 5. 授权模型请求

题包检查完成后进入“模型授权”页面。页面会显示目标模型服务、题目数量和图片数量；确认这些内容无误后授权。授权后题面文字和图片才会发送到所配置的模型服务。

### 6. 解题与复核答案

授权后自动进入“AI 解题”，等待阶段进度完成。解题结束后进入“答案复核”页面，逐题检查和修改答案。答案确认无误后再点击“填写答案”。

### 7. 填写与确认提交

“填写答案”会在对应的网站页面中完成填写和回显验证。验证完成后进入“提交确认”页面，只有在这个本机页面通过一次性确认，系统才会发起一次正常提交；取消、过期、错配或服务中断都不会触发确认提交。

提交结果按网站页面证据记录。若网站可能已自动保存、填写结果不明或提交状态不确定，先检查网页状态，再决定是否重试。

### 安全提醒

Cookie 和 API 密钥由本机后端读取，不会返回给页面。题面文字和图片会发送到所配置的模型服务；提交确认只授权一次网页提交尝试。

## 浏览器组件

- Cookie Manager 需要本机安装 Google Chrome Stable x64。
- Cookie 抓取每次都拉起全新的临时浏览器，不保留上次的登录状态；需要重新登录属于预期行为。
- 需要 Scrapling 浏览器型抓取或交互时，主动执行 `scrapling install`。
- 浏览器组件必须安装在与项目相同的 Python 环境中，否则运行时会提示缺少 Scrapling 浏览器依赖。
- 首次安装需要网络和数百 MiB 级磁盘空间；失败时请先解决网络或浏览器策略问题，再重新执行安装命令。

## 独立命令

通常建议从 WebUI 执行任务。维护或调试时可从项目根目录使用：

| 操作 | 命令 |
| --- | --- |
| 站点爬取 | `python -m core.adapters.<adapter_id>.crawler` |
| Cookie 管理 | `python -m core.auth.cookie_manager <adapter_id>` |
| 解题 | `python -m core.solver.runner RUN_ID` |
| 仅填写、不提交 | `python -m core.interaction.runner RUN_ID --fill-only` |

将 `RUN_ID` 替换为本机题包的运行 ID，将 `<adapter_id>` 替换为 `core/adapters/` 下的适配器目录名。爬虫从本机用户配置读取站点 URL 和 Cookie。独立运行解题器会向模型档案中的服务发送题目及图片；确认目标题包、服务地址和授权后再运行。独立交互命令只提供 `--fill-only` 调试路径；正常提交必须由 WebUI 任务页发起。已有 `answers.json` 或 `execution_result.json` 时，工具默认拒绝覆盖；仅在明确要重新运行时才使用相应的 `--overwrite`。

运行命令时使用与项目依赖相同的 Python 环境；Windows 上可将 `python` 替换为 `.venv\Scripts\python.exe`。爬虫和交互器从项目根目录以 `python -m ...` 方式启动。

## 本机数据

运行数据位于 `%LOCALAPPDATA%\AutoLearn`：

| 路径 | 内容 |
| --- | --- |
| `config.json` | 模型档案、站点 URL 和 Cookie |
| `tasks.sqlite3` | 任务、队列和事件 |
| `runs/<run_id>/` | 题目、交互映射、答案、结果和本地图片 |
| `logs/` | 安全运行日志 |
| `ChromeProfiles/` | 旧版本遗留的浏览器 profile；当前版本不再写入，可安全删除 |
| `backups/` | 数据迁移和数据库备份 |

Cookie 抓取使用的临时 Chrome 配置位于系统临时目录（`%TEMP%\autolearn-<site_id>-*`），抓取结束后自动删除。

首次由 WebUI 或独立命令启动时会检查并迁移旧源码目录中的 `config.json`、`runs/` 和 `logs/`。迁移前会校验并备份数据，原来源保留；目标冲突或验证失败时不会覆盖已有数据。Windows SQLite 数据库中的完整任务 URL 使用当前用户 DPAPI 保护，不能跨用户或跨设备恢复。

## 项目结构

```text
CHANGELOG.md 版本更新记录
setup.bat   一键安装依赖并启动
run.bat     双击启动 WebUI
rerun.bat   结束默认端口上的 AutoLearn 服务后重新启动
core/       业务代码、站点适配器和本机 WebUI
  adapters/ 站点适配器及其 Schema（按目录隔离）
  auth/     Cookie 管理
  common/   通用 Schema 校验
  interaction/ 网页交互入口
  settings/ 模型档案
  solver/   解题和答案复核
  tasks/    SQLite 任务域与串行运行时
  webui/    FastAPI 服务和静态页面
docs/       架构说明和历史方案归档
```

## 适配器文档

当前内置两个适配器，站点差异封装在各自目录中。新增适配器时可以沿用相同结构接入现有任务流程。

- [适配器 1 文档](core/adapters/chaoxing/README.md)
- [适配器 2 文档](core/adapters/cjyuketang/README.md)