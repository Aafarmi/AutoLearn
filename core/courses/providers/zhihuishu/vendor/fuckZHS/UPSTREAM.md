# UPSTREAM — fuckZHS 只读 vendor 快照

本目录是上游第三方项目的**只读快照**，供智慧树课程 provider 在运行时复制到工作目录后子进程加载。
本目录内除本文件外，任何文件都不得修改、格式化或"顺手修一下"；升级只允许整体替换并重算 MD5。

## 1. 上游来源

| 项 | 值 |
| --- | --- |
| 仓库 | https://github.com/VermiIIi0n/fuckZHS.git |
| 分支 | `master`（取自上游 `meta.json` 的 `branch`） |
| 版本 | `2.6.3`（取自上游 `meta.json` 的 `version`） |
| 提交 | `04cbbb8bebcf80f13e7589286bb95be2b6ed3a9d`（2025-10-29 01:14:05 +0800） |
| 作者 | VermiIIi0n |
| 许可 | 见同目录 `LICENSE` |
| 快照复制日期 | 2026-10-23 |
| 快照来源路径 | `D:\Ddesktop\AIandCODE\code\fuckZHS` |

## 2. 文件清单与 MD5（9 条）

MD5 为小写十六进制，按复制后实际落盘文件计算，并与来源文件逐条比对一致。

| 文件 | 字节 | MD5 |
| --- | ---: | --- |
| `fucker.py` | 106674 | `90121d3acf3be80f59cc93f02b256567` |
| `ObjDict.py` | 5102 | `e1a84b7d0f1f49ed2f87ec12883eb066` |
| `zd_utils.py` | 3337 | `673ed550b1f7cf1bf0dd273487edcb88` |
| `sign.py` | 315 | `f99ed81345a16c0f98e29a86df3ec10c` |
| `utils.py` | 5922 | `be834c232f423bdd1de3f373138c7a5a` |
| `logger.py` | 5258 | `93f01cfc47fd5d74c564d0c0e753740c` |
| `push.py` | 568 | `148bfe91e1e8d7d9b116970f877663eb` |
| `LICENSE` | 1081 | `c5a3b1c8f17c3ecf25f721532bac0704` |
| `meta.json` | 82 | `5cb975c392e6fa9b7d332406d6690218` |

台账条数：**9**。

复核命令（在本目录执行）：

```powershell
Get-ChildItem -File | Where-Object Name -ne 'UPSTREAM.md' |
  ForEach-Object { '{0,-12} {1}' -f $_.Name, (Get-FileHash $_.FullName -Algorithm MD5).Hash.ToLower() }
```

## 3. 本地改动

**本地改动：无。**

- 本快照与上游对应提交逐字节一致，未做任何补丁、裁剪或格式化（含行尾、编码、空白）。
- 后续任何人不得修改 vendor 内文件。上游缺陷（例如 §4.6 第 5、6 条提到的 `os.get_terminal_size()` 无 try、
  `logger.py` 导入期建 `./logs`）必须在 **bridge / 运行目录准备阶段**处理，不得回改 vendor 源码。
- 确需修复时：升级上游或另建 patch 层，并同步更新本文件的版本、提交与 MD5 台账。

## 4. 排除清单与理由

| 未复制条目 | 理由 |
| --- | --- |
| `main.py` | 上游 CLI 入口（交互式参数解析与主循环）。运行由 bridge 接管，复制会形成第二个入口并与子进程协议冲突。 |
| `config.json` | 本机用户配置（账号、模型、运行参数），属于用户数据而非代码，且可能含敏感值。 |
| `cookies.json` | 登录凭据。凭据只允许经 Cookie Manager 与凭据白名单写入运行目录，不得进仓库。 |
| `execution.json` | 上次运行留下的运行态产物，不是运行依赖。 |
| `images/` | 仓库 README 配图与运行时截图产物，非运行依赖。 |
| `decrypt/` | `decrypt_api.py` / `decrypt_hike.py` 及 `hike.zip` / `zhidao.zip` 等协议逆向与解密辅助物，`fucker.py` 运行不引用。 |
| `logs/` | 运行时日志，`logger.py` 会在**当前工作目录**自建；复制进来只会污染仓库。 |
| `__pycache__/` | 字节码缓存，解释器可按需重建，且绑定本机 Python 版本。 |
| `.github/` | 上游 CI 工作流，与本仓库无关。 |
| `.git/`、`.gitignore`、`.workbuddy/` | 版本库元数据与本地工具目录，不属于快照内容。 |
| `README.md` | 上游使用说明，与本仓库文档体系重复；必要信息已摘录到本文件。 |
| `requirements.txt` | 上游依赖声明，依赖最终由本项目 `requirements.txt` 统一固定（T0-04），避免双份事实源。 |

