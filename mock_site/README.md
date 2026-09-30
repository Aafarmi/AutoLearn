# AutoLearn 双靶场

全项目的**地面真值来源**。靶场质量直接决定后面所有验收能不能做。

```bash
python scripts/serve_mock.py
```

| 地址 | 说明 |
|------|------|
| http://127.0.0.1:8899/quiz.html | 题目靶场 · 22 题 · 8 类坑 |
| http://127.0.0.1:8899/course.html | 网课靶场 · 6 集 · 可配置弹题 |
| http://127.0.0.1:8900/frame.html | 跨域 iframe 内容（**只在 8900 提供**） |

自检：`python scripts/check_mock.py --channel chrome --all`

---

## 一、URL 参数

**题目靶场**

| 参数 | 作用 |
|------|------|
| `?seq=21,22,23` | 只跑这几题（按坑聚焦测试用） |
| `?seed=123` | 打乱种子，默认 `20260925`。**同 seed 渲染结果完全可复现** |
| `?q=21` | 从题库序号 21 开始 |

**网课靶场**

| 参数 | 作用 |
|------|------|
| `?interrupt_at=30` | 播放到第 30s 弹题（视频**不停**） |
| `?interrupt_at=end` | 弹题与 `ended` **同刻**到达，专测「`ended` 优先」 |
| `?dur=8` | 覆盖全部集时长，把 M5 验收压到十几秒 |
| `?ep=3` | 从第 3 集开始 |
| `?quiz=21` | 弹题用哪一道题库题 |

---

## 二、锚点契约

> **v0.2.0 起程序只使用模型读页面**：题目几何由视觉模型从**截图**里给出，
> 不再解析这里的文档结构。所以下表的**题目锚点**不再是产品读题路径 ——
> 它们现在是**为测试与自检脚本保留的地面真值坐标**（判分、查坑都要靠它）。
>
> 媒体锚点仍是产品路径：网课任务要读 `<video>` 的 `paused` / `currentTime` /
> 集号，页面上只有属性这一个可靠来源（见 `adapters/mock_exam/selectors_media.yaml`）。

### 媒体锚点 `data-media`（**产品路径**）

`video` · `episode-list` · `episode` · `next` · `interrupt` · `interrupt-panel` ·
`play-button` · `progress` · `overlay`

`episode` 上带 `data-episode-index`、`data-vid`、`data-duration`、`data-title`、`data-active`。

### 题目锚点 `data-quiz`（v0.2.0 起仅供测试/自检）

**在任何坑下都稳定**，测试一律锚它们，**绝不依赖 `.qz-*` 类名**。

| 取值 | 元素 |
|------|------|
| `question` | 题目表单（`<form>`），ground truth 属性都挂在它上面 |
| `stem` | 题干 `<legend>`（canvas 题会**留空**） |
| `stem-canvas` | 图画题干 `<canvas>`，正文镜像在 `data-quiz-stem-text` |
| `figure` | 内联 SVG 图（`image` 标记题） |
| `options` | 选项 `<ul>` |
| `option` | 选项 `<li>`，带 `data-index`（呈现序号）、`data-label`（字母标号） |
| `option-text` | 选项正文 `<span>` |
| `input` | `radio`（单选）/ `checkbox`（多选），`value` 即字母标号 |
| `submit` / `next` | 提交 / 下一题按钮 |
| `result` | 结果区，`role="status"`，带 `data-ok` |
| `spacer` | 懒加载占位块（`lazy` 坑） |
| `placeholder` | 加载占位（`spa` 坑） |
| `modal` / `modal-panel` | 弹窗遮罩（`modal` 坑） |
| `frame` | 跨域 `<iframe>`（`iframe` 坑） |

### 表单上的 ground truth 属性

| 属性 | 内容 |
|------|------|
| `data-answer` | 正确项的**呈现标号**，多选逗号分隔，如 `"A,C"` |
| `data-answer-texts` | 正确项正文的 JSON 数组（内容比对用） |
| `data-qtype` | `single` / `multiple` |
| `data-traps` / `data-flags` | 本题命中的坑与标记，逗号分隔 |
| `data-shuffled` | `true` / `false` |
| `data-question-id` | 靶场自己的编号（`q021`），**仅用于对照 traps.md** |

