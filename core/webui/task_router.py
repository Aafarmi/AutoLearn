"""做题 / 课程任务的分发层（T1-04）。

为什么需要它：现有路由全部假设任务躺在 `tasks.sqlite3` 里（`TaskStore`），
而课程任务在独立的 JSON 存储里（T2-02 的 `CourseJobStore`）。路由层先问这里
“这条 id 属于哪一类”，再决定走哪套 store / runtime。

职责边界：
- 本模块**不序列化 DTO**，只返回 `(kind, payload)`；各接口的 JSON 形状由路由层
  （以及 T2-04 的课程 DTO）决定。
- 本模块**不改任何路由**——T1-05 才把六个通用接口接到这里。
- 课程侧动作方法（停止 / 归档 / 删除 / 鉴权 / 事件）属于 T2-06 / T3-06 / T5-01，
  这里只按 `_COURSE_METHODS` 表转发；方法还不存在时抛 `CourseError(501)`，
  而不是把 `AttributeError` 漏成 500。
"""

from __future__ import annotations

import importlib
import re
from typing import Any

from core.courses.errors import CourseError, CourseNotFound
from core.tasks import QueueState, TaskTransitionError

COURSE_ID_PREFIX = "course-"

# 课程 id 的形状：前缀 + 32 位小写 hex（domain.job_id() 的产物）。
# 只认前缀会把 "course-evil" 这类伪造 id 送进课程分支，因此形状必须严格。
COURSE_ID_PATTERN = re.compile(r"course-[0-9a-f]{32}")

# 课程存储在 T2-02 落地前的可选依赖；做成常量 + 延迟导入，
# 这样课程模块缺席时做题链路照常工作，课程分支退化成 404/503。
_COURSE_STORE_MODULE = "core.courses.store"
_COURSE_STORE_CLASS = "CourseJobStore"

# 课程侧动作方法名（唯一转发点）。值是候选名，按顺序取第一个存在的：
# 这样 T2-06 / T3-06 / T5-01 落地时方法叫 archive() 还是 archive_job() 都能接上。
_COURSE_METHODS = {
    "detail": ("get_job",),
    "stop": ("request_stop", "stop"),
    "archive": ("archive", "archive_job"),
    "delete": ("delete", "delete_job"),
    "runtime_events": ("read_runtime_events", "events_after"),
    "authenticate": ("authenticate", "request_authentication"),
}

#: 归属服务层的动作（T5-01）：认证要拉浏览器、读凭据、失败回退状态，
#: 不属于持久化职责。命中这里的动作**优先**走 service，store 只作旧实现兜底。
_COURSE_SERVICE_METHODS = {
    "authenticate": ("core.courses.service", "authenticate"),
    "start": ("core.courses.service", "start"),
    "rerun": ("core.courses.service", "rerun"),
    "update_options": ("core.courses.service", "update_options"),
    "select_course": ("core.courses.service", "select_course"),
    "stop": ("core.courses.service", "stop"),
    "archive": ("core.courses.service", "archive"),
    "delete": ("core.courses.service", "delete"),
}

# 课程 DTO 模块（T2-04）。缺席时课程详情/摘要接口回 501，而不是漏 500。
_COURSE_DTO_MODULE = "core.courses.dto"


class TaskRouterError(Exception):
    """路由层可翻译的错误（携带建议的 HTTP 状态码）。

    `app.py` 的 `WebUIRequestError` 不能在这里 import（T1-05 之后 app 会 import
    本模块，module 级互相 import 会成环），所以做题侧的用户输入错误用这个类型
    表达；T1-05 在路由层统一转成 422/409。
    """

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _course_store() -> Any | None:
    """课程存储实例；模块尚未落地时返回 None（测试可替换此函数）。"""
    try:
        module = importlib.import_module(_COURSE_STORE_MODULE)
    except ImportError:
        return None
    store_class = getattr(module, _COURSE_STORE_CLASS, None)
    if store_class is None:
        return None
    return store_class()


