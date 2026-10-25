"use strict";

const STATUS_LABELS = {
  draft: "草稿", queued: "等待执行", authenticating: "登录验证中", crawling: "抓取中",
  package_review: "待检查题包", waiting_model_authorization: "等待模型授权", solving: "解题中",
  answer_review: "待复核答案", ready_for_interaction: "等待填写", filling: "填写中",
  awaiting_submit_confirmation: "等待提交确认", submitting: "提交中", cancelling: "正在停止",
  filled_unsubmitted: "已填写，未提交", confirmed: "已提交并确认", failed: "失败",
  ready: "待开始", running: "运行中", stopping: "停止中", completed: "已完成", partial: "部分完成",
  unconfirmed: "结果未确认", cancelled: "已取消", paused: "已暂停",
  needs_inspection: "需要检查后恢复", archived: "已归档"
};
const STATUS_TONES = {
  authenticating: "info", crawling: "info", solving: "info", filling: "info", submitting: "info",
  confirmed: "success", completed: "success", failed: "danger", unconfirmed: "danger", needs_inspection: "danger",
  ready: "warning", running: "info", stopping: "warning", partial: "warning",
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
const COURSE_STEPS = [
  ["setup", "任务设置"], ["authentication", "登录验证"], ["catalog", "课程目录"],
  ["learning", "学习中"], ["exam", "考试"], ["result", "查看结果"]
];
const COURSE_STEP_LABELS = Object.fromEntries(COURSE_STEPS);
const WIZARD_STEP_IDS = WIZARD_STEPS.map(([step]) => step);
const WIZARD_LABELS = Object.fromEntries(WIZARD_STEPS);
const REVIEW_STATUSES = new Set(["package_review", "answer_review"]);
const state = {
  meta: { adapters: [] }, tasks: [], queue: [], profiles: [],
  activeTaskId: localStorage.getItem("autolearn.activeTask") || null,
  detail: null, detailTaskId: null, detailRevision: null, detailEventRevision: null, detailRefreshing: false, overviewRefreshing: false, review: null, reviewError: null, answerMap: new Map(),
  confirmationId: null, modelAuthorizationId: null, profileSelection: null,
  editTaskId: null, busy: false, shuttingDown: false, courseOptionsEditing: false, courseDetailsOpen: false,
  courseQr: null, courseCatalogs: new Map(), courseCatalogSelection: new Map(),
  courseCatalogQueries: new Map(), courseCatalogFilters: new Map(),
  courseOutlines: new Map(), courseOutlineErrors: new Map(), courseOutlineLoading: new Set(), courseOutlineViews: new Map(),
  cookieStatus: { captured: false, configured: false }, submitPending: null, authenticationHint: null,
  wizardTaskId: null, wizardStep: null, wizardDerivedStep: null,
  runtimeEvents: [], runtimeSequence: 0, captchaAlertKey: null
};
let paneEntranceTimer = null;

function el(id) { return document.getElementById(id); }
function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined && text !== null) item.textContent = String(text);
  return item;
}
function clear(element) { element.replaceChildren(); }
function playPaneEntrance(pane) {
  if (paneEntranceTimer) window.clearTimeout(paneEntranceTimer);
  pane.classList.remove("is-entering");
  void pane.offsetWidth;
  pane.classList.add("is-entering");
  paneEntranceTimer = window.setTimeout(() => {
    pane.classList.remove("is-entering");
    paneEntranceTimer = null;
  }, 460);
}
function motionScrollBehavior() {
  return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
}
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
  if (task.task_type === "course") return STATUS_LABELS[task.status] || task.status;
  if (task.queue_state === "active") return "运行中";
  if (task.queue_state === "queued") return task.queue_position ? `排队中 · 第 ${task.queue_position} 位` : "排队中";
  return STATUS_LABELS[task.status] || task.status;
}
function statusBadge(task) {
  const tone = task.task_type !== "course" && task.queue_state ? "warning" : (STATUS_TONES[task.status] || "neutral");
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
  if (state.busy) return false;
  state.busy = true;
  try {
    await action();
    if (successMessage) toast(successMessage, "success");
    await refreshOverview();
    await refreshDetail();
    return true;
  } catch (error) {
    toast(error.message || "操作失败。", "error");
    return false;
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
    el("serviceStatus").textContent = "已关闭";
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

function showCaptchaRequiredDialog(task) {
  if (task.status !== "failed" || task.last_safe_error_category !== "captcha_required") return;
  const alertKey = `${task.task_id}:${task.updated_at}`;
  if (state.captchaAlertKey === alertKey) return;
  state.captchaAlertKey = alertKey;
  const dialog = el("captchaRequiredDialog");
  if (dialog && !dialog.open) dialog.showModal();
}

function showAuthenticationHint() {
  const hint = state.authenticationHint;
  const target = el("authCaptureHint");
  if (target) {
    target.textContent = hint || "浏览器已打开登录页面。请完成登录并手动进入任务里的目标页面；识别到题目页面后会自动保存 Cookie。";
  }
  openDialog("authCaptureDialog");
}
function startAuthentication(task, retryFailed) {
  if (task.task_type !== "course") {
    showAuthenticationHint();
    return runAction(
      () => api(`/api/tasks/${encodeURIComponent(task.task_id)}/authenticate`, { method: "POST", body: retryFailed ? { retry_failed: true } : {} }),
      "已加入登录验证队列。"
    );
  }
  return runAction(async () => {
    state.courseQr = null;
    state.courseCatalogs.delete(task.task_id);
    state.courseCatalogSelection.delete(task.task_id);
    await api(`/api/tasks/${encodeURIComponent(task.task_id)}/authenticate`, { method: "POST", body: retryFailed ? { retry_failed: true } : {} });
    void pollCourseAuthentication(task.task_id);
  }, "请使用下方二维码登录智慧树。");
}

async function pollCourseAuthentication(taskId) {
  for (let attempt = 0; attempt < 720; attempt += 1) {
    try {
      const result = await api(`/api/tasks/${encodeURIComponent(taskId)}/course/authentication`);
      const nextQr = result.state === "waiting_qr" && result.qr ? result.qr : null;
      if (nextQr !== state.courseQr) {
        state.courseQr = nextQr;
        if (state.detail?.task?.task_id === taskId) renderCourseDetail(state.detail.task);
      }
      if (result.state === "authenticated") {
        state.courseQr = null;
        await refreshOverview(); await refreshDetail(true);
        toast("二维码登录成功，请抓取课程目录。", "success");
        return;
      }
      if (result.state === "failed") { toast(result.error || "二维码登录失败。", "error"); await refreshDetail(true); return; }
    } catch (error) { toast(error.message || "读取登录状态失败。", "error"); return; }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  toast("二维码等待超时，请重新登录。", "error");
}

async function fetchCourseCatalog(task) {
  let warnings = [];
  let empty = false;
  const succeeded = await runAction(async () => {
    const result = await api(`/api/tasks/${encodeURIComponent(task.task_id)}/course/catalog`);
    state.courseCatalogs.set(task.task_id, Array.isArray(result.courses) ? result.courses : []);
    state.courseCatalogSelection.delete(task.task_id);
    warnings = Array.isArray(result.warnings) ? result.warnings : [];
    empty = (state.courseCatalogs.get(task.task_id) || []).length === 0;
    if (state.detail?.task?.task_id === task.task_id) renderCourseDetail(state.detail.task);
  }, null);
  // runAction owns the error toast; never turn a rejected or skipped request
  // into the success toast below (a catalog failure previously looked like
  // "课程目录已抓取" while the picker stayed empty).
  if (!succeeded) return;
  if (warnings.length) {
    const modes = warnings.map((item) => ({ shared: "共享课", hike: "校内课", ai: "AI 课程" })[item.mode] || item.mode).join("、");
    toast(`${modes}目录读取失败；成功读取的课程仍可选择。`, "error");
  } else if (empty) toast("账号课程目录为空。", "info");
  else toast("课程目录已抓取。", "success");
}

async function selectCatalogCourse(task, course) {
  const currentCourse = state.detail?.task?.task_id === task.task_id ? state.detail.course : null;
  const currentOptions = currentCourse?.options || {};
  const values = {
    ...course,
    speed: currentOptions.speed ?? 1.5,
    threshold: currentOptions.threshold ?? 0.91,
    time_limit_minutes: currentOptions.time_limit_minutes ?? 0,
    process_exam: currentOptions.process_exam ?? true,
    model_profile_id: course.mode === "ai"
      ? (currentCourse?.mode === "ai" && task.model_profile_id ? task.model_profile_id : (state.profiles.find((profile) => profile.is_default)?.profile_id || null))
      : null
  };
  const succeeded = await runAction(() => api(`/api/tasks/${encodeURIComponent(task.task_id)}/course/select`, { method: "POST", body: values }), "课程已选择。");
  if (!succeeded) return;
  state.courseCatalogs.delete(task.task_id);
  state.courseCatalogSelection.delete(task.task_id);
  state.courseOutlines.delete(task.task_id);
  state.courseOutlineErrors.delete(task.task_id);
  state.courseOutlineViews.delete(task.task_id);
  await refreshOverview(); await refreshDetail(true);
}

function courseCatalogKey(course) {
  return `${course.mode || ""}|${course.course_id || ""}|${course.class_id || ""}`;
}
function renderCourseCatalogPicker(task, courses) {
  const section = card("选择要学习的课程");
  section.classList.add("course-catalog-picker");
  section.appendChild(node("p", "form-hint", `账号中找到 ${courses.length} 门课程。选择一门后确认，即可查看学习目录。`));
  const toolbar = node("div", "course-catalog-toolbar");
  const search = node("input", "course-catalog-search");
  search.type = "search";
  search.placeholder = "搜索课程名称";
  search.setAttribute("aria-label", "搜索课程");
  search.value = state.courseCatalogQueries.get(task.task_id) || "";
  const filter = node("div", "course-catalog-filters");
  filter.setAttribute("role", "group");
  filter.setAttribute("aria-label", "按课程类型筛选");
  const currentFilter = state.courseCatalogFilters.get(task.task_id) || "all";
  [["all", "全部"], ["shared", "共享课"], ["hike", "校内课"], ["ai", "AI 课程"]].forEach(([value, label]) => {
    const count = value === "all" ? courses.length : courses.filter((item) => item.mode === value).length;
    const button = node("button", `course-catalog-filter${currentFilter === value ? " selected" : ""}`, `${label} ${count}`);
    button.type = "button";
    button.setAttribute("aria-pressed", String(currentFilter === value));
    button.addEventListener("click", () => {
      state.courseCatalogFilters.set(task.task_id, value);
      filter.querySelectorAll("button").forEach((item) => {
        const selected = item === button;
        item.classList.toggle("selected", selected);
        item.setAttribute("aria-pressed", String(selected));
      });
      applyCourseCatalogFilter(section, task.task_id);
    });
    filter.appendChild(button);
  });
  toolbar.append(search, filter);
  section.appendChild(toolbar);

  const list = node("div", "course-catalog-list");
  const selectedCourse = state.courseCatalogSelection.get(task.task_id);
  courses.forEach((course) => {
    const key = courseCatalogKey(course);
    const modeLabel = ({ shared: "共享课", hike: "校内课", ai: "AI 课程" })[course.mode] || "网课";
    const option = node("button", `course-option-card${selectedCourse && courseCatalogKey(selectedCourse) === key ? " selected" : ""}`);
    option.type = "button";
    option.dataset.courseMode = course.mode || "";
    option.dataset.courseSearch = `${course.name || ""} ${course.course_id || ""} ${course.class_id || ""}`.toLocaleLowerCase();
    option.setAttribute("aria-pressed", String(Boolean(selectedCourse && courseCatalogKey(selectedCourse) === key)));
    option.append(node("span", "course-option-type", modeLabel), node("strong", "course-option-name", course.name || "未命名课程"));
    option.appendChild(node("span", "course-option-meta", course.mode === "ai" ? `课程 ${course.course_id || "—"} · 班级 ${course.class_id || "—"}` : `课程编号 ${course.course_id || "—"}`));
    option.addEventListener("click", () => {
      state.courseCatalogSelection.set(task.task_id, course);
      for (const cardItem of list.querySelectorAll(".course-option-card")) {
        const selected = cardItem === option;
        cardItem.classList.toggle("selected", selected);
        cardItem.setAttribute("aria-pressed", String(selected));
      }
      const confirm = section.querySelector("[data-confirm-course]");
      if (confirm) confirm.disabled = false;
    });
    list.appendChild(option);
  });
  const empty = node("p", "form-hint course-catalog-empty", courses.length ? "没有匹配的课程。" : "课程列表为空。可以重新读取，或检查智慧树登录状态。");
  empty.hidden = courses.length > 0;
  list.appendChild(empty);
  search.addEventListener("input", () => {
    state.courseCatalogQueries.set(task.task_id, search.value);
    applyCourseCatalogFilter(section, task.task_id);
  });
  section.appendChild(list);
  const actions = actionRow();
  const confirm = actionButton("确认选择课程", () => {
    const course = state.courseCatalogSelection.get(task.task_id);
    if (course) void selectCatalogCourse(task, course);
  });
  confirm.dataset.confirmCourse = "true";
  confirm.disabled = !selectedCourse;
  actions.appendChild(confirm);
  if (taskCourseIsSelected(task)) {
    actions.appendChild(actionButton("取消更换", () => {
      state.courseCatalogs.delete(task.task_id);
      state.courseCatalogSelection.delete(task.task_id);
      renderCourseDetail(state.detail.task);
    }, "quiet"));
  }
  if (!courses.length) actions.appendChild(actionButton("重新读取课程列表", () => fetchCourseCatalog(task), "secondary"));
  section.appendChild(actions);
  applyCourseCatalogFilter(section, task.task_id);
  return section;
}
function taskCourseIsSelected(task) {
  const course = state.detail?.task?.task_id === task.task_id ? state.detail.course : null;
  return Boolean(course?.mode && course?.course_id);
}
function applyCourseCatalogFilter(section, taskId) {
  const query = (state.courseCatalogQueries.get(taskId) || "").trim().toLocaleLowerCase();
  const filter = state.courseCatalogFilters.get(taskId) || "all";
  let visible = 0;
  for (const option of section.querySelectorAll(".course-option-card")) {
    const matches = (filter === "all" || option.dataset.courseMode === filter) && (!query || option.dataset.courseSearch.includes(query));
    option.hidden = !matches;
    if (matches) visible += 1;
  }
  const empty = section.querySelector(".course-catalog-empty");
  if (empty && state.courseCatalogs.get(taskId)?.length) {
    empty.hidden = visible > 0;
    empty.textContent = visible ? "" : "没有匹配的课程。";
  }
}

function renderMeta() {
  const siteFilter = el("siteFilter");
  clear(siteFilter);
  siteFilter.appendChild(new Option("全部站点", ""));
  const taskFilter = el("typeFilter").value;
  for (const adapter of state.meta.adapters.filter((item) => !taskFilter || item.task_types.includes(taskFilter))) siteFilter.appendChild(new Option(adapter.label, adapter.site_id));
  const statusFilter = el("statusFilter");
  clear(statusFilter);
  statusFilter.appendChild(new Option("全部状态", ""));
  for (const [value, label] of Object.entries(STATUS_LABELS)) statusFilter.appendChild(new Option(label, value));
  const newSite = el("newTaskForm").elements.site_id;
  clear(newSite);
  applyTaskType();
}
function applyTaskType(taskType = el("newTaskForm").elements.task_type.value) {
  const form = el("newTaskForm");
  form.elements.task_type.value = taskType;
  const course = taskType === "course";
  form.querySelector('[data-section="homework"]').hidden = course;
  form.querySelector('[data-section="course"]').hidden = !course;
  form.elements.page_url.required = !course;
  const site = form.elements.site_id;
  const previous = site.value;
  const allowed = state.meta.adapters.filter((item) => item.task_types.includes(taskType));
  clear(site);
  for (const adapter of allowed) site.appendChild(new Option(adapter.label, adapter.site_id));
  if (allowed.some((item) => item.site_id === previous)) site.value = previous;
  else if (allowed.length) site.value = allowed[0].site_id;
  document.querySelectorAll(".type-switch-button").forEach((button) => button.classList.toggle("selected", button.dataset.taskType === taskType));
  if (el("typeFilter")) {
    const filter = el("typeFilter").value;
    const siteFilter = el("siteFilter");
    const selected = siteFilter.value;
    clear(siteFilter);
    siteFilter.appendChild(new Option("全部站点", ""));
    for (const adapter of state.meta.adapters.filter((item) => !filter || item.task_types.includes(filter))) siteFilter.appendChild(new Option(adapter.label, adapter.site_id));
    if ([...siteFilter.options].some((option) => option.value === selected)) siteFilter.value = selected;
  }
}
function renderProfileOptions() {
  const select = el("newTaskForm").elements.model_profile_id;
  const signature = state.profiles.map((profile) => [profile.profile_id, profile.name, profile.version, profile.is_default].join("\u0001")).join("\u0002");
  if (select.dataset.profileSignature === signature) return;
  const current = select.value;
  clear(select);
  select.appendChild(new Option("稍后选择", ""));
  for (const profile of state.profiles) select.appendChild(new Option(`${profile.name} · v${profile.version}`, profile.profile_id));
  if ([...select.options].some((option) => option.value === current)) select.value = current;
  select.dataset.profileSignature = signature;
}
function renderTaskList() {
  const list = el("taskList");
  const search = el("taskSearch").value.trim().toLowerCase();
  const site = el("siteFilter").value;
  const type = el("typeFilter").value;
  const status = el("statusFilter").value;
  const tasks = state.tasks.filter((task) => {
    if (type && task.task_type !== type) return false;
    if (site && task.site_id !== site) return false;
    if (status && task.status !== status) return false;
    if (!search) return true;
    return `${task.title} ${task.site_id} ${siteLabel(task.site_id)}`.toLowerCase().includes(search);
  });
  const sort = el("sortFilter")?.value || "updated_desc";
  tasks.sort((left, right) => {
    if (sort === "title_asc") return String(left.title || "").localeCompare(String(right.title || ""), "zh-CN");
    const difference = (Date.parse(right.updated_at || "") || 0) - (Date.parse(left.updated_at || "") || 0);
    return sort === "updated_asc" ? -difference : difference;
  });
  const existing = new Map([...list.querySelectorAll(":scope > .task-card")].map((card) => [card.dataset.taskId, card]));
  if (tasks.length) list.querySelector(":scope > .task-list-empty")?.remove();
  let position = 0;
  for (const task of tasks) {
    const signature = taskCardSignature(task);
    let card = existing.get(task.task_id);
    existing.delete(task.task_id);
    if (!card || card.dataset.cardSignature !== signature) {
      const focused = card?.contains(document.activeElement) ? document.activeElement : null;
      const focusSelector = focused?.classList.contains("task-card-menu") ? ".task-card-menu"
        : (focused?.classList.contains("task-card-main") ? ".task-card-main" : null);
      if (taskMenuTaskId === task.task_id) closeTaskMenu();
      const replacement = renderTaskCard(task, signature);
      if (card) card.replaceWith(replacement);
      card = replacement;
      if (focusSelector) card.querySelector(focusSelector)?.focus({ preventScroll: true });
    }
    card.classList.toggle("selected", card.dataset.taskId === state.activeTaskId);
    const progress = card.querySelector("[data-task-course-progress]");
    if (progress && typeof task.progress_percent === "number" && progress.textContent !== `${task.progress_percent}%`) progress.textContent = `${task.progress_percent}%`;
    const updated = card.querySelector("[data-task-updated-at]");
    const updatedText = formatTime(task.updated_at);
    if (updated && updated.textContent !== updatedText) updated.textContent = updatedText;
    const atPosition = list.children[position] || null;
    if (card !== atPosition) list.insertBefore(card, atPosition);
    position += 1;
  }
  for (const [taskId, card] of existing) {
    if (taskMenuTaskId === taskId) closeTaskMenu();
    card.remove();
  }
  if (!tasks.length && !list.querySelector(":scope > .task-list-empty")) {
    list.appendChild(node("p", "form-hint task-list-empty", "没有匹配的任务。"));
  }
}
function taskCardSignature(task) {
  return [task.status, task.title, task.site_id, task.queue_state, task.queue_position, task.task_type,
    task.current_step, task.last_operation, task.run_id, typeof task.progress_percent === "number"].join("\u0001");
}
function renderTaskCard(task, signature) {
  const card = node("div", "task-card");
  card.dataset.taskId = task.task_id;
  card.dataset.cardSignature = signature;
  if (task.task_id === state.activeTaskId) card.classList.add("selected");
  const main = node("button", "task-card-main");
  main.type = "button";
  const top = node("span", "task-card-top");
  const heading = node("span", "task-card-heading");
  heading.appendChild(node("span", "task-type-label", task.task_type === "course" ? "网课" : "做题"));
  heading.appendChild(node("span", "task-card-title", task.title));
  top.appendChild(heading);
  top.appendChild(statusBadge(task));
  const meta = node("span", "task-card-meta");
  meta.appendChild(node("span", "", siteLabel(task.site_id)));
  const stepLabel = task.task_type === "course"
    ? (COURSE_STEP_LABELS[task.current_step] || task.current_step)
    : (WIZARD_LABELS[wizardStepForTask(task)] || STEP_LABELS[task.current_step] || task.current_step);
  meta.appendChild(node("span", "", stepLabel));
  if (task.task_type === "course" && typeof task.progress_percent === "number") {
    const progress = node("span", "", `${task.progress_percent}%`);
    progress.dataset.taskCourseProgress = "true";
    meta.appendChild(progress);
  }
  const updated = node("span", "", formatTime(task.updated_at));
  updated.dataset.taskUpdatedAt = "true";
  meta.appendChild(updated);
  main.append(top, meta);
  main.addEventListener("click", () => selectTask(task.task_id));
  const menuButton = node("button", "task-card-menu");
  menuButton.type = "button";
  menuButton.setAttribute("aria-label", `任务操作：${task.title}`);
  menuButton.setAttribute("aria-haspopup", "menu");
  menuButton.setAttribute("aria-expanded", "false");
  menuButton.appendChild(kebabIcon());
  menuButton.addEventListener("click", (event) => { event.stopPropagation(); openTaskMenu(task, menuButton); });
  card.append(main, menuButton);
  return card;
}
function kebabIcon() {
  const namespace = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(namespace, "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("width", "14");
  svg.setAttribute("height", "14");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  for (const cx of ["3", "8", "13"]) {
    const dot = document.createElementNS(namespace, "circle");
    dot.setAttribute("cx", cx);
    dot.setAttribute("cy", "8");
    dot.setAttribute("r", "1.35");
    dot.setAttribute("fill", "currentColor");
    svg.appendChild(dot);
  }
  return svg;
}
let taskMenu = null;
let taskMenuTaskId = null;
function closeTaskMenu() {
  document.querySelectorAll(".task-card-menu[aria-expanded=true]").forEach((button) => button.setAttribute("aria-expanded", "false"));
  if (!taskMenu) return;
  taskMenu.remove();
  taskMenu = null;
  taskMenuTaskId = null;
  document.removeEventListener("pointerdown", onTaskMenuPointerDown, true);
  document.removeEventListener("keydown", onTaskMenuKeyDown, true);
}
function onTaskMenuPointerDown(event) {
  if (taskMenu && !taskMenu.contains(event.target)) closeTaskMenu();
}
function onTaskMenuKeyDown(event) {
  if (event.key === "Escape") closeTaskMenu();
}
function openTaskMenu(task, anchor) {
  const reopen = taskMenuTaskId === task.task_id;
  closeTaskMenu();
  if (reopen) return;
  const busy = task.task_type === "course"
    ? ["running", "stopping"].includes(task.status)
    : Boolean(task.queue_state);
  const menu = node("div", "task-menu");
  menu.setAttribute("role", "menu");
  const archive = node("button", "", "归档任务");
  archive.type = "button";
  archive.setAttribute("role", "menuitem");
  archive.disabled = busy || task.status === "archived";
  archive.title = busy ? "有等待或运行中的操作，暂不能归档" : "";
  archive.addEventListener("click", () => { closeTaskMenu(); archiveTask(task); });
  const removeLabel = task.task_type === "course" ? "删除运行记录" : (task.run_id ? "删除任务与题包" : "删除任务");
  const remove = node("button", "danger", removeLabel);
  remove.type = "button";
  remove.setAttribute("role", "menuitem");
  remove.disabled = busy;
  remove.title = busy ? "有等待或运行中的操作，暂不能删除" : "";
  remove.addEventListener("click", () => { closeTaskMenu(); openDeleteTaskDialog(task); });
  menu.append(archive, remove);
  document.body.appendChild(menu);
  const anchorBox = anchor.getBoundingClientRect();
  const menuBox = menu.getBoundingClientRect();
  const top = anchorBox.bottom + menuBox.height + 10 > window.innerHeight
    ? Math.max(10, anchorBox.top - menuBox.height - 6)
    : anchorBox.bottom + 6;
  const left = Math.max(10, Math.min(anchorBox.right - menuBox.width, window.innerWidth - menuBox.width - 10));
  menu.style.top = `${top}px`;
  menu.style.left = `${left}px`;
  anchor.setAttribute("aria-expanded", "true");
  taskMenu = menu;
  taskMenuTaskId = task.task_id;
  document.addEventListener("pointerdown", onTaskMenuPointerDown, true);
  document.addEventListener("keydown", onTaskMenuKeyDown, true);
}
function renderQueueSummary() {
  const summary = el("queueSummary");
  const active = state.queue.find((item) => item.state === "active");
  const queued = state.queue.filter((item) => item.state === "queued").length;
  if (!active && !queued) {
    summary.hidden = true;
    summary.dataset.summaryText = "";
    return;
  }
  const task = state.tasks.find((item) => item.task_id === active?.task_id);
  const text = active
    ? `${task?.title || "任务"} · ${active.operation}${queued ? ` · 另 ${queued} 项` : ""}`
    : `${queued} 项等待执行`;
  summary.hidden = false;
  if (summary.dataset.summaryText === text && summary.childElementCount === 2) return;
  if (summary.childElementCount === 2) {
    summary.lastElementChild.textContent = text;
  } else {
    summary.replaceChildren(node("span", "dot"), node("span", "", text));
  }
  summary.dataset.summaryText = text;
}
async function refreshOverview() {
  if (state.shuttingDown || state.overviewRefreshing) return;
  state.overviewRefreshing = true;
  try {
    // 只有把状态筛选切到「已归档」时才把归档任务一起取回来
    const withArchived = el("statusFilter").value === "archived";
    const [overview, profiles] = await Promise.all([
      api(`/api/overview${withArchived ? "?include_archived=true" : ""}`),
      api("/api/profiles")
    ]);
    state.tasks = overview.tasks;
    state.queue = overview.queue;
    state.profiles = profiles.profiles;
    const serviceStatus = el("serviceStatus");
    if (serviceStatus.textContent !== "已连接") serviceStatus.textContent = "已连接";
    if (serviceStatus.className !== "service-status ready") serviceStatus.className = "service-status ready";
    if (!state.activeTaskId && state.tasks.length) state.activeTaskId = state.tasks[0].task_id;
    if (state.activeTaskId && !state.tasks.some((task) => task.task_id === state.activeTaskId)) {
      const current = state.detail?.task;
      const currentTaskStillUsable = current?.task_id === state.activeTaskId && current.status !== "archived";
      if (!currentTaskStillUsable) state.activeTaskId = state.tasks[0]?.task_id || null;
    }
    renderTaskList();
    renderQueueSummary();
    renderProfileOptions();
    renderProfileList();
  } catch (error) {
    if (!state.shuttingDown) {
      const serviceStatus = el("serviceStatus");
      if (serviceStatus.textContent !== "未连接") serviceStatus.textContent = "未连接";
      if (serviceStatus.className !== "service-status error") serviceStatus.className = "service-status error";
    }
  } finally {
    state.overviewRefreshing = false;
  }
}
async function selectTask(taskId) {
  state.activeTaskId = taskId;
  localStorage.setItem("autolearn.activeTask", taskId);
  state.detail = null; state.detailTaskId = null; state.detailRevision = null;
  state.detailEventRevision = null;
  state.review = null; state.reviewError = null; state.answerMap = new Map();
  state.confirmationId = null; state.modelAuthorizationId = null; state.submitPending = null;
  state.wizardTaskId = null; state.wizardStep = null; state.wizardDerivedStep = null;
  state.courseOptionsEditing = false;
  state.courseDetailsOpen = false;
  state.runtimeEvents = []; state.runtimeSequence = 0;
  renderTaskList();
  await refreshDetail();
}

async function refreshDetail(force = false) {
  if (state.shuttingDown) return;
  if (!state.activeTaskId) {
    state.detail = null;
    state.detailTaskId = null;
    state.detailRevision = null;
    state.detailEventRevision = null;
    renderEmptyDetail();
    return;
  }
  if (state.detailRefreshing) return;
  state.detailRefreshing = true;
  const taskId = state.activeTaskId;
  const switchedTask = state.detailTaskId !== taskId;
  if (switchedTask) {
    state.detailTaskId = taskId;
    state.detailEventRevision = null;
    state.review = null; state.reviewError = null; state.answerMap = new Map();
    state.submitPending = null;
    state.wizardTaskId = null; state.wizardStep = null; state.wizardDerivedStep = null;
    state.runtimeEvents = []; state.runtimeSequence = 0;
    state.authenticationHint = null;
    state.courseOptionsEditing = false;
    state.courseDetailsOpen = false;
  }
  try {
    const detail = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
    if (state.activeTaskId !== taskId) return;
    const revision = detail.task.task_type === "course"
      ? [detail.task.task_id, detail.task.title, detail.task.site_id, detail.task.status,
        detail.task.current_step, detail.course?.mode, detail.course?.course_id,
        detail.course?.class_id, detail.course?.course_name, detail.course?.phase,
        detail.course?.run_id, detail.task.model_profile_id, JSON.stringify(detail.course?.options || {}),
        detail.cookie_status?.captured, detail.cookie_status?.configured].join("|")
      : [detail.task.task_id, detail.task.title, detail.task.site_id, detail.task.status, detail.task.current_step,
        detail.task.last_operation, detail.task.run_id, detail.task.model_profile_id, detail.task.queue_state,
        detail.task.queue_position, detail.task.interaction_retry_authorized, detail.task.pending_operation].join("|");
    const eventRevision = JSON.stringify((detail.events || []).map((event) => [
      event.sequence, event.occurred_at, event.event_type, event.from_status, event.to_status, event.safe_error_category
    ]));
    const changed = force || revision !== state.detailRevision;
    const eventsChanged = eventRevision !== state.detailEventRevision;
    const previousRunId = state.detail?.task?.task_id === taskId ? state.detail.course?.run_id : null;
    state.detail = detail;
    if (detail.task.task_type === "course" && previousRunId !== detail.course?.run_id) {
      state.courseOutlines.delete(taskId);
      state.courseOutlineErrors.delete(taskId);
    }
    state.cookieStatus = detail.cookie_status || { captured: false, configured: false };
    state.authenticationHint = detail.task.authentication_hint || null;
    let runtime = { events: [], latest_sequence: state.runtimeSequence };
    try {
      runtime = await api(`/api/tasks/${encodeURIComponent(taskId)}/runtime-events?after=${switchedTask ? 0 : state.runtimeSequence}`);
    } catch (_) {
      // Runtime events are optional UI data. A transient event-store failure must
      // not turn a valid task detail into the unavailable screen.
    }
    if (state.activeTaskId !== taskId) return;
    for (const event of runtime.events || []) {
      if (event.task_id === taskId) state.runtimeEvents.push(event);
    }
    state.runtimeEvents = state.runtimeEvents.slice(-300);
    state.runtimeSequence = runtime.latest_sequence ?? state.runtimeSequence;
    if (detail.task.task_type === "course") {
      if (detail.task.status !== "ready") state.courseOptionsEditing = false;
      state.review = null; state.reviewError = null; state.answerMap = new Map();
      state.confirmationId = null; state.modelAuthorizationId = null; state.submitPending = null;
    } else {
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
        try {
          const pending = await api(`/api/tasks/${encodeURIComponent(taskId)}/model-authorization`);
          if (pending.pending) showModelAuthorization(pending.pending);
        } catch (_) {
          // Keep the task detail visible while the authorization summary retries.
        }
      }
      if (detail.task.status === "awaiting_submit_confirmation") {
        try {
          const pending = await api(`/api/tasks/${encodeURIComponent(taskId)}/submit-confirmation`);
          state.submitPending = pending.pending || null;
          if (pending.pending) showSubmitConfirmation(pending.pending);
        } catch (_) {
          // The confirmation endpoint is auxiliary; preserve the loaded task view.
        }
      } else {
        state.submitPending = null;
      }
    }
    if (changed) state.detailRevision = revision;
    if (changed) {
      renderDetail();
    } else {
      if (eventsChanged) updateEventCard();
      const updated = el("detailPane").querySelector("[data-detail-updated-at]");
      const updatedText = `更新 ${formatTime(detail.task.updated_at)}`;
      if (updated && updated.textContent !== updatedText) updated.textContent = updatedText;
      if (detail.task.task_type === "course") updateCourseProgress(detail.task);
      else updateSolveProgress();
    }
    if (detail.task.task_type === "course") showCaptchaRequiredDialog(detail.task);
    state.detailEventRevision = eventRevision;
    if (detail.task.task_type === "course" && detail.course?.mode && detail.course?.course_id) {
      void loadCourseOutline(taskId);
    }
  } catch (error) {
    if (state.activeTaskId === taskId) {
      const hasCurrentDetail = state.detail?.task?.task_id === taskId;
      if (!hasCurrentDetail) renderEmptyDetail(error.message);
    }
  } finally {
    state.detailRefreshing = false;
  }
}
function renderEmptyDetail(errorMessage) {
  const pane = el("detailPane");
  const key = `${state.activeTaskId || ""}\u0000${errorMessage ? "unavailable" : "empty"}`;
  state.detailRevision = null;
  state.detailEventRevision = null;
  if (pane.dataset.emptyStateKey === key && pane.childElementCount) {
    const message = pane.querySelector("[data-empty-error]");
    if (message && message.textContent !== errorMessage) message.textContent = errorMessage;
    return;
  }
  pane.dataset.emptyStateKey = key;
  clear(pane);
  const empty = node("div", "empty-workspace");
  const logo = node("img", "empty-logo");
  logo.src = "/static/logo.svg?v=2";
  logo.alt = "";
  empty.appendChild(logo);
  empty.appendChild(node("h2", "", errorMessage ? "任务暂不可用" : "选择一个任务"));
  if (errorMessage) {
    const message = node("p", "", errorMessage);
    message.dataset.emptyError = "true";
    empty.appendChild(message);
  }
  else empty.appendChild(actionButton("新建任务", openNewTask));
  pane.appendChild(empty);
  playPaneEntrance(pane);
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
function card(title) {
  const section = node("section", "content-card");
  if (title) section.appendChild(node("h3", "", title));
  return section;
}
function facts(items) {
  const list = node("dl", "facts");
  for (const [label, value] of items) {
    const item = node("div", "fact");
    item.appendChild(node("dt", "", label));
    item.appendChild(node("dd", "", value));
    list.appendChild(item);
  }
  return list;
}
function statusLine(tone, text) {
  const line = node("div", `status-line ${tone}`);
  line.append(node("span", "dot"), node("span", "", text));
  return line;
}
function actionRow() { return node("div", "action-bar"); }
function actionButton(label, handler, variant = "primary") {
  const button = node("button", `button button-${variant}`, label);
  button.type = "button";
  button.addEventListener("click", handler);
  return button;
}
function addPageAction(row, label, handler, variant = "primary", disabled = false) {
  const button = actionButton(label, handler, variant);
  button.disabled = disabled;
  row.appendChild(button);
  return button;
}
function renderDetail() {
  if (!state.detail) return;
  const task = state.detail.task;
  if (task.task_type === "course") {
    renderCourseDetail(task);
    return;
  }
  const pane = el("detailPane");
  delete pane.dataset.emptyStateKey;
  clear(pane);
  const header = node("header", "detail-header");
  const heading = node("div", "detail-title-wrap");
  heading.appendChild(node("h2", "detail-title", task.title));
  const meta = node("div", "detail-meta");
  meta.appendChild(statusBadge(task));
  meta.appendChild(node("span", "", siteLabel(task.site_id)));
  const updated = node("span", "", `更新 ${formatTime(task.updated_at)}`);
  updated.dataset.detailUpdatedAt = "true";
  meta.appendChild(updated);
  heading.appendChild(meta);
  header.appendChild(heading);
  const refresh = node("button", "icon-button", "↻");
  refresh.type = "button";
  refresh.title = "刷新";
  refresh.setAttribute("aria-label", "刷新任务详情");
  refresh.addEventListener("click", () => refreshDetail(true));
  header.appendChild(refresh);
  pane.appendChild(header);
  syncWizardStep(task);
  pane.appendChild(renderStepper(task));
  const alert = renderDetailAlert(task);
  if (alert) pane.appendChild(alert);
  const page = renderWizardPage(task);
  if (page) pane.appendChild(page);
  pane.appendChild(renderWizardNav(task));
  const queue = renderQueueLine();
  if (queue) pane.appendChild(queue);
  pane.appendChild(renderEventCard());
  playPaneEntrance(pane);
}
function renderCourseStepper(task) {
  const course = state.detail.course || {};
  const options = course.options || {};
  const steps = COURSE_STEPS.filter(([step]) => step !== "exam" || (course.mode === "ai" && options.process_exam));
  const currentStep = course.phase || task.current_step;
  const currentIndex = steps.findIndex(([step]) => step === currentStep);
  const stepper = node("div", "stepper course-stepper");
  steps.forEach(([step, label], index) => {
    const item = node("div", "step", label);
    if (currentIndex >= 0 && index < currentIndex) item.classList.add("done");
    if (index === currentIndex) {
      item.classList.add("current");
      item.setAttribute("aria-current", "step");
    }
    if (currentIndex >= 0 && index === currentIndex + 1) item.classList.add("reached");
    stepper.appendChild(item);
  });
  return stepper;
}
function renderCourseDetail(task) {
  const detail = state.detail;
  const course = detail.course || {};
  const options = course.options || {};
  const hasCourse = Boolean(course.mode && course.course_id);
  const pane = el("detailPane");
  delete pane.dataset.emptyStateKey;
  clear(pane);
  const header = node("header", "detail-header");
  const heading = node("div", "detail-title-wrap");
  heading.appendChild(node("h2", "detail-title", task.title));
  const meta = node("div", "detail-meta");
  meta.append(statusBadge(task), node("span", "", siteLabel(task.site_id)));
  const updated = node("span", "", `更新 ${formatTime(task.updated_at)}`);
  updated.dataset.courseUpdatedAt = "true";
  meta.appendChild(updated);
  heading.appendChild(meta);
  header.appendChild(heading);
  const refresh = node("button", "icon-button", "↻");
  refresh.type = "button";
  refresh.title = "刷新";
  refresh.setAttribute("aria-label", "刷新课程详情");
  refresh.addEventListener("click", () => refreshDetail(true));
  header.appendChild(refresh);
  pane.append(header, renderCourseStepper(task));
  if (state.courseQr && state.detailTaskId === task.task_id) {
    const qrCard = card("智慧树二维码登录");
    const image = node("img", "course-login-qr");
    image.alt = "智慧树登录二维码";
    image.src = `data:image/png;base64,${state.courseQr}`;
    qrCard.append(image, node("p", "form-hint", "使用智慧树 App 扫码。登录完成后，这里会出现你的课程列表。"));
    pane.appendChild(qrCard);
  }

  const modeLabels = { shared: "共享课", hike: "爬课", ai: "AI 课程" };
  const actions = actionRow();
  if (!hasCourse) {
    const setup = card("连接你的智慧树账号");
    if (task.status === "draft") {
      setup.appendChild(statusLine("info", "先扫码登录，系统会读取账号可用的课程目录。"));
      addPageAction(actions, "二维码登录", () => startAuthentication(task, false));
    } else if (task.status === "authenticating") {
      setup.appendChild(statusLine("info", "等待扫码登录。请使用智慧树 App 扫描上方二维码。"));
    } else if (task.status === "ready") {
      setup.appendChild(state.cookieStatus.configured
        ? statusLine("success", "账号已连接，可以读取课程列表。")
        : statusLine("warning", "登录状态不可用，请重新扫码登录。"));
      if (state.cookieStatus.configured) addPageAction(actions, "读取课程列表", () => fetchCourseCatalog(task));
      addPageAction(actions, "重新登录", () => startAuthentication(task, false), "secondary");
    } else if (task.status === "failed") {
      setup.appendChild(statusLine("danger", "上次登录没有完成，请重新登录后继续。"));
      addPageAction(actions, "重新登录", () => startAuthentication(task, true));
    } else {
      setup.appendChild(statusLine("info", "登录后即可选择要学习的课程。"));
    }
    if (actions.childElementCount) setup.appendChild(actions);
    pane.appendChild(setup);
    const catalog = state.courseCatalogs.get(task.task_id);
    if (task.status === "ready" && Array.isArray(catalog)) pane.appendChild(renderCourseCatalogPicker(task, catalog));
    pane.appendChild(renderEventCard());
    playPaneEntrance(pane);
    return;
  }

  const summary = node("section", "content-card course-summary");
  const summaryTop = node("div", "course-summary-top");
  const courseTitle = node("div", "course-summary-title");
  courseTitle.append(node("span", "course-mode-pill", modeLabels[course.mode] || "网课"), node("h3", "", course.course_name || task.title || "已选课程"));
  summaryTop.append(courseTitle);
  if (task.status === "ready" && !state.courseOptionsEditing) {
    summaryTop.appendChild(actionButton("编辑运行参数", () => {
      state.courseOptionsEditing = true;
      state.courseDetailsOpen = true;
      renderCourseDetail(task);
    }, "secondary"));
  }
  summary.append(summaryTop);
  const courseInfo = node("details", "course-details-disclosure");
  courseInfo.open = state.courseOptionsEditing || state.courseDetailsOpen;
  courseInfo.addEventListener("toggle", () => { state.courseDetailsOpen = courseInfo.open; });
  const courseInfoSummary = node("summary", "", "查看课程与运行参数");
  courseInfo.append(courseInfoSummary, facts([
    ["课程 ID", course.course_id || "—"], ["班级 ID", course.class_id || "—"],
    ["播放速度", options.speed ?? "—"], ["完成阈值", options.threshold ?? "—"],
    ["运行时限", options.time_limit_minutes == null ? "—" : `${options.time_limit_minutes} 分钟`]
  ]));
  if (state.courseOptionsEditing) courseInfo.appendChild(renderCourseOptionsEditor(task, options));
  summary.appendChild(courseInfo);
  pane.appendChild(summary);
  const catalog = state.courseCatalogs.get(task.task_id);
  const changingCourse = task.status === "ready" && Array.isArray(catalog);
  if (changingCourse) pane.appendChild(renderCourseCatalogPicker(task, catalog));
  if (course.mode === "ai" && !task.model_profile_id) pane.appendChild(statusLine("warning", "开始前请在运行参数中选择模型档案。"));

  if (task.status === "ready" && !state.cookieStatus.configured) {
    pane.appendChild(statusLine("warning", "智慧树登录状态不可用，请重新扫码登录后再开始。"));
  }
  const captchaRequired = task.status === "failed" && task.last_safe_error_category === "captcha_required";
  if (captchaRequired) {
    pane.appendChild(statusLine("warning", "请在浏览器中自行打开智慧树网页并完成验证码；完成后返回此页重试。验证后不会自动续跑。"));
  } else if (["partial", "failed"].includes(task.status)) {
    pane.appendChild(statusLine("warning", "本次运行未全部完成；可以查看目录进度后重跑。"));
  }

  if (task.status === "ready") {
    if (changingCourse) {
      actions.appendChild(node("span", "form-hint", "确认新课程，或取消更换以保留当前课程。"));
    } else {
      addPageAction(actions, "开始学习", () => runAction(() => api(`/api/tasks/${encodeURIComponent(task.task_id)}/course/start`, { method: "POST", body: {} }), "课程已开始。"), "primary", !state.cookieStatus.configured || (course.mode === "ai" && !task.model_profile_id));
      addPageAction(actions, "更换课程", () => fetchCourseCatalog(task), "secondary", !state.cookieStatus.configured);
      addPageAction(actions, "重新登录", () => startAuthentication(task, false), "quiet");
    }
  } else if (task.status === "running") {
    addPageAction(actions, "停止学习", () => stopTask(task), "danger");
  } else if (task.status === "stopping") {
    actions.appendChild(statusLine("warning", "正在安全停止课程…"));
  } else if (["completed", "partial", "failed", "cancelled"].includes(task.status)) {
    addPageAction(actions, captchaRequired ? "验证码完成后重试" : "重跑课程", () => runAction(() => api(`/api/tasks/${encodeURIComponent(task.task_id)}/course/rerun`, { method: "POST", body: {} }), "课程已重新运行。"));
    addPageAction(actions, "归档", () => archiveTask(task), "secondary");
  } else if (task.status === "archived") {
    addPageAction(actions, "删除运行记录", () => openDeleteTaskDialog(task), "danger");
  }
  if (actions.childElementCount) {
    const actionCard = node("section", "content-card course-primary-actions");
    actionCard.appendChild(actions);
    pane.appendChild(actionCard);
  }

  if (!changingCourse) {
    const progress = node("section", "content-card course-progress-card");
    const progressHeader = node("div", "course-progress-header");
    const current = node("div", "course-progress-current", course.current_title ? `正在学习：${course.current_title}` : (task.status === "running" ? "正在准备学习内容…" : "尚未开始"));
    current.dataset.courseCurrent = "true";
    const percent = node("strong", "course-progress-percent", Number.isFinite(course.progress_percent) ? `${course.progress_percent}%` : "—");
    percent.dataset.coursePercent = "true";
    progressHeader.append(current, percent);
    const track = node("div", "progress-track course-progress-track");
    const fill = node("div", "progress-fill");
    if (Number.isFinite(course.progress_percent)) fill.style.width = `${Math.max(0, Math.min(100, course.progress_percent))}%`;
    else if (task.status === "running") fill.classList.add("indeterminate");
    track.appendChild(fill);
    const counts = node("div", "course-progress-meta");
    const completed = node("span", "", `${course.completed_count ?? 0} / ${course.total_count ?? "—"} 项完成`);
    completed.dataset.courseCompleted = "true";
    const failed = node("span", "", `${course.failed_count ?? 0} 项失败`);
    failed.dataset.courseFailed = "true";
    const updatedAt = node("span", "course-progress-updated", `更新 ${formatTime(task.updated_at)}`);
    updatedAt.dataset.courseUpdatedAt = "true";
    counts.append(completed, failed, updatedAt);
    progress.append(progressHeader, track, counts);
    pane.appendChild(progress);
    pane.appendChild(renderCourseOutlineCard(task));
    updateCourseOutlineProgress(task);
    updateCourseOutlineBody(task.task_id);
  }
  pane.appendChild(renderEventCard());
  playPaneEntrance(pane);
}
function updateCourseProgress(task) {
  const course = state.detail?.course || {};
  const pane = el("detailPane");
  const current = pane.querySelector("[data-course-current]");
  const completed = pane.querySelector("[data-course-completed]");
  const percent = pane.querySelector("[data-course-percent]");
  const failed = pane.querySelector("[data-course-failed]");
  const updated = pane.querySelector("[data-course-updated-at]");
  const fill = pane.querySelector(".course-progress-track .progress-fill");
  if (!current || !completed || !percent || !failed || !updated || !fill) return;
  const currentText = course.current_title ? `正在学习：${course.current_title}` : (task.status === "running" ? "正在准备学习内容…" : "尚未开始");
  if (current.textContent !== currentText) current.textContent = currentText;
  const completedText = `${course.completed_count ?? 0} / ${course.total_count ?? "—"} 项完成`;
  const percentText = Number.isFinite(course.progress_percent) ? `${course.progress_percent}%` : "—";
  const failedText = `${course.failed_count ?? 0} 项失败`;
  const updatedText = `更新 ${formatTime(task.updated_at)}`;
  if (completed.textContent !== completedText) completed.textContent = completedText;
  if (percent.textContent !== percentText) percent.textContent = percentText;
  if (failed.textContent !== failedText) failed.textContent = failedText;
  if (updated.textContent !== updatedText) updated.textContent = updatedText;
  if (Number.isFinite(course.progress_percent)) {
    if (fill.classList.contains("indeterminate")) fill.classList.remove("indeterminate");
    const width = `${Math.max(0, Math.min(100, course.progress_percent))}%`;
    if (fill.style.width !== width) fill.style.width = width;
  } else {
    if (!fill.classList.contains("indeterminate")) fill.classList.add("indeterminate");
    if (fill.style.width) fill.style.removeProperty("width");
  }
  updateCourseOutlineProgress(task);
}
function renderCourseOutlineCard(task) {
  const section = card("课程目录");
  section.classList.add("course-outline-card");
  const heading = node("div", "course-outline-progress-heading");
  heading.appendChild(node("strong", "", "学习项目"));
  const counts = node("span", "");
  counts.dataset.outlineCounts = "true";
  heading.appendChild(counts);
  const toolbar = node("div", "course-outline-toolbar");
  const search = node("input", "course-outline-search");
  search.type = "search";
  search.placeholder = "搜索章节或学习项目";
  search.setAttribute("aria-label", "搜索课程目录");
  const view = courseOutlineView(task.task_id);
  search.value = view.query;
  search.addEventListener("input", () => {
    view.query = search.value.trim().toLocaleLowerCase();
    applyCourseOutlineFilter(task.task_id);
  });
  const filter = node("div", "course-outline-filters");
  filter.setAttribute("role", "group");
  filter.setAttribute("aria-label", "筛选学习项目");
  [["all", "全部"], ["pending", "待学习"], ["completed", "已完成"]].forEach(([value, label]) => {
    const button = node("button", `course-outline-filter${view.filter === value ? " selected" : ""}`, label);
    button.type = "button";
    button.setAttribute("aria-pressed", String(view.filter === value));
    button.addEventListener("click", () => {
      view.filter = value;
      filter.querySelectorAll("button").forEach((item) => {
        const selected = item === button;
        item.classList.toggle("selected", selected);
        item.setAttribute("aria-pressed", String(selected));
      });
      applyCourseOutlineFilter(task.task_id);
    });
    filter.appendChild(button);
  });
  const treeActions = node("div", "course-outline-actions");
  const follow = node("button", "button button-quiet button-small course-outline-follow", "跟随当前项目");
  follow.type = "button";
  follow.setAttribute("aria-pressed", String(view.followCurrent));
  follow.classList.toggle("active", view.followCurrent);
  follow.addEventListener("click", () => {
    view.followCurrent = !view.followCurrent;
    follow.setAttribute("aria-pressed", String(view.followCurrent));
    follow.classList.toggle("active", view.followCurrent);
    if (view.followCurrent) {
      updateCourseOutlineProgress(task);
      locateCurrentCourseItem(task.task_id);
    }
  });
  const locate = node("button", "button button-secondary button-small", "定位当前");
  locate.type = "button";
  locate.addEventListener("click", () => locateCurrentCourseItem(task.task_id));
  const expand = node("button", "button button-quiet button-small", "展开全部");
  expand.type = "button";
  expand.addEventListener("click", () => setCourseOutlineExpanded(task.task_id, true));
  const collapse = node("button", "button button-quiet button-small", "收起全部");
  collapse.type = "button";
  collapse.addEventListener("click", () => setCourseOutlineExpanded(task.task_id, false));
  const reload = node("button", "button button-quiet button-small", "刷新目录");
  reload.type = "button";
  reload.addEventListener("click", () => loadCourseOutline(task.task_id, true));
  treeActions.append(follow, locate, expand, collapse, reload);
  toolbar.append(search, filter, treeActions);
  const body = node("div", "course-outline-body");
  body.dataset.courseOutlineBody = task.task_id;
  section.append(heading, toolbar, body);
  return section;
}
async function loadCourseOutline(taskId, force = false) {
  if (force) {
    state.courseOutlines.delete(taskId);
    state.courseOutlineErrors.delete(taskId);
  }
  if (state.courseOutlines.has(taskId) || state.courseOutlineLoading.has(taskId)) return;
  if (!force && state.courseOutlineErrors.has(taskId)) return;
  state.courseOutlineLoading.add(taskId);
  updateCourseOutlineBody(taskId);
  try {
    const result = await api(`/api/tasks/${encodeURIComponent(taskId)}/course/outline`);
    state.courseOutlines.set(taskId, result);
    state.courseOutlineErrors.delete(taskId);
  } catch (error) {
    state.courseOutlineErrors.set(taskId, error.message || "课程目录读取失败。");
  } finally {
    state.courseOutlineLoading.delete(taskId);
    if (state.detail?.task?.task_id === taskId) updateCourseOutlineBody(taskId);
  }
}
function updateCourseOutlineBody(taskId) {
  const body = el("detailPane").querySelector(`[data-course-outline-body="${CSS.escape(taskId)}"]`);
  if (!body) return;
  clear(body);
  const outline = state.courseOutlines.get(taskId);
  if (outline && Array.isArray(outline.items)) {
    const count = body.closest(".course-outline-card")?.querySelector("[data-outline-counts]");
    if (count) count.textContent = `${countOutlineLeaves(outline.items)} 个学习项目`;
    if (!outline.items.length) {
      body.appendChild(statusLine("info", "当前课程没有可显示的学习项目。"));
      return;
    }
    const tree = node("div", "course-outline-tree");
    tree.dataset.currentTitle = state.detail?.course?.current_title || "";
    const view = courseOutlineView(taskId);
    const currentPath = view.followCurrent ? findCourseOutlinePath(outline.items, tree.dataset.currentTitle) : "";
    const matchMap = courseOutlineMatchMap(outline.items, view);
    tree.dataset.currentPath = currentPath;
    appendCourseOutlineNodes(tree, outline.items, "", currentPath, view, matchMap);
    body.appendChild(tree);
    if (outline.truncated) body.appendChild(node("p", "form-hint course-outline-note", "目录过长，当前仅显示前 2500 项。"));
    updateCourseOutlineProgress(state.detail.task);
    applyCourseOutlineFilter(taskId);
    return;
  }
  const error = state.courseOutlineErrors.get(taskId);
  const count = body.closest(".course-outline-card")?.querySelector("[data-outline-counts]");
  if (error) {
    if (count) count.textContent = "读取失败";
    body.appendChild(statusLine("warning", error));
    body.appendChild(actionButton("重试读取目录", () => loadCourseOutline(taskId, true), "secondary"));
  } else {
    if (count) count.textContent = "正在读取目录";
    body.appendChild(statusLine("info", state.courseOutlineLoading.has(taskId) ? "正在读取课程目录…" : "等待读取课程目录…"));
  }
}
function courseOutlineView(taskId) {
  if (!state.courseOutlineViews.has(taskId)) {
    state.courseOutlineViews.set(taskId, { query: "", filter: "all", expanded: new Set(), followCurrent: true });
  }
  return state.courseOutlineViews.get(taskId);
}
function findCourseOutlinePath(items, title, parentPath = "") {
  if (!title) return "";
  for (const [index, item] of items.entries()) {
    const path = parentPath ? `${parentPath}.${index}` : String(index);
    if (item.title === title && !(item.children || []).length) return path;
    const childPath = findCourseOutlinePath(item.children || [], title, path);
    if (childPath) return childPath;
  }
  return "";
}
function courseOutlineMatchMap(items, view) {
  const matches = new Map();
  const visit = (children, parentPath = "") => {
    let anyMatch = false;
    children.forEach((item, index) => {
      const path = parentPath ? `${parentPath}.${index}` : String(index);
      const ownMatch = (!view.query || item.title.toLocaleLowerCase().includes(view.query))
        && (view.filter === "all" || (view.filter === "completed" ? item.completed === true : item.completed === false));
      const childMatch = visit(item.children || [], path);
      const match = ownMatch || childMatch;
      matches.set(path, match);
      anyMatch = anyMatch || match;
    });
    return anyMatch;
  };
  visit(items);
  return matches;
}
function outlineStatusCounts(item) {
  const children = item.children || [];
  if (!children.length) return { completed: item.completed === true ? 1 : 0, known: typeof item.completed === "boolean" ? 1 : 0, items: 1 };
  return children.reduce((sum, child) => {
    const nested = outlineStatusCounts(child);
    sum.completed += nested.completed;
    sum.known += nested.known;
    sum.items += nested.items;
    return sum;
  }, { completed: 0, known: 0, items: 0 });
}
function outlineStatusLabel(item) {
  const children = item.children || [];
  if (!children.length) return item.completed === true ? "已完成" : (item.completed === false ? "待学习" : "状态未知");
  const totals = outlineStatusCounts(item);
  return totals.known ? `${totals.completed}/${totals.known} 已完成` : `${totals.items} 个项目`;
}
function appendCourseOutlineNodes(container, items, parentPath, currentPath, view, matchMap) {
  items.forEach((item, index) => {
    const children = Array.isArray(item.children) ? item.children : [];
    const path = parentPath ? `${parentPath}.${index}` : String(index);
    const containsCurrent = Boolean(currentPath && (path === currentPath || currentPath.startsWith(`${path}.`)));
    const row = node(children.length ? "summary" : "div", "course-outline-row");
    row.dataset.outlineTitle = item.title;
    row.dataset.outlinePath = path;
    row.dataset.completed = item.completed === true ? "true" : "false";
    row.appendChild(node("span", "course-outline-title", item.title));
    const active = path === currentPath && Boolean(currentPath);
    if (active) row.classList.add("is-current");
    if (item.completed === true) row.classList.add("is-completed");
    const baseStateLabel = outlineStatusLabel(item);
    row.dataset.outlineStatusLabel = baseStateLabel;
    const stateBadge = node("span", "course-outline-state", active ? "正在学习" : baseStateLabel);
    stateBadge.dataset.outlineState = "true";
    row.appendChild(stateBadge);
    if (!children.length) {
      container.appendChild(row);
      return;
    }
    const group = node("details", "course-outline-group");
    group.dataset.outlinePath = path;
    group.open = view.expanded.has(path) || (view.followCurrent && containsCurrent) || Boolean((view.query || view.filter !== "all") && matchMap.get(path));
    row.setAttribute("aria-expanded", String(group.open));
    group.addEventListener("toggle", () => {
      if (group.open) view.expanded.add(path);
      else view.expanded.delete(path);
      const summary = group.querySelector(":scope > summary");
      summary?.setAttribute("aria-expanded", String(group.open));
    });
    group.appendChild(row);
    const nested = node("div", "course-outline-children");
    appendCourseOutlineNodes(nested, children, path, currentPath, view, matchMap);
    group.appendChild(nested);
    container.appendChild(group);
  });
}
function applyCourseOutlineFilter(taskId) {
  const body = el("detailPane").querySelector(`[data-course-outline-body="${CSS.escape(taskId)}"]`);
  const tree = body?.querySelector(".course-outline-tree");
  if (!tree) return;
  const view = courseOutlineView(taskId);
  const matches = courseOutlineMatchMap(state.courseOutlines.get(taskId)?.items || [], view);
  for (const row of tree.querySelectorAll("[data-outline-path]")) {
    const isMatch = Boolean(matches.get(row.dataset.outlinePath));
    if (row.hidden !== !isMatch) row.hidden = !isMatch;
    const group = row.closest("details");
    if (group) {
      const groupMatches = Boolean(matches.get(group.dataset.outlinePath));
      if (group.hidden !== !groupMatches) group.hidden = !groupMatches;
      if ((view.query || view.filter !== "all") && groupMatches && !group.open) group.open = true;
      const summary = group.querySelector(":scope > summary");
      if (summary?.getAttribute("aria-expanded") !== String(group.open)) summary?.setAttribute("aria-expanded", String(group.open));
    }
  }
  const visible = tree.querySelectorAll("[data-outline-path]:not([hidden])").length;
  let empty = body.querySelector(".course-outline-empty");
  if (!visible && !empty) {
    empty = node("p", "form-hint course-outline-empty", view.query ? "没有匹配的学习项目。" : "此筛选下没有学习项目。");
    body.appendChild(empty);
  } else if (visible && empty) empty.remove();
  else if (empty) {
    const text = view.query ? "没有匹配的学习项目。" : "此筛选下没有学习项目。";
    if (empty.textContent !== text) empty.textContent = text;
  }
}
function setCourseOutlineExpanded(taskId, expanded) {
  const view = courseOutlineView(taskId);
  const outline = state.courseOutlines.get(taskId);
  if (!outline) return;
  view.expanded.clear();
  if (expanded) {
    const visit = (items, parent = "") => items.forEach((item, index) => {
      const path = parent ? `${parent}.${index}` : String(index);
      if ((item.children || []).length) view.expanded.add(path);
      visit(item.children || [], path);
    });
    visit(outline.items);
  } else {
    view.followCurrent = false;
  }
  const follow = el("detailPane").querySelector(".course-outline-follow");
  if (follow) {
    follow.classList.toggle("active", view.followCurrent);
    follow.setAttribute("aria-pressed", String(view.followCurrent));
  }
  updateCourseOutlineBody(taskId);
}
function locateCurrentCourseItem(taskId) {
  const title = state.detail?.course?.current_title || "";
  const path = findCourseOutlinePath(state.courseOutlines.get(taskId)?.items || [], title);
  if (!path) {
    toast(title ? "目录中暂时没有匹配的当前项目。" : "当前还没有正在学习的项目。", "info");
    return;
  }
  const tree = el("detailPane").querySelector(`[data-course-outline-body="${CSS.escape(taskId)}"] .course-outline-tree`);
  const row = tree?.querySelector(`[data-outline-path="${CSS.escape(path)}"]`);
  if (row) {
    const view = courseOutlineView(taskId);
    view.followCurrent = true;
    if (row.hidden) {
      view.query = "";
      view.filter = "all";
      const search = el("detailPane").querySelector(".course-outline-search");
      if (search) search.value = "";
      el("detailPane").querySelectorAll(".course-outline-filter").forEach((button) => {
        const selected = button.textContent === "全部";
        button.classList.toggle("selected", selected);
        button.setAttribute("aria-pressed", String(selected));
      });
    }
    const follow = el("detailPane").querySelector(".course-outline-follow");
    if (follow) {
      follow.classList.add("active");
      follow.setAttribute("aria-pressed", "true");
    }
    updateCourseOutlineProgress(state.detail.task);
    let group = row.closest("details");
    while (group) {
      group.open = true;
      group = group.parentElement?.closest("details") || null;
    }
    row.scrollIntoView({ behavior: motionScrollBehavior(), block: "center" });
    row.classList.add("course-outline-locate");
    window.setTimeout(() => row.classList.remove("course-outline-locate"), 1200);
  }
}
function updateCourseOutlineProgress(task) {
  const course = state.detail?.course || {};
  const body = el("detailPane").querySelector(`[data-course-outline-body="${CSS.escape(task.task_id)}"]`);
  if (!body) return;
  const card = body.closest(".content-card");
  if (!card) return;
  const counts = card.querySelector("[data-outline-counts]");
  const outline = state.courseOutlines.get(task.task_id);
  const countsText = outline ? `${countOutlineLeaves(outline.items || [])} 个学习项目` : "正在读取目录";
  if (counts && counts.textContent !== countsText) counts.textContent = countsText;
  const tree = body.querySelector(".course-outline-tree");
  if (!tree) return;
  const title = course.current_title || "";
  const titleChanged = tree.dataset.currentTitle !== title;
  if (titleChanged) tree.dataset.currentTitle = title;
  const view = courseOutlineView(task.task_id);
  const path = view.followCurrent ? findCourseOutlinePath(state.courseOutlines.get(task.task_id)?.items || [], title) : "";
  if (tree.dataset.currentPath !== path) tree.dataset.currentPath = path;
  for (const row of tree.querySelectorAll("[data-outline-title]")) {
    const active = Boolean(path) && row.dataset.outlinePath === path;
    row.classList.toggle("is-current", active);
    if (active && row.getAttribute("aria-current") !== "location") row.setAttribute("aria-current", "location");
    else if (!active && row.hasAttribute("aria-current")) row.removeAttribute("aria-current");
    const label = row.querySelector("[data-outline-state]");
    if (label) {
      const labelText = active ? "正在学习" : row.dataset.outlineStatusLabel;
      if (label.textContent !== labelText) label.textContent = labelText;
    }
    if (active) {
      let group = row.parentElement?.closest("details") || null;
      while (group) {
        group.open = true;
        const summary = group.querySelector(":scope > summary");
        if (summary?.getAttribute("aria-expanded") !== "true") summary?.setAttribute("aria-expanded", "true");
        group = group.parentElement?.closest("details") || null;
      }
      if (titleChanged && view.followCurrent) row.scrollIntoView({ behavior: motionScrollBehavior(), block: "nearest" });
    }
  }
  applyCourseOutlineFilter(task.task_id);
}
function countOutlineLeaves(items) {
  return items.reduce((count, item) => {
    const children = item.children || [];
    return count + (children.length ? countOutlineLeaves(children) : 1);
  }, 0);
}
function renderCourseOptionsEditor(task, options) {
  const form = node("form", "course-settings-form");
  const isAi = state.detail.course?.mode === "ai";
  const speed = courseOptionInput("播放速度（倍）", "speed", "number", options.speed, { min: "0.1", max: "500", step: "0.1" },
    "视频进度的推进倍率：1 为原速，1.5 为 1.5 倍。可填 0.1–500；speed: 校内学分课播放速度正常最高为1.25, 共享学分课为1.5, 但似乎更高的服务器也接受, 不过怕暴露就谨慎些吧");
  const threshold = courseOptionInput("视频完成阈值（比例）", "threshold", "number", options.threshold, { min: "0.01", max: "1", step: "0.01" },
    isAi ? "当前 AI 课程按完整视频时长运行，此参数暂不生效，保留原值。"
      : "0.91 表示到视频时长的 91% 后尝试结束，1 表示完整时长；共享课还会覆盖后续视频内题目。它不是课程总进度或考试及格线，完成情况以站点认定为准。");
  threshold.input.disabled = isAi;
  const limit = courseOptionInput("运行时限（分钟）", "time_limit_minutes", "number", options.time_limit_minutes, { min: "0", max: "14400", step: "1" },
    "本次运行的时间预算，0 表示不限时。部分模式在学习项目边界检查，另有约 10 分钟收尾缓冲，因此不是精确倒计时。");
  form.append(speed.label, threshold.label, limit.label);
  if (isAi) {
    const exam = courseOptionInput("处理 AI 课程考试", "process_exam", "checkbox", options.process_exam, {},
      "开启后会调用所选模型作答并提交 AI 课程考试；关闭后只处理学习资源。模型请求可能产生服务费用。");
    form.appendChild(exam.label);
    const profileLabel = node("label", "course-setting-field", "模型档案");
    const profile = node("select");
    profile.name = "model_profile_id";
    profile.id = "course-setting-model-profile";
    profile.setAttribute("aria-describedby", "course-setting-model-profile-help");
    profile.appendChild(new Option("稍后选择", ""));
    for (const item of state.profiles) profile.appendChild(new Option(`${item.name} · v${item.version}`, item.profile_id));
    profile.value = task.model_profile_id || "";
    profileLabel.appendChild(profile);
    const profileHelp = node("span", "form-hint course-setting-help", "指定 AI 课程使用的模型与服务；当前启动流程要求 AI 课程选择可用档案，即使关闭考试也需要配置。");
    profileHelp.id = "course-setting-model-profile-help";
    profileLabel.appendChild(profileHelp);
    form.appendChild(profileLabel);
  }
  form.appendChild(renderCourseParameterTips(form, isAi));
  const row = actionRow();
  const save = actionButton("保存参数", async () => {
    if (!form.reportValidity()) return;
    const values = {
      speed: Number(form.elements.speed.value), threshold: Number(form.elements.threshold.value),
      time_limit_minutes: Number(form.elements.time_limit_minutes.value)
    };
    if (state.detail.course?.mode === "ai") {
      values.process_exam = form.elements.process_exam.checked;
      values.model_profile_id = form.elements.model_profile_id.value || null;
    }
    const saved = await runAction(() => api(`/api/tasks/${encodeURIComponent(task.task_id)}/course-settings`, { method: "PUT", body: { options: values } }), "课程参数已保存。");
    if (!saved) return;
    state.courseOptionsEditing = false;
    if (state.detail) renderCourseDetail(state.detail.task);
  });
  row.appendChild(save);
  row.appendChild(actionButton("取消", () => { state.courseOptionsEditing = false; renderCourseDetail(task); }, "quiet"));
  form.appendChild(row);
  form.addEventListener("submit", (event) => { event.preventDefault(); save.click(); });
  return form;
}
function courseOptionInput(labelText, name, type, value, attributes = {}, description = "") {
  const label = node("label", `course-setting-field${type === "checkbox" ? " course-setting-checkbox" : ""}`);
  const title = node("span", "", labelText);
  const input = node("input");
  input.name = name;
  input.id = `course-setting-${name}`;
  input.type = type;
  if (type === "number") input.required = true;
  if (type === "checkbox") input.checked = Boolean(value);
  else input.value = value ?? "";
  for (const [key, attribute] of Object.entries(attributes)) input.setAttribute(key, attribute);
  if (type === "checkbox") label.append(input, title);
  else label.append(title, input);
  if (description) {
    const help = node("span", "form-hint course-setting-help", description);
    help.id = `${input.id}-help`;
    input.setAttribute("aria-describedby", help.id);
    label.appendChild(help);
  }
  return { label, input };
}
function renderCourseParameterTips(form, isAi) {
  const tips = node("aside", "course-parameter-tips");
  const heading = node("h4", "", "Tips · 按场景选一组参数");
  heading.id = "course-parameter-tips-heading";
  tips.setAttribute("aria-labelledby", heading.id);
  tips.append(heading, node("p", "form-hint", "以下是使用场景参考，不能保证站点完成认定或避免验证码。填入后仍需点击“保存参数”。"));
  const presets = [
    { title: "首次试运行", description: "先观察课程内容和进度反馈。", speed: 1, threshold: 1, minutes: 15, exam: false },
    { title: "日常分段学习", description: "沿用项目默认速度，给本次学习留一小时预算。", speed: 1.5, threshold: 0.91, minutes: 60, exam: false },
    { title: "按完整时长学习", description: "原速运行，不设置本次时间上限。", speed: 1, threshold: 1, minutes: 0, exam: false },
    { title: "快速刷网课", description: "高倍率推进；建议先短时试跑并确认站点进度正常。", speed: 100, threshold: 0.91, minutes: 60, exam: false }
  ];
  if (isAi) presets.push({ title: "AI 课程含考试", description: "学习资源并处理考试；需配置可用模型。", speed: 1, threshold: 1, minutes: 60, exam: true });
  const list = node("div", "course-preset-list");
  const status = node("p", "form-hint course-preset-status", "推荐值只填写当前表单，不会直接保存或启动任务；模型档案由你选择。");
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  for (const preset of presets) {
    const item = node("div", "course-preset");
    item.append(node("strong", "", preset.title), node("p", "form-hint", preset.description));
    const thresholdText = isAi ? "阈值不适用" : `阈值 ${Math.round(preset.threshold * 100)}%`;
    const timeText = preset.minutes ? `${preset.minutes} 分钟` : "不限时";
    const examText = isAi ? ` · 考试${preset.exam ? "开启" : "关闭"}` : "";
    item.appendChild(node("p", "course-preset-values", `${preset.speed} 倍 · ${thresholdText} · ${timeText}${examText}`));
    const apply = node("button", "button button-secondary button-small", "填入参数");
    apply.type = "button";
    apply.setAttribute("aria-label", `填入${preset.title}推荐参数`);
    apply.addEventListener("click", () => {
      form.elements.speed.value = String(preset.speed);
      if (!isAi) form.elements.threshold.value = String(preset.threshold);
      form.elements.time_limit_minutes.value = String(preset.minutes);
      if (isAi) form.elements.process_exam.checked = preset.exam;
      const needsProfile = isAi && !form.elements.model_profile_id.value;
      status.textContent = `已填入“${preset.title}”，尚未保存。${needsProfile ? "请选择模型档案后保存。" : "确认后点击“保存参数”。"}`;
    });
    item.appendChild(apply);
    list.appendChild(item);
  }
  tips.append(list, status);
  return tips;
}
function renderDetailAlert(task) {
  if (task.queue_state === "queued" && task.queue_position) return statusLine("warning", `队列第 ${task.queue_position} 位`);
  return null;
}
function renderQueueLine() {
  const queue = state.detail.queue;
  if (!queue) return null;
  const line = node("div", "queue-line");
  line.append(node("span", "dot"), node("span", "", [queue.operation, queue.state, queue.queue_position ? `第 ${queue.queue_position} 位` : null, queue.overwrite_result ? "允许覆盖" : null].filter(Boolean).join(" · ")));
  return line;
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
  return state.busy || Boolean(task.queue_state) || ["authenticating", "crawling", "solving", "filling", "submitting", "cancelling", "running", "stopping"].includes(task.status);
}
function cookieReady() { return Boolean(state.cookieStatus.captured && state.cookieStatus.configured); }
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
  const section = card();
  const busy = isTaskBusy(task) && task.last_operation === "authenticate";
  if (busy) {
    section.appendChild(statusLine("info", "正在浏览器中等待登录"));
  } else if (task.status === "failed" && task.last_operation === "authenticate") {
    section.appendChild(statusLine("danger", "上次登录验证失败"));
  } else if (task.status === "failed" && task.last_operation === "crawl" && !task.run_id) {
    section.appendChild(statusLine("warning", "上次抓取失败 · 需重新抓取 Cookie"));
  } else if (state.cookieStatus.captured && state.cookieStatus.configured) {
    section.appendChild(statusLine("success", "Cookie 已就绪"));
  } else if (state.cookieStatus.captured) {
    section.appendChild(statusLine("warning", "Cookie 记录存在，当前配置不可用"));
  } else {
    section.appendChild(statusLine("info", "尚未抓取 Cookie"));
  }
  const row = actionRow();
  if (busy) {
    addPageAction(row, "停止登录验证", () => stopTask(task), "quiet");
  } else if (task.status === "failed") {
    addPageAction(row, state.cookieStatus.captured ? "重新抓取 Cookie" : "抓取 Cookie", () => startAuthentication(task, true));
  } else {
    addPageAction(row, state.cookieStatus.captured ? "更新登录验证" : "抓取 Cookie", () => startAuthentication(task, false));
    if (task.status === "draft") addPageAction(row, "任务设置", openEditTask, "secondary");
  }
  section.appendChild(row);
  return section;
}
function renderCrawlPage(task) {
  const section = card();
  const ready = cookieReady();
  section.appendChild(facts([["Cookie", ready ? "可用" : "未就绪"], ["题包", task.run_id || "尚未生成"]]));
  const row = actionRow();
  if (isTaskBusy(task) && task.last_operation === "crawl") {
    addPageAction(row, "停止抓取", () => stopTask(task), "quiet");
  } else if (task.status === "failed" && task.last_operation === "crawl") {
    addPageAction(row, "重试抓取", () => runAction(() => api(`/api/tasks/${task.task_id}/crawl`, { method: "POST", body: { retry_failed: true } }), "已重试抓取。"), "primary", !ready);
  } else if (!task.run_id) {
    addPageAction(row, "抓取题目", () => runAction(() => api(`/api/tasks/${task.task_id}/crawl`, { method: "POST", body: { retry_failed: false } }), "已加入抓取队列。"), "primary", !ready);
  }
  if (row.childElementCount) section.appendChild(row);
  return section;
}
function renderPackageReviewPage() {
  if (state.review) return renderReviewCard(false);
  const section = card();
  section.appendChild(statusLine(state.reviewError ? "danger" : "info", state.reviewError || "正在读取题目与附件"));
  return section;
}
function renderModelAuthorizationPage(task) {
  const section = card();
  const profile = state.profiles.find((item) => item.profile_id === task.model_profile_id);
  const select = node("select");
  select.setAttribute("aria-label", "模型档案");
  select.appendChild(new Option("选择模型档案", ""));
  for (const item of state.profiles) select.appendChild(new Option(`${item.name} · v${item.version}`, item.profile_id));
  select.value = profile ? profile.profile_id : "";
  select.disabled = task.status === "waiting_model_authorization";
  select.addEventListener("change", () => changeTaskModelProfile(task, select.value));
  section.appendChild(select);
  if (!profile) section.appendChild(statusLine("warning", "尚未选择模型档案"));
  return section;
}
function renderSolveProgress() {
  const [percent, label] = solveProgressView();
  const wrapper = node("div", "solve-progress");
  wrapper.dataset.solveProgress = "true";
  const heading = node("div", "solve-progress-heading");
  const title = node("strong", "", label);
  title.dataset.solveProgressLabel = "true";
  const amount = node("span", "", `${percent}%`);
  amount.dataset.solveProgressPercent = "true";
  heading.append(title, amount);
  const track = node("div", "progress-track");
  const fill = node("div", "progress-fill");
  fill.dataset.solveProgressFill = "true";
  fill.style.width = `${percent}%`;
  track.appendChild(fill);
  wrapper.append(heading, track);
  return wrapper;
}
function solveProgressView() {
  const stages = {
    started: [8, "正在准备解题任务"],
    model_request_started: [38, "正在请求模型服务"],
    model_response_received: [74, "模型已返回，正在解析答案"],
    answers_written: [94, "答案已写入，正在校验题包"],
    completed: [100, "解题完成"]
  };
  const solveEvents = state.runtimeEvents.filter((event) => event.stage === "solve");
  const latest = solveEvents[solveEvents.length - 1];
  return stages[latest?.status] || [5, "正在等待解题进度"];
}
function updateSolveProgress() {
  const wrapper = el("detailPane").querySelector("[data-solve-progress]");
  if (!wrapper) return;
  const [percent, label] = solveProgressView();
  const title = wrapper.querySelector("[data-solve-progress-label]");
  const amount = wrapper.querySelector("[data-solve-progress-percent]");
  const fill = wrapper.querySelector("[data-solve-progress-fill]");
  if (title && title.textContent !== label) title.textContent = label;
  if (amount && amount.textContent !== `${percent}%`) amount.textContent = `${percent}%`;
  if (fill && fill.style.width !== `${percent}%`) fill.style.width = `${percent}%`;
}
function renderSolvePage(task) {
  const section = card();
  const failed = task.status === "failed" && task.last_operation === "solve";
  if (failed) {
    section.appendChild(statusLine("danger", "上次解题失败"));
  } else {
    section.appendChild(renderSolveProgress());
  }
  const row = actionRow();
  if (failed) addPageAction(row, "重新授权解题", () => requestModelAuthorization(task, { retry_failed: true, overwrite_existing_result: true }));
  else if (isTaskBusy(task)) addPageAction(row, "停止解题", () => stopTask(task), "quiet");
  if (row.childElementCount) section.appendChild(row);
  return section;
}
function renderAnswerReviewPage(task) {
  const section = card();
  if (["ready_for_interaction", "filling"].includes(task.status)) {
    section.appendChild(renderFillingProgress(task));
    if (task.status === "filling") {
      const row = actionRow();
      addPageAction(row, "停止填写", () => stopTask(task), "quiet");
      section.appendChild(row);
    }
    return section;
  }
  if (["filled_unsubmitted", "unconfirmed", "needs_inspection"].includes(task.status) || (task.status === "failed" && task.last_operation === "interact")) {
    section.appendChild(statusLine("warning", "上次填写需检查网页后重新填写"));
    const row = actionRow();
    addPageAction(row, "我已检查，重新填写", () => retryInteraction(task));
    section.appendChild(row);
    return section;
  }
  if (!state.review) {
    section.appendChild(statusLine(state.reviewError ? "danger" : "info", state.reviewError || "正在读取答案"));
    return section;
  }
  const wrap = node("div", "wizard-page-stack");
  wrap.appendChild(renderReviewCard(true));
  const row = actionRow();
  const save = actionButton("保存并校验", saveReviewedAnswers, "secondary");
  save.disabled = task.status !== "answer_review" || task.queue_state !== null;
  row.appendChild(save);
  wrap.appendChild(row);
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
  const section = card();
  if (state.submitPending) {
    section.appendChild(facts([["题包", state.submitPending.run_id], ["题目", `${state.submitPending.question_count} 题`], ["有效期", formatTime(state.submitPending.expires_at)]]));
    section.appendChild(statusLine("warning", "提交前会重新核验页面控件"));
    const row = actionRow();
    addPageAction(row, "打开提交确认", () => showSubmitConfirmation(state.submitPending));
    section.appendChild(row);
    return section;
  }
  if (task.status === "submitting") {
    section.appendChild(statusLine("info", "已确认提交，等待网站返回结果"));
  } else if (task.status === "cancelling") {
    section.appendChild(statusLine("warning", "正在等待安全停止"));
  } else if (["filled_unsubmitted", "confirmed", "failed", "unconfirmed", "cancelled", "needs_inspection", "archived"].includes(task.status)) {
    if (task.run_id) section.appendChild(facts([["题包", task.run_id]]));
    if (["filled_unsubmitted", "unconfirmed", "needs_inspection"].includes(task.status)) {
      const row = actionRow();
      addPageAction(row, "我已检查，重新填写", () => retryInteraction(task), "secondary");
      if (!section.childElementCount) return row;
      section.appendChild(row);
    }
  } else {
    section.appendChild(statusLine("info", "等待填写步骤完成"));
  }
  return section.childElementCount ? section : null;
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
  if (state.wizardStep === "model_authorization") return "授权并解题";
  if (state.wizardStep === "solve") return "等待解题完成";
  if (state.wizardStep === "answer_review") return "填写答案";
  if (state.wizardStep === "submit_confirmation") return state.submitPending ? "打开提交确认" : "等待提交确认";
  return "下一步";
}
function renderWizardNav(task) {
  const selectedIndex = wizardIndex(state.wizardStep);
  const section = node("section", "wizard-nav");
  const previous = actionButton("上一步", () => handleWizardPrevious(task), "quiet");
  previous.disabled = selectedIndex <= 0 || state.wizardStep === "submit_confirmation" || isTaskBusy(task);
  const next = actionButton(nextButtonLabel(task), () => handleWizardNext(task), "primary");
  next.disabled = !canAdvanceWizard(task);
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
  const message = task.task_type === "course" ? "确定要停止当前课程学习吗？" : "确定要停止当前任务操作吗？后端会在安全位置停止，不会自动重试提交。";
  if (!window.confirm(message)) return;
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
  const section = card(`题目与答案 · ${review.questions.length} 题`);
  if (editable) section.appendChild(renderAnswerReviewSummary(review.questions));
  const list = node("div", "question-list");
  review.questions.forEach((question, index) => list.appendChild(renderQuestion(question, editable, index)));
  section.appendChild(list);
  return section;
}
function renderAnswerReviewSummary(questions) {
  const summary = node("div", "answer-review-summary");
  const top = node("div", "answer-review-summary-top");
  const label = node("strong", "", "答案完成情况");
  const count = node("span", "");
  count.dataset.answerReviewCount = "true";
  top.append(label, count);
  const track = node("div", "progress-track");
  const fill = node("div", "progress-fill");
  fill.dataset.answerReviewFill = "true";
  track.appendChild(fill);
  const navigation = node("nav", "answer-question-nav");
  navigation.setAttribute("aria-label", "按题号定位");
  questions.forEach((question, index) => {
    const link = node("button", "answer-question-link", String(question.number));
    link.type = "button";
    link.setAttribute("aria-label", `跳转到第 ${question.number} 题`);
    link.dataset.answerQuestionLink = String(index);
    link.addEventListener("click", () => {
      const target = el(`answer-question-${index}`);
      if (!target) return;
      target.scrollIntoView({ behavior: motionScrollBehavior(), block: "start" });
      target.focus({ preventScroll: true });
    });
    navigation.appendChild(link);
  });
  summary.append(top, track, navigation);
  updateAnswerReviewSummary(questions, summary);
  return summary;
}
function questionHasAnswer(question) {
  const answer = state.answerMap.get(question.question_id);
  if (!answer) return false;
  if (question.question_type === "single_choice") return Boolean(answer.value);
  if (question.question_type === "multiple_choice") return answer.values.size > 0;
  if (question.question_type === "fill_blank") return question.answer_slots.every((slot) => (answer.values.get(slot.slot_id) || "").trim());
  return Boolean(answer.value && answer.value.trim());
}
function updateAnswerReviewSummary(questions = state.review?.questions || [], root = el("detailPane")) {
  const summary = root.matches?.(".answer-review-summary") ? root : root.querySelector(".answer-review-summary");
  if (!summary) return;
  const answered = questions.filter(questionHasAnswer).length;
  const count = summary.querySelector("[data-answer-review-count]");
  const fill = summary.querySelector("[data-answer-review-fill]");
  if (count) count.textContent = `${answered} / ${questions.length} 题已完成`;
  if (fill) fill.style.width = `${questions.length ? answered / questions.length * 100 : 0}%`;
  summary.querySelectorAll("[data-answer-question-link]").forEach((link, index) => {
    const complete = questionHasAnswer(questions[index]);
    link.classList.toggle("complete", complete);
    link.classList.toggle("incomplete", !complete);
    link.setAttribute("aria-label", `第 ${questions[index].number} 题，${complete ? "已填写" : "未填写"}`);
  });
}
function renderQuestion(question, editable, index) {
  const item = node("article", "question-card");
  item.id = `answer-question-${index}`;
  item.dataset.answerQuestionIndex = String(index);
  item.tabIndex = -1;
  const heading = node("div", "question-heading");
  heading.appendChild(node("strong", "question-number", `第 ${question.number} 题`));
  heading.appendChild(node("span", "question-type", question.source_type || question.question_type));
  item.appendChild(heading);
  item.appendChild(node("div", "question-stem", question.stem.text || "（无文字题干，请查看附件）"));
  const stemAssets = renderAssets(question.stem.assets);
  if (stemAssets) item.appendChild(stemAssets);
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
    const optionAssets = renderAssets(option.content?.assets || []);
    if (optionAssets) wrapper.appendChild(optionAssets);
  }
  if (!(question.options || []).length) wrapper.appendChild(node("p", "form-hint", "此题没有选项。"));
  return wrapper;
}
function renderAssets(assets) {
  if (!assets || !assets.length) return null;
  const grid = node("div", "asset-grid");
  for (const source of assets) {
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
      input.addEventListener("change", () => { answer.value = option.option_id; updateAnswerReviewSummary(); });
      const label = node("label", "", `${option.label}. ${option.content.text || "（图片选项）"}`);
      row.append(input, label); wrapper.appendChild(row);
      const assets = renderAssets(option.content.assets);
      if (assets) wrapper.appendChild(assets);
    }
  } else if (question.question_type === "multiple_choice") {
    for (const option of question.options) {
      const row = node("div", "option-editor");
      const input = node("input"); input.type = "checkbox"; input.value = option.option_id; input.checked = answer.values.has(option.option_id);
      input.addEventListener("change", () => { if (input.checked) answer.values.add(option.option_id); else answer.values.delete(option.option_id); updateAnswerReviewSummary(); });
      const label = node("label", "", `${option.label}. ${option.content.text || "（图片选项）"}`);
      row.append(input, label); wrapper.appendChild(row);
      const assets = renderAssets(option.content.assets);
      if (assets) wrapper.appendChild(assets);
    }
  } else if (question.question_type === "fill_blank") {
    for (const slot of question.answer_slots) {
      const field = node("div", "slot-editor");
      field.appendChild(node("span", "slot-label", `空位 ${slot.ordinal}`));
      const input = node("input"); input.type = "text"; input.value = answer.values.get(slot.slot_id) || "";
      input.addEventListener("input", () => { answer.values.set(slot.slot_id, input.value); updateAnswerReviewSummary(); });
      field.appendChild(input); wrapper.appendChild(field);
    }
  } else {
    const input = node("textarea"); input.value = answer.value || ""; input.placeholder = "填写纯文本答案";
    input.addEventListener("input", () => { answer.value = input.value; updateAnswerReviewSummary(); });
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
  const rows = [["模型档案", `${summary.profile_name} · v${summary.profile_version}`], ["模型", summary.model], ["请求目的地", summary.service_destination], ["题目数量", summary.question_count], ["图片附件", summary.image_count]];
  const panel = el("modelAuthorizationSummary");
  const signature = JSON.stringify(rows);
  if (panel.dataset.authorizationSignature !== signature || panel.childElementCount !== rows.length) {
    clear(panel);
    for (const [label, value] of rows) { const row = node("div", "summary-row"); row.append(node("span", "", label), node("span", "", value)); panel.appendChild(row); }
    panel.dataset.authorizationSignature = signature;
  }
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
  const rows = [["题包", pending.run_id], ["站点", siteLabel(pending.site_id)], ["题目数量", pending.question_count], ["确认有效期", formatTime(pending.expires_at)]];
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
  const details = node("details", "disclosure event-card");
  details.appendChild(node("summary", ""));
  const list = node("div", "event-list");
  details.appendChild(list);
  syncEventCard(details);
  return details;
}
function updateEventCard() {
  const pane = el("detailPane");
  let details = pane.querySelector(".event-card");
  if (!details) {
    details = renderEventCard();
    pane.appendChild(details);
    return;
  }
  syncEventCard(details);
}
function eventList() {
  return [...(state.detail?.events || [])].reverse().slice(-20);
}
function eventKey(event, index) {
  const identity = event.sequence ?? event.event_id;
  return identity == null
    ? `${event.occurred_at || ""}\u0001${event.event_type || ""}\u0001${event.from_status || ""}\u0001${event.to_status || ""}\u0001${event.safe_error_category || ""}\u0001${index}`
    : String(identity);
}
function makeEventRow(event, key) {
  const row = node("div", "event-row");
  row.dataset.eventKey = key;
  row.appendChild(node("div", "event-time", formatTime(event.occurred_at)));
  row.appendChild(node("div", "event-text", [event.event_type, event.to_status, event.safe_error_category].filter(Boolean).join(" · ")));
  return row;
}
function syncEventCard(details) {
  const events = eventList();
  const summary = details.querySelector(":scope > summary");
  const list = details.querySelector(":scope > .event-list");
  const summaryText = `事件记录 · ${events.length}`;
  if (summary && summary.textContent !== summaryText) summary.textContent = summaryText;
  const existing = new Map([...list.querySelectorAll(".event-row")].map((row) => [row.dataset.eventKey, row]));
  const desiredKeys = new Set();
  let cursor = list.firstElementChild;
  if (!events.length) {
    list.replaceChildren(node("p", "form-hint", "暂无事件记录。"));
    return;
  }
  list.querySelector(":scope > .form-hint")?.remove();
  for (const [index, event] of events.entries()) {
    const key = eventKey(event, index);
    desiredKeys.add(key);
    const row = existing.get(key) || makeEventRow(event, key);
    const time = row.querySelector(".event-time");
    const text = row.querySelector(".event-text");
    const timeText = formatTime(event.occurred_at);
    const eventText = [event.event_type, event.to_status, event.safe_error_category].filter(Boolean).join(" · ");
    if (time && time.textContent !== timeText) time.textContent = timeText;
    if (text && text.textContent !== eventText) text.textContent = eventText;
    if (row !== cursor) list.insertBefore(row, cursor);
    cursor = row.nextElementSibling;
  }
  for (const [key, row] of existing) if (!desiredKeys.has(key)) row.remove();
}
async function archiveTask(task) {
  const message = task.task_type === "course" ? "确认归档此课程任务？归档会保留课程记录。" : "确认归档此任务？归档不会删除题包，可在任务库中保留记录。";
  if (!window.confirm(message)) return;
  await runAction(() => api(`/api/tasks/${task.task_id}/archive`, { method: "POST", body: {} }), "任务已归档。");
}
let deleteTarget = null;
function openDeleteTaskDialog(task) {
  const isCourse = task.task_type === "course";
  const packageMode = !isCourse && Boolean(task.run_id);
  deleteTarget = task;
  el("deleteTaskHeading").textContent = isCourse ? "删除运行记录" : (packageMode ? "删除任务与题包" : "删除任务");
  el("deleteTaskTitle").textContent = task.title;
  const facts = el("deleteTaskFacts");
  clear(facts);
  const rows = [["站点", siteLabel(task.site_id)]];
  if (packageMode) rows.push(["题包", task.run_id]);
  else rows.push(["状态", STATUS_LABELS[task.status] || task.status]);
  rows.push(["更新", formatTime(task.updated_at)]);
  for (const [label, value] of rows) {
    const row = node("div", "summary-row");
    row.append(node("span", "", label), node("span", "", value));
    facts.appendChild(row);
  }
  el("deleteTaskWarning").textContent = packageMode
    ? "题目、附件、答案、执行结果和任务记录会一并移除，无法恢复。"
    : (isCourse ? "课程进度、事件记录与运行目录会一并移除。" : "该任务还没有题包，只会移除任务记录与状态历史，不涉及本机文件。");
  el("deleteTaskConfirmRow").hidden = !packageMode;
  const input = el("deleteTaskConfirmInput");
  input.value = "";
  const confirm = el("confirmDeleteTask");
  confirm.textContent = packageMode ? "永久删除" : (isCourse ? "删除运行记录" : "删除任务");
  confirm.disabled = packageMode;
  input.oninput = () => { confirm.disabled = input.value.trim() !== "DELETE"; };
  input.onkeydown = (event) => { if (event.key === "Enter" && !confirm.disabled) { event.preventDefault(); confirmDeleteTask(); } };
  openDialog("deleteTaskDialog");
  if (packageMode) setTimeout(() => input.focus(), 30);
}
async function confirmDeleteTask() {
  const task = deleteTarget;
  if (!task) return;
  const packageMode = task.task_type !== "course" && Boolean(task.run_id);
  closeDialog("deleteTaskDialog");
  deleteTarget = null;
  try {
    await api(`/api/tasks/${task.task_id}`, {
      method: "DELETE",
      body: packageMode ? { mode: "package", confirmation: "DELETE" } : { mode: "record" }
    });
    toast(packageMode ? "任务与题包已删除。" : "任务已删除。", "success");
    if (state.activeTaskId === task.task_id) {
      state.activeTaskId = null;
      state.detail = null;
      localStorage.removeItem("autolearn.activeTask");
      renderEmptyDetail();
    }
    await refreshOverview();
    await refreshDetail();
  } catch (error) { toast(error.message, "error"); }
}

function openNewTask() {
  state.editTaskId = null;
  const form = el("newTaskForm");
  form.reset();
  renderProfileOptions();
  form.elements.model_profile_id.value = state.profiles.find((profile) => profile.is_default)?.profile_id || "";
  applyTaskType("homework");
  openDialog("newTaskDialog");
}
async function openEditTask() {
  if (!state.detail) return;
  state.editTaskId = state.detail.task.task_id;
  try {
    const settings = await api(`/api/tasks/${state.editTaskId}/draft-settings`);
    const form = el("newTaskForm");
    form.elements.task_type.value = "homework";
    applyTaskType("homework");
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
  const taskType = form.elements.task_type.value;
  const payload = taskType === "course" ? {
    task_type: "course",
    site_id: form.elements.site_id.value,
    title: form.elements.title.value.trim(),
    course: {}
  } : {
    title: form.elements.title.value.trim(),
    site_id: form.elements.site_id.value,
    page_url: form.elements.page_url.value.trim(),
    model_profile_id: form.elements.model_profile_id.value || null
  };
  let createdCourseTask = null;
  if (taskType === "homework" && !payload.page_url) { toast("请填写作业网址。", "error"); return; }
  try {
    if (state.editTaskId && taskType === "homework") {
      await api(`/api/tasks/${state.editTaskId}`, { method: "PUT", body: payload });
      toast("任务设置已保存。", "success");
    } else {
      const task = await api("/api/tasks", { method: "POST", body: payload });
      state.activeTaskId = task.task_id;
      localStorage.setItem("autolearn.activeTask", task.task_id);
      toast("任务草稿已创建。", "success");
      if (taskType === "course") createdCourseTask = task;
    }
    closeDialog("newTaskDialog"); state.editTaskId = null;
    await refreshOverview(); await refreshDetail();
    if (createdCourseTask) await startAuthentication(createdCourseTask, false);
  } catch (error) { toast(error.message, "error"); }
}
function renderProfileList() {
  const list = el("profileList");
  const signature = state.profiles.map((profile) => [profile.profile_id, profile.name, profile.version, profile.is_default, profile.api_key_configured].join("\u0001")).join("\u0002");
  if (list.dataset.profileSignature === signature && list.childElementCount) {
    list.querySelectorAll(".profile-item").forEach((button) => button.classList.toggle("selected", button.dataset.profileId === state.profileSelection));
    return;
  }
  clear(list);
  for (const profile of state.profiles) {
    const button = node("button", "profile-item");
    button.type = "button";
    button.dataset.profileId = profile.profile_id;
    if (profile.profile_id === state.profileSelection) button.classList.add("selected");
    button.appendChild(node("strong", "", profile.name));
    button.appendChild(node("span", "", `v${profile.version}${profile.is_default ? " · 默认" : ""}${profile.api_key_configured ? " · 已配置密钥" : ""}`));
    button.addEventListener("click", () => selectProfile(profile.profile_id));
    list.appendChild(button);
  }
  if (!state.profiles.length) list.appendChild(node("p", "form-hint", "还没有模型档案。"));
  list.dataset.profileSignature = signature;
}
function selectProfile(profileId) {
  state.profileSelection = profileId;
  renderProfileList();
  const profile = state.profiles.find((item) => item.profile_id === profileId);
  const form = el("profileForm");
  el("deleteProfile").disabled = !profile;
  el("profileTestResult").textContent = "";
  el("profileTestResult").className = "form-message";
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
function readProfileForm() {
  const form = el("profileForm");
  return {
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
}
async function testProfileConnection() {
  const button = el("testProfile");
  const message = el("profileTestResult");
  const payload = readProfileForm();
  if (!payload.settings.base_url.trim()) {
    message.className = "form-message error"; message.textContent = "请先填写完整请求 URL。";
    return;
  }
  const label = button.textContent;
  button.disabled = true; button.textContent = "测试中…";
  message.className = "form-message"; message.textContent = "正在发送测试请求…";
  try {
    const result = await api("/api/profiles/test", { method: "POST", body: payload });
    if (result.ok) {
      const reply = result.reply ? `，模型回复：${result.reply}` : "";
      message.className = "form-message success";
      message.textContent = `连接正常：${result.endpoint_host || "目标地址"} 在 ${result.latency_ms} ms 内响应（${result.api_style || "未知"}）${reply}`;
    } else {
      message.className = "form-message error";
      message.textContent = result.message || "连接测试失败。";
    }
  } catch (error) {
    message.className = "form-message error"; message.textContent = error.message;
  } finally {
    button.disabled = false; button.textContent = label;
  }
}
async function saveProfile(event) {
  event.preventDefault();
  const message = el("profileMessage");
  const payload = readProfileForm();
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
  el("newTaskFromEmpty").addEventListener("click", openNewTask);
  el("openSettings").addEventListener("click", () => { if (!state.profileSelection && state.profiles.length) { const selected = state.profiles.find((profile) => profile.is_default) || state.profiles[0]; selectProfile(selected.profile_id); } openDialog("profileDialog"); });
  el("newTaskForm").addEventListener("submit", submitNewTask);
  document.querySelectorAll(".type-switch-button").forEach((button) => button.addEventListener("click", () => { state.editTaskId = null; applyTaskType(button.dataset.taskType); }));
  el("profileForm").addEventListener("submit", saveProfile);
  el("newProfileButton").addEventListener("click", () => selectProfile(null));
  el("testProfile").addEventListener("click", testProfileConnection);
  el("makeDefaultProfile").addEventListener("click", makeDefaultProfile);
  el("deleteProfile").addEventListener("click", deleteSelectedProfile);
  el("confirmDeleteTask").addEventListener("click", confirmDeleteTask);
  el("shutdownService").addEventListener("click", shutdownService);
  el("refreshTasks").addEventListener("click", async () => { await refreshOverview(); await refreshDetail(true); });
  el("taskSearch").addEventListener("input", renderTaskList);
  el("sortFilter").addEventListener("change", renderTaskList);
  el("siteFilter").addEventListener("change", renderTaskList);
  el("typeFilter").addEventListener("change", () => { renderMeta(); renderTaskList(); });
  el("statusFilter").addEventListener("change", async () => { await refreshOverview(); });
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