> ### ⚠ ground truth 的读取边界
>
> **只有 `MockProvider`（测试替身）可以读 `data-answer` / `data-answer-texts`。**
> 产品和测试断言之外的东西读了它，M2 闸门的数字就全是假的。
>
> 视觉模型看的是像素：只要正确项没有**可见**地渲染出来，它就无从知道答案 ——
> 靶场刻意不在画面上标出正确项，这条边界因此仍然成立。
>
> 系统一律用 `core/qid.py` 的算法自行计算 `qid`，**不得使用 `data-question-id`**。

### 选项打乱与标号重算

`shuffle=true` 的题会在渲染时打乱选项，并按**呈现位置重新分配字母标号**，
`data-answer` 同步重算。所以：

- 页面标号 ≠ 题库下标，跨采样比对**必须按内容**（M2-2）；
- 同一道题打乱后 `qid` 必须不变（T0-1，已有单测）。

---

## 三、8 类坑

v0.2.0 起判据只有一个：**这一坑让「截图 → 模型读题」这件事变难在哪里**。
（「期望的降级层级」一栏因此改写；原先按 DOM 通道的读法分的层级已经不存在。）

| 坑 | 实现 | 对模型读题意味着什么 |
|----|------|--------------------|
| `spa` | 表单延迟 300~900ms 才挂载 | 截早了就是一张空画面 —— 必须等就绪再截，否则模型只能答「图里没有题」 |
| `lazy` | 题干与选项间插 1100px 占位，`<ul>` 进视口后才填充（3s 兜底） | 不滚动就截不到选项 —— 必须先把选项滚进视口 |
| `canvas` | 题干画在 canvas 上，`legend` 留空 | **正面考验视觉**：题干只在像素里，纯文本读法一律失效 |
| `iframe` | 整题搬进 8900 端口的**跨域** iframe | 题目在视口里照样看得见（截的是画面，不是主文档）—— 这正是视觉路线相对 DOM 路线的优势 |
| `cls` | 题内类名换成随机串 | **不影响视觉**（模型不认类名）；保留它是为了惩罚任何还在依赖类名的写法 |
| `modal` | 渲染后 250ms 弹遮罩，1.2~1.8s 后自动消失 | 遮罩期间截到的图上看不见选项 —— 点击也会被遮罩吞掉，需按 T0-5 重试 |
| `xhr` | 题干由 `/mock-api/question/<n>` 异步下发 | 响应没回来时画面里没有题干 —— 同样是「等就绪再截」 |
| `next_after_scroll` | 非末题：「下一题」被 1200px 占位推到首屏之外，**滚到才出现**；<br>**末题：按钮永不出现**，改为「已是最后一题」 | 非末题 → **有界滚动**后再找；<br>末题 → 推进全失败 → **视觉组收尾确认**「是否全部完成」 |

> `next_after_scroll` 复刻的是真实站点上「**还没滚到**」与「**真的没有了**」长得一样
> 这个处境 —— 而把前者当成后者就是**静默跳掉后面所有题**。
> 两条分支的靶子：`?seq=8`（非末题，滚出来）与 `?seq=15`（末题，永不出现）；
> `?seq=8,15` 一次覆盖两条。
>
> **两条分支分别验的是新流程的两端**（2026-09-28 起）：
> 非末题验「按标定出的推进方式真的能滚出按钮」，末题验「推不动时视觉组确认一次」。
> （P14 那套「靠进度正则猜是不是最后一题」已整套推倒，别再按它理解这个坑。）
>
> **它只推「下一题」，不动提交按钮** —— 一个坑只测一件事。
> 注意：靠点「下一题」遍历题库的测试助手必须**先滚再点**（`tests/helpers.py::click_next`），
> 因为 Playwright 只会把「可见」元素滚进视口，对 `display:none` 等多久都不会变可见。

