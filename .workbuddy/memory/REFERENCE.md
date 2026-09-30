# AutoLearn 参考手册（按模块查）

> 本文件是 `MEMORY.md` 的细节配套：**只在要动对应模块时才读**。日期日志（`2026-09-25/26/27/28/29.md`）是更原始的记录。
>
> **2026-09-30 增量（开局判定）**：新增 `core/run_plan.py::derive_plan` → `RunPlan`；
> `core/advance_library.py::PLAN_PREFERENCE` 只在**开局**用一次（不再是运行期降级顺序）；
> 推进/提交全程按方案走、**不换方式**；提示词剩 5 份（见 §B5）。**先读 §0 收口 + §F 速查。**
>
> **2026-09-28 重读校准（v0.2.0）**：项目已从「视觉 + DOM 双通道」改为「**只使用模型**」。
> 本文件里凡是提到 DOM 读题 / `selectors.yaml` / 通道优先级 / 六级点击阶梯的段落，
> **只作历史参考，不要再照做** —— 详见 §B0。
>
> **2026-09-29 增量**：**Tier 分级整体删除**（`TierUsed` → `SolvePath`；`solve.escalated`、
> `tier2_min_votes`、`should_escalate_to_tier2` 一并删）。§E 里所有「Tier1/Tier2」段落**只作历史参考**。
> 新增：`core/advance_library.py`（推进方式库）、`solve/training.py`（训练模式）、
> `core/trace.py::run_dir/purge_run_cache`（任务缓存）、`prompts/33-推进方式库.md`、`prompts/34-训练总结.md`。
> ⚠️ 另外：`docs/CHANGELOG-interface.md` / `channel-bench.md` / `degrade-heatmap.md` **已永久删除**，
> 本文件与源码里「见 CHANGELOG §xxx」的指路现在**是断的**（历史记录，以 README + tests 为准）。

## 0. 2026-09-30 收口（四条修复 + 为什么）

1. **开局判定一次，之后全程照它走**：`core/run_plan.py::derive_plan(batch, *, batch_size=0)`
   把视觉组的观测裁决成唯一一份 `RunPlan`（推进方式恰好一种 / 推进几何 / 提交范围 / 总题数）。
   删掉的是运行期的换方式降级、「滚回顶部重读一次」、「这一屏没有提交按钮就中途暂停」、
   「每题问模型下一题在哪」。**为什么**：每一步临时判断都可能一次跳过十几道题，
   而漏题的代价不对称（几乎没人会发现）。
2. **视觉组只做观测、解题组只吃文本**：读图请求恒返回同一份固定格式
   `{"page": {...}, "questions": [...], "more_below", "note"}`；解题组请求里 `images` 恒为空；
   选项顺序与标号一律照页面原样。**为什么**：控制流不能交给看不见程序的模型（每次说法可能不同）；
   标号一旦错位，下游从输出上看不出来。
3. **置信度两种口径 + 有界重发**：不复算（`sample_n == 1`）用模型自报 `confidence`，
   <`guards.confidence_review_min`（0.5）→ ⚠复核停下；复算用多数票占比。
   「回复不可用」→ 有界重发（`SAMPLE_RETRY_MAX`=2，每条采样项 ≤3 次请求）；
   「显式空作答」= 模型的结论，不重发。**为什么**：单样本一致率恒 1.0、不带信息；
   混成一件事故障只在不复算时暴露。
4. **回读三态 + 墨迹上「没变化」不重点**：`changed`(≥2.0) / `weak`(≥0.05) / `none`；
   `ink_centroid` 落点点完 `none` → `ok=True` + `no_change_on_ink`（绝不重点）；
   几何兜底落点 `none` → 换候选点（换点前先重测）；全部 `none` → `_exhausted`（`ok=False` + 截图 + pause）。
   **为什么**：画面本来就没什么可变时，旧布尔判据会判「没点中」→ 反复换点、乱点一气，
   多选上还会把刚选上的勾取消。

