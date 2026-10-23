"""Browser-backed answer entry and submission for Changjiang Yuketang."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from core.paths import data_path

SITE_ID = "cjyuketang"
ADAPTER_VERSION = "0.1.0"
SCHEMA_VERSION = "1.0"
CONFIG_PATH = data_path("config.json")
_ALLOWED_COOKIE_DOMAINS = ("yuketang.cn",)
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

_SUBMIT_LABELS = {
    "提交",
    "提交答案",
    "提交答题",
    "提交作业",
    "提交试卷",
    "交卷",
    "确认交卷",
    "完成并提交",
}
_LOGIN_MARKERS = ("请先登录", "请登录后", "登录超时", "登录失效", "重新登录")
_CLOSED_MARKERS = ("作业已截止", "考试已结束", "答题时间已结束", "作业已结束", "不可提交")
_FAILURE_MARKERS = ("提交失败", "答案提交失败", "保存失败", "网络异常", "无法提交")
_SUCCESS_MARKERS = (
    "提交成功",
    "答案提交成功",
    "作业提交成功",
    "交卷成功",
    "提交完成",
    "交卷完成",
)
_RESULT_MARKERS = ("答题完成", "已提交", "提交记录", "查看成绩", "作答结果", "考试完成")


class CjyuketangInteractionError(RuntimeError):
    """A safe-to-display error without page content, URLs, or credentials."""

    def __init__(
        self,
        message: str,
        code: str = "interaction",
        *,
        attempted: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.attempted = attempted


# The runner uses this adapter-neutral name to classify safe pre-submit errors.
InteractionAdapterError = CjyuketangInteractionError


class _PreflightError(RuntimeError):
    """The live page does not safely match the saved package."""


def _require_desktop_submission_confirmation(state: Mapping[str, Any]) -> None:
    """Fail closed unless this run received the WebUI confirmation decision."""
    if (
        state.get("manual_confirmation_requested") is not True
        or state.get("manual_confirmation_accepted") is not True
    ):
        raise _PreflightError("WebUI submission confirmation was not accepted")


def _report_progress(
    callback: Callable[..., None] | None,
    state: str,
    *,
    question_count: int | None = None,
    completed_count: int | None = None,
) -> None:
    if callback is None:
        return
    try:
        callback(state, question_count=question_count, completed_count=completed_count)
    except Exception:
        pass


def _is_yuketang_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        _ = parsed.port
    except ValueError:
        return False
    allowed_host = any(
        host == domain or host.endswith(f".{domain}")
        for domain in _ALLOWED_COOKIE_DOMAINS
    )
    return (
        parsed.scheme.lower() == "https"
        and parsed.username is None
        and parsed.password is None
        and allowed_host
    )


def _parse_cookie_header(value: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    if not value.strip():
        return cookies
    for part in value.split(";"):
        name, separator, cookie_value = part.strip().partition("=")
        if (
            not separator
            or not name
            or any(character.isspace() or character in "=," for character in name)
            or any(character in cookie_value for character in "\r\n;")
        ):
            raise CjyuketangInteractionError(
                "cjyuketang.cookie 格式无效；请检查本地配置。",
                "config_cookie_invalid",
            )
        previous = cookies.get(name)
        if previous is not None and previous != cookie_value:
            raise CjyuketangInteractionError(
                "cjyuketang.cookie 中存在重复且冲突的 Cookie 名称。",
                "config_cookie_ambiguous",
            )
        cookies[name] = cookie_value
    return cookies


def _load_cjyuketang_config() -> tuple[str, dict[str, str]]:
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise CjyuketangInteractionError(
            "用户数据目录缺少 config.json；请先配置雨课堂页面和 Cookie。",
            "config_missing",
        ) from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise CjyuketangInteractionError(
            "config.json 无法读取或不是有效 JSON；修复后再运行。",
            "config_invalid",
        ) from None
    if not isinstance(config, dict):
        raise CjyuketangInteractionError(
            "config.json 顶层必须是 JSON 对象。",
            "config_shape",
        )
    section = config.get(SITE_ID)
    if not isinstance(section, dict):
        raise CjyuketangInteractionError(
            "config.json 中缺少 cjyuketang 配置对象。",
            "config_section_missing",
        )
    url = section.get("url")
    cookie_header = section.get("cookie", "")
    if not isinstance(url, str) or not url.strip():
        raise CjyuketangInteractionError(
            "请在 config.json 的 cjyuketang.url 中填写当前答题页地址。",
            "config_url_missing",
        )
    if not isinstance(cookie_header, str):
        raise CjyuketangInteractionError(
            "config.json 中的 cjyuketang.cookie 必须是字符串。",
            "config_cookie_invalid",
        )
    url = url.strip()
    if not _is_yuketang_url(url):
        raise CjyuketangInteractionError(
            "cjyuketang.url 必须是 HTTPS 的 yuketang.cn 页面。",
            "config_url_invalid",
        )
    return url, _parse_cookie_header(cookie_header)


def _browser_cookies(url: str, cookies: Mapping[str, str]) -> list[dict[str, Any]]:
    host = urlsplit(url).hostname
    if cookies and (not host or not _is_yuketang_url(url)):
        raise CjyuketangInteractionError(
            "雨课堂 Cookie 只能用于 HTTPS 的 yuketang.cn 及其子域名。",
            "config_url_invalid",
        )
    result: list[dict[str, Any]] = []
    for name, value in cookies.items():
        cookie: dict[str, Any] = {
            "name": name,
            "value": value,
            "path": "/",
            "secure": True,
        }
        if name.startswith("__Host-"):
            cookie["url"] = f"https://{host}/"
        else:
            cookie["domain"] = ".yuketang.cn"
        result.append(cookie)
    return result


def validate_interaction_against_questions(
    questions_package: dict[str, Any], interaction_package: dict[str, Any]
) -> list[str]:
    """Check the saved page controls cover exactly the packaged questions."""
    issues: list[str] = []
    questions = questions_package.get("questions", [])
    question_controls = interaction_package.get("question_controls", [])
    page_meta = interaction_package.get("page")
    if not isinstance(page_meta, dict) or not _valid_selector(page_meta.get("selector")):
        issues.append("interaction page selector is missing or invalid")

    questions_by_id = {
        question.get("question_id"): question
        for question in questions
        if isinstance(question, dict) and isinstance(question.get("question_id"), str)
    }
    controls_by_id: dict[str, dict[str, Any]] = {}
    for index, control in enumerate(question_controls):
        if not isinstance(control, dict):
            issues.append(f"question_controls[{index}] is not an object")
            continue
        question_id = control.get("question_id")
        if not isinstance(question_id, str) or question_id not in questions_by_id:
            issues.append(f"question_controls[{index}] references an unknown question_id")
            continue
        if question_id in controls_by_id:
            issues.append(f"question {question_id} has duplicate interaction controls")
            continue
        controls_by_id[question_id] = control
    if set(controls_by_id) != set(questions_by_id):
        issues.append("interaction controls do not cover exactly the question IDs")

    selectors: set[str] = set()
    page_selector = page_meta.get("selector") if isinstance(page_meta, dict) else None
    if isinstance(page_selector, str):
        selectors.add(page_selector)
    submit_selector = page_meta.get("submit_selector") if isinstance(page_meta, dict) else None
    if submit_selector is not None:
        if not _valid_selector(submit_selector):
            issues.append("interaction submit selector is invalid")
        elif submit_selector in selectors:
            issues.append("interaction selectors are reused")
        else:
            selectors.add(submit_selector)

    for question_id, question in questions_by_id.items():
        control = controls_by_id.get(question_id)
        if control is None:
            continue
        if control.get("question_type") != question.get("question_type"):
            issues.append(f"question {question_id} has a question type mismatch")
        question_selector = control.get("question_selector")
        if not _valid_selector(question_selector):
            issues.append(f"question {question_id} has an invalid question selector")
        elif question_selector in selectors:
            issues.append("interaction selectors are reused")
        else:
            selectors.add(question_selector)

        options = question.get("options", [])
        expected_options = {
            item.get("option_id") for item in options if isinstance(item, dict)
        }
        choice_controls = control.get("choice_options", [])
        actual_option_ids: list[Any] = []
        for option_control in choice_controls:
            if not isinstance(option_control, dict):
                issues.append(f"question {question_id} has an invalid choice control")
                continue
            option_id = option_control.get("option_id")
            actual_option_ids.append(option_id)
            selector = option_control.get("selector")
            if not _valid_selector(selector):
                issues.append(f"question {question_id} has an invalid choice selector")
            elif selector in selectors:
                issues.append("interaction selectors are reused")
            else:
                selectors.add(selector)
        if (
            len(actual_option_ids) != len(set(actual_option_ids))
            or set(actual_option_ids) != expected_options
        ):
            issues.append(f"question {question_id} choice controls do not match its options")

        slots = {
            slot.get("slot_id"): slot
            for slot in question.get("answer_slots", [])
            if isinstance(slot, dict)
        }
        slot_controls = control.get("answer_slots", [])
        actual_slot_ids: list[Any] = []
        for slot_control in slot_controls:
            if not isinstance(slot_control, dict):
                issues.append(f"question {question_id} has an invalid answer control")
                continue
            slot_id = slot_control.get("slot_id")
            actual_slot_ids.append(slot_id)
            slot = slots.get(slot_id)
            if slot is None or slot_control.get("control_type") != slot.get("input_kind"):
                issues.append(f"question {question_id} has an answer control type mismatch")
            selector = slot_control.get("selector")
            if not _valid_selector(selector):
                issues.append(f"question {question_id} has an invalid answer selector")
            elif selector in selectors:
                issues.append("interaction selectors are reused")
            else:
                selectors.add(selector)
        if (
            len(actual_slot_ids) != len(set(actual_slot_ids))
            or set(actual_slot_ids) != set(slots)
        ):
            issues.append(f"question {question_id} answer controls do not match its slots")

        question_type = question.get("question_type")
        if question_type == "single_choice":
            selected = control.get("selected_option_id")
            if selected is not None and selected not in expected_options:
                issues.append(f"question {question_id} has an unknown selected option")
        elif question_type == "multiple_choice":
            selected = control.get("selected_option_ids", [])
            if len(selected) != len(set(selected)) or not set(selected) <= expected_options:
                issues.append(f"question {question_id} has unknown selected options")
    return issues


def _valid_selector(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and not any(ord(character) < 32 for character in value)
        and not value.lstrip().lower().startswith("javascript:")
    )


def _one_locator(page: Any, selector: str) -> Any:
    if not _valid_selector(selector):
        raise _PreflightError("页面选择器缺失或无效")
    try:
        locator = page.locator(selector)
        if locator.count() != 1:
            raise _PreflightError("页面选择器没有唯一匹配项")
        return locator
    except _PreflightError:
        raise
    except Exception:
        raise _PreflightError("页面选择器无法解析") from None


def _is_clickable(locator: Any) -> bool:
    try:
        return bool(
            locator.is_visible()
            and locator.is_enabled()
            and locator.evaluate(
                """el => el.getAttribute('aria-disabled') !== 'true' &&
                    !el.classList.contains('is-disabled') &&
                    !el.classList.contains('disabled') &&
                    !el.querySelector(':disabled, [aria-disabled="true"]')"""
            )
        )
    except Exception:
        return False


def _click_choice_option(locator: Any) -> None:
    """Click the radio/checkbox control itself through the DOM.

    Element-UI options frequently wrap `<img>`; a pointer click targets the label
    centre, can land on that image, and gets swallowed by its preview/zoom handler,
    leaving the option unselected. A DOM click on the inner input ignores hit
    testing, so it always toggles the control without touching the image.
    """
    try:
        locator.evaluate(
            """el => {
                const box = el.querySelector('input[type=radio], input[type=checkbox]');
                (box || el).click();
            }"""
        )
    except Exception:
        raise _PreflightError("选项控件无法点击") from None


def _wait_choice_state(locator: Any, expected: bool, timeout_seconds: float = 1.0) -> bool:
    """Wait briefly for the framework to settle the option state after a click."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        if _choice_selected(locator) == expected:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _choice_selected(locator: Any) -> bool:
    try:
        return bool(
            locator.evaluate(
                """el => {
                    const nodes = [el, ...el.querySelectorAll(
                        'input[type=radio], input[type=checkbox], [role=radio], [role=checkbox]'
                    )];
                    return nodes.some(node => {
                        const classes = new Set((node.getAttribute('class') || '').toLowerCase().split(/\\s+/));
                        return node.checked === true ||
                            node.getAttribute('aria-checked') === 'true' ||
                            node.getAttribute('aria-selected') === 'true' ||
                            ['is-checked', 'selected', 'checked', 'active', 'current'].some(name => classes.has(name));
                    });
                }"""
            )
        )
    except Exception:
        raise _PreflightError("选项状态无法读取") from None


