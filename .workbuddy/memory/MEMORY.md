# AutoLearn 项目长期记忆

> 每会话必读的**铁律**。模块细节查 `REFERENCE.md`；原始过程按日期查 `YYYY-MM-DD.md`。
> 0.1.0 的「视觉 + DOM 双通道」叙述**已作废**，别再按它改代码。
> 2026-09-30 起：**开局裁决一次**（`core/run_plan.py`），之后整条运行只按那份方案走。

## 0. 定位与现状（v0.2.0 =「只使用模型」）
截**视口图** → 视觉组**只做观测**（固定格式 `{"page": {...}, "questions": [...]}`：题干 /
选项文本 / 每个选项的**归一化框** `0..1`，`page` 块带进度、推进控件、答题卡、提交按钮与范围）
→ 程序**开局裁决**唯一一份 `RunPlan`（`core/run_plan.py::derive_plan`）→ 解题组**只吃文本**作答
→ **全程照方案**按归一化坐标点击 / 点题号 / 滚动 / 滑动 → **截图差分**校验。

**2026-09-30 一批增量（开局判定 + 「观测 / 裁决」分离；仍算 v0.2.0，未升版号）**：
- **开局判定一次，之后全程照它走**：新增 `core/run_plan.py::derive_plan(batch, *, batch_size=0)
  -> RunPlan`；`RunPlan`（在 `core/models.py`）**取代旧的开局标定结果模型**
  （旧的只有 `total` + `method`，**给不出落点**，于是每一步还得再问一次模型「按钮在哪」）。
  开局那一次读图定死四件事：①推进方式（**恰好一种**）②推进几何（`control_box`，或答题卡
  `card_origin`/`card_step`/`card_anchor`）③提交范围（整卷 `paper` / 每题 `question`）④总题数。
- **推进方式按固定优先级裁决**（`core/advance_library.py::PLAN_PREFERENCE`）：
  `CARD`（点答题卡题号）→ `CLICK`（点固定控件）→ `SCROLL`（向下滚动）→ `SWIPE`（滑动）。
  `AdvanceMethod` 现有 5 个取值（多一个 `card`；`unknown` 只是裁决前的占位，裁决一定给出可执行的）。
  `PLAN_PREFERENCE` **只在开局读一次** —— 它不再是运行期的降级尝试顺序。
- **不换方式、不重读、不中途暂停**：运行期不再每题问模型「下一题在哪」，「点击→滚动→滑动」
  那条降级已整条删除；也**删掉**了「这一屏没有提交按钮就滚回顶部重读一次」那条恢复路径
  （滚回顶部会毁掉刚滚到的位置，读回来的是更早的题）。方案里那一种推不动 → `_settle_end`
  请视觉组确认「是不是全部完成了」→ 确认 `all_done` 才收工，否则 `advance_failed` 停下等人。
- **答题卡落点是纯算术**：`grid_geometry` / `card_cell_box`（网格**按行换行**，
  容差 `CARD_GRID_TOLERANCE = 0.10`）；缺 `cols`/`rows`、题号越界、算出的格子超出可见区域
  → 返回 `None` → **绝不点**。
- **视觉组只做观测、解题组只吃文本**：所有读图请求返回**同一份固定格式**；
  `page` = `progress`/`total`/`current`/`next_control{box,label}`/`card{...}`/`submit{box,scope}`/
  `completed`(`all_done|not_done|unknown`)/`scrolling`/`reason`；解题组请求里 `images` **恒为空**；
  **选项顺序与标号一律照页面原样**（单次与复算都不重排；标号可能不连续，如 A、B、D），
  解题组输出多一个必填 `confidence`。
- **置信度两种口径**：不复算（`sample_n == 1`）用模型自报的 `confidence`，低于
  `guards.confidence_review_min`（默认 0.5）→ 标 `⚠复核` 停下；复算（`sample_n > 1`）用多数票占比。
  **不可用的单次回复是「请求失败」** → **有界重发**（`solve/solver.py::SAMPLE_RETRY_MAX = 2`，
  每条采样项最多 3 次请求），**不是复算**；**显式空作答是模型的结论** → 不重发。