## A. 打包与桌面化（改 `launcher.py` / `AutoLearn.spec` / `assets/` 必读）
- `launcher.py`：单进程起靶场(8899/8900) + UI(8800) + **pywebview 原生桌面窗口**；`main()` 先 `chdir` 到 exe 所在目录，让 `state/`/`logs/` 落位。uvicorn 与靶场都在 daemon 线程，主线程跑 webview 事件循环；`_wait_ui_ready()` 先轮询端口再开窗。
- `serve_mock` 必须**静态 import**（spec 里 `pathex=["scripts"]`），不能用 `importlib` 按路径加载 —— 后者让 PyInstaller 追踪不到 `wave`/`http.server` → 靶场线程 `ModuleNotFoundError`。`serve_mock._load_questions` 已改实例方法读注入的 `self.root`（原 staticmethod 读模块级 `SITE_ROOT`，`__file__` 失效后题库接口 500）。
- 资源收集：`ui/static`/`mock_site`/`adapters/*.yaml` 走 `--add-data`；`certifi`/`playwright` 走 `collect_data_files`（官方 hook 带 driver，含 node.exe 约 103MB）；`uvicorn`/`keyring`/`keyring.backends` 全量 `collect_submodules`（动态导入静态分析看不到）。复用系统 WebView2，不捆绑浏览器内核（pywebview 6.2.1 + pythonnet 3.1.0，有 cp313 wheel）。
- **空白窗口头号坑**：注入 GUI 进程的第三方 DLL（WPS `qingnse64.dll`）与 Chromium 沙箱冲突 → msedge.dll CHECK 崩溃（0x80000003 同地址复现），http 页永远白屏而 `data:` 正常。修复：开窗前设 `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--disable-features=CalculateNativeWinOcclusion --no-sandbox`。
- **关窗即退**：CLR 收尾极慢（10s+）→ `window.events.closed += lambda: os._exit(0)`（FormClosed 同步触发）+ start() 后兜底；此钩子使窗口关闭后的代码永不执行。
- **PyInstaller `--clean` 触发 safe-delete 拦截**阻塞打包 → 手动删 `build/AutoLearn` `dist/AutoLearn` 后**不带** `--clean` 重跑。产物 `dist/AutoLearn/AutoLearn.exe`（11MB，含 `_internal/` 共 161MB）；冷启动验收三端口全 OPEN + UI/health/题库/frame 全 200。
- 排障利器：`webview.settings['REMOTE_DEBUGGING_PORT']` + playwright `connect_over_cdp` 页内取证（`evaluate_js` 从自建线程调用会挂死，别用）。
- spec 增量：icon datas + `EXE icon=` + hiddenimports `webview`/`webview.platforms.winforms`/`clr`（hook-clr 自动收 Python.Runtime.dll/ClrLoader.dll）。

## B. UI / 任务接口（改 `ui/static/` / `ui/store.py` / `ui/runner.py` 必读）

### B0. v0.2.0「只使用模型」破坏性变更（**先读这节**）

题目侧已**没有第二条通道**，以下东西全部删除、**不要再引用**：

| 删掉的 | 原用途 |
|---|---|
| `perception/dom_probe.py` / `perception/net_probe.py` | DOM 读题 / XHR 取题面 |
| `act/readback.py` + `read_state()` | 回读 `checked → aria-checked → aria-pressed → class` |
| `adapters/mock_exam/selectors.yaml` | 题目侧锚点（适配器现在**只有媒体侧** YAML） |
| `ProbeOrder` / `ProbeMode` / `PROBE_CHAINS` / `CAPABILITIES` / `modes_for` | 「DOM 优先 / 模型优先 / 通道模式」整套选择 |
| `actuator` 的六级**点击**阶梯 `LEVELS` | 题目侧改按归一化坐标点（媒体阶梯保留，仍是冻结契约） |
| `verify_readback` / `verify_submit` / `recheck_stem_hash` / `StemMismatchError` | 题目 DOM 断言 / 执行前重读题干比对 |
| `TargetKind` 能力矩阵与 `TARGET_CHANNEL_UNSUPPORTED` | 目标类型不再决定「信哪条通道」，只决定**怎么拿到画面** |

**留下的（关键）**：
- `core/config.py::active_probe_chain(cfg)` 恒返回 `[ProbeName.VISION]`（保留函数只为「只有一条通道」这件事有唯一定义点）。
- `perception/media_probe.py`：网课媒体态 + 弹题探测 + 分集目录 —— **唯一仍读页面结构的地方**。
- `act/screen.py`（新增）：`NormBox` / `ImageSize` / `norm_box_center()` / `region_bounds()` /
  `region_mean_abs_diff()` / `shot_viewport()`（转调 `VisionProbe`，不另写一份）。
- `core/run_plan.py`（2026-09-30 新增）：`derive_plan` / `grid_geometry` / `card_cell_box` /
  `all_done` / `plan_summary` / `CARD_GRID_TOLERANCE` —— **开局裁决**的唯一实现。
- `core/advance_library.py`：`ADVANCE_LIBRARY` / `strategy_for` / `PLAN_PREFERENCE`（开局优先级）。
- `solve/reader.py`：只有 `READ_SYSTEM_PROMPT` 一个提示词；入口 `read_questions` / `read_question`
  （兼容）/ `parse_read_batch` / `parse_read_payload`（兼容）/ `parse_page_view` / `gate_read_result`。
- `RunConfig` 去掉 `probe_order` / `probe_mode`；`RunConfigOut` 去掉 `available_modes`；
  向导第 4 步不再有「通道模式」下拉。
