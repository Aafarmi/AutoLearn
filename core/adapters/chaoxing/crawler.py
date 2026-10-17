"""Scrapling-based crawler for Chaoxing assignment pages.

Call :func:`crawl_chaoxing` with the page URL and a run ID. Authentication
cookies are optional, transient request arguments; they are never written to
the generated package.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import html
import json
import re
from html.parser import HTMLParser
from pathlib import Path
import threading
from typing import Any, Callable, Mapping
from urllib.parse import urljoin, urlsplit

from scrapling.fetchers import FetcherSession
from core.data_migration import ensure_user_data_migrated
from core.paths import data_path

if __package__:
    from . import SITE_ID
else:
    # Support running this file directly from an IDE or terminal.
    SITE_ID = "chaoxing"


ADAPTER_VERSION = "0.1.0"
SCHEMA_VERSION = "1.0"
_CONFIG_PATH = data_path("config.json")
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HONG_KONG_TIMEZONE = timezone(timedelta(hours=8))


def _new_run_id() -> str:
    """Create a readable site-and-time run ID, avoiding same-second clashes."""
    timestamp = datetime.now(_HONG_KONG_TIMEZONE).strftime("%Y%m%d_%H%M%S")
    base_id = f"{SITE_ID}_{timestamp}"
    candidate = base_id
    suffix = 2
    runs_dir = data_path("runs")
    while (runs_dir / candidate).exists():
        candidate = f"{base_id}_{suffix}"
        suffix += 1
    return candidate


class CrawlError(RuntimeError):
    """Raised when a page cannot be converted to the Chaoxing data contract."""


class CrawlCancelled(RuntimeError):
    """Raised at a crawler safe point after a cooperative cancel request."""


def _check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise CrawlCancelled("crawl cancelled")


def _report_progress(
    callback: Callable[..., None] | None,
    state: str,
    *,
    question_count: int | None = None,
    asset_count: int | None = None,
) -> None:
    if callback is None:
        return
    try:
        callback(state, question_count=question_count, asset_count=asset_count)
    except Exception:
        pass


def _text_of(element: Any) -> str:
    return str(element.get_all_text(strip=True)).strip()


def _selector_for_id(element_id: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", element_id):
        return f"#{element_id}"
    escaped = element_id.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", "\\a ").replace("\r", "\\d ")
    return f'[id="{escaped}"]'


def _css_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\a ")


def _question_selector(question: Any, number: int) -> str:
    element_id = str(question.attrib.get("id", "")).strip()
    if element_id:
        return _selector_for_id(element_id)
    generated = str(getattr(question, "generate_css_selector", "") or "").strip()
    if generated:
        return generated
    return f".questionLi:nth-of-type({number})"


def _unique_question_id(question: Any, number: int, used: set[str]) -> str:
    raw_id = str(question.attrib.get("id", "")).strip()
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


def _source_type(question: Any) -> str:
    return str(question.attrib.get("typename", "")).strip() or "未知题型"


def _find_stem_nodes(question: Any) -> list[Any]:
    stem_nodes = question.css(".mark_name p")
    if stem_nodes:
        return list(stem_nodes)

    excluded_ancestors = {"answerBg", "answer_p", "stem_answer", "qtDetail", "analysis"}
    paragraphs: list[Any] = []
    for paragraph in question.css("p"):
        parent = paragraph.parent
        inside_answer = False
        while parent:
            classes = set(str(parent.attrib.get("class", "")).split())
            if classes & excluded_ancestors:
                inside_answer = True
                break
            parent = parent.parent
        if not inside_answer:
            paragraphs.append(paragraph)
    if paragraphs:
        return paragraphs
    return list(question.css(".mark_name"))


def _option_label(option: Any, ordinal: int) -> str:
    label_nodes = option.css(".num_option")
    if label_nodes:
        label = _text_of(label_nodes[0]).strip().rstrip(".、．")
        if label:
            return label

    aria_label = str(option.attrib.get("aria-label", "")).strip()
    match = re.match(r"^([A-Za-z0-9]+)", aria_label)
    if match:
        return match.group(1)
    return f"选项{ordinal}"


def _option_id(label: str, ordinal: int, used: set[str]) -> str:
    candidate = re.sub(r"[^A-Za-z0-9._:-]+", "-", label).strip("-._:")
    if not candidate:
        candidate = f"option-{ordinal:02d}"
    if not candidate[0].isalnum():
        candidate = f"option-{candidate}"
    candidate = candidate[:128]
    base = candidate
    suffix = 2
    while candidate in used:
        suffix_text = f"-{suffix}"
        candidate = f"{base[:128 - len(suffix_text)]}{suffix_text}"
        suffix += 1
    used.add(candidate)
    return candidate


class _SafeMarkupParser(HTMLParser):
    """Keep presentational markup while dropping scripts and active controls."""

    _ALLOWED_TAGS = {
        "a", "abbr", "b", "blockquote", "br", "caption", "code", "dd", "del",
        "div", "dl", "dt", "em", "h1", "h2", "h3", "h4", "hr", "i", "img",
        "li", "math", "mfrac", "mi", "mn", "mo", "mrow", "msqrt", "msub",
        "msup", "mtext", "ol", "p", "pre", "q", "s", "semantics", "small",
        "span", "strong", "sub", "sup", "table", "tbody", "td", "th", "thead",
        "tr", "u", "ul",
    }
    _VOID_TAGS = {"br", "hr", "img"}
    _BLOCKED_CONTENT_TAGS = {"script", "style", "textarea", "iframe", "object", "embed", "svg"}
    _SAFE_ATTRIBUTES = {"alt", "class", "colspan", "height", "rowspan", "title", "width"}

    def __init__(self, image_paths: Mapping[str, str], base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.image_paths = image_paths
        self.base_url = base_url
        self.parts: list[str] = []
        self.open_tags: list[str] = []
        self.blocked_tag: str | None = None
        self.blocked_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attributes = {key.lower(): value or "" for key, value in attrs}

        if self.blocked_depth:
            if tag == self.blocked_tag:
                self.blocked_depth += 1
            return
        style = attributes.get("style", "")
        hidden_style = re.search(
            r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", style, re.IGNORECASE
        )
        if tag in self._BLOCKED_CONTENT_TAGS or "hidden" in attributes or hidden_style:
            if tag in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
                return
            self.blocked_tag = tag
            self.blocked_depth = 1
            return
        if tag not in self._ALLOWED_TAGS:
            return

        if tag == "img":
            source = (
                attributes.get("data-original")
                or attributes.get("data-src")
                or attributes.get("src")
            )
            if not source:
                return
            absolute_source = urljoin(self.base_url, source)
            local_path = self.image_paths.get(absolute_source)
            if not local_path:
                return
            safe_attrs = [("src", local_path)]
        else:
            safe_attrs = []

        for key in self._SAFE_ATTRIBUTES:
            value = attributes.get(key)
            if value:
                if key == "class":
                    value = " ".join(re.findall(r"[A-Za-z0-9_-]+", value))
                if value:
                    safe_attrs.append((key, value))

        rendered_attrs = "".join(
            f' {key}="{html.escape(value, quote=True)}"' for key, value in safe_attrs
        )
        self.parts.append(f"<{tag}{rendered_attrs}>")
        if tag not in self._VOID_TAGS:
            self.open_tags.append(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.blocked_depth:
            if tag == self.blocked_tag:
                self.blocked_depth -= 1
                if self.blocked_depth == 0:
                    self.blocked_tag = None
            return
        if tag not in self.open_tags:
            return
        while self.open_tags:
            opened = self.open_tags.pop()
            self.parts.append(f"</{opened}>")
            if opened == tag:
                break

    def handle_data(self, data: str) -> None:
        if not self.blocked_depth:
            self.parts.append(html.escape(data))

    def get_markup(self) -> str:
        while self.open_tags:
            self.parts.append(f"</{self.open_tags.pop()}>")
        return "".join(self.parts)


class _PlainTextParser(HTMLParser):
    _BLOCK_TAGS = {"br", "dd", "div", "dl", "dt", "h1", "h2", "h3", "h4", "li", "ol", "p", "pre", "table", "tr", "ul"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self._BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _plain_text(markup: str) -> str:
    parser = _PlainTextParser()
    parser.feed(markup)
    parser.close()
    return " ".join("".join(parser.parts).split())


class _AssetStore:
    """Download raster images and expose only package-relative file paths."""

    _MIME_EXTENSIONS = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
    }

    def __init__(
        self,
        session: Any,
        run_dir: Path,
        cookies: Mapping[str, str] | None,
        page_url: str,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.session = session
        self.run_dir = run_dir
        self.cookies = dict(cookies) if cookies else None
        self.page_url = page_url
        self.cancel_event = cancel_event
        self.cache: dict[str, str | None] = {}
        self.failed_urls: set[str] = set()
        self.created_files: list[Path] = []

    def cleanup_created_files(self) -> None:
        for path in self.created_files:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _is_chaoxing_host(hostname: str) -> bool:
        host = hostname.lower().rstrip(".")
        return host == "chaoxing.com" or host.endswith(".chaoxing.com")

    def _same_secure_site(self, image_parts: Any) -> bool:
        page_parts = urlsplit(self.page_url)
        page_host = page_parts.hostname or ""
        image_host = image_parts.hostname or ""
        return (
            page_parts.scheme.lower() == "https"
            and image_parts.scheme.lower() == "https"
            and self._is_chaoxing_host(page_host)
            and self._is_chaoxing_host(image_host)
        )

    def _safe_referer(self) -> str | None:
        page_parts = urlsplit(self.page_url)
        hostname = page_parts.hostname or ""
        if page_parts.scheme.lower() != "https" or not self._is_chaoxing_host(hostname):
            return None
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        if page_parts.port and page_parts.port != 443:
            hostname = f"{hostname}:{page_parts.port}"
        # Keep the page origin, but never forward signed query strings or paths.
        return f"https://{hostname}/"

    @property
    def saved_count(self) -> int:
        return sum(path is not None for path in self.cache.values())

    def _mark_failed(self, absolute_url: str) -> None:
        self.cache[absolute_url] = None
        self.failed_urls.add(absolute_url)

    @staticmethod
    def _extension(content_type: str, body: bytes) -> str | None:
        mime = content_type.split(";", 1)[0].strip().lower()
        if mime in _AssetStore._MIME_EXTENSIONS:
            return _AssetStore._MIME_EXTENSIONS[mime]
        if body.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if body.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if body.startswith((b"GIF87a", b"GIF89a")):
            return ".gif"
        if body.startswith(b"RIFF") and body[8:12] == b"WEBP":
            return ".webp"
        if body.startswith(b"BM"):
            return ".bmp"
        return None

    def save(self, image_url: str) -> str | None:
        _check_cancel(self.cancel_event)
        absolute_url = urljoin(self.page_url, image_url)
        if absolute_url in self.cache:
            return self.cache[absolute_url]

        parsed = urlsplit(absolute_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            self._mark_failed(absolute_url)
            return None

        same_secure_site = self._same_secure_site(parsed)
        request_kwargs: dict[str, Any] = {}
        if same_secure_site:
            referer = self._safe_referer()
            if referer:
                request_kwargs["headers"] = {"Referer": referer}
        if self.cookies and same_secure_site:
            request_kwargs["cookies"] = self.cookies
        try:
            response = self.session.get(absolute_url, **request_kwargs)
            _check_cancel(self.cancel_event)
            status = int(getattr(response, "status", 0))
            if not 200 <= status < 300:
                self._mark_failed(absolute_url)
                return None
            body = response.body
            if not isinstance(body, bytes):
                body = bytes(body)
            headers = getattr(response, "headers", {}) or {}
            content_type = str(
                headers.get("content-type") or headers.get("Content-Type") or ""
            )
            extension = self._extension(content_type, body)
            if not extension:
                self._mark_failed(absolute_url)
                return None
        except CrawlCancelled:
            raise
        except Exception:
            # Do not emit a URL or request exception; signed URLs may contain secrets.
            self._mark_failed(absolute_url)
            return None

        digest = hashlib.sha256(absolute_url.encode("utf-8")).hexdigest()[:16]
        filename = f"image-{digest}{extension}"
        asset_dir = self.run_dir / "assets"
        asset_dir.mkdir(parents=True, exist_ok=True)
        (asset_dir / filename).write_bytes(body)
        self.created_files.append(asset_dir / filename)
        local_path = f"assets/{filename}"
        self.cache[absolute_url] = local_path
        return local_path


def _image_sources(element: Any, page_url: str) -> list[str]:
    sources: list[str] = []
    seen: set[str] = set()
    for image in element.css("img"):
        attrs = image.attrib
        source = attrs.get("data-original") or attrs.get("data-src") or attrs.get("src")
        if not source:
            continue
        absolute = urljoin(page_url, str(source).strip())
        if absolute not in seen:
            seen.add(absolute)
            sources.append(absolute)
    return sources


def _content(element: Any, page_url: str, assets: _AssetStore) -> dict[str, Any]:
    image_paths: dict[str, str] = {}
    asset_paths: list[str] = []
    for source in _image_sources(element, page_url):
        local_path = assets.save(source)
        if local_path:
            image_paths[source] = local_path
            if local_path not in asset_paths:
                asset_paths.append(local_path)

    parser = _SafeMarkupParser(image_paths, page_url)
    parser.feed(str(element.html_content))
    parser.close()
    sanitized_html = parser.get_markup()
    return {
        "text": _plain_text(sanitized_html),
        "html": sanitized_html,
        "asset_paths": asset_paths,
    }


def _choice_option_selector(question_selector: str, option: Any, ordinal: int) -> str:
    option_id = str(option.attrib.get("id", "")).strip()
    if option_id:
        return _selector_for_id(option_id)
    aria_label = str(option.attrib.get("aria-label", "")).strip()
    qid = str(option.attrib.get("qid", "")).strip()
    if aria_label:
        selector = f"{question_selector} .answerBg[aria-label=\"{_css_string(aria_label)}\"]"
        if qid:
            selector += f'[qid="{_css_string(qid)}"]'
        return selector
    generated = str(getattr(option, "generate_css_selector", "") or "").strip()
    if generated:
        return generated
    return f"{question_selector} .answerBg:nth-of-type({ordinal})"


def _choice_is_selected(option: Any) -> bool:
    attrs = option.attrib
    if str(attrs.get("aria-checked", "")).lower() == "true":
        return True
    if str(attrs.get("aria-pressed", "")).lower() == "true":
        return True
    if "checked" in attrs:
        return True
    classes = set(str(attrs.get("class", "")).lower().split())
    return bool(classes & {"selected", "checked", "active", "current"})


def _text_controls(question: Any) -> list[Any]:
    controls: list[Any] = []
    for selector in ("textarea", 'input[type="text"]', 'input:not([type])', 'input[type="number"]'):
        controls.extend(question.css(selector))

    # Avoid duplicates in case the selector engine returns a textarea for an
    # input selector or a page has repeated matching markup.
    unique: list[Any] = []
    seen: set[tuple[str, str, str]] = set()
    for control in controls:
        attrs = control.attrib
        tag = str(getattr(control, "tag", "")).lower()
        if not tag:
            tag = "textarea" if "name" in attrs and "type" not in attrs else "input"
        key = (tag, str(attrs.get("id", "")), str(attrs.get("name", "")))
        if key in seen:
            continue
        seen.add(key)
        unique.append(control)
    return unique


def _control_selector(question_selector: str, control: Any, ordinal: int) -> str:
    attrs = control.attrib
    element_id = str(attrs.get("id", "")).strip()
    if element_id:
        return _selector_for_id(element_id)
    name = str(attrs.get("name", "")).strip()
    if name:
        tag = str(getattr(control, "tag", "textarea")).lower()
        return f'{question_selector} {tag}[name="{_css_string(name)}"]'
    generated = str(getattr(control, "generate_css_selector", "") or "").strip()
    if generated:
        return generated
    tag = str(getattr(control, "tag", "textarea")).lower()
    return f"{question_selector} {tag}:nth-of-type({ordinal})"


def _control_value(control: Any) -> str:
    attrs = control.attrib
    tag = str(getattr(control, "tag", "")).lower()
    if tag == "input":
        value = str(attrs.get("value", ""))
    else:
        value = _text_of(control)
    value = value.strip()
    if re.search(r"<[A-Za-z][^>]*>", value):
        parser = _SafeMarkupParser({}, "")
        parser.feed(value)
        parser.close()
        value = _plain_text(parser.get_markup())
    return value


def _control_kind(question: Any, control: Any) -> str:
    tag = str(getattr(control, "tag", "")).lower()
    attrs = control.attrib
    control_id = str(attrs.get("id", ""))
    has_editor = bool(question.css(".edui-editor"))
    style = str(attrs.get("style", ""))
    hidden_source = (
        "hidden" in attrs
        or re.search(
            r"(?:^|;)\s*display\s*:\s*none(?:\s*!important)?\s*(?:;|$)",
            style,
            re.IGNORECASE,
        )
        is not None
    )
    if tag == "textarea" and (
        control_id.startswith("answerEditor")
        or str(attrs.get("name", "")).startswith("answerEditor")
        or has_editor
        # UEditor may be initialized by page JavaScript, leaving only its
        # hidden backing textarea in the crawler's server-rendered HTML.
        or hidden_source
    ):
        return "rich_text_editor"
    return "text_input"


def _question_type(source_type: str, options: list[Any], controls: list[Any], question: Any) -> str:
    if options:
        if question.css('[role="checkbox"], input[type="checkbox"]'):
            raise CrawlError("检测到多选控件，但当前超星题目 Schema 仅支持单选、填空和简答。")
        return "single_choice"

    source = source_type.lower()
    if "填空" in source or "计算" in source:
        return "fill_blank"
    if "简答" in source or "论述" in source or "问答" in source:
        return "short_answer"
    if not controls:
        raise CrawlError(f"题型“{source_type}”未识别到可记录的答题控件。")

    has_plain_input = any(str(getattr(control, "tag", "")).lower() == "input" for control in controls)
    if has_plain_input:
        return "fill_blank"
    if question.css(".edui-editor"):
        return "short_answer"
    return "fill_blank"


def _extract_question(
    question: Any,
    number: int,
    page_url: str,
    assets: _AssetStore,
    used_ids: set[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    question_id = _unique_question_id(question, number, used_ids)
    source_type = _source_type(question)
    question_selector = _question_selector(question, number)

    stem_nodes = _find_stem_nodes(question)
    if not stem_nodes:
        raise CrawlError(f"第 {number} 题未找到题干节点。")
    stem_parts = [_content(node, page_url, assets) for node in stem_nodes]
    stem = {
        "text": "\n".join(part["text"] for part in stem_parts if part["text"]),
        "html": "\n".join(part["html"] for part in stem_parts if part["html"]),
        "asset_paths": list(dict.fromkeys(path for part in stem_parts for path in part["asset_paths"])),
    }

    option_nodes = list(question.css(".answerBg"))
    controls = _text_controls(question)
    question_type = _question_type(source_type, option_nodes, controls, question)

    options: list[dict[str, Any]] = []
    choice_controls: list[dict[str, str]] = []
    selected_option_id: str | None = None
    used_option_ids: set[str] = set()
    for ordinal, option in enumerate(option_nodes, start=1):
        label = _option_label(option, ordinal)
        option_id = _option_id(label, ordinal, used_option_ids)
        content_nodes = option.css(".answer_p")
        content_node = content_nodes[0] if content_nodes else option
        options.append({
            "option_id": option_id,
            "label": label,
            "content": _content(content_node, page_url, assets),
        })
        choice_controls.append({
            "option_id": option_id,
            "selector": _choice_option_selector(question_selector, option, ordinal),
        })
        if _choice_is_selected(option):
            if selected_option_id is not None:
                raise CrawlError(f"第 {number} 题出现多个已选选项，无法按单选题记录。")
            selected_option_id = option_id

    answer_slots: list[dict[str, Any]] = []
    answer_slot_controls: list[dict[str, Any]] = []
    for ordinal, control in enumerate(controls, start=1):
        slot_id = f"blank_{ordinal}"
        kind = _control_kind(question, control)
        answer_slots.append({"slot_id": slot_id, "ordinal": ordinal, "input_kind": kind})
        answer_slot_controls.append({
            "slot_id": slot_id,
            "selector": _control_selector(question_selector, control, ordinal),
            "control_type": kind,
            "current_value": _control_value(control),
        })

    if question_type == "single_choice" and len(options) < 2:
        raise CrawlError(f"第 {number} 题被识别为单选题，但选项少于两个。")
    if question_type == "fill_blank" and not answer_slots:
        raise CrawlError(f"第 {number} 题被识别为填空题，但未找到答题空位。")
    if question_type == "short_answer" and len(answer_slots) != 1:
        raise CrawlError(f"第 {number} 题被识别为简答题，但答题控件数量不是一个。")

    package_question = {
        "question_id": question_id,
        "number": number,
        "question_type": question_type,
        "source_type": source_type,
        "stem": stem,
        "options": options,
        "answer_slots": answer_slots,
    }
    question_controls: dict[str, Any] = {
        "question_id": question_id,
        "question_selector": question_selector,
        "choice_options": choice_controls,
        "answer_slots": answer_slot_controls,
    }
    if options:
        question_controls["selected_option_id"] = selected_option_id

    return package_question, question_controls


def _form_metadata(page: Any) -> dict[str, Any]:
    # Chaoxing assignment pages use this form as the submission boundary.
    # Do not silently select another form (for example, a search/login form)
    # when the expected assignment form is absent or duplicated.
    forms = list(page.css("form#submitForm"))
    if not forms:
        raise CrawlError("页面中未找到 id 为 submitForm 的作业表单。")
    if len(forms) != 1:
        raise CrawlError("页面中存在多个 id 为 submitForm 的表单，无法安全记录交互控件。")
    form = forms[0]
    form_selector = _selector_for_id("submitForm")
    result: dict[str, Any] = {"selector": form_selector}

    for selector in ('button[type="submit"]', 'input[type="submit"]'):
        buttons = form.css(selector)
        if buttons:
            button_id = str(buttons[0].attrib.get("id", "")).strip()
            result["submit_selector"] = (
                _selector_for_id(button_id) if button_id else f"{form_selector} {selector}"
            )
            break
    return result


def crawl_chaoxing(
    url: str,
    *,
    task_id: str | None = None,
    run_id: str | None = None,
    model_profile_id: str | None = None,
    output_dir: str | Path | None = None,
    cookies: Mapping[str, str] | None = None,
    credential_provider: Any | None = None,
    cancel_event: threading.Event | None = None,
    progress_callback: Callable[..., None] | None = None,
    session: Any | None = None,
) -> dict[str, Any]:
    """Fetch a Chaoxing assignment and write questions/interaction JSON files.

    ``session`` may be an already configured Scrapling ``FetcherSession``; it
    should use ``stealthy_headers=False`` to avoid Scrapling's Google Referer.
    If omitted, a temporary static-fetch session with that setting is created.
    Pass cookies as an in-memory mapping when the page requires an existing login.
    """
    if task_id is not None and not _ID_PATTERN.fullmatch(task_id):
        raise ValueError("task_id has an invalid format")
    if model_profile_id is not None and not _ID_PATTERN.fullmatch(model_profile_id):
        raise ValueError("model_profile_id has an invalid format")
    _check_cancel(cancel_event)
    if cookies is None and credential_provider is not None:
        cookies = credential_provider.get_site_cookies(SITE_ID)
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")
    url = url.strip()
    run_id = run_id or _new_run_id()
    if not _ID_PATTERN.fullmatch(run_id):
        raise ValueError("run_id must match the adapter ID format")

    run_dir = Path(output_dir) if output_dir is not None else data_path("runs", run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "assets").mkdir(parents=True, exist_ok=True)

    if session is None:
        with FetcherSession(timeout=30, stealthy_headers=False) as active_session:
            return _crawl_with_session(
                url, run_id, run_dir, cookies, active_session, cancel_event, progress_callback
            )
    return _crawl_with_session(
        url, run_id, run_dir, cookies, session, cancel_event, progress_callback
    )


def _crawl_with_session(
    url: str,
    run_id: str,
    run_dir: Path,
    cookies: Mapping[str, str] | None,
    session: Any,
    cancel_event: threading.Event | None = None,
    progress_callback: Callable[..., None] | None = None,
) -> dict[str, Any]:
    request_kwargs: dict[str, Any] = {}
    if cookies:
        page_parts = urlsplit(url)
        if (
            page_parts.scheme.lower() != "https"
            or not _AssetStore._is_chaoxing_host(page_parts.hostname or "")
        ):
            raise CrawlError("登录 Cookie 只可用于 HTTPS 的 chaoxing.com 页面。")
        request_kwargs["cookies"] = dict(cookies)
    _report_progress(progress_callback, "requesting_page")
    _check_cancel(cancel_event)
    try:
        page = session.get(url, **request_kwargs)
    except Exception as error:
        raise CrawlError(f"超星页面请求失败（{type(error).__name__}）。") from None

    _check_cancel(cancel_event)
    status = int(getattr(page, "status", 0))
    if not 200 <= status < 300:
        raise CrawlError(f"超星页面返回 HTTP {status}，未生成数据包。")

    question_nodes = list(page.css(".questionLi"))
    if not question_nodes:
        question_nodes = list(page.css('div[id^="question"]'))
    if not question_nodes:
        raise CrawlError("响应中未找到超星题目容器；请确认页面内容和登录状态。")

    page_url = str(getattr(page, "url", "") or url)
    assets = _AssetStore(session, run_dir, cookies, page_url, cancel_event)
    used_question_ids: set[str] = set()
    questions: list[dict[str, Any]] = []
    question_controls: list[dict[str, Any]] = []
    _report_progress(progress_callback, "page_loaded", question_count=len(question_nodes))
    try:
        for number, node in enumerate(question_nodes, start=1):
            _check_cancel(cancel_event)
            question, controls = _extract_question(
                node, number, page_url, assets, used_question_ids
            )
            questions.append(question)
            question_controls.append(controls)
            _report_progress(
                progress_callback,
                "extracting_questions",
                question_count=len(questions),
                asset_count=assets.saved_count,
            )

        _check_cancel(cancel_event)
        if assets.failed_urls:
            raise CrawlError(
                f"题目中有 {len(assets.failed_urls)} 个图片资源下载失败，未生成题目数据包；"
                "请检查 Cookie、资源权限或页面访问状态后重试。"
            )

        questions_package = {
            "site_id": SITE_ID,
            "adapter_version": ADAPTER_VERSION,
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "questions": questions,
        }
        interaction_package = {
            "site_id": SITE_ID,
            "adapter_version": ADAPTER_VERSION,
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "form": _form_metadata(page),
            "question_controls": question_controls,
        }

        _check_cancel(cancel_event)
        (run_dir / "questions.json").write_text(
            json.dumps(questions_package, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (run_dir / "interaction.json").write_text(
            json.dumps(interaction_package, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except CrawlCancelled:
        assets.cleanup_created_files()
        for filename in ("questions.json", "interaction.json"):
            try:
                (run_dir / filename).unlink(missing_ok=True)
            except OSError:
                pass
        raise

    _report_progress(
        progress_callback,
        "package_written",
        question_count=len(questions),
        asset_count=assets.saved_count,
    )

    return {
        "run_id": run_id,
        "output_dir": str(run_dir.resolve()),
        "questions_path": str((run_dir / "questions.json").resolve()),
        "interaction_path": str((run_dir / "interaction.json").resolve()),
        "question_count": len(questions),
        "asset_count": assets.saved_count,
    }


def _parse_cookie_header(cookie_header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in cookie_header.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name:
            cookies[name] = value
    return cookies


def _load_chaoxing_cli_config() -> tuple[str, dict[str, str]]:
    """Load the Chaoxing URL and Cookie header from the project config file."""
    config_exists = _CONFIG_PATH.exists()
    if config_exists:
        try:
            config = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise CrawlError("config.json 无法读取或不是有效 JSON；请修复配置文件后重试。") from None
        if not isinstance(config, dict):
            raise CrawlError("config.json 顶层必须是 JSON 对象；未修改现有文件。")
    else:
        config = {}

    changed = False
    if "chaoxing" not in config:
        config["chaoxing"] = {}
        changed = True
    chaoxing = config["chaoxing"]
    if not isinstance(chaoxing, dict):
        raise CrawlError("config.json 中的 chaoxing 必须是 JSON 对象；未修改现有文件。")

    for key in ("url", "cookie"):
        if key not in chaoxing:
            chaoxing[key] = ""
            changed = True
        if not isinstance(chaoxing[key], str):
            raise CrawlError(f"config.json 中的 chaoxing.{key} 必须是字符串；未修改现有文件。")

    if changed:
        try:
            _CONFIG_PATH.write_text(
                json.dumps(config, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            raise CrawlError("无法创建或补齐 config.json；请检查用户数据目录的写入权限。") from None

    url = chaoxing["url"].strip()
    if not url:
        raise CrawlError(
            f"请在 {_CONFIG_PATH} 的 chaoxing.url 中填写作业页面 URL 后重试。"
        )

    cookie_header = chaoxing["cookie"].strip()
    return url, _parse_cookie_header(cookie_header)


def main() -> int:
    """Run the adapter using URL and Cookie values from config.json."""
    parser = argparse.ArgumentParser(
        description="从用户数据目录 config.json 读取超星 URL 和 Cookie 并生成数据包"
    )
    parser.add_argument("--run-id", help="可选的本次运行 ID")
    parser.add_argument("--output-dir", help="可选的数据包输出目录")
    args = parser.parse_args()

    try:
        ensure_user_data_migrated()
        url, cookies = _load_chaoxing_cli_config()
        result = crawl_chaoxing(
            url,
            run_id=args.run_id,
            output_dir=args.output_dir,
            cookies=cookies,
        )
    except DataMigrationError as error:
        parser.exit(1, f"用户数据迁移失败：{error}\n")
    except (CrawlError, ValueError) as error:
        parser.exit(1, f"抓取失败：{error}\n")

    print(f"抓取题数：{result['question_count']}")
    print(f"图片附件：{result['asset_count']}")
    print(f"数据包：{result['output_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
