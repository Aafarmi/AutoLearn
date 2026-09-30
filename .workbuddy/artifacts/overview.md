# AutoLearn · 收尾批次概览（2026-09-25）

**批次**：验收闭环 + 文档补全
**范围**：修第二轮验收报告 bug + 补 README + 归档维护总结

---

## 做了什么

1. **修复 bug**：消除 `pytest` 的 `StarletteDeprecationWarning`（starlette 的 TestClient 已迁移到 httpx2，缺它则回退 httpx 并告警）。
   - 修复：`requirements-dev.txt` 新增 `httpx2>=2.0.0`（独立命名空间，与运行时 httpx 共存）。
   - 验证：`pytest` 309 passed **0 warning** · `ruff` clean · `mypy` 55 文件零问题。
2. **补全 `README.md`**：架构总览、目录结构、技术栈、安装运行、质量门槛、里程碑现状、硬约束、靶场 DOM 契约、文档体系、开发约定。
3. **变更流水**：`docs/CHANGELOG-interface.md` 追加 §P5-7 记录本次修复。
4. **归档维护总结**：`.workbuddy/artifacts/AutoLearn-维护总结-2026-09-25.md`（施工现状 + stub 分布 + 维护须知 + 下一步）。

## 关键结论

- 两份验收报告的工程质量问题**全部清零**（ruff exclude / types-PyYAML / HTTP_422 / @app.on_event / StarletteDeprecationWarning）。
- 当前质量门槛：pytest 309 / ruff clean / mypy 55 / check_mock 50/50。
- 施工现状：T0 ✅ P0 ✅ P1 ✅ · M0 ⏸（待 P2）· P5 后端 ✅ 前端 ⏳ · M1–M5 ⏳。

## 下一步

1. P2 关闭 M0 缺口（DOM/媒体探针 + `selectors.yaml`）。
2. P5 前端三文件（五区单页）。
3. 闭环待确认清单第 6 项（DOM 契约）作为 P2 前置。

> 详细维护手册见 `AutoLearn-维护总结-2026-09-25.md`。