def _task_store(request: Any) -> Any:
    return request.app.state.task_store


def _runtime(request: Any) -> Any:
    return request.app.state.runtime


def has_course_prefix(task_id: Any) -> bool:
    """只判断前缀（不查存储），用于日志/筛选等不关心存在性的场合。"""
    return isinstance(task_id, str) and task_id.startswith(COURSE_ID_PREFIX)


def is_course_id(task_id: Any) -> bool:
    """这条 id 是否指向一个**真实存在**的课程任务。

    两个条件都要满足：形状合规（防伪造前缀）+ 存储里查得到。
    形状不对或查不到一律返回 False，让调用方回落到做题路径（最终 404），
    避免伪造 id 在课程分支炸成 500。
    """
    if not isinstance(task_id, str) or COURSE_ID_PATTERN.fullmatch(task_id) is None:
        return False
    store = _course_store()
    if store is None:
        return False
    try:
        store.get_job(task_id)
    except (KeyError, CourseNotFound):
        return False
    return True


def resolve_task(request: Any, task_id: str) -> tuple[str, Any]:
    """把一条 id 解析成 `("homework", TaskRecord)` 或 `("course", CourseJob)`。

    - 课程前缀：走 `CourseJobStore.get_job()`；查不到抛 `CourseNotFound`（404）。
    - 其它：走 `TaskStore.get_task()`；`KeyError` 原样抛出，保持现有 404 语义。
    """
    if has_course_prefix(task_id):
        return "course", _load_course_job(task_id)
    return "homework", _task_store(request).get_task(task_id)


def _load_course_job(task_id: str) -> Any:
    store = _course_store()
    if store is None:
        raise CourseError("课程存储尚未就绪，请先完成任务存储落地。", 503)
    try:
        return store.get_job(task_id)
    except (KeyError, CourseNotFound, ValueError):
        raise CourseNotFound("课程任务不存在。") from None


def _course_service_call(action: str) -> Any | None:
    """服务层动作（T5-01）；模块或函数尚未落地时返回 None，由 store 候选名兜底。"""
    target = _COURSE_SERVICE_METHODS.get(action)
    if target is None:
        return None
    module_name, function_name = target
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return None
    function = getattr(module, function_name, None)
    return function if callable(function) else None


def _course_call(action: str, *args: Any, **kwargs: Any) -> Any:
    """按服务层 → 存储层顺序调课程动作；都没落地时抛 501 而不是 500。"""
    service = _course_service_call(action)
    if service is not None:
        return service(*args, **kwargs)
    store = _course_store()
    if store is None:
        raise CourseError("课程存储尚未就绪，请先完成任务存储落地。", 503)
    for method_name in _COURSE_METHODS[action]:
        method = getattr(store, method_name, None)
        if method is not None:
            return method(*args, **kwargs)
    raise CourseError(f"课程动作尚未接通：{_COURSE_METHODS[action][0]}。", 501)


def _course_dto() -> Any:
    """课程 DTO 模块（T2-04）；缺席时抛 501，路由不会因此漏成 500。"""
    try:
        return importlib.import_module(_COURSE_DTO_MODULE)
    except ImportError:
        raise CourseError("课程 DTO 尚未落地（T2-04）。", 501) from None


def course_detail(task_id: str) -> dict[str, Any]:
    """`GET /api/tasks/{id}` 的课程分支：task + cookie_status + queue:null + events + course。

    业务事件在这里从存储读出来注入 DTO（T2-03 的 `read_business_events`）：
    DTO 只管序列化，不碰存储；存储替身没有读事件能力时字段缺席，由 DTO 兜成
    空表。读数失败不致命——详情的主体是任务状态，事件是附加信息。
    """
    job = _load_course_job(task_id)
    dto = _course_dto()
    reader = getattr(_course_store(), "read_business_events", None)
    records = None
    if callable(reader):
        try:
            records = reader(job.job_id)
        except CourseError:
            records = None
    authenticated = any(
        isinstance(event, dict) and event.get("event_type") == "authenticated"
        for event in (records or [])
    )
    try:
        from core.credentials import LocalCredentialProvider
        from core.courses.providers.zhihuishu.preflight import check_cookies

        cookies = LocalCredentialProvider().get_site_cookies(job.site_id)
        check_cookies(cookies)
        configured = True
    except Exception:
        configured = False
    return dto.detail(
        job,
        cookie_status={"captured": authenticated, "configured": configured},
        events=records,
    )


