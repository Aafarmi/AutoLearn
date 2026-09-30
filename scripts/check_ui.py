"""前端验收自检（P5 建面 / P13 重构为任务化界面）。

起**真 uvicorn + 真 Chrome**，逐条验证「必须真浏览器才能验」的项。

P13 把信息架构改成 **任务为中心**：主页只有「任务 + 模型库」两块，一道题不再是一条
任务；点开任务卡才进入详情（过程 / 待办 / 条目 / 日志）。因此本自检也整体重写 ——
它验的是**用户能不能把这条新路径走通**，而不只是 DOM id 还在不在：

1. 主页骨架：任务区 + 模型库区都在，空态各自带**下一步出口**（不是一句「暂无数据」）；
2. SSE 消费端连通（``X-Accel-Buffering`` 由后端保证，这里验消费端）；
3. 新建任务向导五步走通：任务名 → 目标 → 两次模型选择 → 运行选项 → 确认；
4. 模型库为空时向导第 3 步给出**「去添加模型」出口**，且下拉里只有模型库里的东西；
5. 目标类型**真的驱动界面**：选「应用程序」后「接管启动浏览器」收起（v0.2.0 起
   题目只经模型读，没有通道模式可收敛，这条是能力矩阵仅存的一块）；
6. 建任务 → 主页出现**一张任务卡**（不是一道题一行）→ 点进详情，
   过程 / 条目 / 日志三段齐全，且**日志在最后**；
7. 详情页能回到主页；删除任务后回到空态；
8. 顶栏「关机」能弹出确认框、清单来自后端、取消能收起
   （**绝不点「确认关闭」** —— 那会真的把自检进程自己关掉，见函数注释）。

用法::

    .venv/Scripts/python scripts/check_ui.py          # 自检后退出
    .venv/Scripts/python scripts/check_ui.py --keep   # 保留服务便于人工查看

退出码：全部通过 0；任一失败 1。
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

import uvicorn
from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 自检脚本**不允许**往用户真实的 OS 凭据库写东西。
# 默认后端会走真实 keyring，而每条配置的 ``api_key_ref`` 都带新 uuid ——
# 每跑一次自检就留一条 ``autolearn/<uuid>``，跑几十次把凭据库写满
# （``CredWrite`` 报 ``WinError 8``），从此**用户自己在软件里也存不进密钥**。
# 需要验证真实 keyring 路径时显式覆盖：``AUTOLEARN_SECRET_BACKEND=keyring``。
os.environ.setdefault("AUTOLEARN_SECRET_BACKEND", "memory")

# 自检必须跑在**隔离的落盘位置**上，四个都要隔离 —— 少隔离一个就会读到用户的
# 真实数据，让「默认值」类断言随用户的配置而偶发失败（实测踩到两次）：
#
#   AUTOLEARN_RUN_CONFIG    手工把界面切到全自动并存盘后，「默认半自动」断言就红
#   AUTOLEARN_MODELS_PATH   用户配了自己的模型后，「模型库空态」断言就红
#   AUTOLEARN_LOG_ROOT      否则自检往仓库 logs/ 里灌运行日志
#   AUTOLEARN_DB_PATH       否则自检往仓库 SQLite 里写任务与留痕
_SANDBOX = Path(tempfile.mkdtemp(prefix="autolearn-checkui-"))
os.environ.setdefault("AUTOLEARN_RUN_CONFIG", str(_SANDBOX / "run_config.json"))
os.environ.setdefault("AUTOLEARN_MODELS_PATH", str(_SANDBOX / "models.yaml"))
os.environ.setdefault("AUTOLEARN_LOG_ROOT", str(_SANDBOX / "logs"))
os.environ.setdefault("AUTOLEARN_DB_PATH", str(_SANDBOX / "autolearn.db"))

from ui import deps, server  # noqa: E402

#: 主页两块地盘 + 详情容器。任务与条目**分成两个视图**是本轮改动的核心。
HOME_ZONES = ("zone-tasks", "zone-models")
DETAIL_SECTIONS = ("section-activity", "section-items", "section-logs")

_results: list[tuple[bool, str]] = []

# 控制台编码可能是 GBK（Windows 中文环境的默认值）。标题里一旦出现 ``⚠`` / ``→``
# 这类符号，``print`` 会直接抛 UnicodeEncodeError —— 而它抛在**打印结果的那一行**，
# 于是已经跑出来的几十条结果一条也看不到。让它容错降级，别让排版符号毁掉一次验收。
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if callable(_reconfigure):
        with suppress(Exception):
            _reconfigure(errors="replace")


def check(ok: bool, label: str) -> None:
    _results.append((ok, label))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}")


def _free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


@contextmanager
def live_ui() -> Iterator[str]:
    """起真 uvicorn（线程内），返回 base_url。"""
    deps.reset_state()
    app = server.create_app()
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(200):
        if srv.started:
            break
        time.sleep(0.02)
    if not srv.started:  # pragma: no cover - 环境异常
        raise RuntimeError("uvicorn 没起来")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.should_exit = True
        thread.join(timeout=5)
        deps.reset_state()


def open_page() -> tuple[Any, Page]:
    pw = sync_playwright().start()
    browser = pw.chromium.launch(channel="chrome", headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    return (pw, browser), page


def _wait_visible(page: Page, selector: str, timeout: int = 8000) -> bool:
    """等元素可见。**不用固定 sleep** —— 异步链在负载高的机器上会偶发超时。"""
    try:
        page.wait_for_selector(selector, state="visible", timeout=timeout)
        return True
    except Exception:
        return False


def check_home_skeleton(page: Page) -> None:
    print("[1] 主页骨架（任务 + 模型库）")
    for zone in HOME_ZONES:
        check(page.locator(f"#{zone}").count() > 0, f"#{zone} 存在")
    check(page.locator("#view-home.is-active").count() == 1, "默认停在主页视图")
    check(page.locator("#view-task.is-active").count() == 0, "详情视图默认不显示")
    check(page.locator("#btn-new-task").is_visible(), "「＋ 新建任务」在顶栏可见")

    check(_wait_visible(page, "#tasks-empty"), "空任务时展示引导空态")
    check(
        page.locator('#tasks-empty [data-act="new-task"]').count() == 1,
        "空态里带「新建第一个任务」出口（不是干巴巴一句暂无数据）",
    )
    check(_wait_visible(page, "#models-empty"), "空模型库时展示引导空态")
    check(
        page.locator('#models-empty [data-act="add-model"]').count() == 1,
        "模型库空态里带「添加模型」出口",
    )
    hint = page.inner_text("#tasks-empty")
    check("任务就是" in hint, "空态解释「任务是什么」")
    check(page.locator("#tasks-count").inner_text() == "0", "任务计数为 0")


def check_sse(page: Page) -> None:
    print("[2] SSE 消费端")
    with suppress(Exception):
        page.wait_for_function(
            "document.querySelector('#conn-label').textContent.includes('已连接')", timeout=8000
        )
    check("已连接" in page.inner_text("#conn-label"), "连接指示灯变绿")


def _goto_step(page: Page, step: int) -> None:
    """点「下一步」直到指定步骤（每步都是同步渲染，点完即可断言）。

    带上次数上限：一旦向导**因为挡住了某一步而不再前进**，这里必须立刻报错，
    否则就是一个静默的死循环（实测踩到过 —— 提示写着「可以继续」，代码却在拦）。
    """
    for _ in range(8):
        current = page.get_attribute("#wizard-steps .step.is-current", "data-step") or "1"
        if int(current) >= step:
            return
        page.click("#btn-wizard-next")
        page.wait_for_timeout(120)
    raise AssertionError(
        f"点不动了：向导停在 "
        f"{page.get_attribute('#wizard-steps .step.is-current', 'data-step')}，"
        f"到不了第 {step} 步"
    )


def check_wizard_skeleton(page: Page) -> None:
    print("[3] 向导第 1 步：任务名")
    page.click("#btn-new-task")
    check(_wait_visible(page, "#wizard"), "「新建任务」打开向导")
    check(
        page.get_attribute("#wizard-steps .step.is-current", "data-step") == "1",
        "向导从第 1 步开始",
    )
    check(page.locator("#wizard-steps .step").count() == 5, "步骤条共 5 步")
    placeholder = page.get_attribute("#wizard-name", "placeholder") or ""
    check("留空" in placeholder, "任务名说明留空会怎么办")
    page.fill("#wizard-name", "自检任务")

    print("[3b] 向导第 2 步：目标")
    _goto_step(page, 2)
    check(page.locator('input[name="wizard_target_kind"]').count() == 2, "两种目标类型都在")
    check(page.locator("#btn-scan-targets").is_visible(), "「抓取网页」按钮可见")
    check(
        page.inner_text("#btn-scan-targets").strip() in {"抓取网页", "抓取窗口"},
        "按钮文案是「抓取网页/抓取窗口」而不是「扫描」",
    )
    check(page.locator("#btn-launch-browser").is_visible(), "「接管启动浏览器」按钮可见")
    pane2 = page.inner_text('.wizard-pane[data-step="2"]')
    check("无法事后" in pane2, "写明「已开的浏览器无法事后开调试端口」（最高频卡点）")
    check("静默忽略" in pane2, "写明专用 Profile 要绕开 Chrome 136+ 静默忽略")
    check("不会导航" in pane2 or "只读你选的那一个" in pane2, "写明附加模式不导航、不关标签页")
    # 抓取是只读接口，可反复点；**点了必须有反馈**（P15）：
    # 没有带调试端口的浏览器时也要明确说「一个都没抓到 + 为什么」。
    page.click("#btn-scan-targets")
    _wait_visible(page, "#target-grab-result")
    grab_text = page.inner_text("#target-grab-result").strip()
    check(grab_text != "", "抓取后结果行有话说（不是静默变化）")
    check(
        "抓到" in grab_text or "没抓到" in grab_text or "失败" in grab_text,
        f"结果行说清了抓到几个 / 为什么没有：{grab_text[:40]}",
    )
    check(
        page.locator("#btn-scan-targets").is_enabled(),
        "抓取结束后按钮恢复可用（不会一直转）",
    )

    # 切到「应用程序」：通道必须自动收敛为仅视觉，接管启动按钮消失
    page.check('input[name="wizard_target_kind"][value="desktop_window"]')
    page.wait_for_timeout(300)
    check(not page.is_visible("#btn-launch-browser"), "「接管启动浏览器」对程序窗口隐藏")
    page.check('input[name="wizard_target_kind"][value="browser_page"]')
    page.wait_for_timeout(300)

    print("[3c] 向导第 3 步：四组模型选择")
    _goto_step(page, 3)
    check(page.locator("#wizard-judge").count() > 0, "解题组下拉存在")
    check(page.locator("#wizard-vision").count() > 0, "视觉组下拉存在")
    check(page.locator("#wizard-judge-backup").count() > 0, "解题备用列表存在")
    check(page.locator("#wizard-vision-backup").count() > 0, "视觉备用列表存在")
    check(
        page.get_attribute("#wizard-judge-backup", "class") is not None
        and "pick-list" in (page.get_attribute("#wizard-judge-backup", "class") or ""),
        "解题备用是**勾选列表**（原生 multi-select 要按住 Ctrl，用户点第二下会取消第一个）",
    )
    check(
        page.get_attribute("#wizard-vision-backup", "class") is not None
        and "pick-list" in (page.get_attribute("#wizard-vision-backup", "class") or ""),
        "视觉备用同样是勾选列表",
    )
    check(
        page.get_attribute("#wizard-judge", "multiple") is None,
        "解题组是单选（一套配置一个模型）",
    )
    check(_wait_visible(page, "#wizard-models-empty"), "模型库为空时给出提示块")
    check(
        page.locator("#btn-wizard-goto-models").is_visible(),
        "提示块里带「去添加模型」出口（不是让用户自己找）",
    )
    # 下拉里只允许出现模型库里的东西 + 一个「不选」占位 —— 不能让用户手打 profile_id
    judge_options = page.locator("#wizard-judge option").count()
    check(judge_options == 1, "模型库为空时下拉只有「不选」一项，没有让用户瞎猜的输入框")
    check(
        page.locator("#wizard-judge-backup .pick").count() == 0,
        "备用组为空时没有任何可勾选项（多选的空态由「一项都不勾」表达，不塞占位项）",
    )

    print("[3d] 向导第 4 步：运行选项")
    _goto_step(page, 4)
    check(page.locator('input[name="wizard_auto_apply"]').count() == 2, "半自动 / 全自动都在")
    check(
        page.is_checked('input[name="wizard_auto_apply"][value="manual"]'),
        "默认半自动（不自动提交）",
    )
    check(
        "必停" in page.inner_text('.wizard-pane[data-step="4"]'),
        "说明「复核题与失败必停」，不让人误以为全自动等于无人值守",
    )
    check(page.locator("#wizard-recalc").count() > 0, "「复算」开关存在")
    check(not page.is_checked("#wizard-recalc"), "默认**不复算**（以第一次答案为准）")
    check(page.is_disabled("#wizard-sample-n"), "不复算时「复算次数」置灰（改它没有意义）")
    check(page.locator("#wizard-training").count() > 0, "「训练模式」开关存在")
    check(not page.is_checked("#wizard-training"), "默认不开启训练模式（它会改磁盘上的提示词）")
    check(page.locator("#wizard-autostart").is_checked(), "默认「创建后立即启动」")

    print("[3e] 向导第 5 步：确认摘要")
    _goto_step(page, 5)
    check(page.locator("#wizard-summary .summary__row").count() >= 8, "摘要列出各项配置")
    summary = page.inner_text("#wizard-summary")
    check(
        "视觉组" in summary and "解题组" in summary,
        "摘要含两组主模型选择（视觉组 / 解题组）",
    )
    check("复算" in summary, "摘要说明要不要复算")
    check("内置靶场" in page.inner_text("#wizard-blocker"), "没选目标时预告会走内置靶场")


def _step_back(page: Page, times: int) -> None:
    for _ in range(times):
        page.click("#btn-wizard-prev")
        page.wait_for_timeout(120)


def check_wizard_constraint(page: Page) -> None:
    """目标类型驱动界面：切到程序窗口后，「接管启动浏览器」必须收起。

    v0.2.0 删掉了向导里的「通道优先级 / 探针模式」整块 —— 题目只经模型读，
    没有通道可选，也就没有「通道模式随目标类型收敛」这回事可断言。留下的是
    **目标类型仍然影响界面**这条：原生窗口没有浏览器可接管，按钮必须藏起来。
    它现在是「能力矩阵驱动界面」仅存的一块，所以更要看住。

    注意断言要发生在**那一步显示着的时候** —— 向导是「一次只显示一个 pane」，
    退到第 5 步再去找第 2 步里的按钮，元素存在但不可见（``is_visible`` 为假）。
    """
    print("[3f] 目标类型驱动界面（原生窗口不接管浏览器）")
    _step_back(page, 3)  # 第 5 步 → 第 2 步
    check(
        page.get_attribute("#wizard-steps .step.is-current", "data-step") == "2",
        "「上一步」能一路回退到第 2 步",
    )

    page.check('input[name="wizard_target_kind"][value="desktop_window"]')
    page.wait_for_timeout(150)
    check(
        not page.is_visible("#btn-launch-browser"),
        "选「应用程序」后「接管启动浏览器」收起（界面按目标类型收敛）",
    )
    page.check('input[name="wizard_target_kind"][value="browser_page"]')
    page.wait_for_timeout(150)
    check(
        page.is_visible("#btn-launch-browser"),
        "切回网页后按钮恢复（收敛是双向的，不是一次性隐藏）",
    )

    _goto_step(page, 5)


def check_model_library(page: Page) -> None:
    print("[4] 模型库：从向导跳到添加，存盘后回到向导可选")
    # 「去添加模型」出口在**第 3 步**的提示块里，所以得先退回去 ——
    # 上一步结束时向导停在第 5 步，那时第 3 步的 pane 是 display:none，
    # 元素在 DOM 里但不可点（Playwright 会一直等它可见直到超时）。
    _step_back(page, 2)  # 第 5 步 → 第 3 步
    page.click("#btn-wizard-goto-models")
    check(_wait_visible(page, "#model-dialog"), "「去添加模型」直接打开模型表单")
    check(not page.is_visible("#wizard"), "打开表单时向导已收起（不叠两层浮层）")

    page.fill("#model-name", "自检 · DeepSeek")
    page.fill("#model-base-url", "https://api.deepseek.com/v1")
    page.fill("#model-model", "deepseek-chat")
    page.fill("#model-api-key", "sk-check-ui-secret")
    page.click("#btn-model-save")
    check(_wait_visible(page, ".model-card", timeout=5000), "保存后模型卡出现")
    check(page.locator(".model-card").count() == 1, "模型库里恰好一条")
    check(page.input_value("#model-api-key") == "", "密钥输入框已清空（不回显）")
    check("sk-check-ui-secret" not in page.content(), "页面无明文密钥")
    check("密钥已存" in page.inner_text(".model-card"), "卡片仅显示 has_api_key 投影")
    check(
        page.locator('.model-card button[data-act="up"]').count() > 0
        and page.locator('.model-card button[data-act="down"]').count() > 0,
        "排序（降级链）按钮存在",
    )

    # 再开一次向导：第 3 步应能真正选到这一套
    page.click("#btn-new-task")
    page.fill("#wizard-name", "自检任务")
    _goto_step(page, 3)
    check(not page.is_visible("#wizard-models-empty"), "已有模型后不再显示空态提示")
    check(page.locator("#wizard-judge option").count() == 2, "解题组下拉 = 占位 + 模型库里的 1 套")
    page.select_option("#wizard-judge", index=1)
    check(page.locator("#wizard-vision option").count() == 2, "视觉组下拉同样来自模型库")
    page.select_option("#wizard-vision", index=1)
    check(
        page.locator("#wizard-judge-backup .pick").count() == 1,
        "备用组列出模型库里的那一套（勾选列表，不带占位项）",
    )
    page.check("#wizard-judge-backup .pick input")
    check(
        page.locator("#wizard-judge-backup .pick.is-on").count() == 1,
        "勾选后该项被标成已选（勾选操作真的生效）",
    )
    _goto_step(page, 5)
    check(
        "自检 · DeepSeek" in page.inner_text("#wizard-summary"),
        "摘要显示的是模型库里那套的名字",
    )
    summary = page.inner_text("#wizard-summary")
    check("视觉组" in summary and "解题组" in summary, "摘要按「视觉组 / 解题组」两组呈现")
    check("视觉备用" in summary and "解题备用" in summary, "摘要里有两个备用组")
    check(
        "自检 · DeepSeek" in summary.split("解题备用")[-1],
        "勾进解题备用的那套出现在「解题备用」一行里",
    )
    check("复算" in summary, "摘要里是「复算」而不是含糊的「核对次数」")


def check_task_flow(page: Page) -> None:
    print("[5] 建任务 → 主页任务卡 → 任务详情")
    # 取消「创建后立即启动」：本自检不起靶场，只验界面链路。
    # 点 label 而不是 input —— 这个开关的 input 被 ``.checkbox--switch`` 隐藏了，
    # 对隐藏元素调 ``uncheck(force=True)`` 拿不到可点的位置。
    page.click("#wizard-autostart-label")
    page.wait_for_timeout(120)
    check(not page.is_checked("#wizard-autostart"), "开关可取消（只建任务不启动）")
    check(page.inner_text("#btn-wizard-next").strip() == "只创建任务", "取消启动后按钮文案跟着变")
    page.click("#btn-wizard-next")
    check(_wait_visible(page, "#view-task.is-active"), "建完直接进入任务详情")
    check(page.locator("#wizard").count() > 0 and not page.is_visible("#wizard"), "向导已收起")

    print("[5b] 任务详情三段")
    for section in DETAIL_SECTIONS:
        check(page.locator(f"#{section}").count() > 0, f"#{section} 存在")
    # 日志必须是**最后**一段：过程与交互在前，排障用的流水在后
    order = page.eval_on_selector_all(
        "#detail-body .section",
        "els => els.map(e => e.id)",
    )
    check(
        order and order[-1] == "section-logs",
        f"日志在详情页最后（实际顺序 {order}）",
    )
    check("自检任务" in page.inner_text("#detail-body"), "详情标题是刚建的任务名")
    pills = (("#rs-media", "媒体态"), ("#rs-interrupt", "弹题"), ("#rs-stack", "任务栈"))
    for pill, label in pills:
        check(page.locator(pill).count() > 0, f"{label}指示灯存在")
    check(_wait_visible(page, "#logs .log-line"), "日志流有输出")

    print("[5c] 回到主页：一条任务 = 一张卡")
    page.click("#btn-home")
    check(_wait_visible(page, "#view-home.is-active"), "「← 返回任务」回到主页")
    check(page.locator(".task-card").count() == 1, "主页出现 1 张任务卡（不是一道题一行）")
    card = page.inner_text(".task-card")
    check("自检任务" in card, "卡片显示任务名")
    check("待启动" in card or "已完成" in card or "进行中" in card, "卡片显示人话状态")
    check("判题：" in card, "卡片显示用了哪个判题模型")
    check(page.inner_text("#tasks-count") == "1", "任务计数更新")
    check(not page.is_visible("#tasks-empty"), "有任务后空态收起")


def check_task_bulk_ops(page: Page) -> None:
    """任务卡上的多选 / 批量删除 / 「重试」入口（2026-09-28 加）。

    **只验界面**：勾选、全选、按钮的显隐与文案，以及卡片上那两个操作按钮存在。
    真删由下面那条 ``check_cleanup`` 负责 —— 这里要是也删一遍，
    后面那条就没有任务可删了。
    """
    print("[5d] 任务卡：多选 / 批量删除 / 重试入口")
    check(page.locator("[data-pick]").count() == 1, "每张任务卡左侧有多选框")
    check(page.locator('[data-act="delete-task"]').count() == 1, "卡片上有「删除」按钮")
    check(
        page.locator('[data-act="retry-task"]').count() == 1,
        "没跑完的任务卡上有「重试」按钮",
    )
    check(not page.is_visible("#btn-bulk-delete"), "没勾选时不显示「删除选中」")

    page.check("[data-pick]")
    check(_wait_visible(page, "#btn-bulk-delete"), "勾选后出现「删除选中」")
    check("1" in page.inner_text("#btn-bulk-delete"), "按钮文案带上选中数量")
    check(page.is_checked("#pick-all"), "勾满时「全选」自动打勾")

    page.uncheck("#pick-all")
    check(not page.is_visible("#btn-bulk-delete"), "取消全选后「删除选中」收起")


def check_cleanup(page: Page) -> None:
    print("[6] 删除任务回到空态")
    page.on("dialog", lambda d: d.accept())
    page.click(".task-card")
    _wait_visible(page, "#btn-delete-task")
    page.click("#btn-delete-task")
    check(_wait_visible(page, "#tasks-empty"), "删除后任务区回到空态")
    check(page.locator(".task-card").count() == 0, "任务卡已消失")


def check_calibration_visible(page: Page) -> None:
    """开局判定 / 收尾确认**在界面上看得见**（2026-09-30 口径）。

    这两条事件是用户理解「它凭什么说做完了」的唯一窗口：
    判定事件说清「一共几题、按哪种方式推进、按什么范围提交」，
    收尾确认说清「视觉组观测到做完了没有」。
    纯逻辑由 `tests/test_advance_calibration.py` 覆盖，这里只验**界面认得这两个事件**
    —— 事件不认识的表现是「任务跑完了，但过程里一句话都没有」，很难被发现。
    """
    print("[7] 开局判定 / 收尾确认的事件文案")
    labels = page.evaluate(
        "() => ({"
        " calibrated: !!ACTIVITY_TEXT['advance.calibrated'],"
        " confirm: !!ACTIVITY_TEXT['advance.completion_check'],"
        " oldLastq: !!ACTIVITY_TEXT['advance.last_question'],"
        " cards: typeof METHOD_LABELS !== 'undefined' && !!METHOD_LABELS.card,"
        " click: typeof METHOD_LABELS !== 'undefined' && !!METHOD_LABELS.click"
        "})"
    )
    check(labels["calibrated"], "任务过程认识 advance.calibrated（开局判定）")
    check(labels["confirm"], "任务过程认识 advance.completion_check（收尾确认）")
    check(not labels["oldLastq"], "旧事件 advance.last_question 已随 P14 机制一并移除")
    check(
        labels["click"] and labels["cards"],
        "推进方式有中文说法（点击按钮 / 点答题卡题号 / 滑动翻页 / 向下滚动）",
    )


def check_shutdown_entry(page: Page) -> None:
    """顶栏「关机」：确认框能弹、清单来自后端、取消能收。

    **绝不点「确认关闭」** —— 那会让 ``ui.system`` 真的调 ``os._exit``，
    把这个自检进程连同它自己起的 uvicorn 一起关掉（自检是**同进程内**起服务的，
    点了就再也没有后续断言可跑）。

    所以这里的分工是刻意的：**「点下去会发生什么」由
    ``tests/test_system_shutdown.py`` 覆盖**（那边把退出函数换成替身，
    还能验「只杀兄弟进程、绝不碰外人」）；本函数只保证**入口在、文案对、清单真**。
    """
    print("[8] 关机入口（只验弹框与取消，不点确认）")
    check(page.locator("#btn-shutdown").count() == 1, "#btn-shutdown 存在")
    label = page.locator("#btn-shutdown").inner_text().strip()
    check("关机" in label, f"按钮文案说明它会关机（实际 {label!r}）")

    page.click("#btn-shutdown")
    opened = _wait_visible(page, "#shutdown-dialog")
    check(opened, "点「关机」弹出确认框")
    if not opened:
        return

    try:
        page.wait_for_selector("#shutdown-preview li", timeout=8000)
        items = page.locator("#shutdown-preview li").all_inner_texts()
    except Exception:
        items = []
    check(len(items) >= 1, f"「会关闭什么」清单有内容（{len(items)} 条）")
    # 端口号来自后端 /api/system/status —— 前端写死的话这一条就失去意义了。
    check("8800" in " ".join(items), "清单点名控制台端口（说明清单来自后端而非前端硬编码）")

    confirm_text = page.locator("#btn-shutdown-confirm").inner_text().strip()
    check(confirm_text == "确认关闭", f"确认按钮是两段式文案（实际 {confirm_text!r}）")

    page.click("#btn-shutdown-cancel")
    try:
        page.wait_for_selector("#shutdown-dialog", state="hidden", timeout=5000)
        hidden = True
    except Exception:
        hidden = False
    check(hidden, "「取消」能收起确认框")


def run_checks(base_url: str, keep: bool) -> None:
    handles, page = open_page()
    try:
        # 本页面挂着 SSE 长连接，networkidle 永远不会到 —— 全局禁用。
        # 用「主页骨架出现」作为就绪信号：它证明首屏数据链路已经跑通。
        page.goto(base_url, wait_until="load")
        if not _wait_visible(page, "#zone-tasks"):
            raise RuntimeError("首屏没渲染出任务区，后续检查无意义")

        check_home_skeleton(page)
        check_sse(page)
        check_wizard_skeleton(page)
        check_wizard_constraint(page)
        check_model_library(page)
        check_task_flow(page)
        check_task_bulk_ops(page)
        check_cleanup(page)
        check_calibration_visible(page)
        check_shutdown_entry(page)

        if keep:
            print(f"\n服务保留在 {base_url}，Ctrl+C 退出。")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
    finally:
        handles[1].close()
        handles[0].stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="AutoLearn 前端验收自检（任务化界面）")
    parser.add_argument("--keep", action="store_true", help="自检后保留服务供人工查看")
    args = parser.parse_args()

    print("AutoLearn 前端自检（真 Chrome · 任务化界面）\n")
    with live_ui() as base_url:
        run_checks(base_url, args.keep)

    failed = [label for ok, label in _results if not ok]
    print(f"\n结果：{len(_results) - len(failed)}/{len(_results)} 通过")
    if failed:
        print("未通过项：")
        for label in failed:
            print(f"  - {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