def _read_control_value(locator: Any) -> str:
    try:
        return str(
            locator.evaluate(
                """el => {
                    const tag = el.tagName.toLowerCase();
                    if (tag === 'input' || tag === 'textarea') return el.value || '';
                    return el.innerText || el.textContent || '';
                }"""
            )
        )
    except Exception:
        raise _PreflightError("答题控件当前值无法读取") from None


def _snapshot_text(value: str) -> str:
    return " ".join(value.split()).strip()


def _editor_target(page: Any, slot_control: Any) -> dict[str, Any] | None:
    """Resolve a rich-text editor whose editable body lives inside an iframe."""
    frame_selector = slot_control.get("frame_selector")
    if not isinstance(frame_selector, str) or not frame_selector:
        return None
    iframe = _one_locator(page, frame_selector)
    try:
        frame = iframe.content_frame
    except Exception:
        raise _PreflightError("富文本编辑器的 iframe 无法访问") from None
    if frame is None:
        raise _PreflightError("富文本编辑器的 iframe 无法访问")
    try:
        body = frame.locator("body")
        deadline = time.monotonic() + 5.0
        while True:
            if body.count() == 1:
                break
            if time.monotonic() >= deadline:
                raise _PreflightError("富文本编辑器没有唯一的可编辑区域")
            time.sleep(0.2)
        candidates = frame.locator(
            "[contenteditable='true'], [contenteditable=''], [contenteditable='plaintext-only']"
        )
        editable_count = candidates.count()
        if editable_count == 1:
            element = candidates
        elif editable_count == 0:
            if not bool(body.evaluate("el => el.isContentEditable")):
                raise _PreflightError("富文本编辑器没有可编辑区域")
            element = body
        else:
            raise _PreflightError("富文本编辑器存在多个可编辑区域")
    except _PreflightError:
        raise
    except Exception:
        raise _PreflightError("富文本编辑器的 iframe 无法访问") from None
    return {"locator": element, "frame": frame, "iframe": iframe}