def course_runtime_events(task_id: str, after: int = 0) -> dict[str, Any]:
    """`GET /api/tasks/{id}/runtime-events` 的课程分支（§4.3 的增量契约）。"""
    job = _load_course_job(task_id)
    events = _course_call("runtime_events", job.job_id, after)
    latest = 0
    for event in events:
        sequence = event.get("sequence")
        if isinstance(sequence, int) and sequence > latest:
            latest = sequence
    return {"events": events, "latest_sequence": latest}


def course_stop(task_id: str) -> dict[str, Any]:
    """`POST /api/tasks/{id}/stop` 的课程分支：走 CourseRuntime，返回摘要 + stopped。"""
    job = _load_course_job(task_id)
    stopped = _course_call("stop", job.job_id)
    if not isinstance(stopped, bool):
        stopped = True
    return {"task": _course_dto().summary(_load_course_job(task_id)), "stopped": stopped}


def course_archive(task_id: str) -> dict[str, Any]:
    """`POST /api/tasks/{id}/archive` 的课程分支：走 store.archive()。"""
    job = _load_course_job(task_id)
    archived = _course_call("archive", job.job_id)
    target = archived if getattr(archived, "job_id", None) else _load_course_job(task_id)
    return _course_dto().summary(target)


def course_delete(
    task_id: str, *, mode: str | None = None, confirmation: str | None = None
) -> dict[str, Any]:
    """`DELETE /api/tasks/{id}` 的课程分支：课程没有题包，只接受记录删除。

    mode 校验放在最前面：非法 mode 直接 422，与做题侧同码，且不触碰存储。
    """
    if mode not in {"record", "draft"}:
        raise CourseError("课程任务只能删除记录（mode=record）。", 422)
    job = _load_course_job(task_id)
    _course_call("delete", job.job_id)
    return {"deleted": True}


def course_authenticate(task_id: str, *, retry_failed: bool = False) -> dict[str, Any]:
    """Start upstream QR login and return without blocking for the scan."""
    job = _load_course_job(task_id)
    from core.courses.providers.zhihuishu import qr_sessions
    session = qr_sessions.start(job.job_id)
    return {"task": _course_dto().summary(_load_course_job(task_id)), "authentication": session}


def course_authentication_status(task_id: str) -> dict[str, Any]:
    _load_course_job(task_id)
    from core.courses.providers.zhihuishu import qr_sessions
    return qr_sessions.public_state(task_id)


def course_catalog(task_id: str) -> dict[str, Any]:
    job = _load_course_job(task_id)
    from core.courses.providers.zhihuishu import qr_sessions
    return qr_sessions.fetch_catalog(job.job_id)


def course_outline(task_id: str) -> dict[str, Any]:
    job = _load_course_job(task_id)
    from core.courses.providers.zhihuishu import qr_sessions
    return qr_sessions.fetch_outline(job.job_id)


def course_select(task_id: str, values: Any) -> dict[str, Any]:
    job = _load_course_job(task_id)
    from core.courses.providers.zhihuishu import qr_sessions
    if not isinstance(values, dict) or not qr_sessions.contains_catalog_course(job.job_id, values):
        raise CourseError("请先抓取课程目录，并从列表中选择课程。", 422)
    _course_call("select_course", job.job_id, values)
    return {"task": _course_dto().summary(_load_course_job(task_id))}


def course_start(task_id: str) -> dict[str, Any]:
    """启动课程任务，经 service → runtime；只返回现有摘要契约。"""
    job = _load_course_job(task_id)
    _course_call("start", job.job_id)
    return {"task": _course_dto().summary(_load_course_job(task_id))}