- **动作回读三态**（`act/verifier.py::region_change_state`）：`changed`（≥`REGION_CHANGE_MIN_MAD`=2.0）/
  `weak`（≥`REGION_CHANGE_WEAK_MAD`=0.05）/ `none`；`verify_region_changed` 的
  `ok = state in {"changed","weak"}`，`actual` 形如 `region_mad=0.31 state=weak`。
  `act/actuator.py::select_option`：`ink_centroid` 落点点完 `none` → **绝不重点**
  （`ok=True`，readback 写 `no_change_on_ink:...`）；几何兜底落点 `none` → 换下一个候选点
  （换点前先重测）；全部候选 `none` → `_exhausted`（`ok=False` + 截图 + pause）。
- **提示词只剩 5 份**：`00-共享契约.md` / `10-视觉组.md` / `20-解题组.md` / `33-推进方式库.md` /
  `34-训练总结.md`。`solve/reader.py` 只有 `READ_SYSTEM_PROMPT` 一个提示词。
- 细节与排障见 `REFERENCE.md` §0（收口）与 §F（速查）。

### 2026-09-30 收口（四条修复 + 为什么）

1. **开局判定一次，之后全程照它走**（`core/run_plan.py::derive_plan` → `RunPlan`）：推进方式
   （恰好一种）、推进几何、提交范围、总题数在开局定死；运行期不再问模型「下一题在哪」、
   不换第二种方式、不「滚回顶部重读」、不因「这一屏没有提交按钮」中途暂停。
   **为什么**：每一步临时判断都可能一次跳过十几道题，而漏题几乎没人会发现（代价不对称）。
2. **视觉组只做观测、解题组只吃文本**：所有读图请求返回同一份固定格式（`page` + `questions`），
   解题组请求里 `images` 恒为空，选项顺序与标号一律照页面原样。
   **为什么**：把「该怎么推进」交给看不见程序的模型，它每次说法都可能不同；
   标号一旦错位，下游从输出上看不出来。
3. **置信度两种口径 + 有界重发**：不复算用模型自报 `confidence`（<0.5 → ⚠复核必停），
   复算用多数票占比；「回复不可用」→ 有界重发（每条采样项 ≤3 次请求），
   「显式空作答」→ 不重发、直接复核。**为什么**：单样本一致率恒为 1.0、不带信息；
   把「这次回复坏了」与「模型说没答案」混成一件事，故障只在不复算时暴露。
4. **回读三态 + 墨迹上「没变化」不重点**（`changed`/`weak`/`none`；`no_change_on_ink`）：
   `ink_centroid` 点完 `none` 按「本来就已选中」收工，只有几何兜底落点才换候选点。
   **为什么**：画面本来就没什么可变时，旧布尔判据会判「没点中」→ 反复换点、乱点一气，
   多选上还会把刚选上的勾取消。

**同一批里顺手补掉的四个真 bug（都是这一批的测试逼出来的）**：

5. **开局那一次读图曾经丢掉首屏第 1 题**：`_read_question_by_vision` 把同屏**其余**题入队、
   把第一道**返回**给调用方，而 `_plan_run` 没人接返回值；主循环又先取队列
   （`_take_queued_question`）—— 于是「一屏多题」时首屏第 1 题**静默跳过**，单题屏则被
   **读第二遍**（白花一次调用）。现在 `_plan_run` 把它 `insert(0, ...)` 放回队首
   （与 `_scroll_reveal_next` 早就有的那一句约定一致）。
6. **整卷提交后其余题停在 `applied`（不是终态）**：整卷只有一次提交动作，回读只挂在
   「最后作答的那道题」上；续跑时 `applied` 会被**重做** —— 多选题上重新点选项 =
   把刚选上的勾**取消**。现在提交确认后 `_settle_paper_batch()` 把这一轮其余 `applied` 的题
   按 `applied → submitted → verified` 一并推终态（状态机里 `applied` **没有**直达 `verified` 的边）；
   **提交失败/暂停时不推**（那一卷结果未知）。
7. **「只有 page 观测、没有题目」的开局屏曾经拿不到方案**：`_last_batch`/`_last_size`/`_last_submit`
   原先记在「至少一道题过门禁」之后 → 那种开局屏把整份观测丢掉、整条运行在 `no_plan` 上打转。
   现在观测在**解析成功**时就留下（`page` 是页面级事实，与这一屏有没有题无关）。
