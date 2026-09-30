"""网课场景批处理驱动（M5-1 ~ M5-5 的验收工具）。

按真实链路跑一遍**网课靶场**：分集目录 → 逐集播放 → 弹题打断（压栈/弹栈）
→ 播完推进下一集，全程落 SQLite 与 ``logs/``。没有模型配置时自动落
``MockProvider``（任务书 §3.2），所以**零密钥就能把整条链路跑完**。

用法::

    # 先起靶场
    python scripts/serve_mock.py

    # 快跑：每集 8 秒、第 3 秒弹题
    python scripts/run_course.py --dur 8 --interrupt-at 3

    # 只跑前 2 集（验收常用）
    python scripts/run_course.py --dur 6 --limit 2

    # 断点续跑：杀掉进程后再用同一个 run_id 起一次，已播完的集不再重播
    python scripts/run_course.py --dur 6 --run-id drill1
    python scripts/run_course.py --dur 6 --run-id drill1

与 UI 的关系：本脚本与 ``POST /api/run/start``（``task_sequence=[video]``）走的是
**同一套装配**（``ui.assembly.build_run_deps``），差别只在浏览器由谁开、进度怎么看。
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
if str(ROOT) not in sys.path:  # 允许 `python scripts/run_course.py` 直接跑
    sys.path.insert(0, str(ROOT))

from core.config import RunConfig  # noqa: E402
from core.enums import MediaState, TaskType  # noqa: E402
from core.events import Event  # noqa: E402
from core.model_registry import DEFAULT_MODELS_PATH, ModelRegistry  # noqa: E402
from core.orchestrator import Orchestrator, RunContext  # noqa: E402
from core.trace import EventBus  # noqa: E402
from ui.assembly import COURSE_START_URL, build_run_deps  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AutoLearn 网课场景批处理")
    parser.add_argument("--run-id", default=None, help="运行 ID（默认随机；相同即续跑）")
    parser.add_argument("--limit", type=int, default=0, help="最多播几集（0 = 全部）")
    parser.add_argument("--dur", type=int, default=0, help="覆盖每集时长（秒），0 = 用 course.json")
    parser.add_argument(
        "--interrupt-at",
        default="0",
        help="弹题时机：秒数 / end（与 ended 同刻）/ 0（不弹题）",
    )
    parser.add_argument("--browser", default="msedge", help="系统浏览器通道：msedge / chrome")
    parser.add_argument("--db", default=None, help="SQLite 路径（默认 state/autolearn.db）")
    parser.add_argument("--logs", default=None, help="留痕根目录（默认 logs/）")
    parser.add_argument("--models", default=str(DEFAULT_MODELS_PATH), help="models.yaml 路径")
    parser.add_argument("--manual", action="store_true", help="半自动：弹题停下等人确认")
    parser.add_argument("--quiet", action="store_true", help="不打事件流")
    return parser.parse_args(argv)


def course_start_url(args: argparse.Namespace) -> str:
    params = []
    if args.dur:
        params.append(f"dur={args.dur}")
    if args.interrupt_at and args.interrupt_at != "0":
        params.append(f"interrupt_at={args.interrupt_at}")
    return COURSE_START_URL + ("?" + "&".join(params) if params else "")


async def run_course(args: argparse.Namespace) -> int:
    run_id = args.run_id or uuid.uuid4().hex[:12]
    cfg = RunConfig(
        task_sequence=[TaskType.VIDEO],
        auto_apply=not args.manual,
    )

    registry = ModelRegistry(path=Path(args.models))
    registry.load()
    bus = EventBus()

    echo_task = None
    if not args.quiet:
        stream = bus.subscribe()

        async def echo() -> None:
            async for name, payload in stream:
                if name in {
                    Event.MEDIA_STATE_CHANGED,
                    Event.MEDIA_INTERRUPT_DETECTED,
                    Event.STACK_PUSHED,
                    Event.STACK_POPPED,
                    Event.RUN_PAUSED,
                }:
                    print(f"  [{name}] {payload}")

        echo_task = asyncio.create_task(echo())

    start_url = course_start_url(args)
    deps = build_run_deps(
        cfg=cfg,
        registry=registry,
        bus=bus,
        run_id=run_id,
        start_url=start_url,
        logs=Path(args.logs) if args.logs else None,
        db_path=Path(args.db) if args.db else None,
    )
    deps.browser_channel = args.browser

    ctx = RunContext(run_id=run_id, cfg=cfg, started_at=datetime.now(UTC))
    orchestrator = Orchestrator(ctx, deps=deps)

    print(f"run_id={run_id}")
    print(f"起始={start_url}")
    print(f"模式={'半自动（弹题等人确认）' if args.manual else '全自动'}")

    try:
        await _run_limited(orchestrator, args.limit, conn=deps.conn)
    finally:
        if echo_task is not None:
            echo_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await echo_task
            await stream.aclose()

    report(run_id, Path(args.db) if args.db else None)
    return 0 if not orchestrator.paused else 2


async def _run_limited(orchestrator: Orchestrator, limit: int, *, conn=None) -> None:
    """跑 ``limit`` 集。限量是**脚本自己的事**，不改编排层。"""

    async def supervise() -> None:
        while not orchestrator.stopped:
            if limit > 0 and _ended(orchestrator) >= limit:
                await orchestrator.stop()
                return
            if orchestrator.paused:
                # 必停分支：脚本不替人做决定，打个招呼就收工（退出码 2）
                return
            await asyncio.sleep(0.2)

    watcher = asyncio.create_task(supervise())
    try:
        await orchestrator.run()
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


def _ended(orchestrator: Orchestrator) -> int:
    """已播完的分集数。**只数 video 条目** —— 弹题子任务在同一个队列里。"""
    return sum(
        1
        for item in orchestrator.items
        if item.type is TaskType.VIDEO and MediaState(item.state) is MediaState.ENDED
    )


def report(run_id: str, db_path: Path | None) -> None:
    """跑完后从库里重新读一份汇总（**另开连接**：编排层会关掉自己那条）。"""
    from core import db
    from ui.store import db_file

    target = db_path if db_path is not None else db_file()
    if not Path(target).exists():
        print("\n（没有找到库文件，本次没有任何落盘）")
        return
    with db.connect(target) as conn:
        items = [item for item in db.load_task_items(conn, run_id) if item.vid]
        frames = db.load_suspend_frames(conn, run_id)
        vids = {item.vid for item in items}
        # 分集序号按 run 内 video 条目的顺序推（与 ui/store.py 同一口径）
        positions = {
            (row["vid"], row["episode_index"]): row["last_position"]
            for row in conn.execute(
                "SELECT vid, episode_index, last_position FROM media_position"
            )
            if row["vid"] in vids
        }

    counts: dict[str, int] = {}
    for item in items:
        counts[str(item.state)] = counts.get(str(item.state), 0) + 1

    print("\n—— 本次运行（网课）——")
    print(f"分集数：{len(items)}")
    for state, count in sorted(counts.items()):
        print(f"  {state:<12} {count}")
    print(f"挂起栈残留：{len(frames)} 帧")
    for index, item in enumerate(items, start=1):
        saved = positions.get((item.vid, index))
        print(f"  第 {index} 集  {item.vid}  {item.state:<10} 末次位置={saved}")
    print(f"留痕目录：logs/{run_id}/")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(run_course(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
