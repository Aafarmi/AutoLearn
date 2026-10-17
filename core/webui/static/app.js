"use strict";

const STATUS_LABELS = {
  draft: "草稿", queued: "等待执行", authenticating: "登录验证中", crawling: "抓取中",
  package_review: "待检查题包", waiting_model_authorization: "等待模型授权", solving: "解题中",
  answer_review: "待复核答案", ready_for_interaction: "等待填写", filling: "填写中",
  awaiting_submit_confirmation: "等待提交确认", submitting: "提交中", cancelling: "正在停止",
  filled_unsubmitted: "已填写，未提交", confirmed: "已提交并确认", failed: "失败",
  unconfirmed: "结果未确认", cancelled: "已取消", paused: "已暂停",
  needs_inspection: "需要检查后恢复", archived: "已归档"
};
const STATUS_TONES = {
  authenticating: "info", crawling: "info", solving: "info", filling: "info", submitting: "info",
  confirmed: "success", failed: "danger", unconfirmed: "danger", needs_inspection: "danger",
  waiting_model_authorization: "warning", awaiting_submit_confirmation: "warning",
  filled_unsubmitted: "warning", queued: "warning", cancelling: "warning"
};
const STEP_LABELS = {
  setup: "任务设置", authentication: "登录验证", crawl: "抓取题目", package_review: "检查题包",
  model_authorization: "模型授权", solve: "AI 解题", answer_review: "复核答案",
  interaction: "填写答案", submit_confirmation: "提交确认", result: "查看结果"
};
const WIZARD_STEPS = [
  ["authentication", "登录验证"],
  ["crawl", "抓取题目"],
  ["package_review", "检查题包"],
  ["model_authorization", "模型授权"],
  ["solve", "AI 解题"],
  ["answer_review", "复核答案"],
  ["submit_confirmation", "提交确认"]
];
const WIZARD_STEP_IDS = WIZARD_STEPS.map(([step]) => step);
const WIZARD_LABELS = Object.fromEntries(WIZARD_STEPS);
const REVIEW_STATUSES = new Set(["package_review", "answer_review"]);
const state = {
  meta: { adapters: [] }, tasks: [], queue: [], profiles: [],
  activeTaskId: localStorage.getItem("autolearn.activeTask") || null,
  detail: null, detailTaskId: null, detailRevision: null, detailRefreshing: false, overviewRefreshing: false, review: null, reviewError: null, answerMap: new Map(),
  confirmationId: null, modelAuthorizationId: null, profileSelection: null,
  editTaskId: null, busy: false, shuttingDown: false,
  cookieStatus: { captured: false, configured: false }, submitPending: null,
  wizardTaskId: null, wizardStep: null, wizardDerivedStep: null,
  runtimeEvents: [], runtimeSequence: 0
};

function el(id) { return document.getElementById(id); }
function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined && text !== null) item.textContent = String(text);
  return item;
}
function clear(element) { element.replaceChildren(); }
function formatTime(value) {
  if (!value) return "未知";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).replace("T", " ").slice(0, 16);
  return date.toLocaleString("zh-CN", { hour12: false });
}
function siteLabel(siteId) {
  const adapter = state.meta.adapters.find((item) => item.site_id === siteId);
  return adapter ? adapter.label : siteId;
}
function statusLabel(task) {
  if (task.queue_state === "active") return "运行中";
  if (task.queue_state === "queued") return task.queue_position ? `排队中 · 第 ${task.queue_position} 位` : "排队中";
  return STATUS_LABELS[task.status] || task.status;
}
function statusBadge(task) {
  const tone = task.queue_state ? "warning" : (STATUS_TONES[task.status] || "neutral");
  return node("span", `badge ${tone}`, statusLabel(task));
}
async function api(path, options = {}) {
  const fetchOptions = { method: options.method || "GET", credentials: "same-origin", headers: { Accept: "application/json" } };
  if (options.body !== undefined) {
    fetchOptions.headers["Content-Type"] = "application/json";
    fetchOptions.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, fetchOptions);
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const message = payload && typeof payload === "object" ? payload.detail : payload;
    throw new Error(message || `请求失败（${response.status}）`);
  }
  return payload;
}
function toast(message, tone = "info") {
  const item = node("div", `toast ${tone}`, message);
  el("toastRegion").appendChild(item);
  window.setTimeout(() => item.remove(), 4200);
}
async function runAction(action, successMessage) {
  if (state.busy) return;
  state.busy = true;
  try {
    await action();
    if (successMessage) toast(successMessage, "success");
    await refreshOverview();
    await refreshDetail();
  } catch (error) {
    toast(error.message || "操作失败。", "error");
  } finally { state.busy = false; }
}
async function shutdownService() {
  if (!window.confirm("关闭 AutoLearn 本机服务？正在运行的任务会等待安全位置结束。")) return;
  const button = el("shutdownService");
  button.disabled = true;
  button.textContent = "正在关闭";
  try {
    await api("/api/shutdown", { method: "POST" });
    state.shuttingDown = true;
    el("serviceStatus").textContent = "本机服务已关闭";
    el("serviceStatus").className = "service-status";
    toast("服务已关闭，可以关闭此页面。", "success");
    window.setTimeout(() => { try { window.close(); } catch (_) {} }, 400);
  } catch (error) {
    button.disabled = false;
    button.textContent = "关闭服务";
    toast(error.message || "无法关闭本机服务。", "error");
  }
}
async function deleteSelectedProfile() {
  const profileId = el("profileForm").elements.profile_id.value;
  if (!profileId) { toast("请先选择一个已保存的模型档案。", "error"); return; }
  const profile = state.profiles.find((item) => item.profile_id === profileId);
  const name = profile ? profile.name : "该模型档案";
  if (!window.confirm(`确定删除模型档案“${name}”吗？任务和题包不会被删除。`)) return;
  try {
    await api(`/api/profiles/${encodeURIComponent(profileId)}`, { method: "DELETE" });
    state.profileSelection = null;
    await refreshOverview();
    const next = state.profiles.find((item) => item.is_default) || state.profiles[0];
    selectProfile(next ? next.profile_id : null);
    toast("模型档案已删除。", "success");
  } catch (error) {
    toast(error.message || "删除模型档案失败。", "error");
  }
}
function openDialog(id) { const dialog = el(id); if (dialog && !dialog.open) dialog.showModal(); }
function closeDialog(id) { const dialog = el(id); if (dialog && dialog.open) dialog.close(); }

