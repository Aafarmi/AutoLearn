"""M4-4 降级热力图：**能不能定位最易离开常规路径的控件类型**。

v0.2.0 起「降级」的判据**按动作类型分别取基线**（= 该动作那条阶梯的第一级）：
题目侧只有一条路（模型给坐标 → ``L6_VISION_XY``），所以它们的基线就是 L6，
记 0% 是正常的；媒体动作各有各的第一级（播放 L1 / 暂停 L4 / seek L2）。

这里用**合成数据**把排序、比例、基线判定、失败动作、空数据五件事钉住 ——
真机跑批的数据里题目动作全在 L6，光靠它证明不了「排序与定位」能力。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from export_heatmap import LEVELS, baseline_of, load_rows, render, summarize

from core import db
from core.enums import ActionKind, ActLevel, QuestionState, TaskType


def seed(
    conn,
    *,
    run_id: str = "r1",
    item_id: str,
    kind: str,
    level: ActLevel,
    ok: bool = True,
) -> None:
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO task_item "
        "(item_id, run_id, type, qid, state, attempts, suspended, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?)",
        (item_id, run_id, TaskType.QUIZ.value, item_id, QuestionState.VERIFIED.value, now, now),
    )
    db.record_level_stat(
        conn,
        item_id=item_id,
        kind=kind,
        level_used=level.value,
        ok=ok,
        elapsed_ms=10,
    )


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "heat.db"


# --------------------------------------------------------------------------- #
# 基线：按动作类型分别取，不是一把尺子
# --------------------------------------------------------------------------- #
def test_baseline_is_the_first_rung_of_each_action_s_own_ladder() -> None:
    """**这条是 v0.2.0 的核心口径**：用一把尺子量所有动作，结论必然是错的。"""
    # 题目侧：模型给坐标是唯一路径，基线就是 L6
    for kind in ("select_option", "submit", "click", "swipe"):
        assert baseline_of(kind) is ActLevel.L6_VISION_XY, kind
    # 媒体侧：三条阶梯的第一级刻意不同（遮罩 / 精确落点各自的原因）
    assert baseline_of("play_media") is ActLevel.L1_LOCATOR
    assert baseline_of("next_episode") is ActLevel.L1_LOCATOR
    assert baseline_of("pause_media") is ActLevel.L4_FOCUS_KEYS
    assert baseline_of("seek_media") is ActLevel.L2_FORCE


def test_question_actions_on_l6_are_not_counted_as_degraded(db_path: Path) -> None:
    """题目动作全落 L6 是**既定路径**，不许记成降级。

    回归守卫：改回「不是 L1 就算降级」的话，这条立刻红 ——
    而那正是这张表最容易变成「一句恒真的废话」的地方。
    """
    conn = db.init_db(db_path)
    for index in range(3):
        seed(conn, item_id=f"q{index}", kind="select_option", level=ActLevel.L6_VISION_XY)
    seed(conn, item_id="s1", kind="submit", level=ActLevel.L6_VISION_XY)

    summary = summarize(load_rows(db_path, "r1"))

    assert summary["select_option"].total == 3
    assert summary["select_option"].degraded == 0
    assert summary["submit"].degraded == 0


def test_media_pause_on_its_own_baseline_is_not_degraded(db_path: Path) -> None:
    """``pause_media`` 的第一级是 L4（弹题遮罩会盖住播放按钮），落在它不算降级。"""
    conn = db.init_db(db_path)
    seed(conn, item_id="p1", kind="pause_media", level=ActLevel.L4_FOCUS_KEYS)

    assert summarize(load_rows(db_path, "r1"))["pause_media"].degraded == 0


def test_healthy_run_reports_no_degradation(db_path: Path) -> None:
    conn = db.init_db(db_path)
    for index in range(3):
        seed(conn, item_id=f"i{index}", kind="select_option", level=ActLevel.L6_VISION_XY)

    body = render(load_rows(db_path, "r1"), db_path=db_path, run_id="r1")

    assert "各自的常规路径" in body
    # 基线列 + 六级计数 + 合计 + 离开路径
    assert "| `select_option` | `l6_vision_xy` | 0 | 0 | 0 | 0 | 0 | 3 | 3 | 0.0% |" in body


# --------------------------------------------------------------------------- #
# 排序与定位
# --------------------------------------------------------------------------- #
def test_media_action_falling_off_its_ladder_is_ranked_first(db_path: Path) -> None:
    """一路掉到后面的媒体动作必须排在结论第一条 —— 这就是热力图的用途。"""
    conn = db.init_db(db_path)
    # play_media：一半退到 L5 坐标点击（基线是 L1）
    for index in range(4):
        seed(conn, item_id=f"a{index}", kind="play_media", level=ActLevel.L1_LOCATOR)
    for index in range(4):
        seed(conn, item_id=f"b{index}", kind="play_media", level=ActLevel.L5_BBOX)
    # 题目侧常规路径，不该被算成降级，也不该排在前面
    for index in range(8):
        seed(conn, item_id=f"c{index}", kind="submit", level=ActLevel.L6_VISION_XY)

    body = render(load_rows(db_path, "r1"), db_path=db_path, run_id="r1")
    conclusion = body.split("## 结论")[1]

    first_line = next(line for line in conclusion.splitlines() if line.startswith("- `"))
    assert "`play_media`" in first_line, "降级的是媒体动作，排在第一条的必须是它"
    assert "基线 `l1_locator`" in first_line
    assert "4/8 次离开基线（50.0%）" in first_line
    assert "l5_bbox" in first_line, "要指出最常落到哪一级（只看非基线级别）"


def test_failed_actions_are_called_out(db_path: Path) -> None:
    conn = db.init_db(db_path)
    seed(conn, item_id="f1", kind="submit", level=ActLevel.L6_VISION_XY, ok=False)

    body = render(load_rows(db_path, "r1"), db_path=db_path, run_id="r1")

    assert "## 失败动作" in body
    assert "| `submit` | 1 |" in body


def test_run_filter_excludes_other_runs(db_path: Path) -> None:
    conn = db.init_db(db_path)
    seed(conn, run_id="r1", item_id="i1", kind="play_media", level=ActLevel.L1_LOCATOR)
    seed(conn, run_id="r2", item_id="i2", kind="play_media", level=ActLevel.L5_BBOX)

    assert len(load_rows(db_path, "r1")) == 1
    assert summarize(load_rows(db_path, "r1"))["play_media"].degraded == 0

    everything = summarize(load_rows(db_path, None))
    assert everything["play_media"].total == 2
    assert everything["play_media"].degraded == 1


def test_empty_database_says_so_instead_of_crashing(db_path: Path) -> None:
    db.init_db(db_path)
    body = render(load_rows(db_path, None), db_path=db_path, run_id=None)
    assert "## 暂无数据" in body


def test_missing_database_is_not_an_error(tmp_path: Path) -> None:
    assert load_rows(tmp_path / "nope.db", None) == []


def test_levels_cover_the_whole_ladder() -> None:
    """列必须覆盖六级，少一级就会把动作算丢。"""
    assert [level.value for level in LEVELS] == [
        "l1_locator",
        "l2_force",
        "l3_scroll",
        "l4_focus_keys",
        "l5_bbox",
        "l6_vision_xy",
    ]


def test_every_action_kind_has_a_ladder() -> None:
    """``ActionKind`` 每新增一个成员，都必须在这张表里登记一条阶梯。

    漏登记会静默退回媒体锚点路径，于是新动作的降级率从一开始就是错的 ——
    这种错不会以异常的形式出现，只会让报告说谎。
    """
    from export_heatmap import LADDER_BY_KIND

    missing = sorted({kind.value for kind in ActionKind} - set(LADDER_BY_KIND))
    assert not missing, f"这些动作没有登记阶梯：{missing}"