def _resolve_text_target(page: Any, slot_control: Any) -> dict[str, Any]:
    editor = _editor_target(page, slot_control)
    if editor is not None:
        return editor
    return {
        "locator": _one_locator(page, slot_control["selector"]),
        "frame": None,
        "iframe": None,
    }


def _read_target_value(target: dict[str, Any]) -> str:
    if target.get("frame") is not None:
        try:
            return str(
                target["locator"].evaluate(
                    "el => el ? (el.innerText || el.textContent || '') : ''"
                )
            )
        except Exception:
            raise _PreflightError("富文本编辑器当前内容无法读取") from None
    return _read_control_value(target["locator"])


def _editor_is_editable(target: dict[str, Any]) -> bool:
    try:
        return bool(
            target["locator"].evaluate(
                "el => !!el && (el.isContentEditable || "
                "el.getAttribute('contenteditable') === 'true')"
            )
        )
    except Exception:
        return False


def _fill_text_target(target: dict[str, Any], value: str) -> None:
    locator = target["locator"]
    if target.get("frame") is not None:
        locator.click()
        locator.fill(value)
        # UEditor keeps its model in sync from events raised inside the iframe.
        try:
            locator.evaluate(
                "el => {"
                " for (const name of ['input', 'change', 'blur']) {"
                "   el.dispatchEvent(new Event(name, {bubbles: true}));"
                " }"
                "}"
            )
        except Exception:
            pass
        return
    locator.fill(value)


def _button_label(locator: Any) -> str:
    try:
        value = locator.evaluate(
            """el => el.getAttribute('aria-label') || el.value ||
                el.innerText || el.getAttribute('title') || ''"""
        )
    except Exception:
        return ""
    return "".join(str(value).split())


def _ensure_safe_submit_target(locator: Any) -> None:
    try:
        target = locator.evaluate(
            """el => ({
                href: el.tagName.toLowerCase() === 'a' ? el.href : '',
                action: el.hasAttribute('formaction')
                    ? new URL(el.getAttribute('formaction'), document.baseURI).href
                    : ((el.form && el.form.action) || '')
            })"""
        )
    except Exception:
        raise _PreflightError("无法校验提交控件的目标页面") from None
    if target.get("href") and not _is_yuketang_url(str(target["href"])):
        raise _PreflightError("提交链接不在允许的雨课堂 HTTPS 域名内")
    if target.get("action") and not _is_yuketang_url(str(target["action"])):
        raise _PreflightError("提交表单目标不在允许的雨课堂 HTTPS 域名内")


def _find_submit_locator(page: Any, page_meta: dict[str, Any]) -> Any:
    saved_selector = page_meta.get("submit_selector")
    if saved_selector:
        locator = _one_locator(page, saved_selector)
        if not _is_clickable(locator) or _button_label(locator) not in _SUBMIT_LABELS:
            raise _PreflightError("保存的交卷控件未通过实时校验")
        _ensure_safe_submit_target(locator)
        return locator

    try:
        candidates = page.locator(
            "button, input[type='submit'], input[type='button'], [role='button'], a[role='button']"
        )
        count = candidates.count()
        matches = [
            candidates.nth(index)
            for index in range(count)
            if candidates.nth(index).is_visible()
            and _button_label(candidates.nth(index)) in _SUBMIT_LABELS
        ]
    except Exception:
        raise _PreflightError("无法检查页面交卷控件") from None
    if len(matches) != 1 or not _is_clickable(matches[0]):
        raise _PreflightError("页面没有唯一且可用的提交或交卷按钮")
    _ensure_safe_submit_target(matches[0])
    return matches[0]


