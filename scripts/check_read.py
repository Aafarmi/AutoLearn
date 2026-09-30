"""视觉「读题」真机自检：让模型读**真实网页 / 程序画面**里的题目与页面观测。

这是「真实站点读不出题目」这条链路的验收工具。它只做一件事：

    截一张视口 / 窗口图 → 交给视觉组读（**一份**固定格式：``page`` 观测 + ``questions``）
    → 打印题目（题干 / 选项 / 每个包围框）与**页面观测**
    → 用**真的** :func:`core.run_plan.derive_plan` 裁决一份运行方案并打印
    → 把框画回图上存一份 PNG，供人肉眼核对「模型指的地方对不对」。

**2026-09-30 起不再有「开局标定 / 找下一题控件 / 收尾确认」三个一次性契约**
（``--calibrate`` / ``--locate-next`` / ``--confirm`` 与它们对应的三套提示词都已删除）：
读图只有一个契约，而「怎么推进 / 什么时候提交 / 是不是做完了」全部由程序从
``page`` 观测里**裁决**。所以本脚本打的就是那一份裁决结果 —— 它来自与运行期
**同一段代码**（``core.run_plan.derive_plan``），不是另写一套说法；
两边若不一致，这份诊断就失去了意义。

为什么仍然只打印、不动手：读题的验收与执行的验收必须分开。
点错一次就是一次真实的误操作，所以本脚本**绝不点击、绝不导航、绝不滚动、绝不提交**。

用法::

    # 按 URL 片段挑标签页（需要先用界面「接管启动浏览器」）
    .venv/Scripts/python scripts/check_read.py --url-contains chaoxing
    # 直接给 CDP target id
    .venv/Scripts/python scripts/check_read.py --target-id 28EA69BF...
    # 读**桌面窗口**（原生程序，没有文档可附加）
    .venv/Scripts/python scripts/check_read.py --window-title 微信

退出码：读到题目 0；**一道题都没读到 / 调用失败** 1。
（一道题都没读到时，观测与方案照常打印 —— 收尾那一屏本来就没有题，
``page.completed`` 才是那一屏真正要看的东西。）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import suppress
from io import BytesIO
from pathlib import Path
from typing import Any

# 控制台编码常常是 GBK（Windows 中文环境默认）。失败路径要打印 ❌ / ⚠ 这类符号，
# 一旦 `print` 抛 UnicodeEncodeError，**真正的失败原因会被这条编码错误盖掉** ——
# 排查时看到的变成「脚本崩了」，而不是「读题失败：provider_unavailable」。
# 让它容错降级，别让排版符号毁掉一次诊断。
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if callable(_reconfigure):
        with suppress(Exception):
            _reconfigure(errors="replace")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw  # noqa: E402

from act.screen import candidate_points  # noqa: E402
from core.enums import AdvanceMethod  # noqa: E402
from core.models import PageView, ReadBatch, ReadResult  # noqa: E402
from core.run_plan import derive_plan, plan_summary  # noqa: E402
from perception.vision_probe import VisionProbe  # noqa: E402
from solve.providers.factory import build_default_chain  # noqa: E402
from solve.reader import parse_read_batch, read_questions  # noqa: E402
from ui import deps  # noqa: E402

#: 画框用的颜色。四类框各一色，是因为它们**用途完全不同**：
#: 红=选项（执行层会去点它作答）、蓝=提交（不可逆）、绿=推进控件、黄=答题卡当前格。
_BOX_COLORS = {
    "option": (220, 20, 20),
    "submit": (20, 60, 220),
    "next": (20, 150, 60),
    "card": (200, 150, 0),
}

NormBox = tuple[float, float, float, float]
ImageSize = tuple[int, int]


def _box_text(box: NormBox | None) -> str:
    """归一化框 → 可读文本（``None`` 一律显示 ``—``，不要显示成 ``0,0,0,0``）。"""
    if box is None:
        return "—"
    return "(" + ", ".join(f"{value:.3f}" for value in box) + ")"


def _center(box: NormBox, size: ImageSize) -> tuple[float, float]:
    """归一化框 → 视口像素中心（与执行层同一条换算：只乘一次图像尺寸）。"""
    return ((box[0] + box[2] / 2.0) * size[0], (box[1] + box[3] / 2.0) * size[1])


def _draw(batch: ReadBatch, png: bytes, out: Path) -> None:
    """把模型给出的包围框画回图上，方便肉眼核对。

    为什么要画：执行层是**照着这些框去点坐标**的，而「模型给的框到底落在哪」
    在日志里只能看到一串数字。两次真机事故（点在行尾空白、点了被禁用的交卷按钮）
    最后都是靠把框画回去才看明白的。
    """
    with Image.open(BytesIO(png)) as source:
        image = source.convert("RGB")
    width, height = image.size
    draw = ImageDraw.Draw(image)

    def rect(box: NormBox, color: tuple[int, int, int], label: str = "") -> None:
        x, y, w, h = box
        draw.rectangle(
            [x * width, y * height, (x + w) * width, (y + h) * height], outline=color, width=3
        )
        if label:
            draw.text((x * width + 6, y * height + 4), label, fill=color)

    for result in batch.questions:
        for option in result.options:
            rect(option.box, _BOX_COLORS["option"], option.label)

    page = batch.page
    if page is not None:
        if page.submit is not None and page.submit.box is not None:
            rect(page.submit.box, _BOX_COLORS["submit"], "submit")
        if page.next_control is not None:
            rect(page.next_control.box, _BOX_COLORS["next"], "next")
        if page.card is not None and page.card.current_box is not None:
            rect(page.card.current_box, _BOX_COLORS["card"])

    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out)


def _print_page(page: PageView | None) -> None:
    """打印 ``page`` 观测 —— 它是**程序那套判断逻辑的唯一输入**，所以逐字段看。"""
    if page is None:
        print("\n── page 观测：**缺失**（模型没给这一块）")
        print("   → 没有观测就没有方案：运行期会如实停下，绝不会猜一种推进方式硬试。")
        return
    print("\n── page 观测（视觉组看到的这一屏，只报「有什么」）")
    print(
        f"   进度文字：{page.progress or '—'}"
        f"   总题数：{page.total if page.total else '—'}"
        f"   当前题号：{page.current if page.current else '—'}"
    )
    card = page.card
    if card is None:
        print("   答题卡：无")
    else:
        print(
            f"   答题卡：{card.cols}×{card.rows} 格 box={_box_text(card.box)}"
            f"  当前格={_box_text(card.current_box)}  下一格={_box_text(card.next_box)}"
        )
    control = page.next_control
    if control is None:
        print("   推进控件：无")
    else:
        print(f"   推进控件：{control.label or '（无文字）'} box={_box_text(control.box)}")
    submit = page.submit
    if submit is None:
        # 这一行是 2026-09-29 那个 bug 的「看一眼就明白」之处：整卷页面上
        # 「这一屏没有提交按钮」是**正常形态**，不影响推进，也不影响提交时机。
        print("   提交框：无（整卷页面上很正常 —— 提交时机由方案定，不看这一屏）")
    else:
        scope = submit.scope.value if submit.scope else "模型未说"
        print(f"   提交框：{_box_text(submit.box)}  范围={scope}")
    print(f"   是否整卷做完：{page.completed.value}   能否向下滚动：{page.scrolling}")
    if page.reason:
        print(f"   模型给的依据：{page.reason}")


def _print_plan(batch: ReadBatch, size: ImageSize) -> None:
    """用**真的** ``derive_plan`` 裁决并打印方案（含算出来的下一格落点）。

    与运行期同一段代码是关键：诊断工具若自己另写一套「我觉得它会这么走」，
    两边一旦分叉，这份输出反而会把排查方向带偏。
    """
    plan = derive_plan(batch, batch_size=len(batch.questions))
    print("\n── 运行方案（derive_plan 裁决；运行期**完全**照它执行，中途不换招）")
    print(f"   {plan_summary(plan)}")
    print(f"   推进方式：{plan.method.value}   开局题号：{plan.current or '—'}")
    print(f"   裁决依据：{plan.reason}")

    if plan.method is AdvanceMethod.CARD:
        number = (plan.current or plan.card_anchor) + 1
        cell = plan.card_target(number)
        if cell is None:
            # 正常情况下走不到这里：``derive_plan`` 自己就会因为「下一格推算不出来」
            # 而**整体放弃 CARD**（连答题卡几何一起清掉）。留着这一支是双保险 ——
            # 万一将来那条否决被改松，这里必须仍然打印「算不出来」，而不是编一个格子。
            print(f"   下一格（题号 {number}）：**推算不出来**（方案本不该选 CARD，请报给开发者）")
        else:
            x, y = _center(cell, size)
            print(f"   下一格（题号 {number}）：box={_box_text(cell)} → 点 ({x:.0f}, {y:.0f})")
    elif plan.method is AdvanceMethod.CLICK:
        assert plan.control_box is not None  # CARD/CLICK 的裁决前提就是它有框
        x, y = _center(plan.control_box, size)
        print(
            f"   落点：推进控件 {plan.control_label or '（无文字）'}"
            f" box={_box_text(plan.control_box)} → 点 ({x:.0f}, {y:.0f})"
        )
    elif plan.method is AdvanceMethod.SCROLL:
        print("   落点：向下滚动一步（滚完会再读一屏，确认**新题真的进来了**才算到位）")
    else:
        print("   落点：滑动手势翻页（页面不可滚动时的最后一招）")

    if plan.submit_box is None:
        print("   提交框：方案里没有 —— 收尾那一屏读到才用得上（运行期不会猜坐标）")
    else:
        x, y = _center(plan.submit_box, size)
        scope = plan.submit_scope.value if plan.submit_scope else "未定"
        print(
            f"   提交框：box={_box_text(plan.submit_box)} → 点 ({x:.0f}, {y:.0f})  范围={scope}"
        )


def _print_question(result: ReadResult, png: bytes, size: ImageSize) -> None:
    """打印一道题，以及**程序会去点的那一点**。

    ``candidate_points`` 与执行层用的是同一个函数（同一个框、同一张图），
    所以这里打出来的第一个候选点就是运行时第一下会落到的像素；
    它取的是**框内墨迹的质心**，不是框的几何中心 —— 这一点是 2026-09-28
    那次「点在行尾空白」事故的修复，诊断时必须能一眼看出来。
    """
    qtype = result.unsupported_qtype or result.qtype.value
    number = result.num_text or (str(result.index) if result.index else "?")
    print(f"\n── 第 {number} 道题 · qtype={qtype}")
    print(f"   题干：{result.stem}")
    if result.unsupported_qtype:
        print("   ⚠️ 这个题型本项目不支持（执行层只会点选项）→ 运行期会**如实拒绝**，不会硬套成单选")
    for option in result.options:
        candidates = candidate_points(png, option.box, size)
        x, y, reason = candidates[0]
        print(
            f"   {option.label}. {option.text}\n"
            f"      box={_box_text(option.box)} → 会点 ({x:.0f}, {y:.0f})  取点方式={reason}"
            f"   候选数={len(candidates)}"
        )
    if result.clipped or result.uncertain:
        # 这两组字段非空 = 运行期**门禁会拦下它**（不下传解题，停下等人）。
        # 诊断里必须显式提示，否则「读了但没用」看起来就像「读题失败」。
        print(
            f"   ⚠️ 门禁会拦：clipped={result.clipped or '[]'} uncertain={result.uncertain or '[]'}"
            " → 运行期不下传，会停下来等人核对画面"
        )


def _as_payload(batch: ReadBatch) -> str:
    """把这一屏的产出还原成 JSON 文本，用来验证**解析器**吃得回真实形状的输出。

    ``page`` **必须一起带上**：它是回复的一部分，只还原题目等于验一个比真实回复
    更简单的形状 —— 那样往返一致说明不了任何事（``parse_read_batch`` 正是按
    「有 page 没有题也算一批」的口径写的）。
    """
    body: dict[str, Any] = {
        "page": batch.page.model_dump(mode="json") if batch.page is not None else None,
        "questions": [
            {
                "index": result.index,
                "num_text": result.num_text,
                "qtype": result.unsupported_qtype or result.qtype.value,
                "stem": result.stem,
                "options": [
                    {"label": option.label, "text": option.text, "box": list(option.box)}
                    for option in result.options
                ],
                "clipped": list(result.clipped),
                "uncertain": list(result.uncertain),
                "note": result.note,
            }
            for result in batch.questions
        ],
        "more_below": batch.more_below,
        "note": batch.note,
    }
    return json.dumps(body, ensure_ascii=False)


async def _grab_browser(target_id: str, port: int) -> tuple[bytes, ImageSize, str]:
    """附加到标签页，截一张视口图。

    走 ``VisionProbe.crop_question(page)`` —— 与生产链路**同一段代码**截同一张图
    （视口 + ``scale="css"`` + 超时重试）。诊断工具若自己另写一套截图，
    「模型看到的不对」这类结论就没法复现到线上。
    """
    from target.browsers import BrowserTargetSource

    source = BrowserTargetSource(channel="chrome", port=port)
    async with source.open(target_id) as handle:
        page = handle.page
        assert page is not None
        png = await VisionProbe().crop_question(page)
        with Image.open(BytesIO(png)) as image:
            size = (image.width, image.height)
        return png, size, f"viewport {getattr(page, 'url', '')[:80]}"


async def _grab_window(title_contains: str) -> tuple[bytes, ImageSize, str]:
    """读**桌面窗口**（原生程序，没有文档可附加，只能整窗截图）。"""
    from target.windows import DesktopWindowSource

    source = DesktopWindowSource()
    for info in await source.list_targets():
        if title_contains.lower() in (info.title or "").lower():
            async with source.open(info.target_id) as handle:
                assert handle.surface is not None
                png = await handle.surface.capture()
                with Image.open(BytesIO(png)) as image:
                    size = (image.width, image.height)
                return png, size, f"window {info.title!r}（{info.app}）"
    raise SystemExit(f"找不到标题含 {title_contains!r} 的窗口")


async def _resolve_target(args: argparse.Namespace) -> tuple[bytes, ImageSize, str] | int:
    """拿到这一屏的图：桌面窗口 / 指定 target / 按 URL 片段挑标签页。"""
    if args.window_title:
        return await _grab_window(args.window_title)

    target_id = args.target_id
    if target_id is None:
        from target.browsers import BrowserTargetSource

        source = BrowserTargetSource(channel="chrome", port=args.port)
        candidates = await source.list_targets()
        hit = [t for t in candidates if args.url_contains.lower() in (t.url or "").lower()]
        if not hit:
            print("当前标签页：")
            for target in candidates:
                print(f"  - {target.target_id}  {(target.url or '')[:70]}")
            print(f"\n没有 URL 含 {args.url_contains!r} 的标签页。")
            return 1
        target_id = hit[0].target_id
    if target_id is None:  # pragma: no cover - 上面两条分支必有一条给出它
        print("没能确定目标标签页。")
        return 1
    return await _grab_browser(target_id, args.port)


async def run(args: argparse.Namespace) -> int:
    deps.reset_state()
    registry = deps.get_registry()
    providers = build_default_chain(
        registry.active_chain(),
        credentials=registry.credentials(),
    )
    print(f"降级链：{', '.join(getattr(p, 'name', '?') for p in providers)}")

    grabbed = await _resolve_target(args)
    if isinstance(grabbed, int):
        return grabbed
    png, size, what = grabbed
    print(f"画面：{what}  尺寸={size}  字节={len(png)}")

    batch, error = await read_questions(png, providers=providers)
    if batch is None:
        print(f"\n❌ 读题失败：{error or 'unknown'}")
        print("   可能：模型没回 / 回复解析不了 / 这一套配置里没有可用模型。")
        print("   原始回复见后台窗口日志；这一屏截图已落盘，可与模型看到的画面核对。")
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "screen.png").write_bytes(png)
        return 1

    _print_page(batch.page)
    _print_plan(batch, size)

    annotated = args.out / "read-annotated.png"
    _draw(batch, png, annotated)
    print(f"\n已把框画回图上：{annotated}")

    payload = _as_payload(batch)
    round_trip = parse_read_batch(payload)
    if round_trip is None or len(round_trip.questions) != len(batch.questions):
        print("\n⚠️ 往返校验失败：解析器吃不回刚刚这份结果（请连同这条一起报给开发者）")
        return 1
    print(f"往返校验：解析器能吃回这份结果（{len(round_trip.questions)} 道题）")

    if not batch.questions:
        print("\n⚠️ 这一屏**一道完整题目都没有**（收尾屏就长这样）。")
        print("   观测与方案仍然有效：收工与否只看 page.completed —— 那是收尾闸门唯一的放行条件。")
        return 1

    print(f"\n✅ 读到 {len(batch.questions)} 道完整题目（more_below={batch.more_below}）")
    for result in batch.questions:
        _print_question(result, png, size)
    if batch.note:
        print(f"\n模型备注：{batch.note}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="视觉读题真机自检（只读：不点击、不滚动、不提交）"
    )
    parser.add_argument("--url-contains", default="", help="按 URL 片段挑标签页")
    parser.add_argument("--target-id", default=None, help="直接给 CDP target id")
    parser.add_argument("--window-title", default=None, help="改为读桌面窗口（标题片段）")
    parser.add_argument("--port", type=int, default=9222, help="CDP 调试端口")
    parser.add_argument("--out", type=Path, default=Path("state/read-check"))
    args = parser.parse_args()

    print("AutoLearn 视觉读题真机自检（**不点击任何东西**）\n")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
