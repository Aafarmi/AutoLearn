# AutoLearn 项目现状重读报告

> 重读时间：2026-09-28 18:38 ~ 19:00 · 触发：用户「项目已经修改过了，重新阅读」
> 结论一句话：**项目已从「视觉模型 + DOM 双通道」演进为 v0.2.0「只使用模型」** ——
> 题目侧的 DOM / 网络通道整套删除，读题只剩「截图 → 模型 → 归一化坐标 → 按坐标点 → 截图差分」一条路。

---

## 1. 这次到底改了什么（与上一轮记忆的差异）

上一轮（09-28 白天，记在 `.workbuddy/memory/2026-09-28.md`）结束时项目还是
「P15 四项改造 + DOM 通道仍在」。本轮重读发现**这之后还有一次破坏性重构**：

| 维度 | 上一轮认知（P15 时代） | 现在（v0.2.0） |
|---|---|---|
| 版本 | 0.1.0 系 | **0.2.0**（`pyproject.toml`） |
| 读题通道 | 视觉 + DOM 双通道，可选「DOM 优先 / 模型优先 / 通道模式」 | **只有视觉一条**；`active_probe_chain()` 恒返回 `[VISION]` |
| 已删除文件 | — | `perception/dom_probe.py`、`perception/net_probe.py`、`act/readback.py`、`adapters/mock_exam/selectors.yaml` |
| 已删除概念 | — | `ProbeOrder` / `ProbeMode` / `PROBE_CHAINS` / `CAPABILITIES` / `modes_for`「通道选择」整套 |
| 题目侧点击 | 六级阶梯（L1 locator.click → … → L6 视觉坐标） | **没有阶梯**：一律按模型给的归一化坐标点（`L6_VISION_XY`） |
| 题目侧校验 | DOM 回读 `checked` / `aria-checked` | **截图差分**（只证明「像素变了」） |
| 新增 | — | `act/screen.py`（坐标与截图的唯一口径点）、`scripts/export_heatmap.py`、`tests/test_heatmap.py`、`docs/真实站点作业做题手册.md`、`docs/双模型分工-视觉组与解题组.md` |
| 读题质量门禁 | 无 | **新增**：`ReadResult.clipped/uncertain/more_below` + `gate_read_result()` |
| 配置加载 | 会抛异常 | **`load_run_config()` 永不抛异常**（修掉「控制台打不开」的 500） |

**保留的例外**：网课/媒体侧**仍然解析页面结构**（读 `<video>` 的 `paused` / `currentTime` / 分集索引）。
准确的说法是「不再用页面结构读**题**」，而不是「从不接触页面结构」——README 自己把这条边界写清楚了。

## 2. 现在这条链路长什么样

```
截视口图（scale="css"，禁 full_page）
   └─▶ 视觉组模型读题 read_question() → ReadResult（题干 / 选项文本 / 每个选项的归一化框 + submit_box / next_box）
          └─▶ 门禁 gate_read_result()
                ├─ clipped / uncertain 非空 → 拦下，带 vision_incomplete / vision_uncertain 暂停问人
                └─ 放行 → 解题组模型作答（不看图）
                        └─▶ 按归一化坐标点击 select_option(box, size, qtype)
                              └─▶ 截图差分 verify_region_changed() 校验
```

三处**结构性**模型调用（每题之外）：开局标定 `calibrate_task`（总数 + 推进方式）→
按需找推进控件 `find_advance_control` → 收尾确认 `confirm_completion`。

**v0.2.0 的两个代价**（README 明确写出来，不是缺陷而是设计取舍）：
1. 每题都是一次模型调用 —— 0.1.0 那条「锚点站点读题零调用」的便宜路没了。
2. 读错题在下游**不可见**（模型回的是结构完好的 JSON）→ 这是全链路唯一「错了也不报错」的环节，
   所以读题门禁成了必需品而非优化。

## 3. 施工现状与门槛（本轮实测）