def _build_actions(
    page: Any,
    questions_package: dict[str, Any],
    interaction_package: dict[str, Any],
    answers_package: dict[str, Any],
    *,
    include_submit: bool = True,
) -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]], Any]:
    page_meta = interaction_package["page"]
    content_root = _one_locator(page, page_meta["selector"])
    questions = questions_package["questions"]
    controls_by_id = {
        control["question_id"]: control
        for control in interaction_package["question_controls"]
    }
    answers_by_id = {answer["question_id"]: answer for answer in answers_package["answers"]}
    question_selectors = [
        controls_by_id[question["question_id"]]["question_selector"]
        for question in questions
    ]

    try:
        live_count = page.locator(".exercise-item").count()
        selectors_match_order = page.evaluate(
            """({rootSelector, selectors}) => {
                const root = document.querySelector(rootSelector);
                const live = Array.from(document.querySelectorAll('.exercise-item'));
                const mapped = selectors.map(selector => document.querySelector(selector));
                return !!root && live.length === selectors.length &&
                    mapped.every((node, index) => node && root.contains(node) && node === live[index]);
            }""",
            {"rootSelector": page_meta["selector"], "selectors": question_selectors},
        )
    except Exception:
        live_count = -1
        selectors_match_order = False
    if live_count != len(questions) or not selectors_match_order:
        raise _PreflightError("当前答题页的题目数量或顺序与题包不一致")

    action_list: list[dict[str, Any]] = []
    answer_echo: list[dict[str, Any]] = []
    for question in questions:
        question_id = question["question_id"]
        question_control = controls_by_id[question_id]
        answer = answers_by_id[question_id]
        question_locator = _one_locator(page, question_control["question_selector"])
        try:
            is_question = question_locator.evaluate(
                "el => el.classList.contains('exercise-item')"
            )
            belongs_to_root = content_root.evaluate(
                """(root, selector) => {
                    const question = document.querySelector(selector);
                    return !!question && root.contains(question);
                }""",
                question_control["question_selector"],
            )
        except Exception:
            is_question = False
            belongs_to_root = False
        if not is_question or not belongs_to_root:
            raise _PreflightError("题目选择器没有指向当前答题容器")

        if question["question_type"] in {"single_choice", "multiple_choice"}:
            option_locators: dict[str, Any] = {}
            selected_now: set[str] = set()
            for option_control in question_control["choice_options"]:
                option_id = option_control["option_id"]
                locator = _one_locator(page, option_control["selector"])
                try:
                    belongs = locator.evaluate(
                        """(el, selector) => {
                            const question = document.querySelector(selector);
                            return !!question && question.contains(el);
                        }""",
                        question_control["question_selector"],
                    )
                except Exception:
                    belongs = False
                if not belongs or not _is_clickable(locator):
                    raise _PreflightError("选项控件不属于对应题目或当前不可操作")
                option_locators[option_id] = locator
                if _choice_selected(locator):
                    selected_now.add(option_id)

            if question["question_type"] == "single_choice":
                desired = {answer["value"]}
            else:
                desired = set(answer["values"])
            # The answer package is authoritative. After the full structural
            # preflight above, reconcile any current selection to its target;
            # the captured selection is page state, never a model answer.

            if question["question_type"] == "single_choice":
                desired_option_id = next(iter(desired))
                if selected_now != desired:
                    action_list.append(
                        {
                            "kind": "choice",
                            "question_id": question_id,
                            "option_id": desired_option_id,
                            "locator": option_locators[desired_option_id],
                            "selected": True,
                        }
                    )
            else:
                for option_id, locator in option_locators.items():
                    target = option_id in desired
                    if (option_id in selected_now) != target:
                        action_list.append(
                            {
                                "kind": "choice",
                                "question_id": question_id,
                                "option_id": option_id,
                                "locator": locator,
                                "selected": target,
                            }
                        )
            answer_echo.append(
                {
                    "kind": "choice_group",
                    "question_id": question_id,
                    "option_locators": option_locators,
                    "desired": desired,
                }
            )
            continue

        slots_by_id = {
            slot["slot_id"]: slot for slot in question["answer_slots"]
        }
        controls_by_slot = {
            slot["slot_id"]: slot for slot in question_control["answer_slots"]
        }
        answer_values = (
            {entry["slot_id"]: entry["value"] for entry in answer["values"]}
            if answer["answer_type"] == "fill_blank"
            else {question["answer_slots"][0]["slot_id"]: answer["value"]}
        )
        for slot_id, value in answer_values.items():
            slot = slots_by_id[slot_id]
            slot_control = controls_by_slot[slot_id]
            target = _resolve_text_target(page, slot_control)
            anchor = target["iframe"] if target["iframe"] is not None else target["locator"]
            try:
                belongs = anchor.evaluate(
                    """(el, selector) => {
                        const question = document.querySelector(selector);
                        return !!question && question.contains(el);
                    }""",
                    question_control["question_selector"],
                )
                visible = anchor.is_visible()
            except Exception:
                belongs = False
                visible = False
            if not belongs or not visible:
                raise _PreflightError("文本答题控件不属于对应题目或不可编辑")
            if slot_control["control_type"] != slot["input_kind"]:
                raise _PreflightError("文本控件类型与题包记录不一致")
            if target["frame"] is not None:
                if not _editor_is_editable(target):
                    raise _PreflightError("富文本控件没有唯一可见的编辑区域")
            else:
                try:
                    editable = bool(
                        target["locator"].is_enabled()
                        and target["locator"].is_editable()
                        and target["locator"].evaluate(
                            "el => el.isContentEditable || "
                            "['input', 'textarea'].includes(el.tagName.toLowerCase())"
                        )
                    )
                except Exception:
                    editable = False
                if not editable:
                    raise _PreflightError("文本答题控件不属于对应题目或不可编辑")
            current = _snapshot_text(_read_target_value(target))
            target_text = _snapshot_text(value)
            # Existing page text is not an answer source. Keep the target if
            # it is already present; otherwise fill it from answers.json.
            if current != target_text:
                action_list.append(
                    {
                        "kind": "text",
                        "question_id": question_id,
                        "slot_id": slot_id,
                        "target": target,
                        "value": value,
                    }
                )
            answer_echo.append(
                {
                    "kind": "text",
                    "question_id": question_id,
                    "slot_id": slot_id,
                    "target": target,
                    "value": value,
                }
            )

    submit_locator = _find_submit_locator(page, page_meta) if include_submit else None
    return content_root, action_list, answer_echo, submit_locator


def _wait_for_question_list(page: Any, page_selector: str, expected_count: int) -> None:
    """Wait for the SPA to finish rendering before using captured selectors."""
    if not _valid_selector(page_selector):
        raise _PreflightError("答题页容器选择器缺失或无效")
    try:
        page.wait_for_function(
            """({rootSelector, expectedCount}) => {
                let root;
                try { root = document.querySelector(rootSelector); }
                catch (_) { return false; }
                return !!root &&
                    root.querySelectorAll('.exercise-item').length === expectedCount;
            }""",
            arg={"rootSelector": page_selector, "expectedCount": expected_count},
            polling=250,
            timeout=20_000,
        )
        # Vue can expose the root before its option and input nodes are hydrated.
        page.wait_for_timeout(900)
    except Exception:
        raise _PreflightError("答题页题目容器尚未完整加载") from None


