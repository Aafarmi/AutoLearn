"""P1 靶场验收：数据、文档、页面契约、服务端四层各卡一道。

这是 P1 的验收清单变成可执行断言的地方。任何一条红了，都不该把 Part 标完成。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from gen_traps_md import render as render_traps
from serve_mock import MAX_MEDIA_SECONDS, MIN_MEDIA_SECONDS, build_wav, parse_duration, parse_range

from core.vid import make_vid

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "mock_site"
QUESTIONS_PATH = SITE / "static" / "questions.json"
COURSE_PATH = SITE / "static" / "course.json"
TRAPS_PATH = SITE / "static" / "traps.md"

REQUIRED_TRAPS = {"spa", "lazy", "canvas", "iframe", "cls", "modal"}
KNOWN_TRAPS = REQUIRED_TRAPS | {"xhr", "next_after_scroll"}
KNOWN_FLAGS = {"self_ref", "image", "truncated"}

#: 页面必须提供的稳定锚点（适配器契约）。(属性, 取值)
QUIZ_ANCHORS = [
    ("data-quiz", "question"),
    ("data-quiz", "stem"),
    ("data-quiz", "stem-canvas"),
    ("data-quiz", "options"),
    ("data-quiz", "option"),
    ("data-quiz", "option-text"),
    ("data-quiz", "input"),
    ("data-quiz", "submit"),
    ("data-quiz", "next"),
    ("data-quiz", "result"),
]
MEDIA_ANCHORS = [
    ("data-media", "video"),
    ("data-media", "episode-list"),
    ("data-media", "episode"),
    ("data-media", "next"),
    ("data-media", "interrupt"),
    ("data-media", "play-button"),
    ("data-media", "progress"),
]
QUIZ_ANCHOR_IDS = [f"{attr}={value}" for attr, value in QUIZ_ANCHORS]
MEDIA_ANCHOR_IDS = [f"{attr}={value}" for attr, value in MEDIA_ANCHORS]

_COMMENT_PATTERNS = [
    re.compile(r"<!--.*?-->", re.DOTALL),
    re.compile(r"/\*.*?\*/", re.DOTALL),
    re.compile(r"(?m)^\s*//.*$"),
]


def strip_comments(text: str) -> str:
    """去掉注释再断言 —— 文档里的说明文字不该触发误报。"""
    for pattern in _COMMENT_PATTERNS:
        text = pattern.sub("", text)
    return text


def has_anchor(source: str, attr: str, value: str) -> bool:
    """同时兼容 HTML 写法 ``data-x="v"`` 与 JS 对象写法 ``'data-x': 'v'``。"""
    pattern = re.compile(
        rf"""["']?{re.escape(attr)}["']?\s*[:=]\s*["']{re.escape(value)}["']"""
    )
    return pattern.search(source) is not None


@pytest.fixture(scope="module")
def payload() -> dict:
    return json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def questions(payload: dict) -> list[dict]:
    return payload["questions"]


@pytest.fixture(scope="module")
def course() -> dict:
    return json.loads(COURSE_PATH.read_text(encoding="utf-8"))