- **预检收紧**：以前只有「模型优先」才要求有支持视觉的模型，现在**任何**运行都必须有
  （没有模型就完全跑不了）→ 预检抛 `TaskStartError` / 400 + 引导码。

### B1. 信息架构（P13 起）
- **主页**只有两块：**任务**（主角，一任务一张卡）+ **模型库**；点任务卡进 **任务详情**。
- **任务详情**顺序固定：头部信息 → 运行态条 → 「需要你决定」→ **任务过程** → 题目/分集 → **日志放最后**。
  （`scripts/check_ui.py` 会断言 `#detail-body .section` 的最后一个 id 是 `section-logs`。）
- **新建任务五步向导**：任务名 → 目标 → **两次模型选择**（判题 + 视觉识别）→ 运行选项 → 确认。
  每步一个 `.why` 说明块；第 3 步模型库为空时给**「去添加模型」**按钮。
- **目标与模型都不是必填**（不选目标 → 内置靶场；模型库空 → Mock）。硬拦截只留给
  「任务序列为空」这种**配置自相矛盾**。
- 界面按 `RunConfigOut.available_modes`（`core.targets.modes_for()`）**渲染**通道选项：
  选「应用程序」→ 只剩「仅视觉」、`DOM 优先` 置灰并说明原因、`接管启动浏览器` 隐藏。

### B2. 接口（**任务与条目分两层，别混**）
| 层 | 路径 |
|----|------|
| 任务（run） | `GET/POST /api/tasks`、`GET/PATCH/DELETE /api/tasks/{run_id}`、`POST /api/tasks/{run_id}/{start,pause,resume,stop}` |
| 条目（item） | `GET /api/items`、`GET /api/items/{item_id}`、`POST /api/items/{item_id}/confirm`、`GET /api/items/{item_id}/artifacts[/name]` |

- `item_id` = `f"{run_id}-{qid}"`（**运行内标识**；`task_item.item_id` 是全局主键）。
- `TaskOut.status` 是**展示态 key**（`created`/`running`/`paused`/`finished`/`stopped`/`error`），
  由 `ui/store.display_status()` 从库里的 `status_raw` 投影 —— **前端按 key 选样式，别解析 `status_raw`**。
  `running` 但无活跃编排器 → `(stopped, "已中断（进程已退出）")`。
- **`ui/runner.py` 是任务生命周期的唯一实现**，`POST /api/tasks/{id}/start` 与兼容的
  `POST /api/run/start` 共用它；预检失败 → 400 + `error_code` + `next_action`（界面据此给引导卡）。
- `POST /api/tasks` 的配置补丁用 **`exclude_unset`**（显式 `null` = 明确清空）；`PUT /api/run/config` 仍是 `exclude_none`。
- 配置**快照进任务**（`run.config_json`），启动用任务自己的快照，不用当前草稿。
- 运行配置里新增的**视觉识别模型**：`RunConfig.vision_profile_id`（留空 = 与判题共用）。

### B3. 界面约定
- **提交模式开关**：向导第 4 步 `name="wizard_auto_apply"`（manual/auto）。**`auto_apply` 只决定
  「要不要等人点头」，不影响 T0-3 `⚠复核` 必停与执行失败必停 —— 开关旁必须写这句话**。
- **运行态条** pill `#rs-media`/`#rs-interrupt`/`#rs-stack`：**栈深用 `stack.*` 事件的 `depth`，不自增**，
  重连从 `/api/run/progress.stack_depth` 还原（SSE 增量断线不补发）。
- **前端两条纪律**：① 模型只能从 `/api/models` 选（下拉不许手打 `profile_id`）② 每个空状态/置灰都要有下一步出口。
- **`style.css` 必须有全局 `[hidden] { display: none !important }`**（作者样式的 `display` 会压过浏览器默认规则）。
- **前端零构建**：`ui/static/{index.html, app.js, style.css}` 三文件封顶（+ `landing.html` 起始页）。
  **改完必须跑 `scripts/check_ui.py`**（真 Chrome，DOM id 是契约；`node --check` 查不出未声明变量）。
- **自检隔离**：`check_ui.py` 在 import `ui` **之前** `os.environ.setdefault(...)` 隔离四个落盘位置
  （run_config / models / log_root / db），否则断言会被用户真实数据写脏。

### B4. 开局判定 / 推进 / 收尾（改这几处必读）

**现在的三步**：**开局读一屏 → 程序裁决唯一一份 `RunPlan` → 逐题解题、全程照方案走**；
推不动或做满总数时，**再由视觉组读一屏确认一次「是否全部完成」**。
P14 那套启发式末题判定与 P15 那套「开局标定 + 找不到控件再问」都已推倒。