def _show_status_panel(page: Any, title_text: str, message_text: str) -> None:
    """Show a safe, non-submitting status notice in the visible page."""
    try:
        page.evaluate(
            """({titleText, messageText}) => {
                const oldPanel = document.getElementById('autolearn-cjyuketang-status');
                if (oldPanel) oldPanel.remove();
                const panel = document.createElement('section');
                panel.id = 'autolearn-cjyuketang-status';
                panel.setAttribute('role', 'status');
                panel.style.cssText = [
                    'position:fixed', 'top:16px', 'right:16px', 'z-index:2147483647',
                    'width:min(420px,calc(100vw - 32px))', 'box-sizing:border-box',
                    'padding:18px', 'border:1px solid #cbd5e1', 'border-radius:12px',
                    'background:#fff', 'color:#111827', 'box-shadow:0 12px 40px rgba(0,0,0,.28)',
                    'font:14px/1.5 system-ui,sans-serif'
                ].join(';');
                const title = document.createElement('strong');
                title.textContent = titleText;
                title.style.cssText = 'display:block;font-size:17px;margin-bottom:8px;padding-right:32px';
                const message = document.createElement('p');
                message.textContent = messageText;
                message.style.cssText = 'margin:0';
                const closeButton = document.createElement('button');
                closeButton.type = 'button';
                closeButton.textContent = '×';
                closeButton.setAttribute('aria-label', '关闭提示');
                closeButton.title = '关闭提示';
                closeButton.style.cssText = [
                    'position:absolute', 'top:10px', 'right:10px', 'width:30px', 'height:30px',
                    'display:grid', 'place-items:center', 'padding:0', 'border:0',
                    'border-radius:6px', 'background:transparent', 'color:#475569',
                    'font:24px/1 system-ui,sans-serif', 'cursor:pointer'
                ].join(';');
                closeButton.addEventListener('click', () => panel.remove());
                panel.append(title, message, closeButton);
                document.body.append(panel);
            }""",
            {"titleText": title_text, "messageText": message_text},
        )
    except Exception:
        return


def _hold_visible_browser_until_closed(page: Any) -> None:
    """Keep an unconfirmed visible run open until the user closes its window."""
    try:
        if not page.is_closed():
            page.wait_for_event("close", timeout=0)
    except Exception:
        return


def _choice_group_matches(entry: dict[str, Any]) -> bool:
    selected = {
        option_id
        for option_id, locator in entry["option_locators"].items()
        if _choice_selected(locator)
    }
    return selected == entry["desired"]


def _echo_matches(entries: list[dict[str, Any]]) -> bool:
    try:
        for entry in entries:
            if entry["kind"] == "choice_group":
                if not _choice_group_matches(entry):
                    return False
            elif _snapshot_text(_read_target_value(entry["target"])) != _snapshot_text(entry["value"]):
                return False
        return True
    except Exception:
        return False


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _skipped_items(question_ids: list[str], reason: str) -> list[dict[str, Any]]:
    return [
        {"question_id": question_id, "status": "skipped", "attempted": False, "reason": reason}
        for question_id in question_ids
    ]


def _filled_unsubmitted_items(
    question_ids: list[str],
    *,
    possible_autosave: bool = False,
    reason: str | None = None,
) -> list[dict[str, Any]]:
    reason = reason or "所有答题控件已填写并通过页面回显核对；没有调用网站提交按钮。"
    if possible_autosave:
        reason += "填写期间观察到可能的自动保存请求。"
    return [
        {
            "question_id": question_id,
            "status": "filled_unsubmitted",
            "attempted": False,
            "reason": reason,
        }
        for question_id in question_ids
    ]


def _unconfirmed_items(question_ids: list[str], reason: str) -> list[dict[str, Any]]:
    return [
        {"question_id": question_id, "status": "unconfirmed", "attempted": True, "reason": reason}
        for question_id in question_ids
    ]


def _confirmed_items(
    question_ids: list[str], method: str, detail: str
) -> list[dict[str, Any]]:
    observed_at = _iso_now()
    return [
        {
            "question_id": question_id,
            "status": "confirmed",
            "attempted": True,
            "confirmation": {"method": method, "observed_at": observed_at, "detail": detail},
        }
        for question_id in question_ids
    ]


def _failed_items(question_ids: list[str], code: str, message: str) -> list[dict[str, Any]]:
    return [
        {
            "question_id": question_id,
            "status": "failed",
            "attempted": True,
            "error": {"code": code, "message": message},
        }
        for question_id in question_ids
    ]


def _marker_snapshot(value: str, markers: tuple[str, ...]) -> set[str]:
    folded = value.casefold()
    return {marker for marker in markers if marker.casefold() in folded}


def _page_text(page: Any) -> str:
    try:
        return str(page.locator("body").inner_text(timeout=1500))
    except Exception:
        return ""


def _new_marker(
    current_text: str,
    baseline: set[str],
    markers: tuple[str, ...],
    dialog_markers: set[str],
) -> str | None:
    current = _marker_snapshot(current_text, markers)
    newly_visible = current - baseline
    for marker in markers:
        if marker in newly_visible or marker in dialog_markers:
            return marker
    return None


def _post_submission_outcome(state: dict[str, Any], page: Any) -> str | None:
    text = _page_text(page)
    dialog_markers = state["dialog_markers"]
    if _new_marker(text, state["baseline_login"], _LOGIN_MARKERS, dialog_markers):
        return "login_required"
    if _new_marker(text, state["baseline_closed"], _CLOSED_MARKERS, dialog_markers):
        return "submission_blocked"
    if _new_marker(text, state["baseline_failure"], _FAILURE_MARKERS, dialog_markers):
        return "submission_rejected"
    if _new_marker(text, state["baseline_success"], _SUCCESS_MARKERS, dialog_markers):
        return "success_message"
    try:
        final_path = urlsplit(str(page.url)).path
    except ValueError:
        final_path = ""
    if (
        final_path
        and final_path != state["initial_path"]
        and _new_marker(text, state["baseline_result"], _RESULT_MARKERS, dialog_markers)
    ):
        return "result_page"
    if (
        any(200 <= response["status"] < 300 for response in state["submit_responses"])
        and _echo_matches(state["answer_echo"])
    ):
        return "answer_echo"
    return None


