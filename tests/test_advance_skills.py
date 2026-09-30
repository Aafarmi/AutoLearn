"""推进技能（``advance_click`` / ``advance_swipe`` / ``advance_card``）的回归。

这一层守的是三件事：

1. **注册表完整**：每个推进技能都能取到正文、文件真实存在、ID 不与题型技能互借；
2. **解析只做严格校验**：未注册的 ``advance_skill_id`` 落 ``None``，``swipe`` 的方向或
   幅度有一项不合法就**整块**丢弃 —— 幅度猜错会变成一次真实滑过头的静默跳题，
   所以解析层不修、不猜、不沿用上一屏（真机事故：点了 27 号格却读回 26 题）；
3. **视觉组 system prompt 真的带上了推进技能目录**，并且把「每一步都要重新给推进目标」
   写进了给模型看的那份 Markdown —— 这是「推进后与实际不符」的修复点，
   提示词里少这句话，代码再严也只是拦下错误，不会让模型给出正确的本屏落点。

全部是纯内存用例：不连模型、不起浏览器，所以永不 skip。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from core.models import PageView
from solve.prompt_files import VISION_PROMPT_FILE, load_prompt, vision_system_prompt
from solve.reader import READ_SYSTEM_PROMPT, parse_page_view, parse_read_batch
from solve.skill_library import (
    ADVANCE_SKILL_SPECS,
    ADVANCE_SWIPE_SKILL_ID,
    SKILL_SPECS,
    advance_skill_catalog,
    advance_skill_prompt,
    get_advance_skill,
    get_skill,
    vision_skill_catalog,
)

#: 仓库根（本文件在 ``<root>/tests/``）。技能正文在这个根下的 ``skills/``。
REPO_ROOT = Path(__file__).resolve().parents[1]


def _page(**page: object) -> PageView | None:
    """把一组 ``page`` 字段包成模型的回复形态再解析（与真实调用同一条路径）。"""
    return parse_page_view({"page": page})


# --------------------------------------------------------------------------- #
# 注册表：每个推进技能都能取到正文
# --------------------------------------------------------------------------- #
def test_every_advance_skill_has_a_file_and_a_substantial_body() -> None:
    """注册了就必须读得出来，而且正文要像样（空文件等于没有规则）。"""
    assert len(ADVANCE_SKILL_SPECS) >= 3
    for spec in ADVANCE_SKILL_SPECS:
        path = REPO_ROOT / "skills" / spec.filename
        assert path.is_file(), f"推进技能 {spec.skill_id} 缺正文文件 {spec.filename}"
        body = advance_skill_prompt(spec.skill_id)
        assert len(body) > 400, f"{spec.skill_id} 正文只有 {len(body)} 字符，像是被清空了"
        # 正文标题里必须写明自己的 ID：文件与注册项错配时，正文会指向另一个技能。
        assert spec.skill_id in body, f"{spec.filename} 里找不到 {spec.skill_id}，注册项与文件对不上"


def test_advance_ids_do_not_borrow_question_skill_ids() -> None:
    """两张注册表**各自封闭**：推进技能不是题型技能，ID 不许互相冒充。

    混用会让模型把「这一步怎么走」读成「这道题属于哪类」，而误判题型的代价是整题解错。
    """
    advance_ids = {spec.skill_id for spec in ADVANCE_SKILL_SPECS}
    question_ids = {spec.skill_id for spec in SKILL_SPECS}

    assert len(advance_ids) == len(ADVANCE_SKILL_SPECS), "推进技能 ID 有重复"
    assert not advance_ids & question_ids
    for spec in ADVANCE_SKILL_SPECS:
        assert get_advance_skill(spec.skill_id) is spec
        assert get_skill(spec.skill_id) is None, "推进技能不该出现在题型注册表里"
    for spec in SKILL_SPECS:
        assert get_advance_skill(spec.skill_id) is None, "题型技能不该出现在推进注册表里"


def test_only_the_swipe_skill_requires_a_swipe_payload() -> None:
    """只有 ``advance_swipe`` 需要方向 + 幅度载荷；其余技能不带载荷也能落地。"""
    needing = {spec.skill_id for spec in ADVANCE_SKILL_SPECS if spec.needs_swipe}
    assert needing == {ADVANCE_SWIPE_SKILL_ID}


@pytest.mark.parametrize(
    "raw",
    [None, "", "   ", "advance_scroll", "advance_clickk", "single_choice", "swipe", 7, ["advance_click"]],
)
def test_unknown_advance_skill_id_has_no_match(raw: object) -> None:
    assert get_advance_skill(raw) is None  # type: ignore[arg-type]


def test_unknown_advance_skill_prompt_raises() -> None:
    """未知 ID 读正文必须**大声失败**：静默返回空串等于把技能规则悄悄丢掉。"""
    with pytest.raises(ValueError):
        advance_skill_prompt("advance_scroll")


def test_advance_catalog_lists_every_registered_skill() -> None:
    catalog = advance_skill_catalog()

    for spec in ADVANCE_SKILL_SPECS:
        assert f"`{spec.skill_id}`" in catalog
        assert spec.vision_hint in catalog
    assert "每一步" in catalog
    assert "advance_swipe" in catalog and "amplitude" in catalog


def test_vision_catalog_is_question_section_plus_advance_section() -> None:
    """视觉组目录 = 题型目录 + 推进目录，且原有题型段落一字不动（其他测试断言它）。"""
    catalog = vision_skill_catalog()

    assert "本地注册表自动选择" in catalog  # 题型段落的既有标志
    assert catalog.endswith(advance_skill_catalog())
    assert advance_skill_catalog() in catalog


# --------------------------------------------------------------------------- #
# 解析：合法输入
# --------------------------------------------------------------------------- #
def test_parses_advance_skill_and_swipe() -> None:
    page = _page(
        current=3,
        advance_skill_id="advance_swipe",
        swipe={"direction": "up", "amplitude": 0.6, "reason": "当前题占屏约六成"},
    )

    assert page is not None
    assert page.advance_skill_id == "advance_swipe"
    assert page.swipe is not None
    assert page.swipe.direction == "up"
    assert page.swipe.amplitude == pytest.approx(0.6)
    assert page.swipe.reason == "当前题占屏约六成"


@pytest.mark.parametrize("skill_id", ["advance_click", "advance_card"])
def test_parses_non_swipe_advance_skill(skill_id: str) -> None:
    page = _page(advance_skill_id=skill_id)

    assert page is not None
    assert page.advance_skill_id == skill_id
    assert page.swipe is None


def test_advance_skill_id_is_normalized() -> None:
    page = _page(advance_skill_id="  Advance_Click ")

    assert page is not None
    assert page.advance_skill_id == "advance_click"


def test_missing_advance_fields_are_none() -> None:
    """两个字段都可缺：缺 = 「这一屏没给推进目标」，绝不填默认值。"""
    page = _page(total=10, current=2)

    assert page is not None
    assert page.advance_skill_id is None
    assert page.swipe is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("up", "up"),
        ("UP", "up"),
        (" up ", "up"),
        ("down", "up"),  # 「往下滚」与手势「向上滑」是同一个动作
        ("向下", "up"),
        ("向上", "up"),
        ("left", "left"),
        ("Left", "left"),
        ("向左", "left"),
        ("leftward", "left"),
    ],
)
def test_swipe_direction_aliases(raw: str, expected: str) -> None:
    page = _page(advance_skill_id="advance_swipe", swipe={"direction": raw, "amplitude": 0.5})

    assert page is not None
    assert page.swipe is not None
    assert page.swipe.direction == expected


@pytest.mark.parametrize("raw", [0.6, "0.45", 1, 0.05, 0.999, 1.0])
def test_valid_amplitudes(raw: object) -> None:
    page = _page(advance_skill_id="advance_swipe", swipe={"direction": "left", "amplitude": raw})

    assert page is not None
    assert page.swipe is not None
    assert 0.0 < page.swipe.amplitude <= 1.0
    assert page.swipe.amplitude == pytest.approx(float(raw))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 解析：非法输入必须整块丢弃（执行层会照着幅度真的滑）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw",
    [0, 0.0, -0.1, 1.5, 2, "abc", "", "60%", None, [0.6], {}, True, False],
)
def test_invalid_amplitude_drops_the_whole_swipe(raw: object) -> None:
    """幅度不能转成 float、或不在 ``(0, 1]`` 内 → **整块** swipe 落 None。

    ``True`` 也在拒绝之列：``float(True) == 1.0``，那是模型给了垃圾值却被当成
    「滑一整屏」——最贵的一种静默错。
    """
    page = _page(advance_skill_id="advance_swipe", swipe={"direction": "up", "amplitude": raw})

    assert page is not None
    assert page.swipe is None
    assert page.advance_skill_id is None, "没有载荷的 advance_swipe 不该交给执行层"


@pytest.mark.parametrize(
    "raw", ["right", "next", "forward", "diagonal", "", "  ", None, 3, ["up"]]
)
def test_invalid_direction_drops_the_whole_swipe(raw: object) -> None:
    """只认 up / left：``right`` 是往回翻，认错方向比停下贵得多。"""
    page = _page(advance_skill_id="advance_swipe", swipe={"direction": raw, "amplitude": 0.5})

    assert page is not None
    assert page.swipe is None
    assert page.advance_skill_id is None


def test_non_dict_swipe_is_dropped() -> None:
    page = _page(advance_skill_id="advance_swipe", swipe="向上滑六成")

    assert page is not None
    assert page.swipe is None
    assert page.advance_skill_id is None


def test_advance_swipe_without_payload_is_demoted_to_none() -> None:
    """``advance_swipe`` 的唯一载荷缺失时，连技能选择一起降级为「没选」。

    交一个没有幅度的「滑一下」下去，执行层只能猜着滑 —— 上一版一跳十几题正是这么来的。
    """
    page = _page(advance_skill_id="advance_swipe")

    assert page is not None
    assert page.swipe is None
    assert page.advance_skill_id is None


@pytest.mark.parametrize("raw", ["advance_scroll", "single_choice", "true_false", "swipe"])
def test_unregistered_advance_skill_id_becomes_none(raw: str) -> None:
    """未注册 ID 一律落 None，但**合法观测不被牵连**。"""
    page = _page(advance_skill_id=raw, swipe={"direction": "up", "amplitude": 0.4})

    assert page is not None
    assert page.advance_skill_id is None
    assert page.swipe is not None, "推进技能没选，不影响这一屏本就合法的滑动观测"


def test_swipe_lives_independently_of_the_skill_choice() -> None:
    """动作由 ``advance_skill_id`` 决定；合法的 swipe 观测不因选择不同而丢弃。"""
    page = _page(
        advance_skill_id="advance_click",
        swipe={"direction": "left", "amplitude": 0.3},
        next_control={"box": [0.86, 0.94, 0.12, 0.05], "label": "下一题"},
    )

    assert page is not None
    assert page.advance_skill_id == "advance_click"
    assert page.next_control is not None
    assert page.swipe is not None


def test_invalid_values_are_visible_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    """非法值要能在留痕里认出来 —— 否则「模型给了什么」事后无从查起。"""
    with caplog.at_level(logging.WARNING, logger="solve.reader"):
        _page(advance_skill_id="advance_scroll", swipe={"direction": "right", "amplitude": 0})

    assert "advance_skill_id" in caplog.text
    assert "swipe" in caplog.text


def test_read_batch_carries_the_page_advance_target() -> None:
    """整份回复（``page`` + ``questions``）走一遍：推进目标必须原样到达 ``ReadBatch``。"""
    text = json.dumps(
        {
            "page": {
                "current": 5,
                "advance_skill_id": "advance_card",
                "card": {
                    "box": [0.01, 0.10, 0.16, 0.60],
                    "cols": 5,
                    "rows": 9,
                    "current_box": [0.01, 0.31, 0.028, 0.05],
                    "next_box": [0.04, 0.31, 0.028, 0.05],
                },
            },
            "questions": [],
        },
        ensure_ascii=False,
    )

    batch = parse_read_batch(text)

    assert batch is not None and batch.page is not None
    assert batch.page.advance_skill_id == "advance_card"
    assert batch.page.card is not None and batch.page.card.next_box == (0.04, 0.31, 0.028, 0.05)


# --------------------------------------------------------------------------- #
# 提示词：推进技能目录与「每一步重给」都真的喂给了模型
# --------------------------------------------------------------------------- #
def test_read_system_prompt_carries_the_advance_catalog() -> None:
    assert vision_system_prompt() == READ_SYSTEM_PROMPT
    assert READ_SYSTEM_PROMPT.endswith(advance_skill_catalog())
    for spec in ADVANCE_SKILL_SPECS:
        assert spec.skill_id in READ_SYSTEM_PROMPT, f"system prompt 漏了 {spec.skill_id}"


def test_vision_prompt_requires_a_fresh_advance_target_every_step() -> None:
    """视觉组 Markdown 必须写清「每一步都要按当前画面重新给」。

    这是「推进后与实际不符」（点了 27 号格读回 26 题）的修复点：光靠解析层严格校验，
    只能拦下错值；要拿到正确落点，必须让模型知道上一屏的坐标不能再用。
    """
    vision = load_prompt(VISION_PROMPT_FILE)

    assert "advance_skill_id" in vision
    assert "advance_swipe" in vision and "amplitude" in vision
    assert "每一步都要重新给" in vision
    assert "不要沿用" in vision or "不要回忆" in vision
    assert '"up"' in vision and '"left"' in vision
    # 题目同屏、看不到控件时选滑动，并给出幅度依据 —— 用户点名要的那条规则。
    assert "题目在同一屏" in vision or "题目同屏" in vision
    assert "幅度" in vision