function renderMeta() {
  const siteFilter = el("siteFilter");
  clear(siteFilter);
  siteFilter.appendChild(new Option("全部站点", ""));
  for (const adapter of state.meta.adapters) siteFilter.appendChild(new Option(adapter.label, adapter.site_id));
  const statusFilter = el("statusFilter");
  clear(statusFilter);
  statusFilter.appendChild(new Option("全部状态", ""));
  for (const [value, label] of Object.entries(STATUS_LABELS)) statusFilter.appendChild(new Option(label, value));
  const newSite = el("newTaskForm").elements.site_id;
  clear(newSite);
  for (const adapter of state.meta.adapters) newSite.appendChild(new Option(adapter.label, adapter.site_id));
}
function renderProfileOptions() {
  const select = el("newTaskForm").elements.model_profile_id;
  const current = select.value;
  clear(select);
  select.appendChild(new Option("稍后选择", ""));
  for (const profile of state.profiles) select.appendChild(new Option(`${profile.name} · v${profile.version}`, profile.profile_id));
  if ([...select.options].some((option) => option.value === current)) select.value = current;
}
function renderTaskList() {
  const list = el("taskList");
  clear(list);
  const search = el("taskSearch").value.trim().toLowerCase();
  const site = el("siteFilter").value;
  const status = el("statusFilter").value;
  const tasks = state.tasks.filter((task) => {
    if (site && task.site_id !== site) return false;
    if (status && task.status !== status) return false;
    if (!search) return true;
    return `${task.title} ${task.site_id} ${siteLabel(task.site_id)}`.toLowerCase().includes(search);
  });
  for (const task of tasks) {
    const card = node("button", "task-card");
    card.type = "button";
    if (task.task_id === state.activeTaskId) card.classList.add("selected");
    const top = node("div", "task-card-top");
    top.appendChild(node("span", "task-card-title", task.title));
    top.appendChild(statusBadge(task));
    const meta = node("div", "task-card-meta");
    meta.appendChild(node("span", "", siteLabel(task.site_id)));
    meta.appendChild(node("span", "", WIZARD_LABELS[wizardStepForTask(task)] || STEP_LABELS[task.current_step] || task.current_step));
    meta.appendChild(node("span", "", formatTime(task.updated_at)));
    card.append(top, meta);
    card.addEventListener("click", () => selectTask(task.task_id));
    list.appendChild(card);
  }
  if (!tasks.length) list.appendChild(node("p", "form-hint", "没有匹配的任务。"));
}
function renderQueueSummary() {
  const summary = el("queueSummary");
  const active = state.queue.find((item) => item.state === "active");
  const queued = state.queue.filter((item) => item.state === "queued").length;
  if (!active && !queued) { summary.hidden = true; return; }
  const task = state.tasks.find((item) => item.task_id === active?.task_id);
  summary.hidden = false;
  summary.textContent = active
    ? `当前执行：${task?.title || "任务"} · ${active.operation}${queued ? ` · 另有 ${queued} 项等待` : ""}`
    : `有 ${queued} 项操作等待执行。`;
}
async function refreshOverview() {
  if (state.shuttingDown || state.overviewRefreshing) return;
  state.overviewRefreshing = true;
  try {
    const [overview, profiles] = await Promise.all([api("/api/overview"), api("/api/profiles")]);
    state.tasks = overview.tasks;
    state.queue = overview.queue;
    state.profiles = profiles.profiles;
    el("serviceStatus").textContent = "本机服务已连接";
    el("serviceStatus").className = "service-status ready";
    if (!state.activeTaskId && state.tasks.length) state.activeTaskId = state.tasks[0].task_id;
    if (state.activeTaskId && !state.tasks.some((task) => task.task_id === state.activeTaskId)) state.activeTaskId = state.tasks[0]?.task_id || null;
    renderTaskList();
    renderQueueSummary();
    renderProfileOptions();
    renderProfileList();
  } catch (error) {
    if (!state.shuttingDown) {
      el("serviceStatus").textContent = "本机服务不可用";
      el("serviceStatus").className = "service-status error";
    }
  } finally {
    state.overviewRefreshing = false;
  }
}
async function selectTask(taskId) {
  state.activeTaskId = taskId;
  localStorage.setItem("autolearn.activeTask", taskId);
  state.detail = null; state.detailTaskId = null; state.detailRevision = null;
  state.review = null; state.reviewError = null; state.answerMap = new Map();
  state.confirmationId = null; state.modelAuthorizationId = null; state.submitPending = null;
  state.wizardTaskId = null; state.wizardStep = null; state.wizardDerivedStep = null;
  state.runtimeEvents = []; state.runtimeSequence = 0;
  renderTaskList();
  await refreshDetail();
}