8. **提交框的第三个来源 `_last_submit`**（最近一次读图看到的框）：关掉开局判定
   （`guards.advance_calibrate=False`）+ 每题提交，原先**必然** `vision_no_submit_box`。
   现在按范围排序 `PAPER → [方案, 收尾, 最近]`、`QUESTION → [方案, 最近, 收尾]`；
   **每一对框与尺寸永远同源**。
9. **解题请求带图会当场炸**（`Solver._complete` 里的硬闸）：用户原话「解题组模型严禁拿到
   非文本输入」。构造请求那一处（`_run_pass`）从来不填 `images`，这道闸的作用是让将来
   「顺手把截图也发过去」在第一时间暴露 —— 真机上发图之后推理模型会不回答、只烧 token，
   `content` 留空，从输出上完全看不出来。看页面是视觉组的事（`solve/reader.py`）。

**2026-09-29 一批增量（仍算 v0.2.0，未升版号）**：
- **Tier 分级整体删除** → `SolvePath`（`single`/`mock`/`cache`，`TierUsed` 留兼容别名，
  `SolvePath.parse` 把老 `tier1/tier2` 读回 `single`）；`solve.escalated` 事件、`tier2_min_votes`
  一并删除。**只有一种求解模式**。
- **复算改为任务级显式开关**：`RunConfig.recalculate`（默认 False = 以第一次答案为准）+
  `sample_n`（复算时总采样次数，≥2）。
- **固定的推进方式库** `core/advance_library.py`（+ `prompts/33-推进方式库.md`）：`_advance`
  经 `strategy_for` 取方案里那**一种**方式执行（`PLAN_PREFERENCE` 只供开局裁决，见上）。
- **滚动加固**：步长 0.5、上限 12、`advance_scroll_recover_max` 滚过头有界回滚。
- **任务级缓存**：`core.trace.run_dir()`/`purge_run_cache()`；删任务级联清目录；
  `GET /api/tasks/{run_id}/cache` 预览。
- **训练模式**：`solve/training.py` + `POST /api/training/{run_id}` + `prompts/34-训练总结.md`，
  经验写进三份 md 的 `AUTOTRAIN` 区。
- **判题组只吃文字**：删掉 `_vision_images`（判题不再把截图回发模型 —— 推理模型会烧光
  token 让 content 留空）；`SOLVE_MAX_TOKENS=4096`；`READ_MAX_TOKENS=16384`。
- **干跑（dry_run）已整体删除**（config/enums/actuator/orchestrator/UI/scripts/tests 全清）。
- **launcher.py / run.bat 不再启动靶场**（8899/8900）；靶场仍是回归基础设施（手动起）。
- **日志清理**：`vision_probe` 只留 `vision:crop_ok`+`vision:bytes`；`perception.done` 事件
  不再带 `warnings`（清掉 `vision:crop_only`/`requires_model_config`/`arbiter:vision_only` 残留）。

- 题目侧**只有视觉一条通道**。已删：`perception/dom_probe.py`、`net_probe.py`、`act/readback.py`、
  `adapters/mock_exam/selectors.yaml`，以及 `ProbeOrder`/`ProbeMode`/`PROBE_CHAINS`/`CAPABILITIES`/`modes_for`。
  `active_probe_chain()` **恒返回 `[ProbeName.VISION]`**。
- **唯一例外**：网课媒体侧仍读 `<video>`（`paused`/`currentTime`/分集索引）。准确说法是
  **不再用它读题** —— 说「本程序从不接触页面结构」是错的。
- 读题是全链路**唯一「错了也不报错」**的环节（模型回的 JSON 结构完好，少个负号看不出来）→ 才有门禁。
- 只做「附加到**用户正在用的**页面 / 用户选定的窗口」：不导航、不新开/关标签页、不猜答案。
- ⚠️ **仓库没有 git**：查「改了什么」只能靠文件 mtime / CHANGELOG / `code_rev()`。
- 靶场**降级为测试专用**（UI 入口已摘）但**保留** —— 唯一回归基础设施。
- 未交付：① 站点适配向导 ② 桌面窗口视觉识别（真机未验）③ 填空/简答写回（执行器只会点选项）。