def _run_browser(
    url: str,
    cookies: Mapping[str, str],
    questions_package: dict[str, Any],
    interaction_package: dict[str, Any],
    answers_package: dict[str, Any],
    *,
    fill_only: bool = False,
    cancel_event: threading.Event | None = None,
    progress_callback: Callable[..., None] | None = None,
    submit_confirmation_callback: Callable[[int], bool] | None = None,
) -> list[dict[str, Any]]:
    if not fill_only and not callable(submit_confirmation_callback):
        raise CjyuketangInteractionError(
            "正常提交必须由 AutoLearn 桌面确认流程发起。",
            "desktop_confirmation_required",
        )
    question_ids = [question["question_id"] for question in questions_package["questions"]]
    if cancel_event is not None and cancel_event.is_set():
        return _skipped_items(question_ids, "操作在浏览器交互开始前已取消。")
    state: dict[str, Any] = {
        "page_action_called": False,
        "preflight_complete": False,
        "preflight_error": None,
        "edit_error": False,
        "edit_error_detail": "",
        "filled": False,
        "editing_started": False,
        "submit_attempted": False,
        "manual_confirmation_requested": False,
        "manual_confirmation_accepted": False,
        "manual_cancelled": False,
        "mutations_during_edit": False,
        "submit_request_ids": set(),
        "submit_responses": [],
        "dialog_markers": set(),
        "answer_echo": [],
        "outcome": None,
    }
    try:
        from scrapling.fetchers import DynamicFetcher
    except ImportError:
        raise CjyuketangInteractionError(
            "当前 Python 环境缺少 Scrapling 浏览器组件；请在运行项目的同一解释器安装 scrapling[fetchers] 并运行 scrapling install。",
            "browser_dependency",
        ) from None

    browser_cookies = _browser_cookies(url, cookies)

    def page_setup(page: Any) -> None:
        if browser_cookies:
            page.context.add_cookies(browser_cookies)

        def track_request(request: Any) -> None:
            try:
                if not _is_yuketang_url(str(request.url)):
                    return
                method = str(request.method).upper()
                if state["submit_attempted"]:
                    if method in _MUTATING_METHODS or request.is_navigation_request():
                        state["submit_request_ids"].add(id(request))
                elif state["editing_started"] and method in _MUTATING_METHODS:
                    state["mutations_during_edit"] = True
            except Exception:
                return

        def track_response(response: Any) -> None:
            try:
                if id(response.request) not in state["submit_request_ids"]:
                    return
                state["submit_responses"].append(
                    {
                        "status": int(response.status),
                        "main_frame": response.frame == page.main_frame,
                    }
                )
            except Exception:
                return

        def handle_dialog(dialog: Any) -> None:
            try:
                message = str(dialog.message)
                all_markers = (*_LOGIN_MARKERS, *_CLOSED_MARKERS, *_FAILURE_MARKERS, *_SUCCESS_MARKERS)
                state["dialog_markers"].update(_marker_snapshot(message, all_markers))
                if state["submit_attempted"]:
                    dialog.accept()
                else:
                    dialog.dismiss()
            except Exception:
                return

        page.on("request", track_request)
        page.on("response", track_response)
        page.on("dialog", handle_dialog)

    def page_action(page: Any) -> None:
        state["page_action_called"] = True
        try:
            current_url = str(page.url)
            if not _is_yuketang_url(current_url):
                raise _PreflightError("浏览器未停留在雨课堂 HTTPS 页面")
            try:
                state["initial_path"] = urlsplit(current_url).path
            except ValueError:
                state["initial_path"] = ""
            _wait_for_question_list(
                page,
                interaction_package["page"]["selector"],
                len(question_ids),
            )
            _, actions, answer_echo, submit_locator = _build_actions(
                page,
                questions_package,
                interaction_package,
                answers_package,
                include_submit=False,
            )
            state["answer_echo"] = answer_echo
            state["preflight_complete"] = True
            _report_progress(
                progress_callback,
                "preflight_complete",
                question_count=len(question_ids),
            )

            if actions:
                state["editing_started"] = True
            try:
                completed_count = 0
                for action in actions:
                    if cancel_event is not None and cancel_event.is_set():
                        raise RuntimeError("operation cancelled at a safe fill point")
                    if action["kind"] == "choice":
                        locator = action["locator"]
                        _click_choice_option(locator)
                        if not _wait_choice_state(locator, action["selected"]):
                            number = action["question_id"].rsplit("-", 1)[-1].lstrip("0") or action["question_id"]
                            raise _PreflightError(
                                f"第 {number} 题（{action['option_id']}）点击后页面没有按预期选中"
                            )
                    else:
                        _fill_text_target(action["target"], action["value"])
                        if _snapshot_text(_read_target_value(action["target"])) != _snapshot_text(
                            action["value"]
                        ):
                            raise _PreflightError("文本答案未能在页面控件中回显")
                    completed_count += 1
                    _report_progress(
                        progress_callback,
                        "filling_answers",
                        question_count=len(question_ids),
                        completed_count=completed_count,
                    )
                for entry in answer_echo:
                    if entry["kind"] == "choice_group" and not _choice_group_matches(entry):
                        raise _PreflightError("页面选项回显与答案不一致")
                    if entry["kind"] == "text" and _snapshot_text(
                        _read_target_value(entry["target"])
                    ) != _snapshot_text(entry["value"]):
                        raise _PreflightError("页面文本回显与答案不一致")
            except Exception as error:
                state["edit_error"] = True
                state["edit_error_detail"] = str(error).strip()[:200]
                if not fill_only:
                    detail = state["edit_error_detail"]
                    _show_status_panel(
                        page,
                        "答题操作未完成",
                        (
                            f"{detail}。未点击网站提交按钮，页面会保持打开，请检查后手动关闭。"
                            if detail
                            else "填写或回显核对失败，未点击网站提交按钮。页面会保持打开，请检查后手动关闭。"
                        ),
                    )
                    _hold_visible_browser_until_closed(page)
                return

            state["filled"] = True
            _report_progress(
                progress_callback,
                "answers_filled",
                question_count=len(question_ids),
                completed_count=len(question_ids),
            )
            if fill_only:
                _show_status_panel(
                    page,
                    "调试填写完成",
                    "答案已填写并核对，未调用网站提交按钮。页面可能自动保存；窗口保持打开，请检查后手动关闭。",
                )
                _hold_visible_browser_until_closed(page)
                return

            state["manual_confirmation_requested"] = True
            if cancel_event is not None and cancel_event.is_set():
                state["manual_cancelled"] = True
                _show_status_panel(
                    page,
                    "已取消提交",
                    "没有触发网站提交按钮。页面会保持打开，请检查后手动关闭。",
                )
                _hold_visible_browser_until_closed(page)
                return
            try:
                _report_progress(
                    progress_callback,
                    "awaiting_submit_confirmation",
                    question_count=len(question_ids),
                )
                confirmed = submit_confirmation_callback(len(question_ids))
            except Exception:
                state["edit_error"] = True
                _show_status_panel(
                    page,
                    "等待桌面提交确认时中断",
                    "没有触发网站提交按钮。页面会保持打开，请检查后手动关闭。",
                )
                _hold_visible_browser_until_closed(page)
                return
            if confirmed is not True:
                state["manual_cancelled"] = True
                _show_status_panel(
                    page,
                    "已取消提交",
                    "没有触发网站提交按钮。页面会保持打开，请检查后手动关闭。",
                )
                _hold_visible_browser_until_closed(page)
                return
            if cancel_event is not None and cancel_event.is_set():
                state["manual_cancelled"] = True
                _show_status_panel(
                    page,
                    "已取消提交",
                    "没有触发网站提交按钮。页面会保持打开，请检查后手动关闭。",
                )
                _hold_visible_browser_until_closed(page)
                return
            state["manual_confirmation_accepted"] = True

            if not _is_yuketang_url(str(page.url)):
                raise _PreflightError("桌面确认后浏览器已不在雨课堂 HTTPS 页面")
            _, actions, answer_echo, submit_locator = _build_actions(
                page,
                questions_package,
                interaction_package,
                answers_package,
                include_submit=True,
            )
            state["answer_echo"] = answer_echo
            for entry in answer_echo:
                if entry["kind"] == "choice_group" and not _choice_group_matches(entry):
                    raise _PreflightError("桌面确认后选项状态发生变化")
                if entry["kind"] == "text" and _snapshot_text(
                    _read_target_value(entry["target"])
                ) != _snapshot_text(entry["value"]):
                    raise _PreflightError("桌面确认后文本答案发生变化")

            baseline_text = _page_text(page)
            state["baseline_login"] = _marker_snapshot(baseline_text, _LOGIN_MARKERS)
            state["baseline_closed"] = _marker_snapshot(baseline_text, _CLOSED_MARKERS)
            state["baseline_failure"] = _marker_snapshot(baseline_text, _FAILURE_MARKERS)
            state["baseline_success"] = _marker_snapshot(baseline_text, _SUCCESS_MARKERS)
            state["baseline_result"] = _marker_snapshot(baseline_text, _RESULT_MARKERS)

            _require_desktop_submission_confirmation(state)
            _report_progress(
                progress_callback,
                "submitting",
                question_count=len(question_ids),
            )
            if cancel_event is not None and cancel_event.is_set():
                state["manual_cancelled"] = True
                _show_status_panel(
                    page,
                    "已取消提交",
                    "没有触发网站提交按钮。页面会保持打开，请检查后手动关闭。",
                )
                _hold_visible_browser_until_closed(page)
                return
            state["submit_attempted"] = True
            try:
                submit_locator.click()
            except Exception:
                state["outcome"] = "unconfirmed"
            for _ in range(40):
                try:
                    page.wait_for_timeout(250)
                except Exception:
                    break
                outcome = _post_submission_outcome(state, page)
                if outcome:
                    state["outcome"] = outcome
                    break
        except _PreflightError as error:
            state["preflight_error"] = str(error)
            if not fill_only:
                _show_status_panel(
                    page,
                    "页面预检未通过",
                    "未填写或提交答案。页面会保持打开，请检查后手动关闭。",
                )
                _hold_visible_browser_until_closed(page)
        except Exception:
            if state["submit_attempted"]:
                state["outcome"] = "unconfirmed"
            elif state["editing_started"]:
                state["edit_error"] = True
            else:
                state["preflight_error"] = "页面预检遇到无法安全处理的结构"
            if not fill_only and not state["submit_attempted"]:
                _show_status_panel(
                    page,
                    "答题操作未完成",
                    "页面结构或控件操作遇到错误；未触发网站提交按钮。页面会保持打开，请检查后手动关闭。",
                )
                _hold_visible_browser_until_closed(page)

        if not fill_only and state["submit_attempted"] and state.get("outcome") not in {
            "success_message",
            "answer_echo",
            "result_page",
        }:
            _show_status_panel(
                page,
                "提交结果未确认",
                "请检查当前页面状态；浏览器会保持打开，确认后请手动关闭。",
            )
            _hold_visible_browser_until_closed(page)

    previous_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        DynamicFetcher.fetch(
            url,
            page_setup=page_setup,
            page_action=page_action,
            headless=False,
            google_search=False,
            network_idle=False,
            timeout=30_000,
            wait=10_000,
        )
    except Exception:
        if fill_only and state["filled"]:
            return _filled_unsubmitted_items(
                question_ids,
                possible_autosave=state["mutations_during_edit"],
            )
        if state["submit_attempted"]:
            return _unconfirmed_items(
                question_ids,
                "已触发雨课堂提交，但浏览器未返回明确结果；请先检查页面状态再决定是否重试。",
            )
        if state["mutations_during_edit"]:
            detail = state.get("edit_error_detail") or ""
            reason = "填写期间页面可能已自动保存答案；请先检查页面状态再决定是否重试。"
            if detail:
                reason = f"{reason}（{detail}）"
            return _unconfirmed_items(question_ids, reason)
        if state["page_action_called"] and state["preflight_complete"]:
            return _skipped_items(question_ids, "页面交互中断，尚未触发提交按钮。")
        raise CjyuketangInteractionError(
            "雨课堂答题页未能在浏览器中打开；尚未填写答案。",
            "page_request",
        ) from None
    finally:
        logging.disable(previous_disable)

    if not state["page_action_called"]:
        raise CjyuketangInteractionError(
            "浏览器未能打开雨课堂答题页；尚未填写答案。",
            "page_action_missing",
        )
    if state["preflight_error"] or not state["preflight_complete"]:
        if state["submit_attempted"] or state["mutations_during_edit"]:
            return _unconfirmed_items(
                question_ids,
                "页面操作中断且可能已保存答案或已发送请求；请先检查雨课堂页面。",
            )
        if state["filled"]:
            return _filled_unsubmitted_items(
                question_ids,
                reason="答案已填写并核对，但提交控件复核未通过；没有触发网站提交。请先检查页面。",
            )
        detail = state["preflight_error"] or "页面预检没有完成"
        return _skipped_items(question_ids, f"交互预检未通过：{detail}；未填写或提交答案。")
    if fill_only and state["filled"]:
        return _filled_unsubmitted_items(
            question_ids,
            possible_autosave=state["mutations_during_edit"],
        )
    if state["manual_cancelled"]:
        if state["mutations_during_edit"]:
            return _unconfirmed_items(
                question_ids,
                "已取消提交，但页面可能自动保存了部分答案；请先检查雨课堂页面。",
            )
        if state["filled"]:
            return _filled_unsubmitted_items(
                question_ids,
                reason="答案已填写并核对；用户取消了提交，未触发网站提交按钮。",
            )
        return _skipped_items(question_ids, "用户取消提交；未触发页面提交按钮。")
    if state["manual_confirmation_requested"] and not state["manual_confirmation_accepted"]:
        if state["mutations_during_edit"]:
            return _unconfirmed_items(
                question_ids,
                "桌面提交确认中断，且页面可能自动保存了答案；请先检查雨课堂页面。",
            )
        if state["filled"]:
            return _filled_unsubmitted_items(
                question_ids,
                reason="答案已填写并核对；尚未确认提交。请检查页面后手动关闭浏览器。",
            )
        return _skipped_items(question_ids, "未完成桌面提交确认；未触发页面提交按钮。")
    if state["edit_error"]:
        if state["mutations_during_edit"]:
            return _unconfirmed_items(
                question_ids,
                "填写中断且页面可能已自动保存部分答案；请先检查雨课堂页面。",
            )
        return _skipped_items(question_ids, "页面控件填写中断；未触发提交按钮。")
    if not state["submit_attempted"]:
        return _skipped_items(question_ids, "未触发页面提交按钮。")

    outcome = state["outcome"]
    if outcome == "login_required" or any(
        response["status"] in {401, 403} for response in state["submit_responses"]
    ):
        return _failed_items(question_ids, "login_required", "雨课堂拒绝了提交请求；请检查登录状态和作业状态。")
    if outcome == "submission_blocked":
        return _failed_items(question_ids, "submission_blocked", "雨课堂提示当前作业或考试不可提交。")
    if outcome == "submission_rejected":
        return _failed_items(question_ids, "submission_rejected", "雨课堂页面明确提示提交失败。")
    if outcome == "success_message":
        return _confirmed_items(question_ids, "success_message", "提交后页面显示成功提示。")
    if outcome == "result_page":
        return _confirmed_items(question_ids, "result_page", "提交后页面进入答题结果状态。")
    if outcome == "answer_echo":
        return _confirmed_items(question_ids, "answer_echo", "提交请求成功后页面答案回显与本次填写一致。")
    return _unconfirmed_items(
        question_ids,
        "已触发雨课堂提交，但页面没有返回足够的成功或失败证据。",
    )