async function refreshDetail(force = false) {
  if (state.shuttingDown) return;
  if (!state.activeTaskId) { renderEmptyDetail(); return; }
  if (state.detailRefreshing) return;
  state.detailRefreshing = true;
  const taskId = state.activeTaskId;
  const switchedTask = state.detailTaskId !== taskId;
  if (switchedTask) {
    state.detailTaskId = taskId;
    state.review = null; state.reviewError = null; state.answerMap = new Map();
    state.submitPending = null;
    state.wizardTaskId = null; state.wizardStep = null; state.wizardDerivedStep = null;
    state.runtimeEvents = []; state.runtimeSequence = 0;
  }
  try {
    const detail = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
    if (state.activeTaskId !== taskId) return;
    const revision = [detail.task.updated_at, detail.task.status, detail.task.queue_state, detail.task.queue_position, detail.task.interaction_retry_authorized].join("|");
    const changed = force || revision !== state.detailRevision;
    state.detail = detail;
    state.cookieStatus = detail.cookie_status || { captured: false, configured: false };
    const runtime = await api(`/api/tasks/${encodeURIComponent(taskId)}/runtime-events?after=${switchedTask ? 0 : state.runtimeSequence}`);
    if (state.activeTaskId !== taskId) return;
    for (const event of runtime.events || []) {
      if (event.task_id === taskId) state.runtimeEvents.push(event);
    }
    state.runtimeEvents = state.runtimeEvents.slice(-300);
    state.runtimeSequence = runtime.latest_sequence ?? state.runtimeSequence;
    syncWizardStep(detail.task);
    if (changed) {
      state.detailRevision = revision;
      state.review = null; state.reviewError = null; state.answerMap = new Map();
      if (REVIEW_STATUSES.has(detail.task.status) || detail.task.status === "ready_for_interaction") {
        try {
          state.review = await api(`/api/tasks/${encodeURIComponent(taskId)}/review`);
          buildAnswerMap(detail, state.review);
        } catch (error) { state.reviewError = error.message; }
      }
    }
    if (detail.task.status === "waiting_model_authorization" || (detail.task.status === "paused" && detail.task.pending_operation === "solve")) {
      const pending = await api(`/api/tasks/${encodeURIComponent(taskId)}/model-authorization`);
      if (pending.pending) showModelAuthorization(pending.pending);
    }
    if (detail.task.status === "awaiting_submit_confirmation") {
      const pending = await api(`/api/tasks/${encodeURIComponent(taskId)}/submit-confirmation`);
      state.submitPending = pending.pending || null;
      if (pending.pending) showSubmitConfirmation(pending.pending);
    } else {
      state.submitPending = null;
    }
    if (changed) renderDetail();
  } catch (error) {
    if (state.activeTaskId === taskId) renderEmptyDetail(error.message);
  } finally {
    state.detailRefreshing = false;
  }
}
function renderEmptyDetail(errorMessage) {
  const pane = el("detailPane");
  clear(pane);
  const empty = node("div", "empty-workspace");
  empty.appendChild(node("div", "empty-symbol", "AL"));
  empty.appendChild(node("h2", "", errorMessage ? "任务暂不可用" : "选择一个任务"));
  empty.appendChild(node("p", "", errorMessage || "从左侧任务列表打开任务，查看当前阶段并执行下一步。"));
  pane.appendChild(empty);
}
function buildAnswerMap(detail, review) {
  state.answerMap = new Map();
  if (!review) return;
  const existing = new Map((review.answers || []).map((answer) => [answer.question_id, answer]));
  for (const question of review.questions) {
    const answer = existing.get(question.question_id);
    if (question.question_type === "multiple_choice") {
      state.answerMap.set(question.question_id, { type: "multiple_choice", values: new Set(answer?.values || []) });
    } else if (question.question_type === "fill_blank") {
      const values = new Map((answer?.values || []).map((item) => [item.slot_id, item.value]));
      state.answerMap.set(question.question_id, { type: "fill_blank", values });
    } else {
      state.answerMap.set(question.question_id, { type: question.question_type, value: answer?.value || "" });
    }
  }
}
function card(title, description) {
  const section = node("section", "content-card");
  if (title) section.appendChild(node("h3", "", title));
  if (description) section.appendChild(node("p", "card-description", description));
  return section;
}
function infoGrid(items) {
  const grid = node("div", "settings-summary");
  for (const [label, value] of items) {
    const cell = node("div", "info-cell");
    cell.appendChild(node("div", "info-cell-label", label));
    cell.appendChild(node("div", "info-cell-value", value));
    grid.appendChild(cell);
  }
  return grid;
}
function actionButton(label, handler, variant = "primary") {
  const button = node("button", `button button-${variant}`, label);
  button.type = "button";
  button.addEventListener("click", handler);
  return button;
}
function renderDetail() {
  if (!state.detail) return;
  const task = state.detail.task;
  const pane = el("detailPane");
  clear(pane);
  const header = node("header", "detail-header");
  const heading = node("div", "detail-title-wrap");
  heading.appendChild(node("p", "eyebrow", "TASK WORKSPACE"));
  heading.appendChild(node("h2", "detail-title", task.title));
  const subtitle = node("div", "detail-subtitle");
  subtitle.appendChild(statusBadge(task));
  subtitle.appendChild(node("span", "", siteLabel(task.site_id)));
  subtitle.appendChild(node("span", "", `更新 ${formatTime(task.updated_at)}`));
  heading.appendChild(subtitle);
  header.appendChild(heading);
  const headerActions = node("div", "detail-header-actions");
  headerActions.appendChild(actionButton("刷新", () => refreshDetail(true), "secondary"));
  header.appendChild(headerActions);
  pane.appendChild(header);
  const status = node("section", "hero-status");
  const statusMain = node("div", "hero-status-main");
  statusMain.appendChild(statusBadge(task));
  statusMain.appendChild(node("strong", "hero-status-label", WIZARD_LABELS[state.wizardStep] || STEP_LABELS[task.current_step] || task.current_step));
  status.appendChild(statusMain);
  if (task.last_safe_error_category) status.appendChild(node("div", "hero-status-detail", `最近的安全错误分类：${task.last_safe_error_category}`));
  if (task.queue_state === "queued" && task.queue_position) status.appendChild(node("div", "hero-status-detail", `队列位置：第 ${task.queue_position} 位。`));
  pane.appendChild(status);
  syncWizardStep(task);
  pane.appendChild(renderStepper(task));
  pane.appendChild(renderWizardPage(task));
  pane.appendChild(renderWizardNav(task));
  if (state.detail.queue) {
    const queue = card("当前串行操作", "后端一次只执行一个耗时操作。");
    queue.appendChild(infoGrid([["操作", state.detail.queue.operation], ["状态", state.detail.queue.state], ["队列位置", state.detail.queue.queue_position || "-"], ["允许覆盖", state.detail.queue.overwrite_result ? "是" : "否"]]));
    pane.appendChild(queue);
  }
  pane.appendChild(renderEventCard());
  pane.appendChild(renderManagementCard(task));
}
function wizardIndex(step) { return WIZARD_STEP_IDS.indexOf(step); }
function stepFromTaskStep(step) {
  if (step === "crawl") return "crawl";
  if (step === "package_review") return "package_review";
  if (step === "model_authorization") return "model_authorization";
  if (step === "solve") return "solve";
  if (step === "answer_review" || step === "interaction") return "answer_review";
  if (step === "submit_confirmation" || step === "result") return "submit_confirmation";
  return "authentication";
}
function wizardStepForTask(task) {
  if (["draft", "authenticating"].includes(task.status)) return "authentication";
  if (task.status === "crawling") return "crawl";
  if (task.status === "package_review") return "package_review";
  if (task.status === "waiting_model_authorization") return "model_authorization";
  if (task.status === "solving") return "solve";
  if (["answer_review", "ready_for_interaction", "filling"].includes(task.status)) return "answer_review";
  if (["awaiting_submit_confirmation", "submitting", "cancelling", "filled_unsubmitted", "confirmed", "unconfirmed", "cancelled", "needs_inspection", "archived"].includes(task.status)) return "submit_confirmation";
  if (task.status === "failed") {
    if (task.last_operation === "authenticate") return "authentication";
    if (task.last_operation === "crawl") return "crawl";
    if (task.last_operation === "solve") return "solve";
    if (task.last_operation === "interact") return "answer_review";
    return stepFromTaskStep(task.current_step);
  }
  if (["queued", "paused"].includes(task.status)) return stepFromTaskStep(task.current_step);
  return stepFromTaskStep(task.current_step);
}
function syncWizardStep(task) {
  const derived = wizardStepForTask(task);
  const previousDerivedIndex = wizardIndex(state.wizardDerivedStep);
  if (state.wizardTaskId !== task.task_id || !WIZARD_STEP_IDS.includes(state.wizardStep)) {
    state.wizardTaskId = task.task_id;
    state.wizardStep = derived;
  } else if (wizardIndex(derived) > previousDerivedIndex) {
    state.wizardStep = derived;
  }
  state.wizardDerivedStep = derived;
}
function isTaskBusy(task) {
  return state.busy || Boolean(task.queue_state) || ["authenticating", "crawling", "solving", "filling", "submitting", "cancelling"].includes(task.status);
}
function cookieReady() { return Boolean(state.cookieStatus.captured && state.cookieStatus.configured); }
function addPageAction(section, label, handler, variant = "primary", disabled = false) {
  const button = actionButton(label, handler, variant);
  button.disabled = disabled;
  section.appendChild(button);
  return button;
}
function renderStepper(task) {
  const stepper = node("div", "stepper");
  const selectedIndex = wizardIndex(state.wizardStep);
  const derivedIndex = wizardIndex(state.wizardDerivedStep || wizardStepForTask(task));
  WIZARD_STEPS.forEach(([step, label], index) => {
    const item = node("div", "step", label);
    if (index < derivedIndex) item.classList.add("done");
    if (index === derivedIndex && index !== selectedIndex) item.classList.add("reached");
    if (index === selectedIndex) item.classList.add("current");
    stepper.appendChild(item);
  });
  return stepper;
}
function renderWizardPage(task) {
  if (state.wizardStep === "authentication") return renderAuthenticationPage(task);
  if (state.wizardStep === "crawl") return renderCrawlPage(task);
  if (state.wizardStep === "package_review") return renderPackageReviewPage(task);
  if (state.wizardStep === "model_authorization") return renderModelAuthorizationPage(task);
  if (state.wizardStep === "solve") return renderSolvePage(task);
  if (state.wizardStep === "answer_review") return renderAnswerReviewPage(task);
  return renderSubmitConfirmationPage(task);
}
function renderAuthenticationPage(task) {
  const section = card("登录验证", "先在本机浏览器完成登录并抓取 Cookie。只有检测到本任务的 Cookie 已抓取且当前配置可用，下一步才会开放。");
  section.appendChild(infoGrid([
    ["任务状态", STATUS_LABELS[task.status] || task.status],
    ["Cookie 是否已抓取", state.cookieStatus.captured ? "是" : "否"],
    ["当前 Cookie 配置", state.cookieStatus.configured ? "可用" : "缺失"]
  ]));
  if (isTaskBusy(task) && task.last_operation === "authenticate") {
    section.appendChild(node("div", "notice info", "正在打开登录页面并等待认证完成。"));
    addPageAction(section, "停止登录验证", () => stopTask(task), "quiet");
  } else if (task.status === "failed" && task.last_operation === "authenticate") {
    section.appendChild(node("div", "notice danger", "上次登录验证失败，可以重新抓取 Cookie。"));
    addPageAction(section, "重试抓取 Cookie", () => runAction(() => api(`/api/tasks/${task.task_id}/authenticate`, { method: "POST", body: { retry_failed: true } }), "已重试登录验证。"));
  } else {
    const label = state.cookieStatus.captured ? "更新登录验证" : "抓取 Cookie";
    addPageAction(section, label, () => runAction(() => api(`/api/tasks/${task.task_id}/authenticate`, { method: "POST", body: {} }), "已加入登录验证队列。"));
    if (!state.cookieStatus.captured && task.status === "draft") addPageAction(section, "修改任务设置", openEditTask, "secondary");
  }
  if (state.cookieStatus.captured && !state.cookieStatus.configured) {
    section.appendChild(node("div", "notice warning", "任务记录显示抓取过 Cookie，但当前配置中没有可用 Cookie；请重新抓取。"));
  } else if (state.cookieStatus.captured) {
    section.appendChild(node("div", "notice success", "本任务已完成 Cookie 抓取，可以进入下一步抓取题目。"));
  } else {
    section.appendChild(node("div", "notice info", "此任务尚未抓取 Cookie，请先点击“抓取 Cookie”。"));
  }
  return section;
}
function renderCrawlPage(task) {
  const section = card("抓取题目", "确认 Cookie 后，本页只负责抓取题目和图片；Cookie 操作请返回上一步。");
  section.appendChild(infoGrid([
    ["Cookie", cookieReady() ? "可用" : "未准备好"],
    ["题包", task.run_id || "尚未生成"]
  ]));
  if (isTaskBusy(task) && task.last_operation === "crawl") {
    section.appendChild(node("div", "notice info", "正在请求页面并生成题包。"));
    addPageAction(section, "停止抓取", () => stopTask(task), "quiet");
  } else if (task.status === "failed" && task.last_operation === "crawl") {
    section.appendChild(node("div", "notice danger", "上次抓取失败，可以重试。"));
    addPageAction(section, "重试抓取题目", () => runAction(() => api(`/api/tasks/${task.task_id}/crawl`, { method: "POST", body: { retry_failed: true } }), "已重试抓取。"), "primary", !cookieReady());
  } else if (task.run_id) {
    section.appendChild(node("div", "notice success", `题包已生成：${task.run_id}`));
  } else {
    section.appendChild(node("div", "notice info", "登录验证完成后，点击下面的按钮抓取题目。"));
    addPageAction(section, "抓取题目", () => runAction(() => api(`/api/tasks/${task.task_id}/crawl`, { method: "POST", body: { retry_failed: false } }), "已加入抓取队列。"), "primary", !cookieReady());
  }
  return section;
}
function renderPackageReviewPage(task) {
  if (state.review) return renderReviewCard(false);
  const section = card("检查题包", "题包尚未读取完成。");
  section.appendChild(node("div", state.reviewError ? "notice danger" : "notice info", state.reviewError || "正在读取题目和附件，请稍后刷新。"));
  return section;
}
function renderModelAuthorizationPage(task) {
  const section = card("模型授权", "选择本次任务使用的模型档案。点击下一步后会显示题目数量、图片数量和目标服务，确认后才开始解题。");
  const profile = state.profiles.find((item) => item.profile_id === task.model_profile_id);
  const label = node("label", "", "模型档案");
  const select = node("select");
  select.appendChild(new Option("请选择模型档案", ""));
  for (const item of state.profiles) select.appendChild(new Option(`${item.name} · v${item.version}`, item.profile_id));
  select.value = profile ? profile.profile_id : "";
  select.disabled = task.status === "waiting_model_authorization";
  select.addEventListener("change", () => changeTaskModelProfile(task, select.value));
  label.appendChild(select);
  section.appendChild(label);
  if (!profile) section.appendChild(node("div", "notice warning", "请选择有效的模型档案后才能继续。"));
  else section.appendChild(node("div", "notice success", `将使用：${profile.name} · ${profile.settings.model || "未命名模型"}`));
  if (state.review) section.appendChild(infoGrid([["题目数量", `${state.review.questions.length} 题`], ["题包", task.run_id || "-"]]));
  return section;
}
function renderSolveProgress() {
  const stages = {
    started: [8, "正在准备解题任务"],
    model_request_started: [38, "正在请求模型服务"],
    model_response_received: [74, "模型已返回，正在解析答案"],
    answers_written: [94, "答案已写入，正在校验题包"],
    completed: [100, "解题完成"]
  };
  const solveEvents = state.runtimeEvents.filter((event) => event.stage === "solve");
  const latest = solveEvents[solveEvents.length - 1];
  const [percent, label] = stages[latest?.status] || [5, "正在等待解题进度"];
  const wrapper = node("div", "solve-progress");
  const heading = node("div", "solve-progress-heading");
  heading.append(node("strong", "", label), node("span", "", `${percent}%`));
  const track = node("div", "progress-track");
  const fill = node("div", "progress-fill");
  fill.style.width = `${percent}%`;
  track.appendChild(fill);
  wrapper.append(heading, track);
  if (latest?.question_count) wrapper.appendChild(node("div", "form-hint", `题目数量：${latest.question_count}`));
  return wrapper;
}
function renderSolvePage(task) {
  const section = card("AI 解题", "本页不需要操作。解题完成后会自动进入答案复核。");
  section.appendChild(renderSolveProgress());
  section.appendChild(infoGrid([["状态", STATUS_LABELS[task.status] || task.status], ["题包", task.run_id || "-"]]));
  if (task.status === "failed" && task.last_operation === "solve") {
    section.appendChild(node("div", "notice danger", "上次解题失败，可以重新授权并覆盖已有答案。"));
    addPageAction(section, "重新授权解题", () => requestModelAuthorization(task, { retry_failed: true, overwrite_existing_result: true }));
  } else if (isTaskBusy(task)) {
    addPageAction(section, "停止解题", () => stopTask(task), "quiet");
  } else {
    section.appendChild(node("div", "notice info", "解题任务正在队列中或已准备自动进入下一步。"));
  }
  return section;
}
function renderAnswerReviewPage(task) {
  if (["ready_for_interaction", "filling"].includes(task.status)) {
    const section = card("复核答案", "答案已确认，正在填写网页。此步骤不单独显示填写页面，完成后会自动进入提交确认。");
    section.appendChild(renderFillingProgress(task));
    if (task.status === "filling") addPageAction(section, "停止填写", () => stopTask(task), "quiet");
    return section;
  }
  if (["filled_unsubmitted", "unconfirmed", "needs_inspection"].includes(task.status) || (task.status === "failed" && task.last_operation === "interact")) {
    const section = card("复核答案", "上次填写结果需要检查，检查网页后可以重新填写。");
    addPageAction(section, "我已检查，重新填写", () => retryInteraction(task));
    return section;
  }
  if (!state.review) {
    const section = card("复核答案", "答案尚未读取完成。");
    section.appendChild(node("div", state.reviewError ? "notice danger" : "notice info", state.reviewError || "正在读取答案，请稍后刷新。"));
    return section;
  }
  const wrap = node("div", "wizard-page-stack");
  wrap.appendChild(renderReviewCard(true));
  const actions = card("答案操作", "保存并校验后，点击下一步开始填写网页。");
  const row = node("div", "action-bar");
  const save = actionButton("保存并校验答案", saveReviewedAnswers, "secondary");
  save.disabled = task.status !== "answer_review" || task.queue_state !== null;
  row.appendChild(save);
  actions.appendChild(row);
  wrap.appendChild(actions);
  return wrap;
}
function renderFillingProgress(task) {
  const wrapper = node("div", "solve-progress");
  const heading = node("div", "solve-progress-heading");
  heading.append(node("strong", "", task.status === "filling" ? "正在填写答案" : "等待填写任务开始"), node("span", "", task.status === "filling" ? "进行中" : "排队中"));
  const track = node("div", "progress-track");
  const fill = node("div", "progress-fill indeterminate");
  track.appendChild(fill);
  wrapper.append(heading, track);
  return wrapper;
}
function renderSubmitConfirmationPage(task) {
  const section = card("提交确认", "页面结构预检、填写和回显验证完成后，才会在这里出现一次性提交确认。");
  if (state.submitPending) {
    section.appendChild(infoGrid([["题包", state.submitPending.run_id], ["题目数量", state.submitPending.question_count], ["有效期", formatTime(state.submitPending.expires_at)]]));
    section.appendChild(node("div", "notice warning", "确认后仍会重新检查网页上的提交控件；取消、超时或错配不会提交。"));
    addPageAction(section, "打开提交确认", () => showSubmitConfirmation(state.submitPending));
  } else if (task.status === "submitting") {
    section.appendChild(node("div", "notice info", "已确认提交，正在等待网站页面结果。"));
  } else if (task.status === "cancelling") {
    section.appendChild(node("div", "notice warning", "正在等待安全停止，不会自动重试提交。"));
  } else if (["filled_unsubmitted", "confirmed", "failed", "unconfirmed", "cancelled", "needs_inspection", "archived"].includes(task.status)) {
    section.appendChild(infoGrid([["最终状态", STATUS_LABELS[task.status] || task.status], ["题包", task.run_id || "-"]]));
    if (["filled_unsubmitted", "unconfirmed", "needs_inspection"].includes(task.status)) addPageAction(section, "我已检查，重新填写", () => retryInteraction(task), "secondary");
  } else {
    section.appendChild(node("div", "notice info", "正在等待填写步骤完成。"));
  }
  return section;
}
function canAdvanceWizard(task) {
  const selectedIndex = wizardIndex(state.wizardStep);
  const derivedIndex = wizardIndex(state.wizardDerivedStep || wizardStepForTask(task));
  if (selectedIndex < derivedIndex) return true;
  if (task.status === "failed" || isTaskBusy(task)) return false;
  if (state.wizardStep === "authentication") return cookieReady();
  if (state.wizardStep === "crawl") return Boolean(task.run_id);
  if (state.wizardStep === "package_review") return Boolean(state.review) && !state.reviewError;
  if (state.wizardStep === "model_authorization") return task.status === "package_review" && state.profiles.some((item) => item.profile_id === task.model_profile_id);
  if (state.wizardStep === "answer_review") return task.status === "answer_review" && Boolean(state.review) && !state.reviewError;
  if (state.wizardStep === "submit_confirmation") return Boolean(state.submitPending);
  return false;
}
function nextButtonLabel(task) {
  if (state.wizardStep === "authentication") return "下一步：抓取题目";
  if (state.wizardStep === "crawl") return "下一步：检查题包";
  if (state.wizardStep === "package_review") return "下一步：模型授权";
  if (state.wizardStep === "model_authorization") return "下一步：授权并解题";
  if (state.wizardStep === "solve") return "等待 AI 解题完成";
  if (state.wizardStep === "answer_review") return "填写答案";
  if (state.wizardStep === "submit_confirmation") return state.submitPending ? "打开提交确认" : "等待提交确认";
  return "下一步";
}
function renderWizardNav(task) {
  const selectedIndex = wizardIndex(state.wizardStep);
  const section = node("section", "wizard-nav");
  const previous = actionButton("上一步", () => handleWizardPrevious(task), "secondary");
  previous.disabled = selectedIndex <= 0 || state.wizardStep === "submit_confirmation" || isTaskBusy(task);
  const next = actionButton(nextButtonLabel(task), () => handleWizardNext(task), "primary");
  next.disabled = !canAdvanceWizard(task);
  next.title = next.disabled ? "完成当前页面任务后才能进入下一步" : "";
  section.append(previous, next);
  return section;
}
async function handleWizardPrevious(task) {
  const currentIndex = wizardIndex(state.wizardStep);
  if (currentIndex <= 0 || state.wizardStep === "submit_confirmation" || isTaskBusy(task)) return;
  if (state.wizardStep === "model_authorization" && task.status === "waiting_model_authorization") {
    if (!window.confirm("返回上一步会取消当前模型授权请求，是否继续？")) return;
    try {
      await api(`/api/tasks/${task.task_id}/model-authorization/response`, { method: "POST", body: { accepted: false } });
      await refreshOverview();
      await refreshDetail(true);
    } catch (error) { toast(error.message, "error"); return; }
  }
  state.wizardStep = WIZARD_STEP_IDS[currentIndex - 1];
  renderDetail();
}
async function handleWizardNext(task) {
  const selectedIndex = wizardIndex(state.wizardStep);
  const derivedIndex = wizardIndex(state.wizardDerivedStep || wizardStepForTask(task));
  if (selectedIndex < derivedIndex) {
    state.wizardStep = WIZARD_STEP_IDS[Math.min(selectedIndex + 1, derivedIndex)];
    renderDetail();
    return;
  }
  if (state.wizardStep === "model_authorization") {
    await requestModelAuthorization(task, {});
    return;
  }
  if (state.wizardStep === "answer_review") {
    await startAnswerFilling();
    return;
  }
  if (state.wizardStep === "submit_confirmation") {
    if (state.submitPending) showSubmitConfirmation(state.submitPending);
    return;
  }
  const nextIndex = selectedIndex + 1;
  if (nextIndex < WIZARD_STEP_IDS.length) {
    state.wizardStep = WIZARD_STEP_IDS[nextIndex];
    renderDetail();
  }
}
async function changeTaskModelProfile(task, profileId) {
  if (!profileId || profileId === task.model_profile_id) return;
  try {
    await api(`/api/tasks/${task.task_id}`, { method: "PUT", body: { model_profile_id: profileId } });
    await refreshOverview();
    await refreshDetail(true);
  } catch (error) { toast(error.message, "error"); }
}
async function startAnswerFilling() {
  if (!state.review || state.busy) return;
  if (!window.confirm("确认答案无误并开始填写网页吗？填写完成后不会自动提交。")) return;
  state.busy = true;
  try {
    const answers = collectAnswers();
    const saved = await api(`/api/tasks/${state.detail.task.task_id}/answers`, {
      method: "PUT",
      body: { answers, revision: state.review.answers_revision }
    });
    state.review.answers_revision = saved.answers_revision;
    await api(`/api/tasks/${state.detail.task.task_id}/approve-answers`, { method: "POST", body: {} });
    await api(`/api/tasks/${state.detail.task.task_id}/interaction`, { method: "POST", body: { overwrite_existing_result: false } });
    toast("答案已确认，已加入填写队列。", "success");
    await refreshOverview();
    await refreshDetail(true);
  } catch (error) {
    toast(error.message || "无法开始填写。", "error");
  } finally {
    state.busy = false;
  }
}
async function stopTask(task) {
  if (!window.confirm("确定要停止当前任务操作吗？后端会在安全位置停止，不会自动重试提交。")) return;
  await runAction(() => api(`/api/tasks/${task.task_id}/stop`, { method: "POST", body: {} }), "已请求停止。");
}
async function retryInteraction(task) {
  if (!window.confirm("请先亲自检查网站页面和已经保存的内容。确认页面状态后，是否授权重新填写并覆盖本地交互结果？")) return;
  await runAction(() => api(`/api/tasks/${task.task_id}/interaction/retry`, { method: "POST", body: { page_inspected: true, overwrite_existing_result: true } }), "已授权并加入填写队列。");
}