## 1. 硬口径（违反即事故）
1. **坐标只乘一次**：一律 `scale="css"` 截图；`act/screen.py::norm_box_center(box, size)` **只乘、不除 DPR**，
   换算点全项目只有这一处。**别把除法加回来**（加了每次点击偏一半）。
2. **一律视口截图，禁 `full_page=True`**（有守门单测）。
3. **门禁不许绕过**：`clipped` / `uncertain` 非空 → 不许下传，带原因码（`vision_incomplete` / `vision_uncertain`）
   暂停问人。`more_below` 只留痕不拦；`submit_box` 看不到**也不拦** —— 提交范围在开局就定死
   （整卷页面本来就不需要每屏都有提交按钮），看不到按钮的处置在**收尾**那一步（`vision_no_submit_box`）。
4. **截图差分 ≠ 选中正确**：`verify_region_changed()` 只证明「那块像素变了」。题目侧校验从
   「能确认选中内容」退化成「能确认点到了东西」—— 这是设计代价，也是门禁必需的原因。
5. `submitted` 是**唯一危险态**：续跑只回读，**绝不重新点击/重放提交**；确认按钮置灰 + 后端 409。
6. 提交**不重试、不重放**，超时暂停等人（`Actuator.submit` 失败 → 截图 + `SUBMIT_TIMEOUT` + 暂停）。
7. **禁 `networkidle`**；媒体态必须读 `paused`/`ended`/`currentTime`/`duration`，**不能拿元素可见性代替**。
8. 弹题**不是**媒体态（弹窗不会让 `paused` 变真）：MutationObserver + 轮询双保险。
9. 失败一律「暂停 + 截图 + 留档」；**禁静默跳过、禁用 assert 做流程控制**。
10. `qid` 不含答案；选项排序后参与哈希；NFKC 归一化。**选项顺序一律照页面原样**（编排层与解题组
    都不重排、不改写标号；标号可能不连续如 A、B、D）；自指选项（以上/上述/都正确…）由
    `solver.allows_shuffle` 作**风险标注**用，但呈现顺序不再因它改变。
11. 投票按**内容**比对（不依赖字母位置），样本仍带「呈现顺序 ↔ 原始顺序」映射（历史留痕可归并）。
    `MockProvider` 直接读地面真值，**不得作 M2 闸门依据**。
12. 密钥只进 OS 凭据管理器；`models.yaml` 零敏感字段，仅存 `api_key_ref`。
13. **`load_run_config()` 永不抛异常** —— 它被路由直接调用，抛出去就是控制台 HTTP 500。
    陈旧字段剔除 + **自愈回写**；只有类型/取值这类真错误才继续抛。
14. 契约计数：事件总数 **28**（`test_t0_definitions.py::EVENT_COUNT`；2026-09-29 删了
    `solve.escalated` 又加了 `training.done`，仍 28）、建表数 **6**（`DB_TABLE_COUNT`）
    —— 改一处必须同步常量。6 表：`run`/`task_item`/`answer`/`suspend_frame`/`level_stat`/`media_position`。
15. `prompts/*.md` 是提示词**唯一真源**（`solve/prompt_files.py` 导入时读一次，缺文件大声失败）；
    **改完必须重启服务**；现存 **5 份**：`00-共享契约.md`（两个模型都读）/ `10-视觉组.md` /
    `20-解题组.md` / `33-推进方式库.md`（给训练模式看，**不进模型请求**）/ `34-训练总结.md`
    —— 一次请求只该看到一个输出契约（读图 = `00` + `10`，解题 = `00` + `20`）。
16. 题目侧不得加回「解析题目 DOM / 抓 XHR / 通道优先级」，也不得把题目锚点塞回适配器。

## 2. 推进与收尾（开局裁决一次，别再引入启发式）
```
任务开始 ─► 读一屏（视觉组只做观测）─► 程序裁决唯一一份 RunPlan
                                       （推进方式 + 落点几何 + 提交范围 + 总题数）
     逐题解题（推进严格照 plan.method 那一种执行；推进后校验题号）
   已完成数 == plan.total  或  那种方式推不动
        └─► 视觉组再读一屏确认「是否全部完成」（看 page.completed）
              ├ all_done → 干净收工 finished（整卷还据此交卷）
              └ 其余（not_done / unknown / 问不成）→ advance_failed 停下（绝不静默跳过）
```
1. `AdvanceMethod`：`click` / `card` / `swipe` / `scroll` / `unknown`（`unknown` 只是裁决前的占位，
   裁决一定给出一个**可执行**的方式）。优先级 `core/advance_library.PLAN_PREFERENCE` =
   `CARD → CLICK → SCROLL → SWIPE`，**只在开局读一次**。