| 落点 | 说明 |
|------|------|
| `core/run_plan.py::derive_plan` | 开局裁决：观测 + 本次读到的题数 → `RunPlan`（一定给一个**可执行**的方式） |
| `core/models.py::RunPlan` | `method` / `total` / `current` / `control_box`+`control_label` / `card`+`card_step`+`card_origin`+`card_anchor` / `submit_scope`+`submit_box` / `reason` / `raw`；`knows_total`、`reached_total(done)` |
| `core/advance_library.py::PLAN_PREFERENCE` | 裁决优先级 `(CARD, CLICK, SCROLL, SWIPE)` —— **只在开局读一次** |
| `core/enums.py::AdvanceMethod` | `click` / `card` / `swipe` / `scroll` / `unknown`（`unknown` 只是裁决前占位） |
| `core/enums.py::SubmitScope` | `question`（每题提交一次）/ `paper`（整卷、全部做完后交一次） |
| `core/models.py::PageView` | 视觉组的**观测**块：`progress`/`total`/`current`/`next_control{box,label}`/`card{box,cols,rows,current_box,next_box}`/`submit{box,scope}`/`completed`/`scrolling`/`reason` |
| `core/orchestrator.py` | `_plan_run` / `_advance` / `_run_advance_strategy` / `_advance_by_card` / `_advance_by_control` / `_advance_by_scroll` / `_advance_by_swipe` / `_settle_end`（收尾确认的那次读图）/ `_expected_number_ok` |
| `solve/reader.py` | `read_questions` / `parse_read_batch` / `parse_page_view`（**没有**单列的控制流提示词了） |
| 提示词 | `prompts/00-共享契约.md` + `10-视觉组.md`（读图，含 `page` 观测块）；**5 份**见 §B5 |
| 事件 | `advance.calibrated`（方案：`method`/`total`/`current`/`scope`/`summary`/`reason`）· `advance.completion_check`（`completed`/`observed`/`progress`/`reason`，`trigger` = `reached_total` 或 `stuck`） |
| 错误码 | `ErrorCode.ADVANCE_FAILED`（推进不动、题号对不上、确认不了完成，共用） |
| 旋钮 | `guards.advance_calibrate`(true) / `advance_confirm_*`（收尾确认总开关，默认 true） / `advance_scroll_step_ratio`(0.5) / `advance_scroll_max_steps`(12) / `advance_scroll_recover_max`(2) |

**八条纪律**（每条都对应一种真实错法）：

1. **裁决一定会给出可执行的方式**；`total` 不是正整数就按「**不知道**」→ `reached_total()` **永远 False**
   —— 不能拿「不知道」当「做完了」。
2. **问不成 = 未确认**：没有 provider / 截图失败 / 解析失败 / `page.completed` 不是 `all_done`
   → 一律 False → 停下等人。这是**唯一防跳题的闸门**。
3. **同一题同一触发点只问一次**（`_completion_checks` 缓存，键 `(qid, trigger)`），
   触发点只有 `reached_total` 与 `stuck`。
4. **裁决失败不终止任务，但一个坐标都不点**：`_plan = None` → 推进直接进收尾确认
   （没配视觉模型的人也能跑到「明确的暂停理由」，而不是崩）。
5. **推进严格按方案**：`_run_advance_strategy` 只走 `plan.method` 那一支，**不换第二种方式**；
   `CARD` 的落点由 `card_cell_box` 纯算术推算，**算不出来返回 `None` → 不点**。
6. **推进后必须校题号**：`_expected_num` 与读回的 `num_text` 不符 → `advance=wrong_question` +
   `advance_failed` 停下。这是防「静默跳题」的最后一道。
7. **滚动必须有界**（0.5 步长 / 12 步 / 回滚 ≤2），滚轮优先（虚拟列表只认真实 `wheel`）；
   滚动到位 = **滚一步 → 读一屏 → 确认新题进来了**。
8. **整卷提交只在 `_completion_confirmed` 之后**，且 `_paper_submitted` 只置一次（**绝不重放**）；
   看不到提交按钮时按范围处置（`paper` → 提示手动交卷；`question` → 停）。

### B5. 提示词（`prompts/*.md`，唯一真源）

提示词从 `.py` 内联字符串搬到仓库根 `prompts/*.md`，由 `solve/prompt_files.py` 装载：

| 文件 | 谁读 |
|------|------|
| `00-共享契约.md` | 两个模型都读（分工 / 中间数据结构 / LaTeX / qid / 不确定时的处置） |
| `10-视觉组.md` | 视觉组（抄录 / 题型 / 几何 / 画面边界 / **`page` 观测块** / Pass A·B 复核） |
| `20-解题组.md` | 解题组（输出契约 / 四条硬约束 / `confidence` / 无法作答时的行为） |
| `33-推进方式库.md` | **只给训练模式与文档看**（`library_prompt_text()`），**不进模型请求** |
| `34-训练总结.md` | 训练模式（`solve/training.py`）的总结用提示词 |

