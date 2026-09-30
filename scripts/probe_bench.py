"""M1-4a 视觉通道量化框架（零密钥可验收）。

v0.2.0 起题目**只经模型的眼睛读**，原先「Net / DOM / Vision 三通道对比」没有
对比对象了 —— 通道只剩一条。于是本脚本只量一件事：

    **把一个题号的当前画面截成能交给模型的图，要多久、多大。**

指标口径（冻结）：
    - 一次「通道尝试」= 一个题号 × 一次截图；
    - `load_ms` 从导航开始到页面 load 事件为止。**它只在本脚本里量**，用来解释
      「等待」在总耗时里的占比 —— 程序自己已经不用任何页面结构判据了
      （曾经的「就绪断言」随 DOM 通道一并删除）；
    - `elapsed_ms` 从调 ``VisionProbe.probe()`` 计时到它返回，含截图重试，
      这才是视觉通道自己的成本；
    - `tokens` 只统计**该通道自己发起**的模型调用。视觉探针这一层只截图不识别，
      识别归 ``solve.reader``（Tier2），故恒为 0 并记 `pending_model_config`；
    - 本脚本**不判断「图里到底有没有题」** —— 那要模型来看，见 ``check_read.py``。
      两个工具分工明确：这里量截图成本，那里验模型读得对不对。

    **用户未添加模型配置时，本脚本依然可完整跑完**（M1-4a 的要求）：
    耗时与产物字节全量产出，token 一栏标注待回填。

用法：
    python scripts/probe_bench.py                       # 默认题库代表样
    python scripts/probe_bench.py --questions 1,21,27,30,39
    python scripts/probe_bench.py --out docs --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# 允许 `python scripts/probe_bench.py` 直接运行（把项目根放进 sys.path）
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 控制台编码常常是 GBK（Windows 中文环境默认）。报告里带 ✅ / ❌ / ≥ 这类符号，
# 一旦 `print(markdown)` 抛 UnicodeEncodeError，**采集已经全部跑完**却一条也看不到。
# 让输出容错降级，别让排版符号毁掉一次采集。
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if callable(_reconfigure):
        with suppress(Exception):
            _reconfigure(errors="replace")

from playwright.async_api import async_playwright  # noqa: E402

from adapters.mock_exam.adapter import load_adapter  # noqa: E402
from core.config import RunConfig  # noqa: E402
from perception.pipeline import PerceptionContext  # noqa: E402
from perception.vision_probe import VisionProbe  # noqa: E402

DEFAULT_BASE = "http://127.0.0.1:8899"
# 代表样：覆盖基准题 + 六类坑 + XHR 题型
DEFAULT_QUESTIONS = [1, 21, 24, 27, 30, 33, 36, 39]
QUESTIONS_JSON = ROOT / "mock_site" / "static" / "questions.json"

#: 报告里的坑码中英对照。只有生成 markdown 时才用得上，
#: 缺了 questions.json 也不影响采集（那一列留空而已）。
TRAP_LABELS = {
    "spa": "SPA 延迟挂载",
    "lazy": "懒加载",
    "canvas": "Canvas 题干",
    "iframe": "跨域 iframe",
    "cls": "类名混淆",
    "modal": "弹窗遮罩",
    "xhr": "XHR 拉题",
    "next_after_scroll": "下一题要滚动",
}


@dataclass
class VisionRecord:
    """一个题号的一次截图尝试。

    只有**一个**成功判据：``ok`` = 探针报出 ``vision:crop_ok``（截到了非空画面）。
    它**不等于**「读到了题」—— 那是模型的事，本层没有能力也不该替它下结论。
    """

    question_index: int
    ok: bool
    elapsed_ms: int = 0
    load_ms: int = 0
    tokens: int = 0
    token_state: str = "pending_model_config"
    bytes_out: int = 0
    traps: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class BenchReport:
    base_url: str
    questions: list[int]
    records: list[VisionRecord]

    def summary(self) -> dict[str, float]:
        """本次采集的汇总口径。**只有一个通道**，所以不再按通道分组。"""
        rows = self.records
        if not rows:
            return {}
        elapsed = sorted(r.elapsed_ms for r in rows)
        return {
            "attempts": len(rows),
            "ok": sum(1 for r in rows if r.ok),
            "rate": round(sum(1 for r in rows if r.ok) / len(rows), 3),
            "elapsed_ms_avg": round(statistics.fmean(elapsed), 1),
            "elapsed_ms_p95": round(elapsed[max(0, int(len(elapsed) * 0.95) - 1)], 1),
            "elapsed_ms_min": float(elapsed[0]),
            "elapsed_ms_max": float(elapsed[-1]),
            "load_ms_avg": round(statistics.fmean(r.load_ms for r in rows), 1),
            "tokens_total": float(sum(r.tokens for r in rows)),
            "bytes_out_total": float(sum(r.bytes_out for r in rows)),
            "bytes_out_avg": round(statistics.fmean(r.bytes_out for r in rows), 1),
        }


def _load_traps() -> dict[int, list[str]]:
    """从题库读「每个题号踩了哪些坑」，只用于给报告加可读的注解。

    读的是**靶场自己的题库文件**，与已删除的读题通道无关；
    缺文件就让这一列为空，采集本身不受影响。
    """
    try:
        payload = json.loads(QUESTIONS_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        int(q["index"]): list(q.get("traps") or []) for q in payload.get("questions", [])
    }


async def _bench_one(
    page: Any,
    adapter: Any,
    question_index: int,
    base_url: str,
    timeout_s: float,
    traps: list[str],
) -> VisionRecord:
    """对单个题号跑一次视觉截图。

    导航用 ``load`` 而不是 ``domcontentloaded``：视觉读的是**渲染后的画面**，
    在「文档骨架刚到、题目还没挂上来」的那一刻截图，量到的只是一张白屏 ——
    那不是通道慢，是量早了。
    """
    ctx = PerceptionContext(
        item_id=f"bench-{question_index}",
        run_id="probe-bench",
        cfg=RunConfig(),
        timeout_s=timeout_s,
    )

    started = time.perf_counter()
    await page.goto(
        f"{base_url}/quiz.html?seq={question_index}",
        wait_until="load",
        timeout=20000,
    )
    load_ms = int((time.perf_counter() - started) * 1000)

    vision = VisionProbe()
    started = time.perf_counter()
    result = await vision.probe(page, adapter, ctx)
    elapsed = int((time.perf_counter() - started) * 1000)

    size = 0
    for warning in result.warnings:
        if warning.startswith("vision:bytes="):
            size = int(warning.split("=", 1)[1])

    # 常规标记（crop_ok / bytes）每次都出现，进报告只会淹没
    # 真正反常的那一条（失败原因）；所以只留异常信息。
    noise = {"vision:crop_ok"}
    notes = [
        warning
        for warning in result.warnings
        if warning not in noise and not warning.startswith("vision:bytes=")
    ]

    return VisionRecord(
        question_index=question_index,
        ok=any("crop_ok" in warning for warning in result.warnings),
        elapsed_ms=elapsed,
        load_ms=load_ms,
        tokens=0,
        token_state="pending_model_config",
        bytes_out=size,
        traps=traps,
        notes=notes,
    )


async def run_bench(base_url: str, questions: list[int], timeout_s: float) -> BenchReport:
    adapter = load_adapter()
    trap_map = _load_traps()
    records: list[VisionRecord] = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            channel=os.environ.get("AUTOLEARN_BROWSER_CHANNEL", "msedge"),
            headless=True,
            args=["--autoplay-policy=no-user-gesture-required", "--mute-audio"],
        )
        context = await browser.new_context(viewport={"width": 1280, "height": 720})
        page = await context.new_page()
        try:
            for index in questions:
                records.append(
                    await _bench_one(
                        page, adapter, index, base_url, timeout_s, trap_map.get(index, [])
                    )
                )
        finally:
            await context.close()
            await browser.close()

    return BenchReport(base_url=base_url, questions=questions, records=records)


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #


def render_markdown(report: BenchReport) -> str:
    summary = report.summary()
    lines: list[str] = []
    lines.append("# 视觉通道耗时 / token 采集（M1-4a 骨架）\n")
    lines.append(
        "> 本文件由 `scripts/probe_bench.py` 生成。改口径请改脚本，勿手改本文件。\n"
    )
    lines.append(f"- 靶场：`{report.base_url}`")
    lines.append(f"- 采样题号：{', '.join(str(q) for q in report.questions)}")
    lines.append(f"- 采样题数：{len(report.questions)}；截图尝试总数：{len(report.records)}")
    lines.append(
        "- 截图口径：**一律视口截图**（`scale=\"css\"`，图像像素 == 视口 CSS 像素），"
        "禁用全页截图"
    )
    lines.append(
        "- token 口径：只统计通道自身发起的模型调用。视觉探针这一层只截图不识别，"
        "读题 token 归 Tier2，故恒为 0"
    )
    lines.append(
        "- 本表只回答「截一张图多贵」。**「图里有没有题」要模型说了算** "
        "（见 `scripts/check_read.py`）\n"
    )

    lines.append("## 1. 汇总\n")
    if summary:
        lines.append("| 尝试 | 截到画面 | 成功率 | 平均耗时 ms | P95 ms | 最小 ms | 最大 ms "
                     "| 平均等待 load ms | token 合计 | 产物字节合计 |")
        lines.append("|------|---------|--------|------------|--------|---------|---------"
                     "|-----------------|-----------|-------------|")
        lines.append(
            f"| {int(summary['attempts'])} | {int(summary['ok'])} "
            f"| {summary['rate'] * 100:.1f}% "
            f"| {summary['elapsed_ms_avg']} | {summary['elapsed_ms_p95']} "
            f"| {int(summary['elapsed_ms_min'])} | {int(summary['elapsed_ms_max'])} "
            f"| {summary['load_ms_avg']} | {int(summary['tokens_total'])} "
            f"| {int(summary['bytes_out_total'])} |"
        )
        lines.append("")
        lines.append(
            f"平均每张图 **{int(summary['bytes_out_avg'])} 字节**"
            f"（共 {int(summary['bytes_out_total'])} 字节）—— "
            "它是「每次读题要传给模型多少图」的直接成本。"
        )
    else:
        lines.append("本次没有任何采样。")

    lines.append("\n## 2. 逐题明细\n")
    lines.append("| 题号 | 坑 | 截到画面 | 耗时 ms | 等待 load ms | 字节 | token | 异常信息 |")
    lines.append("|------|----|---------|--------|-------------|------|-------|---------|")
    for record in report.records:
        traps = "、".join(TRAP_LABELS.get(code, code) for code in record.traps) or "—"
        notes = ", ".join(record.notes[:3]) or "—"
        lines.append(
            f"| {record.question_index} | {traps} | "
            f"{'✅' if record.ok else '❌'} | {record.elapsed_ms} | {record.load_ms} "
            f"| {record.bytes_out} | {record.tokens if record.tokens else '0*'} | {notes} |"
        )
    lines.append("\n`0*` = 本层不调模型（识别阶段尚未接入模型配置）。\n")
    lines.extend(_render_conclusions(report, summary))
    return "\n".join(lines)


def _render_conclusions(report: BenchReport, summary: dict[str, float]) -> list[str]:
    """从本次采集数据里**直接算出来**的结论，避免手写数字与数据脱节。"""
    lines: list[str] = ["\n## 3. 结论（由本次数据自动推导）\n"]
    rows = report.records
    if not rows or not summary:
        lines.append("1. 本次没有数据，无法推导结论。")
        return lines

    lines.append(
        f"1. **截图可用率 {int(summary['ok'])}/{int(summary['attempts'])}"
        f"（{summary['rate'] * 100:.1f}%）**：视觉通道是唯一读题路径，"
        "截不到画面就是这一题**停下来留档**（`arbiter:vision_failed` + "
        "`paused_for_dump`），不会被静默跳过。"
    )

    slowest = max(rows, key=lambda r: r.elapsed_ms)
    fastest = min(rows, key=lambda r: r.elapsed_ms)
    lines.append(
        f"2. **耗时分布**：最慢 {slowest.question_index} 题 {slowest.elapsed_ms} ms、"
        f"最快 {fastest.question_index} 题 {fastest.elapsed_ms} ms"
        f"（P95 {summary['elapsed_ms_p95']} ms）。"
        "截图带重试（真实站点的持续动画常让第一次超时），"
        "所以**单次毛刺不代表通道不可用**。"
    )

    share = (summary["load_ms_avg"] / summary["elapsed_ms_avg"]) if summary["elapsed_ms_avg"] else 0
    lines.append(
        f"3. **等待与截图的占比**：平均等待页面 load {summary['load_ms_avg']} ms，"
        f"截图本身平均 {summary['elapsed_ms_avg']} ms（等待约占 {share * 100:.0f}%）。"
        "等待是**页面慢**，不是通道慢 —— 两者分开记，才不会把站点的锅算到视觉头上。"
    )

    empty = [r.question_index for r in rows if r.bytes_out == 0]
    if empty:
        lines.append(
            f"4. **空白产物**：第 {', '.join(str(i) for i in empty)} 题产出 0 字节 —— "
            "看图核对（`check_read.py` 会把模型指的位置画回图上）。"
        )
    else:
        lines.append(
            "4. **产物齐全**：每一题都产出了非空截图，没有 0 字节的空图。"
        )

    lines.append(
        "5. **token 待回填**：用户尚未在软件内添加模型配置，读题的识别 token 记 0。"
        "接入模型后本表由同一脚本重新生成即可 —— 截图成本与识别成本是两笔账，"
        "这一栏只记后者。"
    )
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description="M1-4a 视觉通道量化")
    parser.add_argument("--base", default=os.environ.get("AUTOLEARN_MOCK_URL", DEFAULT_BASE))
    parser.add_argument(
        "--questions",
        default=",".join(str(q) for q in DEFAULT_QUESTIONS),
        help="逗号分隔的题号",
    )
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--out", default="logs", help="输出目录（量化产物，不进 docs/）")
    parser.add_argument("--json", action="store_true", help="同时输出 JSON")
    args = parser.parse_args()

    questions = [int(x) for x in args.questions.split(",") if x.strip()]
    report = asyncio.run(run_bench(args.base.rstrip("/"), questions, args.timeout))

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    markdown = render_markdown(report)
    (out_dir / "vision-bench.md").write_text(markdown, encoding="utf-8")
    if args.json:
        (out_dir / "vision-bench.json").write_text(
            json.dumps(
                {
                    "base_url": report.base_url,
                    "questions": report.questions,
                    "summary": report.summary(),
                    "records": [asdict(r) for r in report.records],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    print(markdown)


if __name__ == "__main__":
    main()
