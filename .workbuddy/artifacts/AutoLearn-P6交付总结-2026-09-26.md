# AutoLearn P6 交付总结（执行层 + dry_run）

**日期**：2026-09-26 ｜ **泳道**：C 执行 ｜ **对应里程碑**：M3 ｜ **结论**：✅ 通过，关键路径前移到 P7

---

## 一、做了什么

把执行层从 stub 变成可用实现：让系统**真的动手、且能自证**。

| 模块 | 内容 |
|------|------|
| `act/readback.py` | M3-1 状态读取适配层：`checked → aria-checked → aria-pressed → class` 顺序探测，逻辑集中一处 |
| `act/actuator.py` | M3-2/3/4/6/7/8：六级升级阶梯、媒体动作、回读重放与升级、提交不重放、身份重校验、`dry_run` |
| `act/verifier.py` | M3-5 + M5 四条量化断言（时间窗断言 + 瞬时断言两类） |
| `core/trace.py` | `RunLogger` 落盘：截图 / JSON / 模型原始响应全文 |
| 测试 | 新增 5 个文件 / 72 例 + 一个内存替身层 `tests/act_helpers.py` |

**stub 清零**：`act/**` 18 处 + `core/trace.py:RunLogger` 4 处。
全库剩余 16 处 `NotImplementedError` 中 12 处业务 stub **全在 P7/P8**，另 4 处是 ABC 抽象方法占位。

---

## 二、几个关键决策（为什么这么做）

| 决策 | 理由 |
|------|------|
| **"已经达成期望状态就不动手"** —— 每次尝试前（含重放）先读一次状态 | 表面是满足 TOCTOU「临点前一刻读」，实际顺手解决两个真问题：**多选重复点击会取消勾选**、**播放键重复点击会把视频点开** |
| **动作失败 → 立刻升级；回读不一致 → 先原地重放** | 点击本身抛异常时重放同一级毫无意义（元素根本点不到）；只有「点下去了但状态没变」才按 T0-5 重放 ≤3 次再升级 |
| `_attempt.ok` **只表示输入事件发出去了** | 把「机制失败」与「状态没变」分开是 T0-5 的前提。混在一起会「元素点不到却还傻等 3 次重放」 |
| **`seek` 是媒体阶梯的唯一例外**（脚本路径第一） | `play`/`pause` 严格照 `MEDIA_LEVELS`（可信手势优先、`evaluate` 垫底，风险 #4）；但靶场进度条是纯文本 `<span>`，没有可点的 scrubber，点击路径不可能落到指定秒数。这条写进了 CHANGELOG 待媒体评审确认 |
| **`dry_run` 抛异常而不是静默跳过** | 静默跳过会让编排层误以为提交成功了 |
| **`stem_hash` 重校验做成不可绕过的入口** `Actuator.apply_answer()` | M3-7 原来只以「验收口径」形式存在，谁都能漏做；做成方法后漏做直接抛 `StemMismatchError` |
| **核心逻辑全部写成纯内存用例** | 靶场不可达时真浏览器用例会 `skip`。若把阶梯 / 重放 / dry_run / 身份校验挂在浏览器上，这些断言会在「今天没起靶场」时**静默不跑** —— 真浏览器用例只负责证明「在真页面上确实有效」 |

---

## 三、验收结果

| 项 | 结果 |
|----|------|
| pytest | ✅ **578 例：578 passed / 0 failed / 0 skipped（147.7s）**（靶场已起 + 真 Chrome） |
| ruff / mypy | ✅ 干净 / 57 文件零问题 |
| `check_mock --all` | ✅ 50/50 |
| `check_ui` | ✅ 17/17 |
| P6 九条验收口径 | ✅ 全部达标（逐条证据见 `docs/验收报告-2026-09-26-第五轮-P6.md`） |

---

## 四、验收过程额外修掉的 4 个既有缺陷（都不是 P6 引入的）

> 共同点：**都只在「靶场真跑起来 / 跑够多轮」时才暴露**，所以历次「全绿」没照到。