- **导入时读一次** → 改完必须**重启服务**；`prompts/` 与 `.md` 都已计入 `ui.code_rev()`，
  所以 `check_server.py` 能判出「改了没生效」。
- **缺文件大声失败**（`PromptFileMissingError`）：空提示词不报错，只会让模型自由发挥 ——
  最贵的一种失败。
- 打包：`AutoLearn.spec` 的 `datas` 已收 `("prompts", "prompts")`；漏了会让打包版直接跑不起来。
- 一次请求**只喂一个输出契约**：读图 = `00` + `10`，解题 = `00` + `20`。
  旧的「控制流」提示词已随运行期裁决一起删除 —— 现在读图与收尾确认**共用**同一份固定格式。

### B6. 关机 / 端口清理（P16，改这几处必读）

「关机」= 收掉本程序**自己起**的一切，最后 `os._exit`。顶栏右侧「⏻ 关机」是它的入口。

| 落点 | 说明 |
|------|------|
| `ui/system.py` | `parse_netstat_listening`（纯函数）/ `collect_occupants` / `build_plan` / `apply_shutdown`。判定与 I/O **全部可注入**（`kill` / `exit_fn` / `sleep_fn`），单测因此不必真杀进程 |
| `ui/routes/system.py` | `GET /api/system/status`（**预览，只读**）、`POST /api/system/shutdown?force=`（有任务跑 → 409） |
| `target/browsers.py` | `managed_processes()` / `shutdown_managed()` —— 自管浏览器登记表 `_MANAGED`，由 `launch()` 登记、`shutdown()` 注销 |
| 前端 | `#btn-shutdown` → `#shutdown-dialog`（清单来自后端）→ `#shutdown-veil`；断言在 `scripts/check_ui.py` 第 8 组 |
| 端口名单 | `MANAGED_PORTS = (8800, 8899, 8900)`。**调试端口不进去**：它随自管浏览器进程一起没 |

四条纪律（每条都对应一次真实错法）：

1. **只关确定的**：端口在名单 + PID ≠ 自己 + exe 与本进程解释器一致，三条齐了才动手；
   否则归 `foreign` 只报告。**别加「按端口盲杀」**。
2. **虚拟环境陷阱**：进程镜像报的常是 `.venv` 背后的基础解释器 → 比 `own_executables()`
   那一**组**路径，不是 `sys.executable` 一条（实测：只比后者会让 `run.bat` 起的靶场被判成外人，
   端口一个都不释放）。
3. **先回执后动手**：`BackgroundTasks` + 0.35s 宽限 + 4s 兜底硬退。
4. **前端用探活判成败**：响应常被进程退出截断，fetch 失败 ≠ 关机失败。

> ⚠️ `scripts/check_ui.py` **不点「确认关闭」**（自检与 uvicorn 同进程，点了会把自己关掉）；
> 收尾路径由 `tests/test_system_shutdown.py` 的 26 条纯内存用例覆盖。

## C. 网课场景 / 媒体流程（改 `core/orchestrator.py` / 媒体必读）- 场景由 `cfg.task_sequence` 决定：含 `video` → 网课循环（分集驱动），否则刷题循环（题目驱动）。起始页由 `ui.assembly.default_start_url(cfg)` 裁决。
- **中断-恢复**：弹题到来 → **显式暂停** → 读媒体态 → 压栈 → 处理弹题（复用题目主循环那条路）→ 弹栈 → 回读位置连续（≤2s）→ 恢复播放。
- **`PAUSE_LEVELS` 与 `MEDIA_LEVELS` 是两套**：弹题弹窗是全屏遮罩（`position:fixed; inset:0`）**正好盖住播放按钮**，纯点击路径必然超时 → 暂停走「键盘 → 脚本 → 点击垫底」；播放仍走点击优先（`play()` 需要可信手势）。`SEEK_LEVELS` 例外：脚本第一（靶场无 scrubber）。
- **回读紧贴提交**：弹题面板提交后 ~400ms 就被移除，提交级限速（5~15s）必须放在**回读之后**。
- **`ENDED_GRACE_S`**：`currentTime ≥ duration − ε` 比 `ended` 早 ε，"接近片尾"只作**待定**，给同刻到达的弹题 1s 露头窗。
- **`media_position` 是课程级进度**（表无 `run_id`）：跨运行继承，「不重播已完成集」是 M5-5 的设计意图。做干净对照验收要换 `--db` 或清表。
- 续跑：`_prune_stale_frames` 丢弃「父集已 ended / 子任务已了结」的过期帧；帧复用（先弹再压）而不是叠层。
- 靶场所有分集的弹题固定用同一道题（`?quiz=21`），而 `(run_id, qid)` 唯一 → 「每集都弹题」的跑批会在第二集按 `interrupt_already_answered` 必停。
- 验收工具：`scripts/run_course.py --dur N --interrupt-at 秒|end --limit N --run-id X --db Y`。

