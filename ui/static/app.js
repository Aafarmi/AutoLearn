/* ============================================================================
   AutoLearn 控制台（P13 重构）

   信息架构
   --------
   主页 = **任务**（主角）+ **模型库**；点任务进入**任务详情**（过程 / 待办 / 条目 / 日志）。
   新建任务走五步向导：任务名 → 目标 → 模型（判题 + 视觉）→ 运行选项 → 确认。

   两条前端纪律
   1. **模型选择只来自模型库**：下拉里出现的必须是 `/api/models` 里的东西。
      让用户手打 profile_id 等于让他猜，而且一旦填错要在运行中才炸。
   2. **"现在能做什么"必须有出口**：每个空状态、每个禁用按钮旁边都要有下一步动作。
      置灰是引导，不是静默失败。
   ========================================================================== */
"use strict";

/* ------------------------------ 常量 ------------------------------ */
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const now = () => new Date().toLocaleTimeString("zh-CN", { hour12: false });

const STATE_LABELS = {
  pending: "待处理", perceived: "已感知", solved: "已求解",
  pending_confirm: "待确认", applied: "已应用", submitted: "已提交",
  verified: "已验证", failed: "失败", skipped: "跳过",
  idle: "空闲", playing: "播放中", paused: "已暂停",
  interrupted: "被打断", resumed: "已恢复", ended: "已结束",
};

const TYPE_LABELS = { quiz: "题目", video: "分集" };
// 作答路径标签。**只有一种求解模式** —— 原先的 Tier1 / Tier2 分级已删除，
// 现在只剩「正常作答 / Mock（无模型自检）/ 缓存命中」三种来源。
const SOLVE_PATH_LABELS = { single: "正常", mock: "Mock", cache: "缓存" };
const TARGET_KIND_LABELS = { browser_page: "网页", desktop_window: "应用程序" };
/* 执行阶梯（ActLevel）。题目侧 v0.2.0 起恒为 l6_vision_xy；
   其余几级由网课的媒体控件使用。 */
const LEVEL_LABELS = {
  l1_locator: "锚点定位",
  l2_force: "强制点击",
  l3_scroll: "滚动",
  l4_focus_keys: "键盘",
  l5_bbox: "包围盒",
  l6_vision_xy: "模型坐标",
};

const ACTIVITY_LEVEL = {
  "run.error": "error",
  "solve.review_required": "warn",
  "act.submit_timeout": "warn",
  "act.readback_mismatch": "warn",
  "run.finished": "success",
  "run.started": "success",
  "advance.calibrated": "info",
  "advance.completion_check": "warn",
};

/* ------------------------------ 状态 ------------------------------ */
const state = {
  view: "home",          // home | task
  tasks: [],
  models: [],
  presets: [],
  config: null,
  targets: { targets: [], hint: null, alive: false, endpoint: null },
  activeRunId: null,
  running: false,
  taskId: null,          // 当前打开的任务
  taskDetail: null,
  editingModelId: null,
  shutdownStatus: null,  // GET /api/system/status 的最近一次回执
  //: 批量删除用：勾选了哪些任务（run_id）。**只存 id**，渲染时再与列表对齐 ——
  //: 存对象会在刷新后留下幽灵项。
  pickedTasks: new Set(),
  // 日志常驻内存：切视图不该把已产生的日志丢掉
  logs: [],
};

/* ------------------------------ 工具 ------------------------------ */
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/** 极简 Markdown：转义后仅处理 **粗体** 与 `行内代码`。够用，且不引入 XSS 面。 */
function mdInline(text) {
  return esc(text)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString("zh-CN", { hour12: false });
}

function shortId(id, n = 18) {
  return id ? String(id).slice(0, n) : "—";
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const ct = res.headers.get("content-type") || "";
  let body = null;
  try {
    body = ct.includes("application/json") ? await res.json() : await res.text();
  } catch { body = null; }

  if (!res.ok) {
    const detail = (body && body.detail) || {};
    const err = new Error(detail.message || `HTTP ${res.status}`);
    err.status = res.status;
    err.code = detail.error_code;
    err.nextAction = detail.next_action;
    throw err;
  }
  return body;
}

function handleApiError(err, quiet = false) {
  console.warn(err);
  if (quiet) return;
  if (err.nextAction) {
    showBanner("error", `${esc(err.message)} —— ${mdInline(err.nextAction)}`);
  } else {
    toast(err.message || "请求失败", "error");
  }
}

let toastTimer = null;
function toast(msg, kind = "info") {
  let el = $("#toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast";
    document.body.appendChild(el);
  }
  el.className = "toast" + (kind === "success" ? " toast--success"
    : kind === "error" ? " toast--error" : "");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 2600);
}

function showBanner(kind, html) {
  const el = $("#global-banner");
  el.className = "global-banner" + (kind === "error" ? "" : " global-banner--info");
  el.innerHTML = html;
  el.hidden = false;
  clearTimeout(showBanner._t);
  showBanner._t = setTimeout(() => { el.hidden = true; }, 12000);
}

/* ------------------------------ 视图切换 ------------------------------ */
function showView(name) {
  state.view = name;
  $("#view-home").classList.toggle("is-active", name === "home");
  $("#view-task").classList.toggle("is-active", name === "task");
  $("#btn-home").hidden = name === "home";
  $("#btn-new-task").hidden = name === "task";
}

async function openTask(runId) {
  state.taskId = runId;
  showView("task");
  $("#detail-empty").hidden = true;
  $("#detail-body").hidden = false;
  $("#detail-body").innerHTML = '<div class="empty"><p class="empty__hint">正在读取任务…</p></div>';
  await refreshTaskDetail();
  renderLogs();
}

function goHome() {
  state.taskId = null;
  showView("home");
  refreshTasks().catch(() => {});
}

/* ============================ ① 主页：任务 ============================ */
async function refreshTasks() {
  const data = await api("/api/tasks");
  state.tasks = data.tasks || [];
  state.activeRunId = data.active_run_id;
  state.running = !!data.running;
  renderTasks();
  renderRunningHint();
}

function renderRunningHint() {
  const el = $("#running-hint");
  const active = state.tasks.find((t) => t.active);
  if (!active) { el.hidden = true; return; }
  el.hidden = false;
  el.className = "pill pill--accent";
  el.textContent = "正在跑：" + active.name;
}

function renderTasks() {
  const list = $("#tasks-list");
  const empty = $("#tasks-empty");
  $("#tasks-count").textContent = String(state.tasks.length);
  // 列表刷新后把已经不在的选中项剔掉 —— 不然「删除选中」会带上幽灵 id，
  // 后端只能回一句「任务不存在」，用户看着莫名其妙。
  const alive = new Set(state.tasks.map((t) => t.run_id));
  for (const id of [...state.pickedTasks]) if (!alive.has(id)) state.pickedTasks.delete(id);

  if (!state.tasks.length) {
    list.innerHTML = "";
    empty.hidden = false;
    empty.innerHTML = `
      <p class="empty__icon" aria-hidden="true">＋</p>
      <p class="empty__title">还没有任务</p>
      <p class="empty__hint">
        任务就是「抓一次目标、跑一遍完整流程」。点下面的按钮开始 ——
        向导会一步步带你选目标、选模型、设选项，不懂的地方每步都有说明。
      </p>
      <div class="empty__cta">
        <button type="button" class="btn btn--primary btn--lg" data-act="new-task">＋ 新建第一个任务</button>
      </div>`;
    syncPickUi();
    return;
  }
  empty.hidden = true;

  list.innerHTML = state.tasks.map((t) => {
    const pct = t.total ? Math.round((t.done / t.total) * 100) : 0;
    const seq = (t.task_sequence || []).map((s) => TYPE_LABELS[s] || s).join(" + ") || "—";
    // 「重试」只对**已开始、没在跑、也没跑完**的任务有意义。
    const canRetry = !t.active && t.status !== "finished" && t.status !== "created";
    return `
      <li class="task-row">
        <label class="task-pick" title="选中以便批量删除">
          <input type="checkbox" data-pick="${esc(t.run_id)}"
                 ${state.pickedTasks.has(t.run_id) ? "checked" : ""}
                 aria-label="选中任务 ${esc(t.name)}">
        </label>
        <div class="task-card status--${esc(t.status)}${t.active ? " is-active-task" : ""}"
             data-task="${esc(t.run_id)}" role="button" tabindex="0">
          <div class="task-card__top">
            <span class="task-card__name">${esc(t.name)}</span>
            <span class="status-tag status--${esc(t.status)}">
              <span class="status-dot" aria-hidden="true"></span>${esc(t.status_label)}
            </span>
          </div>
          <div class="task-card__meta">
            <span>目标：<b>${esc(TARGET_KIND_LABELS[t.target_kind] || t.target_kind || "—")}</b></span>
            <span>序列：<b>${esc(seq)}</b></span>
            <span>判题：<b>${esc(t.judge_model || "Mock")}</b></span>
            <span>创建：<b>${esc(fmtTime(t.created_at))}</b></span>
          </div>
          <div class="task-card__progress">
            <div class="bar"><div class="bar__fill${t.status === "finished" ? " bar__fill--done" : ""}"
                 style="width:${pct}%"></div></div>
            <div class="task-card__stats">
              <span>进度：<b>${t.done}/${t.total}</b></span>
              ${t.failed ? `<span>失败：<b>${t.failed}</b></span>` : ""}
              ${t.pending_confirm ? `<span>待确认：<b>${t.pending_confirm}</b></span>` : ""}
              <span class="task-card__go">查看详情 →</span>
            </div>
          </div>
          <div class="task-card__ops">
            ${canRetry ? `<button type="button" class="btn btn--sm btn--ghost"
                     data-act="retry-task" data-target="${esc(t.run_id)}">↻ 重试</button>` : ""}
            <button type="button" class="btn btn--sm btn--ghost"
                    data-act="delete-task" data-target="${esc(t.run_id)}">删除</button>
          </div>
        </div>
      </li>`;
  }).join("");
  syncPickUi();
}

