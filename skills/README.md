# AutoLearn 技能库

此目录包含可单独组合进解题组 system prompt 的题型技能正文，以及给视觉组选择「怎么进下一题」的推进技能正文。技能不执行视觉分析、不包含答案；视觉组只输出从注册表选择的 `skill_id` / `advance_skill_id`，编排层校验后把对应技能文本与结构化题面一起交给解题组。

技能分两类，**注册表也是两张**（都在 `solve/skill_library.py`）：

- **题型技能**（`SKILL_SPECS`）按 `qtype` 一一对应，回答「这道题怎么解」；
- **推进技能**（`ADVANCE_SKILL_SPECS`）不绑题型，回答「**这一步**怎么进下一题」，由视觉组**每一步**按当前画面重新选、重新给坐标。

## 当前题型技能

| ID | `qtype` | 文件 | 规则 |
|---|---|---|---|
| `single_choice` | `single` | [single_choice.md](single_choice.md) | 单项选择，严格回页面标号中的一个答案 |
| `true_false` | `true_false` | [true_false.md](true_false.md) | 先判断陈述真假，再按选项文字映射页面标号 |

## 当前推进技能

| ID | 文件 | 需要的观测 | 规则 |
|---|---|---|---|
| `advance_click` | [advance_click.md](advance_click.md) | `page.next_control.box` | 点「下一题 / 下一页 / 继续」控件进下一题 |
| `advance_swipe` | [advance_swipe.md](advance_swipe.md) | `page.swipe.direction` + `amplitude` | 题目同屏时滑动换题；幅度是相对视口宽/高的比例，缺了整块丢弃 |
| `advance_card` | [advance_card.md](advance_card.md) | `page.card`（`box`/`cols`/`rows`/`current_box`/`next_box`） | 点题号答题卡里「下一题号」那一格 |

注册表唯一定义于 `solve/skill_library.py::SKILL_SPECS` 与 `ADVANCE_SKILL_SPECS`，两张表的 ID 不许互相借用。增加技能必须同步添加注册项、视觉契约（`prompts/10-视觉组.md`）与测试。缺失题型 / 无匹配技能必须将读题原文与题型存档、把条目标成 `skipped`，随后按既定推进方案处理下一题；不能用“最相近”的技能，也不能把未判定题型默认为单选。

推进技能由 `solve/reader.py::parse_page_view` 严格校验：未注册的 `advance_skill_id` 一律落 `None`；`swipe.amplitude` 转不成 `float` 或不在 `(0, 1]` 内时**整块**丢弃，`direction` 只认 `up` / `left`（`down` 归一为 `up`）。解析层不修、不猜、不延续上一屏。

选项题技能要求题目有可映射的选项与有效坐标。判断题也通过这条坐标执行通道：不能可靠读出「正确/错误」等选项的页面标号时，不作答。