逐题分布见 **[`static/traps.md`](static/traps.md)**（由 `scripts/gen_traps_md.py` 从
`questions.json` 生成，改题库后请重跑）。

**为什么跨域非要两个端口**：`srcdoc` 与同源 iframe 都会被同源策略放行，
测不出真实的跨域形态。端口不同即不同源，这是最小代价的真跨域方案。
`frame.html` 在主端口访问会返回 404 并说明原因 —— 防止有人误用同源 iframe 还以为测得通。

---

## 四、媒体方案

`.wav` 由 `serve_mock.py` 按 `?d=<秒>` **现场合成**（8kHz / 16bit / 单声道 / 低幅 440Hz 正弦），
完整支持 HTTP Range（seek 与断点续跑依赖它）。

- M5 的全部量化断言只依赖 `paused` / `ended` / `currentTime` / `duration` —— 全部满足；
- 画面变化由 canvas 时间码叠加提供（`#十分秒` 帧号），截图差分够用；
- 好处：**仓库零二进制、服务零外部依赖**（本机无可用视频编码器）。

**弹题弹窗绝不调用 `video.pause()`。** 弹题不是媒体态，覆盖层不会让 `paused` 变真；
系统必须自己显式暂停，这正是 M5-2 要测的东西。`tests/test_mock_site.py` 对此有断言。

---

## 五、加一道题 / 改一道题

1. 改 `static/questions.json`（`answer` 填 **options 的下标**，不是标签）
2. 含自指选项（以上/都正确…）的题必须 `"shuffle": false`
3. 重跑 `python scripts/gen_traps_md.py`
4. `python -m pytest tests/test_mock_site.py`
5. `python scripts/check_mock.py --channel chrome --all`

改网课分集：改 `static/course.json` 后**必须重算 `vid`**（`core/vid.py` 的
`make_vid(course_id, episode_index, title)`），`test_course_vids_match_core_algorithm`
会逐集比对。

### 加一个**新的坑类型**（比加题麻烦，三处必须同步）

1. `static/quiz_runtime.js`（题目坑）或 `course_runtime.js`（媒体坑）里实现它；
2. `scripts/gen_traps_md.py::TRAP_INFO` 补 `(中文名, 触发条件, 期望降级层级)` ——
   **不补则 `gen_traps_md.py` 直接 `KeyError` 退出**（这是刻意的：坑位表必须与数据同源）；
3. `tests/test_mock_site.py::KNOWN_TRAPS` 加进去 —— 否则「未知坑名」断言会红；
4. 重跑 `python scripts/gen_traps_md.py`，并跑 `tests/test_mock_site.py` + `scripts/check_mock.py`。

⚠️ 若新坑会改变**「下一题」按钮的可见性**（如 `next_after_scroll`），还要检查所有
**靠点「下一题」遍历题库的测试助手**：Playwright 只把**可见**元素滚进视口，
对 `display:none` 元素等多久都不会变可见 —— 需要先滚动再点（见 `tests/helpers.py::click_next`）。

---

## 六、文件说明

```
mock_site/
├─ quiz.html            题目靶场外壳（__FRAME_ORIGIN__ 由服务端注入）
├─ course.html          网课靶场外壳
├─ frame.html           跨域 iframe 内容（只在 8900 提供）
└─ static/
   ├─ quiz_shared.js    共享构件：乱序 / 表单 / 画布 / 遮罩 / 提交
   ├─ quiz_runtime.js   页面编排 + 注入坑
   ├─ frame_runtime.js  frame 内渲染（CORS 拉题，postMessage 翻页）
   ├─ course_runtime.js 分集 / 播放 / 弹题
   ├─ questions.json    22 题地面真值（编号**稀疏**：1,4,7,…,50,51）
   ├─ course.json       6 集 + vid + duration
   ├─ mock.css          外观（类名可被 cls 坑抹掉，别依赖）
   └─ traps.md          坑位分布表（生成物）
```

`quiz_shared.js` 存在的理由：主文档与跨域 frame 必须产出**完全一致**的锚点契约与
答案口径。两份实现必然漂移，而靶场是地面真值 —— 漂移等于全部验收失效。