/** 同步「全选 / 删除选中」两个控件的显隐与文案（列表一变就要调一次）。 */
function syncPickUi() {
  const total = state.tasks.length;
  const picked = state.pickedTasks.size;
  $("#pick-all-wrap").hidden = total === 0;
  const all = $("#pick-all");
  all.checked = total > 0 && picked === total;
  // 半选态：部分勾选时「全选」框显示成横杠，而不是骗人的对勾。
  all.indeterminate = picked > 0 && picked < total;
  const button = $("#btn-bulk-delete");
  button.hidden = picked === 0;
  button.disabled = picked === 0;
  button.textContent = `删除选中 (${picked})`;
}

/* ============================ ② 主页：模型库 ============================ */
async function refreshModels() {
  try {
    state.models = await api("/api/models");
  } catch { state.models = []; }
  renderModels();
}

/**
 * 生成模型下拉的选项。
 *
 * @param {string} selected 单选时选中的 profile_id
 * @param {string[]} selectedMany 多选时已选的 profile_id（**按数组顺序即降级顺序**）
 * @param {boolean} allowEmpty 是否带「不选」那一项。多选的空态由「一项都不勾」表达，
 *   再放一个空字符串选项反而会让人以为必须选它。
 */
function modelOptions(selected, selectedMany = null, allowEmpty = true) {
  const many = Array.isArray(selectedMany) ? selectedMany : null;
  const opts = [];
  if (allowEmpty && !many) {
    opts.push('<option value="">（不选 · 自动用 Mock 或与解题组共用）</option>');
  }
  for (const m of state.models) {
    if (!m.enabled) continue;
    const vision = m.capabilities && m.capabilities.supports_vision ? " · 支持视觉" : "";
    const isOn = many ? many.includes(m.profile_id) : m.profile_id === selected;
    opts.push(
      `<option value="${esc(m.profile_id)}"${isOn ? " selected" : ""}>` +
      `${esc(m.name)}（${esc(m.model)}${vision}）</option>`);
  }
  return opts.join("");
}

/** 读多选框的值，**按 DOM 顺序**（= 用户勾选后元素在列表里的顺序）。 */
function multiValues(selector) {
  return $$(`${selector} option:checked`).map((el) => el.value).filter(Boolean);
}

/* ------------------------- 备用组：勾选列表 ------------------------- */
/**
 * 备用组用**勾选列表**而不是 ``<select multiple>``。
 *
 * 为什么换掉原生多选：原生多选在桌面浏览器上要按住 Ctrl / ⌘ 才能选第二个，
 * 用户点第二下时第一个就被取消 —— 看起来就是「勾选根本没生效」。
 * 这里用普通复选框，并把**勾选先后**记进 ``pickOrder``，因为「顺序即降级顺序」
 * 是这个控件唯一的语义。
 */
const pickOrder = {};

function paintBackupPicker(containerId) {
  const el = $(containerId);
  if (!el) return;
  const order = pickOrder[containerId] || (pickOrder[containerId] = []);
  const usable = state.models.filter((m) => m.enabled);
  if (!usable.length) {
    el.innerHTML = '<p class="form__hint">模型库为空 —— 先在「模型库」里添加一套。</p>';
    return;
  }
  el.innerHTML = usable.map((m) => {
    const idx = order.indexOf(m.profile_id);
    const vision = m.capabilities && m.capabilities.supports_vision ? " · 支持视觉" : "";
    return `<label class="pick${idx >= 0 ? " is-on" : ""}" data-id="${esc(m.profile_id)}">` +
      `<input type="checkbox" value="${esc(m.profile_id)}"${idx >= 0 ? " checked" : ""} />` +
      `<span class="pick__order">${idx >= 0 ? idx + 1 : ""}</span>` +
      `<span class="pick__name">${esc(m.name)}（${esc(m.model)}${vision}）</span>` +
      `</label>`;
  }).join("");
}

function renderBackupPicker(containerId, selected) {
  const known = new Set(state.models.filter((m) => m.enabled).map((m) => m.profile_id));
  // 保留用户已勾选、且模型还在的项；顺序沿用已记录的先后
  const kept = (pickOrder[containerId] || []).filter((id) => known.has(id));
  for (const id of Array.isArray(selected) ? selected : []) {
    if (known.has(id) && !kept.includes(id)) kept.push(id);
  }
  pickOrder[containerId] = kept;
  paintBackupPicker(containerId);
}

/** 备用组当前勾选值，**顺序 = 勾选先后**（第一个勾的排最前）。 */
function backupValues(containerId) {
  const checked = new Set($$(`${containerId} .pick input:checked`).map((el) => el.value));
  return (pickOrder[containerId] || []).filter((id) => checked.has(id));
}

function onBackupToggle(box) {
  const list = box.closest(".pick-list");
  if (!list) return;
  const key = `#${list.id}`;
  const order = pickOrder[key] || (pickOrder[key] = []);
  const id = box.value;
  if (box.checked) {
    if (!order.includes(id)) order.push(id);
  } else {
    const at = order.indexOf(id);
    if (at >= 0) order.splice(at, 1);
  }
  paintBackupPicker(key);
}

function visionBadge(caps) {
  if (!caps) return '<span class="cap">未实测</span>';
  const items = [
    caps.auth_ok ? '<span class="cap cap--ok">鉴权 OK</span>'
      : '<span class="cap cap--off">鉴权失败</span>',
    caps.supports_vision ? '<span class="cap cap--ok">支持视觉</span>'
      : '<span class="cap cap--off">不支持视觉</span>',
    caps.supports_structured_output ? '<span class="cap cap--ok">结构化输出</span>'
      : '<span class="cap cap--off">无结构化输出</span>',
  ];
  if (caps.max_qps) items.push(`<span class="cap">≈${esc(caps.max_qps)} QPS</span>`);
  return items.join("");
}

function renderModels() {
  const list = $("#models-list");
  const empty = $("#models-empty");
  $("#models-count").textContent = String(state.models.length);

  if (!state.models.length) {
    list.innerHTML = "";
    empty.hidden = false;
    empty.innerHTML = `
      <p class="empty__icon" aria-hidden="true">◈</p>
      <p class="empty__title">模型库是空的</p>
      <p class="empty__hint">
        新建任务时要在这里挑「判题模型」和「视觉识别模型」。
        先添加一套 —— 需要一个 OpenAI 兼容的接口地址、模型名和 API Key。
        密钥只写进系统凭据管理器，界面不回显。
      </p>
      <div class="empty__cta">
        <button type="button" class="btn btn--primary" data-act="add-model">＋ 添加模型</button>
      </div>`;
    return;
  }
  empty.hidden = true;

  list.innerHTML = state.models.map((m, i) => `
    <div class="model-card${m.enabled ? "" : " is-disabled"}" data-model="${esc(m.profile_id)}">
      <div class="model-card__top">
        <span class="model-card__name">${esc(m.name)}</span>
        ${m.enabled ? "" : '<span class="badge">已停用</span>'}
        <span class="badge">${i + 1}</span>
      </div>
      <div class="model-card__url">${esc(m.base_url)} · ${esc(m.model)}</div>
      <div class="model-card__caps">
        ${visionBadge(m.capabilities)}
        ${m.has_api_key ? '<span class="cap cap--ok">密钥已存</span>' : '<span class="cap cap--off">无密钥</span>'}
      </div>
      <div class="model-card__actions">
        <button type="button" class="btn btn--sm" data-act="test" data-id="${esc(m.profile_id)}">测试连接</button>
        <button type="button" class="btn btn--sm" data-act="edit" data-id="${esc(m.profile_id)}">编辑</button>
        <button type="button" class="btn btn--sm" data-act="up" data-id="${esc(m.profile_id)}"${i === 0 ? " disabled" : ""}>↑</button>
        <button type="button" class="btn btn--sm" data-act="down" data-id="${esc(m.profile_id)}"${i === state.models.length - 1 ? " disabled" : ""}>↓</button>
        <button type="button" class="btn btn--sm btn--danger" data-act="delete" data-id="${esc(m.profile_id)}">删除</button>
      </div>
    </div>`).join("");
}

/* ============================ ③ 新建任务向导 ============================ */
const WIZARD_STEPS = 5;
const wizard = { step: 1 };

function openWizard() {
  wizard.step = 1;
  $("#wizard-name").value = "";
  $("#target-select").value = "";
  state.targets = { targets: [], hint: null };
  renderTargetOptions();
  resetWizardOptions();
  refreshWizardModels();
  $("#btn-launch-browser").hidden = false;
  gotoWizardStep(1);
  $("#wizard").hidden = false;

  // 顺手告诉用户默认名会是什么
  api("/api/tasks").then((d) => {
    const n = (d.tasks || []).length + 1;
    $("#wizard-name").placeholder = `留空 = 自动取「任务${n}」`;
  }).catch(() => {});
}

function closeWizard() { $("#wizard").hidden = true; }

function resetWizardOptions() {
  const cfg = state.config || {};
  const recalc = !!cfg.recalculate;
  $("#wizard-recalc").checked = recalc;
  $("#wizard-sample-n").value = Math.max(2, Number(cfg.sample_n) || 3);
  $("#wizard-training").checked = !!cfg.training;
  syncRecalcUI();
  const seq = cfg.task_sequence || ["quiz"];
  $("#wizard-task-quiz").checked = seq.includes("quiz");
  $("#wizard-task-video").checked = seq.includes("video");
  $$('input[name="wizard_auto_apply"]').forEach((el) => {
    el.checked = el.value === (cfg.auto_apply ? "auto" : "manual");
  });
}

