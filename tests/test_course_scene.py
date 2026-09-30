"""P8 网课场景真浏览器验收（M5-2 ~ M5-5）。

内存用例（``tests/test_video_scene.py``）证明的是**编排逻辑**；这里证明的是
「在真页面上确实有效」—— 媒体态读得对、弹窗真的会挡住点击、位置真的能连上。

靶场不可达时整文件 ``skip``（``conftest.mock_base`` 会给启动指引，**不静默通过**）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core import db
from core.config import RunConfig
from core.enums import MediaState, TaskType
from core.model_registry import ModelRegistry
from core.orchestrator import Orchestrator, RunContext
from perception.media_probe import read_episode_catalog
from tests.helpers import course_url
from ui.assembly import build_run_deps

RUN_ID = "course-scene"


async def _boot(page, url: str) -> None:
    """打开网课页并等第 1 集真的挂上（靶场 boot 是异步的，早读会读到空源）。"""
    await page.goto(url, wait_until="domcontentloaded", timeout=15000)
    await page.wait_for_function(
        "() => document.body.getAttribute('data-current-episode') === '1'", timeout=8000
    )


async def _quick() -> None:
    """把限速钩子换成空操作：两条限速本身由 ``test_ratelimit.py`` 卡着，
    这里只关心网课流程，不必每题真睡 5~15 秒。"""
    return


def _orchestrator(tmp_path: Path, page, url: str, *, cfg: RunConfig) -> tuple[Orchestrator, dict]:
    registry = ModelRegistry(path=tmp_path / "models.yaml")
    registry.load()
    run_id = f"{RUN_ID}-{abs(hash(url)) % 10000}"
    deps = build_run_deps(
        cfg=cfg,
        registry=registry,
        bus=None,
        run_id=run_id,
        start_url=url,
        logs=tmp_path / "logs",
        db_path=tmp_path / "autolearn.db",
    )
    # 注入测试自己的页面：``run()`` 看到 ``deps.page`` 已就绪就不会再开浏览器
    deps.page = page
    deps.click_gap = _quick
    deps.submit_gap = _quick
    ctx = RunContext(run_id=run_id, cfg=cfg, started_at=datetime.now(UTC))
    return Orchestrator(ctx, deps=deps), {"run_id": run_id, "conn": deps.conn}


def _video_states(conn, run_id: str) -> dict[str, str]:
    return {
        item.vid or item.item_id: str(item.state)
        for item in db.load_task_items(conn, run_id)
        if item.type is TaskType.VIDEO
    }


# --------------------------------------------------------------------------- #
# 分集目录（M5-1）
# --------------------------------------------------------------------------- #
async def test_read_episode_catalog_matches_course_json(
    page, mock_base, adapter, course_meta
) -> None:
    await _boot(page, course_url(mock_base))

    catalog = await read_episode_catalog(page, adapter)

    assert [ep.episode_index for ep in catalog] == [
        ep["episode_index"] for ep in course_meta["episodes"]
    ]
    assert [ep.vid for ep in catalog] == [ep["vid"] for ep in course_meta["episodes"]]
    assert all(ep.title for ep in catalog)
    assert all(ep.duration > 0 for ep in catalog)


# --------------------------------------------------------------------------- #
# 连续播完全部分集（M5-4 / M5-5）
# --------------------------------------------------------------------------- #
@pytest.mark.slow
async def test_course_scene_plays_every_episode(tmp_path: Path, page, mock_base) -> None:
    url = course_url(mock_base, dur=2)
    await _boot(page, url)
    cfg = RunConfig(task_sequence=[TaskType.VIDEO], auto_apply=True)
    orchestrator, env = _orchestrator(tmp_path, page, url, cfg=cfg)

    await orchestrator.run()

    assert orchestrator.paused is False, f"不该停下：{orchestrator.paused_by}"
    states = _video_states(env["conn"], env["run_id"])
    assert states, "一集都没入库"
    assert set(states.values()) == {MediaState.ENDED.value}, states


# --------------------------------------------------------------------------- #
# 弹题打断 → **显式暂停媒体** → 没有视觉模型时如实停下（M5-2）
# --------------------------------------------------------------------------- #
@pytest.mark.slow
async def test_interrupt_pauses_the_media_then_stops_honestly(
    tmp_path: Path, page, mock_base
) -> None:
    """弹题到来 → **先显式暂停媒体**，然后带具体原因停下。

    v0.2.0 起弹题也要**交给模型读**（题目通道只剩视觉一条），而本用例的模型链是
    Mock —— 它答不了「读图」这个契约，所以弹题读不出来。此时正确的行为有两条，
    正是这里要钉的：

    1. **挂起纪律**：弹窗自己不会暂停视频，必须由我们显式暂停
       （少了这一步，挂起期间 ``currentTime`` 会继续往前走）；
    2. **不静默继续**：读不出弹题就用具体原因停下（``vision_read_failed``），
       而不是假装处理完了、把视频接着播下去。

    「恢复位置连续（Δt ≤ 2s）」那条断言需要弹题被真的答完，它由
    ``tests/test_video_scene.py``（内存替身）与 ``tests/test_verifier.py``
    覆盖 —— 真机上的弹题作答要有真正的视觉模型，见 ``docs/测试与验收`` 的口径。
    """
    url = course_url(mock_base, dur=10, interrupt_at=3)
    await _boot(page, url)
    cfg = RunConfig(task_sequence=[TaskType.VIDEO], auto_apply=True)
    orchestrator, env = _orchestrator(tmp_path, page, url, cfg=cfg)

    await orchestrator.run()

    # 弹题路径**复用**读题失败的具体理由（``_vision_gate_reason``），
    # 只有它为空时才退回兜底的 ``interrupt_perception_failed``。
    # 2026-09-28 起读题失败会给出一句具体的 ``vision_read_failed``，
    # 所以这里断言的是它 —— 比兜底文案更有利于排查。
    assert orchestrator.paused_by == "vision_read_failed", orchestrator.paused_by

    item = next(
        i for i in db.load_task_items(env["conn"], env["run_id"]) if i.type is TaskType.VIDEO
    )
    assert str(item.state) == MediaState.INTERRUPTED.value

    action = tmp_path / "logs" / env["run_id"] / item.item_id / "action.json"
    assert action.is_file(), "挂起时的媒体动作必须留痕"
    payload = json.loads(action.read_text(encoding="utf-8"))
    assert payload["kind"] == "pause_media", payload
    assert payload["ok"] is True, payload


# --------------------------------------------------------------------------- #
# ended 与弹题同刻 → ended 优先（M5-2）
# --------------------------------------------------------------------------- #
async def test_ended_and_interrupt_at_the_same_moment(tmp_path: Path, page, mock_base) -> None:
    url = course_url(mock_base, dur=4, interrupt_at="end")
    await _boot(page, url)
    cfg = RunConfig(task_sequence=[TaskType.VIDEO], auto_apply=True)
    orchestrator, env = _orchestrator(tmp_path, page, url, cfg=cfg)

    await orchestrator.run()

    # 这一集要记成已完成 —— 弹题被处理了，但不恢复播放
    closed = tmp_path / "logs" / env["run_id"]
    video_dir = next((d for d in closed.iterdir() if (d / "perception.json").is_file()), None)
    assert video_dir is not None
    assert (video_dir / "after.png").is_file() or (video_dir / "before.png").is_file()

    states = _video_states(env["conn"], env["run_id"])
    first = next(iter(states.values()))
    assert first in {MediaState.ENDED.value, MediaState.INTERRUPTED.value}