## 5. 导入验证

`logger.py` 在导入期会按 `utils.py` 所在目录创建 `./logs`，**禁止在本目录直接 import**（会污染仓库）。
验证须先把快照复制到系统临时目录再导入：

```powershell
$py     = 'D:\Ddesktop\AIandCODE\code\AutoLearn\.venv\Scripts\python.exe'
$vendor = 'D:\Ddesktop\AIandCODE\code\AutoLearn\core\courses\providers\zhihuishu\vendor\fuckZHS'
$tmp = Join-Path $env:TEMP ("zhs-probe-" + [guid]::NewGuid())
New-Item -ItemType Directory $tmp | Out-Null
Copy-Item (Join-Path $vendor '*') $tmp -Recurse
Push-Location $tmp
& $py -c "import fucker; print(fucker.Fucker, fucker.progressBar)"
Pop-Location
```

2026-10-23 验证结果：

```text
<class 'fucker.Fucker'> <function progressBar at 0x...>
EXIT=0
```

临时目录内生成了 `logs/`（预期行为），本目录保持 9 个文件、无 `logs/`。

运行时依赖（`requests`、`pycryptodome`、`openai`、`tiktoken`、`Pillow`）由项目虚拟环境提供，锁定版本见 §6。

## 6. 运行依赖

上游 `requirements.txt` 声明的五个包，与 AutoLearn 实际锁定的版本（2026-10-23 在 `.venv` 内实测可导入，T0-04）：

| 上游声明 | 本项目锁定 | 用途 | 使用点 |
| --- | --- | --- | --- |
| `Pillow>=8.1.1` | `Pillow==13.0.0` | 二维码/图片读取、缩放与灰度 | `utils.py:7,25-32,50-52`（`Image.open` / `Image.Resampling.NEAREST` / `ImageOps.grayscale`） |
| `pycryptodome` | `pycryptodome==4.1.0` | AES 解密上游协议常量（`HOME_KEY`/`VIDEO_KEY`/`QA_KEY`/`AI_KEY`/`EXAM_KEY`） | `zd_utils.py:1`（`Crypto.Cipher.AES`） |
| `tiktoken` | `tiktoken==0.14.0` | 用 `encoding_for_model("gpt-4")` 估算提示词 token 数 | `fucker.py:34,2000` |
| `OpenAI` | `openai==3.37.0` | `OpenAI(...)` 客户端请求兼容 endpoint 生成答案 | `fucker.py:29,2229` |
| `requests` | `requests==2.34.2` | HTTP 会话、`HTTPAdapter/Retry`、`RequestsCookieJar` 与 cookie 互转 | `fucker.py:4-6,100`、`push.py:11`、`utils.py:6` |

口径说明：

- 版本来源：本机 `.venv`（Python 3.12.14）`pip install` 后逐个 import 验证通过的确切版本，已**双写**进 `requirements.txt` 与 `pyproject.toml`（两处集合与顺序一致，属项目约定）。
- 只追加、不升级既有锁定：`fastapi==0.143.3` / `uvicorn==0.54.0` / `scrapling[fetchers]==0.4.15` 保持原样。
- `requests` 不在这张任务卡点名的四个包内，但上游 `requirements.txt` 有声明且 `fucker.py`、`push.py`、`utils.py` 直接 `import requests`；原项目依赖里没有它，不锁定则新装环境 import 必失败，故一并锁死。
- `tiktoken` 的 `encoding_for_model` 首次调用会联网下载 BPE 词表并缓存到用户目录；纯离线环境需先预热，否则该路径会抛异常。
- 这些包只在**子进程**里 import，但与主进程共用同一个 `.venv`；`setup.bat` 第 58 行 `pip install -r requirements.txt` 已覆盖，无需改动。

