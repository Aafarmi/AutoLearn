"""批处理驱动（M4-1 / M4-2 / M4-3 的验收工具）。

按真实链路跑一遍题目靶场：感知 → 求解 → 执行 → 提交 → 校验，全程落 SQLite
与 ``logs/``。没有模型配置时自动落 ``MockProvider``（任务书 §3.2），
所以**零密钥就能把整条链路跑完**。

用法::

    # 先起靶场
    python scripts/serve_mock.py

    # 跑一遍题库（全自动、无模型配置 → MockProvider）
    python scripts/run_batch.py --limit 50

    # 之后导出降级热力图
    python scripts/export_heatmap.py

与 UI 的关系：本脚本与 ``POST /api/run/start`` 走的是**同一套装配**
（``ui.assembly.build_run_deps``），差别只在浏览器由谁开、进度怎么看。
跑批是验收与排查用的，日常使用走界面。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # 允许 `python scripts/run_batch.py` 直接跑
    sys.path.insert(0, str(ROOT))

from core.config import RunConfig  # noqa: E402
from core.enums import QuestionState  # noqa: E402
from core.events import Event  # noqa: E402
from core.model_registry import DEFAULT_MODELS_PATH, ModelRegistry  # noqa: E402
from core.orchestrator import Orchestrator, RunContext  # noqa: E402
from core.trace import EventBus  # noqa: E402
from ui.assembly import DEFAULT_START_URL, build_run_deps  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AutoLearn 批处理驱动")
    parser.add_argument("--url", default=DEFAULT_START_URL, help="起始 URL（靶场题目页）")
    parser.add_argument("--limit", type=int, default=50, help="最多跑多少题")
    parser.add_argument("--run-id", default=None, help="运行 ID（默认随机）")
    parser.add_argument("--browser", default="msedge", help="系统浏览器通道：msedge / chrome")
    parser.add_argument("--db", default=None, help="SQLite 路径（默认 state/autolearn.db）")
    parser.add_argument("--logs", default=None, help="留痕根目录（默认 logs/）")
    parser.add_argument("--models", default=str(DEFAULT_MODELS_PATH), help="models.yaml 路径")
    parser.add_argument("--manual", action="store_true", help="半自动：每题停下等人确认")
    parser.add_argument(
        "--skip-review",
        action="store_true",
        help="遇到 ⚠复核 必停的题时标记为 skipped 并继续（**无人值守跑批用**，生产别开）",
    )
    parser.add_argument("--quiet", action="store_true", help="不打事件流")
    return parser.parse_args(argv)


async def run_batch(args: argparse.Namespace) -> int:
    run_id = args.run_id or uuid.uuid4().hex[:12]
    cfg = RunConfig(
        auto_apply=not args.manual,
        storage_state_path=Path("state/storage_state.json"),
    )

    registry = ModelRegistry(path=Path(args.models))
    registry.load()
    bus = EventBus()

    if not args.quiet:
        stream = bus.subscribe()

        async def echo() -> None:
            async for name, payload in stream:
                if name in {Event.TASK_STATE_CHANGED, Event.TASK_NEEDS_CONFIRM}:
                    print(f"  [{name}] {payload}")

        echo_task = asyncio.create_task(echo())
    else:
        echo_task = None

    deps = build_run_deps(
        cfg=cfg,
        registry=registry,
        bus=bus,
        run_id=run_id,
        start_url=args.url,
        logs=Path(args.logs) if args.logs else None,
        db_path=Path(args.db) if args.db else None,
    )
    deps.browser_channel = args.browser

    ctx = RunContext(run_id=run_id, cfg=cfg, started_at=datetime.now(UTC))
    orchestrator = Orchestrator(ctx, deps=deps)

    print(f"run_id={run_id}  起始={args.url}  最多 {args.limit} 题")
    print(f"模式={'半自动（每题确认）' if args.manual else '全自动'}")
    print(f"模型配置：{len(registry.active_chain())} 套"
          f"{'（无 → MockProvider）' if not registry.active_chain() else ''}")

    if echo_task is not None:
        # 只跑 limit 题：靠「每完成一题就检查」来收口，而不是改编排层
        await _run_limited(
            orchestrator, args.limit, skip_review=args.skip_review, conn=deps.conn
        )
        echo_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await echo_task
        await stream.aclose()
    else:
        await _run_limited(
            orchestrator, args.limit, skip_review=args.skip_review, conn=deps.conn
        )

    report(run_id, Path(args.db) if args.db else None)
    return 0


async def _run_limited(
    orchestrator: Orchestrator,
    limit: int,
    *,
    skip_review: bool = False,
    conn=None,
) -> None:
    """跑 ``limit`` 题，并盯住两种「运行停下来了」的情形。

    1. **跑到量了** → ``stop()``。编排层是「一直点到没有下一题」的，
       限量不该写成编排层的参数，那是脚本自己的事。
    2. **停在 ⚠复核 等人确认**（T0-3 必停）→ ``--skip-review`` 时把这题记成
       ``skipped`` 再恢复。

       .. warning::
          这不是「自动批准」—— 批准会让一个**没有把握的答案**被真提交。
          标记 ``skipped`` 表示「这题没做」，是最保守的处置。
          无人值守跑批可以这么干；生产环境请让人来看。
    """
    async def supervise() -> None:
        while not orchestrator.stopped:
            if limit > 0 and _finished(orchestrator) >= limit:
                await orchestrator.stop()
                return
            if skip_review and orchestrator.paused_by == "needs_confirm" and conn is not None:
                _skip_pending_confirms(orchestrator, conn)
                await orchestrator.resume()
            await asyncio.sleep(0.2)

    watcher = asyncio.create_task(supervise())
    try:
        await orchestrator.run()
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


def _finished(orchestrator: Orchestrator) -> int:
    return sum(
        1
        for item in orchestrator.items
        if QuestionState(item.state)
        in {QuestionState.VERIFIED, QuestionState.FAILED, QuestionState.SKIPPED}
    )


def _skip_pending_confirms(orchestrator: Orchestrator, conn) -> None:
    """把仍在等人的题记成 ``skipped``（人工「否决」的等价物）。"""
    for item in orchestrator.items:
        if QuestionState(item.state) is QuestionState.PENDING_CONFIRM:
            conn.execute(
                "UPDATE task_item SET state = ? WHERE item_id = ?",
                (QuestionState.SKIPPED.value, item.item_id),
            )
    conn.commit()


def report(run_id: str, db_path: Path | None) -> None:
    """跑完后从库里重新读一份汇总。

    **另开连接**：编排层收尾时会关掉自己那条，复用会拿到「已关闭」的库。
    """
    from core import db
    from ui.store import db_file

    target = db_path if db_path is not None else db_file()
    if not Path(target).exists():
        print("\n（没有找到库文件，本次没有任何落盘）")
        return
    with db.connect(target) as conn:
        items = db.load_task_items(conn, run_id)
        stats = db.load_level_stats(conn, run_id)

    counts: dict[str, int] = {}
    for item in items:
        key = str(item.state)
        counts[key] = counts.get(key, 0) + 1
    degraded = sum(1 for row in stats if row["level_used"] != "l1_locator")

    print("\n—— 本次运行 ——")
    print(f"题目数：{len(items)}")
    for state, count in sorted(counts.items()):
        print(f"  {state:<16} {count}")
    print(f"动作记录：{len(stats)}（其中降级 {degraded}）")
    print(f"留痕目录：logs/{run_id}/")
    print(f"降级热力图：python scripts/export_heatmap.py --run {run_id}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # 编排层的「落库失败」是 warning 级 —— 不打开就看不见，等于又静默了一次
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(run_batch(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