2. **裁决失败不终止任务，但也不动手**：开局读图没成 → `_plan = None`，推进按「没有逻辑就不动手」
   处理（**一个坐标都不点**），直接进收尾确认。
3. `total` 不是正整数就按「**不知道**」；不知道总数时 `reached_total()` **永远 False**
   —— **不能拿「不知道」当「做完了」**。
4. **收尾确认：问不成 = 未确认**（无 provider / 截图失败 / 解析失败 / 观测不是 `all_done` → False）。
   这是整个流程里**唯一防跳题的闸门**。
5. **同一题同一触发点只问一次**（`_completion_checks`，键 `(qid, trigger)`）；触发点只有
   `reached_total`（做满方案总数）与 `stuck`（`_settle_end` 里那种方式推不动）。
6. **推进严格按方案，绝不换第二种方式**：`_advance` → `_run_advance_strategy` 只走 `plan.method`
   对应那一支（`card` 点题号格 / `click` 点 `control_box` / `scroll` 滚半步 / `swipe` 滑一下）；
   推不动 → `_settle_end` 请视觉组确认，确认不了就 `advance_failed` 停下等人。
   ⚠️ 2026-09-28 真机事故根因：那版「是哪种方式」只管谁先谁后，`scroll` 模式下照样去找按钮、
   找不到就继续滚，一路滚到上限（0.8 屏 × 6 = 4.8 屏），**题号从 3 跳到 16**，中间 13 题全被跳过。
7. **有界滚动**（`advance_scroll_step_ratio`=0.5、`advance_scroll_max_steps`=12、
   `advance_scroll_recover_max`=2 有界回滚）：滚轮优先（虚拟列表只认真实 `wheel`），退化脚本滚动。
8. **滚动到位**：`_advance_by_scroll` **滚一步 → 读一屏 → 确认新题进来了**才算成功。
   判据是「这一屏有没有**没做过的题**」：有 → 到位（那批题直接入队，主循环不再读图）；
   还是做过的 → **滚少了** → 再滚一步；滚不动 → 如实返回 `False` 交收尾确认。
9. ⚠️ v0.2.0 起题目侧没有锚点：「找不到按钮」**不再等于**「跑完了」，收尾**一律**问模型确认。
10. **推进后题号必须对得上**（`_expected_num` / `_expected_number_ok`）：读回来的 `num_text` 与
    方案预期不符 → 日志 `advance=wrong_question`（带 `expected` / `got`）+ `advance_failed` 停下。
    这是防「静默跳题」的最后一道 —— 坐标算歪、答题卡内部滚过、页面重排都会表现为这一条。

## 3. 提交时机（P19 + 2026-09-30，改提交逻辑必读）
`submit_scope`：`question`=每题点一次提交（靶场既有行为）；`paper`=整卷、全部做完的收尾点一次。
**范围在开局裁决的 `RunPlan.submit_scope` 里就定死**，运行期不再漂移（`_submit_scope_of_run()`
先看方案，只有在「开局读图没成、没有方案」时才走结构性兜底：一屏读到过 ≥2 题 → `paper`，
否则 → `question`）。详细表格与事故复盘见 `REFERENCE.md` §B4。

1. ⚠️ **2026-09-28 真机事故**（`logs/de615cf6d625`）：44 题作业页做完**第 1 题**就点「交卷」，
   落点正确但 `region_mad=0.06`（`0/44题` 时按钮**被禁用**）→ `submit_timeout` 停下。
   这次只是停下；**站点若允许交卷，那一按就是 43 题作废**。
2. **整卷提交只在「视觉组确认全部完成」之后**（`_completion_confirmed` 是主闸门）。没确认 → 一次都不交。
3. **提交绝不重放**（`_paper_submitted` 只置一次）。
4. `solve/reader.py::_submit_scope_of` **只认明确词**（`question`/`paper`/本题/整卷），
   含糊说法一律 `None`（再由开局裁决按结构证据补）—— **猜错的方向是提前交卷**。