def course_rerun(task_id: str) -> dict[str, Any]:
    """重跑终态课程任务，经 service → runtime。"""
    job = _load_course_job(task_id)
    _course_call("rerun", job.job_id)
    return {"task": _course_dto().summary(_load_course_job(task_id))}


def course_update_options(task_id: str, values: Any) -> dict[str, Any]:
    """更新课程运行选项，经 service 做状态与参数校验。"""
    job = _load_course_job(task_id)
    _course_call("update_options", job.job_id, values)
    return {"task": _course_dto().summary(_load_course_job(task_id))}


# --- 六个转发函数（统一返回 (kind, payload)） --------------------------------


def get_detail(request: Any, task_id: str) -> tuple[str, Any]:
    """详情：payload 为 TaskRecord / CourseJob 本体，DTO 由路由层组装。"""
    return resolve_task(request, task_id)


def stop(request: Any, task_id: str) -> tuple[str, Any]:
    kind, target = resolve_task(request, task_id)
    if kind == "course":
        stopped = _course_call("stop", target.job_id)
        return kind, {
            "job": stopped,
            "stopped": stopped if isinstance(stopped, bool) else True,
        }

    store = _task_store(request)
    item = store.get_queue_item(target.task_id)
    if item.state in {QueueState.QUEUED, QueueState.PAUSED}:
        return kind, {"task": store.cancel_queued_operation(target.task_id), "stopped": True}
    if item.state == QueueState.ACTIVE:
        store.request_cancel_active_operation(target.task_id)
        _runtime(request).request_stop(target.task_id)
        return kind, {"task": store.get_task(target.task_id), "stopped": False}
    raise TaskTransitionError("当前操作不能停止。")


def archive(request: Any, task_id: str) -> tuple[str, Any]:
    kind, target = resolve_task(request, task_id)
    if kind == "course":
        return kind, {"job": _course_call("archive", target.job_id)}
    return kind, {"task": _task_store(request).archive_task(target.task_id)}


def delete(
    request: Any,
    task_id: str,
    *,
    mode: str | None = None,
    confirmation: str | None = None,
) -> tuple[str, Any]:
    kind, target = resolve_task(request, task_id)
    if kind == "course":
        # 课程只有记录、没有题包（§4.5）：只接受 mode=record。
        if mode not in {"record", "draft"}:
            raise CourseError("课程任务只能删除记录（mode=record）。", 422)
        return kind, {"job": _course_call("delete", target.job_id), "deleted": True}

    store = _task_store(request)
    if mode in {"record", "draft"}:
        store.delete_task_record(target.task_id)
    elif mode == "package" and confirmation == "DELETE":
        store.delete_task_and_package(target.task_id)
    else:
        raise TaskRouterError("删除确认无效。", 422)
    return kind, {"deleted": True}


def runtime_events(request: Any, task_id: str, after: int = 0) -> tuple[str, Any]:
    kind, target = resolve_task(request, task_id)
    if kind == "course":
        return kind, {"events": _course_call("runtime_events", target.job_id, after)}

    runtime = _runtime(request)
    return kind, {
        "events": runtime.events_after(after, task_id=target.task_id),
        "latest_sequence": runtime.latest_event_sequence,
    }


def authenticate(request: Any, task_id: str, *, retry_failed: bool = False) -> tuple[str, Any]:
    kind, target = resolve_task(request, task_id)
    if kind == "course":
        from core.courses.providers.zhihuishu import qr_sessions
        _course_call("authenticate", target.job_id, retry_failed=retry_failed)
        return kind, {"job": _course_dto().summary(_load_course_job(task_id)), "authentication": qr_sessions.public_state(task_id)}
    item = _runtime(request).enqueue_authentication(target.task_id, retry_failed=retry_failed)
    return kind, {"task_id": item.task_id, "operation": item.operation, "state": item.state}