## D. 靶场 DOM 契约（改靶场必读）

> **v0.2.0 的边界**：题目锚点 `data-quiz` 在**产品侧已无人消费**（题目改由截图 + 模型读），
> 但靶场**仍然保留**这组属性 —— `scripts/check_mock.py`（靶场自检）与 `MockProvider`
> （读 `data-answer` 地面真值）还在用。**不要再为它们写题目选择器 / 适配器锚点**。
> 媒体锚点 `data-media` **仍在用**（`adapters/mock_exam/selectors_media.yaml`），不要动。

- 题目锚点 `data-quiz`：question/stem/stem-canvas/options/option/option-text/input/submit/next/result/figure/spacer/placeholder/modal/frame
- 媒体锚点 `data-media`：video/episode-list/episode/next/interrupt/interrupt-panel/play-button/progress/overlay
- 任何坑（含 cls 类名混淆）**都不得破坏这两组锚点**；适配器一律锚锚点，**禁用 `.qz-*` 类名**。
- `data-answer`（呈现标号）/ `data-answer-texts`（正文）**只许 MockProvider 读**；iframe 题的锚点在 frame 内。
- 跨域靠**双端口**（不同端口=不同源）；`srcdoc` 与同源 iframe 不算真跨域。
- `traps.md`（`mock_site/static/`）由 `scripts/gen_traps_md.py` 生成，**勿手改**。
- **题库 P14 起是 22 题**（编号**稀疏**：`1,4,7,8,15,21,22,24,26,27,28,30,33,36,39,40,42,44,46,49,50,51`），
  刻意保留不连续编号来验「题号不连续也得能读」。坑位覆盖：
  spa 4 / lazy 3 / canvas 5 / iframe 3 / cls 3 / modal 3 / xhr 2 / **`next_after_scroll` 3**，
  `self_ref` 3 / `image` 2 / `truncated` 1。
  **断言一律按 `len(questions)` 推导，禁止写死题数**（写死 50 的三处断言已改）。
- **坑 `next_after_scroll`（P14）**：非末题「下一题」被 1200px 占位推到首屏之外、**滚到才出现**；
  **末题则永不出现**并显示「已是最后一题」。加在题 `8`/`15`/`51` 上，`51` 位于文件末尾 →
  默认全量跑的「最后一题」就走「永不出现」那条分支；`?seq=8,15` 一次覆盖两条。
  **只推「下一题」，不动提交按钮**（一个坑只测一件事）。
  末题分支（按钮永不出现 + 「已是最后一题」）现在是**收尾确认**的靶子：
  推进全失败 → 视觉组确认「已完成」→ 干净收工。
  ⚠️ 加坑必须同步三处：`quiz_runtime.js::installScrolledNext`、`gen_traps_md.py::TRAP_INFO`、
  `test_mock_site.py::KNOWN_TRAPS`，并重跑 `gen_traps_md.py`。
  ⚠️ 靠点「下一题」遍历题库的助手必须**先滚再点**（`tests/helpers.py::click_next`）——
  Playwright 只把**可见**元素滚进视口，对 `display:none` 等多久都不会变可见。
- 靶场参数：`?seq=21,22,23` `?seed=7` `?q=21` `?interrupt_at=30|end` `?dur=8`

## E. 技术栈 / 工作流 / 关键数字
- 技术栈（不得随意替换）：Python 3.13 + Playwright(≥1.49) + FastAPI/uvicorn + pydantic(-settings) + httpx + pyyaml + pillow + keyring。**已移除 typer**，CLI 用 argparse。SQLite 落盘状态与任务栈。版本 **0.2.0**（见 `pyproject.toml`）。
- **v0.2.0 坐标口径**（改执行 / 校验前必读）：截图一律 `scale="css"` → **只乘一次图像尺寸**
  （图像像素 == 视口 CSS 像素），**不除 `devicePixelRatio`**；换算点只有 `act/screen.py::norm_box_center()`。
  一律**视口截图**，`full_page=True` 被守门单测禁掉。
- **v0.2.0 每次任务的结构性模型调用**（2026-09-30 起）：
  开局读一屏 ×1（`orchestrator._plan_run`，**同一次调用既产出方案、也产出首屏的题**）、
  收尾确认 ×1（`_settle_end` 里读一屏，看 `page.completed`），
  外加**每题/每屏一次读图**（`read_questions`）。读题**本身就是发图** ——
  0.1.0 那条「锚点站点读题零模型调用」的便宜路已随 DOM 通道消失。
  同屏多题走 `_pending_reads` 复用几何，**不再多花调用**。
