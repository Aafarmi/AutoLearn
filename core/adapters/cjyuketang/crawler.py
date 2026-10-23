"""Scrapling crawler for Changjiang Yuketang assignment pages."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import html
from html.parser import HTMLParser
import json
import logging
import os
from pathlib import Path
import re
import threading
import tempfile
from typing import Any, Callable, Mapping
from urllib.parse import urljoin, urlsplit

try:
    from scrapling.fetchers import FetcherSession
except ImportError:  # Keep offline helpers importable when Scrapling is not installed.
    FetcherSession = None  # type: ignore[assignment,misc]

from . import SITE_ID
from core.data_migration import DataMigrationError, ensure_user_data_migrated
from core.paths import data_path


ADAPTER_VERSION = "0.1.0"
SCHEMA_VERSION = "1.0"
_CONFIG_PATH = data_path("config.json")
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HONG_KONG_TIMEZONE = timezone(timedelta(hours=8))
_ALLOWED_COOKIE_DOMAINS = ("yuketang.cn",)
_ALLOWED_ASSET_DOMAINS = ("yuketang.cn", "xuetangx.com")
_VOID_TAGS = {"br", "hr", "img"}
_EXAM_SESSION_BLOCK_HINT = "请勿同时开启多个考试页面"
_EXAM_SESSION_BLOCK_MESSAGE = (
    "考试端判定同一场考试被多个页面同时打开（页面已跳转到 /warn）；"
    "请关闭本机其它标签页、其它浏览器以及手机端打开的同一场考试，等待约 30 秒后再抓取。"
)


class CrawlError(RuntimeError):
    """Safe user-facing error that never includes page or credential data."""


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


def _is_allowed_site_host(hostname: str) -> bool:
    host = hostname.lower().rstrip(".")
    return any(host == domain or host.endswith(f".{domain}") for domain in _ALLOWED_COOKIE_DOMAINS)


def _is_allowed_asset_host(hostname: str) -> bool:
    host = hostname.lower().rstrip(".")
    return any(host == domain or host.endswith(f".{domain}") for domain in _ALLOWED_ASSET_DOMAINS)


def _validate_page_url(url: str) -> str:
    if not isinstance(url, str) or not url.strip() or any(c in url for c in "\r\n"):
        raise CrawlError("config.json 中的 cjyuketang.url 不是有效页面地址。")
    candidate = url.strip()
    try:
        parsed = urlsplit(candidate)
        host = parsed.hostname
        _ = parsed.port
    except ValueError:
        host = None
        parsed = None
    if (
        parsed is None
        or parsed.scheme.lower() != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or not _is_allowed_site_host(host)
    ):
        raise CrawlError("cjyuketang 页面必须是 yuketang.cn 及其子域名下的 HTTPS 地址。")
    return candidate


def _new_run_id() -> str:
    timestamp = datetime.now(_HONG_KONG_TIMEZONE).strftime("%Y%m%d_%H%M%S")
    base = f"{SITE_ID}_{timestamp}"
    candidate = base
    suffix = 2
    while (data_path("runs") / candidate).exists():
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def _parse_cookie_header(value: str) -> dict[str, str]:
    if not value.strip():
        return {}
    cookies: dict[str, str] = {}
    for part in value.split(";"):
        name, separator, cookie_value = part.strip().partition("=")
        if (
            not separator
            or not name
            or any(character.isspace() or character in "=," for character in name)
            or any(character in cookie_value for character in "\r\n;")
        ):
            raise CrawlError("cjyuketang.cookie 的格式无效；请使用 name=value; name2=value2。")
        cookies[name] = cookie_value
    return cookies


def _atomic_write_config(config: dict[str, Any]) -> None:
    serialized = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".config.", suffix=".tmp", dir=_CONFIG_PATH.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, _CONFIG_PATH)
    except OSError:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise CrawlError("无法安全更新 config.json；原配置保持不变。") from None


def _load_cli_config() -> tuple[str, dict[str, str]]:
    if _CONFIG_PATH.exists():
        try:
            config = json.loads(_CONFIG_PATH.read_bytes().decode("utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise CrawlError("config.json 无法读取或不是有效 UTF-8 JSON；文件未修改。") from None
        if not isinstance(config, dict):
            raise CrawlError("config.json 顶层必须是 JSON 对象；文件未修改。")
    else:
        config = {}

    changed = SITE_ID not in config
    if changed:
        section = {}
        config[SITE_ID] = section
    else:
        section = config[SITE_ID]
    if not isinstance(section, dict):
        raise CrawlError("config.json 中的 cjyuketang 必须是 JSON 对象；文件未修改。")

    for key in ("url", "cookie"):
        if key not in section:
            section[key] = ""
            changed = True
        if not isinstance(section[key], str):
            raise CrawlError(f"config.json 中的 cjyuketang.{key} 必须是字符串；文件未修改。")
    if changed:
        _atomic_write_config(config)

    url = section["url"].strip()
    if not url:
        raise CrawlError(f"请在 {_CONFIG_PATH} 的 cjyuketang.url 中填写作业页面 URL 后重试。")
    return _validate_page_url(url), _parse_cookie_header(section["cookie"])


def _text_of(element: Any) -> str:
    return " ".join(str(element.get_all_text(strip=True)).split())


def _selector_for_id(element_id: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", element_id):
        return f"#{element_id}"
    escaped = element_id.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", "\\a ").replace("\r", "\\d ")
    return f'[id="{escaped}"]'


def _same_dom_node(left: Any, right: Any) -> bool:
    left_root = getattr(left, "_root", None)
    right_root = getattr(right, "_root", None)
    if left_root is None or right_root is None:
        return False
    try:
        return left_root is right_root or left_root == right_root
    except Exception:
        return left_root is right_root


def _structural_css_selector(element: Any) -> str:
    """Build a full CSS path from verified parent/child positions."""
    parts: list[str] = []
    target = element
    try:
        while True:
            parent = getattr(target, "parent", None)
            if parent is None:
                break
            tag = str(getattr(target, "tag", "")).lower()
            if not re.fullmatch(r"[a-z][a-z0-9_-]*", tag):
                return ""
            target_root = getattr(target, "_root", None)
            if target_root is None:
                return ""
            position = 0
            found = False
            for sibling in parent.children:
                sibling_tag = str(getattr(sibling, "tag", "")).lower()
                if sibling_tag == tag:
                    position += 1
                sibling_root = getattr(sibling, "_root", None)
                try:
                    is_target = sibling_root is target_root or sibling_root == target_root
                except Exception:
                    is_target = sibling_root is target_root
                if is_target:
                    found = True
                    break
            if not found or position < 1:
                return ""
            parts.append(f"{tag}:nth-of-type({position})")
            if str(getattr(parent, "tag", "")).lower() == "html":
                break
            target = parent
    except Exception:
        return ""
    return " > ".join(reversed(parts))


def _unique_selector(
    page: Any,
    element: Any,
    *,
    fallback: str | None = None,
    description: str = "页面控件",
) -> str:
    element_id = str(element.attrib.get("id", "")).strip()
    candidates: list[str] = []
    if element_id:
        candidates.append(_selector_for_id(element_id))
    for attribute_name in ("generate_full_css_selector", "generate_css_selector"):
        try:
            generated_value = getattr(element, attribute_name, "")
            if callable(generated_value):
                generated_value = generated_value()
            generated = str(generated_value or "").strip()
        except Exception:
            generated = ""
        if generated and generated not in candidates:
            candidates.append(generated)
    structural = _structural_css_selector(element)
    if structural and structural not in candidates:
        candidates.append(structural)
    if fallback:
        candidates.append(fallback)
    for selector in candidates:
        try:
            matches = list(page.css(selector))
            if len(matches) == 1 and _same_dom_node(matches[0], element):
                return selector
        except Exception:
            continue
    raise CrawlError(f"{description}无法生成唯一 CSS 选择器；未写入数据包。")


class _SafeMarkupParser(HTMLParser):
    """Retain harmless presentation markup and localize image references."""

    _ALLOWED_TAGS = {
        "a", "abbr", "b", "blockquote", "br", "caption", "code", "dd", "del",
        "div", "dl", "dt", "em", "h1", "h2", "h3", "h4", "hr", "i", "img",
        "li", "math", "mfrac", "mi", "mn", "mo", "mrow", "msqrt", "msub",
        "msup", "mtext", "ol", "p", "q", "s", "semantics", "small", "span",
        "strong", "sub", "sup", "table", "tbody", "td", "th", "thead", "tr",
        "u", "ul",
    }
    _BLOCKED_TAGS = {"script", "style", "textarea", "iframe", "object", "embed", "svg"}
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
        if tag in self._BLOCKED_TAGS or "hidden" in attributes or hidden_style:
            if tag not in {
                "area", "base", "br", "col", "embed", "hr", "img", "input",
                "link", "meta", "param", "source", "track", "wbr",
            }:
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
            absolute = urljoin(self.base_url, source) if source else ""
            local_path = self.image_paths.get(absolute)
            if not local_path:
                return
            safe_attrs = [("src", local_path)]
        else:
            safe_attrs = []

        for key in self._SAFE_ATTRIBUTES:
            value = attributes.get(key)
            if not value:
                continue
            if key == "class":
                value = " ".join(re.findall(r"[A-Za-z0-9_-]+", value))
            if value:
                safe_attrs.append((key, value))
        rendered = "".join(
            f' {key}="{html.escape(value, quote=True)}"' for key, value in safe_attrs
        )
        self.parts.append(f"<{tag}{rendered}>")
        if tag not in _VOID_TAGS:
            self.open_tags.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_TAGS:
            self.handle_endtag(tag)

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

    def markup(self) -> str:
        while self.open_tags:
            self.parts.append(f"</{self.open_tags.pop()}>")
        return "".join(self.parts)


class _AssetStore:
    """Download images without ever sending site cookies to external hosts."""

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
        cookies: Mapping[str, str],
        page_url: str,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.session = session
        self.run_dir = run_dir
        self.cookies = dict(cookies)
        self.page_url = page_url
        self.cancel_event = cancel_event
        self.cache: dict[str, str | None] = {}
        self.failed_urls: set[str] = set()
        self.created_files: list[Path] = []

    def _mark_failed(self, url: str) -> None:
        self.cache[url] = None
        self.failed_urls.add(url)

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
        absolute = urljoin(self.page_url, image_url)
        if absolute in self.cache:
            return self.cache[absolute]
        try:
            parsed = urlsplit(absolute)
            host = parsed.hostname or ""
            port = parsed.port
        except ValueError:
            self._mark_failed(absolute)
            return None
        if (
            parsed.scheme.lower() != "https"
            or not host
            or parsed.username
            or parsed.password
            or port not in {None, 443}
            or not _is_allowed_asset_host(host)
        ):
            self._mark_failed(absolute)
            return None

        page = urlsplit(self.page_url)
        origin_host = page.hostname or ""
        referer_host = origin_host
        if ":" in referer_host and not referer_host.startswith("["):
            referer_host = f"[{referer_host}]"
        if page.port and page.port != 443:
            referer_host = f"{referer_host}:{page.port}"
        request: dict[str, Any] = {
            "follow_redirects": False,
            "headers": {"Referer": f"https://{referer_host}/"},
        }
        if _is_allowed_site_host(host):
            request["cookies"] = self.cookies or None
        try:
            response = self.session.get(absolute, **request)
            _check_cancel(self.cancel_event)
            status = int(getattr(response, "status", 0))
            if not 200 <= status < 300:
                self._mark_failed(absolute)
                return None
            body = response.body
            if not isinstance(body, bytes):
                body = bytes(body)
            headers = getattr(response, "headers", {}) or {}
            content_type = str(headers.get("content-type") or headers.get("Content-Type") or "")
            extension = self._extension(content_type, body)
            if not extension:
                self._mark_failed(absolute)
                return None
        except CrawlCancelled:
            raise
        except Exception:
            self._mark_failed(absolute)
            return None

        digest = hashlib.sha256(absolute.encode("utf-8")).hexdigest()[:16]
        filename = f"image-{digest}{extension}"
        path = self.run_dir / "assets" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            try:
                if path.read_bytes() == body:
                    local_path = f"assets/{filename}"
                    self.cache[absolute] = local_path
                    return local_path
            except OSError:
                pass
            self._mark_failed(absolute)
            return None
        path.write_bytes(body)
        self.created_files.append(path)
        local_path = f"assets/{filename}"
        self.cache[absolute] = local_path
        return local_path

    @property
    def saved_count(self) -> int:
        return sum(path is not None for path in self.cache.values())

    def cleanup_created_files(self) -> None:
        for path in self.created_files:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


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
    for image_url in _image_sources(element, page_url):
        local_path = assets.save(image_url)
        if local_path:
            image_paths[image_url] = local_path
    parser = _SafeMarkupParser(image_paths, page_url)
    parser.feed(str(getattr(element, "html_content", "") or ""))
    parser.close()
    return {
        "text": _text_of(element),
        "html": parser.markup(),
        "asset_paths": list(dict.fromkeys(image_paths.values())),
    }


def _content_has_payload(content: Mapping[str, Any]) -> bool:
    if str(content.get("text", "")).strip() or content.get("asset_paths"):
        return True
    markup = str(content.get("html", ""))
    text = html.unescape(re.sub(r"<[^>]*>", " ", markup))
    return bool(text.strip())


def _type_label(question: Any) -> tuple[str, int | None]:
    nodes = question.css(".item-type")
    if not nodes:
        raise CrawlError("某道题缺少题型标签；页面结构与当前适配器不匹配。")
    raw = _text_of(nodes[0])
    number_match = re.match(r"^\s*(\d+)\s*[.、．)]", raw)
    number = int(number_match.group(1)) if number_match else None
    if any(
        token in raw
        for token in ("多选", "多項選擇", "多项选择", "多選", "不定项选择", "不定項選擇")
    ):
        return "多选题", number
    if any(token in raw for token in ("单选", "单项选择", "單選", "單項選擇")):
        return "单选题", number
    if "判断" in raw:
        return "判断题", number
    if "填空" in raw or "计算" in raw:
        return "填空题", number
    if any(token in raw for token in ("简答", "问答", "论述", "主观")):
        return "简答题", number
    if "选择" in raw:
        return "选择题", number
    raise CrawlError("发现未支持的题型标签；页面未生成数据包。")


def _is_hidden_control(control: Any) -> bool:
    attrs = control.attrib
    if "hidden" in attrs:
        return True
    style = str(attrs.get("style", ""))
    return bool(
        re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", style, re.IGNORECASE)
    )


_EDITOR_MARKERS = (
    (".edui-editor", "UEditor"),
    (".ql-editor", "Quill"),
    (".tox-tinymce", "TinyMCE"),
    (".wangEditor", "wangEditor"),
)


def _rich_text_iframes(question: Any) -> list[Any]:
    """UEditor keeps its editable body inside a same-origin child iframe."""
    return [
        node
        for node in question.css("iframe[id^='ueditor_']")
        if not _is_hidden_control(node)
    ]


def _missing_control_message(number: int, question: Any) -> str:
    """Explain why no writable answer control was found, without guessing the page state."""
    for selector, name in _EDITOR_MARKERS:
        if not question.css(selector):
            continue
        if name == "UEditor":
            return (
                f"第 {number} 题使用 UEditor 富文本编辑器，但未找到唯一的可编辑 iframe；"
                "无法记录答题区域。"
            )
        return (
            f"第 {number} 题使用了当前适配器不支持的富文本编辑器（{name}），"
            "无法记录答题区域。"
        )
    return f"第 {number} 题未找到可记录的答题控件。"


def _text_controls(question: Any) -> list[Any]:
    body_nodes = question.css(".item-body")
    scope = body_nodes[0] if body_nodes else question
    selector_groups = (
        (".blank-input",),
        ('[contenteditable="true"]', '[contenteditable="plaintext-only"]', '[contenteditable=""]'),
        ("textarea",),
        ('input[type="text"]', "input:not([type])"),
    )
    controls: list[Any] = []
    for selector_group in selector_groups:
        candidates = [node for selector in selector_group for node in scope.css(selector)]
        candidates = [node for node in candidates if not _is_hidden_control(node)]
        if candidates:
            controls = candidates
            break
    if not controls:
        editors = _rich_text_iframes(question)
        if len(editors) > 1:
            raise CrawlError("该题检测到多个富文本编辑器，无法确定唯一的答题区域。")
        controls = editors

    unique: list[Any] = []
    seen: set[str] = set()
    for control in controls:
        attrs = control.attrib
        tag = str(getattr(control, "tag", "")).lower()
        if tag == "input" and str(attrs.get("type", "text")).lower() not in {"text", ""}:
            continue
        selector = ""
        for attribute_name in ("generate_full_css_selector", "generate_css_selector"):
            try:
                selector_value = getattr(control, attribute_name, "")
                if callable(selector_value):
                    selector_value = selector_value()
                selector = str(selector_value or "").strip()
            except Exception:
                selector = ""
            if selector:
                break
        key = selector or f"{tag}:{attrs.get('id', '')}:{attrs.get('name', '')}:{len(unique)}"
        if key not in seen:
            seen.add(key)
            unique.append(control)
    return unique


def _option_nodes(question: Any, question_type: str) -> list[Any]:
    selectors = (
        (".el-checkbox", '[role="checkbox"]', 'input[type="checkbox"]')
        if question_type == "multiple_choice"
        else (".el-radio", '[role="radio"]', 'input[type="radio"]')
    )
    for selector in selectors:
        nodes = list(question.css(selector))
        if not nodes:
            continue
        if selector.startswith("input"):
            wrapped: list[Any] = []
            for node in nodes:
                ancestor = getattr(node, "parent", None)
                list_item = None
                option_wrapper = None
                for _ in range(12):
                    if ancestor is None:
                        break
                    attrs = ancestor.attrib
                    tag = str(getattr(ancestor, "tag", "")).lower()
                    classes = set(str(attrs.get("class", "")).lower().split())
                    if "exercise-item" in classes:
                        break
                    if tag == "label" or classes & {
                        "option", "option-item", "option-wrapper", "answer-option",
                    }:
                        option_wrapper = ancestor
                        break
                    if tag == "li" and list_item is None:
                        list_item = ancestor
                    ancestor = getattr(ancestor, "parent", None)
                wrapped.append(option_wrapper or list_item or node)
            return wrapped
        return nodes
    return []


def _normalize_question_type(source_type: str, question: Any, controls: list[Any]) -> str:
    if source_type == "多选题":
        return "multiple_choice"
    if source_type in {"单选题", "判断题"}:
        return "single_choice"
    if source_type == "填空题":
        return "fill_blank"
    if source_type == "简答题":
        return "short_answer"

    if source_type == "选择题":
        has_checkboxes = len(
            question.css(".el-checkbox, [role='checkbox'], input[type='checkbox']")
        ) > 0
        has_radios = len(question.css(".el-radio, [role='radio'], input[type='radio']")) > 0
        if has_checkboxes and not has_radios:
            return "multiple_choice"
        if has_radios and not has_checkboxes:
            return "single_choice"
        raise CrawlError("页面只标记为选择题，且控件无法唯一判定单选或多选。")

    if controls:
        return "fill_blank"
    raise CrawlError("题型未能根据页面标签或答题控件确定；页面未生成数据包。")


def _option_label(option: Any, ordinal: int) -> str:
    for selector in (".radioInput", ".checkboxInput", ".option-label"):
        nodes = option.css(selector)
        if nodes:
            label = _text_of(nodes[0]).strip().rstrip(".、．)")
            if label and len(label) <= 12:
                return label
    return f"选项{ordinal}"


def _option_content_node(option: Any) -> Any:
    for selector in (".radioText", ".checkboxText", ".option-content", ".el-radio__label", ".el-checkbox__label"):
        nodes = option.css(selector)
        if nodes:
            return nodes[0]
    return option


def _option_icon_text(option: Any) -> str:
    """Convert icon-only choice labels into readable text symbols."""
    content_classes = {
        "radioInput", "checkboxInput", "radioText", "checkboxText",
        "option-content", "el-radio__label", "el-checkbox__label",
    }
    icon_symbols = {"el-icon-check": "✓", "el-icon-close": "×"}
    symbols: list[str] = []
    for icon in [*option.css("i"), *option.css("span")]:
        classes = set(str(icon.attrib.get("class", "")).split())
        symbol = next(
            (value for class_name, value in icon_symbols.items() if class_name in classes),
            None,
        )
        if not symbol:
            continue
        ancestor = getattr(icon, "parent", None)
        while ancestor is not None:
            ancestor_classes = set(
                str(getattr(ancestor, "attrib", {}).get("class", "")).split()
            )
            if ancestor_classes & content_classes:
                symbols.append(symbol)
                break
            ancestor = getattr(ancestor, "parent", None)
    return "".join(symbols)


def _option_content(
    option: Any, page_url: str, assets: _AssetStore
) -> dict[str, Any]:
    content = _content(_option_content_node(option), page_url, assets)
    icon_text = _option_icon_text(option)
    if icon_text:
        if not str(content["text"]).strip():
            content["text"] = icon_text
        if icon_text not in content["html"]:
            content["html"] += f"<span>{html.escape(icon_text)}</span>"
    known_paths = set(content["asset_paths"])
    rendered_paths = {
        path
        for path in known_paths
        if f'src="{html.escape(path, quote=True)}"' in content["html"]
    }
    additional_markup: list[str] = []
    for image_url in _image_sources(option, page_url):
        local_path = assets.save(image_url)
        if not local_path:
            continue
        if local_path not in known_paths:
            known_paths.add(local_path)
            content["asset_paths"].append(local_path)
        if local_path not in rendered_paths:
            rendered_paths.add(local_path)
            additional_markup.append(f'<img src="{html.escape(local_path, quote=True)}">')
    if additional_markup:
        content["html"] += "".join(additional_markup)
    return content


def _choice_is_selected(option: Any) -> bool:
    attrs = option.attrib
    if str(attrs.get("aria-checked", "")).lower() == "true":
        return True
    if str(attrs.get("aria-selected", "")).lower() == "true":
        return True
    if "checked" in attrs:
        return True
    classes = set(str(attrs.get("class", "")).lower().split())
    if classes & {"is-checked", "selected", "checked", "active", "current"}:
        return True
    return bool(option.css('input[checked], input[aria-checked="true"]'))


def _control_value(control: Any) -> str:
    attrs = control.attrib
    tag = str(getattr(control, "tag", "")).lower()
    if tag == "input":
        return str(attrs.get("value", ""))
    return _text_of(control)


def _control_kind(control: Any) -> str:
    attrs = control.attrib
    if str(getattr(control, "tag", "")).lower() == "iframe":
        return "rich_text_editor"
    if (
        str(attrs.get("contenteditable", "")).lower()
        in {"true", "plaintext-only", ""}
        and "contenteditable" in attrs
    ):
        return "rich_text_editor"
    if _is_hidden_control(control):
        raise CrawlError("发现隐藏答题控件，且没有同题内唯一可操作的编辑区。")
    return "text_input"


_OPTION_CONTAINER_HINTS = ("list-unstyled", "el-radio-group", "el-checkbox-group")
_STEM_SKIP_TAGS = {"ul", "ol", "script", "style", "iframe", "textarea", "input", "label", "button"}


def _node_has_content(node: Any) -> bool:
    if _text_of(node).strip():
        return True
    return bool(node.css("img"))


def _is_stem_candidate(node: Any) -> bool:
    """A direct .item-body child that can carry the stem (not the option list)."""
    if getattr(node, "tag", "") in _STEM_SKIP_TAGS:
        return False
    classes = str(node.attrib.get("class", ""))
    if any(hint in classes for hint in _OPTION_CONTAINER_HINTS):
        return False
    if node.css("ul.list-unstyled, .list-unstyled-radio, .list-unstyled-checkbox"):
        return False
    return _node_has_content(node)


def _stem_structure_hint(question: Any) -> str:
    """Tag/class fingerprint of the stem container, safe to log (never the text)."""
    parts: list[str] = []
    for child in question.css(".item-body > *")[:6]:
        tag = getattr(child, "tag", "?")
        classes = ".".join(str(child.attrib.get("class", "")).split())
        parts.append(f"{tag}.{classes}" if classes else tag)
    return " / ".join(parts) or "没有 .item-body 子节点"


def _stem_nodes(question: Any) -> list[Any]:
    nodes = list(question.css(".item-body h4 .custom_ueditor_cn_body"))
    if nodes:
        return nodes
    nodes = list(question.css(".item-body .custom_ueditor_cn_body"))
    if nodes:
        return nodes
    nodes = [node for node in question.css(".item-body h4") if _node_has_content(node)]
    if nodes:
        return nodes
    # 有的试卷（如客观题卷）把题干放在 h4 之外的直接子节点里，h4 本身是空的。
    nodes = [child for child in question.css(".item-body > *") if _is_stem_candidate(child)]
    if nodes:
        return nodes
    body = question.css(".item-body")
    return list(body[:1])


def _safe_question_id(number: int, used: set[str]) -> str:
    candidate = f"question-{number:03d}"
    base = candidate
    suffix = 2
    while candidate in used:
        extra = f"-{suffix}"
        candidate = f"{base[:128-len(extra)]}{extra}"
        suffix += 1
    used.add(candidate)
    return candidate


def _extract_question(
    question: Any,
    ordinal: int,
    page: Any,
    page_url: str,
    assets: _AssetStore,
    used_question_ids: set[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    source_type, original_number = _type_label(question)
    number = original_number if original_number and original_number > 0 else ordinal
    controls = _text_controls(question)
    question_type = _normalize_question_type(source_type, question, controls)
    question_selector = _unique_selector(
        page,
        question,
        fallback=f".exercise-item:nth-of-type({ordinal})",
        description=f"第 {number} 题的题目容器",
    )
    question_id = _safe_question_id(number, used_question_ids)

    stem_nodes = _stem_nodes(question)
    if not stem_nodes:
        raise CrawlError(f"第 {number} 题未找到题干内容节点。")
    stem_parts = [_content(node, page_url, assets) for node in stem_nodes]
    stem = {
        "text": "\n".join(part["text"] for part in stem_parts if part["text"]),
        "html": "\n".join(part["html"] for part in stem_parts if part["html"]),
        "asset_paths": list(dict.fromkeys(path for part in stem_parts for path in part["asset_paths"])),
    }

    option_nodes = _option_nodes(question, question_type) if question_type in {"single_choice", "multiple_choice"} else []
    if question_type in {"single_choice", "multiple_choice"} and not option_nodes:
        raise CrawlError(f"第 {number} 题未找到与题型匹配的选项控件。")
    if question_type in {"fill_blank", "short_answer"} and not controls:
        raise CrawlError(_missing_control_message(number, question))
    if question_type == "short_answer" and len(controls) != 1:
        raise CrawlError(f"第 {number} 题的简答控件数量不唯一。")

    options: list[dict[str, Any]] = []
    choice_controls: list[dict[str, str]] = []
    selected_option_ids: list[str] = []
    for option_ordinal, option in enumerate(option_nodes, start=1):
        option_id = f"option-{option_ordinal:03d}"
        label = _option_label(option, option_ordinal)
        content = _option_content(option, page_url, assets)
        if not label:
            raise CrawlError(f"第 {number} 题第 {option_ordinal} 个选项缺少标签。")
        options.append({"option_id": option_id, "label": label, "content": content})
        choice_controls.append(
            {
                "option_id": option_id,
                "selector": _unique_selector(
                    page,
                    option,
                    description=f"第 {number} 题第 {option_ordinal} 个选项",
                ),
            }
        )
        if _choice_is_selected(option):
            selected_option_ids.append(option_id)

    if question_type == "single_choice" and len(selected_option_ids) > 1:
        raise CrawlError(f"第 {number} 题有多个当前选项，不能记录为单选题。")
    if question_type in {"single_choice", "multiple_choice"} and len(options) < 2:
        raise CrawlError(f"第 {number} 题识别到的选项少于两个。")

    answer_slots: list[dict[str, Any]] = []
    answer_slot_controls: list[dict[str, Any]] = []
    for slot_ordinal, control in enumerate(controls, start=1):
        slot_id = f"blank-{slot_ordinal:03d}"
        kind = _control_kind(control)
        control_selector = _unique_selector(
            page,
            control,
            description=f"第 {number} 题第 {slot_ordinal} 个答题控件",
        )
        answer_slots.append(
            {"slot_id": slot_id, "ordinal": slot_ordinal, "input_kind": kind}
        )
        slot_control: dict[str, Any] = {
            "slot_id": slot_id,
            "selector": control_selector,
            "control_type": kind,
            "current_value": _control_value(control),
        }
        if str(getattr(control, "tag", "")).lower() == "iframe":
            # The editable body lives inside this iframe, not in the outer document.
            slot_control["frame_selector"] = control_selector
        answer_slot_controls.append(slot_control)

    if question_type == "fill_blank" and not answer_slots:
        raise CrawlError(f"第 {number} 题未识别到填空位置。")
    if question_type in {"single_choice", "multiple_choice"} and answer_slots:
        raise CrawlError(f"第 {number} 题同时包含选择项与文本空位，需单独确认页面结构。")

    package_question = {
        "question_id": question_id,
        "number": number,
        "question_type": question_type,
        "source_type": source_type,
        "stem": stem,
        "options": options,
        "answer_slots": answer_slots,
    }
    interaction_question: dict[str, Any] = {
        "question_id": question_id,
        "question_type": question_type,
        "question_selector": question_selector,
        "choice_options": choice_controls,
        "answer_slots": answer_slot_controls,
    }
    if question_type == "single_choice":
        interaction_question["selected_option_id"] = (
            selected_option_ids[0] if selected_option_ids else None
        )
    elif question_type == "multiple_choice":
        interaction_question["selected_option_ids"] = selected_option_ids
    return package_question, interaction_question


def _page_selector(page: Any) -> str:
    for selector in (".exam-main--content", ".exam-main", "body"):
        try:
            if len(page.css(selector)) == 1:
                return selector
        except Exception:
            continue
    raise CrawlError("页面缺少唯一的题目内容容器。")


def _safe_image_session(session: Any, asset_session: Any, cookies: Mapping[str, str]) -> None:
    if cookies and session is asset_session:
        raise CrawlError("提供 Cookie 时，页面请求与图片请求必须使用独立会话。")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    serialized = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise CrawlError("数据包写入失败；请检查输出目录权限。") from None


def _prepare_run_dir(run_id: str, output_dir: str | Path | None) -> Path:
    run_dir = Path(output_dir).expanduser() if output_dir is not None else data_path("runs", run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    if (run_dir / "questions.json").exists() or (run_dir / "interaction.json").exists():
        raise CrawlError("输出目录已有数据包文件；请指定新的 run_id 或 output_dir。")
    (run_dir / "assets").mkdir(parents=True, exist_ok=True)
    return run_dir


def _blocked_page_reason(page: Any) -> str | None:
    """Explain an exam-site interstitial page (session conflict / anti-cheat)."""
    path = urlsplit(str(getattr(page, "url", "") or "")).path
    if path.rstrip("/").endswith("/warn"):
        return _EXAM_SESSION_BLOCK_MESSAGE
    try:
        html = getattr(page, "html_content", "") or ""
    except Exception:
        html = ""
    if _EXAM_SESSION_BLOCK_HINT in html:
        return _EXAM_SESSION_BLOCK_MESSAGE
    return None


def _browser_cookies(url: str, cookies: Mapping[str, str]) -> list[dict[str, Any]]:
    browser_cookies: list[dict[str, Any]] = []
    host = urlsplit(url).hostname
    if cookies and (not host or not _is_allowed_site_host(host)):
        raise CrawlError("浏览器 Cookie 只能用于 yuketang.cn 及其子域名。")
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
        browser_cookies.append(cookie)
    return browser_cookies


def _probe_blocked_page(url: str, browser_cookies: list[dict[str, Any]]) -> str | None:
    """Best-effort second load that explains a failed render (no wait selector)."""
    try:
        from scrapling.fetchers import DynamicFetcher
    except ImportError:
        return None
    try:
        page = DynamicFetcher.fetch(
            url,
            cookies=browser_cookies or None,
            headless=True,
            google_search=False,
            network_idle=True,
            timeout=45_000,
        )
    except Exception:
        return None
    return _blocked_page_reason(page)


def _fetch_dynamic_page(
    url: str,
    cookies: Mapping[str, str],
    cancel_event: threading.Event | None = None,
) -> Any:
    _check_cancel(cancel_event)
    try:
        from scrapling.fetchers import DynamicFetcher
    except ImportError:
        raise CrawlError(
            "静态响应没有题目容器；需要浏览器方式加载，但当前 Python 环境缺少 Scrapling 浏览器依赖。"
            "请在运行项目的同一解释器中安装 scrapling[fetchers] 并运行 scrapling install。"
        ) from None

    browser_cookies = _browser_cookies(url, cookies)

    previous_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        page = DynamicFetcher.fetch(
            url,
            cookies=browser_cookies or None,
            headless=True,
            google_search=False,
            network_idle=False,
            timeout=60_000,
            wait_selector=".exercise-item",
        )
        _check_cancel(cancel_event)
        blocked = _blocked_page_reason(page)
        if blocked:
            raise CrawlError(blocked)
        return page
    except CrawlCancelled:
        raise
    except CrawlError:
        raise
    except Exception as error:
        blocked = _probe_blocked_page(url, browser_cookies)
        if blocked:
            raise CrawlError(blocked) from None
        error_type = type(error).__name__
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,63}", error_type):
            error_type = "UnknownError"
        raise CrawlError(
            f"浏览器方式加载雨课堂页面失败（异常类型：{error_type}）；"
            "未输出错误详情或 Cookie，请把异常类型发回以便继续定位。"
        ) from None
    finally:
        logging.disable(previous_disable)


def _crawl_with_sessions(
    url: str,
    run_id: str,
    run_dir: Path,
    cookies: Mapping[str, str],
    page_session: Any,
    asset_session: Any,
    cancel_event: threading.Event | None = None,
    progress_callback: Callable[..., None] | None = None,
) -> dict[str, Any]:
    _safe_image_session(page_session, asset_session, cookies)
    _report_progress(progress_callback, "requesting_page")
    _check_cancel(cancel_event)
    try:
        page = page_session.get(url, cookies=dict(cookies) if cookies else None)
    except Exception:
        raise CrawlError("作业页面请求失败；请检查网络、URL 和 Cookie 配置。") from None
    _check_cancel(cancel_event)
    status = int(getattr(page, "status", 0) or 0)
    if not 200 <= status < 300:
        raise CrawlError(f"作业页面请求失败（HTTP {status}）；请检查网络和登录状态。")
    blocked = _blocked_page_reason(page)
    if blocked:
        raise CrawlError(blocked)

    question_nodes = list(page.css(".exercise-item"))
    if not question_nodes:
        _report_progress(progress_callback, "loading_dynamic_page")
        page = _fetch_dynamic_page(url, cookies, cancel_event)
        status = int(getattr(page, "status", 0) or 0)
        if not 200 <= status < 300:
            raise CrawlError(f"浏览器方式加载作业页面失败（HTTP {status}）。")
        blocked = _blocked_page_reason(page)
        if blocked:
            raise CrawlError(blocked)
        question_nodes = list(page.css(".exercise-item"))
        if not question_nodes:
            raise CrawlError(
                "静态和浏览器加载后都没有识别到题目容器；"
                "请确认 URL 指向已打开的答题页面，并确认没有其它页面同时打开同一场考试。"
            )
    page_url = _validate_page_url(str(getattr(page, "url", "") or url))
    page_selector = _page_selector(page)
    asset_store = _AssetStore(asset_session, run_dir, cookies, page_url, cancel_event)
    questions: list[dict[str, Any]] = []
    interaction_questions: list[dict[str, Any]] = []
    used_question_ids: set[str] = set()
    type_counts: Counter[str] = Counter()
    _report_progress(progress_callback, "page_loaded", question_count=len(question_nodes))
    try:
        for ordinal, node in enumerate(question_nodes, start=1):
            _check_cancel(cancel_event)
            question, interaction_question = _extract_question(
                node,
                ordinal,
                page,
                page_url,
                asset_store,
                used_question_ids,
            )
            questions.append(question)
            interaction_questions.append(interaction_question)
            type_counts[question["question_type"]] += 1
            _report_progress(
                progress_callback,
                "extracting_questions",
                question_count=len(questions),
                asset_count=asset_store.saved_count,
            )
        _check_cancel(cancel_event)
        if asset_store.failed_urls:
            raise CrawlError(
                f"{len(asset_store.failed_urls)} 张题目图片下载失败或格式不受支持；未生成数据包。"
            )
        for index, question in enumerate(questions):
            number = question["number"]
            if not _content_has_payload(question["stem"]):
                raise CrawlError(
                    f"第 {number} 题的题干内容为空（题干容器：{_stem_structure_hint(question_nodes[index])}）；"
                    "页面未生成数据包。"
                )
            for option_ordinal, option in enumerate(question["options"], start=1):
                if not _content_has_payload(option["content"]):
                    raise CrawlError(
                        f"第 {number} 题第 {option_ordinal} 个选项内容为空；页面未生成数据包。"
                    )
        if len(questions) != len(question_nodes):
            raise CrawlError("题目提取数量校验失败；未生成数据包。")

        metadata = {
            "site_id": SITE_ID,
            "adapter_version": ADAPTER_VERSION,
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
        }
        question_package = {**metadata, "questions": questions}
        interaction_package = {
            **metadata,
            "page": {"selector": page_selector},
            "question_controls": interaction_questions,
        }
        _atomic_write_json(run_dir / "questions.json", question_package)
        _atomic_write_json(run_dir / "interaction.json", interaction_package)
    except Exception:
        for filename in ("questions.json", "interaction.json"):
            try:
                (run_dir / filename).unlink(missing_ok=True)
            except OSError:
                pass
        asset_store.cleanup_created_files()
        raise

    expected_assets = {
        path
        for question in questions
        for content in [question["stem"], *(option["content"] for option in question["options"])]
        for path in content["asset_paths"]
    }
    missing_assets = [
        path for path in expected_assets if not (run_dir / Path(*path.split("/"))).is_file()
    ]
    if missing_assets:
        (run_dir / "questions.json").unlink(missing_ok=True)
        (run_dir / "interaction.json").unlink(missing_ok=True)
        asset_store.cleanup_created_files()
        raise CrawlError("数据包附件校验失败；未保留不完整数据包。")

    _report_progress(
        progress_callback,
        "package_written",
        question_count=len(questions),
        asset_count=asset_store.saved_count,
    )

    return {
        "run_id": run_id,
        "output_dir": str(run_dir.resolve()),
        "questions_path": str((run_dir / "questions.json").resolve()),
        "interaction_path": str((run_dir / "interaction.json").resolve()),
        "question_count": len(questions),
        "type_counts": dict(sorted(type_counts.items())),
        "asset_count": asset_store.saved_count,
    }


def crawl_cjyuketang(
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
    asset_session: Any | None = None,
) -> dict[str, Any]:
    """Fetch a Yuketang question page, using browser rendering when static HTML is insufficient."""
    if task_id is not None and not _ID_PATTERN.fullmatch(task_id):
        raise ValueError("task_id has an invalid format")
    if model_profile_id is not None and not _ID_PATTERN.fullmatch(model_profile_id):
        raise ValueError("model_profile_id has an invalid format")
    _check_cancel(cancel_event)
    if cookies is None and credential_provider is not None:
        cookies = credential_provider.get_site_cookies(SITE_ID)
    safe_url = _validate_page_url(url)
    selected_run_id = run_id or _new_run_id()
    if not _ID_PATTERN.fullmatch(selected_run_id):
        raise ValueError("run_id must match the adapter ID format")
    cookie_values = dict(cookies or {})
    if session is not None and cookie_values and asset_session is None:
        raise CrawlError("提供 Cookie 时还需指定独立的 asset_session。")
    if session is not None and cookie_values and session is asset_session:
        raise CrawlError("提供 Cookie 时，页面请求与图片请求必须使用独立会话。")
    if session is None and FetcherSession is None:
        raise CrawlError("当前 Python 环境未安装 Scrapling；请在项目运行环境中安装 scrapling。")
    run_dir = _prepare_run_dir(selected_run_id, output_dir)

    if session is not None:
        if asset_session is None:
            asset_session = session
        return _crawl_with_sessions(
            safe_url,
            selected_run_id,
            run_dir,
            cookie_values,
            session,
            asset_session,
            cancel_event,
            progress_callback,
        )
    try:
        with FetcherSession(
            timeout=30, stealthy_headers=False, follow_redirects=False
        ) as page_session:
            if asset_session is not None:
                return _crawl_with_sessions(
                    safe_url,
                    selected_run_id,
                    run_dir,
                    cookie_values,
                    page_session,
                    asset_session,
                    cancel_event,
                    progress_callback,
                )
            with FetcherSession(
                timeout=30, stealthy_headers=False, follow_redirects=False
            ) as anonymous_asset_session:
                return _crawl_with_sessions(
                    safe_url,
                    selected_run_id,
                    run_dir,
                    cookie_values,
                    page_session,
                    anonymous_asset_session,
                    cancel_event,
                    progress_callback,
                )
    except (CrawlError, CrawlCancelled):
        raise
    except Exception:
        raise CrawlError("抓取过程中发生错误；Cookie 和页面地址未输出。") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="从用户数据目录 config.json 读取长江雨课堂 URL 和 Cookie 并生成题包"
    )
    parser.add_argument("--run-id", help="可选的本次运行 ID")
    parser.add_argument("--output-dir", help="可选的数据包输出目录")
    args = parser.parse_args(argv)

    try:
        ensure_user_data_migrated()
        url, cookies = _load_cli_config()
        result = crawl_cjyuketang(
            url,
            run_id=args.run_id,
            output_dir=args.output_dir,
            cookies=cookies,
        )
    except DataMigrationError as error:
        parser.exit(1, f"用户数据迁移失败：{error}\n")
    except (CrawlError, ValueError) as error:
        parser.exit(1, f"抓取失败：{error}\n")
    except Exception:
        parser.exit(1, "抓取失败：发生未分类错误；未输出 Cookie、题目正文或请求 URL。\n")

    print(f"抓取题数：{result['question_count']}")
    print(f"题型统计：{result['type_counts']}")
    print(f"图片附件：{result['asset_count']}")
    print(f"数据包：{result['output_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
