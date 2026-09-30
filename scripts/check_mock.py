#!/usr/bin/env python
"""靶场自检 —— 用真浏览器把 P1 的验收口径跑一遍。

    python scripts/check_mock.py                 # 自动挑空闲端口，跑完即退
    python scripts/check_mock.py --channel msedge
    python scripts/check_mock.py --keep-open     # 保留服务，方便手动看

**复用系统浏览器**（chrome / msedge），不下载自带 chromium —— 与 P9 的分发策略一致。

这个脚本是靶场与上层之间的接口：读题 / 执行链路在某个页码上跑不通时，
先怀疑适配器或目标页面，而不是靶场本身。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from serve_mock import build_server  # noqa: E402

SITE = ROOT / "mock_site"
QUESTIONS = json.loads((SITE / "static" / "questions.json").read_text(encoding="utf-8"))
COURSE = json.loads((SITE / "static" / "course.json").read_text(encoding="utf-8"))

LAUNCH_ARGS = [
    # M3-3 要求的启动参数：媒体动作不能依赖用户手势
    "--autoplay-policy=no-user-gesture-required",
    "--mute-audio",
]

RESULTS: list[tuple[bool, str, str]] = []


def record(ok: bool, name: str, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))
    flag = "PASS" if ok else "FAIL"
    line = f"[{flag}] {name}"
    if detail:
        line += f" — {detail}"
    print(line, flush=True)


class Checker:
    """极简断言收集器。"""

    def __init__(self) -> None:
        self.failures = 0

    def check(self, condition: bool, name: str, detail: str = "") -> bool:
        record(bool(condition), name, detail)
        if not condition:
            self.failures += 1
        return bool(condition)


def question_by_index(index: int) -> dict:
    return next(q for q in QUESTIONS["questions"] if q["index"] == index)


# --------------------------------------------------------------------------- #
# 服务
# --------------------------------------------------------------------------- #
class LocalMock:
    """在两个空闲端口上起靶场，退出时收摊。"""

    def __init__(self, quiet: bool = True) -> None:
        self.quiet = quiet
        self.main: Any = None
        self.frame: Any = None
        self.threads: list[threading.Thread] = []

    def __enter__(self) -> LocalMock:
        self.frame = build_server(SITE, "127.0.0.1", 0, "frame", "", self.quiet)
        frame_port = self.frame.server_address[1]
        origin = f"http://127.0.0.1:{frame_port}"
        self.main = build_server(SITE, "127.0.0.1", 0, "main", origin, self.quiet)
        main_port = self.main.server_address[1]

        self.frame.frame_origin = origin
        for server in (self.frame, self.main):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.threads.append(thread)

        self.frame_origin = origin
        self.base = f"http://127.0.0.1:{main_port}"
        time.sleep(0.15)
        return self

    def __exit__(self, *exc: object) -> None:
        for server in (self.main, self.frame):
            if server is not None:
                server.shutdown()
                server.server_close()


# --------------------------------------------------------------------------- #
# 浏览器
# --------------------------------------------------------------------------- #
async def launch(playwright: Any, channel: str | None) -> Any:
    candidates = [channel] if channel else ["chrome", "msedge", "chromium"]
    errors: list[str] = []
    for name in candidates:
        try:
            kwargs: dict[str, Any] = {"headless": True, "args": LAUNCH_ARGS}
            if name != "chromium":
                kwargs["channel"] = name
            browser = await playwright.chromium.launch(**kwargs)
            record(True, "启动浏览器", f"channel={name}")
            return browser
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}")
    raise RuntimeError(
        "没有可用浏览器。请安装 Chrome/Edge，或用 playwright install chromium。"
        f" 尝试过：{'；'.join(errors)}"
    )


async def new_page(browser: Any) -> Any:
    context = await browser.new_context(viewport={"width": 1280, "height": 720})
    return await context.new_page()


# --------------------------------------------------------------------------- #
# 各项检查
# --------------------------------------------------------------------------- #
async def check_quiz_baseline(check: Checker, browser: Any, base: str) -> None:
    page = await new_page(browser)
    await page.goto(f"{base}/quiz.html?seq=1", wait_until="domcontentloaded")

    form = page.locator('[data-quiz="question"]')
    await form.wait_for(state="visible", timeout=8000)

    stem = await form.locator('[data-quiz="stem"]').inner_text()
    options = await form.locator('[data-quiz="option-text"]').all_inner_texts()
    answer = (await form.get_attribute("data-answer")) or ""

    check.check(stem.strip() != "", "基准题 · 题干非空", stem[:24])
    check.check(len(options) == 4, "基准题 · 选项数 4", f"实得 {len(options)}")
    check.check(
        (await form.get_attribute("data-qtype")) == "single", "基准题 · 题型为单选"
    )
    check.check(await form.locator('[data-quiz="submit"]').is_visible(), "基准题 · 提交按钮可见")

    # ground truth 必须落在页面标号集合内
    labels = await form.locator("[data-quiz=input]").evaluate_all(
        "els => els.map(e => e.value)"
    )
    answer_labels = [part for part in answer.split(",") if part]
    check.check(
        bool(answer_labels) and set(answer_labels) <= set(labels),
        "基准题 · data-answer 是页面标号的子集",
        f"answer={answer} labels={labels}",
    )
    check.check(
        len(answer_labels) == 1, "基准题 · 单选只有 1 个正确标号", answer
    )

    # 提交 → 结果区
    for label in answer.split(","):
        await form.locator(f'[data-quiz="input"][value="{label}"]').check()
    await form.locator('[data-quiz="submit"]').click()
    result = form.locator('[data-quiz="result"]')
    await result.wait_for(state="visible", timeout=5000)
    check.check(
        (await result.get_attribute("data-ok")) == "true",
        "基准题 · 照 ground truth 提交后判为正确",
        (await result.inner_text())[:40],
    )
    check.check(
        await form.locator('[data-quiz="submit"]').is_disabled(), "基准题 · 提交后按钮置灰"
    )
    check.check(
        (await form.get_attribute("data-submitted")) == "true", "基准题 · 表单标记已提交"
    )
    await page.close()


async def check_trap_spa(check: Checker, browser: Any, base: str) -> None:
    page = await new_page(browser)
    await page.goto(f"{base}/quiz.html?seq=21", wait_until="domcontentloaded")
    # SPA 坑：刚导航完不该有表单，但要有占位元素
    immediate = await page.locator('[data-quiz="question"]').count()
    placeholder = await page.locator('[data-quiz="placeholder"]').count()
    check.check(immediate == 0, "坑 spa · 导航瞬间表单尚未挂载", f"count={immediate}")
    check.check(placeholder == 1, "坑 spa · 存在加载占位元素")
    await page.locator('[data-quiz="question"]').wait_for(state="visible", timeout=8000)
    check.check(True, "坑 spa · 稍后表单挂载成功")
    await page.close()


async def check_trap_lazy(check: Checker, browser: Any, base: str) -> None:
    page = await new_page(browser)
    await page.goto(f"{base}/quiz.html?seq=24", wait_until="domcontentloaded")
    form = page.locator('[data-quiz="question"]')
    await form.wait_for(state="visible", timeout=8000)

    check.check(await page.locator('[data-quiz="spacer"]').count() == 1, "坑 lazy · 存在占位块")
    before = await form.locator('[data-quiz="option-text"]').count()
    check.check(before == 0, "坑 lazy · 未滚动时选项为空", f"count={before}")

    await form.locator('[data-quiz="options"]').scroll_into_view_if_needed()
    await form.locator('[data-quiz="option-text"]').first.wait_for(timeout=6000)
    after = await form.locator('[data-quiz="option-text"]').count()
    check.check(after == 4, "坑 lazy · 滚动进视口后选项填充", f"count={after}")
    await page.close()


async def check_trap_canvas(check: Checker, browser: Any, base: str) -> None:
    page = await new_page(browser)
    await page.goto(f"{base}/quiz.html?seq=27", wait_until="domcontentloaded")
    form = page.locator('[data-quiz="question"]')
    await form.wait_for(state="visible", timeout=8000)

    legend = (await form.locator('[data-quiz="stem"]').inner_text()).strip()
    check.check(
        legend == "",
        "坑 canvas · legend 文案为空（题干只在 canvas 像素里，文档结构里没有）",
        repr(legend),
    )

    canvas = form.locator('[data-quiz="stem-canvas"]')
    mirror = (await canvas.get_attribute("data-quiz-stem-text")) or ""
    check.check(mirror.strip() != "", "坑 canvas · 题干正文镜像可读", mirror[:24])

    ink = await canvas.evaluate(
        """el => {
            const ctx = el.getContext('2d');
            const d = ctx.getImageData(0, 0, el.width, el.height).data;
            let nonBg = 0;
            for (let i = 0; i < d.length; i += 4) {
              if (d[i] < 240 || d[i+1] < 240 || d[i+2] < 240) nonBg++;
            }
            return nonBg / (d.length / 4);
        }"""
    )
    check.check(ink > 0.05, "坑 canvas · 非背景像素占比 > 5%", f"{ink:.3f}")
    await page.close()


async def check_trap_iframe(check: Checker, browser: Any, base: str, frame_origin: str) -> None:
    page = await new_page(browser)
    await page.goto(f"{base}/quiz.html?seq=30", wait_until="domcontentloaded")
    frame_el = page.locator('[data-quiz="frame"]')
    await frame_el.wait_for(state="attached", timeout=8000)

    src = (await frame_el.get_attribute("src")) or ""
    check.check(src.startswith(frame_origin), "坑 iframe · iframe 指向跨域源", src[:44])
    check.check(
        await page.locator('[data-quiz="question"]').count() == 0,
        "坑 iframe · 主文档没有题目表单（题目只在跨域 frame 内）",
    )

    frame = page.frame_locator('[data-quiz="frame"]')
    await frame.locator('[data-quiz="question"]').wait_for(timeout=8000)
    frame_answer = await frame.locator('[data-quiz="question"]').get_attribute("data-answer")
    check.check(
        bool(frame_answer),
        "坑 iframe · frame 内锚点与 ground truth 可读",
        frame_answer or "",
    )
    await page.close()


async def check_trap_cls(check: Checker, browser: Any, base: str) -> None:
    page = await new_page(browser)
    await page.goto(f"{base}/quiz.html?seq=33", wait_until="domcontentloaded")
    form = page.locator('[data-quiz="question"]')
    await form.wait_for(state="visible", timeout=8000)

    legacy = await page.locator(".qz-option").count()
    check.check(legacy == 0, "坑 cls · 原始类名已被抹掉（.qz-option 应为 0）", f"count={legacy}")
    check.check(
        await form.locator('[data-quiz="option"]').count() == 4,
        "坑 cls · data-quiz 锚点不受影响",
    )
    classes = await form.locator('[data-quiz="option"]').evaluate_all(
        "els => els.map(e => e.className)"
    )
    check.check(all(c != "qz-option" for c in classes), "坑 cls · 类名确已随机化", classes[0])

    answer = (await form.get_attribute("data-answer")) or ""
    q = question_by_index(33)
    check.check(
        q["shuffle"] is False and "self_ref" in q["flags"],
        "坑 cls · 第 33 题同时是自指题，shuffle 已关闭",
    )
    check.check(bool(answer), "坑 cls · 题型与答案仍可读")
    await page.close()


async def check_trap_modal(check: Checker, browser: Any, base: str) -> None:
    page = await new_page(browser)
    await page.goto(f"{base}/quiz.html?seq=36", wait_until="domcontentloaded")
    form = page.locator('[data-quiz="question"]')
    await form.wait_for(state="visible", timeout=8000)

    overlay = page.locator('[data-quiz="modal"]')
    await overlay.wait_for(state="attached", timeout=6000)
    check.check(True, "坑 modal · 遮罩已出现")

    answer = ((await form.get_attribute("data-answer")) or "A").split(",")[0]
    target = form.locator(f'[data-quiz="input"][value="{answer}"]')

    # 遮罩期间强制点击会被吞掉 —— 这正是回读重放机制存在的原因。
    # 用 click 而不是 check：check 会在"点了但状态没变"时直接抛错，
    # 而这里要观察的恰恰就是"点了没变"。
    await target.click(force=True, timeout=3000)
    swallowed = not await target.is_checked()
    check.check(swallowed, "坑 modal · 遮罩期间强制点击被吞掉（回读会不一致）")

    await overlay.wait_for(state="detached", timeout=6000)
    check.check(True, "坑 modal · 遮罩自动消失")

    from datetime import datetime

    t0 = datetime.now()
    await target.check(timeout=5000)
    elapsed = (datetime.now() - t0).total_seconds()
    check.check(await target.is_checked(), "坑 modal · 遮罩消失后正常选中")
    check.check(elapsed < 2.0, "坑 modal · 重放窗口内即可成功", f"{elapsed:.2f}s")
    await page.close()


async def check_trap_xhr(check: Checker, browser: Any, base: str) -> None:
    page = await new_page(browser)
    seen: list[str] = []
    page.on("response", lambda r: seen.append(r.url) if "mock-api/question" in r.url else None)

    await page.goto(f"{base}/quiz.html?seq=39", wait_until="domcontentloaded")
    form = page.locator('[data-quiz="question"]')
    await form.wait_for(state="visible", timeout=8000)

    check.check(
        any("/mock-api/question/39" in url for url in seen),
        "坑 xhr · 题干确实由 /mock-api/question 下发",
        str(seen[:2]),
    )
    stem = (await form.locator('[data-quiz="stem"]').inner_text()).strip()
    remote_stem = question_by_index(39)["stem"]
    check.check(stem == remote_stem, "坑 xhr · 远端题干与本地题库一致")
    await page.close()


async def check_course_playback(check: Checker, browser: Any, base: str) -> None:
    page = await new_page(browser)
    await page.goto(f"{base}/course.html?dur=8", wait_until="domcontentloaded")

    episodes = page.locator('[data-media="episode"]')
    await episodes.first.wait_for(timeout=8000)
    count = await episodes.count()
    check.check(count == len(COURSE["episodes"]), "网课 · 分集列表完整", f"{count} 集")

    first = episodes.first
    vid = await first.get_attribute("data-vid")
    expected = COURSE["episodes"][0]["vid"]
    check.check(vid == expected, "网课 · data-vid 与 course.json 一致", vid or "")

    video = page.locator('[data-media="video"]')
    await page.wait_for_function(
        "() => { const v = document.querySelector('[data-media=video]');"
        " return v && v.readyState >= 1; }",
        timeout=10000,
    )
    duration = await video.evaluate("v => v.duration")
    check.check(abs(duration - 8.0) < 0.2, "网课 · ?dur=8 覆盖生效", f"duration={duration:.2f}")

    paused_before = await video.evaluate("v => v.paused")
    check.check(paused_before is True, "网课 · 初始为暂停态")

    await page.locator('[data-media="play-button"]').click()
    await page.wait_for_function(
        "() => { const v = document.querySelector('[data-media=video]');"
        " return !v.paused && v.currentTime > 1.0; }",
        timeout=10000,
    )
    check.check(True, "网课 · 点击播放后 currentTime 推进 > 1s")

    await page.wait_for_function(
        "() => document.querySelector('[data-media=video]').ended",
        timeout=20000,
    )
    ended = await video.evaluate("v => ({ended: v.ended, t: v.currentTime, d: v.duration})")
    check.check(
        ended["ended"] and ended["t"] >= ended["d"] - 0.2,
        "网课 · ended 事件到达",
        str(ended),
    )

    before_index = await page.locator('[data-media="episode"][data-active="true"]').get_attribute(
        "data-episode-index"
    )
    await page.locator('[data-media="next"]').click()
    await page.wait_for_function(
        f"() => document.querySelector('[data-media=\"episode\"][data-active=\"true\"]')"
        f".getAttribute('data-episode-index') !== '{before_index}'",
        timeout=8000,
    )
    after_index = await page.locator('[data-media="episode"][data-active="true"]').get_attribute(
        "data-episode-index"
    )
    check.check(
        int(after_index or 0) == int(before_index or 0) + 1,
        "网课 · 下一集索引严格 +1",
        f"{before_index} → {after_index}",
    )
    await page.close()


async def check_course_interrupt(check: Checker, browser: Any, base: str) -> None:
    page = await new_page(browser)
    await page.goto(f"{base}/course.html?dur=10&interrupt_at=4", wait_until="domcontentloaded")
    video = page.locator('[data-media="video"]')
    await page.locator('[data-media="play-button"]').click()

    popup = page.locator('[data-media="interrupt"]')
    await popup.wait_for(state="attached", timeout=12000)
    check.check(True, "网课弹题 · 到点弹出")

    state = await video.evaluate("v => ({paused: v.paused, t: v.currentTime})")
    check.check(
        state["paused"] is False,
        "网课弹题 · 弹窗**没有**暂停视频（弹题不是媒体态）",
        f"paused={state['paused']} t={state['t']:.2f}",
    )

    # 弹题里必须能读到题目锚点，且答题后自动关闭
    await popup.locator('[data-quiz="question"]').wait_for(timeout=6000)
    answer = (await popup.locator('[data-quiz="question"]').get_attribute("data-answer")) or "A"
    for label in answer.split(","):
        await popup.locator(f'[data-quiz="input"][value="{label}"]').check()
    await popup.locator('[data-quiz="submit"]').click()
    await popup.wait_for(state="detached", timeout=8000)
    check.check(True, "网课弹题 · 答完自动关闭")

    mode = await page.locator("body").get_attribute("data-interrupt-mode")
    check.check(mode == "mid", "网课弹题 · 记为播放中打断", str(mode))
    await page.close()


async def check_course_interrupt_at_end(check: Checker, browser: Any, base: str) -> None:
    page = await new_page(browser)
    await page.goto(f"{base}/course.html?dur=6&interrupt_at=end", wait_until="domcontentloaded")
    video = page.locator('[data-media="video"]')
    await page.locator('[data-media="play-button"]').click()

    await page.locator('[data-media="interrupt"]').wait_for(state="attached", timeout=15000)
    state = await video.evaluate("v => ({ended: v.ended, t: v.currentTime, d: v.duration})")
    check.check(state["ended"], "网课弹题 · interrupt_at=end 时视频确已 ended", str(state))
    mode = await page.locator("body").get_attribute("data-interrupt-mode")
    check.check(
        mode == "at-end",
        "网课弹题 · 记为「与 ended 同刻」，供优先级判断",
        str(mode),
    )
    await page.close()


async def check_seed_reproducible(check: Checker, browser: Any, base: str) -> None:
    """同一 seed 下选项顺序必须一致 —— 否则失败复现成本会失控。"""
    orders = []
    for _ in range(2):
        page = await new_page(browser)
        await page.goto(f"{base}/quiz.html?seq=1&seed=7", wait_until="domcontentloaded")
        form = page.locator('[data-quiz="question"]')
        await form.wait_for(state="visible", timeout=8000)
        orders.append(await form.locator('[data-quiz="option-text"]').all_inner_texts())
        await page.close()
    check.check(orders[0] == orders[1], "确定性 · 同 seed 两次渲染选项顺序一致")

    page = await new_page(browser)
    await page.goto(f"{base}/quiz.html?seq=1&seed=99", wait_until="domcontentloaded")
    form = page.locator('[data-quiz="question"]')
    await form.wait_for(state="visible", timeout=8000)
    other = await form.locator('[data-quiz="option-text"]').all_inner_texts()
    await page.close()
    check.check(
        sorted(other) == sorted(orders[0]) and other != orders[0],
        "确定性 · 换 seed 会打乱（内容集合不变）",
    )


async def check_all_questions(check: Checker, browser: Any, base: str) -> None:
    """题库全量的渲染核对。

    这是靶场自检的**参考路径**：跨域帧、懒加载滚动、遮罩等待分别该怎么处理，
    这里各给一遍最小可用的做法。上层链路在某个页码上跑不过时，先拿这段对照。
    """
    problems: list[str] = []
    page = await new_page(browser)

    for q in QUESTIONS["questions"]:
        index = q["index"]
        tag = f"第 {index} 题"
        await page.goto(f"{base}/quiz.html?seq={index}&q={index}", wait_until="domcontentloaded")

        if "iframe" in q["traps"]:
            form = page.frame_locator('[data-quiz="frame"]').locator('[data-quiz="question"]')
        else:
            form = page.locator('[data-quiz="question"]')

        try:
            await form.wait_for(timeout=12000)
        except Exception:
            problems.append(f"{tag}: 表单未出现（坑 {q['traps']}）")
            continue

        if "lazy" in q["traps"]:
            await form.locator('[data-quiz="options"]').scroll_into_view_if_needed()
        if "modal" in q["traps"]:
            try:
                await page.locator('[data-quiz="modal"]').wait_for(state="detached", timeout=8000)
            except Exception:
                problems.append(f"{tag}: 遮罩没有消失")
                continue

        try:
            await form.locator('[data-quiz="option-text"]').first.wait_for(timeout=10000)
        except Exception:
            problems.append(f"{tag}: 选项未填充")
            continue

        texts = [
            t.strip()
            for t in await form.locator('[data-quiz="option-text"]').all_inner_texts()
        ]
        labels = [x for x in ((await form.get_attribute("data-answer")) or "").split(",") if x]
        raw_texts = await form.get_attribute("data-answer-texts")
        answer_texts = json.loads(raw_texts) if raw_texts else []
        qtype = await form.get_attribute("data-qtype")
        expected_texts = sorted(q["options"][i] for i in q["answer"])

        if sorted(texts) != sorted(q["options"]):
            problems.append(f"{tag}: 选项集合与题库不符")
        if sorted(answer_texts) != expected_texts:
            problems.append(f"{tag}: 正确答案正文不符 {answer_texts} != {expected_texts}")
        if len(labels) != len(q["answer"]):
            problems.append(f"{tag}: 正确标号数 {len(labels)} != {len(q['answer'])}")
        if qtype != q["qtype"]:
            problems.append(f"{tag}: 题型 {qtype} != {q['qtype']}")
        for label in labels:
            if not any(
                input_value == label
                for input_value in await form.locator("[data-quiz=input]").evaluate_all(
                    "els => els.map(e => e.value)"
                )
            ):
                problems.append(f"{tag}: 标号 {label} 不在选项里")

    await page.close()
    check.check(
        not problems,
        "全量题库 · 结构化可读出且 ground truth 一致",
        "全部通过" if not problems else f"{len(problems)} 处：{problems[:4]}",
    )


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
CHECKS: list[tuple[str, Callable[..., Any]]] = [
    ("基准题", check_quiz_baseline),
    ("坑 spa", check_trap_spa),
    ("坑 lazy", check_trap_lazy),
    ("坑 canvas", check_trap_canvas),
    ("坑 iframe", check_trap_iframe),
    ("坑 cls", check_trap_cls),
    ("坑 modal", check_trap_modal),
    ("坑 xhr", check_trap_xhr),
    ("网课播放", check_course_playback),
    ("网课弹题", check_course_interrupt),
    ("网课 ended 同刻", check_course_interrupt_at_end),
    ("确定性", check_seed_reproducible),
]

#: 较慢的检查，用 --all 打开（题库全量会跑一分多钟）
HEAVY_CHECKS: list[tuple[str, Callable[..., Any]]] = [
    ("全量题库", check_all_questions),
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="check_mock", description="靶场自检（真浏览器）")
    parser.add_argument("--channel", default=None, help="浏览器通道：chrome / msedge")
    parser.add_argument("--only", default=None, help="只跑名字包含该串的检查项")
    parser.add_argument("--all", action="store_true", help="附跑题库全量核对（较慢）")
    parser.add_argument("--keep-open", action="store_true", help="结束后保留服务端口")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    import asyncio

    from playwright.async_api import async_playwright

    args = parse_args(argv)
    check = Checker()

    with LocalMock() as mock:
        print(f"靶场服务  main={mock.base}  frame={mock.frame_origin}")
        print("-" * 72)

        async def run_all() -> None:
            async with async_playwright() as playwright:
                browser = await launch(playwright, args.channel)
                try:
                    suite = CHECKS + (HEAVY_CHECKS if args.all else [])
                    for name, fn in suite:
                        if args.only and args.only not in name:
                            continue
                        print(f"\n## {name}")
                        try:
                            if fn is check_trap_iframe:
                                await fn(check, browser, mock.base, mock.frame_origin)
                            else:
                                await fn(check, browser, mock.base)
                        except Exception as exc:
                            check.check(False, f"{name} · 执行异常", f"{type(exc).__name__}: {exc}")
                finally:
                    await browser.close()

        asyncio.run(run_all())

        if args.keep_open:
            print(f"\n服务保留中：{mock.base}/quiz.html （Ctrl+C 退出）")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass

    total = len(RESULTS)
    failed = sum(1 for ok, _, _ in RESULTS if not ok)
    print("\n" + "=" * 72)
    print(f"共 {total} 项，通过 {total - failed}，失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