| # | 缺陷 | 性质 | 修复 | 实测 |
|---|------|------|------|------|
| 1 | `DomProbe.is_available()` 瞬时判定 → XHR/SPA 题**丢掉整条 DOM 通道**，白白降级到网络/视觉 | **产品 bug** | 改为在 1500ms 内等「题目根**或** frame 挂上」（与同方法 frame 分支早已存在的 1500ms 对齐），两者共用一份预算 | 修复前 15 轮 1 次 MISS → 修复后 **30 轮 0 次** |
| 2 | 测试把密钥写进**用户真实 Windows 凭据库**，累积 **1730 条**后写满 → `CredWrite` 报 `WinError 8`，**从此用户自己存密钥也失败** | **测试隔离缺陷** | 新增 `AUTOLEARN_SECRET_BACKEND=memory`；`conftest` 与 `check_ui.py` 都改用它 | 修复后跑 44 例：凭据条目 **1730 → 1730，零新增** |
| 3 | `test_connection` 读的是**新建的** `CredentialStore()`，而密钥存在 `registry.credentials()` 里 | **产品 bug** | `test_connection(profile, credentials=)`；路由传 `registry.credentials()` | 真实 keyring 进程级共享掩盖了它；内存后端（合法部署模式）上**永远无法通过 [测试连接]** |
| 4 | `POST /api/models` 密钥写失败时**配置已落 `models.yaml`**（无回滚），界面留下「配置在、密钥不在」的卡片 | **产品 bug** | `put` 失败则 `registry.remove()` 回滚 + 带 `error_code` 的 500 | `state/models.yaml` 已恢复 `profiles: []` |

**需要谁评审**：#1 泳道 A；#2/#3/#4 泳道 B / D。四处在 CHANGELOG §P6 都已标注，都是最小改动 + 真机数据。

---

## 五、遗留 / 待办

| # | 事项 | 归属 |
|---|------|------|
| 1 | 「阶梯到顶 / 提交超时」的**暂停**：执行层只发 `ok=False` + `screenshot_ref` + 事件 `pause: true`，实际挂起由编排层做 | P7 |
| 2 | 历史泄漏的 1730 条 `autolearn/*` 凭据条目**未清理** —— 与用户真实密钥同名同格式，不做脚本批删；`state/models.yaml` 为 `profiles: []` 可确认全是测试垃圾，但操作对象是 OS 凭据库，**须用户确认后单独执行** | 用户决策 |
| 3 | 「恢复位置连续」断言的端到端验证（挂起→恢复） | P8 |
| 4 | L6 视觉坐标需编排层注入 `vision_locate`（视觉识别归 P4 的 Tier2） | P7 装配 |
| 5 | `app.js` 补 `act.*` 3 条事件消费 | 泳道 D |
| 6 | M2 闸门三项指标 | 等用户配模型 |

> **上表后续状态（2026-09-26 更新，不改写当时的记录）**：
> #1 / #4 → **P7 已交付**（编排层落实现，`pause: true` 已成为真暂停）；#3 → **P8 已交付**（真机验证恢复位置连续）；
> #5 → **第九轮已交付**（运行态条 + `media.*` / `stack.*` 可视化，见 `docs/验收报告-2026-09-26-第九轮-问题修复.md`）；
> #2 → **已清理**（实测 865 条，0 失败，见第五轮验收报告 §七）；#6 仍等用户添加模型配置。

**下一步**：**P7 编排循环 + 限速 + 留痕**。P6 已备好全部旋钮
（`run_logger` / `item_id` / `bus` 注入、`apply_answer()`、`ensure_media_locatable()`、
`RunLogger(run_id, root)`、统一的 `pause: true` 信号）。

---

## 六、关键文件

```
act/readback.py  act/actuator.py  act/verifier.py  core/trace.py
core/enums.py  core/models.py          # ErrorCode +4 / ActionResult.screenshot_ref
core/model_registry.py  ui/routes/models.py   # 缺陷 #2 #3 #4 的修复
perception/dom_probe.py                # 缺陷 #1 的修复
tests/{act_helpers,test_readback,test_actuator,test_verifier,test_dry_run,test_run_logger}.py
docs/{CHANGELOG-interface.md §P6, 验收报告-2026-09-26-第五轮-P6.md}
```

**文档**：`docs/验收报告-2026-09-26-第五轮-P6.md`（逐条验收证据）、
`docs/CHANGELOG-interface.md` §P6（14 项契约增量 + 4 处既有缺陷修复）、
`docs/AutoLearn-实施规划书-v2.0-进度版.md`（已升 v2.3）、`README.md`（新增「执行层约定」）。
