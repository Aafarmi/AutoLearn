"""点哪儿：**框内内容质心**取点 —— 2026-09-28 事故的回归测试。

背景（留痕 ``logs/a090315220c6``）：真实站点上模型给的框比可点内容大得多 ——
选项文字只占框左侧一小段，右侧整行都是空白；而执行层当时点的是**框的几何中心**，
于是四次尝试全部落在行尾空白上，``region_mad=0.00``（页面上除右上角计时器外
一个像素都没动），题目以 ``action_failed`` 停下。

这个文件把那条几何关系**画出来**并钉住：

    内容在左、空白在右 → 质心必须落在内容上；而几何中心必须落在内容之外。

后半句同样重要 —— 它是「旧行为为什么会点空」的证据，改坏了会立刻红。
"""

from __future__ import annotations

import json
from io import BytesIO

from PIL import Image, ImageDraw

from act.actuator import Actuator
from act.screen import candidate_points, region_ink_centroid
from core.config import GuardThresholds, RunConfig
from core.enums import ActLevel, QType
from solve.reader import parse_read_batch, parse_read_payload
from tests.act_helpers import FakePage, RecordingBus

#: 只点一次（成功路径用）
FAST = RunConfig(guards=GuardThresholds(click_replay_max=0, click_replay_gap_ms=(0, 0)))
#: 允许换点重试（失败路径用）；间隔设 0 让用例跑得快
RETRY = RunConfig(guards=GuardThresholds(click_replay_max=3, click_replay_gap_ms=(0, 0)))

WIDTH, HEIGHT = 600, 40
TEXT_LEFT, TEXT_RIGHT = 20, 180  # 内容只占左边 1/3 —— 事故里的比例
TEXT_TOP, TEXT_BOTTOM = 12, 28

#: 模型按「整行」给的框：含右侧一大片空白。**这就是事故里的那个框。**
FULL_ROW = (0.0, 0.0, 1.0, 1.0)
#: 紧贴内容的框：理想情况。
TIGHT = (
    TEXT_LEFT / WIDTH,
    TEXT_TOP / HEIGHT,
    (TEXT_RIGHT - TEXT_LEFT) / WIDTH,
    (TEXT_BOTTOM - TEXT_TOP) / HEIGHT,
)


def _row_png(*, ink: int = 0, selected_ink: bool = False) -> bytes:
    """画一行「选项」：左边一小段内容、右边大片空白。

    ``selected_ink`` 在右半边再画一块 —— 模拟「选中态发生了可见变化」，
    让像素差分有东西可比。
    """
    image = Image.new("L", (WIDTH, HEIGHT), 255)
    draw = ImageDraw.Draw(image)
    draw.rectangle((TEXT_LEFT, TEXT_TOP, TEXT_RIGHT, TEXT_BOTTOM), fill=ink)
    if selected_ink:
        draw.rectangle((WIDTH - 80, TEXT_TOP, WIDTH - 20, TEXT_BOTTOM), fill=ink)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# 取点（纯函数）
# --------------------------------------------------------------------------- #


def test_ink_centroid_lands_on_content_while_box_center_lands_on_blank() -> None:
    """**本文件的核心断言**：同一个「整行框」，质心在内容上、几何中心在空白上。"""
    points = candidate_points(_row_png(), FULL_ROW, (WIDTH, HEIGHT))

    assert points[0][2] == "ink_centroid", "首选必须是内容质心"
    centroid_x, centroid_y = points[0][0], points[0][1]
    assert TEXT_LEFT <= centroid_x <= TEXT_RIGHT, f"质心跑出内容区：{centroid_x}"
    assert TEXT_TOP <= centroid_y <= TEXT_BOTTOM, f"质心跑出内容区：{centroid_y}"

    center_x = next(p[0] for p in points if p[2] == "box_center")
    assert center_x > TEXT_RIGHT, (
        "几何中心应当落在文字右侧的空白上 —— 这正是旧行为点不中的原因；"
        "若这条不成立，说明测试图的几何关系已经不再复刻事故"
    )


