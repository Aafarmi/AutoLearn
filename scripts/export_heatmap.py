"""降级热力图导出（M4-4）。

读 ``level_stat`` 表，按「动作类型 × 降级级别」统计落点，产出
``logs/降级热力图.md``，回答一个问题：**哪一类控件最容易降级**。

⚠️ 默认输出挪到 ``logs/``（2026-09-29）：它是**量化产物**，不是人工维护的文档 ——
``docs/`` 只放「人写的、要长期维护的」那几份。

为什么看这个
------------
执行阶梯是「点不到就换一招」，所以 ``level_used`` 越高越说明这一路的自动化越脆。
热力图把「脆」按控件类型摊开，回答的是同一个问题：**哪一类控件最容易降级**。

v0.2.0 的读法变了（**这张表最容易读错的地方**）：题目侧只剩一条路 ——
模型看截图给出坐标（``L6_VISION_XY``），选项选中由**选项区域像素差分**校验。
于是 ``select_option`` / ``submit`` / ``click`` / ``swipe`` 恒落 L6，而 L6 对它们
**就是常规路径，不是劣化**。

所以「降级」不能再按「是不是 L1」判 —— 那会把每一个题目动作都记成降级，
把整张表变成一句恒真的废话。判据改为**按动作类型取各自的基线**：
``level_used`` 不是该动作那条阶梯的**第一级**才算降级。基线与阶梯全部从
:mod:`act.actuator` 读，不在这里抄一份（抄一份迟早与实现分叉，
而分叉的表现就是这张表开始说谎）。

真正值得盯的是**媒体动作**：``play_media`` / ``pause_media`` / ``seek_media`` /
``next_episode`` 大量离开各自的第一级，才说明媒体锚点漂了、该修了。

用法::

    python scripts/export_heatmap.py                       # 全部运行
    python scripts/export_heatmap.py --run <run_id>        # 只看某一次运行
    python scripts/export_heatmap.py --out logs/降级热力图.md
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # 允许 `python scripts/export_heatmap.py` 直接跑
    sys.path.insert(0, str(ROOT))

from act.actuator import LEVELS as NEXT_EPISODE_LEVELS  # noqa: E402
from act.actuator import MEDIA_LEVELS, PAUSE_LEVELS, SEEK_LEVELS  # noqa: E402
from core.db import DEFAULT_DB_PATH, connect  # noqa: E402
from core.enums import ActionKind, ActLevel  # noqa: E402

#: 表格的列顺序（= 阶梯的层次顺序）。它只是**展示顺序**，与「基线」无关。
LEVELS: tuple[ActLevel, ...] = (
    ActLevel.L1_LOCATOR,
    ActLevel.L2_FORCE,
    ActLevel.L3_SCROLL,
    ActLevel.L4_FOCUS_KEYS,
    ActLevel.L5_BBOX,
    ActLevel.L6_VISION_XY,
)

#: 题目侧动作的「阶梯」：只有一级。模型给的坐标就是唯一路径，
#: 没有「退到下一招」这回事 —— 所以它们**永远不会被记成降级**。
_QUESTION_LADDER: tuple[ActLevel, ...] = (ActLevel.L6_VISION_XY,)

#: 动作类型 → 它**设计上**该走的那条阶梯。基线 = 该元组的第 0 项。
#:
#: 三条媒体阶梯的第一级**刻意各不相同**（见 ``act/actuator.py`` 里各自的理由）：
#: 播放先试真实点击、暂停先试键盘/脚本（弹题遮罩会盖住播放按钮）、
#: seek 先走脚本（唯一能精确落点）。所以「暂停落在 L4 是正常的」——
#: 用一把尺子去量三类动作，结论必然是错的。
LADDER_BY_KIND: dict[str, tuple[ActLevel, ...]] = {
    ActionKind.SELECT_OPTION.value: _QUESTION_LADDER,
    ActionKind.SUBMIT.value: _QUESTION_LADDER,
    ActionKind.CLICK.value: _QUESTION_LADDER,
    ActionKind.SWIPE.value: _QUESTION_LADDER,
    ActionKind.PLAY_MEDIA.value: MEDIA_LEVELS,
    ActionKind.PAUSE_MEDIA.value: PAUSE_LEVELS,
    ActionKind.SEEK_MEDIA.value: SEEK_LEVELS,
    ActionKind.NEXT_EPISODE.value: NEXT_EPISODE_LEVELS,
}

#: 没登记过的动作按「媒体锚点的常规路径」算 —— 旧数据里可能有历史 kind。
_FALLBACK_LADDER: tuple[ActLevel, ...] = NEXT_EPISODE_LEVELS

#: 动作类型的中文说明（报告给不熟悉枚举的人看）
KIND_LABELS: dict[str, str] = {
    "click": "通用点击（下一题 / 关弹窗）",
    "select_option": "选中选项",
    "submit": "提交",
    "play_media": "播放",
    "pause_media": "暂停",
    "seek_media": "跳转进度",
    "next_episode": "下一集",
    #: 「下一题」的另一种实现：画面上没有按钮可点，只能靠手势翻页（P12）
    "swipe": "滑动（下一页 / 下一题）",
}

DEFAULT_OUT = ROOT / "logs" / "degrade-heatmap.md"


def ladder_of(kind: str) -> tuple[ActLevel, ...]:
    """该动作设计上该走的阶梯。未登记的动作退回媒体锚点路径。"""
    return LADDER_BY_KIND.get(kind, _FALLBACK_LADDER)


def baseline_of(kind: str) -> ActLevel:
    """常规路径落点 = 该动作那条阶梯的第一级。"""
    return ladder_of(kind)[0]


@dataclass
class KindStats:
    """一种动作的降级画像。"""

    counts: Counter[str] = field(default_factory=Counter)
    total: int = 0
    degraded: int = 0
    failed: int = 0
    elapsed_ms: int = 0


def load_rows(db_path: Path | str, run_id: str | None) -> list[sqlite3.Row]:
    if not Path(db_path).exists():
        return []
    with connect(db_path) as conn:
        if run_id:
            return list(
                conn.execute(
                    "SELECT s.* FROM level_stat s JOIN task_item t ON t.item_id = s.item_id "
                    "WHERE t.run_id = ? ORDER BY s.kind, s.level_used",
                    (run_id,),
                ).fetchall()
            )
        return list(conn.execute("SELECT * FROM level_stat ORDER BY kind, level_used").fetchall())


def summarize(rows: list[sqlite3.Row]) -> dict[str, KindStats]:
    """按 ``kind`` 汇总：各级计数、总数、降级数、降级率。

    **降级 = ``level_used`` 不是该动作那条阶梯的第一级**（见 :func:`baseline_of`）。
    题目侧只有一级，因此永远记 0 —— 这是刻意的，不是漏判。
    """
    summary: dict[str, KindStats] = {}
    for row in rows:
        kind = str(row["kind"])
        bucket = summary.setdefault(kind, KindStats())
        level = str(row["level_used"])
        bucket.counts[level] += 1
        bucket.total += 1
        bucket.elapsed_ms += int(row["elapsed_ms"] or 0)
        if not row["ok"]:
            bucket.failed += 1
        if level != baseline_of(kind).value:
            bucket.degraded += 1
    return summary


def _rate(part: int, whole: int) -> str:
    if whole <= 0:
        return "—"
    return f"{part / whole * 100:.1f}%"


def render(rows: list[sqlite3.Row], *, db_path: Path | str, run_id: str | None) -> str:
    summary = summarize(rows)
    total = sum(bucket.total for bucket in summary.values())
    degraded = sum(bucket.degraded for bucket in summary.values())

    lines = [
        "# 降级热力图（M4-4）",
        "",
        "> **本文件由 `scripts/export_heatmap.py` 从 SQLite 的 `level_stat` 表生成，请勿手改。**",
        "> 重跑一次批处理之后，重新执行该脚本即可刷新。",
        "",
        f"- 数据源：`{Path(db_path).as_posix()}`" + (f"（run_id=`{run_id}`）" if run_id else ""),
        f"- 动作总数：**{total}**，其中离开自身常规路径的："
        f"**{degraded}**（{_rate(degraded, total)}）",
        "",
        "「离开常规路径」= `level_used` **不是该动作那条阶梯的第一级**。"
        "各类动作的基线不同，见下表「基线」列 —— 用一把尺子量所有动作是错的。",
        "",
        "> **v0.2.0 的读法（最容易读错的地方）**：题目侧"
        "（`select_option` / `submit` / `click` / `swipe`）只有一条路 ——"
        " 模型看截图给坐标（`L6_VISION_XY`），所以 **L6 就是它们的基线**，"
        " 记作 0% 是**正常的**，不代表没测到。真正值得盯的是**媒体动作**："
        " 它们大量离开各自的第一级，才说明媒体锚点漂了、或被遮罩挡住。",
        "",
    ]

    if not rows:
        lines += [
            "## 暂无数据",
            "",
            "跑一次批处理（`python scripts/run_batch.py --limit 50`）后重新导出即可。",
            "",
        ]
        return "\n".join(lines)

    # ---- 热力图 ---------------------------------------------------------- #
    header = [
        "动作类型",
        "基线",
        *[level.value.replace("l", "L").split("_")[0] for level in LEVELS],
        "合计",
        "离开路径",
    ]
    lines += ["## 动作 × 级别 分布", "", "| " + " | ".join(header) + " |"]
    lines.append("|" + "---|" * len(header))
    for kind in sorted(summary, key=lambda k: (-summary[k].degraded, k)):
        bucket = summary[kind]
        cells = [str(bucket.counts.get(level.value, 0)) for level in LEVELS]
        lines.append(
            "| `{}` | `{}` | {} | {} | {} |".format(
                kind,
                baseline_of(kind).value,
                " | ".join(cells),
                bucket.total,
                _rate(bucket.degraded, bucket.total),
            )
        )
    lines.append("")

    # ---- 结论 ------------------------------------------------------------ #
    ranked = sorted(
        summary.items(),
        key=lambda kv: (-kv[1].degraded / max(kv[1].total, 1), kv[0]),
    )
    lines += ["## 结论", ""]
    worst = [item for item in ranked if item[1].degraded > 0]
    if not worst:
        lines += [
            "- 所有动作都停在**各自的常规路径**上，不需要针对性加固。",
            "",
        ]
    else:
        lines += ["**最容易离开常规路径的控件类型（按其自身基线排序）**：", ""]
        for kind, bucket in worst[:5]:
            # 「最常落到哪一级」只看**非基线**级别：把基线算进来会写出
            # 「最容易降级的控件最常落在它自己的基线上」这种自相矛盾的结论
            baseline = baseline_of(kind).value
            heaviest = max(
                (
                    (level, bucket.counts.get(level.value, 0))
                    for level in LEVELS
                    if level.value != baseline
                ),
                key=lambda pair: pair[1],
            )[0]
            lines += [
                f"- `{kind}`（{KIND_LABELS.get(kind, '—')}）："
                f"基线 `{baseline}`，"
                f"{bucket.degraded}/{bucket.total} 次离开基线（"
                f"{_rate(bucket.degraded, bucket.total)}），"
                f"最常落到 **{heaviest.value}**",
            ]
        lines.append("")
        lines += [
            "> 处置建议：先看**媒体动作**为什么离开第一级 —— 多为锚点漂移或被遮罩挡住"
            "（`pause_media` 的第一级是 `L4_FOCUS_KEYS`，落在它是**设计如此**，"
            "因为弹题遮罩会盖住播放按钮）。"
            "题目侧落在 `L6_VISION_XY` 是 v0.2.0 的既定路径，不按「劣化」处置。",
            "",
        ]

    failed = {kind: bucket.failed for kind, bucket in summary.items() if bucket.failed}
    if failed:
        lines += ["## 失败动作", "", "| 动作 | 失败次数 |", "|---|---|"]
        lines += [f"| `{kind}` | {count} |" for kind, count in sorted(failed.items())]
        lines.append("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出降级热力图（M4-4）")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite 路径")
    parser.add_argument("--run", dest="run_id", default=None, help="只统计某一次运行")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出 markdown 路径")
    parser.add_argument("--check", action="store_true", help="只校验不写盘（CI 用）")
    args = parser.parse_args(argv)

    rows = load_rows(args.db, args.run_id)
    body = render(rows, db_path=args.db, run_id=args.run_id)

    if args.check:
        target = Path(args.out)
        if not target.exists():
            print(f"{target} 不存在，请先跑 export_heatmap.py", file=sys.stderr)
            return 1
        if target.read_text(encoding="utf-8") != body:
            print(f"{target} 与当前数据不一致，请重跑 export_heatmap.py", file=sys.stderr)
            return 1
        print(f"{target} 与当前数据一致")
        return 0

    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    print(f"已写出 {target}（{len(rows)} 条动作记录）")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