def execute_answers(
    questions_package: dict[str, Any],
    interaction_package: dict[str, Any],
    answers_package: dict[str, Any],
    *,
    task_id: str | None = None,
    run_id: str | None = None,
    page_url: str | None = None,
    model_profile_id: str | None = None,
    credential_provider: Any | None = None,
    cancel_event: threading.Event | None = None,
    progress_callback: Callable[..., None] | None = None,
    submit_confirmation_callback: Callable[[int], bool] | None = None,
) -> list[dict[str, Any]]:
    """Fill a Rain Classroom package and await WebUI authorization before submit."""
    if not callable(submit_confirmation_callback):
        raise CjyuketangInteractionError(
            "正常提交必须由 AutoLearn 桌面确认流程发起。",
            "desktop_confirmation_required",
        )
    if task_id is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", task_id):
        raise CjyuketangInteractionError("任务 ID 格式无效。", "invalid_task")
    if model_profile_id is not None and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", model_profile_id
    ):
        raise CjyuketangInteractionError("模型档案标识无效。", "invalid_task")
    if run_id is not None and questions_package.get("run_id") != run_id:
        raise CjyuketangInteractionError("任务 run_id 与题包不匹配。", "invalid_package")
    if page_url is None:
        url, cookies = _load_cjyuketang_config()
    else:
        url = page_url
        if not _is_yuketang_url(url):
            raise CjyuketangInteractionError(
                "任务 URL 必须是 HTTPS 的 yuketang.cn 页面。", "config_url_invalid"
            )
        try:
            cookies = (
                credential_provider.get_site_cookies(SITE_ID)
                if credential_provider is not None
                else _load_cjyuketang_config()[1]
            )
        except Exception:
            raise CjyuketangInteractionError(
                "无法安全读取站点登录状态。", "config_cookie_invalid"
            ) from None
    return _run_browser(
        url,
        cookies,
        questions_package,
        interaction_package,
        answers_package,
        cancel_event=cancel_event,
        progress_callback=progress_callback,
        submit_confirmation_callback=submit_confirmation_callback,
    )


