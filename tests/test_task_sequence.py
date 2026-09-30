"""P8 / M5-1：多任务类型队列组装（``core.tasks.build_task_sequence``）。

纯内存、零浏览器。这一条要天天跑 —— 「分集目录被正确展开成队列」是网课场景的
地基，坏了之后症状是「一集都不跑」，而不是报错。
"""

from __future__ import annotations

from core.config import RunConfig
from core.enums import MediaState, TaskType
from core.tasks import build_task_sequence
from tests.orchestrator_helpers import make_episode

EPISODES = [make_episode(1), make_episode(2), make_episode(3)]


def _cfg(*sequence: TaskType) -> RunConfig:
    return RunConfig(task_sequence=list(sequence))


def test_video_step_expands_every_episode() -> None:
    items = build_task_sequence(_cfg(TaskType.VIDEO), run_id="r1", episodes=EPISODES)

    assert [item.vid for item in items] == ["vid01", "vid02", "vid03"]
    assert all(item.type is TaskType.VIDEO for item in items)
    # 新排出来的条目一律从 idle 起跑
    assert all(item.state is MediaState.IDLE for item in items)
    assert all(item.qid is None for item in items)
    assert all(item.attempts == 0 and item.suspended is False for item in items)


def test_item_id_is_run_scoped() -> None:
    """``item_id`` 必须带 ``run_id`` 前缀。

    ``task_item.item_id`` 是全局主键，而 ``vid`` 是课程身份 —— 同一门课重跑一次
    若沿用裸 ``vid`` 当主键，就会重演 P7 那个「新运行一条任务都没有」的缺陷。
    """
    items = build_task_sequence(_cfg(TaskType.VIDEO), run_id="run-a", episodes=EPISODES)
    assert items[0].item_id == "run-a-vid01"

    again = build_task_sequence(_cfg(TaskType.VIDEO), run_id="run-b", episodes=EPISODES)
    assert again[0].item_id == "run-b-vid01"
    assert {item.item_id for item in items}.isdisjoint({item.item_id for item in again})


def test_only_video_steps_contribute_items() -> None:
    """``quiz`` 步不入序列 —— 题目身份读过页面才知道，没法预先排。"""
    quiz_only = build_task_sequence(_cfg(TaskType.QUIZ), run_id="r1", episodes=EPISODES)
    assert quiz_only == []

    mixed = build_task_sequence(
        _cfg(TaskType.QUIZ, TaskType.VIDEO), run_id="r1", episodes=EPISODES
    )
    assert [item.vid for item in mixed] == ["vid01", "vid02", "vid03"]


def test_video_without_catalog_is_empty_not_a_crash() -> None:
    """分集目录拿不到时返回空表，由调用方决定「暂停留档」，而不是在这里抛。"""
    assert build_task_sequence(_cfg(TaskType.VIDEO), run_id="r1", episodes=None) == []
    assert build_task_sequence(_cfg(TaskType.VIDEO), run_id="r1", episodes=[]) == []


def test_order_follows_the_catalog() -> None:
    shuffled = [make_episode(3), make_episode(1), make_episode(2)]
    items = build_task_sequence(_cfg(TaskType.VIDEO), run_id="r1", episodes=shuffled)
    # 组装本身不改顺序（目录读取侧已按 index 排过序）
    assert [item.vid for item in items] == ["vid03", "vid01", "vid02"]


def test_created_at_is_strictly_increasing() -> None:
    """库按 ``(created_at, item_id)`` 读回 —— 同一时刻的条目会退化成字典序。

    分集顺序一旦乱掉，界面上「第几集」和「末次位置」就全对不上号。
    """
    items = build_task_sequence(_cfg(TaskType.VIDEO), run_id="r1", episodes=EPISODES)
    stamps = [item.created_at for item in items]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(stamps), "时间戳必须两两不同"


def test_without_run_id_falls_back_to_bare_vid() -> None:
    """只给单测 / 临时组装用的退化路径：``item_id`` 就是 ``vid``。"""
    items = build_task_sequence(_cfg(TaskType.VIDEO), episodes=EPISODES)
    assert items[0].item_id == "vid01"


def test_default_config_builds_no_video_tasks() -> None:
    """默认配置是纯刷题场景（``task_sequence=[quiz]``）。"""
    assert RunConfig().task_sequence == [TaskType.QUIZ]
    assert build_task_sequence(RunConfig(), run_id="r1", episodes=EPISODES) == []
