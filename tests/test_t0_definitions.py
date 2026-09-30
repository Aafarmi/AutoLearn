"""T0 八项定义的「落为代码常量」验收。

这个文件的职责不是测算法，而是**卡住口径**：任何人偷偷改一个阈值、少一个
状态、漏一个事件，这里立刻红。
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from core import db, events
from core.config import MIN_SAMPLE_N, GuardThresholds, RateLimits
from core.enums import (
    MediaState,
    ProbeName,
    QuestionState,
    SolvePath,
    TaskType,
    TierUsed,
)
from core.qid import make_qid
from core.states import TRANSITIONS
from core.tasks import SuspendFrame, TaskStack
from core.vid import make_vid

HEX16 = re.compile(r"^[0-9a-f]{16}$")

DB_TABLE_COUNT = 6
#: 事件总数。P0 冻结 22 条；P2/P3 新增 4 条感知事件 → 26；
#: P14 的 2 条（``advance.scroll_probe`` / ``advance.last_question``）已随末题判定
#: 整套推倒而删除，换成 2 条新的（``advance.calibrated`` / ``advance.completion_check``）
#: → 28。
#:
#: 2026-09-29：``solve.escalated`` 随 Tier 分级删除 → 27；
#: 训练模式新增 ``training.done`` → **仍是 28**。
EVENT_COUNT = 28


# --------------------------------------------------------------------------- #
# 1. qid 算法
# --------------------------------------------------------------------------- #
def test_t0_1_qid_algorithm() -> None:
    qid = make_qid("题干", ["甲", "乙", "丙"])
    assert HEX16.match(qid), "qid 应为 sha1 前 16 位十六进制"
    assert make_qid("题干", ["丙", "甲", "乙"]) == qid, "选项排序后 qid 必须不变"


# --------------------------------------------------------------------------- #
# 2. 状态机
# --------------------------------------------------------------------------- #
def test_t0_2_state_machine_shapes() -> None:
    assert len(QuestionState) == 9
    assert set(TRANSITIONS) == set(QuestionState)
    assert QuestionState.SUBMITTED.value == "submitted"


# --------------------------------------------------------------------------- #
# 3 / 5 / 6. 阈值类定义全部落为具名配置项
# --------------------------------------------------------------------------- #
def test_t0_3_review_threshold_is_still_a_named_knob() -> None:
    """T0-3 的「一致率门限」仍在，但它不再触发「升级 Tier2」。

    Tier 分级删除后，它只用来判断「复算出来的结果分歧是不是大到该让人看一眼」。
    复算本身由用户在创建任务时显式决定（``RunConfig.recalculate``）。
    """
    g = GuardThresholds()
    assert g.agreement_accept == 0.8, "T0-3：一致率 ≥0.8 直接采用"
    assert "tier2_min_votes" not in GuardThresholds.model_fields, (
        "tier2_min_votes 是 Tier 分级的遗留，已随分级一起删除"
    )


def test_t0_5_readback_mismatch_disposal() -> None:
    g = GuardThresholds()
    assert g.click_replay_max == 3, "T0-5：重放 click ≤3 次"
    assert g.click_replay_gap_ms == (200, 400), "T0-5：重放间隔 200~400ms 随机"


def test_t0_6_m2_stop_loss_numbers() -> None:
    g = GuardThresholds()
    assert g.single_top1_min == 0.85, "T0-6：单选 Top-1 ≥0.85"
    assert g.multi_exact_min == 0.75, "T0-6：多选完全匹配 ≥0.75"
    assert g.valid_ratio_min == 0.80, "T0-6：一致率 ≥0.8 的题占比 ≥0.80"


def test_m4_2_rate_limits() -> None:
    r = RateLimits()
    assert r.click_gap_ms == (200, 600), "M4-2：click 间 200~600ms"
    assert r.submit_gap_s == (5, 15), "M4-2：提交间 5~15s"
    assert r.llm_concurrency_free == 2
    assert r.llm_concurrency_paid == 3
    assert r.llm_concurrency_free < r.llm_concurrency_paid


def test_thresholds_reject_illegal_values() -> None:
    with pytest.raises(ValueError):
        GuardThresholds(single_top1_min=1.5)
    with pytest.raises(ValueError):
        GuardThresholds(click_replay_gap_ms=(400, 200))
    with pytest.raises(ValueError):
        RateLimits(click_gap_ms=(-1, 10))


# --------------------------------------------------------------------------- #
# 7. vid 视频标识
# --------------------------------------------------------------------------- #
def test_t0_7_vid_algorithm() -> None:
    vid = make_vid("course-01", 3, "第三节 索引原理")
    assert HEX16.match(vid)
    assert make_vid("course-01", 3, "第三节 索引原理") == vid, "同一集必须稳定"
    assert make_vid("course-01", 4, "第三节 索引原理") != vid, "集数不同必须不同"
    assert make_vid("course-02", 3, "第三节 索引原理") != vid, "课程不同必须不同"
    assert make_vid("course-01", "3", "第三节 索引原理") == vid, "集数按整型归一"


def test_vid_never_collides_with_qid_namespace() -> None:
    """``vid`` 与 ``qid`` 是两套标识，不能互相顶替。"""
    vid = make_vid("c", 1, "t")
    assert HEX16.match(vid)


# --------------------------------------------------------------------------- #
# 8. 任务类型与中断模型
# --------------------------------------------------------------------------- #
def test_t0_8_task_types_and_interrupt_model() -> None:
    assert {t.value for t in TaskType} == {"video", "quiz"}
    assert {"idle", "playing", "paused", "interrupted", "resumed", "ended"} == {
        s.value for s in MediaState
    }

    fields = set(SuspendFrame.model_fields)
    assert fields == {
        "frame_id",
        "parent_item_id",
        "child_item_id",
        "media_state_at_suspend",
        "reason",
        "created_at",
    }
    for name in ("push", "pop", "peek", "snapshot", "restore"):
        assert hasattr(TaskStack(), name), f"TaskStack 缺 {name}"
    assert isinstance(TaskStack().depth, int)


# --------------------------------------------------------------------------- #
# 探针与档位枚举
# --------------------------------------------------------------------------- #
def test_probe_enums_frozen() -> None:
    """v0.2.0：题目通道只剩 ``vision``，外加不在题目链里的媒体读口 ``media``。

    ``net`` / ``dom`` 已随「只使用模型」一起删除 —— 它们的存在意味着
    页面还会被按文档结构解析一遍，而那正是本版删掉的东西。
    """
    assert {p.value for p in ProbeName} == {"vision", "media"}
    # 2026-09-29：Tier 分级删除 → 只剩「正常 / Mock / 缓存」三条来源。
    # ``TierUsed`` 作为**向后兼容别名**保留，但它的取值里不再有 tier1 / tier2。
    assert {t.value for t in SolvePath} == {"single", "mock", "cache"}
    assert TierUsed is SolvePath, "旧名仍可导入（老库 / 老留痕要读得回来）"
    assert SolvePath.parse("tier2") is SolvePath.SINGLE, "老库里的 tier1/tier2 兼容读成 single"
    assert MIN_SAMPLE_N == 1, "M2-2：默认每题只核对一次（n = 1）"


def test_recalculate_is_explicit_and_off_by_default() -> None:
    """复算是**用户在创建任务时的显式决定**，不是隐式升级。

    这条把三件事一起钉住：默认关、开了要至少 2 次、关着时次数不生效（由
    ``Solver._sample_count`` 强制成 1）。
    """
    from core.config import RunConfig

    assert RunConfig().recalculate is False
    assert RunConfig().sample_n == MIN_SAMPLE_N
    assert RunConfig(recalculate=True, sample_n=5).sample_n == 5
    with pytest.raises(ValueError):
        RunConfig(recalculate=True, sample_n=1)


def test_p14_end_probe_surface_is_gone() -> None:
    """P14 那套末题判定（进度正则 + 终点文案 + 滚到底）已整套推倒，不许回流。

    它被判定为严重逻辑错误的原因：推理链每一条都可能误判，而误判成「做完了」
    的代价是**静默跳掉后面所有题**。取而代之的是开局标定 + 收尾问视觉组。
    """
    from core.enums import AdvanceMethod, ErrorCode

    for knobs in (
        "advance_scroll_probe",
        "advance_scroll_settle_polls",
        "end_vision_check",
        "end_progress_patterns",
        "end_progress_max_total",
        "end_marker_texts",
        "end_unknown_action",
        "end_decision_timeout_s",
    ):
        assert knobs not in GuardThresholds.model_fields, f"P14 的 {knobs} 不该回来"

    assert not hasattr(ErrorCode, "LAST_QUESTION_UNCERTAIN")
    assert ErrorCode.ADVANCE_FAILED.value == "advance_failed"
    # 2026-09-30：``AdvanceMethod`` 新增 ``card``（点答题卡题号格）——
    # 推进方式集合是**冻结契约**，加一个取值就必须同步这里，否则这条守卫白设。
    assert {m.value for m in AdvanceMethod} == {"click", "card", "swipe", "scroll", "unknown"}
    assert "run_flag" not in db.SCHEMA_SQL, "run_flag 表已随末题判定删除"


def test_media_probe_is_not_in_the_question_chain() -> None:
    """``media`` 是探针名，但**不属于题目链** —— 视频流程单独调度。

    v0.2.0：``PROBE_CHAINS`` 已删除（只剩一条通道，没有「展开表」可言），
    改为直接断言 ``active_probe_chain()`` 的内容。
    """
    from core.config import RunConfig, active_probe_chain

    chain = active_probe_chain(RunConfig())
    assert chain == [ProbeName.VISION]
    assert ProbeName.MEDIA not in chain


# --------------------------------------------------------------------------- #
# 契约表结构 / 事件清单
# --------------------------------------------------------------------------- #
def test_db_schema_has_six_tables() -> None:
    """建表清单守卫。P14 的 ``run_flag``（「这是不是最后一题」的信箱）已随
    末题判定整套推倒而删除 → 回到 **6** 张。

    函数名里的 ``six`` 是历史名字，故意不改 —— 它会出现在测试报告里，
    改名字会让历史记录对不上；数字以 :data:`DB_TABLE_COUNT` 为准。
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA_SQL)
    names = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    expected = {
        "run",
        "task_item",
        "answer",
        "suspend_frame",
        "level_stat",
        "media_position",
        # 加新表必须在这里登记 —— 这条断言的用途就是「不漏表」。
    }
    assert expected <= names, f"缺表：{expected - names}"
    assert len(expected) == DB_TABLE_COUNT
    conn.close()


def test_db_init_is_idempotent(tmp_path) -> None:
    target = tmp_path / "nested" / "autolearn.db"
    db.init_db(target).close()
    db.init_db(target).close()
    assert target.exists()


def test_event_names_are_point_separated_and_complete() -> None:
    assert len(events.ALL_EVENTS) == EVENT_COUNT, (
        f"事件数变了（{len(events.ALL_EVENTS)} != {EVENT_COUNT}），"
        "新增事件必须同步规划书 §2.5 与 CHANGELOG"
    )
    for name in events.ALL_EVENTS:
        assert re.fullmatch(r"[a-z]+\.[a-z_]+", name), f"{name} 不符合 <domain>.<object> 规则"
    assert events.is_known_event("media.interrupt_detected")
    assert not events.is_known_event("media.interrupt")  # 拼错的名字必须被识破
    # 2026-09-29：Tier 升级事件随分级删除；训练模式补了一条新事件。
    assert not events.is_known_event("solve.escalated"), "Tier 升级事件已删除，不许回流"
    assert events.is_known_event("training.done")