5. 提交按钮的框有两个来源：开局方案里的 `submit_box`（配开局那张图的尺寸）→ 收尾那一屏读到的框
   （配收尾那张图的尺寸）。两个都没有时**按范围分别处置**，共同点是**都不猜坐标**：
   整卷 → 提示「请手动点一次交卷」+ `vision_no_submit_box` 暂停；每题 → 必须停下（答案可能没被记录）。
6. 故障表现是「**没提交**」而不是报错 → 必须有测试钉住**顺序**：
   `[select, select, submit]` vs `[select, submit, select, submit]`。

## 4. 点哪儿 / 一屏多题（P17/P19/P20，改 `act/` 与读题必读）
1. **落点 = 框内内容质心**（`candidate_points`），**不是几何中心**。模型给的框常比内容大，
   几何中心会落在**行尾空白**上（事故：4 次点击全废，`region_mad=0.00`）。
2. **回读是三态，不是布尔**（`act/verifier.py::region_change_state`）：`mad ≥ 2.0` → `changed`；
   `0.05 ≤ mad < 2.0` → `weak`（1px 描边/小圆点这类轻变化）；`mad < 0.05` → `none`。
   `verify_region_changed` 的 `ok = state in {"changed","weak"}`，`actual` 形如 `region_mad=0.31 state=weak`。
3. **`select_option` 的处置按落点分**：`ink_centroid`（落在选项自己的文字/标号上）点完 `none`
   → **绝不重点**（`ok=True`，readback 写 `no_change_on_ink:...`，最可能是「本来就已选中」，
   多选上再点一下是把勾取消）；几何兜底落点（`box_center` / `left_half`）`none`
   → 换下一个候选点（**换点前先重测**，已是 `changed`/`weak` 就收工）；全部候选 `none`
   → `_exhausted`（`ok=False` + 截图 + pause），绝不静默报成功。
4. `click`/`submit` 也走同一套取点；`readback` 里 `aim=<来由>` 是定位信息，非校验结论。
5. 视觉组**一次读一屏**，输出 `questions` **数组**（载体 `ReadBatch`，`raw` 是原始回复留痕）；
   被切掉半截的题**不进数组**；门禁**逐题**判（坏题不挡好题）。提示词**不再要求输出 `qid`**。
6. `parse_read_batch`/`read_questions` 是新入口；`parse_read_payload`/`read_question` 是兼容入口（只取第一题）。
7. **同屏先做完再推进**：`_drain_quiz` 在 `_pending_reads` 非空时 `continue`，不走 `_advance`
   （后者入口**清空队列**）。缺这一句 → 同屏第 2、3 题必被丢。
8. ⚠️ **`READ_MAX_TOKENS` 跟着题数走**：一屏多题后 2048 会**截断**，截断的 JSON **整份解析失败**。
   已提到 4096 + `_scan_question_objects` 逐个对象抢救（兼容**裸数组**）。
9. ⚠️ **读题失败必须留痕**：解析不出来时 `logger.warning` 打印**模型原始回复前 400 字**，
   并把模型原文落成 `logs/<run_id>/vision_read_raw.txt`。**没有证据时，先补观测，再补防御。**
10. **滚动路径两个坑**：① `_scroll_reveal_next` 读到的新题**必须入队**
    （`self._pending_reads.insert(mark, question)`）—— 否则丢掉刚滚出来的题；
    ② 传 `recover_at_top=False` —— **绝不在滚动之后触发「滚回顶部重读」**（会毁掉刚滚到的位置并清空队列）。
11. **留痕目录是"目录 + 文件"混排**：`logs/<run_id>/` 下条目一律是**子目录**，另有 run 级 `events.jsonl`。
    任何"遍历条目"的代码都要按 `is_dir()` 筛。

## 5. 模型选择
- **Tier 分级已删（2026-09-29）**：`TierUsed` → `SolvePath`（`single`/`mock`/`cache`）。
  `TierUsed` 是**兼容别名**（老库/老留痕读得回来），但**没有 `TIER1`/`TIER2` 成员**了 ——
  引用它们会在导入期就 `AttributeError`（刻意的）。`Answer.tier_used` → `Answer.solve_path`
  （DB 列名仍 `tier_used`，表结构冻结）。`provider.model_for()` 已**去掉 tier 参数**。