def test_blank_region_has_no_centroid_and_falls_back_to_geometry() -> None:
    """整块纯白（纯色块 / 图片选项 / 框本来就框错了）→ 没有质心，回退几何中心。"""
    blank = BytesIO()
    Image.new("L", (WIDTH, HEIGHT), 255).save(blank, format="PNG")
    png = blank.getvalue()

    assert region_ink_centroid(png, FULL_ROW, (WIDTH, HEIGHT)) is None
    points = candidate_points(png, FULL_ROW, (WIDTH, HEIGHT))
    assert points[0][2] == "box_center"
    assert len(points) >= 1, "至少要有几何中心这一个兜底"


def test_ink_centroid_gives_up_when_size_disagrees_with_the_image() -> None:
    """图与给坐标时那一帧尺寸对不上 → 返回 ``None``（**不许硬算**）。

    这类错配意味着「这两帧不是同一个视口」，按旧尺寸裁出来的区域指向别的东西。
    """
    assert region_ink_centroid(_row_png(), FULL_ROW, (WIDTH * 2, HEIGHT)) is None


def test_candidates_collapse_to_one_when_the_box_is_tight() -> None:
    """框本来就贴着内容时，几个候选点会落得很近 —— 去重后不该有四个重复点。"""
    points = candidate_points(_row_png(), TIGHT, (WIDTH, HEIGHT))
    assert 1 <= len(points) <= 3
    xs = [round(p[0]) for p in points]
    assert len(set(xs)) == len(xs), "同一条垂线上的重复候选点应当被去掉"


# --------------------------------------------------------------------------- #
# 落到真实点击上（Actuator 集成）
# --------------------------------------------------------------------------- #


async def test_select_option_clicks_the_ink_centroid() -> None:
    """整行框 + 内容在左 → 点的是**内容**，不是框中心。"""
    page = FakePage(screenshot_frames=[_row_png(), _row_png(selected_ink=True)])
    result = await Actuator(page, FAST, bus=RecordingBus()).select_option(
        FULL_ROW, (WIDTH, HEIGHT), QType.SINGLE
    )

    assert result.ok is True
    assert result.level_used is ActLevel.L6_VISION_XY
    assert "aim=ink_centroid" in (result.readback or ""), result.readback
    x, y = page.mouse.clicks[0]
    assert TEXT_LEFT <= x <= TEXT_RIGHT, f"点在内容之外：({x}, {y})"


async def test_select_option_switches_position_on_retry() -> None:
    """一直点不中时必须**换位置重试**，而不是把同一个错坐标重复点四遍。

    事故里执行层只会重复同一个点，于是四次全打在行尾空白上。
    换点之所以有意义，是因为「没生效」有两种相反的成因：
    点对了但还没渲染（重放同点就行）、**点错位置了**（必须换点）。
    """
    page = FakePage(screenshot_frames=[_row_png(ink=255)])  # 纯白行：无墨迹 → 只能走几何兜底，点不中必须换点
    result = await Actuator(page, RETRY, bus=RecordingBus()).select_option(
        FULL_ROW, (WIDTH, HEIGHT), QType.SINGLE
    )

    assert result.ok is False, "区域一直没变，就该按失败停下（绝不静默放过）"
    clicks = page.mouse.clicks
    assert len(clicks) >= 2, "应当尝试过不止一个落点"
    assert len(set(clicks)) >= 2, f"重试却始终点在同一个位置：{clicks}"


# --------------------------------------------------------------------------- #
# 一屏多题的解析（新契约 + 旧格式兼容）
# --------------------------------------------------------------------------- #

_ONE = {
    "stem": "题一",
    "qtype": "single",
    "options": [{"label": "A", "text": "a", "box": [0.1, 0.1, 0.2, 0.05]}],
}
_TWO = {
    "stem": "题二",
    "qtype": "single",
    "num_text": "2.",
    "options": [
        {"label": "A", "text": "a", "box": [0.1, 0.4, 0.2, 0.05]},
        {"label": "B", "text": "b", "box": [0.1, 0.5, 0.2, 0.05]},
    ],
}


def _payload(questions: list[dict], **extra: object) -> str:
    return json.dumps({"questions": questions, **extra}, ensure_ascii=False)


def test_batch_parses_every_question_in_one_screen() -> None:
    """一屏三道题要一次全拿到 —— 这是「减少消耗」的前提。"""
    batch = parse_read_batch(_payload([_ONE, _TWO], more_below=True, note="本屏 2 题"))

    assert batch is not None
    assert len(batch.questions) == 2
    assert batch.questions[0].stem == "题一"
    assert len(batch.questions[1].options) == 2
    assert batch.more_below is True
    assert batch.note == "本屏 2 题"