function renderReviewCard(editableOverride) {
  const review = state.review;
  const task = state.detail.task;
  const editable = editableOverride === undefined
    ? task.status === "answer_review" || (task.status === "ready_for_interaction" && task.interaction_retry_authorized)
    : Boolean(editableOverride);
  const section = card(`题目与答案 · ${review.questions.length} 题`, editable ? "答案只写回当前题包的 answers.json。保存前会按适配器 Schema 和引用关系重新校验。" : "当前阶段可查看题包，答案编辑将在解题完成后开放。");
  const list = node("div", "question-list");
  review.questions.forEach((question) => list.appendChild(renderQuestion(question, editable)));
  section.appendChild(list);
  return section;
}
function renderQuestion(question, editable) {
  const item = node("article", "question-card");
  const heading = node("div", "question-heading");
  heading.appendChild(node("strong", "question-number", `第 ${question.number} 题`));
  heading.appendChild(node("span", "question-type", question.source_type || question.question_type));
  item.appendChild(heading);
  item.appendChild(node("div", "question-stem", question.stem.text || "（无文字题干，请查看附件）"));
  item.appendChild(renderAssets(question.stem.assets));
  item.appendChild(editable ? renderAnswerEditor(question, editable) : renderQuestionOptions(question));
  return item;
}
function renderQuestionOptions(question) {
  const wrapper = node("div", "answer-editor readonly-options");
  for (const option of question.options || []) {
    const row = node("div", "option-editor");
    row.appendChild(node("span", "slot-label", option.label || ""));
    row.appendChild(node("div", "", option.content?.text || "（图片选项）"));
    wrapper.appendChild(row);
    wrapper.appendChild(renderAssets(option.content?.assets || []));
  }
  if (!(question.options || []).length) wrapper.appendChild(node("p", "form-hint", "此题没有选项。"));
  return wrapper;
}
function renderAssets(assets) {
  const grid = node("div", "asset-grid");
  for (const source of assets || []) {
    const image = node("img");
    image.src = source;
    image.alt = "题目附件";
    image.loading = "lazy";
    grid.appendChild(image);
  }
  return grid;
}
function renderAnswerEditor(question, editable) {
  const wrapper = node("div", "answer-editor");
  const answer = state.answerMap.get(question.question_id);
  if (!editable) return wrapper;
  if (question.question_type === "single_choice") {
    for (const option of question.options) {
      const row = node("div", "option-editor");
      const input = node("input"); input.type = "radio"; input.name = `answer_${question.question_id}`; input.value = option.option_id; input.checked = answer.value === option.option_id;
      input.addEventListener("change", () => { answer.value = option.option_id; });
      const label = node("label", "", `${option.label}. ${option.content.text || "（图片选项）"}`);
      row.append(input, label); wrapper.appendChild(row); wrapper.appendChild(renderAssets(option.content.assets));
    }
  } else if (question.question_type === "multiple_choice") {
    for (const option of question.options) {
      const row = node("div", "option-editor");
      const input = node("input"); input.type = "checkbox"; input.value = option.option_id; input.checked = answer.values.has(option.option_id);
      input.addEventListener("change", () => { if (input.checked) answer.values.add(option.option_id); else answer.values.delete(option.option_id); });
      const label = node("label", "", `${option.label}. ${option.content.text || "（图片选项）"}`);
      row.append(input, label); wrapper.appendChild(row); wrapper.appendChild(renderAssets(option.content.assets));
    }
  } else if (question.question_type === "fill_blank") {
    for (const slot of question.answer_slots) {
      const field = node("div", "slot-editor");
      field.appendChild(node("span", "slot-label", `空位 ${slot.ordinal}`));
      const input = node("input"); input.type = "text"; input.value = answer.values.get(slot.slot_id) || "";
      input.addEventListener("input", () => answer.values.set(slot.slot_id, input.value));
      field.appendChild(input); wrapper.appendChild(field);
    }
  } else {
    const input = node("textarea"); input.value = answer.value || ""; input.placeholder = "填写纯文本答案";
    input.addEventListener("input", () => { answer.value = input.value; });
    wrapper.appendChild(input);
  }
  return wrapper;
}
async function saveReviewedAnswers() {
  if (!state.review || state.busy) return;
  let answers;
  try { answers = collectAnswers(); } catch (error) { toast(error.message, "error"); return; }
  state.busy = true;
  try {
    const result = await api(`/api/tasks/${state.detail.task.task_id}/answers`, {
      method: "PUT",
      body: { answers, revision: state.review.answers_revision }
    });
    state.review.answers_revision = result.answers_revision;
    toast("答案已保存并通过校验。", "success");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    state.busy = false;
  }
}
function collectAnswers() {
  const answers = [];
  for (const question of state.review.questions) {
    const answer = state.answerMap.get(question.question_id);
    if (!answer) throw new Error("答案状态不完整，请刷新后重试。");
    if (question.question_type === "single_choice") {
      if (!answer.value) throw new Error(`第 ${question.number} 题尚未选择答案。`);
      answers.push({ question_id: question.question_id, answer_type: "single_choice", value: answer.value });
    } else if (question.question_type === "multiple_choice") {
      const values = [...answer.values];
      if (!values.length) throw new Error(`第 ${question.number} 题尚未选择答案。`);
      answers.push({ question_id: question.question_id, answer_type: "multiple_choice", values });
    } else if (question.question_type === "fill_blank") {
      const values = question.answer_slots.map((slot) => ({ slot_id: slot.slot_id, value: answer.values.get(slot.slot_id) || "" }));
      if (values.some((item) => !item.value)) throw new Error(`第 ${question.number} 题存在未填写的空位。`);
      answers.push({ question_id: question.question_id, answer_type: "fill_blank", values });
    } else {
      if (!answer.value || !answer.value.trim()) throw new Error(`第 ${question.number} 题尚未填写答案。`);
      answers.push({ question_id: question.question_id, answer_type: "short_answer", value: answer.value });
    }
  }
  return answers;
}
async function requestModelAuthorization(task, extraOptions) {
  try {
    const pending = await api(`/api/tasks/${task.task_id}/model-authorization`);
    if (pending.pending) { showModelAuthorization(pending.pending); return; }
    const profile = state.profiles.find((item) => item.profile_id === task.model_profile_id);
    if (!profile) throw new Error("任务没有可用的模型档案，请先在设置中添加并选择。");
    let profileUpdateConfirmed = Boolean(extraOptions.profile_update_confirmed);
    const profileChanged = profile.updated_at && task.created_at && new Date(profile.updated_at) > new Date(task.created_at);
    if (profileChanged && !profileUpdateConfirmed) {
      profileUpdateConfirmed = window.confirm("此模型档案在任务创建后更新过。确认使用当前版本发送题目和图片吗？");
      if (!profileUpdateConfirmed) return;
    }
    const summary = await api(`/api/tasks/${task.task_id}/model-authorization`, { method: "POST", body: {
      profile_version: profile.version, profile_update_confirmed: profileUpdateConfirmed,
      retry_failed: Boolean(extraOptions.retry_failed), overwrite_existing_result: Boolean(extraOptions.overwrite_existing_result),
      resume_paused_solve: Boolean(extraOptions.resume_paused_solve)
    }});
    showModelAuthorization(summary);
  } catch (error) { toast(error.message, "error"); }
}
function showModelAuthorization(summary) {
  state.modelAuthorizationId = summary.task_id;
  const panel = el("modelAuthorizationSummary"); clear(panel);
  const rows = [["任务", summary.task_id], ["题包", summary.run_id], ["模型档案", `${summary.profile_name} · v${summary.profile_version}`], ["服务类型", summary.provider || "未指定"], ["模型", summary.model], ["服务主机", summary.service_host], ["请求目的地", summary.service_destination], ["题目数量", summary.question_count], ["图片附件", summary.image_count]];
  for (const [label, value] of rows) { const row = node("div", "summary-row"); row.append(node("span", "", label), node("span", "", value)); panel.appendChild(row); }
  openDialog("modelAuthorizationDialog");
  el("confirmModelAuthorization").onclick = () => respondModelAuthorization(true);
  el("cancelModelAuthorization").onclick = () => respondModelAuthorization(false);
}
async function respondModelAuthorization(accepted) {
  const taskId = state.modelAuthorizationId;
  if (!taskId) return;
  closeDialog("modelAuthorizationDialog"); state.modelAuthorizationId = null;
  await runAction(() => api(`/api/tasks/${taskId}/model-authorization/response`, { method: "POST", body: { accepted } }), accepted ? "已授权模型请求。" : "已取消模型授权。");
}
function showSubmitConfirmation(pending) {
  if (state.confirmationId === pending.confirmation_id) return;
  state.confirmationId = pending.confirmation_id;
  const panel = el("submitConfirmationSummary"); clear(panel);
  const rows = [["任务", pending.task_id], ["题包", pending.run_id], ["站点", siteLabel(pending.site_id)], ["题目数量", pending.question_count], ["确认有效期", formatTime(pending.expires_at)]];
  for (const [label, value] of rows) { const row = node("div", "summary-row"); row.append(node("span", "", label), node("span", "", value)); panel.appendChild(row); }
  openDialog("submitConfirmationDialog");
  el("confirmSubmitConfirmation").onclick = () => respondSubmitConfirmation("confirm");
  el("cancelSubmitConfirmation").onclick = () => respondSubmitConfirmation("cancel");
}
async function respondSubmitConfirmation(decision) {
  const taskId = state.detail?.task.task_id || state.activeTaskId;
  const confirmationId = state.confirmationId;
  if (!taskId || !confirmationId) return;
  state.confirmationId = null; closeDialog("submitConfirmationDialog");
  try {
    await api(`/api/tasks/${taskId}/submit-confirmation`, { method: "POST", body: { run_id: state.detail?.task.run_id, confirmation_id: confirmationId, decision } });
    toast(decision === "confirm" ? "已提交一次性确认。" : "已取消本次提交。", decision === "confirm" ? "success" : "info");
    await refreshOverview(); await refreshDetail();
  } catch (error) { toast(error.message, "error"); }
}
function renderEventCard() {
  const section = card("事件记录", "只显示安全状态，不包含 Cookie、密钥、完整 URL 或答案正文。");
  const list = node("div", "event-list");
  const events = [...(state.detail.events || [])].reverse().slice(-20);
  for (const event of events) {
    const row = node("div", "event-row");
    row.appendChild(node("div", "event-time", formatTime(event.occurred_at)));
    row.appendChild(node("div", "event-text", [event.event_type, event.to_status, event.safe_error_category].filter(Boolean).join(" · ")));
    list.appendChild(row);
  }
  if (!events.length) list.appendChild(node("p", "form-hint", "暂无事件记录。"));
  section.appendChild(list); return section;
}
function renderManagementCard(task) {
  const section = card("任务管理", "归档不会删除题包。永久删除会同时移除数据库记录和关联题包，无法从页面恢复。");
  const actions = node("div", "action-bar secondary-actions");
  if (task.status === "draft" && !task.run_id) actions.appendChild(actionButton("删除草稿", () => deleteTask(task, "draft"), "quiet"));
  actions.appendChild(actionButton("归档任务", () => archiveTask(task), "quiet"));
  if (task.run_id) actions.appendChild(actionButton("永久删除任务和题包", () => deleteTask(task, "package"), "danger"));
  section.appendChild(actions); return section;
}
async function archiveTask(task) {
  if (!window.confirm("确认归档此任务？归档不会删除题包，可在任务库中保留记录。")) return;
  await runAction(() => api(`/api/tasks/${task.task_id}/archive`, { method: "POST", body: {} }), "任务已归档。");
}
async function deleteTask(task, mode) {
  let body;
  if (mode === "draft") {
    if (!window.confirm("确认删除这个草稿？")) return;
    body = { mode: "draft" };
  } else {
    const typed = window.prompt("永久删除会丢失题目、附件、答案、执行结果和任务记录。请输入 DELETE 确认：");
    if (typed !== "DELETE") return;
    body = { mode: "package", confirmation: "DELETE" };
  }
  try {
    await api(`/api/tasks/${task.task_id}`, { method: "DELETE", body });
    toast(mode === "draft" ? "草稿已删除。" : "任务和题包已删除。", "success");
    state.activeTaskId = null;
    localStorage.removeItem("autolearn.activeTask");
    await refreshOverview();
    renderEmptyDetail();
  } catch (error) { toast(error.message, "error"); }
}

