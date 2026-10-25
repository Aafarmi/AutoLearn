"""课程任务服务编排（T5-01 的认证部分）。

**为什么不让 `CourseJobStore` 承担认证**：存储层的职责是"把状态写对"，认证要
拉浏览器、读 config.json、判登录态，还要在失败时把状态退回 `draft`。这些放进
store 会让一个纯持久化类突然依赖 cookie manager 与凭据读取，既难测也违背
T2-02 的分层。所以认证动作放这里，由 `task_router._COURSE_METHODS` 转发过来。

认证链路沿用 `CourseJobStore` 状态与 Cookie Manager 配置写入：WebUI 启动 `qr_sessions`
后端会话，由独立 bridge 直接调用上游 `Fucker.login(use_qr=True)`；扫码成功后用
`cookie_manager.persist_cookies_for_module()` 原子保存，再按上游 `Fucker.cookies`
setter 的口径做 `CASLOGC → uuid` 预检。QR 状态由单独接口轮询，认证启动不阻塞请求。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from typing import Any

from ..auth import cookie_manager
from ..common.clock import timestamp
from ..credentials import CredentialProviderError, LocalCredentialProvider
from .domain import (
    CourseJob,
    CourseMode,
    CourseOptions,
    CoursePhase,
    CourseStatus,
    ensure_transition,
    job_id as new_job_id,
)
from .errors import (
    CourseAuthRequired,
    CourseBusy,
    CourseError,
    CourseInvalid,
    CourseTransitionError,
    CourseUnsupported,
)
from .providers.zhihuishu import aimap
from .providers.zhihuishu import auth as zhihuishu_auth
from .providers.zhihuishu import preflight
from .sites import is_course_site, modes_for
from .store import CourseJobStore
from core.settings import ModelProfileError, ModelProfileStore

logger = logging.getLogger(__name__)

#: 能直接进入 `authenticating` 的状态——迁移表里只有这两条边。
AUTHENTICATABLE: frozenset[CourseStatus] = frozenset(
    {CourseStatus.DRAFT, CourseStatus.READY}
)
RERUNNABLE: frozenset[CourseStatus] = frozenset(
    {CourseStatus.COMPLETED, CourseStatus.PARTIAL, CourseStatus.FAILED, CourseStatus.CANCELLED}
)

#: 浏览器首先打开的登录页（取适配器声明，别在这里重复写域名）。
CAPTURE_START_URL: str = zhihuishu_auth.CAPTURE_START_URL


def validate_create(
    site_id: str,
    mode: CourseMode | str,
    course_id: str,
    class_id: str | None,
    options: CourseOptions | Mapping[str, Any],
    *,
    require_ai_profile: bool = True,
) -> None:
    """验证课程创建参数；AI 配置只解析到内存，不写入任务数据。"""
    if not isinstance(site_id, str) or not is_course_site(site_id):
        raise CourseUnsupported("不支持的课程站点。")
    try:
        selected_mode = mode if isinstance(mode, CourseMode) else CourseMode(mode)
    except (TypeError, ValueError):
        raise CourseInvalid("mode 字段无效。") from None
    if selected_mode.value not in modes_for(site_id):
        raise CourseUnsupported("该课程站点不支持此学习模式。")

    if class_id is not None and not isinstance(class_id, str):
        raise CourseInvalid("class_id 字段必须是文本。")

    if not isinstance(course_id, str) or not course_id or len(course_id) > 128:
        raise CourseInvalid("course_id 字段长度必须为 1 至 128 个字符。")
    if selected_mode is CourseMode.SHARED:
        if not any(character.isalpha() for character in course_id):
            raise CourseInvalid("course_id 字段必须包含字母。")
    elif not course_id.isdecimal():
        raise CourseInvalid("course_id 字段必须为纯数字。")

    if selected_mode is CourseMode.AI and (
        not isinstance(class_id, str) or not class_id.strip()
    ):
        raise CourseInvalid("class_id 字段不能为空。")

    values = _option_values(options)
    speed = values["speed"]
    if (
        isinstance(speed, bool)
        or not isinstance(speed, (int, float))
        or not math.isfinite(float(speed))
        or not 0.1 <= float(speed) <= 500
    ):
        raise CourseInvalid("speed 字段必须在 0.1 至 500 之间。")

    threshold = values["threshold"]
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(float(threshold))
        or not 0 < float(threshold) <= 1
    ):
        raise CourseInvalid("threshold 字段必须大于 0 且不超过 1。")

    limit = values["time_limit_minutes"]
    if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 14400:
        raise CourseInvalid("time_limit_minutes 字段必须为 0 至 14400 的整数。")
    if not isinstance(values["process_exam"], bool):
        raise CourseInvalid("process_exam 字段必须为布尔值。")

    profile_id = values["model_profile_id"]
    if profile_id is not None and (
        not isinstance(profile_id, str) or not profile_id.strip()
    ):
        raise CourseInvalid("model_profile_id 字段无效。")
    if selected_mode is CourseMode.AI and (require_ai_profile or profile_id is not None):
        if not isinstance(profile_id, str) or not profile_id.strip():
            raise CourseInvalid("model_profile_id 字段为 AI 模式必填。")
        try:
            profile_settings = ModelProfileStore().resolve_settings(profile_id)
        except (ModelProfileError, OSError, ValueError):
            raise CourseUnsupported("指定的模型档案不可用。") from None
        # 及早拒绝无法供上游使用的档案；密钥只在返回值的临时运行配置中存在。
        aimap.build(profile_settings)


def create_job(
    site_id: str,
    title: str | None,
    course: Mapping[str, Any],
) -> CourseJob:
    """校验并新建课程草稿；只写课程 JSON 存储，不触碰做题 TaskStore。"""
    if not isinstance(course, Mapping):
        raise CourseInvalid("course 字段必须是对象。")
    allowed = {
        "mode", "course_id", "class_id", "speed", "threshold",
        "time_limit_minutes", "process_exam", "model_profile_id",
    }
    unknown = set(course) - allowed
    if unknown:
        raise CourseInvalid(f"course 字段含不支持的字段：{', '.join(sorted(unknown))}。")
    if title is not None and not isinstance(title, str):
        raise CourseInvalid("title 字段必须是文本。")
    if not isinstance(site_id, str) or not is_course_site(site_id):
        raise CourseUnsupported("不支持的课程站点。")
    mode_value = course.get("mode")
    if mode_value is None:
        mode = None
        course_id = None
        class_id = None
    else:
        try:
            mode = mode_value if isinstance(mode_value, CourseMode) else CourseMode(mode_value)
        except (TypeError, ValueError):
            raise CourseInvalid("mode 字段无效。") from None
        course_id = course.get("course_id")
        class_id = course.get("class_id")
    option_values = {
        "speed": course.get("speed", CourseOptions.speed),
        "threshold": course.get("threshold", CourseOptions.threshold),
        "time_limit_minutes": course.get(
            "time_limit_minutes", CourseOptions.time_limit_minutes
        ),
        "process_exam": course.get("process_exam", CourseOptions.process_exam),
        "model_profile_id": course.get("model_profile_id"),
    }
    if mode is not None:
        validate_create(site_id, mode, course_id, class_id, option_values)
    elif course_id is not None or class_id is not None or option_values["model_profile_id"] is not None:
        raise CourseInvalid("创建任务时不接受课程信息；请登录后从课程目录选择。")
    options = CourseOptions(**option_values)
    now = timestamp()
    job = CourseJob(
        job_id=new_job_id(),
        site_id=site_id,
        title=(title or "课程任务").strip() or "课程任务",
        mode=mode,
        course_id=course_id,
        class_id=class_id,
        status=CourseStatus.DRAFT,
        phase=CoursePhase.SETUP,
        current_title=None,
        progress_percent=0.0,
        completed_count=0,
        total_count=0,
        failed_count=0,
        run_id=None,
        options=options,
        created_at=now,
        updated_at=now,
        last_safe_error_category=None,
    )
    return _store().create(job)


def start(job_id: str) -> CourseJob:
    """从 ready 启动；draft 必须先走合法认证状态链再启动。"""
    store = _store()
    job = store.get_job(job_id)
    if job.mode is None or not job.course_id:
        raise CourseTransitionError("请先登录并选择课程。")
    if job.status is CourseStatus.DRAFT:
        job = _verify_draft_credentials(store, job)
    elif job.status is not CourseStatus.READY:
        raise CourseTransitionError("当前状态不能直接开始；终态任务请使用重跑。")
    _checked_cookies(job.site_id)
    ai_config = _ai_config(job)
    run = _runtime().start(job, {"ai_config": ai_config} if ai_config is not None else None)
    return store.get_job(run.job_id)


def rerun(job_id: str) -> CourseJob:
    """重新运行终态任务；状态机允许终态直接迁移到 running。"""
    store = _store()
    job = store.get_job(job_id)
    if job.mode is None or not job.course_id:
        raise CourseTransitionError("请先登录并选择课程。")
    if job.status not in RERUNNABLE:
        raise CourseTransitionError("当前状态不能重跑课程任务。")
    _checked_cookies(job.site_id)
    ai_config = _ai_config(job)
    run = _runtime().start(job, {"ai_config": ai_config} if ai_config is not None else None)
    return store.get_job(run.job_id)


def update_options(job_id: str, values: Mapping[str, Any]) -> CourseJob:
    """更新未运行任务的运行参数；状态和课程身份不可由设置接口修改。"""
    if not isinstance(values, Mapping):
        raise CourseInvalid("设置请求必须是对象。")
    allowed = {"speed", "threshold", "time_limit_minutes", "process_exam", "model_profile_id"}
    unknown = set(values) - allowed
    if unknown:
        raise CourseInvalid(f"设置字段不支持：{', '.join(sorted(unknown))}。")
    store = _store()
    job = store.get_job(job_id)
    if job.status not in {CourseStatus.DRAFT, CourseStatus.READY, *RERUNNABLE}:
        raise CourseTransitionError("任务运行期间不能修改课程设置。")
    merged = {name: getattr(job.options, name) for name in allowed}
    merged.update(values)
    if job.mode is None or not job.course_id:
        if merged["model_profile_id"] is not None:
            raise CourseInvalid("请选择 AI 课程后再配置模型档案。")
        # Permit default runtime options to be set before choosing a course.
    else:
        validate_create(job.site_id, job.mode, job.course_id, job.class_id, merged)
    return store.update(job, options=CourseOptions(**merged))


def select_course(job_id: str, values: Mapping[str, Any]) -> CourseJob:
    """Bind one post-login catalog entry to a draft course job."""
    if not isinstance(values, Mapping):
        raise CourseInvalid("课程选择必须是对象。")
    allowed = {"mode", "course_id", "class_id", "name", "speed", "threshold", "time_limit_minutes", "process_exam", "model_profile_id"}
    unknown = set(values) - allowed
    if unknown:
        raise CourseInvalid("课程选择包含无效字段。")
    store = _store()
    job = store.get_job(job_id)
    if job.status is not CourseStatus.READY:
        raise CourseTransitionError("请先完成二维码登录。")
    mode = values.get("mode")
    course_id = values.get("course_id")
    class_id = values.get("class_id")
    options = {
        "speed": values.get("speed", CourseOptions.speed),
        "threshold": values.get("threshold", CourseOptions.threshold),
        "time_limit_minutes": values.get("time_limit_minutes", CourseOptions.time_limit_minutes),
        "process_exam": values.get("process_exam", CourseOptions.process_exam),
        "model_profile_id": values.get("model_profile_id"),
    }
    validate_create(job.site_id, mode, course_id, class_id, options, require_ai_profile=False)
    name = values.get("name")
    if name is not None and not isinstance(name, str):
        raise CourseInvalid("课程名称无效。")
    changes: dict[str, Any] = {
        "mode": mode if isinstance(mode, CourseMode) else CourseMode(mode),
        "course_id": course_id,
        "class_id": class_id,
        "options": CourseOptions(**options),
        "phase": CoursePhase.CATALOG,
    }
    if name and job.title == "课程任务":
        changes["title"] = name.strip()[:120] or job.title
    return store.update(job, **changes)


def stop(job_id: str) -> bool:
    return _runtime().stop(job_id)


def archive(job_id: str) -> CourseJob:
    return _store().archive(job_id)


def delete(job_id: str) -> None:
    _store().delete(job_id)


def _verify_draft_credentials(store: CourseJobStore, job: CourseJob) -> CourseJob:
    """无浏览器副作用地验证已有凭据，并记录 draft→authenticating→ready。"""
    ensure_transition(job.status, CourseStatus.AUTHENTICATING)
    authenticating = store.update(job, status=CourseStatus.AUTHENTICATING)
    store.append_business_event(job.job_id, "auth_started", job.status, CourseStatus.AUTHENTICATING)
    try:
        _checked_cookies(job.site_id)
        ensure_transition(CourseStatus.AUTHENTICATING, CourseStatus.READY)
        ready = store.update(authenticating, status=CourseStatus.READY)
        store.append_business_event(
            job.job_id, "authenticated", CourseStatus.AUTHENTICATING, CourseStatus.READY
        )
        return ready
    except Exception as error:
        try:
            current = store.get_job(job.job_id)
            if current.status is CourseStatus.AUTHENTICATING:
                ensure_transition(CourseStatus.AUTHENTICATING, CourseStatus.DRAFT)
                store.update(current, status=CourseStatus.DRAFT)
                store.append_business_event(
                    job.job_id, "auth_failed", CourseStatus.AUTHENTICATING,
                    CourseStatus.DRAFT, safe_error_category=getattr(error, "safe_category", "auth_failed"),
                )
        except Exception:
            logger.exception("课程启动凭据校验失败后无法恢复 draft")
        if isinstance(error, CourseError):
            raise
        if isinstance(error, CredentialProviderError):
            raise CourseAuthRequired("读取本机登录凭据失败，请重新登录后重试。") from None
        raise CourseAuthRequired("登录态校验失败，请重新登录后重试。") from None


def _checked_cookies(site_id: str) -> dict[str, str]:
    try:
        cookies = LocalCredentialProvider().get_site_cookies(site_id)
    except CredentialProviderError:
        raise CourseAuthRequired("读取本机登录凭据失败，请重新登录后重试。") from None
    if not cookies:
        raise CourseAuthRequired("还没有登录凭据，请先登录后再开始学习。")
    preflight.check_cookies(cookies)
    return cookies


def _ai_config(job: CourseJob) -> dict[str, Any] | None:
    if job.mode is not CourseMode.AI:
        return None
    profile_id = job.options.model_profile_id
    if not profile_id:
        raise CourseUnsupported("AI 模式缺少模型档案。")
    try:
        profile = LocalCredentialProvider().get_model_settings(profile_id)
    except CredentialProviderError:
        raise CourseUnsupported("指定的模型档案不可用。") from None
    return aimap.build(profile)


def _runtime() -> Any:
    from .runtime import shared

    return shared()


def _option_values(options: CourseOptions | Mapping[str, Any]) -> dict[str, Any]:
    """读取已有 CourseOptions 或请求映射，并沿用域模型中的默认值。"""
    defaults = CourseOptions()
    names = ("speed", "threshold", "time_limit_minutes", "process_exam", "model_profile_id")
    if isinstance(options, CourseOptions):
        return {name: getattr(options, name) for name in names}
    if isinstance(options, Mapping):
        return {name: options.get(name, getattr(defaults, name)) for name in names}
    raise CourseInvalid("options 字段必须是对象。")


def authenticate(job_id: str, *, retry_failed: bool = False) -> CourseJob:
    """Start the upstream QR session; completion is observed via its status endpoint."""
    from .providers.zhihuishu import qr_sessions

    qr_sessions.start(job_id)
    return _store().get_job(job_id)


def _ensure_authenticatable(job: CourseJob, *, retry_failed: bool) -> None:
    """状态门槛：能在 running 时并发登录会把临时 profile 与 Cookie 写乱。"""
    if job.status in {CourseStatus.RUNNING, CourseStatus.STOPPING}:
        raise CourseBusy("任务正在运行，请先停止再重新登录。")
    if job.status is CourseStatus.FAILED:
        # 迁移表：failed → {running, archived}，没有回 authenticating 的边。
        reason = "重试" if retry_failed else "登录"
        raise CourseTransitionError(f"任务已失败，无法直接{reason}；请先重新开始学习。")
    if job.status not in AUTHENTICATABLE:
        raise CourseTransitionError(f"当前状态（{job.status.value}）下不能登录。")


def _transition(
    store: CourseJobStore,
    job_id: str,
    target: CourseStatus,
    event_type: str,
    *,
    safe_error_category: str | None = None,
) -> CourseJob:
    """一次合法迁移 + 业务事件；状态以磁盘上的最新值为准。"""
    current = store.get_job(job_id)
    ensure_transition(current.status, target)
    updated = store.update(current, status=target)
    store.append_business_event(
        job_id,
        event_type,
        current.status,
        target,
        safe_error_category=safe_error_category,
    )
    return updated


def _rollback(store: CourseJobStore, job_id: str, error: BaseException) -> None:
    """认证失败后把任务退回 `draft`；回退本身失败时只记日志，不掩盖原始错误。"""
    try:
        _transition(
            store,
            job_id,
            CourseStatus.DRAFT,
            "auth_failed",
            safe_error_category="auth_failed",
        )
    except (CourseError, ValueError):
        logger.warning("课程认证失败后未能回退 draft（%s）", type(error).__name__)


def _as_course_error(error: BaseException) -> CourseError:
    """把各种失败折算成 WebUI 能直接渲染的课程错误（文案全部已脱敏）。

    认证失败必须同时给出"原因"和"下一步"：只报"未识别到登录页"用户不知道要干
    什么，所以统一补一句"请重新登录后重试"。
    """
    if isinstance(error, CourseError):
        return error
    if isinstance(error, CredentialProviderError):
        return CourseAuthRequired("读取本机登录凭据失败，请重新登录后重试。")
    message = str(error).strip().rstrip("。") or "登录未完成"
    return CourseAuthRequired(f"{message}；请重新登录后重试。")


# --- 可替换的边界（测试在这里注入替身，业务逻辑不留分支） ---------------------


def _store() -> CourseJobStore:
    return CourseJobStore()


def _capture(adapter: Any) -> None:
    """走可见 Chrome 抓 Cookie；成功即已写入 config.json。"""
    cookie_manager.capture_for_module(adapter, CAPTURE_START_URL)


def _cookies(site_id: str) -> dict[str, str]:
    return LocalCredentialProvider().get_site_cookies(site_id)