- **复算可选（2026-09-29）**：`RunConfig.recalculate`（默认 False）+ `sample_n`（复算时总采样次数，
  至少 2，由校验器把 `recalculate=True, sample_n=1` 拒掉）。**不复算 = 以第一次答案为准**，
  每题只调一次；复算后多数票一致率 < `guards.agreement_accept` → 标 ⚠复核必停。
- **置信度两种口径（2026-09-30）**：不复算 → 用模型自报的 `confidence`
  （<`guards.confidence_review_min`=0.5 → ⚠复核必停；模型没报则回落到一致率，**不新增暂停**）；
  复算 → 多数票占比。**「回复不可用」与「模型显式空作答」是两件事**：前者**有界重发**
  （`SAMPLE_RETRY_MAX`=2，每条采样项最多 3 次请求），后者不重发、直接 ⚠复核。
- 模型库**一套配置一个模型**（`ModelProfile.model`）；老 `models.yaml` 由 `migrate_profile_payload` 迁移
  （取 `tier1_model` 当 `model`，**并且**把嵌套 `capabilities.tier1_ok/tier2_ok` 合成 `model_ok` ——
  漏迁 `capabilities` 会因 `extra="forbid"` 让**整份文件加载失败**）。
- 任务里**四组**：`vision_profile_id` / `vision_backup_profile_ids` / `model_profile_id`（界面叫「解题组」，字段名故意不改） / `backup_profile_ids`。链 = 主选 → 备用（按用户勾选顺序）。
  主备**皆空** → 退回整条活动链；备用里**死 id** 忽略；视觉链**只在真选过视觉组时**才单独构造。
- **加 `RunConfig` 字段 → 必须检查 `ui/routes/run.py::_config_out()` 有没有回显**（症状：「存得下、读回是空」）。
- **预检必须查「模型是否支持视觉」**：只拦「**明确知道都不支持**」；`capabilities is None`（没测过）放行。
- `POST /api/tasks` 的配置补丁用 **`exclude_unset`**（「都留空」是**明确决定**，必须能覆盖草稿）。

## 6. 环境陷阱
- **`AUTOLEARN_SECRET_BACKEND=memory`**：不设它跑测试会往**用户真实 Windows 凭据库**写
  `autolearn/<uuid>`，写满后 `CredWrite` 报 `WinError 8`，**用户自己再也存不进密钥**。
- **「界面没变化」先查旧进程**：`/api/health` 回 `boot_rev`/`code_rev`/`pid`；`scripts/check_server.py` 判定；
  `run.bat`/`launcher.py` 在端口被占时**拒绝启动**。⚠️ 指纹必须在**模块导入时**算好（`BOOT_REV = code_rev()`）
  —— 请求里现算恒等磁盘现状 → 护栏假绿灯。判陈旧比 `boot_rev`。
- **回环地址不能用环境代理**：httpx 默认 `trust_env=True` → `HTTP_PROXY` 把 `127.0.0.1` 交给代理 → 假超时。
  一律 `trust_env=False`（否则真浏览器用例**整批静默 skip** 而 pytest 全绿）。
- **`.bat` 必须纯 ASCII/GBK**（cmd 按 GBK 读，UTF-8 中文直接崩）。
- **验证环境三坑**：靶场要**常驻后台**起；pytest terminal summary 会被沙箱吃掉 → 用 `--junitxml` 读权威结果；
  `--basetemp` 放项目内（`.pytest-tmp/runN`）。**靶场不起 → 真浏览器用例 skip，pytest 全绿 ≠ 跑过**。
- 浏览器默认 `msedge`，用 `AUTOLEARN_BROWSER_CHANNEL=chrome` 切。
- **跑测试期间不要改** `core/ui/target/act/perception/solve/adapters/scripts` 下的 `.py/.js/.html/.css`
  —— `code_rev()` 变化会让 `test_health_exposes_boot_rev_and_why_it_matters` 变红（**真断言，不是 flaky**）。
- **不要用 PowerShell 管道改文本文件**（会静默跳行并把坏内容写回）。
- **删除文件三条规矩**：① 只删**显式列出的单个路径** ② 先校验在仓库根之下 ③ **绝不与其它命令同行**。
- **改动前先备份 `__pycache__`**：`.pyc` 能被 `SourcelessFileLoader` 直接加载，是误删后**唯一**线索。