def test_batch_keeps_index_and_num_text() -> None:
    """``index`` / ``num_text`` 要解析出来 —— 下游靠它们判断有没有跳题。"""
    payload = _payload([{**_ONE, "index": 1, "num_text": "7."}])
    batch = parse_read_batch(payload)

    assert batch is not None
    assert batch.questions[0].index == 1
    assert batch.questions[0].num_text == "7."


def test_batch_skips_broken_question_but_keeps_healthy_ones() -> None:
    """一屏里坏了一道，不该把另外两道也扔掉。"""
    broken = {"stem": "", "options": []}
    batch = parse_read_batch(_payload([broken, _ONE, _TWO]))

    assert batch is not None
    assert [q.stem for q in batch.questions] == ["题一", "题二"]


def test_batch_returns_none_when_nothing_parses() -> None:
    assert parse_read_batch(_payload([{"stem": ""}, {"nope": 1}])) is None
    assert parse_read_batch("完全不是 JSON") is None


def test_batch_rescues_questions_from_a_truncated_reply() -> None:
    """回复被**截断**时，前面几道完整的题必须救回来。

    2026-09-28 实测故障：提示词改成「一屏多题」后回复变长，token 一紧就被截断，
    而截断的 JSON 用 ``json.loads`` 是**整份失败**的 —— 明明前面抄好了，却一道都读不到，
    界面上只有一句 ``perception_failed``。这条用例钉住「能救多少救多少」。
    """
    full = _payload([_ONE, _TWO], more_below=True)
    marker = '"题二"'
    truncated = full[: full.index(marker) + len(marker)]  # 第二道题刚抄到题干就断了
    assert not truncated.endswith("}"), "这条用例必须跑在「真的截断了」的输入上"

    batch = parse_read_batch(truncated)
    assert batch is not None, "截断不等于一道都读不到"
    assert [q.stem for q in batch.questions] == ["题一"]


def test_batch_accepts_a_bare_array() -> None:
    """模型直接回一个**裸数组**也是常见形态 —— 不能因为少了外层对象就全丢。

    （提示词要求的是 ``{"questions": [...]}``，但把「一屏多题」理解成「给我个列表」
    实在太自然了，这种偏差必须接住。）
    """
    batch = parse_read_batch(json.dumps([_ONE, _TWO], ensure_ascii=False))

    assert batch is not None
    assert [q.stem for q in batch.questions] == ["题一", "题二"]


def test_scanning_does_not_mistake_nested_objects_for_questions() -> None:
    """扫描抢救时**不能把选项这类嵌套小对象误当成题目**，否则会凭空多出几道"题"。"""
    option = {"label": "A", "text": "a", "box": [0.1, 0.1, 0.2, 0.05]}

    # 只有选项、没有题干 → 不是一道题
    assert parse_read_batch(json.dumps([{"options": [option]}])) is None

    # 有题干 + 合法选项的才收；同一个回复里嵌套的选项对象不该被算成额外的题
    text = json.dumps([{"stem": "题一", "options": [option]}])
    batch = parse_read_batch(text)
    assert batch is not None
    assert len(batch.questions) == 1, "嵌套的选项对象被误当成题目了"
    assert batch.questions[0].stem == "题一"


def test_legacy_single_question_payload_still_parses() -> None:
    """旧格式（整份就是一个题目对象）必须继续能读 —— 历史回复与测试替身还在用它。"""
    batch = parse_read_batch(json.dumps(_ONE, ensure_ascii=False))

    assert batch is not None
    assert len(batch.questions) == 1
    assert batch.questions[0].stem == "题一"


def test_single_question_entry_point_returns_the_first_one() -> None:
    """``parse_read_payload`` 保持「一道题」的旧语义（诊断脚本与门禁单测在用）。"""
    batch = parse_read_batch(_payload([_ONE, _TWO]))
    assert batch is not None and len(batch.questions) == 2

    first = parse_read_payload(_payload([_ONE, _TWO]))
    assert first is not None
    assert first.stem == "题一"