def read_js(name: str) -> str:
    return (SITE / "static" / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 1. 题目数据
# --------------------------------------------------------------------------- #
def test_questions_are_uniquely_numbered(questions: list[dict]) -> None:
    """题号唯一且升序。

    **编号刻意保持稀疏**：题库从 50 题精简到覆盖面最小的那一批之后，仍然沿用
    原来的 ``index``。因为测试与脚本用 ``?seq=21`` / ``?seq=27`` 这类**字面编号**
    定位特定坑的题（`tests/helpers.py` 的 ``Q_*`` 常量），重排编号会让它们全部
    静默指向别的题 —— 测试照样绿，但测的不是那道题。
    """
    assert questions, "题库不能为空"
    indexes = [q["index"] for q in questions]
    assert indexes == sorted(indexes), f"题号必须升序：{indexes}"
    assert len(set(indexes)) == len(indexes), "题号有重复"
    assert len({q["id"] for q in questions}) == len(questions), "题目 id 有重复"


def test_every_question_has_ground_truth_answer(questions: list[dict]) -> None:
    """M0-1：**每题**都要埋答案，否则 MockProvider 会静默给错答案。"""
    for q in questions:
        assert q["answer"], f"第 {q['index']} 题没有答案"
        assert q["stem"].strip(), f"第 {q['index']} 题题干为空"
        assert len(q["options"]) >= 4, f"第 {q['index']} 题选项不足 4 个"
        assert len(set(q["options"])) == len(q["options"]), f"第 {q['index']} 题有重复选项"


def test_answer_indices_are_in_range_and_unique(questions: list[dict]) -> None:
    for q in questions:
        n = len(q["options"])
        assert len(set(q["answer"])) == len(q["answer"]), f"第 {q['index']} 题答案下标重复"
        for idx in q["answer"]:
            assert 0 <= idx < n, f"第 {q['index']} 题答案下标 {idx} 越界"


def test_qtype_matches_answer_arity(questions: list[dict]) -> None:
    """单选必须恰好一个答案；多选必须至少两个 —— 否则投票语义对不上。"""
    for q in questions:
        if q["qtype"] == "single":
            assert len(q["answer"]) == 1, f"第 {q['index']} 题是单选却有 {len(q['answer'])} 个答案"
        else:
            assert q["qtype"] == "multiple"
            assert len(q["answer"]) >= 2, f"第 {q['index']} 题是多选却只有 1 个答案"


def test_trap_and_flag_codes_are_known(questions: list[dict]) -> None:
    for q in questions:
        unknown = set(q["traps"]) - KNOWN_TRAPS
        assert not unknown, f"第 {q['index']} 题有未登记的坑 {unknown}"
        unknown_flags = set(q["flags"]) - KNOWN_FLAGS
        assert not unknown_flags, f"第 {q['index']} 题有未登记的标记 {unknown_flags}"


def test_six_trap_types_covered_at_least_three_each(questions: list[dict]) -> None:
    counts: dict[str, int] = {}
    for q in questions:
        for trap in q["traps"]:
            counts[trap] = counts.get(trap, 0) + 1
    for trap in sorted(REQUIRED_TRAPS):
        assert counts.get(trap, 0) >= 3, f"坑 {trap} 只有 {counts.get(trap, 0)} 题，要求 ≥3"
    assert counts.get("xhr", 0) >= 1, "缺少 XHR 拉题题型（M0-1 明确要求 1 个）"


def test_self_ref_questions_never_shuffle(questions: list[dict]) -> None:
    """T0-4：自指选项题必须 shuffle=false，否则「以上都对」会指向别的集合。"""
    self_ref = [q for q in questions if "self_ref" in q["flags"]]
    assert self_ref, "题库里没有自指选项题，T0-4 就没法在真数据上验"
    for q in self_ref:
        assert q["shuffle"] is False, f"第 {q['index']} 题含自指选项却允许打乱"


def test_shuffle_is_enabled_on_the_majority(questions: list[dict]) -> None:
    """不打乱的题要少 —— 否则「打乱后内容比对」这条逻辑没被真跑过。

    只有自指题（T0-4）不允许打乱，所以判据按**题库规模**给，而不是写死一个数 ——
    写死会在题库精简后要么失效、要么催人删掉这条断言。
    """
    self_ref_count = sum(1 for q in questions if "self_ref" in q["flags"])
    shuffled_count = sum(1 for q in questions if q["shuffle"])
    assert shuffled_count >= len(questions) - self_ref_count, (
        f"有 {len(questions) - self_ref_count - shuffled_count} 道非自指题没打乱"
    )
    assert shuffled_count > len(questions) / 2, f"只有 {shuffled_count} 题会打乱，覆盖不足"


def test_canvas_questions_carry_stem_mirror(questions: list[dict]) -> None:
    """canvas 题必须在 ``data-quiz-stem-text`` 里镜像正文。

    v0.2.0 起读题走「截图 → 模型」，镜像属性不再是读题通道：canvas 里的题干
    模型一样看得见。保留它是因为**测试与自检脚本**要靠这份镜像拿到可断言的真值
    —— 把图形题的题面做成纯图片、连一份文本副本都没有，判分就只能靠人眼看。
    """
    canvas_questions = [q for q in questions if "canvas" in q["traps"]]
    assert canvas_questions
    for q in canvas_questions:
        assert q.get("canvas_stem", "").strip(), f"第 {q['index']} 题缺 canvas_stem"


# --------------------------------------------------------------------------- #
# 2. traps.md 与数据同源
# --------------------------------------------------------------------------- #
def test_traps_md_matches_questions_json(payload: dict) -> None:
    assert TRAPS_PATH.exists(), "traps.md 未生成"
    assert TRAPS_PATH.read_text(encoding="utf-8") == render_traps(payload), (
        "traps.md 与 questions.json 漂移了，请重跑 scripts/gen_traps_md.py"
    )


def test_traps_md_covers_every_question(questions: list[dict]) -> None:
    text = TRAPS_PATH.read_text(encoding="utf-8")
    for q in questions:
        assert re.search(rf"^\| {q['index']} \|", text, re.MULTILINE), (
            f"traps.md 漏了第 {q['index']} 题"
        )
        assert f"`{q['id']}`" in text, f"traps.md 漏了 {q['id']}"
    rows = re.findall(r"^\| \d+ \|", text, re.MULTILINE)
    assert len(rows) == len(questions), (
        f"traps.md 题目行数 {len(rows)} != 题库题数 {len(questions)}"
    )


# --------------------------------------------------------------------------- #
# 3. 网课数据
# --------------------------------------------------------------------------- #
def test_course_has_at_least_five_episodes(course: dict) -> None:
    episodes = course["episodes"]
    assert len(episodes) >= 5
    assert [e["episode_index"] for e in episodes] == list(range(1, len(episodes) + 1))


def test_course_vids_match_core_algorithm(course: dict) -> None:
    """每集 data-vid 必须与 core/vid.py 的 make_vid 完全一致。

    不一致会让 M5 的断点续跑与去重认不出同一集。
    """
    course_id = course["meta"]["course_id"]
    for ep in course["episodes"]:
        expected = make_vid(course_id, ep["episode_index"], ep["title"])
        assert ep["vid"] == expected, (
            f"第 {ep['episode_index']} 集 vid 不匹配：{ep['vid']} != {expected}"
            "（改标题后需重跑生成）"
        )


def test_course_durations_stay_short(course: dict) -> None:
    """任务书警告：duration 太长会把 M5 全部验收拖慢。"""
    for ep in course["episodes"]:
        assert 20 <= ep["duration"] <= 60, f"第 {ep['episode_index']} 集时长 {ep['duration']}s 越界"


# --------------------------------------------------------------------------- #
# 4. 页面契约
# --------------------------------------------------------------------------- #
def test_quiz_html_wires_frame_origin_placeholder() -> None:
    html = (SITE / "quiz.html").read_text(encoding="utf-8")
    assert "__FRAME_ORIGIN__" in html, "缺少 frame 源占位符，服务端无法注入跨域地址"


@pytest.mark.parametrize(("attr", "value"), QUIZ_ANCHORS, ids=QUIZ_ANCHOR_IDS)
def test_shared_builds_every_quiz_anchor(attr: str, value: str) -> None:
    assert has_anchor(read_js("quiz_shared.js"), attr, value), (
        f"quiz_shared.js 未构造锚点 {attr}={value}"
    )


@pytest.mark.parametrize(("attr", "value"), MEDIA_ANCHORS, ids=MEDIA_ANCHOR_IDS)
def test_course_html_or_runtime_builds_media_anchors(attr: str, value: str) -> None:
    combined = (SITE / "course.html").read_text(encoding="utf-8") + read_js(
        "course_runtime.js"
    )
    assert has_anchor(combined, attr, value), f"媒体锚点 {attr}={value} 未构造"


def test_no_srcdoc_anywhere() -> None:
    """srcdoc 是同源 iframe，测不出跨域兜底。整个靶场不许出现（注释不计）。"""
    for path in SITE.rglob("*"):
        if path.is_file() and path.suffix in {".html", ".js"}:
            code = strip_comments(path.read_text(encoding="utf-8"))
            assert "srcdoc" not in code, f"{path.name} 用了 srcdoc"


def test_iframe_points_at_frame_port() -> None:
    js = read_js("quiz_runtime.js")
    assert "frame.html?idx=" in js
    assert "FRAME_ORIGIN" in js, "iframe 必须用注入的 frame 源，不能拼相对路径"


def test_interrupt_popup_never_pauses_video() -> None:
    """靶场铁律：弹题弹窗不得让 video.paused 变真（弹题不是媒体态）。"""
    js = read_js("course_runtime.js")
    body = js[js.index("function showInterrupt") : js.index("function hideInterrupt")]
    assert "video.pause()" not in body, "弹题里出现了 video.pause()，M5 的判断依据就失效了"


def test_interrupt_at_end_is_wired_on_ended() -> None:
    js = read_js("course_runtime.js")
    assert "INTERRUPT_END" in js and "onEnded" in js
    assert "at-end" in js, "缺少 interrupt_at=end 的优先级用例"


def test_course_duration_override_exists_for_fast_acceptance() -> None:
    assert "DUR_OVERRIDE" in read_js("course_runtime.js"), "缺少 ?dur= 快速验收开关"


# --------------------------------------------------------------------------- #
# 5. 服务端
# --------------------------------------------------------------------------- #
def test_wav_length_matches_requested_duration() -> None:
    for seconds in (1.0, 8.0, 36.0):
        data = build_wav(seconds)
        # 44 字节 RIFF 头 + 采样数 * 2 字节
        assert len(data) == 44 + int(8000 * seconds) * 2, f"{seconds}s 的 WAV 长度不对"
        assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def test_media_duration_is_clamped() -> None:
    assert parse_duration({}) == 24.0
    assert parse_duration({"d": ["8"]}) == 8.0
    assert parse_duration({"d": ["nonsense"]}) == 24.0
    assert parse_duration({"d": ["0"]}) == MIN_MEDIA_SECONDS
    assert parse_duration({"d": ["9999"]}) == MAX_MEDIA_SECONDS


def test_range_parsing() -> None:
    total = 1000
    assert parse_range(None, total) is None
    assert parse_range("bytes=0-99", total) == (0, 99)
    assert parse_range("bytes=100-", total) == (100, 999)
    assert parse_range("bytes=-100", total) == (900, 999)
    assert parse_range("bytes=0-9999", total) == (0, 999), "尾部越界应截断到文件末尾"
    assert parse_range("bytes=1000-", total) is None, "起点越界应视为不支持"
    assert parse_range("bytes=5-1", total) is None
    assert parse_range("bytes=0-10,20-30", total) is None, "多区间不支持"
    assert parse_range("items=0-10", total) is None


def test_frame_port_must_differ_from_main_port() -> None:
    """同端口就没有跨域可言 —— 命令行必须拦住。"""
    from serve_mock import main

    assert main(["--port", "8899", "--frame-port", "8899"]) == 2
