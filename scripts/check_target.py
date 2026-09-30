"""P11 目标采集层真机自检（模式对齐 ``check_mock.py`` / ``check_ui.py``）。

覆盖两类目标：

**浏览器网页** —— 起一个带调试端口的浏览器（临时 Profile，绝不碰用户数据），
打开靶场题目页，然后**只通过 CDP 附加**进去，截一张视口图，并核验它真的是一张
可用的画面：PNG 非空、尺寸与视口一致、画面上有明暗内容（不是纯色空图）。

**桌面应用程序** —— 枚举当前可见的程序窗口，附加，截图，并验证
「图像坐标 → 屏幕坐标」的换算自洽。**自检脚本一律不点击** ——
点击会真的动用户的桌面，那不该由一个自检脚本来做。

v0.2.0 起题目只经模型的眼睛读，所以附加之后**没有「读题结果」可断言**：
「画面可用」就是「接上了」的最小充分证据，而「图里到底有没有题」要模型回答
（那是 ``check_read.py`` 的活）。这里刻意不去解析页面结构 ——
自检脚本要是自己走了已删除的那条路，就等于在暗中把契约改回去。

为什么必须真机跑
----------------
``tests/test_target_layer.py`` 是纯内存用例 —— 它们钉死了接口契约与
``_page_session`` 的优先顺序，但**证明不了「真的能附加上去」**：
CDP 连接、target id 匹配、PrintWindow 截图、DPI 换算，
这些只有真环境能验。而且这台机器上 ``pytest`` 全绿**不等于**这些用例跑过
（靶场不起时真浏览器用例会静默 skip）—— 所以单独一个自检脚本，退出码说话。

为什么用临时 Profile 而不是用户的 Profile
------------------------------------------
生产路径（``POST /api/targets/launch``）用软件**自管的专用 Profile**。
自检连自管目录也不该动 —— 万一写坏里面的登录态，代价远大于这次验证的收益。
所以这里显式指定临时 ``--user-data-dir``。

用法::

    .venv/Scripts/python scripts/check_target.py                # 需靶场已起
    .venv/Scripts/python scripts/check_target.py --url <地址>
    .venv/Scripts/python scripts/check_target.py --port 9333

退出码：全部通过 0；任一失败 1。
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
from contextlib import suppress
from io import BytesIO
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from adapters.mock_exam.adapter import load_adapter  # noqa: E402
from core.config import RunConfig  # noqa: E402
from core.enums import ProbeName, TargetKind  # noqa: E402
from perception.pipeline import PerceptionContext, PerceptionPipeline  # noqa: E402
from perception.vision_probe import VisionProbe  # noqa: E402
from target.base import TargetUnavailableError  # noqa: E402
from target.browsers import (  # noqa: E402
    BrowserTargetSource,
    managed_user_data_dir,
    system_user_data_dir,
)
from target.windows import DesktopWindowSource  # noqa: E402

#: 自检专用调试端口。**刻意不用默认的 9222** —— 用户手上很可能已经有一个
#: 带 9222 的浏览器在跑（那正是本功能的常见状态），撞上去会把自检搞成偶发。
DEFAULT_PORT = 9333

DEFAULT_URL = "http://127.0.0.1:8899/quiz.html?seq=21"

_results: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    _results.append((ok, label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


def _brightest(image: Any) -> float:
    """灰度图的最高亮度。

    ``getextrema()`` 的返回类型取决于图像模式（单值 / 元组），
    统一折算成 float 再比较 —— 顺手也把 mypy 的联合类型收窄掉。
    """
    extrema = image.convert("L").getextrema()
    values = extrema if isinstance(extrema, tuple) else (extrema,)
    return max(float(value) for value in values)


def _gray_span(image: Any) -> int:
    """灰度图的**明暗跨度**（最亮 − 最暗）。

    为什么量跨度而不是亮度：一张**纯色**图（全白的错误页、全黑的没渲染画面）
    字节数可以很大，但模型在上面什么都读不到。跨度 > 0 才说明画面上真有内容。
    """
    extrema = image.convert("L").getextrema()
    values = extrema if isinstance(extrema, tuple) else (extrema,)
    return int(max(values) - min(values))


def _inspect_png(path: str | None) -> tuple[bytes | None, tuple[int, int], int]:
    """读回落盘的画面，返回 ``(字节, 尺寸, 明暗跨度)``。读不到返回空值。

    为什么从磁盘读回而不是让探针直接交字节：探针的契约就是「图落盘 + 给引用」
    （``screenshot_ref``）。自检要验的正是**这条契约真的落地了** ——
    直接调底层截图会把「引用写错路径」这一类故障盖掉。
    """
    if not path:
        return None, (0, 0), 0
    try:
        png = Path(path).read_bytes()
    except OSError:
        return None, (0, 0), 0
    with Image.open(BytesIO(png)) as image:
        size = (image.width, image.height)
        span = _gray_span(image)
    return png, size, span


def _viewport_size(page: Any) -> tuple[int, int] | None:
    """页面的视口 CSS 尺寸。通过 CDP 附加来的页面常常给不出（实测），故容忍为空。"""
    with suppress(Exception):
        viewport = page.viewport_size
        if viewport:
            return int(viewport["width"]), int(viewport["height"])
    return None


async def run_browser_checks(url: str, port: int, channel: str) -> None:
    profile_dir = Path(tempfile.mkdtemp(prefix="autolearn-checktarget-"))
    source = BrowserTargetSource(
        port=port,
        channel=channel,
        # 临时 Profile：不碰用户真实浏览器数据，也不碰软件的专用 Profile
        user_data_dir=profile_dir,
        extra_args=["--headless=new", "--disable-gpu", "--window-size=1280,900", url],
    )
    try:
        # -- 1. 接管启动（含全部前置） ----------------------------------------
        print("[1] 接管启动（建目录 → 起进程 → 等端口 → 等出标签页）")
        try:
            await source.launch_and_wait(wait_s=25.0, tab_wait_s=10.0)
            check(True, f"调试端口 {port} 就绪且已枚举到标签页")
        except Exception as exc:
            check(False, f"接管启动失败：{exc}")
            return
        check(await source.is_endpoint_alive(), "端点探活通过")

        # 这条是 Chrome 136+ 那条静默限制的守门断言：数据目录必须**不是**
        # 系统默认目录，否则调试端口会被无声忽略。
        default_dir = system_user_data_dir(channel)
        check(
            default_dir is None or source.user_data_dir != default_dir,
            "用的是非默认数据目录（Chrome 136+ 的前提）",
        )
        check(
            "browser_profile" in str(managed_user_data_dir(channel)),
            "自管 Profile 目录命名符合约定",
        )

        # -- 2. 扫描正在运行的目标 ------------------------------------------
        print("[2] 扫描正在运行的标签页")
        targets = await source.list_targets()
        check(bool(targets), f"扫到 {len(targets)} 个标签页")
        matching = [t for t in targets if "quiz.html" in (t.url or "")]
        check(bool(matching), "扫到了目标题目页（url 含 quiz.html）")
        if not matching:
            return
        target = matching[0]
        check(target.kind.value == "browser_page", f"目标类型 = {target.kind.value}")
        check(
            target.channels == [ProbeName.VISION],
            f"能力矩阵只回传视觉通道：{target.channels}",
        )

        # -- 3. 附加 + 视口截图（唯一的读题通道） -----------------------------
        print("[3] 附加 + 视口截图（v0.2.0 起题目只经模型的眼睛读）")
        adapter = load_adapter()
        pipeline = PerceptionPipeline([VisionProbe()], RunConfig())
        async with source.open(target.target_id) as handle:
            page = handle.page
            check(page is not None, "拿到 Playwright Page 句柄")
            if page is None:
                return
            check(
                handle.channels == frozenset({ProbeName.VISION}),
                "目标句柄只支持视觉通道："
                f"{sorted(name.value for name in handle.channels)}",
            )
            check(
                handle.supports(ProbeName.VISION),
                "编排层护栏会放行视觉通道（supports）",
            )
            before = page.url
            ctx = PerceptionContext(
                item_id="check-target",
                run_id="check-target",
                cfg=RunConfig(),
                timeout_s=8.0,
                # 落图目录挂在临时 Profile 下，收尾时随它一起删掉，不留垃圾
                screenshot_dir=profile_dir / "shots",
            )
            result = await pipeline.run(page, adapter, ctx)
            check(
                "vision:unavailable" not in result.warnings,
                "视觉通道在当前页面上可用（有 body 可截）",
            )
            check(
                "vision:crop_ok" in result.warnings,
                f"截到画面（标记 {result.warnings[:3]}）",
            )
            check(
                result.channel_used is ProbeName.VISION,
                f"产出通道 = 视觉（实际 {result.channel_used.value}）",
            )
            check(
                result.trace is not None and result.trace.needs_vision,
                "仲裁判定「把图交给模型读」（needs_vision）",
            )

            png, size, span = _inspect_png(result.screenshot_ref)
            check(png is not None, f"画面确实落盘且读得回来：{result.screenshot_ref}")
            if png is not None:
                check(
                    png.startswith(b"\x89PNG\r\n\x1a\n"),
                    "产物是合法 PNG 头（不是错误页 / 半截文件）",
                )
                check(len(png) > 2048, f"画面非空（{len(png)} 字节）")
                check(
                    320 <= size[0] <= 8192 and 240 <= size[1] <= 8192,
                    f"截图尺寸在合理区间：{size[0]}×{size[1]}"
                    "（上限同时挡住「偷偷截了全页」）",
                )
                viewport = _viewport_size(page)
                if viewport is not None:
                    check(
                        size == viewport,
                        f"图像像素 == 视口 CSS 像素 {size}（scale=css，坐标只乘一次）",
                    )
                check(span > 8, f"画面上有明暗内容（灰度跨度 {span}），不是纯色空图")

            # **附加模式绝不导航**：截完图后页面还停在原地址
            check(page.url == before, "页面 URL 未被导航（附加模式红线）")
            check(
                await page.locator("body").count() > 0,
                "附加拿到的页面上有 body（视觉探针的可用性判据就这一条）",
            )

        # -- 4. 收尾不动用户的浏览器 -----------------------------------------
        after = await source.list_targets()
        check(
            any(t.target_id == target.target_id for t in after),
            "断开后目标仍在（只断连接，不关用户的标签页）",
        )

        # -- 5. 附加不存在的目标要明确报错 ------------------------------------
        print("[4] 目标不存在时的错误语义")
        try:
            async with source.open("no-such-target-id"):
                pass
            check(False, "附加不存在的目标应当报错")
        except TargetUnavailableError as exc:
            check("不存在" in str(exc) or "已关闭" in str(exc), f"报错可读：{exc}")
    finally:
        source.shutdown()
        shutil.rmtree(profile_dir, ignore_errors=True)


async def run_desktop_checks() -> None:
    """桌面窗口目标：枚举 → 附加 → 截图 → 坐标换算自洽。

    **不点击。** 自检脚本去动用户的桌面（切前台、发鼠标事件）是不可接受的；
    点击路径由单测（``to_screen`` 换算）与「提不到前台就放弃点击」的护栏覆盖。
    """
    print("[5] 桌面窗口目标（抓正在运行的应用程序）")
    source = DesktopWindowSource()
    infos = await source.list_targets()
    check(bool(infos), f"扫到 {len(infos)} 个可见程序窗口")
    if not infos:
        return
    check(
        all(i.kind is TargetKind.DESKTOP_WINDOW for i in infos),
        "目标类型都是 desktop_window",
    )
    check(
        all(i.channels == [ProbeName.VISION] for i in infos),
        "能力矩阵：原生窗口也只有视觉通道（没有文档可附加，只能看画面）",
    )
    check(
        all(i.target_id.startswith("win:") for i in infos),
        "目标 ID 形如 win:<hwnd>（与浏览器标签页 ID 不混用）",
    )

    # 取样窗口要挑，不能抓第一个就算：叠加层（NVIDIA/Steam/MSI 那类 overlay）
    # 画面本来就是透明或全黑，「非纯黑」的断言会对它们误报。
    # 自检要验的是**截图管线拿到了真实像素**，所以从若干个窗口里挑一个有画面的。
    candidates = [i for i in infos if "overlay" not in (i.app or "").lower()][:6]
    if not candidates:
        candidates = infos[:6]

    sampled: str | None = None
    for candidate in candidates:
        print(f"      试采窗口：{candidate.title[:40]!r}（{candidate.app}）")
        async with source.open(candidate.target_id) as handle:
            check(handle.page is None, "桌面目标不交 Page（原生窗口没有文档）")
            check(handle.surface is not None, "桌面目标交出 ScreenSurface")
            if handle.surface is None:
                return
            png = await handle.surface.capture()
            check(bool(png), f"截图成功（{len(png)} bytes）")
            with Image.open(BytesIO(png)) as image:
                width, height = image.size
                check(width > 0 and height > 0, f"截图尺寸 {width}x{height}")
                bright = _brightest(image)
            check(
                handle.surface.grab_method in {"printwindow", "screen_grab"},
                f"截图方法 = {handle.surface.grab_method}",
            )
            # 换算自洽：图像左上角对应的屏幕位置必须正好是窗口原点
            left, top = handle.surface.to_screen(0, 0)
            check(
                (left, top) == handle.surface.origin,
                f"图像(0,0) → 屏幕{(left, top)}，与窗口原点一致",
            )
            center_x, center_y = handle.surface.to_screen(width // 2, height // 2)
            check(
                center_x > left and center_y > top,
                f"图像中心 → 屏幕({center_x}, {center_y})，落在窗口内（换算方向正确）",
            )
            if bright > 8:
                sampled = candidate.title
                break

    check(
        sampled is not None,
        f"至少有一个窗口截出了非纯黑画面（取样 {sampled!r}）—— "
        "证明截图管线拿到的是真实像素，不是空位图",
    )

    print("[6] 窗口关闭后的错误语义")
    try:
        async with source.open("win:1"):
            pass
        check(False, "附加无效窗口句柄应当报错")
    except TargetUnavailableError as exc:
        check("不存在" in str(exc), f"报错可读：{exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="P11 目标采集层真机自检")
    parser.add_argument("--url", default=DEFAULT_URL, help="启动时打开的页面")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="调试端口")
    parser.add_argument(
        "--channel", default="chrome", help="浏览器通道（chrome / msedge）"
    )
    parser.add_argument(
        "--only",
        choices=["browser", "desktop"],
        help="只跑其中一类（排障时省时间）",
    )
    args = parser.parse_args()

    print("AutoLearn P11 目标采集层真机自检\n")
    if args.only != "desktop":
        asyncio.run(run_browser_checks(args.url, args.port, args.channel))
    if args.only != "browser":
        asyncio.run(run_desktop_checks())

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
