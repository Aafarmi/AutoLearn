"""Browser-backed answer entry and submission for Chaoxing assignments."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from core.paths import data_path

SITE_ID = "chaoxing"
ADAPTER_VERSION = "0.1.0"
SCHEMA_VERSION = "1.0"
CONFIG_PATH = data_path("config.json")

_CHAoxing_HOST = re.compile(r"(?:^|\.)chaoxing\.com$", re.IGNORECASE)
_SUCCESS_MARKERS = (
    "提交成功",
    "作业提交成功",
    "答案提交成功",
    "交卷成功",
    "提交完成",
    "successfully submitted",
    "submission successful",
)
_FAILURE_MARKERS = (
    "提交失败",
    "答案提交失败",
    "保存失败",
    "提交未成功",
    "无法提交",
    "网络异常",
)
_LOGIN_MARKERS = ("请先登录", "请登录后", "登录超时", "登录失效", "重新登录")
_CLOSED_MARKERS = ("作业已截止", "作业已结束", "不在作答时间", "作业已关闭")
_RESULT_PAGE_MARKERS = ("答题完成", "已提交", "成绩", "得分", "查看答案")


class ChaoxingInteractionError(RuntimeError):
    """A safe-to-display adapter error that never includes page or cookie data."""

    def __init__(self, message: str, code: str = "interaction") -> None:
        super().__init__(message)
        self.code = code


class _PreflightError(RuntimeError):
    """The current page does not match the saved interaction metadata."""


def _require_desktop_submission_confirmation(state: Mapping[str, Any]) -> None:
    """Fail closed unless this run received the WebUI confirmation decision."""
    if (
        state.get("manual_confirmation_requested") is not True
        or state.get("manual_confirmation_accepted") is not True
    ):
        raise _PreflightError("WebUI submission confirmation was not accepted")


def _show_status_panel(page: Any, title_text: str, message_text: str) -> None:
    """Show a safe status notice without interacting with the site's form."""
    try:
        page.evaluate(
            """({titleText, messageText}) => {
                const oldPanel = document.getElementById('autolearn-chaoxing-status');
                if (oldPanel) oldPanel.remove();
                const panel = document.createElement('section');
                panel.id = 'autolearn-chaoxing-status';
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


def validate_interaction_against_questions(
    questions_package: dict[str, Any], interaction_package: dict[str, Any]
) -> list[str]:
    """Validate control-to-question references that JSON Schema cannot express."""
    issues: list[str] = []
    questions = questions_package.get("questions", [])
    question_controls = interaction_package.get("question_controls", [])
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

    used_question_selectors: set[str] = set()
    used_control_selectors: set[str] = set()
    for question_id, question in questions_by_id.items():
        control = controls_by_id.get(question_id)
        if control is None:
            continue
        question_selector = control.get("question_selector")
        if isinstance(question_selector, str):
            if question_selector in used_question_selectors:
                issues.append("question controls reuse a question selector")
            used_question_selectors.add(question_selector)
        expected_options = {
            option.get("option_id")
            for option in question.get("options", [])
            if isinstance(option, dict)
        }
        actual_options = [
            option.get("option_id")
            for option in control.get("choice_options", [])
            if isinstance(option, dict)
        ]
        if len(actual_options) != len(set(actual_options)) or set(actual_options) != expected_options:
            issues.append(f"question {question_id} choice controls do not match its options")
        selectors: list[str] = []
        for option in control.get("choice_options", []):
            if isinstance(option, dict) and isinstance(option.get("selector"), str):
                selectors.append(option["selector"])
        selected_option_id = control.get("selected_option_id")
        if selected_option_id is not None and selected_option_id not in expected_options:
            issues.append(f"question {question_id} has an unknown selected_option_id")

        expected_slots = {
            slot.get("slot_id"): slot
            for slot in question.get("answer_slots", [])
            if isinstance(slot, dict)
        }
        actual_slot_controls = [
            slot
            for slot in control.get("answer_slots", [])
            if isinstance(slot, dict)
        ]
        actual_slot_ids = [slot.get("slot_id") for slot in actual_slot_controls]
        if len(actual_slot_ids) != len(set(actual_slot_ids)) or set(actual_slot_ids) != set(expected_slots):
            issues.append(f"question {question_id} answer controls do not match its answer slots")
        for slot_control in actual_slot_controls:
            slot_id = slot_control.get("slot_id")
            expected_slot = expected_slots.get(slot_id)
            if expected_slot and slot_control.get("control_type") != expected_slot.get("input_kind"):
                issues.append(f"question {question_id} has a control type mismatch")
            selector = slot_control.get("selector")
            if isinstance(selector, str):
                selectors.append(selector)
        if len(selectors) != len(set(selectors)):
            issues.append(f"question {question_id} reuses a page control selector")
        for selector in selectors:
            if selector in used_control_selectors:
                issues.append("question controls reuse a page control selector")
            used_control_selectors.add(selector)

    form = interaction_package.get("form")
    if not isinstance(form, dict) or form.get("selector") != "#submitForm":
        issues.append("interaction metadata must target the #submitForm form")
    return issues


def _is_chaoxing_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
    except ValueError:
        return False
    return (
        parsed.scheme.lower() == "https"
        and parsed.username is None
        and parsed.password is None
        and bool(_CHAoxing_HOST.search(host))
    )


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
        callback(
            state,
            question_count=question_count,
            completed_count=completed_count,
        )
    except Exception:
        pass


def _parse_cookie_header(value: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in value.split(";"):
        name, separator, cookie_value = part.strip().partition("=")
        if separator and name:
            cookies[name] = cookie_value
    return cookies


def _write_config(config: dict[str, Any]) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=CONFIG_PATH.parent,
            prefix=".config.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary_path, CONFIG_PATH)
    except OSError:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise ChaoxingInteractionError(
            "无法创建或补齐用户数据目录的 config.json；请检查目录写入权限。",
            "config_write",
        ) from None


def _load_chaoxing_config() -> tuple[str, dict[str, str]]:
    if CONFIG_PATH.exists():
        try:
            config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise ChaoxingInteractionError(
                "config.json 无法读取或不是有效 JSON；修复配置文件后重试。",
                "config_read",
            ) from None
        if not isinstance(config, dict):
            raise ChaoxingInteractionError(
                "config.json 顶层必须是 JSON 对象；现有文件未修改。",
                "config_shape",
            )
    else:
        config = {}

    changed = False
    if "chaoxing" not in config:
        config["chaoxing"] = {}
        changed = True
    section = config["chaoxing"]
    if not isinstance(section, dict):
        raise ChaoxingInteractionError(
            "config.json 中的 chaoxing 必须是 JSON 对象；现有文件未修改。",
            "config_shape",
        )
    for key in ("url", "cookie"):
        if key not in section:
            section[key] = ""
            changed = True
        if not isinstance(section[key], str):
            raise ChaoxingInteractionError(
                f"config.json 中的 chaoxing.{key} 必须是字符串；现有文件未修改。",
                "config_shape",
            )
    if changed:
        _write_config(config)

    url = section["url"].strip()
    if not url:
        raise ChaoxingInteractionError(
            f"请在 {CONFIG_PATH} 的 chaoxing.url 中填写作业页面 URL 后重试。",
            "config_url_missing",
        )
    if not _is_chaoxing_url(url):
        raise ChaoxingInteractionError(
            "chaoxing.url 必须是 HTTPS 的 chaoxing.com 页面。",
            "config_url_invalid",
        )
    return url, _parse_cookie_header(section["cookie"].strip())


def _browser_cookies(cookies: Mapping[str, str]) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "value": value,
            "domain": ".chaoxing.com",
            "path": "/",
            "secure": True,
        }
        for name, value in cookies.items()
    ]


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _skipped_items(question_ids: list[str], reason: str) -> list[dict[str, Any]]:
    return [
        {
            "question_id": question_id,
            "status": "skipped",
            "attempted": False,
            "reason": reason,
        }
        for question_id in question_ids
    ]


def _filled_unsubmitted_items(
    question_ids: list[str],
    *,
    possible_autosave: bool = False,
    reason: str | None = None,
) -> list[dict[str, Any]]:
    reason = reason or "所有答题控件已填写并通过页面回显核对；没有调用提交按钮。"
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
        {
            "question_id": question_id,
            "status": "unconfirmed",
            "attempted": True,
            "reason": reason,
        }
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
            "confirmation": {
                "method": method,
                "observed_at": observed_at,
                "detail": detail,
            },
        }
        for question_id in question_ids
    ]


def _failed_items(
    question_ids: list[str], code: str, message: str
) -> list[dict[str, Any]]:
    return [
        {
            "question_id": question_id,
            "status": "failed",
            "attempted": True,
            "error": {"code": code, "message": message},
        }
        for question_id in question_ids
    ]


def _normal_text(value: str) -> str:
    return " ".join(value.split()).strip()


def _read_answer_text(locator: Any, control_type: str) -> str:
    if control_type == "text_input":
        try:
            return locator.input_value()
        except Exception:
            return locator.inner_text()
    try:
        return locator.inner_text()
    except Exception:
        return locator.input_value()


def _marker_appeared_after_submit(
    current_text: str,
    baseline_text: str | None,
    new_dialog_text: str,
    markers: tuple[str, ...],
) -> bool:
    current_lower = current_text.casefold()
    baseline_lower = baseline_text.casefold() if baseline_text is not None else ""
    dialog_lower = new_dialog_text.casefold()
    return any(
        (
            baseline_text is not None
            and marker.casefold() in current_lower
            and marker.casefold() not in baseline_lower
        )
        or marker.casefold() in dialog_lower
        for marker in markers
    )


def _one_locator(page: Any, selector: str) -> Any:
    if not isinstance(selector, str) or not selector.strip():
        raise _PreflightError("missing selector")
    locator = page.locator(selector)
    if locator.count() != 1:
        raise _PreflightError("selector did not identify exactly one element")
    return locator


def _stable_question_id(raw_id: str, number: int, used: set[str]) -> str:
    candidate = re.sub(r"[^A-Za-z0-9._:-]+", "-", raw_id).strip("-._:")
    if not candidate:
        candidate = f"question-{number:03d}"
    if not candidate[0].isalnum():
        candidate = f"question-{candidate}"
    candidate = candidate[:128]
    base = candidate
    suffix = 2
    while candidate in used:
        suffix_text = f"-{suffix}"
        candidate = f"{base[:128 - len(suffix_text)]}{suffix_text}"
        suffix += 1
    used.add(candidate)
    return candidate


def _selectors_resolve_to_distinct_elements(page: Any, selectors: list[str]) -> bool:
    try:
        return bool(
            page.evaluate(
                """selectors => {
                    const elements = selectors.map(selector => document.querySelector(selector));
                    return elements.every(Boolean) && new Set(elements).size === elements.length;
                }""",
                selectors,
            )
        )
    except Exception:
        return False


def _is_selected(locator: Any) -> bool:
    return bool(
        locator.evaluate(
            """el => {
                const classes = new Set(Array.from(el.classList || [], name => name.toLowerCase()));
                return el.checked === true || el.getAttribute('aria-checked') === 'true' ||
                    el.getAttribute('aria-pressed') === 'true' ||
                    !!el.querySelector('input[type=radio]:checked, input[type=checkbox]:checked') ||
                    ['selected', 'checked', 'active', 'current'].some(name => classes.has(name));
            }"""
        )
    )


def _ensure_control_belongs_to_question(
    locator: Any, question_selector: str
) -> None:
    try:
        belongs = locator.evaluate(
            """(el, selector) => {
                const form = document.querySelector('#submitForm');
                const question = document.querySelector(selector);
                return !!form && !!question && form.contains(question) &&
                    question.contains(el) && form.contains(el);
            }""",
            question_selector,
        )
    except Exception:
        belongs = False
    if not belongs:
        raise _PreflightError("answer control is outside its question or submitForm")


def _rich_editor_body(
    control_locator: Any,
    question_selector: str,
    control_selector: str,
    rich_control_selectors: list[str],
) -> Any:
    """Map the saved textarea to an editor in its nearest local answer group."""
    try:
        is_textarea = control_locator.evaluate(
            "el => el.tagName.toLowerCase() === 'textarea'"
        )
        group = control_locator.locator(
            "xpath=ancestor::*[.//*[contains(concat(' ', normalize-space(@class), ' '), ' edui-editor ') ]][1]"
        )
        if not is_textarea or group.count() != 1:
            raise _PreflightError("rich editor source textarea has no unique answer group")

        mapping = group.evaluate(
            """(container, args) => {
                const current = document.querySelector(args.currentSelector);
                const controls = args.selectors
                    .map(selector => document.querySelector(selector))
                    .filter(control => control && container.contains(control));
                const editors = Array.from(container.querySelectorAll('.edui-editor'));
                return {
                    currentIndex: controls.indexOf(current),
                    controlCount: controls.length,
                    editorCount: editors.length,
                    valid: !!current && current.tagName.toLowerCase() === 'textarea' &&
                        controls.length === 1 && editors.length === 1 && controls[0] === current
                };
            }""",
            {
                "currentSelector": control_selector,
                "selectors": rich_control_selectors,
            },
        )
        if (
            not mapping["valid"]
            or mapping["currentIndex"] < 0
            or mapping["currentIndex"] >= mapping["editorCount"]
        ):
            raise _PreflightError("rich editor textarea does not map one-to-one to a local editor")

        editors = group.locator(".edui-editor")
        if editors.count() != mapping["editorCount"]:
            raise _PreflightError("rich editor structure changed during page inspection")
        editor = editors.nth(mapping["currentIndex"])
        is_scoped = editor.evaluate(
            """(editor, selector) => {
                const form = document.querySelector('#submitForm');
                const question = document.querySelector(selector);
                return !!form && !!question && form.contains(question) &&
                    question.contains(editor) && form.contains(editor);
            }""",
            question_selector,
        )
        if not is_scoped:
            raise _PreflightError("rich editor is outside its question or submitForm")
        frames = editor.locator("iframe")
        if frames.count() == 1:
            body = editor.frame_locator("iframe").locator("body")
        elif frames.count() == 0:
            body = editor.locator('[contenteditable="true"]')
        else:
            raise _PreflightError("rich editor contains multiple editable frames")
        if body.count() != 1 or not body.is_visible() or not body.is_editable():
            raise _PreflightError("mapped rich editor is not uniquely visible and editable")
        return body
    except _PreflightError:
        raise
    except Exception:
        raise _PreflightError("rich text editor could not be mapped safely") from None


def _build_actions(
    page: Any,
    questions_package: dict[str, Any],
    interaction_package: dict[str, Any],
    answers_package: dict[str, Any],
    *,
    include_submit: bool = True,
) -> tuple[Any, Any | None, list[dict[str, Any]]]:
    form_meta = interaction_package["form"]
    form_locator = _one_locator(page, form_meta["selector"])
    if form_locator.evaluate("el => el.tagName.toLowerCase()") != "form":
        raise _PreflightError("form selector did not identify a form")
    if form_locator.get_attribute("id") != "submitForm":
        raise _PreflightError("form selector did not identify submitForm")

    action_url = str(form_locator.evaluate("form => form.action || window.location.href"))
    if not _is_chaoxing_url(action_url):
        raise _PreflightError("form action is outside the HTTPS Chaoxing domain")

    questions_by_id = {
        question["question_id"]: question for question in questions_package["questions"]
    }
    controls_by_id = {
        control["question_id"]: control
        for control in interaction_package["question_controls"]
    }
    answers_by_id = {
        answer["question_id"]: answer for answer in answers_package["answers"]
    }
    question_selectors = [
        controls["question_selector"] for controls in interaction_package["question_controls"]
    ]
    if not _selectors_resolve_to_distinct_elements(page, question_selectors):
        raise _PreflightError("question selectors do not resolve to distinct page elements")
    page_questions = form_locator.locator(".questionLi")
    if page_questions.count() == 0:
        page_questions = form_locator.locator('div[id^="question"]')
    if page_questions.count() != len(questions_by_id):
        raise _PreflightError("current page question count does not match the run package")

    resolved_controls: dict[str, dict[str, Any]] = {}
    live_question_ids: set[str] = set()
    for question_number, question in enumerate(questions_package["questions"], start=1):
        question_id = question["question_id"]
        controls = controls_by_id[question_id]
        question_locator = _one_locator(page, controls["question_selector"])
        raw_question_id = question_locator.get_attribute("id") or ""
        if _stable_question_id(raw_question_id, question_number, live_question_ids) != question_id:
            raise _PreflightError("question selector does not match its saved question_id")
        is_question_root = question_locator.evaluate(
            """el => el.classList.contains('questionLi') ||
                (el.tagName.toLowerCase() === 'div' && el.id.startsWith('question'))"""
        )
        if not is_question_root:
            raise _PreflightError("question selector does not identify a question container")
        belongs_to_form = form_locator.evaluate(
            """(form, selector) => {
                const question = document.querySelector(selector);
                return !!question && (form === question || form.contains(question));
            }""",
            controls["question_selector"],
        )
        if not belongs_to_form:
            raise _PreflightError("question control is outside submitForm")

        option_locators: dict[str, Any] = {}
        for option in controls["choice_options"]:
            option_locator = _one_locator(page, option["selector"])
            _ensure_control_belongs_to_question(option_locator, controls["question_selector"])
            if not option_locator.is_visible() or not option_locator.is_enabled():
                raise _PreflightError("choice control is not visible and enabled")
            option_locators[option["option_id"]] = option_locator

        slot_locators: dict[str, Any] = {}
        for slot_control in controls["answer_slots"]:
            control_locator = _one_locator(page, slot_control["selector"])
            _ensure_control_belongs_to_question(control_locator, controls["question_selector"])
            slot_locators[slot_control["slot_id"]] = control_locator

        page_control_selectors = [
            option["selector"] for option in controls["choice_options"]
        ] + [slot_control["selector"] for slot_control in controls["answer_slots"]]
        if not _selectors_resolve_to_distinct_elements(page, page_control_selectors):
            raise _PreflightError("question controls do not resolve to distinct page elements")

        question_slots = {
            slot["slot_id"]: slot for slot in question["answer_slots"]
        }
        # Chaoxing can initialize UEditor after the crawler has saved the
        # package. In that case the saved textarea is classified as text_input,
        # then hidden while a visible iframe editor is created beside it.
        # Include short-answer textareas as editor candidates so the same safe
        # textarea-to-editor mapping can resolve this runtime transformation.
        rich_control_selectors = [
            slot_control["selector"]
            for slot_control in controls["answer_slots"]
            if slot_control["control_type"] == "rich_text_editor"
            or (
                question.get("question_type") == "short_answer"
                and slot_control["control_type"] == "text_input"
            )
        ]
        rich_editor_bodies: dict[str, Any] = {}
        for slot_control in controls["answer_slots"]:
            slot_id = slot_control["slot_id"]
            control_locator = slot_locators[slot_id]
            if slot_control["control_type"] == "rich_text_editor":
                if question_slots[slot_id]["input_kind"] != "rich_text_editor":
                    raise _PreflightError("rich editor control does not match its answer slot")
                rich_editor_bodies[slot_id] = _rich_editor_body(
                    control_locator,
                    controls["question_selector"],
                    slot_control["selector"],
                    rich_control_selectors,
                )
            elif slot_control["control_type"] == "text_input":
                if control_locator.is_visible():
                    if not control_locator.is_enabled() or not control_locator.is_editable():
                        raise _PreflightError("text input is not enabled and editable")
                    continue

                is_hidden_textarea = control_locator.evaluate(
                    "el => el.tagName.toLowerCase() === 'textarea'"
                )
                if question.get("question_type") != "short_answer" or not is_hidden_textarea:
                    raise _PreflightError("text input is not visible and editable")
                rich_editor_bodies[slot_id] = _rich_editor_body(
                    control_locator,
                    controls["question_selector"],
                    slot_control["selector"],
                    rich_control_selectors,
                )
        resolved_controls[question_id] = {
            "option_locators": option_locators,
            "slot_locators": slot_locators,
            "rich_editor_bodies": rich_editor_bodies,
        }

    actions: list[dict[str, Any]] = []
    for question_id, answer in answers_by_id.items():
        question = questions_by_id[question_id]
        controls = controls_by_id[question_id]
        resolved = resolved_controls[question_id]
        if answer["answer_type"] == "single_choice":
            option_id = answer["value"]
            option_locator = resolved["option_locators"].get(option_id)
            if option_locator is None:
                raise _PreflightError("answer option has no page control")
            actions.append(
                {
                    "kind": "choice",
                    "question_id": question_id,
                    "locator": option_locator,
                    "needs_action": not _is_selected(option_locator),
                }
            )
            continue

        slot_controls = {
            slot["slot_id"]: slot for slot in controls["answer_slots"]
        }
        question_slots = {
            slot["slot_id"]: slot for slot in question["answer_slots"]
        }
        if answer["answer_type"] == "fill_blank":
            values_by_slot = {item["slot_id"]: item["value"] for item in answer["values"]}
        else:
            values_by_slot = {
                question["answer_slots"][0]["slot_id"]: answer["value"]
            }

        for slot_id, value in values_by_slot.items():
            slot_meta = slot_controls[slot_id]
            slot = question_slots[slot_id]
            control_locator = resolved["slot_locators"][slot_id]
            action: dict[str, Any] = {
                "kind": slot_meta["control_type"],
                "question_id": question_id,
                "slot_id": slot_id,
                "locator": control_locator,
                "value": value,
            }
            use_rich_editor = (
                slot["input_kind"] == "rich_text_editor"
                or slot_id in resolved["rich_editor_bodies"]
            )
            if use_rich_editor:
                action["kind"] = "rich_text_editor"
                action["locator"] = resolved["rich_editor_bodies"][slot_id]
            action["needs_action"] = (
                _normal_text(_read_answer_text(action["locator"], action["kind"]))
                != _normal_text(value)
            )
            actions.append(action)

    submit_locator = None
    submit_selector = form_meta.get("submit_selector") if include_submit else None
    if include_submit and (
        not isinstance(submit_selector, str) or not submit_selector.strip()
    ):
        raise _PreflightError("submit control selector is missing")
    if submit_selector:
        submit_locator = _one_locator(page, submit_selector)
        belongs_to_form = bool(
            submit_locator.evaluate(
                """el => {
                    const form = el.closest('form') || el.form;
                    return !!form && form.id === 'submitForm';
                }"""
            )
        )
        if not belongs_to_form:
            raise _PreflightError("submit control is not associated with submitForm")
        if not submit_locator.is_visible() or not submit_locator.is_enabled():
            raise _PreflightError("submit control is not visible and enabled")

    return form_locator, submit_locator, actions


def _echo_matches(actions: list[dict[str, Any]]) -> bool:
    for action in actions:
        locator = action["locator"]
        if action["kind"] == "choice":
            if not _is_selected(locator):
                return False
        elif _normal_text(_read_answer_text(locator, action["kind"])) != _normal_text(
            action["value"]
        ):
            return False
    return True


def _post_submission_result(
    state: dict[str, Any], page: Any, actions: list[dict[str, Any]]
) -> None:
    body_text = ""
    try:
        body_text = page.locator("body").inner_text(timeout=1500)
    except Exception:
        pass
    baseline_text = state.get("pre_submit_text")
    dialog_texts = state.get("dialog_texts", [])
    new_dialog_text = " ".join(dialog_texts[state.get("pre_submit_dialog_count", 0):])

    if _marker_appeared_after_submit(body_text, baseline_text, new_dialog_text, _LOGIN_MARKERS):
        state["outcome"] = "login_rejected"
    elif _marker_appeared_after_submit(body_text, baseline_text, new_dialog_text, _CLOSED_MARKERS):
        state["outcome"] = "submission_blocked"
    elif _marker_appeared_after_submit(body_text, baseline_text, new_dialog_text, _FAILURE_MARKERS):
        state["outcome"] = "submission_rejected"
    else:
        new_success = next(
            (
                marker
                for marker in _SUCCESS_MARKERS
                if _marker_appeared_after_submit(
                    body_text, baseline_text, new_dialog_text, (marker,)
                )
            ),
            None,
        )
        if new_success:
            state["outcome"] = "success_message"
        elif any(
            response["main_frame"] and 200 <= response["status"] < 300
            for response in state["submit_responses"]
        ) and _echo_matches(actions):
            state["outcome"] = "answer_echo"
        else:
            try:
                final_path = urlsplit(page.url).path
            except ValueError:
                final_path = ""
            if (
                final_path
                and final_path != state.get("initial_path")
                and _marker_appeared_after_submit(
                    body_text, baseline_text, new_dialog_text, _RESULT_PAGE_MARKERS
                )
            ):
                state["outcome"] = "result_page"
            else:
                state["outcome"] = "unconfirmed"


def _run_browser(
    url: str,
    cookies: Mapping[str, str],
    questions_package: dict[str, Any],
    interaction_package: dict[str, Any],
    answers_package: dict[str, Any],
    *,
    fill_only: bool = False,
    headless: bool = False,
    cancel_event: threading.Event | None = None,
    progress_callback: Callable[..., None] | None = None,
    submit_confirmation_callback: Callable[[int], bool] | None = None,
) -> list[dict[str, Any]]:
    if not fill_only and not callable(submit_confirmation_callback):
        raise ChaoxingInteractionError(
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
        "edit_error": None,
        "filled": False,
        "submit_error": None,
        "editing_started": False,
        "submit_attempted": False,
        "manual_confirmation_requested": False,
        "manual_confirmation_accepted": False,
        "manual_cancelled": False,
        "mutation_requests": [],
        "mutation_responses": [],
        "submit_request_ids": set(),
        "submit_responses": [],
        "dialog_texts": [],
        "outcome": None,
    }

    try:
        from scrapling.fetchers import DynamicFetcher
    except ImportError:
        raise ChaoxingInteractionError(
            "当前 Python 环境缺少 Scrapling 浏览器 fetcher；请在运行项目的同一解释器安装 scrapling[fetchers] 并运行 scrapling install。",
            "browser_dependency",
        ) from None

    def page_setup(page: Any) -> None:
        if cookies:
            page.context.add_cookies(_browser_cookies(cookies))

        def track_request(request: Any) -> None:
            try:
                method = str(request.method).upper()
                if method not in {"POST", "PUT", "PATCH"} or not _is_chaoxing_url(request.url):
                    return
                if state["submit_attempted"]:
                    state["submit_request_ids"].add(id(request))
                elif state["editing_started"]:
                    state["mutation_requests"].append(method)
            except Exception:
                return

        def track_response(response: Any) -> None:
            try:
                request = response.request
                method = str(request.method).upper()
                if method not in {"POST", "PUT", "PATCH"} or not _is_chaoxing_url(request.url):
                    return
                if id(request) in state["submit_request_ids"]:
                    main_frame = response.frame == page.main_frame
                    state["submit_responses"].append(
                        {"status": int(response.status), "main_frame": bool(main_frame)}
                    )
                elif state["editing_started"]:
                    state["mutation_responses"].append({"status": int(response.status)})
            except Exception:
                return

        def accept_dialog(dialog: Any) -> None:
            try:
                state["dialog_texts"].append(str(dialog.message))
                if state["submit_attempted"]:
                    dialog.accept()
                else:
                    dialog.dismiss()
            except Exception:
                pass

        page.on("response", track_response)
        page.on("request", track_request)
        page.on("dialog", accept_dialog)

    def page_action(page: Any) -> None:
        state["page_action_called"] = True
        try:
            current_url = str(page.url)
            if not _is_chaoxing_url(current_url):
                raise _PreflightError("page is outside the HTTPS Chaoxing domain")
            try:
                initial_path = urlsplit(current_url).path
            except Exception:
                initial_path = ""
            state["initial_path"] = initial_path
            form_locator, submit_locator, actions = _build_actions(
                page,
                questions_package,
                interaction_package,
                answers_package,
                include_submit=False,
            )
            state["actions"] = actions
            state["preflight_complete"] = True
            state["editing_started"] = any(action["needs_action"] for action in actions)
            _report_progress(
                progress_callback,
                "preflight_complete",
                question_count=len(question_ids),
            )
            try:
                completed_count = 0
                for action in actions:
                    if cancel_event is not None and cancel_event.is_set():
                        raise RuntimeError("operation cancelled at a safe fill point")
                    if not action["needs_action"]:
                        continue
                    locator = action["locator"]
                    if action["kind"] == "choice":
                        locator.click()
                        if not _is_selected(locator):
                            raise _PreflightError("choice click did not select its option")
                    else:
                        locator.fill(action["value"])
                    completed_count += 1
                    _report_progress(
                        progress_callback,
                        "filling_answers",
                        question_count=len(question_ids),
                        completed_count=completed_count,
                    )
                if not _echo_matches(actions):
                    raise _PreflightError("answer controls did not echo the requested values")
            except Exception as error:
                state["edit_error"] = type(error).__name__
                _show_status_panel(
                    page,
                    "答题操作未完成",
                    "填写或回显核对失败，未触发表单提交。页面会保持打开，请检查后手动关闭。",
                )
                if not headless:
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
                    "答案已填写并核对；调试模式没有调用提交按钮。页面可能自动保存；检查后请手动关闭浏览器。",
                )
                if not headless:
                    _hold_visible_browser_until_closed(page)
                return

            state["manual_confirmation_requested"] = True
            if cancel_event is not None and cancel_event.is_set():
                state["manual_cancelled"] = True
                _show_status_panel(
                    page,
                    "已取消提交",
                    "没有触发表单提交。页面会保持打开，请检查后手动关闭。",
                )
                if not headless:
                    _hold_visible_browser_until_closed(page)
                return
            try:
                _report_progress(
                    progress_callback,
                    "awaiting_submit_confirmation",
                    question_count=len(question_ids),
                )
                confirmed = submit_confirmation_callback(len(question_ids))
            except Exception as error:
                state["edit_error"] = type(error).__name__
                _show_status_panel(
                    page,
                    "等待桌面提交确认时中断",
                    "没有触发表单提交。页面会保持打开，请检查后手动关闭。",
                )
                if not headless:
                    _hold_visible_browser_until_closed(page)
                return
            if confirmed is not True:
                state["manual_cancelled"] = True
                _show_status_panel(
                    page,
                    "已取消提交",
                    "没有触发表单提交。页面会保持打开，请检查后手动关闭。",
                )
                if not headless:
                    _hold_visible_browser_until_closed(page)
                return
            if cancel_event is not None and cancel_event.is_set():
                state["manual_cancelled"] = True
                _show_status_panel(
                    page,
                    "已取消提交",
                    "没有触发表单提交。页面会保持打开，请检查后手动关闭。",
                )
                if not headless:
                    _hold_visible_browser_until_closed(page)
                return
            state["manual_confirmation_accepted"] = True

            if not _is_chaoxing_url(str(page.url)):
                raise _PreflightError("桌面确认后浏览器已不在超星 HTTPS 页面")
            form_locator, submit_locator, actions = _build_actions(
                page,
                questions_package,
                interaction_package,
                answers_package,
                include_submit=True,
            )
            state["actions"] = actions
            if not _echo_matches(actions):
                raise _PreflightError("桌面确认后答案控件状态发生变化")

            try:
                state["pre_submit_text"] = page.locator("body").inner_text(timeout=1500)
            except Exception:
                state["pre_submit_text"] = None
            state["pre_submit_dialog_count"] = len(state["dialog_texts"])

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
                    "没有触发表单提交。页面会保持打开，请检查后手动关闭。",
                )
                if not headless:
                    _hold_visible_browser_until_closed(page)
                return
            if submit_locator is None:
                raise _PreflightError("submission control was not revalidated")
            state["submit_attempted"] = True
            try:
                submit_locator.click()
            except Exception as error:
                state["submit_error"] = type(error).__name__

            # Allow the request, page navigation, and success toast to settle.
            for _ in range(20):
                page.wait_for_timeout(250)
                _post_submission_result(state, page, actions)
                if state.get("outcome") in {
                    "login_rejected",
                    "submission_blocked",
                    "submission_rejected",
                    "success_message",
                    "answer_echo",
                    "result_page",
                }:
                    break
        except _PreflightError as error:
            # Preflight messages are fixed, safe diagnostics; they contain no
            # page content, selectors, credentials, or request URLs.
            state["preflight_error"] = str(error)
            _show_status_panel(
                page,
                "页面预检未通过",
                "未触发表单提交。页面会保持打开，请检查后手动关闭。",
            )
            if not headless:
                _hold_visible_browser_until_closed(page)
        except Exception as error:
            if state["submit_attempted"]:
                state["submit_error"] = type(error).__name__
            elif state["editing_started"]:
                state["edit_error"] = type(error).__name__
            else:
                state["preflight_error"] = f"preflight raised {type(error).__name__}"
            if not state["submit_attempted"]:
                _show_status_panel(
                    page,
                    "答题操作未完成",
                    "页面结构或控件操作遇到错误，未触发表单提交。页面会保持打开，请检查后手动关闭。",
                )
                if not headless:
                    _hold_visible_browser_until_closed(page)

        if state["submit_attempted"] and state.get("outcome") not in {
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

    # Suppress third-party request logs so signed page URLs and cookie-bearing
    # browser errors cannot be copied into terminal output or project logs.
    previous_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        DynamicFetcher.fetch(
            url,
            page_setup=page_setup,
            page_action=page_action,
            headless=headless,
            google_search=False,
            network_idle=False,
            timeout=30_000,
            wait=10_000,
        )
    except Exception as error:
        if fill_only and state.get("filled"):
            return _filled_unsubmitted_items(
                question_ids,
                possible_autosave=bool(
                    state.get("mutation_requests") or state.get("mutation_responses")
                ),
            )
        if state.get("submit_attempted"):
            return _unconfirmed_items(
                question_ids,
                "已触发表单提交，但浏览器在收到明确回执前停止；请先检查超星页面再决定是否重试。",
            )
        if state.get("mutation_requests") or state.get("mutation_responses"):
            return _unconfirmed_items(
                question_ids,
                "页面可能已自动保存部分答案，但未能确认提交结果；请先检查超星页面再决定是否重试。",
            )
        if state.get("page_action_called") and state.get("preflight_complete"):
            return _skipped_items(
                question_ids,
                "页面答题控件操作中止，尚未触发表单提交。",
            )
        raise ChaoxingInteractionError(
            f"超星页面请求失败（{type(error).__name__}）；尚未填写答案。",
            "page_request",
        ) from None
    finally:
        logging.disable(previous_disable)

    if not state.get("page_action_called"):
        raise ChaoxingInteractionError(
            "浏览器未能打开超星作业页面；尚未填写答案。",
            "page_action_missing",
        )
    if fill_only and state.get("filled"):
        return _filled_unsubmitted_items(
            question_ids,
            possible_autosave=bool(
                state.get("mutation_requests") or state.get("mutation_responses")
            ),
        )
    if state.get("preflight_error") or not state.get("preflight_complete"):
        if state.get("submit_attempted") or state.get("mutation_requests") or state.get("mutation_responses"):
            return _unconfirmed_items(
                question_ids,
                "页面操作中断且可能已保存答案或已发送请求；请先检查超星页面。",
            )
        if state.get("filled"):
            return _filled_unsubmitted_items(
                question_ids,
                reason="答案已填写并核对，但提交控件复核未通过；没有触发表单提交。请先检查页面。",
            )
        preflight_error = state.get("preflight_error")
        reason = (
            f"交互预检失败：{preflight_error}；未填写或提交答案。"
            if preflight_error
            else "交互预检未完成；未填写或提交答案。"
        )
        return _skipped_items(
            question_ids,
            reason,
        )
    if state.get("manual_cancelled"):
        if state.get("mutation_requests") or state.get("mutation_responses"):
            return _unconfirmed_items(
                question_ids,
                "用户取消了表单提交；页面可能已自动保存答案，请先检查超星页面。",
            )
        if state.get("filled"):
            return _filled_unsubmitted_items(
                question_ids,
                reason="答案已填写并核对；未触发表单提交。请检查页面后手动关闭浏览器。",
            )
        return _skipped_items(question_ids, "用户取消提交，未触发表单提交。")
    if (
        state.get("manual_confirmation_requested")
        and not state.get("manual_confirmation_accepted")
    ):
        if state.get("mutation_requests") or state.get("mutation_responses"):
            return _unconfirmed_items(
                question_ids,
                "桌面提交确认中断；页面可能已自动保存答案，请先检查超星页面。",
            )
        if state.get("filled"):
            return _filled_unsubmitted_items(
                question_ids,
                reason="答案已填写并核对；尚未确认提交。请检查页面后手动关闭浏览器。",
            )
        return _skipped_items(question_ids, "未完成桌面提交确认，未触发表单提交。")
    if state.get("edit_error"):
        if state.get("mutation_requests") or state.get("mutation_responses"):
            return _unconfirmed_items(
                question_ids,
                "填写过程中页面可能已自动保存部分答案；未完成提交确认，请先检查超星页面。",
            )
        return _skipped_items(
            question_ids,
            "页面答题控件操作中止，尚未触发表单提交。",
        )
    if not state.get("submit_attempted"):
        return _skipped_items(question_ids, "未能触发表单提交。")

    outcome = state.get("outcome")
    if outcome == "login_rejected":
        return _failed_items(
            question_ids,
            "login_required",
            "超星提示登录状态失效，答案未获提交确认。",
        )
    if outcome == "submission_blocked":
        return _failed_items(
            question_ids,
            "submission_blocked",
            "超星提示作业当前不可提交。",
        )
    if outcome == "submission_rejected":
        return _failed_items(
            question_ids,
            "submission_rejected",
            "超星页面明确提示提交失败。",
        )
    if outcome == "success_message":
        return _confirmed_items(
            question_ids,
            "success_message",
            "提交后页面显示成功提示。",
        )
    if outcome == "answer_echo":
        return _confirmed_items(
            question_ids,
            "answer_echo",
            "提交后的页面回显与本次答案一致。",
        )
    if outcome == "result_page":
        return _confirmed_items(
            question_ids,
            "result_page",
            "提交后页面进入作答结果状态。",
        )
    if any(
        response["status"] in {401, 403}
        for response in state.get("submit_responses", [])
    ):
        return _failed_items(
            question_ids,
            "login_required",
            "超星拒绝了提交请求；请检查登录状态和作业权限。",
        )
    return _unconfirmed_items(
        question_ids,
        "已触发表单提交，但页面没有返回足够的成功或失败证据。",
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
    """Fill a Chaoxing work page and await WebUI authorization before submit."""
    if not callable(submit_confirmation_callback):
        raise ChaoxingInteractionError(
            "正常提交必须由 AutoLearn 桌面确认流程发起。",
            "desktop_confirmation_required",
        )
    question_ids = [question["question_id"] for question in questions_package["questions"]]
    if task_id is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", task_id):
        raise ChaoxingInteractionError("任务 ID 格式无效。", "invalid_task")
    if model_profile_id is not None and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", model_profile_id
    ):
        raise ChaoxingInteractionError("模型档案标识无效。", "invalid_task")
    package_run_id = questions_package.get("run_id")
    if run_id is not None and package_run_id != run_id:
        raise ChaoxingInteractionError("任务 run_id 与题包不匹配。", "invalid_package")
    if page_url is None:
        url, cookies = _load_chaoxing_config()
    else:
        url = page_url
        if not _is_chaoxing_url(url):
            raise ChaoxingInteractionError("任务 URL 必须是 HTTPS 的 chaoxing.com 页面。", "config_url_invalid")
        try:
            cookies = (
                credential_provider.get_site_cookies(SITE_ID)
                if credential_provider is not None
                else _load_chaoxing_config()[1]
            )
        except Exception:
            raise ChaoxingInteractionError("无法安全读取站点登录状态。", "config_cookie_invalid") from None
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
    """Fill and verify a Chaoxing page without looking up or invoking submit."""
    if task_id is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", task_id):
        raise ChaoxingInteractionError("任务 ID 格式无效。", "invalid_task")
    if model_profile_id is not None and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", model_profile_id
    ):
        raise ChaoxingInteractionError("模型档案标识无效。", "invalid_task")
    if run_id is not None and questions_package.get("run_id") != run_id:
        raise ChaoxingInteractionError("任务 run_id 与题包不匹配。", "invalid_package")
    if page_url is None:
        url, cookies = _load_chaoxing_config()
    else:
        url = page_url
        if not _is_chaoxing_url(url):
            raise ChaoxingInteractionError("任务 URL 必须是 HTTPS 的 chaoxing.com 页面。", "config_url_invalid")
        try:
            cookies = (
                credential_provider.get_site_cookies(SITE_ID)
                if credential_provider is not None
                else _load_chaoxing_config()[1]
            )
        except Exception:
            raise ChaoxingInteractionError("无法安全读取站点登录状态。", "config_cookie_invalid") from None
    return _run_browser(
        url,
        cookies,
        questions_package,
        interaction_package,
        answers_package,
        fill_only=True,
        headless=False,
        cancel_event=cancel_event,
        progress_callback=progress_callback,
    )