function openNewTask() {
  state.editTaskId = null;
  const form = el("newTaskForm");
  form.reset();
  renderProfileOptions();
  form.elements.model_profile_id.value = state.profiles.find((profile) => profile.is_default)?.profile_id || "";
  openDialog("newTaskDialog");
}
async function openEditTask() {
  if (!state.detail) return;
  state.editTaskId = state.detail.task.task_id;
  try {
    const settings = await api(`/api/tasks/${state.editTaskId}/draft-settings`);
    const form = el("newTaskForm");
    form.elements.title.value = state.detail.task.title;
    form.elements.site_id.value = state.detail.task.site_id;
    form.elements.page_url.value = settings.page_url;
    renderProfileOptions();
    form.elements.model_profile_id.value = state.detail.task.model_profile_id || "";
    openDialog("newTaskDialog");
  } catch (error) { toast(error.message, "error"); }
}
async function submitNewTask(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = {
    title: form.elements.title.value.trim(),
    site_id: form.elements.site_id.value,
    page_url: form.elements.page_url.value.trim(),
    model_profile_id: form.elements.model_profile_id.value || null
  };
  if (!payload.page_url) { toast("请填写作业网址。", "error"); return; }
  try {
    if (state.editTaskId) {
      await api(`/api/tasks/${state.editTaskId}`, { method: "PUT", body: payload });
      toast("任务设置已保存。", "success");
    } else {
      const task = await api("/api/tasks", { method: "POST", body: payload });
      state.activeTaskId = task.task_id;
      localStorage.setItem("autolearn.activeTask", task.task_id);
      toast("任务草稿已创建。", "success");
    }
    closeDialog("newTaskDialog"); state.editTaskId = null;
    await refreshOverview(); await refreshDetail();
  } catch (error) { toast(error.message, "error"); }
}
function renderProfileList() {
  const list = el("profileList"); clear(list);
  for (const profile of state.profiles) {
    const button = node("button", "profile-item");
    button.type = "button";
    if (profile.profile_id === state.profileSelection) button.classList.add("selected");
    button.appendChild(node("strong", "", profile.name));
    button.appendChild(node("span", "", `v${profile.version}${profile.is_default ? " · 默认" : ""}${profile.api_key_configured ? " · 已配置密钥" : ""}`));
    button.addEventListener("click", () => selectProfile(profile.profile_id));
    list.appendChild(button);
  }
  if (!state.profiles.length) list.appendChild(node("p", "form-hint", "还没有模型档案。"));
}
function selectProfile(profileId) {
  state.profileSelection = profileId;
  renderProfileList();
  const profile = state.profiles.find((item) => item.profile_id === profileId);
  const form = el("profileForm");
  el("deleteProfile").disabled = !profile;
  if (!profile) {
    form.reset(); form.elements.profile_id.value = "";
    form.elements.temperature.value = "0.2"; form.elements.max_output_tokens.value = "4096"; form.elements.timeout_seconds.value = "120";
    el("profileMessage").textContent = ""; return;
  }
  form.elements.profile_id.value = profile.profile_id;
  form.elements.name.value = profile.name;
  form.elements.provider.value = profile.settings.provider || "";
  form.elements.base_url.value = profile.settings.base_url || "";
  form.elements.model.value = profile.settings.model || "";
  form.elements.temperature.value = profile.settings.temperature;
  form.elements.max_output_tokens.value = profile.settings.max_output_tokens;
  form.elements.timeout_seconds.value = profile.settings.timeout_seconds;
  form.elements.api_key.value = "";
  form.elements.clear_api_key.checked = false;
  el("profileMessage").textContent = profile.api_key_configured ? "已配置 API 密钥；留空会保留当前密钥。" : "尚未配置 API 密钥。";
}
async function saveProfile(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const message = el("profileMessage");
  const payload = {
    profile_id: form.elements.profile_id.value || null,
    name: form.elements.name.value.trim(),
    settings: {
      provider: form.elements.provider.value.trim(),
      base_url: form.elements.base_url.value.trim(),
      model: form.elements.model.value.trim(),
      temperature: Number(form.elements.temperature.value),
      max_output_tokens: Number(form.elements.max_output_tokens.value),
      timeout_seconds: Number(form.elements.timeout_seconds.value)
    },
    api_key: form.elements.api_key.value || null,
    clear_api_key: form.elements.clear_api_key.checked
  };
  try {
    const profile = await api("/api/profiles", { method: "PUT", body: payload });
    message.className = "form-message success"; message.textContent = "模型档案已保存。";
    await refreshOverview(); selectProfile(profile.profile_id);
  } catch (error) {
    message.className = "form-message error"; message.textContent = error.message;
  }
}
async function makeDefaultProfile() {
  const profileId = el("profileForm").elements.profile_id.value;
  if (!profileId) { toast("请先保存档案。", "error"); return; }
  try {
    await api(`/api/profiles/${profileId}/default`, { method: "POST", body: {} });
    toast("已设为默认模型。", "success");
    await refreshOverview();
  } catch (error) { toast(error.message, "error"); }
}
function bindEvents() {
  el("openNewTask").addEventListener("click", openNewTask);
  el("openSettings").addEventListener("click", () => { if (!state.profileSelection && state.profiles.length) { const selected = state.profiles.find((profile) => profile.is_default) || state.profiles[0]; selectProfile(selected.profile_id); } openDialog("profileDialog"); });
  el("newTaskForm").addEventListener("submit", submitNewTask);
  el("profileForm").addEventListener("submit", saveProfile);
  el("newProfileButton").addEventListener("click", () => selectProfile(null));
  el("makeDefaultProfile").addEventListener("click", makeDefaultProfile);
  el("deleteProfile").addEventListener("click", deleteSelectedProfile);
  el("shutdownService").addEventListener("click", shutdownService);
  el("refreshTasks").addEventListener("click", async () => { await refreshOverview(); await refreshDetail(true); });
  el("taskSearch").addEventListener("input", renderTaskList);
  el("siteFilter").addEventListener("change", renderTaskList);
  el("statusFilter").addEventListener("change", renderTaskList);
  document.querySelectorAll("[data-close-dialog]").forEach((button) => button.addEventListener("click", () => closeDialog(button.dataset.closeDialog)));
  el("submitConfirmationDialog").addEventListener("cancel", (event) => { event.preventDefault(); respondSubmitConfirmation("cancel"); });
}
async function start() {
  bindEvents();
  try { state.meta = await api("/api/meta"); } catch (error) { toast(error.message, "error"); return; }
  renderMeta();
  await refreshOverview();
  await refreshDetail();
  window.setInterval(() => { if (!state.shuttingDown) { refreshOverview(); refreshDetail(); } }, 1600);
}
document.addEventListener("DOMContentLoaded", start);
