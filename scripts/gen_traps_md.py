#!/usr/bin/env python
"""从 questions.json 生成 mock_site/static/traps.md。

文档与数据必须同源。手写 50 行表格迟早会漂，而靶场的「地面真值」一漂，
后面所有验收就都没意义了。

    python scripts/gen_traps_md.py            # 写入
    python scripts/gen_traps_md.py --check    # 只校验，不匹配则退出码 1

tests/test_mock_site.py 会用 --check 的等价逻辑卡住一致性。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "mock_site"
QUESTIONS = SITE / "static" / "questions.json"
TARGET = SITE / "static" / "traps.md"

#: 坑类型 -> (中文名, 触发条件, 对视觉读题的难点与处置)
#:
#: 最后一列在 v0.2.0 里换掉了原来的「期望降级层级」：题目**只经模型的眼睛读**，
#: 没有第二条通道可退，也就没有「降级到哪一级」这回事。现在要写清的是
#: **这一坑对一个「看截图 + 让模型读」的读者难在哪、程序会怎么处置**。
TRAP_INFO: dict[str, tuple[str, str, str]] = {
    "spa": (
        "SPA 动态渲染",
        "题目表单延迟 300~900ms 才渲染出来（先给一个加载占位块）",
        "**抢在渲染前截图只会拍到占位块**：模型读不出题、或自报 `clipped` / `uncertain` "
        "→ 读题门禁拦下、`_pause_for` 停下问人（**不静默跳过**）。这一坑考的是"
        "「读题时机」，不是通道选择 —— 程序已经不解析页面结构，没有「就绪断言」可等。",
    ),
    "lazy": (
        "懒加载",
        "题干与选项之间插 1100px 占位，选项在 `<ul>` 进入视口后才填充（另有 3s 兜底）",
        "**首屏截图里根本没有选项**（选项在视口之外、还没被填充）：模型拿不到选项，"
        "解析不出题面 → 这一题停下来问人。它说明「只截一张图」对长页面不够 —— "
        "要看全得**先滚动再读**，而滚动是动作、不属于读题。",
    ),
    "canvas": (
        "Canvas 题干",
        "题干画在 `<canvas>` 上，文字是**像素**：页面结构里没有题干文本（`legend` 留空，"
        "另有一份镜像副本只供靶场自检）",
        "**视觉读题天然读得到**：截图拍到的就是渲染后的画面。这一坑正是「不再解析"
        "文档结构」的理由之一 —— 结构里没有的东西，画面里有。",
    ),
    "iframe": (
        "iframe 嵌套",
        "整题搬进 8900 端口的**跨域** iframe，主文档里没有 `[data-quiz=question]`",
        "**对视觉读题没有影响**：截图拍的是像素，不分文档边界；模型给的是整张视口图上"
        "的归一化框，点击用视口坐标 —— 不需要「跨 frame 找元素」那一步。"
        "唯一代价是画面更小更挤，题面占比低。",
    ),
    "cls": (
        "class 名混淆",
        "题内所有 class 改成随机串（同一次加载内稳定，换个 seed 就换一套）",
        "**对视觉读题零影响**：模型看的是渲染结果，不认 class。这一坑专门惩罚"
        "依赖类名的读法 —— 那正是 v0.2.0 删掉的那条路。",
    ),
    "modal": (
        "弹窗遮罩",
        "选项渲染后 250ms 弹全屏遮罩，1.2~1.8s 后自动消失；期间强制点击会被遮罩吞掉",
        "两重难点：① 遮罩期间截到的画面被盖住 → 模型可能读不到选项、或给出错位坐标；"
        "② 点在遮罩上会被吞掉 → 按**选项区域像素差分**（`VerifyKind.SCREENSHOT_DIFF`）"
        "发现「点了没变」，按 T0-5 重放 ≤3 次、间隔 200~400ms，那时遮罩已消失。",
    ),
    "xhr": (
        "XHR 拉题",
        "题干改由 `/mock-api/question/<n>` 异步下发，页面要等响应回来才有内容",
        "**只看响应落进画面之后的样子**：被动抓 XHR 的网络通道已删除，"
        "画面上有没有题干就是唯一判据。响应还没回来就截图 → 模型读不出题 → "
        "停下问人（与 `spa` 是同一类「时机」问题）。",
    ),
    "next_after_scroll": (
        "下一题要滚动才出现",
        "非末题：「下一题」被 1200px 占位推到首屏之外，滚到才出现；"
        "**末题：按钮永不出现，改为「已是最后一题」**",
        "非末题 → 模型在**当前画面**里找不到「下一题」→ 按有界滚动往下挪"
        "（≤`advance_scroll_max_steps` 步），挪一下再问一次；"
        "末题 → 推进全失败 → **让视觉组再确认一次「是否全部完成」**"
        "（确认完成 → 干净收工；确认不了 → `advance_failed` 停下，绝不静默跳过）",
    ),
}

FLAG_INFO: dict[str, tuple[str, str]] = {
    "self_ref": (
        "含自指选项",
        "`shuffle` 必为 `false` —— 打乱会让「以上都对」指向别的集合（T0-4）",
    ),
    "image": (
        "题干带图",
        "题干里嵌了一张内联 SVG 图。v0.2.0 起每一题都靠模型看图，"
        "所以这一标记的用处变成：**核对图里的条件有没有被读进去**（漏掉图 = 静默答错）",
    ),
    "truncated": (
        "题干被截断",
        "题干超过容器高度，页面上**真的**被裁掉；模型会自报 `clipped` "
        "→ 读题门禁拦下、停下问人（截断的题面看起来完全正常，放行等于认下一个静默的错答案）",
    ),
}


def render(payload: dict) -> str:
    questions = payload["questions"]
    meta = payload["meta"]

    lines: list[str] = []
    lines.append("# 靶场坑位分布表（traps.md）")
    lines.append("")
    lines.append("> **本文件由 `scripts/gen_traps_md.py` 从 `questions.json` 生成，请勿手改。**")
    lines.append("> 改题目请改 `questions.json`，然后重跑生成脚本。")
    lines.append("> `tests/test_mock_site.py` 会卡住两者的一致性。")
    lines.append("")
    lines.append(
        f"- 题库版本：v{meta['version']} ｜ 生成日期：{meta['generated_at']}"
        f" ｜ 题量：**{meta['total']}**"
    )
    lines.append(f"- 打乱种子：`{meta['shuffle_seed']}`（同一 seed 下渲染结果可复现）")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 1. 坑类型总览")
    lines.append("")
    lines.append("| 坑码 | 名称 | 触发条件 | 对视觉读题的难点与处置 |")
    lines.append("|------|------|---------|-----------------------|")
    for code, (name, trigger, degrade) in TRAP_INFO.items():
        lines.append(f"| `{code}` | {name} | {trigger} | {degrade} |")
    lines.append("")
    lines.append("## 2. 标记（flags）")
    lines.append("")
    lines.append("| 标记 | 含义 | 处理要求 |")
    lines.append("|------|------|---------|")
    for code, (name, note) in FLAG_INFO.items():
        lines.append(f"| `{code}` | {name} | {note} |")
    lines.append("")

    # ---- 统计
    counts: dict[str, int] = dict.fromkeys(TRAP_INFO, 0)
    combo = 0
    for q in questions:
        for code in q["traps"]:
            counts[code] = counts.get(code, 0) + 1
        if len(q["traps"]) > 1:
            combo += 1
    lines.append("## 3. 覆盖统计")
    lines.append("")
    lines.append("| 坑码 | 题数 | 要求 | 达标 |")
    lines.append("|------|------|------|------|")
    for code in TRAP_INFO:
        need = "≥1（XHR 拉题题型）" if code == "xhr" else "≥3"
        threshold = 1 if code == "xhr" else 3
        hits = counts.get(code, 0)
        mark = "✅" if hits >= threshold else "❌"
        lines.append(f"| `{code}` | {hits} | {need} | {mark} |")
    lines.append(f"| 复合坑题 | {combo} | — | — |")
    lines.append("")

    # ---- 逐题
    lines.append("## 4. 逐题分布（题库全量，无遗漏）")
    lines.append("")
    lines.append(
        "| 题号 | 题库 id | 题型 | 坑 | 标记 | 触发条件 | 对视觉读题的难点与处置 |"
    )
    lines.append("|------|---------|------|----|------|---------|-----------------------|")
    for q in questions:
        traps = q["traps"]
        flags = q["flags"]
        trap_cell = "、".join(f"`{t}`" for t in traps) if traps else "—"
        flag_cell = "、".join(f"`{f}`" for f in flags) if flags else "—"

        if not traps:
            trigger_cell = "无坑：结构完整的基准题，用于 M2 闸门取数"
            degrade_cell = "无坑：题干、选项、提交都在一屏之内，模型一次读完即可"
        else:
            trigger_cell = "<br>".join(f"**{t}**：{TRAP_INFO[t][1]}" for t in traps)
            degrade_cell = "<br>".join(f"**{t}**：{TRAP_INFO[t][2]}" for t in traps)

        extra = []
        if "self_ref" in flags:
            extra.append("自指选项 → `shuffle=false`")
        if "truncated" in flags:
            extra.append("题干被容器裁断")
        if extra:
            degrade_cell += "<br>标记：" + "；".join(extra)

        lines.append(
            f"| {q['index']} | `{q['id']}` | {q['qtype']} | {trap_cell} | {flag_cell} "
            f"| {trigger_cell} | {degrade_cell} |"
        )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 5. 使用方式")
    lines.append("")
    lines.append("```bash")
    lines.append("python scripts/serve_mock.py")
    lines.append("```")
    lines.append("")
    lines.append("| 目的 | URL |")
    lines.append("|------|-----|")
    lines.append("| 跑题库全量 | `http://127.0.0.1:8899/quiz.html` |")
    lines.append("| 只跑指定题 | `http://127.0.0.1:8899/quiz.html?seq=21,22,23` |")
    lines.append("| 只跑某一类坑 | `http://127.0.0.1:8899/quiz.html?seq=30,31,32` |")
    lines.append("| 换个打乱种子 | `http://127.0.0.1:8899/quiz.html?seq=1,2&seed=7` |")
    lines.append("| 从第 21 题开始 | `http://127.0.0.1:8899/quiz.html?q=21` |")
    lines.append("| 网课 · 无弹题 | `http://127.0.0.1:8899/course.html` |")
    lines.append("| 网课 · 第 30s 弹题 | `http://127.0.0.1:8899/course.html?interrupt_at=30` |")
    lines.append(
        "| 网课 · 弹题与 ended 同刻 | `http://127.0.0.1:8899/course.html?interrupt_at=end` |"
    )
    lines.append(
        "| 网课 · 加速验收（每集 8s） | "
        "`http://127.0.0.1:8899/course.html?dur=8&interrupt_at=4` |"
    )
    lines.append("| 跨域 frame 单页 | `http://127.0.0.1:8900/frame.html?idx=30` |")
    lines.append("")
    lines.append("## 6. ground truth 的读取边界")
    lines.append("")
    lines.append(
        "- `data-answer`（呈现标号）与 `data-answer-texts`（正确项正文）"
        "只允许 **MockProvider** 读。"
    )
    lines.append(
        "- **读题链路**（视觉探针 + 模型 + 求解 + 执行）**不得**读它们，"
        "否则 M2 闸门数字全是假的。MockProvider 是零密钥跑通流程的替身，"
        "它读地面真值是刻意的例外，不是范例。"
    )
    lines.append(
        "- `data-question-id`（形如 `q021`）是靶场自己的编号，仅用于对照本文件，"
        "系统一律用 `qid` 算法自行计算。"
    )
    lines.append(
        "- iframe 题的上述属性位于 **frame 内**，主文档里读不到；"
        "本程序读的是画面，不看这些属性，因此不受影响。"
    )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gen_traps_md")
    parser.add_argument("--check", action="store_true", help="只校验一致性")
    args = parser.parse_args(argv)

    payload = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    rendered = render(payload)

    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != rendered:
            print("traps.md 与 questions.json 不一致，请重跑 gen_traps_md.py", file=sys.stderr)
            return 1
        print("traps.md 与 questions.json 一致")
        return 0

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(rendered, encoding="utf-8")
    print(f"已写入 {TARGET.relative_to(ROOT)}（{len(rendered)} 字符）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
