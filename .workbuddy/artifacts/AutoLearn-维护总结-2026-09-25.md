# AutoLearn · 维护总结（2026-09-25 · 收尾批次）

**日期**：2026-09-25
**批次性质**：验收闭环 + 项目文档补全（本轮无新业务功能，属工程收尾）
**触发**：按第二轮验收报告（`docs/验收报告-2026-09-25-第二轮.md`）修 bug + 补 README + 归档维护总结

---

## 一、本轮做了什么

| 项 | 落点 | 结果 |
|----|------|------|
| 修复验收报告「问题 1」StarletteDeprecationWarning | `requirements-dev.txt` | ✅ 告警消除 |
| 补全项目 README | `README.md`（新建） | ✅ |
| 记录接口变更流水 | `docs/CHANGELOG-interface.md` | ✅ 追加 §P5-7 |
| 归档本维护总结 | `.workbuddy/artifacts/` | ✅ 本文件 |

### 修的 bug 详情

**现象**：`pytest` 输出 1 条 `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2 instead.`

**根因**：`starlette ≥ 1.2` 的 `TestClient` 已迁移到新库 `httpx2`（Pydantic 维护）；缺它时回退 `httpx` 并发弃用告警。

**修复**：`requirements-dev.txt` 新增 `httpx2>=2.0.0`。`httpx2` 使用独立命名空间，与运行时 `httpx`（`core/model_registry.py`、`solve/providers/base.py`、`tests/test_sse.py` 仍在用）共存无冲突。

**验证**：`pytest` **309 passed, 0 warning** · `ruff` clean · `mypy` 55 文件零问题。

> 第一轮验收报告遗留的 ruff exclude / types-PyYAML / HTTP_422 / @app.on_event 已在 P5 批次闭环；本轮闭环的是第二轮唯一残留告警。至此两份验收报告的工程质量问题全部清零。

---

## 二、质量门槛快照（重跑确认）

| 检查 | 命令 | 结果 |
|------|------|------|
| 单测 + 集成 | `.venv/Scripts/python -m pytest` | ✅ 309 passed, 0 warning |
| lint | `.venv/Scripts/python -m ruff check .` | ✅ All checks passed |
| 类型 | `.venv/Scripts/python -m mypy` | ✅ 55 source files 零问题 |
| 靶场自检 | `.venv/Scripts/python scripts/check_mock.py --channel chrome --all` | ✅ 50/50 |

---

## 三、施工现状速查（后期维护入口）

| 里程碑 / Part | 判定 | 一句话 |
|---------------|------|--------|
| T0 定义冻结 | ✅ | 8 项定义落代码常量 + 5 项单测全绿 |
| P0 契约与骨架 | ✅ | 约 60 签名可 import，stub 合入，ruff/mypy 过 |
| P1 双靶场 | ✅ | 50 题 7 类坑 + 6 集网课；check_mock 50/50 |
| M0 靶场 + DOM 通道 | ❌ 未关闭 | 靶场侧 6/9 达标；感知探针仍 stub |
| P5 UI | ⚠️ 部分 | 后端 API + SSE + 五路由已交付；前端三文件未做 |
| M1–M5（P2–P8） | ❌ 未开工 | 均为 stub |

**关键路径**：`T0 ✅ → P0 ✅ → P1 ✅ → M0 ⏸（待 P2）→ P5 后端 ✅ / 前端 ⏳ → M1–M5 ⏳`

### stub 分布速查（`NotImplementedError` 定位）

| Part | 里程碑 | 待实现模块 |
|------|--------|-----------|
| P2 | M0-3/4/5/6/7 | `perception/{dom_probe,media_probe,pipeline}.py`、`adapters/{base.py,mock_exam/adapter.py}` |
| P3 | M1 | `perception/{net_probe,vision_probe}.py`、`core/arbiter.py` |
| P4 | M2-1~4 | `solve/{voting,solver,cache}.py`、`solve/providers/{openai_compat,mock,factory}.py`（仅自指白名单已实现） |
| P6 | M3 | `act/{readback,actuator,verifier}.py`、`core/trace.py:RunLogger` |
| P7 | M4 | `core/{orchestrator,ratelimit}.py` |
| P8 | M5 | `core/tasks.py:build_task_sequence` 及媒体恢复语义 |

已实现（无 stub 残留）：`core/model_registry.py`（ModelRegistry + CredentialStore + test_connection）。

---

## 四、下一步建议（按里程碑顺序）

1. **关闭 M0 缺口（P2）**：DOM 探针 + 媒体探针 + `wait_for_interrupt()` + 适配器 `selectors.yaml` 装配。M0 未关闭前不推进 M1/M2。
2. **收尾 P5 前端**：`ui/static/{index.html, app.js, style.css}` 五区单页 + SSE 消费 + 模型面板 + 引导卡渲染。
3. **闭环待确认清单**：`docs/CHANGELOG-interface.md` 末表第 6 项（DOM 契约 → `selectors.yaml`）是 P2 前置；第 3/7 项分别需泳道 C 在 P6/P8 前回复。
4. **M2 闸门提醒**：需真实模型跑出三项指标，用户未加模型配置时顺延；期间只验 MockProvider 链路 + UI。

---

## 五、维护须知（必读）

- **改任何签名** → 更新 `docs/CHANGELOG-interface.md` 并通知全员；文件名即契约，改路径回写规划书。
- **`mock_site/static/traps.md` 勿手改**，由 `scripts/gen_traps_md.py` 从 `questions.json` 生成。
- **靶场 DOM 契约**：锚点 `data-quiz` / `data-media`，任何坑不得破坏这两组；`data-answer*` 只许 MockProvider 读。
- **密钥**只进 OS 凭据管理器（keyring → WinCred），`models.yaml` 零敏感字段。
- 每个 Part 交付「代码 + 单测 + 验收清单勾选结果」三件套。

### 硬约束（违反即出事故）

1. `submitted` 是唯一危险态：续跑只回读、绝不重新点击。
2. 提交动作不重试、不重放，超时暂停等人。
3. 禁用 `networkidle`；禁用 `full_page=True` 截图。
4. 媒体态读 `paused`/`ended`/`currentTime`/`duration`，不用元素可见性代替。
5. 弹题不是媒体态，MutationObserver + 轮询双保险。
6. 媒体动作阶梯与元素点击相反：可信手势优先，`evaluate("video.play()")` 靠后。
7. `qid` 不含答案；选项排序后参与哈希；NFKC 归一化。
8. 自指选项禁止打乱。
9. 失败一律「暂停 + 截图 + 留档」，禁静默跳过、禁 assert 做流程控制。

---

## 六、文档体系

| 文档 | 位置 | 作用 |
|------|------|------|
| 项目任务书 | `docs/AutoLearn-项目任务书.md` | 需求与里程碑源头（v1.0） |
| 实施规划书 | `docs/AutoLearn-实施规划书.md` | 施工图：Part 拆分 / 接口契约 / 命名规范 |
| 接口变更流水 | `docs/CHANGELOG-interface.md` | 改签名必写 |
| 验收报告 ×2 | `docs/验收报告-*.md` | 历史验收快照 |
| 坑位表 | `mock_site/static/traps.md` | 50 题坑位，脚本生成 |
| 维护总结 | `.workbuddy/artifacts/` | 本文件（后期维护入口） |