- dev 依赖含 `httpx2`（starlette≥1.2 的 TestClient 已迁到它）；运行时 HTTP 仍用 `httpx`，命名空间独立共存。
- 契约先于实现：签名冻结后先提交 `NotImplementedError` stub，下游并行开工。文件名即契约，改路径要回写规划书。**改任何签名 → 更新 `docs/CHANGELOG-interface.md`**。每个 Part 交「代码 + 单测 + 验收清单」三件套。
- 文档体系：任务书 / 实施规划书（项目资产 + `docs/`）、`docs/CHANGELOG-interface.md`、`mock_site/static/traps.md`。
- **核对次数**：`MIN_SAMPLE_N = 1`、`RunConfig.sample_n = 1`（`recalculate=False` 时恒按 1 处理）。
  ⚠️ `tier2_min_votes` **已随 Tier 分级删除**（`GuardThresholds` 里没有它，守门单测钉着），
  别再按它算地板。
- P12 推进旋钮（`GuardThresholds`）：`advance_visual_fallback` / `advance_swipe_mode`(mouse|touch) / `advance_swipe_directions`(默认 `("left","up")`) / `advance_swipe_distance`(0.6) / `advance_swipe_duration_ms`(240) / `advance_change_timeout_ms`(1500) / `advance_change_poll_ms`(150)。
- 开局裁决 / 收尾旋钮（`GuardThresholds`）：`advance_calibrate`(true) / `advance_confirm_*`(true，收尾确认总开关) /
  `advance_scroll_step_ratio`(**0.5**) / `advance_scroll_max_steps`(**12**) / `advance_scroll_recover_max`(2) /
  `confidence_review_min`(0.5) / `agreement_accept`(0.8)。旧的 `end_*` 与 `advance_scroll_probe` /
  `advance_scroll_settle_polls` 已随 P14 一并删除。
- 计数护栏：事件总数 **28**（`tests/test_t0_definitions.py::EVENT_COUNT`；2026-09-29 删 `solve.escalated`
  又加 `training.done`，仍 28）、建表数 **6**（`DB_TABLE_COUNT`；`run_flag` 表已移除）
  —— 加事件/加表必须同步这两个常量。`AdvanceMethod` 现有 5 个取值（含 `card`）。
- M2 闸门（唯一强制止损点，须真实模型跑出）：单选 Top-1 ≥0.85、多选完全匹配 ≥0.75、一致率≥0.8 的题占比 ≥0.80。
- 置信度与复核：**复算**时多数票一致率 ≥`agreement_accept`(0.8) 直接用，<0.8 → ⚠复核必停；
  **不复算**时用模型自报 `confidence`，<`confidence_review_min`(0.5) → ⚠复核必停
  （模型没报就回落到一致率 = 1.0，**不新增暂停**）。回读不一致换点/落点处置见 §B4 与 `MEMORY.md` §4。
- 限速：click 间 200~600ms、提交间 5~15s、模型并发 ≤2(免费)/≤3(付费)，均带抖动。进度断言：播放 3s 内 Δt ≥1.0s；暂停 3s 内 Δt ≤0.2s；恢复位置偏差 ≤2s。

## F. 速查（2026-09-30 口径）

### F1. 文件 → 职责

| 文件 / 符号 | 职责 |
|---|---|
| `core/run_plan.py::derive_plan` | **开局裁决**：观测 + 本次题数 → `RunPlan`（唯一一份） |
| `core/run_plan.py::grid_geometry` / `card_cell_box` | 答题卡网格 → 原点/步距；按题号算格子的归一化框（算不出 → `None`） |
| `core/run_plan.py::all_done` | 观测是否**明确** `all_done`（收尾闸门的唯一放行条件） |
| `core/run_plan.py::plan_summary` | 方案的一句话（进事件流与界面） |
| `core/advance_library.py::PLAN_PREFERENCE` | 裁决优先级 `(CARD, CLICK, SCROLL, SWIPE)`（只在开局用） |
| `core/advance_library.py::strategy_for` | 方式 → `AdvanceStrategy`（`label` / `needs_coords`），认不出一律给 `UNKNOWN` 那条 |
| `core/models.py::RunPlan` / `PageView` / `PageCard` / `PageSubmit` | 方案 / 观测块 / 答题卡 / 提交按钮 |
| `core/orchestrator.py::_plan_run` | 开局：读一屏 → 裁决 → 发 `advance.calibrated` |
| `core/orchestrator.py::_settle_end` | 推不动 / 做满 → 收尾确认（`all_done` 才收工） |
| `core/orchestrator.py::_submit_paper_once` / `_submit_and_confirm` | 整卷收尾提交（只在确认完成后、绝不重放）/ 提交 + 回读 |
| `solve/reader.py::read_questions` / `parse_page_view` / `gate_read_result` | 读一屏 / 解析 `page` 观测 / 门禁（`vision_incomplete`、`vision_uncertain`） |
| `solve/solver.py::SAMPLE_RETRY_MAX` / `_confidence_of` | 有界重发上限(2) / 两种置信度口径 |
| `act/verifier.py::region_change_state` | `mad` → `changed`/`weak`/`none` |
| `act/actuator.py::select_option` | 按落点分处置（`ink_centroid` 上 `none` 绝不重点） |
| `core/trace.py::RunLogger` | 留痕：`logs/<run_id>/<item_id>/{perception,vision_read,solve,action,verify}.json` + run 级 `events.jsonl` |