/** 复算关掉时，次数输入必须**置灰** —— 否则用户会以为改它有用。 */
function syncRecalcUI() {
  const on = $("#wizard-recalc").checked;
  const group = $("#wizard-recalc-n-group");
  const input = $("#wizard-sample-n");
  if (group) group.classList.toggle("is-muted", !on);
  if (input) {
    input.disabled = !on;
    if (on && (Number(input.value) || 0) < 2) input.value = "3";
  }
}

function refreshWizardModels() {
  const cfg = state.config || {};
  $("#wizard-judge").innerHTML = modelOptions(cfg.model_profile_id || "");
  $("#wizard-vision").innerHTML = modelOptions(cfg.vision_profile_id || "");
  renderBackupPicker("#wizard-judge-backup", cfg.backup_profile_ids || []);
  renderBackupPicker("#wizard-vision-backup", cfg.vision_backup_profile_ids || []);
  $("#wizard-models-empty").hidden = state.models.length > 0;
}

function gotoWizardStep(step) {
  wizard.step = Math.min(Math.max(step, 1), WIZARD_STEPS);
  $$("#wizard-steps .step").forEach((el) => {
    const n = Number(el.dataset.step);
    el.classList.toggle("is-current", n === wizard.step);
    el.classList.toggle("is-done", n < wizard.step);
  });
  $$(".wizard-pane").forEach((el) => {
    el.classList.toggle("is-active", Number(el.dataset.step) === wizard.step);
  });
  $("#btn-wizard-prev").disabled = wizard.step === 1;
  refreshWizardNextLabel();
  if (wizard.step === 3) refreshWizardModels();
  if (wizard.step === WIZARD_STEPS) renderWizardSummary();
}

function refreshWizardNextLabel() {
  const btn = $("#btn-wizard-next");
  if (wizard.step !== WIZARD_STEPS) { btn.textContent = "下一步 →"; return; }
  btn.textContent = $("#wizard-autostart").checked ? "创建并启动" : "只创建任务";
}

function collectWizard() {
  const seq = [];
  if ($("#wizard-task-quiz").checked) seq.push("quiz");
  if ($("#wizard-task-video").checked) seq.push("video");
  const kindEl = $('input[name="wizard_target_kind"]:checked');
  return {
    name: $("#wizard-name").value.trim(),
    target_kind: kindEl ? kindEl.value : "browser_page",
    target_id: $("#target-select").value || null,
    config: {
      // 复算开关决定 sample_n 有没有意义：关掉时后端恒按 1 次取样处理。
      recalculate: $("#wizard-recalc").checked,
      sample_n: Math.max(2, parseInt($("#wizard-sample-n").value, 10) || 3),
      training: $("#wizard-training").checked,
      task_sequence: seq,
      auto_apply: $('input[name="wizard_auto_apply"]:checked').value === "auto",
      model_profile_id: $("#wizard-judge").value || null,
      vision_profile_id: $("#wizard-vision").value || null,
      // 四组里的两个备用组：**勾选列表**，顺序即降级顺序（勾选先后）
      backup_profile_ids: backupValues("#wizard-judge-backup"),
      vision_backup_profile_ids: backupValues("#wizard-vision-backup"),
      // 显式带上（含 null）：后端按「传了的字段」合并，null 才能表达
      // 「这次就是不用目标 / 不用模型」，而不是沿用上次的。
      target_kind: kindEl ? kindEl.value : "browser_page",
      target_id: $("#target-select").value || null,
    },
  };
}

/**
 * 每一步「能不能往下走」。返回**硬拦截**的提示语或 ``None``。
 *
 * 两条刻意不拦的：
 * - **目标不是必填项**：不选目标时后端退回内置靶场（``ui.assembly.DEFAULT_START_URL``），
 *   这正是「先跑一遍看流程」的用法。做成硬门槛会把这个用法堵死。
 * - **模型不是必填项**：**建任务**不需要模型 —— 建完再回去加配置、然后启动，
 *   是正常用法；「模型库为空」这件事由第 5 步的提示条说清（启动会被后端拦下）。
 *   在这里硬拦会让「先建个任务、稍后配模型」这条路走不通。
 *
 * 唯一真正拦的是**任务序列为空**：那是配置自相矛盾，后端也会拒绝。
 */
function wizardBlocker(step) {
  const draft = collectWizard();
  if (step === 4 && !draft.config.task_sequence.length) {
    return "任务序列至少要选一个：刷题 或 网课。两个都不选，启动会被后端拒绝。";
  }
  return null;
}

async function wizardNext() {
  const step = wizard.step;
  const blocker = wizardBlocker(step);
  if (blocker) { toast(blocker, "error"); return; }

  // 目标类型是**服务端**的事（扫描 / 接管启动都按它分派），提前落盘
  if (step === 2) {
    const kind = collectWizard().target_kind;
    try {
      await api("/api/run/config", { method: "PUT", body: JSON.stringify({ target_kind: kind }) });
      state.config = await api("/api/run/config");
    } catch (e) { handleApiError(e); return; }
  }

  if (step === WIZARD_STEPS) { await finishWizard(); return; }
  gotoWizardStep(step + 1);
}

function renderWizardSummary() {
  const draft = collectWizard();
  const modelName = (id) => {
    const m = state.models.find((x) => x.profile_id === id);
    return m ? `${m.name}（${m.model}）` : "Mock（不调模型）";
  };
  const backupNames = (ids) => (ids && ids.length
    ? ids.map((id) => modelName(id)).join(" → ") : "（不降级）");
  const target = (state.targets.targets || []).find((t) => t.target_id === draft.target_id);
  const rows = [
    ["任务名", draft.name || "（自动取「任务N」）"],
    ["目标类型", TARGET_KIND_LABELS[draft.target_kind] || draft.target_kind],
    ["目标", target ? (target.title || target.url || target.target_id) : "（未选）"],
    ["视觉组", draft.config.vision_profile_id
      ? modelName(draft.config.vision_profile_id) : "与解题组共用"],
    ["视觉备用", backupNames(draft.config.vision_backup_profile_ids)],
    ["解题组", modelName(draft.config.model_profile_id)],
    ["解题备用", backupNames(draft.config.backup_profile_ids)],
    ["任务序列", draft.config.task_sequence.map((s) => TYPE_LABELS[s] || s).join(" + ")],
    ["复算", draft.config.recalculate
      ? `开启 · 每题采样 ${draft.config.sample_n} 次后投票`
      : "关闭（以第一次答案为准，每题只调一次模型）"],
    ["提交模式", draft.config.auto_apply ? "全自动" : "半自动（每题等你确认）"],
    ["训练模式", draft.config.training ? "开启（成功后自动总结经验）" : "关闭"],
  ];
  $("#wizard-summary").innerHTML = rows.map(([k, v]) =>
    `<div class="summary__row"><span class="summary__k">${esc(k)}</span>` +
    `<span class="summary__v">${esc(v)}</span></div>`).join("");

  // 「接下来会发生什么」——引导的最后一步不能只复述用户刚填的东西，
  // 要明确告诉他会落到哪条路径（靶场 / Mock / 真实模型）。
  const notes = [];
  if (!draft.target_id) {
    notes.push("没有选择目标：本次会抓**内置靶场**（本地示例页），适合先跑一遍看流程。" +
      "要抓真实网页，回第 2 步点「扫描」选一个。");
  }
  if (!state.models.length) {
    notes.push("模型库是空的：**这次起不来** —— v0.2.0 起页面只能由模型读，" +
      "启动检查会直接拦下。先回第 3 步点「去添加模型」加一套支持视觉的配置。");
  } else if (!draft.config.model_profile_id && !draft.config.vision_profile_id) {
    notes.push("两处模型都留空：会用整条降级链（模型库里排在最前的先试）。想固定用某一套，回第 3 步选上。");
  }

  const blocker = wizardBlocker(5);
  const box = $("#wizard-blocker");
  const lines = blocker ? [`⚠ ${blocker}`, ...notes] : notes;
  if (!lines.length) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = lines.map((line) => `<span class="note-line">${mdInline(line)}</span>`)
    .join("<br />");
}

async function finishWizard() {
  const draft = collectWizard();
  let created;
  try {
    created = await api("/api/tasks", {
      method: "POST",
      body: JSON.stringify({ name: draft.name || null, config: draft.config }),
    });
  } catch (e) { handleApiError(e); return; }

  closeWizard();
  await refreshTasks().catch(() => {});
  toast(`已创建「${created.name}」`, "success");

  if ($("#wizard-autostart").checked) {
    // 起不来不吞错误：把后端的引导语原样抛出来。
    try {
      await api(`/api/tasks/${created.run_id}/start`, { method: "POST" });
      toast("已开始运行", "success");
    } catch (e) {
      showBanner("error", `任务已创建，但没启动成功：${esc(e.message)}` +
        (e.nextAction ? " —— " + mdInline(e.nextAction) : ""));
    }
  }
  await refreshTasks().catch(() => {});
  await openTask(created.run_id);
}

/* ---- 抓取目标 / 接管启动（向导第 2 步） ---- */

/** 按钮文案按目标类型分派：网页模式说「抓取网页」，程序模式说「抓取窗口」。 */
function grabButtonLabel(kind) {
  return kind === "browser_page" ? "抓取网页" : "抓取窗口";
}