## 7. 常用命令与基线
```bash
export AUTOLEARN_SECRET_BACKEND=memory
export AUTOLEARN_BROWSER_CHANNEL=chrome       # 默认 msedge
.venv/Scripts/python scripts/serve_mock.py &  # 靶场（8899 主 + 8900 跨域 frame），须常驻
.venv/Scripts/python -m pytest -q --junitxml=.pytest-tmp/report.xml -p no:cacheprovider
.venv/Scripts/python -m ruff check . ; .venv/Scripts/python -m mypy
.venv/Scripts/python scripts/check_ui.py      # 前端验收（真 Chrome）
.venv/Scripts/python scripts/check_mock.py --all ; scripts/check_target.py
.venv/Scripts/python scripts/check_server.py  # 判控制台是否连着旧进程
# ⚠️ check_read.py 目前**起不来**：它仍 import 已删除的 reader 名字（冲突已上报，不在文档范围）
.venv/Scripts/python scripts/check_read.py --url-contains <片段> --locate-next
```
> **基线（2026-09-28 · v0.2.0 + P16~P20）**：pytest **964 passed / 0 failed / 0 skipped**、
> ruff 全绿、mypy **70 files**、check_ui **105/105**、check_target **37/37**。
> ⚠️ **旧基线作废**（915 / 944 / 896 / 937 等都不可与新数相减 —— 用例边界一起变了）。
> ⚠️ 2026-09-30 之后基线**未重测**：新增 `tests/test_run_plan.py` / `tests/test_advance_library.py` 等，
> 用例数一定变了，先跑一遍再引用数字。

## 8. 按模块查
| 要动什么 | 查哪 |
|---|---|
| 打包 / 桌面化 / `AutoLearn.spec` / `launcher.py` | `REFERENCE.md` §A |
| `ui/` 界面 / 任务接口 / 提示词 / 靶场 / 技术栈 | `REFERENCE.md` §B~§E |
| 网课媒体流程 / 弹题中断-恢复 | `REFERENCE.md` §C |
| 关机 / 端口清理 | `REFERENCE.md` §B6 |
| 提交范围 / 整卷提交 | `REFERENCE.md` §B4 + 本文件 §3 |
| 真实站点（超星学习通）实战结论 / 排障手册 | `docs/真实站点作业做题手册.md` §0.5 |
| **开局判定 / 运行方案**（`derive_plan`、`RunPlan`、答题卡几何） | `core/run_plan.py` + `tests/test_run_plan.py` → 本文件 §2 |
| **推进 / 滚动**（`_advance` 按方案分派、滚动到位、题号校验） | 本文件 §2 第 6~10 条 → `tests/test_advance_next.py` |
| **任务列表界面**（多选 / 批量删除 / 重试按钮） | `ui/routes/tasks.py`（`/retry`、`/bulk-delete`、`/cache` **必须排在 `/{run_id}/{action}` 之前**，否则被通配路由吃掉）→ `app.js::renderTasks` / `syncPickUi` → `scripts/check_ui.py` |
| **任务缓存 / 删除级联** | `core/trace.py::run_dir`/`purge_run_cache` → `ui/store.py::delete_task` → `core/db.py::delete_run`（顺带删 `level_stat`）|
| **推进方式库 / 裁决优先级** | `core/advance_library.py`（`PLAN_PREFERENCE`）+ `prompts/33-推进方式库.md` → `core/run_plan.py` → `orchestrator._run_advance_strategy` |
| **置信度 / 复核必停 / 重发** | `solve/solver.py`（`SAMPLE_RETRY_MAX`、`_confidence_of`）+ `guards.confidence_review_min` → 本文件 §5 |
| **训练模式** | `solve/training.py`（`TARGETS`/AUTOTRAIN 区）+ `ui/routes/training.py` + `prompts/34-训练总结.md` |
| **留痕 / 事件流** | `core/trace.py::RunLogger`（条目目录 + run 级 `events.jsonl`）→ `orchestrator._emit` |
| 视觉组 / 解题组的交接契约 | `docs/双模型分工-视觉组与解题组.md` |
| 更原始的过程记录 | `2026-09-25.md` ~ `2026-09-29.md` |