def execute_answers_fill_only(
    questions_package: dict[str, Any],
    interaction_package: dict[str, Any],
    answers_package: dict[str, Any],
    *,
    task_id: str | None = None,
    run_id: str | None = None,
    page_url: str | None = None,
    model_profile_id: str | None = None,
    credential_provider: Any | None = None,
    cancel_event: threading.Event | None = None,
    progress_callback: Callable[..., None] | None = None,
    submit_confirmation_callback: Callable[[int], bool] | None = None,
) -> list[dict[str, Any]]:
    """Exercise controls in a visible browser without locating or invoking submit."""
    if task_id is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", task_id):
        raise CjyuketangInteractionError("任务 ID 格式无效。", "invalid_task")
    if model_profile_id is not None and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", model_profile_id
    ):
        raise CjyuketangInteractionError("模型档案标识无效。", "invalid_task")
    if run_id is not None and questions_package.get("run_id") != run_id:
        raise CjyuketangInteractionError("任务 run_id 与题包不匹配。", "invalid_package")
    if page_url is None:
        url, cookies = _load_cjyuketang_config()
    else:
        url = page_url
        if not _is_yuketang_url(url):
            raise CjyuketangInteractionError(
                "任务 URL 必须是 HTTPS 的 yuketang.cn 页面。", "config_url_invalid"
            )
        try:
            cookies = (
                credential_provider.get_site_cookies(SITE_ID)
                if credential_provider is not None
                else _load_cjyuketang_config()[1]
            )
        except Exception:
            raise CjyuketangInteractionError(
                "无法安全读取站点登录状态。", "config_cookie_invalid"
            ) from None
    return _run_browser(
        url,
        cookies,
        questions_package,
        interaction_package,
        answers_package,
        fill_only=True,
        cancel_event=cancel_event,
        progress_callback=progress_callback,
    )