/**
 * 抓取目标列表。
 *
 * **必须给出反馈**（P15）：原来的实现在成功时只有下拉框悄悄变了一下 ——
 * 用户点了按钮，界面没有任何一句话告诉他「抓到了几个」「为什么一个都没有」，
 * 看上去就像按钮没反应。所以这里三件事都做：
 * ① 按钮进入进行中态；② 结果用 toast 说清数量；③ 结果摘要行落在下拉框下面。
 */
async function scanTargets() {
  const kindEl = $('input[name="wizard_target_kind"]:checked');
  const kind = kindEl ? kindEl.value : "browser_page";
  const btn = $("#btn-scan-targets");
  const label = grabButtonLabel(kind);
  btn.disabled = true;
  btn.textContent = "抓取中…";
  setGrabResult("", null);
  try {
    state.targets = await api(`/api/targets?kind=${encodeURIComponent(kind)}`);
  } catch (e) {
    handleApiError(e);
    state.targets = { targets: [], hint: null };
    setGrabResult(`抓取失败：${e.message}`, false);
    toast(`抓取失败：${e.message}`, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
  renderTargetOptions();
  const targets = state.targets.targets || [];
  if (targets.length) {
    const names = targets.slice(0, 3).map((t) => t.title || t.url || t.target_id);
    const more = targets.length > names.length ? ` 等 ${targets.length} 个` : "";
    setGrabResult(`抓到 ${targets.length} 个${kind === "browser_page" ? "网页" : "窗口"}：` +
      `${names.join("、")}${more}`, true);
    toast(`抓到 ${targets.length} 个目标，已在下面列出`, "success");
  } else {
    // **一个都没抓到也必须说话**（哪怕下面还有后端给的 hint）：
    // 用户点了按钮却什么都没发生，是最容易被误判成「软件坏了」的一种反馈缺失。
    setGrabResult(
      `一个都没抓到${state.targets.hint ? "，原因见下面那行提示" : ""}。`,
      false,
    );
    toast("一个都没抓到", "error");
  }
}

/** 把一条结果写进「抓取结果」那一行。``ok=null`` 表示清空。 */
function setGrabResult(text, ok) {
  const el = $("#target-grab-result");
  if (!el) return;
  if (!text) { el.hidden = true; el.textContent = ""; return; }
  el.hidden = false;
  el.className = "form__note " + (ok ? "form__note--ok" : "form__note--warn");
  el.textContent = text;
}

function renderTargetOptions() {
  const sel = $("#target-select");
  const targets = state.targets.targets || [];
  const keep = sel.value;
  const kindEl = $('input[name="wizard_target_kind"]:checked');
  const kind = kindEl ? kindEl.value : "browser_page";
  sel.innerHTML = [`<option value="">（未选择 · 请先点「${grabButtonLabel(kind)}」）</option>`]
    .concat(targets.map((t) => {
      const label = t.title || t.url || t.target_id;
      const host = t.app ? ` · ${t.app}` : "";
      return `<option value="${esc(t.target_id)}">${esc(label)}${esc(host)}</option>`;
    })).join("");
  if (keep && targets.some((t) => t.target_id === keep)) sel.value = keep;

  const hint = $("#target-hint");
  if (state.targets.hint) {
    hint.hidden = false;
    hint.innerHTML = mdInline(state.targets.hint);
  } else if (!targets.length) {
    hint.hidden = false;
    hint.textContent = `没抓到目标。确认目标已经打开，再点一次「${grabButtonLabel(kind)}」。`;
  } else {
    hint.hidden = true;
  }
}

async function launchBrowser() {
  const btn = $("#btn-launch-browser");
  btn.disabled = true;
  btn.textContent = "正在启动…";
  try {
    state.targets = await api("/api/targets/launch", { method: "POST" });
    renderTargetOptions();
    toast("浏览器已就绪，挑一个标签页", "success");
  } catch (e) {
    handleApiError(e);
  } finally {
    btn.disabled = false;
    btn.textContent = "接管启动浏览器";
  }
}

/* ============================ ④ 任务详情 ============================ */
async function refreshTaskDetail() {
  if (!state.taskId) return;
  try {
    state.taskDetail = await api(`/api/tasks/${state.taskId}`);
  } catch (e) {
    if (e.status === 404) { toast("任务不存在", "error"); goHome(); return; }
    handleApiError(e, true);
    return;
  }
  renderTaskDetail();
}

function renderTaskDetail() {
  const d = state.taskDetail;
  if (!d) return;
  const t = d.task;
  const items = d.items || [];
  const attention = items.filter((i) => i.state === "pending_confirm");
  const pct = t.total ? Math.round((t.done / t.total) * 100) : 0;
  const canStart = t.status === "created" && !t.active;
  const canRetry = !t.active && t.status !== "finished" && t.status !== "created";
  const retryLabel = t.status === "paused" ? "↻ 恢复/重试任务" : "↻ 重试任务";

  $("#detail-body").innerHTML = `
    <div class="detail-head">
      <div>
        <div class="detail-head__title">
          <span>${esc(t.name)}</span>
          <span class="status-tag status--${esc(t.status)}">
            <span class="status-dot" aria-hidden="true"></span>${esc(t.status_label)}
          </span>
          <button type="button" class="btn btn--sm btn--ghost" id="btn-rename-task">改名</button>
        </div>
        <div class="detail-head__sub">
          <span>创建 ${esc(fmtTime(t.created_at))}</span>
          ${t.finished_at ? `<span>结束 ${esc(fmtTime(t.finished_at))}</span>` : ""}
          <span>任务 ID <code>${esc(t.run_id)}</code></span>
        </div>
      </div>
      <div class="detail-actions">
        ${canStart ? '<button type="button" class="btn btn--primary" data-run="start">启动</button>' : ""}
        ${canRetry ? `<button type="button" class="btn btn--primary" data-retry-task="${esc(t.run_id)}">${retryLabel}</button>` : ""}
        ${t.active ? '<button type="button" class="btn" data-run="pause">暂停</button>' : ""}
        ${t.active ? '<button type="button" class="btn" data-run="resume">恢复</button>' : ""}
        ${t.active ? '<button type="button" class="btn btn--danger" data-run="stop">停止</button>' : ""}
        <button type="button" class="btn btn--danger" id="btn-delete-task">删除</button>
      </div>
    </div>

    <div class="runtime">
      <span class="pill pill--muted" id="rs-media" title="当前分集的媒体态">媒体态 —</span>
      <span class="pill pill--muted" id="rs-interrupt" title="弹题中断 / 待确认">弹题 —</span>
      <span class="pill pill--muted" id="rs-stack" title="任务栈深度">任务栈 —</span>
    </div>

    <div class="info-grid">
      <div class="info">
        <div class="info__k">进度</div>
        <div class="info__v">${t.done}/${t.total}${t.failed ? ` · 失败 ${t.failed}` : ""}${t.pending_confirm ? ` · 待确认 ${t.pending_confirm}` : ""}</div>
        <div class="bar" style="margin-top:8px"><div class="bar__fill${t.status === "finished" ? " bar__fill--done" : ""}"
             style="width:${pct}%"></div></div>
      </div>
      <div class="info"><div class="info__k">目标</div>
        <div class="info__v">${esc(TARGET_KIND_LABELS[t.target_kind] || t.target_kind || "—")} · ${esc(shortId(t.target_id))}</div></div>
      <div class="info"><div class="info__k">判题模型</div>
        <div class="info__v">${esc(t.judge_model || "Mock（不调模型）")}</div></div>
      <div class="info"><div class="info__k">视觉识别模型</div>
        <div class="info__v">${esc(t.vision_model || "与判题共用")}</div></div>
      <div class="info"><div class="info__k">任务序列</div>
        <div class="info__v">${esc((t.task_sequence || []).map((s) => TYPE_LABELS[s] || s).join(" + ") || "—")}</div></div>
      <div class="info"><div class="info__k">提交模式</div>
        <div class="info__v">${t.auto_apply ? "全自动" : "半自动"}</div></div>
    </div>

    ${attention.length ? `
    <div class="section" id="section-attention">
      <div class="section__head">
        <h3 class="section__title">需要你决定 <span class="badge">${attention.length}</span></h3>
        <span class="section__hint">半自动模式下每题都要你点头；⚠复核题两种模式都会停</span>
      </div>
      <div class="attention">
        ${attention.map((i) => `
          <div class="attention__item">
            <span class="attention__text">
              <strong>${esc(i.title || i.qid || i.item_id)}</strong>
              <span class="item-row__id"> · ${esc(i.item_id)}</span>
            </span>
            <button type="button" class="btn btn--sm" data-item="${esc(i.item_id)}">看题</button>
            <button type="button" class="btn btn--sm btn--primary" data-confirm="confirm" data-item="${esc(i.item_id)}">确认</button>
            <button type="button" class="btn btn--sm btn--danger" data-confirm="reject" data-item="${esc(i.item_id)}">否决</button>
          </div>`).join("")}
      </div>
    </div>` : ""}

    <div class="section" id="section-activity">
      <div class="section__head">
        <h3 class="section__title">任务过程</h3>
        <span class="section__hint">最新在上 · 点条目看具体题目</span>
      </div>
      <div class="activity" id="activity-stream"></div>
    </div>

    <div class="section" id="section-items">
      <div class="section__head">
        <h3 class="section__title">题目 / 分集 <span class="badge">${items.length}</span></h3>
        <span class="section__hint">点击任意条目查看截图、采样明细与执行层级</span>
      </div>
      <div class="items-list stagger" id="items-list"></div>
    </div>

    <div class="section" id="section-logs">
      <div class="section__head">
        <h3 class="section__title">运行日志</h3>
        <div class="zone__tools">
          <span id="reconciling-note" class="note" hidden>已重连 · 正在对账…</span>
          <button type="button" id="btn-clear-logs" class="btn btn--sm btn--ghost">清空</button>
        </div>
      </div>
      <div id="logs" class="logs" role="log" aria-live="polite"></div>
    </div>

    ${(d.skill_diagnostics || []).length ? `
    <div class="section" id="section-skill-diagnostics">
      <div class="section__head">
        <h3 class="section__title">技能识别诊断 <span class="badge">${d.skill_diagnostics.length}</span></h3>
        <span class="section__hint">比较模型报告与注册表解析结果</span>
      </div>
      <div class="skill-diagnostics">
        ${d.skill_diagnostics.map((x) => `
          <div class="skill-diagnostics__row">
            <strong>${esc(x.title || x.item_id)}</strong>
            <span>题型 ${esc(x.reported_qtype || x.qtype || "未知")}</span>
            <span>skill_id ${esc(x.reported_skill_id ?? "(缺失)")} → ${esc(x.skill_id ?? "(无匹配)")}</span>
            <span>原因 ${esc(x.skill_error || "模型字段不一致")}</span>
          </div>`).join("")}
      </div>
    </div>` : ""}`;

  renderItems(items);
  renderActivity();
  renderLogs();
  renderRuntimeStatus();
}

function renderItems(items) {  const list = $("#items-list");
  if (!list) return;
  if (!items.length) {
    list.innerHTML = '<div class="empty"><p class="empty__hint">' +
      "这个任务还没有条目 —— 启动之后，每读到一道题 / 一集就会在这里出现。</p></div>";
    return;
  }
  list.innerHTML = items.map((i) => {
    const ep = i.type === "video" && i.episode_index != null
      ? `第 ${i.episode_index}/${i.episode_total == null ? "?" : i.episode_total} 集` : "";
    const title = i.title || (i.type === "video" ? "视频任务"
      : (i.qid ? `题目 ${String(i.qid).slice(0, 8)}` : "题目"));
    return `
      <button type="button" class="item-row" data-item="${esc(i.item_id)}">
        <span class="badge">${esc(TYPE_LABELS[i.type] || i.type)}</span>
        <span class="item-row__title">${esc(title)}</span>
        ${ep ? `<span class="item-row__id">${esc(ep)}</span>` : ""}
        ${i.suspended ? '<span class="badge">已挂起</span>' : ""}
        <span class="state-tag state--${esc(i.state)}">${esc(STATE_LABELS[i.state] || i.state)}</span>
      </button>`;
  }).join("");
}

/* ---- 活动流：把「现在在干什么」讲成人话 ---- */
const ACTIVITY_TEXT = {
  boot: (p) => (p && p.msg) || "控制台启动",
  "log.line": (p) => (p && p.line) || (p ? JSON.stringify(p) : ""),
  "run.started": () => "任务开始运行",
  "run.finished": (p) => (p && p.stopped ? "已停止（用户中断）" : "任务已跑完"),
  "run.error": (p) => `任务出错${p && p.error_code ? `：${p.error_code}` : ""}` +
    (p && p.message ? ` —— ${p.message}` : ""),
  "run.paused": (p) => `已暂停${p && p.reason ? `（${p.reason}）` : ""}`,
  "run.resumed": (p) => p && p.retry ? "已从断点重试/恢复任务" : "已恢复运行",
  "perception.probe_started": (p) => `开始读题（通道 ${(p && p.channel) || "?"}）`,
  "perception.probe_fallback": (p) => `通道读不到，降级到 ${(p && p.next_channel) || "下一条"}`,
  "perception.arbitration_decided": (p) => `仲裁选定通道 ${(p && p.channel) || "?"}`,
  "perception.dump_paused": () => "两条通道都读不到，已暂停留档（不会静默跳过）",
  // 同一个事件名服务两种语义：建任务（routes/tasks.py，带 run_id）与
  // 「认领一道新题／新建条目」（orchestrator._claim_item，带 item_id）。
  // 不区分的话，跑起来会反复出现「发现新任务」——看起来像凭空多了任务。
  "task.created": (p) =>
    p && p.item_id ? "认领一道新题（新建条目）" : "发现新任务",
  "task.updated": () => "任务信息已更新",
  "perception.done": (p) => `读到内容（通道 ${(p && p.channel) || "?"}）`,
  "solve.vote": (p) => `投票：一致率 ${(p && p.majority_ratio) ?? "?"}（${p && p.recalculated ? "复算" : "首次样本"}）`,
  "solve.done": (p) => `作答完成${p && p.confidence != null ? `（置信度 ${p.confidence}）` : ""}`,
  "solve.escalated": (p) => `升级复算${p && p.reason ? `：${p.reason}` : ""}`,
  "solve.review_required": () => "标记 ⚠复核，停下来等人",
  // ⚠ 事件的负载字段名是 `level`（见 act/actuator.py 的 `_done`）——
  // 这里原先读的是 `level_used`，对不上，界面上一直显示 "?"。
  "act.level_used": (p) =>
    `动作执行层级：${LEVEL_LABELS[(p && p.level) || ""] || (p && p.level) || "?"}`,
  "act.readback_mismatch": () => "动作回读不一致",
  "act.submit_timeout": () => "提交后没等到结果，暂停等人",
  "media.state_changed": (p) => `媒体态：${(p && p.from) || "?"} → ${(p && p.to) || "?"}`,
  "media.interrupt_detected": () => "检出弹题打断",
  "stack.pushed": () => "压栈（挂起当前任务）",
  "stack.popped": () => "弹栈（恢复原任务）",
  "task.state_changed": (p) => `条目状态：${STATE_LABELS[(p && p.from) || ""] || (p && p.from) || "?"} → ${STATE_LABELS[(p && p.to) || ""] || (p && p.to) || "?"}`,
  "task.needs_confirm": () => "有题目等你确认",
  "advance.calibrated": (p) => {
    if (!p) return "开局判定完成";
    if (p.scroll_revealed_next) return "滚动之后找到了「下一题」";
    const total = p.total == null ? "总数未知" : `共 ${p.total} 题`;
    const cur = p.current == null ? "" : `，当前第 ${p.current} 题`;
    const how = METHOD_LABELS[p.method] || p.method || "方式未知";
    // ``summary`` 是程序侧裁决出的一句话（推进方式 + 总题数 + 提交范围）。
    // 有它就优先显示：用户一眼看到的应当是「这一趟按什么逻辑走」，而不是原始字段。
    const tail = p.summary || (p.reason ? `（${p.reason}）` : "");
    return `开局判定：${total}${cur}，推进方式「${how}」` + (tail ? `（${tail}）` : "");
  },
  "advance.completion_check": (p) => {
    if (!p) return "收尾确认";
    const why = p.trigger === "reached_total" ? "已做满判定的题数" : "这一种推进方式推不动了";
    return p.completed
      ? `${why} → 视觉组观测到整卷已完成 → 收工`
      : `${why} → 视觉组<b>没有</b>确认完成 → 停下等人处理` +
        (p.reason ? `（${p.reason}）` : "");
  },
};

/** 「怎么进入下一题」的中文说法（与 ``core.enums.AdvanceMethod`` 对齐）。 */
const METHOD_LABELS = {
  click: "点击按钮",
  card: "点答题卡题号",
  swipe: "滑动翻页",
  scroll: "向下滚动",
  unknown: "没裁决出来（不该出现）",
};

function renderActivity() {
  const box = $("#activity-stream");
  if (!box) return;
  const entries = state.logs.filter((e) => ACTIVITY_TEXT[e.event] && e.text);
  if (!entries.length) {
    box.innerHTML = '<div class="empty"><p class="empty__hint">' +
      "还没有动作。启动任务后这里会滚动显示它在做什么。</p></div>";
    return;
  }
  box.innerHTML = entries.slice(-60).reverse().map((entry) => {
    const level = ACTIVITY_LEVEL[entry.event] || "";
    return `<div class="activity__item${level ? ` activity__item--${level}` : ""}">
      <span class="activity__time">${esc(entry.time)}</span>
      <span class="activity__event">${esc(entry.event)}</span>
      <span class="activity__body">${esc(entry.text)}</span>
    </div>`;
  }).join("");
}

/* ============================ ⑤ 单题抽屉 ============================ */
async function openItem(itemId) {
  $("#item-drawer").hidden = false;
  $("#item-drawer-body").innerHTML = '<p class="empty__hint">正在读取…</p>';
  let d;
  try {
    d = await api(`/api/items/${itemId}`);
  } catch (e) { handleApiError(e); return; }

  const chosen = new Set(d.chosen_labels || []);
  const options = (d.options || []).map((text, i) => {
    const label = String.fromCharCode(65 + i);
    return `<div class="option${chosen.has(label) ? " is-chosen" : ""}">
      <span class="option__label">${label}</span><span>${esc(text)}</span></div>`;
  }).join("");

  const samples = (d.samples || []).map((s) => `
    <div class="samples__row">
      <span>#${esc(s.sample_index)}</span>
      <span>${esc((s.chosen_labels || []).join(","))}</span>
      <span>${esc(s.latency_ms)}ms</span>
      <span>${esc(s.error_code || "")}</span>
    </div>`).join("");

  const shot = (url, cap) => url
    ? `<div class="shot"><img src="${esc(url)}" alt="${esc(cap)}" loading="lazy" />` +
      `<div class="shot__cap">${esc(cap)}</div></div>`
    : "";

  const item = d.item || {};
  $("#item-drawer-body").innerHTML = `
    <div class="drawer__stem">${esc(d.stem || "（未读到题干）")}</div>
    ${options ? `<div class="options">${options}</div>` : ""}
    <div class="kv">
      <div><div class="kv__k">置信度</div>
        <div class="kv__v">${d.confidence != null ? (d.confidence * 100).toFixed(1) + "%" : "—"}</div></div>
      <div><div class="kv__k">求解档位</div>
        <div class="kv__v">${esc(SOLVE_PATH_LABELS[d.solve_path] || d.solve_path || "—")}</div></div>
      <div><div class="kv__k">执行层级</div>
        <div class="kv__v">${esc(d.level_used || "—")}</div></div>
      <div><div class="kv__k">状态</div>
        <div class="kv__v">${esc(STATE_LABELS[item.state] || item.state || "—")}</div></div>
    </div>
    ${d.readback_mismatch ? '<div class="warn-strip">动作回读不一致，已按阶梯重放或升级</div>' : ""}
    ${samples ? `<div><div class="kv__k" style="margin-bottom:6px">采样明细</div><div class="samples">${samples}</div></div>` : ""}
    <div class="shots">${shot(d.before_screenshot_url, "执行前")}${shot(d.after_screenshot_url, "执行后")}</div>
    <div class="detail-actions">
      ${d.is_danger_state
        ? '<span class="form__hint">该题已提交（危险态），只能回读结果，不能再决策。</span>'
        : `<button type="button" class="btn btn--primary" data-confirm="confirm" data-item="${esc(item.item_id || "")}">确认</button>
           <button type="button" class="btn btn--danger" data-confirm="reject" data-item="${esc(item.item_id || "")}">否决</button>`}
    </div>`;
}

function closeItem() { $("#item-drawer").hidden = true; }

async function onDecision(decision, itemId) {
  if (!itemId) return;
  try {
    await api(`/api/items/${itemId}/confirm`, {
      method: "POST",
      body: JSON.stringify({ decision, note: null }),
    });
    toast(decision === "confirm" ? "已确认" : "已否决", "success");
    closeItem();
    await refreshTaskDetail();
    await refreshTasks().catch(() => {});
  } catch (e) { handleApiError(e); }
}

/* ============================ ⑥ 任务控制 ============================ */
const CONTROL_LABELS = { start: "启动", pause: "暂停", resume: "恢复", stop: "停止" };

async function taskControl(action) {
  if (!state.taskId) return;
  try {
    await api(`/api/tasks/${state.taskId}/${action}`, { method: "POST" });
    toast(`已${CONTROL_LABELS[action] || action}`, "success");
  } catch (e) { handleApiError(e); }
  await refreshTasks().catch(() => {});
  await refreshTaskDetail();
}

async function renameTask() {
  const current = state.taskDetail ? state.taskDetail.task.name : "";
  const name = prompt("新的任务名：", current);
  if (name === null) return;
  const trimmed = name.trim();
  if (!trimmed) { toast("任务名不能为空", "error"); return; }
  try {
    await api(`/api/tasks/${state.taskId}`, { method: "PATCH", body: JSON.stringify({ name: trimmed }) });
    toast("已改名", "success");
    await refreshTaskDetail();
    await refreshTasks().catch(() => {});
  } catch (e) { handleApiError(e); }
}

async function deleteTask() {
  if (!state.taskId) return;
  const id = state.taskId;
  if (!confirm("删除这个任务？它的条目与留痕会一起删掉，不可恢复。")) return;
  try {
    await api(`/api/tasks/${id}`, { method: "DELETE" });
    state.pickedTasks.delete(id);
    toast("已删除", "success");
    goHome();
    await refreshTasks();
  } catch (e) { handleApiError(e); }
}

/** 删单个任务 —— **列表卡片上的按钮**，不必先进详情。 */
async function deleteTaskById(runId) {
  const task = state.tasks.find((t) => t.run_id === runId);
  const name = task ? task.name : runId.slice(0, 8);
  if (!confirm(`删除任务「${name}」？它的条目与留痕会一起删掉，不可恢复。`)) return;
  try {
    await api(`/api/tasks/${runId}`, { method: "DELETE" });
    state.pickedTasks.delete(runId);
    toast("已删除", "success");
    if (state.taskId === runId) goHome();
    await refreshTasks();
  } catch (e) { handleApiError(e); }
}

/**
 * 重试：把失败 / 停下的任务**从断点续跑**。
 *
 * 后端与「启动」是同一份实现 —— 已经做完的题不会重做、提交绝不重放。
 * 所以这里敢于直接点，不需要先问用户「要不要从头上再做一遍」。
 */
async function retryTask(runId) {
  try {
    if (state.taskId === runId && state.view === "task" && state.taskDetail) {
      const { status, name } = state.taskDetail.task;
      const action = status === "paused" || status === "stopped"
        ? "从断点继续此任务" : "从断点重试此任务";
      if (!confirm(`${action}「${name}」？已完成的题不会重做。`)) return;
    }
    await api(`/api/tasks/${runId}/retry`, { method: "POST" });
    toast("已从断点重新启动（已完成的题不会重做）", "success");
    await refreshTasks();
    if (state.view === "task" && state.taskId === runId) await openTask(runId);
  } catch (e) { handleApiError(e); }
}

/**
 * 批量删除。
 *
 * 后端**逐条回报**：正在跑的那条会被跳过并说明原因、其余照删 ——
 * 所以这里必须把跳过的**一条条讲出来**，只弹一句「部分失败」等于什么都没说。
 */
async function bulkDeleteTasks() {
  const ids = [...state.pickedTasks];
  if (!ids.length) return;
  if (!confirm(`删除选中的 ${ids.length} 个任务？它们的条目与留痕会一起删掉，不可恢复。`)) {
    return;
  }
  try {
    const out = await api("/api/tasks/bulk-delete", {
      method: "POST",
      body: JSON.stringify({ run_ids: ids }),
    });
    const deleted = out.deleted || [];
    const skipped = out.skipped || [];
    // 没删掉的那些**保持勾选** —— 用户能看到是哪个没删成功，处理完再点一次。
    state.pickedTasks = new Set(skipped.map((s) => s.run_id));
    if (deleted.length) toast(`已删除 ${deleted.length} 个任务`, "success");
    for (const item of skipped) {
      const task = state.tasks.find((t) => t.run_id === item.run_id);
      toast(`${task ? task.name : item.run_id.slice(0, 8)}：${item.reason}`, "error");
    }
    if (state.taskId && deleted.includes(state.taskId)) goHome();
    await refreshTasks();
  } catch (e) { handleApiError(e); }
}

/* ============================ ⑦ 模型库表单 ============================ */
async function refreshPresets() {
  try {
    state.presets = await api("/api/providers/presets");
  } catch { state.presets = []; }
  $("#model-preset").innerHTML = ['<option value="">（手动填写）</option>']
    .concat(state.presets.map((p) =>
      `<option value="${esc(p.preset_id)}">${esc(p.label || p.preset_id)}</option>`)).join("");
}

function openModelDialog(id = null) {
  state.editingModelId = id;
  const m = id ? state.models.find((x) => x.profile_id === id) : null;
  $("#model-dialog-title").textContent = m ? "编辑模型" : "添加模型";
  $("#model-name").value = m ? m.name : "";
  $("#model-base-url").value = m ? m.base_url : "";
  $("#model-model").value = m ? m.model : "";
  $("#model-temperature").value = m ? m.temperature : 0.2;
  $("#model-timeout").value = m ? m.timeout_s : 60;
  $("#model-concurrency").value = m ? m.concurrency : 2;
  $("#model-enabled").checked = m ? m.enabled : true;
  $("#model-api-key").value = "";
  $("#model-api-key").placeholder = m ? "留空 = 不改动既有密钥" : "sk-…";
  $("#api-key-note").textContent = m && m.has_api_key
    ? "已保存（不回显）。留空表示不改动。" : "只写进系统凭据管理器，界面不回显明文";
  $("#model-form-result").hidden = true;
  $("#model-dialog").hidden = false;
  $("#model-name").focus();
}

function closeModelDialog() { $("#model-dialog").hidden = true; }

function collectModelForm() {
  const payload = {
    name: $("#model-name").value.trim(),
    base_url: $("#model-base-url").value.trim(),
    model: $("#model-model").value.trim(),
    temperature: parseFloat($("#model-temperature").value) || 0.2,
    timeout_s: parseInt($("#model-timeout").value, 10) || 60,
    concurrency: parseInt($("#model-concurrency").value, 10) || 2,
    enabled: $("#model-enabled").checked,
  };
  const key = $("#model-api-key").value;
  if (key) payload.api_key = key;
  return payload;
}

function showTestResult(html, ok) {
  const box = $("#model-form-result");
  box.hidden = false;
  box.className = "test-result " + (ok ? "test-result--ok" : "test-result--fail");
  box.innerHTML = html;
}

async function onTestModel() {
  const payload = collectModelForm();
  if (!payload.base_url || !payload.model) {
    toast("先填 Base URL 和模型名", "error");
    return;
  }
  showTestResult("正在测试…", true);
  try {
    let profileId = state.editingModelId;
    if (!profileId) {
      const created = await api("/api/models", { method: "POST", body: JSON.stringify(payload) });
      profileId = created.profile_id;
      state.editingModelId = profileId;
    } else {
      await api(`/api/models/${profileId}`, { method: "PUT", body: JSON.stringify(payload) });
    }
    $("#model-api-key").value = "";
    const caps = await api(`/api/models/${profileId}/test`, { method: "POST" });
    const ok = !!caps.auth_ok;
    showTestResult(`
      <div>${visionBadge(caps)}</div>
      <div style="margin-top:6px">
        延迟 ${esc(caps.latency_ms)}ms${caps.max_qps ? ` · 实测 QPS ≈ ${esc(caps.max_qps)}` : ""}
        ${caps.vision_model ? ` · 视觉模型 <code>${esc(caps.vision_model)}</code>` : ""}
        ${caps.error_code ? ` · 错误码 <code>${esc(caps.error_code)}</code>` : ""}
      </div>`, ok);
    await refreshModels();
    state.config = await api("/api/run/config");
    refreshWizardModels();
  } catch (e) {
    showTestResult(esc(e.message), false);
  }
}

async function onSaveModel(event) {
  event.preventDefault();
  const payload = collectModelForm();
  if (!payload.name || !payload.base_url || !payload.model) {
    toast("名称、Base URL、模型名都是必填", "error");
    return;
  }
  try {
    if (state.editingModelId) {
      await api(`/api/models/${state.editingModelId}`, { method: "PUT", body: JSON.stringify(payload) });
    } else {
      await api("/api/models", { method: "POST", body: JSON.stringify(payload) });
    }
    $("#model-api-key").value = "";
    closeModelDialog();
    toast("已保存", "success");
    await refreshModels();
    state.config = await api("/api/run/config");
    refreshWizardModels();
  } catch (e) { handleApiError(e); }
}

async function onModelAction(act, id) {
  try {
    if (act === "delete") {
      if (!confirm("删除这套模型配置？它保存的密钥也会一并删除。")) return;
      await api(`/api/models/${id}`, { method: "DELETE" });
      toast("已删除", "success");
    } else if (act === "edit") {
      openModelDialog(id);
      return;
    } else if (act === "test") {
      toast("正在测试连接…");
      const caps = await api(`/api/models/${id}/test`, { method: "POST" });
      toast(caps.auth_ok ? `鉴权 OK · ${caps.latency_ms}ms` : `失败：${caps.error_code}`,
        caps.auth_ok ? "success" : "error");
    } else if (act === "up" || act === "down") {
      const ids = state.models.map((m) => m.profile_id);
      const i = ids.indexOf(id);
      const j = act === "up" ? i - 1 : i + 1;
      if (i < 0 || j < 0 || j >= ids.length) return;
      const tmp = ids[i]; ids[i] = ids[j]; ids[j] = tmp;
      await api("/api/models/order", { method: "PUT", body: JSON.stringify({ profile_ids: ids }) });
    }
    await refreshModels();
    state.config = await api("/api/run/config");
  } catch (e) { handleApiError(e); }
}

/* ============================ ⑧ 日志 ============================ */
function appendLog(eventName, payload) {
  let text = "";
  if (eventName === "log.line") {
    // ``log.line`` 有两种形态：**纯文本**（``payload.line``）与**结构化诊断**
    // （``{"vision_read": "failed", "error": ...}``）。后者过去被丢成空字符串，
    // 于是界面上只剩一个孤零零的事件名 —— 而它偏偏是读题失败时**唯一**的线索。
    text = (payload && payload.line) || (payload ? JSON.stringify(payload) : "");
  } else {
    const fn = ACTIVITY_TEXT[eventName];
    text = fn ? fn(payload) : (payload == null ? "" : JSON.stringify(payload));
  }
  state.logs.push({ time: now(), event: eventName, text });
  if (state.logs.length > 600) state.logs.splice(0, state.logs.length - 600);
  renderLogs();
  if (state.view === "task") renderActivity();
}

function renderLogs() {
  const box = $("#logs");
  if (!box) return;
  if (!state.logs.length) {
    box.innerHTML = '<div class="empty empty--logs"><p class="empty__hint">' +
      "任务产生的事件会实时滚动在这里。</p></div>";
    return;
  }
  box.innerHTML = state.logs.slice(-300).map((entry) => {
    const level = ACTIVITY_LEVEL[entry.event] || "";
    return `<div class="log-line${level ? ` log-line--${level}` : ""}">
      <span class="log-line__time">${esc(entry.time)}</span>
      <span class="log-line__event">${esc(entry.event)}</span>
      <span>${esc(entry.text)}</span>
    </div>`;
  }).join("");
  box.scrollTop = box.scrollHeight;
}

/* ============================ ⑨ 运行态条 ============================ */
function setPill(sel, text, tone) {
  const el = $(sel);
  if (!el) return;
  el.textContent = text;
  el.className = "pill " + (tone === "active" ? "pill--online"
    : tone === "warn" ? "pill--offline" : "pill--muted");
}

function renderRuntimeStatus() {
  const d = state.taskDetail;
  const items = (d && d.items) || [];
  const videos = items.filter((i) => i.type === "video");
  const video = videos.length ? videos[videos.length - 1] : null;
  const media = video ? video.state : null;
  const ep = video && video.episode_index != null ? `第 ${video.episode_index} 集 · ` : "";
  setPill("#rs-media",
    media ? `媒体态 ${ep}${STATE_LABELS[media] || media}` : "媒体态 —",
    media === "playing" ? "active" : media === "interrupted" ? "warn" : null);

  const waiting = items.filter((i) => i.state === "pending_confirm").length;
  const suspended = !!(video && video.suspended);
  setPill("#rs-interrupt",
    waiting ? `待确认 ${waiting}` : suspended ? "弹题 已挂起" : "弹题 —",
    waiting || suspended ? "warn" : null);

  const depth = d ? d.stack_depth : null;
  setPill("#rs-stack", `任务栈 ${depth == null ? "—" : depth}`, depth > 0 ? "warn" : null);
}

/* ============================ ⑩ SSE ============================ */
let es = null;
const SSE_EVENTS = [
  "run.started", "run.paused", "run.resumed", "run.finished", "run.error",
  "task.created", "task.updated", "task.state_changed", "task.needs_confirm",
  "perception.done", "perception.probe_started", "perception.probe_fallback",
  "perception.arbitration_decided", "perception.dump_paused",
  "solve.vote", "solve.done", "solve.escalated", "solve.review_required",
  "act.level_used", "act.readback_mismatch", "act.submit_timeout",
  "media.state_changed", "media.interrupt_detected",
  "stack.pushed", "stack.popped",
  "advance.calibrated", "advance.completion_check",
  "log.line",
];

function setConn(online) {
  $("#conn-indicator").className = "pill " + (online ? "pill--online" : "pill--offline");
  $("#conn-label").textContent = online ? "已连接" : "连接中断·重连中…";
}

/** 事件驱动的刷新要合并：一轮运行会瞬间打出十几个事件，逐个拉接口会打爆后端。 */
function throttle(fn, ms) {
  let timer = null;
  return function (...args) {
    if (timer) return;
    timer = setTimeout(() => { timer = null; fn.apply(null, args); }, ms);
  };
}

const refreshSoon = throttle(() => {
  refreshTasks().catch(() => {});
  if (state.view === "task") refreshTaskDetail().catch(() => {});
}, 400);

async function reconcile() {
  const note = $("#reconciling-note");
  if (note) {
    note.hidden = false;
    setTimeout(() => { note.hidden = true; }, 900);
  }
  await refreshTasks().catch(() => {});
  if (state.view === "task") await refreshTaskDetail().catch(() => {});
  await refreshHealth().catch(() => {});
}

function handleSSE(eventName, payload) {
  if (eventName === "log.line") {
    appendLog("log.line", payload);
    return;
  }
  appendLog(eventName, payload);
  switch (eventName) {
    case "run.started":
      state.running = true;
      break;
    case "run.finished":
    case "run.error":
      state.running = false;
      break;
    default:
      break;
  }
  refreshSoon();
}

function connectSSE() {
  if (es) es.close();
  es = new EventSource("/api/events");
  es.onopen = () => { setConn(true); reconcile(); };
  es.onerror = () => setConn(false);
  SSE_EVENTS.forEach((name) => {
    es.addEventListener(name, (e) => {
      let payload = null;
      try { payload = JSON.parse(e.data); } catch { payload = e.data; }
      handleSSE(name, payload);
    });
  });
}

async function refreshHealth() {
  const h = await api("/api/health");
  $("#health-version").textContent = "v" + h.version;
}

/* ============================ ⑪ 关机 ============================ */
// 「关机」= 收掉本程序自己起的一切（控制台 / 靶场 / 接管启动的浏览器），最后退出进程。
// 三条纪律：
//   ① 后端**先回执、后动手** —— 拿到回执不等于已经关完，要用探活确认；
//   ② 响应可能被进程退出截断（fetch 直接 reject）—— 成败由探活判定，
//      **别把「其实关成功了」报成失败**；
//   ③ 有任务在跑时后端默认 409 —— 在对话框里就地警告，由用户明确选「仍然关闭」。
let shutdownNeedsForce = false;

function closeShutdownDialog() {
  $("#shutdown-dialog").hidden = true;
}

function showShutdownWarn(text) {
  const el = $("#shutdown-warn");
  el.innerHTML = mdInline(esc(text));
  el.hidden = false;
}

function showShutdownVeil(title, text) {
  $("#shutdown-veil-title").textContent = title;
  $("#shutdown-veil-text").innerHTML = text ? mdInline(esc(text)) : "";
  $("#shutdown-veil").hidden = false;
}

async function openShutdownDialog() {
  shutdownNeedsForce = false;
  $("#shutdown-warn").hidden = true;
  $("#btn-shutdown-confirm").textContent = "确认关闭";
  // 占位与真清单**分开两个元素**：`li` 只在清单真的拿到之后才出现。
  // 合成一个的话，自动化会在「请求还没回来」的瞬间把占位当成清单读走
  // （自检实测踩到过：清单只读到一条「正在查看当前占用…」）。
  $("#shutdown-preview").innerHTML = "";
  $("#shutdown-preview").hidden = true;
  $("#shutdown-preview-hint").hidden = false;
  $("#shutdown-dialog").hidden = false;
  try {
    const st = await api("/api/system/status");
    state.shutdownStatus = st;
    // 逐条渲染后端给的清单 —— 文案与判定**同源**，界面不自己拼，
    // 否则会出现「界面说会关靶场、实际没关」这种最伤信任的不一致。
    $("#shutdown-preview").innerHTML = (st.preview || [])
      .map((line) => `<li class="shutdown__item">${mdInline(esc(line))}</li>`)
      .join("");
    $("#shutdown-preview").hidden = false;
    $("#shutdown-preview-hint").hidden = true;
    if (st.running) showShutdownWarn("有任务正在运行 —— 关闭会中断它。");
  } catch (err) {
    $("#shutdown-preview-hint").hidden = true;
    showShutdownWarn(`读不到当前占用情况（${err.message}）。仍然可以尝试关闭。`);
  }
}

async function pingAlive() {
  try {
    const res = await fetch("/api/health", { cache: "no-store" });
    return res.ok;
  } catch {
    return false;
  }
}

// 进程退出需要一点时间：连着探几次都探不到，才判定「已关闭」。
async function waitUntilDown(attempts = 5, gapMs = 400) {
  for (let i = 0; i < attempts; i += 1) {
    if (!(await pingAlive())) return true;
    await new Promise((resolve) => setTimeout(resolve, gapMs));
  }
  return false;
}

async function shutdownConsole() {
  closeShutdownDialog();
  showShutdownVeil("正在关闭…", "正在释放端口，稍等两三秒。");
  try {
    const res = await api(
      `/api/system/shutdown?force=${shutdownNeedsForce ? "true" : "false"}`,
      { method: "POST" }
    );
    const down = await waitUntilDown();
    if (!down) {
      showShutdownVeil("关闭命令已发出，但控制台仍在响应", "请看后台窗口的日志，也可以直接关掉那个窗口。");
      return;
    }
    const skipped = (res && res.skipped) || [];
    showShutdownVeil(
      "已关闭",
      skipped.length
        ? `全部相关端口已释放。${skipped.join("、")} 属于别的程序，没有动它。`
        : "全部相关端口已释放，现在可以关掉这个窗口。"
    );
  } catch (err) {
    // 409 是明确的 HTTP 响应（不可能是被截断），先分流：让用户明确决定要不要中断任务。
    if (err.status === 409) {
      shutdownNeedsForce = true;
      $("#shutdown-veil").hidden = true;
      $("#shutdown-dialog").hidden = false;
      $("#btn-shutdown-confirm").textContent = "仍然关闭";
      showShutdownWarn(err.message || "有任务正在运行。");
      return;
    }
    // 其余情况：真的失败，或者**关机成功但响应被进程退出截断**（后者是常态）。
    if (await waitUntilDown()) {
      showShutdownVeil("已关闭", "全部相关端口已释放，现在可以关掉这个窗口。");
      return;
    }
    showShutdownVeil("关闭失败", `${err.message || "请求失败"}。控制台仍在运行。`);
  }
}

/* ============================ ⑫ 事件绑定与初始化 ============================ */
function bindEvents() {
  // 顶栏
  $("#btn-new-task").addEventListener("click", openWizard);
  $("#btn-home").addEventListener("click", goHome);

  // 顶栏 —— 关机：收掉本程序自己起的一切，释放全部相关端口
  $("#btn-shutdown").addEventListener("click", () => {
    openShutdownDialog().catch((e) => handleApiError(e));
  });
  $("#btn-shutdown-close").addEventListener("click", closeShutdownDialog);
  $("#btn-shutdown-cancel").addEventListener("click", closeShutdownDialog);
  $("#btn-shutdown-confirm").addEventListener("click", () => {
    shutdownConsole().catch((e) => console.warn(e));
  });

  // 主页 —— 任务
  $("#btn-refresh-tasks").addEventListener("click", () => refreshTasks().catch((e) => handleApiError(e)));
  $("#pick-all").addEventListener("change", (e) => {
    state.pickedTasks = e.target.checked
      ? new Set(state.tasks.map((t) => t.run_id))
      : new Set();
    renderTasks();
  });
  $("#btn-bulk-delete").addEventListener("click", () => bulkDeleteTasks());
  $("#tasks-list").addEventListener("change", (e) => {
    const box = e.target.closest("[data-pick]");
    if (!box) return;
    if (box.checked) state.pickedTasks.add(box.dataset.pick);
    else state.pickedTasks.delete(box.dataset.pick);
    // 只更新工具条的显隐，**不重绘整张列表** —— 重绘会把刚点的复选框闪一下。
    syncPickUi();
  });
  $("#zone-tasks").addEventListener("click", (e) => {
    // **卡片内部的操作按钮优先。** 不先拦这一下，点「删除」会先跳进详情页 ——
    // 因为那个按钮也在 `[data-task]` 里面。
    const op = e.target.closest("[data-act][data-target]");
    if (op) {
      if (op.dataset.act === "retry-task") retryTask(op.dataset.target);
      else if (op.dataset.act === "delete-task") deleteTaskById(op.dataset.target);
      return;
    }
    if (e.target.closest('[data-act="new-task"]')) { openWizard(); return; }
    const card = e.target.closest("[data-task]");
    if (card) openTask(card.dataset.task);
  });

  // 主页 —— 模型库
  $("#btn-add-model").addEventListener("click", () => openModelDialog());
  $("#zone-models").addEventListener("click", (e) => {
    if (e.target.closest('[data-act="add-model"]')) { openModelDialog(); return; }
    const btn = e.target.closest("[data-act][data-id]");
    if (btn) onModelAction(btn.dataset.act, btn.dataset.id);
  });

  // 模型对话框
  $("#btn-dialog-close").addEventListener("click", closeModelDialog);
  $("#btn-model-cancel").addEventListener("click", closeModelDialog);
  $("#model-form").addEventListener("submit", onSaveModel);
  $("#btn-model-test").addEventListener("click", onTestModel);
  $("#model-preset").addEventListener("change", (e) => {
    const p = state.presets.find((x) => x.preset_id === e.target.value);
    if (!p) return;
    $("#model-base-url").value = p.base_url || "";
    $("#model-model").value = p.model || "";
  });

  // 向导
  $("#btn-wizard-close").addEventListener("click", closeWizard);
  $("#btn-wizard-cancel").addEventListener("click", closeWizard);
  $("#btn-wizard-next").addEventListener("click", () => wizardNext().catch((e) => handleApiError(e)));
  $("#btn-wizard-prev").addEventListener("click", () => gotoWizardStep(wizard.step - 1));
  $("#btn-scan-targets").addEventListener("click", () => scanTargets().catch((e) => handleApiError(e)));
  $("#btn-launch-browser").addEventListener("click", () => launchBrowser().catch((e) => handleApiError(e)));
  $("#btn-wizard-goto-models").addEventListener("click", () => {
    closeWizard();
    showView("home");
    openModelDialog();
  });
  $$('input[name="wizard_target_kind"]').forEach((el) => {
    el.addEventListener("change", () => {
      state.targets = { targets: [], hint: null };
      renderTargetOptions();
      $("#btn-launch-browser").hidden = el.value !== "browser_page";
    });
  });
  $("#wizard-autostart").addEventListener("change", refreshWizardNextLabel);
  // 复算开关：关掉时把次数输入置灰（「不复算就以第一次答案为准」要看得见）
  $("#wizard-recalc").addEventListener("change", syncRecalcUI);
  // 备用组勾选列表（事件委托：每次重绘后仍有效）
  $("#wizard").addEventListener("change", (e) => {
    const box = e.target;
    if (box instanceof HTMLInputElement && box.type === "checkbox"
        && box.closest(".pick-list")) {
      onBackupToggle(box);
    }
  });

  // 任务详情（事件委托：详情整体重渲染）
  $("#detail-body").addEventListener("click", (e) => {
    const run = e.target.closest("[data-run]");
    if (run) { taskControl(run.dataset.run); return; }
    const retry = e.target.closest("[data-retry-task]");
    if (retry) { retryTask(retry.getAttribute("data-retry-task")); return; }
    const conf = e.target.closest("[data-confirm]");
    if (conf) { onDecision(conf.dataset.confirm, conf.dataset.item); return; }
    const item = e.target.closest("[data-item]");
    if (item) { openItem(item.dataset.item); return; }
    if (e.target.closest("#btn-rename-task")) { renameTask(); return; }
    if (e.target.closest("#btn-delete-task")) { deleteTask(); return; }
    if (e.target.closest("#btn-clear-logs")) {
      state.logs = [];
      renderLogs();
      renderActivity();
    }
  });

  // 单题抽屉
  $("#btn-drawer-close").addEventListener("click", closeItem);
  $("#item-drawer-body").addEventListener("click", (e) => {
    const conf = e.target.closest("[data-confirm]");
    if (conf) onDecision(conf.dataset.confirm, conf.dataset.item);
  });

  // ESC 关闭浮层
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("#item-drawer").hidden) closeItem();
    else if (!$("#model-dialog").hidden) closeModelDialog();
    else if (!$("#wizard").hidden) closeWizard();
  });
}

async function init() {
  bindEvents();
  appendLog("boot", { msg: "控制台已就绪，正在连接事件流…" });

  await Promise.all([
    refreshTasks().catch((e) => handleApiError(e, true)),
    refreshModels(),
    refreshPresets(),
    refreshHealth().catch(() => {}),
  ]);

  try {
    state.config = await api("/api/run/config");
  } catch { state.config = null; }

  connectSSE();
}

document.addEventListener("DOMContentLoaded", init);