| 检查 | 结果 |
|---|---|
| `ruff check .` | ✅ All checks passed |
| `mypy` | ✅ 68 source files，零问题 |
| **全量 `pytest`（靶场常驻 8899+8900 + 真 Chrome）** | ✅ **896 passed / 0 failed / 0 errors / 0 skipped**（368s） |
| 契约计数守卫 | ✅ 事件 **28**、数据库表 **6**（`run` / `task_item` / `answer` / `suspend_frame` / `level_stat` / `media_position`） |
| `scripts/check_ui.py` | ✅ **90/90**（22s，真 Chrome） |

> 本轮实测的 896 与 README 记载的基线**完全一致**，可交叉印证文档没有虚报。
> 另：`.pytest-tmp/` 里留着 14:28~14:59 的分阶段报告（`baseline.xml` 915 / `final_all.xml` 937）——
> 那是**重构中途**的快照，用例数后来随 `tests/` 的改动落回 896，不要拿来当现行基线。

**里程碑**：T0 / P0~P9 / P11 目标采集 / P12 推进阶梯 / P13 任务化重构 / P15 四项改造 / **v0.2.0 重构** 全部 ✅；
P14（靠正则猜末题）被 P15 **整套推倒**，现为「视觉组开局标定 + 收尾确认」。

**未交付**：① 站点适配向导（不做完抓不了真实站点）② 桌面窗口视觉识别（同一条链路，只差真机验证）
③ 填空 / 简答写回路径（执行器只会点选项，需单独拍板）。

## 4. 重读中发现的三个问题（均未擅自修，等你定夺）

### 4.1 `docs/` 断链（影响面最大）

README「文档体系」表与 **约 20 个源码文件**（`core/config.py` / `core/models.py` / `core/events.py` /
`solve/providers/base.py` / `scripts/package.py` …）都写着「见 `docs/CHANGELOG-interface.md` §xxx」，
但该文件**已不在仓库**。同类缺失还有：

| 缺失文件 | 谁在引用 |
|---|---|
| `docs/CHANGELOG-interface.md` | README + ~20 个源码/文档（**改动签名必写的那份**） |
| `docs/AutoLearn-实施规划书-v2.0-进度版.md` | README「开工前先看这份」 |
| `docs/验收报告-*.md` / `docs/交付摘要-2026-09-27-P11修订.md` | README「文档体系」表 |
| `docs/channel-bench.md` / `docs/degrade-heatmap.md` | README + 两个脚本的默认产出路径（脚本仍可再生成） |

`docs/` 现在只剩 4 份：任务书 / 实施规划书 / 双模型分工 / 真实站点作业做题手册。

### 4.2 README 内还有 P14 残留（与同一份文档的 v0.2.0 叙述自相矛盾）

- 第 376 行仍列着 `POST /api/tasks/{run_id}/last-question`（**代码里已无此路由**）；
- 第 338 行界面结构里仍写着「这是最后一题吗（运行级）」（**前端已删，只剩 CSS 注释**）。

### 4.3 文档版本号与代码不同步（轻微）

README 顶部多处按「v0.2.0」叙述，`docs/AutoLearn-实施规划书.md` 也以「v0.2.0」标注改动，
但任务书（`docs/AutoLearn-项目任务书.md`）的修订记录未同步这次重构。

## 5. 验证记录

- `ruff` / `mypy` / `pytest`：见 §3。
- `check_ui.py`（真 Chrome，前端 DOM id 是契约）：✅ **90/90**（22s，与 README 基线一致；
  其中已断言「旧事件 `advance.last_question` 随 P14 机制一并移除」「`advance.calibrated` / `advance.completion_check` 在」）。
- 靶场以**常驻后台**方式启动（8899 主 + 8900 跨域 frame），确保真浏览器用例**不是 skip**。

## 6. 建议的下一步

1. **先定 `docs/` 断链怎么处理**（恢复 CHANGELOG / 改指路为 README §里程碑 / 明确作废）——
   这是唯一影响「以后回来改代码的人」的问题，其余都是文档措辞。
2. 清 README 里的 P14 残留两处（改动很小）。
3. 补做 **M2 闸门真实模型数据**（`MockProvider` 不得作依据，这是唯一强制止损点）。
4. 真实站点适配向导 —— 不做完「抓取正在用的网页」这条主线走不通
   （`docs/真实站点作业做题手册.md` §7 已把差距列清：同页多题推进、填空/简答写回、整卷提交风险）。