### F2. 关键常量

| 常量 | 值 | 含义 |
|---|---|---|
| `core/run_plan.py::CARD_GRID_TOLERANCE` | `0.10` | 推算出的答题卡格子允许超出可见区域的比例 |
| `act/verifier.py::REGION_CHANGE_MIN_MAD` | `2.0` | ≥ 此值 → `changed` |
| `act/verifier.py::REGION_CHANGE_WEAK_MAD` | `0.05` | ≥ 此值（且 <2.0）→ `weak`；< 此值 → `none` |
| `solve/solver.py::SAMPLE_RETRY_MAX` | `2` | 「回复不可用」的有界重发次数（每条采样项最多 3 次请求） |
| `core/config.py::GuardThresholds.confidence_review_min` | `0.5` | 不复算时自报 `confidence` 低于它 → ⚠复核停下 |
| `core/config.py::GuardThresholds.agreement_accept` | `0.8` | 复算多数票一致率低于它 → ⚠复核停下 |
| `core/advance_library.py::PLAN_PREFERENCE` | `(card, click, scroll, swipe)` | 开局裁决优先级（唯一一处） |
| `core/config.py::MIN_SAMPLE_N` | `1` | 每题核对次数下限（`recalculate=False` 时恒 1） |
| `test_t0_definitions.py::EVENT_COUNT` / `DB_TABLE_COUNT` | `28` / `6` | 事件总数 / 建表数 |
| `GuardThresholds.advance_scroll_step_ratio` / `_max_steps` / `_recover_max` | `0.5` / `12` / `2` | 滚动步长 / 步数上限 / 越界回滚上限 |

### F3. 常用命令

```bash
export AUTOLEARN_SECRET_BACKEND=memory
export AUTOLEARN_BROWSER_CHANNEL=chrome            # 默认 msedge
.venv/Scripts/python scripts/serve_mock.py &       # 靶场（8899 + 8900），测试前须常驻
.venv/Scripts/python -m pytest -q --junitxml=.pytest-tmp/report.xml -p no:cacheprovider
.venv/Scripts/python -m ruff check . ; .venv/Scripts/python -m mypy
.venv/Scripts/python scripts/check_ui.py
.venv/Scripts/python scripts/check_server.py       # 判控制台是否连着旧进程
# 说明：scripts/check_read.py 已于 2026-09-30 改到新契约（READ_SYSTEM_PROMPT / parse_read_batch），
#       现在起得来：`python scripts/check_read.py --help` 退出码 0（要真读一屏仍需浏览器 + 视觉模型）
```

### F4. 暂停原因 → 处理动作

| `run.paused` 理由 | 处理动作 |
|---|---|
| `vision_read_failed` | 选一套支持图片的模型并「测试连接」；查 `vision_read_raw.txt` 与后台日志 |
| `vision_read_empty` | 核对画面（题目被切/未加载）→ 滚进视野后恢复 |
| `vision_incomplete` | 题面残缺：核对遮挡/裁剪，调整窗口或滚动位置后恢复 |
| `vision_uncertain` | 模型拿不准：看 `vision_read.json` 的框与文本，确认它没看错画面 |
| `advance_failed` | 手动推进一次再恢复；或把控件/答题卡滚进视野重跑；确认真做完了就自己交卷 |
| `vision_no_submit_box` | 整卷 → 手动点一次「交卷/提交作业」；每题 → 核对画面后重跑 |
| `submit_timeout` | 看截图确认卡在哪（禁用/确认框/验证码）→ 手动处理后恢复（**不自动重放**） |
| `needs_confirm`（**必停复核**） | 在「需要你决定」里确认答案（事件 `solve.review_required` / `task.needs_confirm`；半自动模式每题也会停在这里） |
| `question_did_not_advance` | 又读到同一道题：「下一题」没生效 → 手动推进后恢复 |
| `action_failed` / `vision_geometry_missing` / `vision_box_missing` | 执行失败或几何缺失：核对画面后重跑（**不猜坐标**） |
| `perception_failed` | 感知层双失败（无具体视觉理由时的兜底）→ 查截图与后台日志 |
