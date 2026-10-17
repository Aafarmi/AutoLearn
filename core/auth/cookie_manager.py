"""Capture site cookies in a throw-away Chrome profile.

This utility is intentionally separate from crawlers. A user runs it for a
specific site adapter; after the adapter confirms an authenticated page, the
utility writes that adapter's configured cookie field in the per-user config.json.

Every capture launches a brand-new browser: the profile lives in a temporary
directory that is deleted when the capture ends, so no cookie, login or storage
state survives between runs.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import threading
import time
from pathlib import Path
from types import ModuleType
from typing import Any, Callable
from urllib.parse import urlsplit

from core.data_migration import DataMigrationError, ensure_user_data_migrated
from core.paths import data_path, resource_path


ADAPTERS_ROOT = resource_path("core", "adapters")
CONFIG_PATH = data_path("config.json")
_SITE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_CONFIG_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


class CookieManagerError(RuntimeError):
    """A safe, user-facing cookie-manager error."""


class CookieCaptureCancelled(CookieManagerError):
    """Raised when a UI caller cancels an in-progress authentication capture."""


def _available_adapters() -> list[str]:
    try:
        return sorted(
            entry.name
            for entry in ADAPTERS_ROOT.iterdir()
            if entry.is_dir()
            and _SITE_ID_PATTERN.fullmatch(entry.name)
            and (entry / "auth.py").is_file()
        )
    except OSError:
        return []


def _load_adapter(site_id: str) -> ModuleType:
    if not _SITE_ID_PATTERN.fullmatch(site_id):
        raise CookieManagerError("适配器 ID 格式无效。")
    adapter_dir = (ADAPTERS_ROOT / site_id).resolve()
    if ADAPTERS_ROOT.resolve() not in adapter_dir.parents:
        raise CookieManagerError("适配器目录无效。")
    if not (adapter_dir / "auth.py").is_file():
        available = _available_adapters()
        suffix = f"；可用适配器：{', '.join(available)}" if available else ""
        raise CookieManagerError(f"适配器 {site_id} 尚未提供 Cookie 管理配置{suffix}。")

    try:
        adapter = importlib.import_module(f"core.adapters.{site_id}.auth")
    except Exception:
        raise CookieManagerError("无法加载该适配器的认证配置。") from None

    required = (
        "SITE_ID",
        "CONFIG_SECTION",
        "URL_FIELD",
        "COOKIE_FIELD",
        "ALLOWED_COOKIE_DOMAINS",
        "is_authenticated",
        "serialize_cookies",
    )
    if any(not hasattr(adapter, name) for name in required):
        raise CookieManagerError("适配器认证配置不完整。")
    if adapter.SITE_ID != site_id:
        raise CookieManagerError("适配器认证配置中的 site_id 不匹配。")
    for name in ("CONFIG_SECTION", "URL_FIELD", "COOKIE_FIELD"):
        value = getattr(adapter, name)
        if not isinstance(value, str) or not _CONFIG_KEY_PATTERN.fullmatch(value):
            raise CookieManagerError("适配器配置字段名称无效。")
    domains = adapter.ALLOWED_COOKIE_DOMAINS
    if (
        not isinstance(domains, (tuple, list))
        or not domains
        or any(not isinstance(domain, str) or not domain.strip() for domain in domains)
    ):
        raise CookieManagerError("适配器 Cookie 域名配置无效。")
    if not callable(adapter.is_authenticated) or not callable(adapter.serialize_cookies):
        raise CookieManagerError("适配器认证处理函数无效。")
    return adapter


def _normalized_domain(domain: str) -> str:
    return domain.strip().lower().lstrip(".").rstrip(".")


def _host_is_allowed(host: str, allowed_domains: tuple[str, ...] | list[str]) -> bool:
    normalized_host = _normalized_domain(host)
    return any(
        normalized_host == allowed
        or normalized_host.endswith(f".{allowed}")
        for allowed in (_normalized_domain(domain) for domain in allowed_domains)
    )


def _validate_target_url(url: Any, adapter: ModuleType) -> str:
    if not isinstance(url, str) or not url.strip():
        raise CookieManagerError(
            f"请先在 config.json 的 {adapter.CONFIG_SECTION}.{adapter.URL_FIELD} 中填写页面地址。"
        )
    try:
        parsed = urlsplit(url.strip())
        host = parsed.hostname
    except ValueError:
        host = None
        parsed = None
    if (
        parsed is None
        or parsed.scheme.lower() != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or not _host_is_allowed(host, adapter.ALLOWED_COOKIE_DOMAINS)
    ):
        raise CookieManagerError("页面地址必须是该适配器允许域名下的 HTTPS 地址。")
    return url.strip()


def _read_config() -> tuple[dict[str, Any], bytes]:
    try:
        raw = CONFIG_PATH.read_bytes()
    except FileNotFoundError:
        raise CookieManagerError("用户数据目录缺少 config.json；请先创建站点配置。") from None
    except OSError:
        raise CookieManagerError("无法读取用户数据目录的 config.json。") from None

    try:
        config = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CookieManagerError("config.json 不是有效的 UTF-8 JSON；文件未修改。") from None
    if not isinstance(config, dict):
        raise CookieManagerError("config.json 顶层必须是 JSON 对象；文件未修改。")
    return config, raw


def _adapter_config(config: dict[str, Any], adapter: ModuleType) -> dict[str, Any]:
    section = config.get(adapter.CONFIG_SECTION)
    if not isinstance(section, dict):
        raise CookieManagerError(
            f"config.json 中的 {adapter.CONFIG_SECTION} 必须是对象；文件未修改。"
        )
    cookie_type = getattr(adapter, "COOKIE_VALUE_TYPE", str)
    if adapter.COOKIE_FIELD in section and not isinstance(
        section[adapter.COOKIE_FIELD], cookie_type
    ):
        raise CookieManagerError(
            f"config.json 中的 {adapter.CONFIG_SECTION}.{adapter.COOKIE_FIELD} 类型无效；文件未修改。"
        )
    return section


def _create_ephemeral_profile(profile_name: str) -> Path:
    """Create an empty, per-capture Chrome profile directory.

    A new empty directory means Chrome starts signed out every time: no cookie,
    local storage or cached login from an earlier capture is reused.
    """
    try:
        return Path(tempfile.mkdtemp(prefix=f"autolearn-{profile_name}-"))
    except OSError:
        raise CookieManagerError("无法创建本次会话的临时 Chrome 配置目录。") from None


def _force_removable(function: Any, path: str, error: Any) -> None:
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        function(path)
    except OSError:
        pass


def _discard_ephemeral_profile(profile_dir: Path) -> None:
    """Best-effort cleanup; a locked file must never fail the capture."""
    for attempt in range(10):
        try:
            shutil.rmtree(profile_dir, onexc=_force_removable)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt == 9:
                return
            time.sleep(0.25)


def _capture_cookies(
    url: str,
    adapter: ModuleType,
    *,
    status_callback: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> list[dict[str, Any]]:
    try:
        from scrapling.fetchers import DynamicSession
    except ImportError:
        raise CookieManagerError(
            "当前 Python 环境缺少支持 DynamicSession 的 Scrapling 浏览器 fetcher；"
            "请在运行项目的同一解释器中安装或升级 scrapling[fetchers]。"
        ) from None

    profile_name = getattr(adapter, "PROFILE_NAME", adapter.SITE_ID)
    if not isinstance(profile_name, str) or not _SITE_ID_PATTERN.fullmatch(profile_name):
        raise CookieManagerError("适配器浏览器配置名称无效。")
    profile_dir = _create_ephemeral_profile(profile_name)

    captured: dict[str, Any] = {"cookies": None, "error": None, "error_code": None}

    def notify(status: str) -> None:
        if status_callback is None:
            return
        try:
            status_callback(status)
        except Exception:
            pass

    def page_action(page: Any) -> None:
        try:
            active_page = page
            if not adapter.is_authenticated(page):
                context = page.context
                opened_pages: list[Any] = []

                def remember_page(opened_page: Any) -> None:
                    opened_pages.append(opened_page)

                def current_pages() -> list[Any]:
                    try:
                        pages = list(context.pages)
                    except Exception:
                        pages = [page]
                    candidates: list[Any] = []
                    seen_page_ids: set[int] = set()
                    for candidate in (*pages, *opened_pages):
                        if id(candidate) not in seen_page_ids:
                            seen_page_ids.add(id(candidate))
                            candidates.append(candidate)
                    return candidates

                def find_authenticated_page(candidates: list[Any]) -> Any | None:
                    for candidate in reversed(candidates):
                        try:
                            if not candidate.is_closed() and adapter.is_authenticated(candidate):
                                return candidate
                        except Exception:
                            continue
                    return None

                def find_open_page(candidates: list[Any]) -> Any | None:
                    for candidate in reversed(candidates):
                        try:
                            if not candidate.is_closed():
                                return candidate
                        except Exception:
                            continue
                    return None

                try:
                    context.on("page", remember_page)
                except Exception:
                    pass

                if status_callback is None and cancel_event is None:
                    print(
                        f"Chrome 已打开 {adapter.SITE_ID} 页面。请在浏览器中手动登录并打开目标页面；"
                        "本次使用全新临时配置，登录状态不会保存到本机。"
                        "完成后回到终端按 Enter 采集 Cookie。等待期间浏览器会保持响应，"
                        "按 Ctrl+C 可取消。"
                    )
                    enter_pressed = threading.Event()

                    # Keep terminal input off the browser-control thread.
                    def wait_for_enter() -> None:
                        try:
                            input()
                        except EOFError:
                            pass
                        finally:
                            enter_pressed.set()

                    threading.Thread(target=wait_for_enter, daemon=True).start()
                    while not enter_pressed.is_set():
                        pump_page = find_open_page(current_pages())
                        if pump_page is None:
                            captured["error"] = "Chrome 页面已全部关闭；config.json 未修改。"
                            captured["error_code"] = "browser_closed"
                            return
                        pump_page.wait_for_timeout(250)
                    deadline = time.monotonic() + 10
                else:
                    notify("login_required")
                    deadline = time.monotonic() + 600

                active_page = None
                while time.monotonic() < deadline and active_page is None:
                    if cancel_event is not None and cancel_event.is_set():
                        captured["error"] = "操作已取消；config.json 未修改。"
                        captured["error_code"] = "cancelled"
                        return
                    candidates = current_pages()
                    active_page = find_authenticated_page(candidates)
                    if active_page is not None:
                        break
                    pump_page = find_open_page(candidates)
                    if pump_page is None:
                        captured["error"] = "Chrome 页面已全部关闭；config.json 未修改。"
                        captured["error_code"] = "browser_closed"
                        return
                    pump_page.wait_for_timeout(250)

                if active_page is None:
                    if cancel_event is not None and cancel_event.is_set():
                        captured["error"] = "操作已取消；config.json 未修改。"
                        captured["error_code"] = "cancelled"
                    else:
                        captured["error"] = "未识别到已登录的目标作业页面；config.json 未修改。"
                        captured["error_code"] = "authentication_not_confirmed"
                    return

            if not adapter.is_authenticated(active_page):
                captured["error"] = "未识别到已登录的目标作业页面；config.json 未修改。"
                captured["error_code"] = "authentication_not_confirmed"
                return

            notify("authenticated")
            current_url = active_page.url
            parsed = urlsplit(current_url)
            if (
                parsed.scheme.lower() != "https"
                or not parsed.hostname
                or not _host_is_allowed(parsed.hostname, adapter.ALLOWED_COOKIE_DOMAINS)
            ):
                captured["error"] = "浏览器当前页面不在适配器允许的 HTTPS 域名内。"
                captured["error_code"] = "domain_rejected"
                return

            records = active_page.context.cookies([url])
            captured["cookies"] = [
                record
                for record in records
                if isinstance(record, dict)
                and isinstance(record.get("name"), str)
                and isinstance(record.get("value"), str)
                and isinstance(record.get("domain"), str)
                and _host_is_allowed(record["domain"], adapter.ALLOWED_COOKIE_DOMAINS)
                and (
                    not isinstance(record.get("expires"), (int, float))
                    or record["expires"] <= 0
                    or record["expires"] > time.time()
                )
            ]
        except KeyboardInterrupt:
            raise
        except Exception:
            captured["error"] = "浏览器页面校验或 Cookie 读取失败。"
            captured["error_code"] = "capture_failed"

    previous_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with DynamicSession(
            headless=False,
            real_chrome=True,
            user_data_dir=str(profile_dir),
            network_idle=False,
            google_search=False,
            timeout=60_000,
        ) as session:
            session.fetch(url, page_action=page_action, wait=0, network_idle=False)
    except KeyboardInterrupt:
        raise
    except Exception:
        raise CookieManagerError(
            "无法启动或控制本机 Chrome；请确认 Chrome 已安装，且临时配置目录未被其他进程占用。"
        ) from None
    finally:
        logging.disable(previous_disable)
        _discard_ephemeral_profile(profile_dir)

    if captured["error"]:
        if captured["error_code"] == "cancelled":
            raise CookieCaptureCancelled("操作已取消；config.json 未修改。")
        raise CookieManagerError(str(captured["error"]))
    records = captured["cookies"]
    if not isinstance(records, list) or not records:
        raise CookieManagerError("未取得适用于目标页面的有效 Cookie；config.json 未修改。")
    return records


def _atomic_write_config(config: dict[str, Any], original_bytes: bytes) -> None:
    try:
        if CONFIG_PATH.read_bytes() != original_bytes:
            raise CookieManagerError("浏览器操作期间 config.json 已被其他程序修改；本次未覆盖文件。")
    except OSError:
        raise CookieManagerError("无法确认 config.json 未被并发修改；本次未写入。") from None

    serialized = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary_path: Path | None = None
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=".config.", suffix=".tmp", dir=CONFIG_PATH.parent
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(file_descriptor, "wb") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            existing_mode = stat.S_IMODE(CONFIG_PATH.stat().st_mode)
            os.chmod(temporary_path, existing_mode)
        except OSError:
            pass
        os.replace(temporary_path, CONFIG_PATH)
    except OSError:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise CookieManagerError("无法安全更新 config.json；原配置保持不变。") from None


def capture_for_adapter(
    site_id: str,
    page_url: str | None = None,
    *,
    status_callback: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple[str, int] | None:
    """Capture a site's Cookie; UI callbacks receive status codes only.

    The optional URL is the task's private URL context. If omitted, the legacy
    CLI path continues to read the adapter URL from config.json.
    """
    adapter = _load_adapter(site_id)
    config, original_bytes = _read_config()
    section = _adapter_config(config, adapter)
    url = _validate_target_url(
        page_url if page_url is not None else section.get(adapter.URL_FIELD),
        adapter,
    )
    if cancel_event is not None and cancel_event.is_set():
        raise CookieCaptureCancelled("操作已取消；config.json 未修改。")
    if status_callback is not None:
        try:
            status_callback("starting")
        except Exception:
            pass
    records = _capture_cookies(
        url,
        adapter,
        status_callback=status_callback,
        cancel_event=cancel_event,
    )
    if cancel_event is not None and cancel_event.is_set():
        raise CookieCaptureCancelled("操作已取消；config.json 未修改。")

    try:
        cookie_value = adapter.serialize_cookies(records)
        json.dumps(cookie_value, ensure_ascii=False)
    except Exception:
        raise CookieManagerError("适配器无法安全地序列化 Cookie；config.json 未修改。") from None
    expected_type = getattr(adapter, "COOKIE_VALUE_TYPE", str)
    if not isinstance(cookie_value, expected_type):
        raise CookieManagerError("适配器生成的 Cookie 配置类型无效；config.json 未修改。")
    if isinstance(cookie_value, str) and not cookie_value.strip():
        raise CookieManagerError("生成的 Cookie 内容为空；config.json 未修改。")
    if cookie_value is None:
        raise CookieManagerError("生成的 Cookie 内容为空；config.json 未修改。")

    if status_callback is not None:
        try:
            status_callback("saving")
        except Exception:
            pass
    section[adapter.COOKIE_FIELD] = cookie_value
    _atomic_write_config(config, original_bytes)
    if status_callback is not None:
        try:
            status_callback("saved")
        except Exception:
            pass
        return None
    return adapter.COOKIE_FIELD, len(records)


def main(argv: list[str] | None = None) -> int:
    available = _available_adapters()
    parser = argparse.ArgumentParser(
        description="为指定适配器拉起全新 Chrome 会话并采集 Cookie（不保留登录状态），写入本机 config.json。"
    )
    parser.add_argument("site_id", nargs="?", help="适配器 ID，例如 chaoxing")
    parser.add_argument("--list-adapters", action="store_true", help="列出已提供认证配置的适配器")
    args = parser.parse_args(argv)

    if args.list_adapters:
        print("\n".join(available) if available else "当前没有可用的认证适配器。")
        return 0
    if not args.site_id:
        parser.error("请提供 site_id，或使用 --list-adapters 查看可用适配器。")
    if args.site_id not in available:
        parser.error("该 site_id 没有可用的认证适配器配置。")

    try:
        ensure_user_data_migrated()
        cookie_field, cookie_count = capture_for_adapter(args.site_id)
    except KeyboardInterrupt:
        parser.exit(130, "已取消；config.json 未更新。\n")
    except CookieManagerError as error:
        parser.exit(1, f"Cookie 获取失败：{error}\n")
    except DataMigrationError as error:
        parser.exit(1, f"用户数据迁移失败：{error}\n")

    print(
        f"已更新 config.json 中 {args.site_id}.{cookie_field}（采集 {cookie_count} 个适用 Cookie）。"
    )
    print("Cookie 值未输出。现在可单独运行对应适配器的爬取或交互命令。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
