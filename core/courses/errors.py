"""Error types shared by the course domain and its WebUI surface.

契约见 docs/TASKS.md §4.4 / §4.6。所有异常都只携带**安全**信息：不包含
URL、Cookie、API key 或提交确认令牌，可直接映射成 WebUI 的 JSON 错误。
"""

from __future__ import annotations

import re

_MIN_STATUS_CODE = 400
_MAX_STATUS_CODE = 599
_HAN_CHAR = re.compile(r"[\u3400-\u9fff]")
_WINDOWS_PATH = re.compile(r"\b[A-Za-z]:[\\/]")
_POSIX_PRIVATE_PATH = re.compile(r"(?:^|\s)/(?:tmp|temp|var/tmp|home|users)(?:/|\b)", re.I)
_SENSITIVE_TEXT = re.compile(r"traceback|api_key|cookie|https?://", re.I)

_SAFE_FALLBACKS = (
    ("CourseInvalid", "课程参数无效。"),
    ("CourseTransitionError", "当前课程任务状态不允许此操作。"),
    ("CourseBusy", "课程任务正在运行，请稍后重试。"),
    ("CourseNotFound", "课程任务不存在。"),
    ("CourseAuthRequired", "课程登录状态无效，请重新登录后重试。"),
    ("CourseUnsupported", "当前课程能力暂不支持。"),
    ("CourseProtocolError", "课程运行协议异常，请重试。"),
)


class CourseError(Exception):
    """课程任务的基类错误：安全文案 + 建议的 HTTP 状态码。

    `status_code` 省略时使用子类的 `default_status_code`。
    """

    default_status_code = 400
    safe_category = "course_error"

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        code = self.default_status_code if status_code is None else status_code
        if isinstance(code, bool) or not isinstance(code, int):
            raise ValueError("status_code 必须是整数")
        if not _MIN_STATUS_CODE <= code <= _MAX_STATUS_CODE:
            raise ValueError("status_code 必须落在 400-599")
        self.status_code = code

    def __str__(self) -> str:
        return self.message


class CourseTransitionError(CourseError):
    """当前状态不允许该操作（例如 running 时又点开始学习）。"""

    default_status_code = 409
    safe_category = "course_transition"


class CourseNotFound(CourseError):
    """任务或课程资源不存在，或不属于当前用户数据目录。"""

    default_status_code = 404
    safe_category = "course_not_found"


class CourseBusy(CourseError):
    """站点单实例 / 运行目录被占用，稍后可重试。"""

    default_status_code = 409
    safe_category = "course_busy"


class CourseInvalid(CourseError):
    """用户提供的课程任务字段无效。"""

    default_status_code = 422
    safe_category = "course_invalid"


class CourseUnsupported(CourseError):
    """课程站点、学习模式或模型能力当前不受支持。"""

    default_status_code = 422
    safe_category = "course_unsupported"


class CourseAuthRequired(CourseError):
    """登录态缺失或不可用，需要用户重新登录（重试同一个请求不会变好）。

    与 `CourseBusy` 分开：两者都是 409，但前端文案与下一步动作不同——busy 提示
    "稍后再试"，auth_required 必须引导去登录。
    """

    default_status_code = 409
    safe_category = "course_auth_required"


class CourseProtocolError(CourseError):
    """子进程事件流或终局结果不合契约（服务端上游问题，不是用户输入问题）。"""

    default_status_code = 502
    safe_category = "course_protocol"


def safe_course_message(error: CourseError) -> str:
    """保留安全中文文案；错误内容若像栈、路径或凭据则回退为固定提示。"""
    message = error.message.strip() if isinstance(error.message, str) else ""
    unsafe = (
        not message
        or not _HAN_CHAR.search(message)
        or _WINDOWS_PATH.search(message) is not None
        or _POSIX_PRIVATE_PATH.search(message) is not None
        or _SENSITIVE_TEXT.search(message) is not None
    )
    if unsafe:
        for class_name, fallback in _SAFE_FALLBACKS:
            if type(error).__name__ == class_name:
                return fallback
        return "课程操作失败，请稍后重试。"
    if not message.endswith("。"):
        message += "。"
    return message
