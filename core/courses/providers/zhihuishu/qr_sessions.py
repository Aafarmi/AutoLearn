"""In-memory QR login sessions backed by the upstream read-only bridge."""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from ...store import CourseJobStore
from ...domain import CoursePhase, CourseStatus, ensure_transition
from ...errors import CourseAuthRequired, CourseTransitionError
from ....auth import cookie_manager
from ....paths import data_path
from . import auth as zhihuishu_auth, preflight

logger = logging.getLogger(__name__)
_LOCK = threading.RLock()
_SESSIONS: dict[str, dict[str, Any]] = {}
_CATALOGS: dict[str, list[dict[str, Any]]] = {}
# Preserve the exact upstream RequestsCookieJar representation between QR login
# and catalog fetch while this WebUI process is alive. config.json remains the
# Cookie Manager's portable Cookie header; the full jar is memory-only.
_COOKIE_JARS: dict[str, list[dict[str, Any]]] = {}
_OUTLINES: dict[str, tuple[tuple[str, str, str, str], dict[str, Any]]] = {}


class _BridgeLoginError(RuntimeError):
    def __init__(self, category: str) -> None:
        self.category = category if category.isalnum() else "upstream_error"
        super().__init__(self.category)


def _bridge(job_id: str) -> Path:
    run = preflight.prepare_run_dir(job_id)
    return run / "upstream"


def start(job_id: str) -> dict[str, Any]:
    store = CourseJobStore()
    job = store.get_job(job_id)
    with _LOCK:
        _CATALOGS.pop(job_id, None)
        _OUTLINES.pop(job_id, None)
        current = _SESSIONS.get(job_id)
        if current and current.get("state") in {"waiting_qr", "scanned"}:
            return public_state(job_id)
        _COOKIE_JARS.pop(job_id, None)
        if job.status not in {CourseStatus.DRAFT, CourseStatus.READY}:
            raise CourseTransitionError("当前状态不能登录。")
        if job.status is CourseStatus.READY:
            # READY tasks can re-authenticate through the existing transition.
            ensure_transition(job.status, CourseStatus.AUTHENTICATING)
            store.update(job, status=CourseStatus.AUTHENTICATING, phase=CoursePhase.AUTHENTICATION)
            store.append_business_event(job_id, "auth_started", job.status, CourseStatus.AUTHENTICATING)
        else:
            ensure_transition(job.status, CourseStatus.AUTHENTICATING)
            store.update(job, status=CourseStatus.AUTHENTICATING, phase=CoursePhase.AUTHENTICATION)
            store.append_business_event(job_id, "auth_started", job.status, CourseStatus.AUTHENTICATING)
        session = {"state": "starting", "qr": None, "error": None, "stage": "starting"}
        _SESSIONS[job_id] = session
        threading.Thread(target=_run_login, args=(job_id, session), daemon=True, name=f"course-qr-{job_id}").start()
        return {"state": "starting", "qr": None, "error": None}


def public_state(job_id: str) -> dict[str, Any]:
    with _LOCK:
        state = _SESSIONS.get(job_id)
        if not state:
            return {"state": "idle", "qr": None}
        return {
            "state": state["state"], "qr": state.get("qr"),
            "error": state.get("error"), "error_category": state.get("error_category"),
        }


def _run_login(job_id: str, state: dict[str, Any]) -> None:
    process: subprocess.Popen[str] | None = None
    try:
        upstream = _bridge(job_id)
        process = subprocess.Popen(
            [sys.executable, "-u", str(Path(__file__).with_name("auth_bridge.py")), str(upstream)],
            cwd=str(upstream.parent), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1,
        )
        with _LOCK:
            state["process"] = process
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(json.dumps({"action": "login"}) + "\n")
        process.stdin.flush()
        process.stdin.close()
        for line in process.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") == "qr":
                with _LOCK:
                    state.update(state="waiting_qr", qr=event.get("image"), stage="waiting_for_scan")
            elif event.get("event") == "authenticated":
                with _LOCK:
                    state.update(stage="saving_cookies")
                records = event.get("cookies")
                cookie_manager.persist_cookies_for_module(zhihuishu_auth, records)
                with _LOCK:
                    state.update(stage="validating_cookies")
                persisted = _cookies_for_bridge("zhihuishu")
                preflight.check_cookies(persisted)
                with _LOCK:
                    state.update(stage="updating_task")
                store = CourseJobStore()
                job = store.get_job(job_id)
                ensure_transition(job.status, CourseStatus.READY)
                store.update(job, status=CourseStatus.READY, phase=CoursePhase.CATALOG)
                store.append_business_event(job_id, "authenticated", CourseStatus.AUTHENTICATING, CourseStatus.READY)
                with _LOCK:
                    _COOKIE_JARS[job_id] = [dict(record) for record in records if isinstance(record, dict)]
                    state.update(state="authenticated", qr=None, error=None)
                return
            elif event.get("event") == "error":
                raise _BridgeLoginError(str(event.get("category") or "upstream_error"))
        raise _BridgeLoginError("process_ended")
    except Exception as error:
        stage = state.get("stage", "starting")
        category = getattr(error, "category", type(error).__name__)
        category = category if isinstance(category, str) and category.isalnum() else "auth_error"
        messages = {
            "waiting_for_scan": "智慧树登录流程未完成，请确认已在智慧树 App 扫码并完成确认后重试。",
            "saving_cookies": "扫码已返回，但本机 Cookie 保存失败；请重新扫码登录。",
            "validating_cookies": "扫码已返回，但登录态校验失败；请重新扫码登录。",
            "updating_task": "登录态已校验，但课程任务状态更新失败；请刷新任务详情。",
        }
        logger.warning("智慧树二维码认证失败（task=%s stage=%s category=%s）", job_id, stage, category)
        try:
            store = CourseJobStore()
            job = store.get_job(job_id)
            if job.status is CourseStatus.AUTHENTICATING:
                ensure_transition(job.status, CourseStatus.DRAFT)
                store.update(job, status=CourseStatus.DRAFT, phase=CoursePhase.SETUP)
                safe_category = f"qr_{stage}_{category}"[:64]
                store.append_business_event(job_id, "auth_failed", CourseStatus.AUTHENTICATING, CourseStatus.DRAFT, safe_error_category=safe_category)
        except Exception:
            logger.error("二维码认证失败后无法恢复任务状态（task=%s）", job_id)
        with _LOCK:
            message = messages.get(stage, "二维码登录未完成，请重新获取二维码后重试。")
            safe_detail = str(error) if isinstance(error, (cookie_manager.CookieManagerError, CourseAuthRequired)) else category
            state.update(state="failed", qr=None, error=f"{message}（阶段：{stage}，{safe_detail}）", error_category=category)
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()


def fetch_catalog(job_id: str) -> dict[str, Any]:
    store = CourseJobStore()
    job = store.get_job(job_id)
    if job.status is not CourseStatus.READY:
        raise CourseTransitionError("请先完成二维码登录。")
    with _LOCK:
        cookie_jar = _COOKIE_JARS.get(job_id)
        cookies: dict[str, str] | list[dict[str, Any]] = (
            [dict(record) for record in cookie_jar]
            if cookie_jar
            else _cookies_for_bridge(job.site_id)
        )
    upstream = _bridge(job_id)
    try:
        completed = subprocess.run(
            [sys.executable, "-u", str(Path(__file__).with_name("auth_bridge.py")), str(upstream)],
            cwd=str(upstream.parent), input=json.dumps({"action": "catalog", "cookies": cookies}),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise CourseAuthRequired("读取智慧树课程目录失败，请重新登录后重试。") from None
    events = _jsonl_events(completed.stdout)
    catalog_event = next((event for event in events if event.get("event") == "catalog"), None)
    catalog = catalog_event.get("courses") if isinstance(catalog_event, dict) else None
    warnings = catalog_event.get("warnings", []) if isinstance(catalog_event, dict) else []
    if completed.returncode or not isinstance(catalog, list):
        category = next((event.get("category") for event in events if event.get("event") == "error"), "catalog_error")
        category = category if isinstance(category, str) and category.isalnum() else "catalog_error"
        logger.warning(
            "智慧树目录子进程失败（task=%s exit=%s category=%s）",
            job_id,
            completed.returncode,
            category,
        )
        raise CourseAuthRequired(f"读取智慧树课程目录失败（{category}），请检查登录状态或网络后重试。")
    for warning in warnings if isinstance(warnings, list) else []:
        if isinstance(warning, dict):
            mode = warning.get("mode")
            category = warning.get("category")
            if mode in {"shared", "hike", "ai"} and isinstance(category, str) and category.isalnum():
                logger.warning("智慧树课程目录读取失败（task=%s mode=%s category=%s）", job_id, mode, category)
    with _LOCK:
        _CATALOGS[job_id] = catalog
    return {"courses": catalog, "warnings": warnings if isinstance(warnings, list) else []}


def contains_catalog_course(job_id: str, values: dict[str, Any]) -> bool:
    with _LOCK:
        entries = _CATALOGS.get(job_id, [])
        return any(
            entry.get("mode") == values.get("mode")
            and entry.get("course_id") == str(values.get("course_id"))
            and entry.get("class_id") == values.get("class_id")
            for entry in entries
        )


def fetch_outline(job_id: str) -> dict[str, Any]:
    """Read the selected course outline through upstream context methods.

    Use a separate temporary vendor copy: the active course worker owns
    jobs/<id>/run and must not have that directory rebuilt while it is running.
    """
    job = CourseJobStore().get_job(job_id)
    mode = getattr(job.mode, "value", job.mode)
    if mode not in {"shared", "hike", "ai"} or not job.course_id:
        raise CourseTransitionError("请先选择课程，再查看课程目录。")
    signature = (mode, str(job.course_id), str(job.class_id or ""), str(job.run_id or ""))
    with _LOCK:
        cached = _OUTLINES.get(job_id)
        if cached and cached[0] == signature:
            return cached[1]
        cookie_jar = _COOKIE_JARS.get(job_id)
        cookies: dict[str, str] | list[dict[str, Any]] = (
            [dict(record) for record in cookie_jar]
            if cookie_jar
            else _cookies_for_bridge(job.site_id)
        )

    parent = data_path("courses")
    parent.mkdir(parents=True, exist_ok=True)
    request = {
        "action": "outline",
        "mode": mode,
        "course_id": job.course_id,
        "class_id": job.class_id,
        "threshold": job.options.threshold,
        "cookies": cookies,
    }
    try:
        with tempfile.TemporaryDirectory(prefix="course-outline-", dir=str(parent)) as scratch:
            preflight.materialize_vendor(scratch)
            upstream = Path(scratch) / "upstream"
            completed = subprocess.run(
                [sys.executable, "-u", str(Path(__file__).with_name("auth_bridge.py")), str(upstream)],
                cwd=scratch,
                input=json.dumps(request),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=90,
            )
    except (OSError, subprocess.TimeoutExpired):
        raise CourseAuthRequired("读取课程目录失败，请检查登录状态或网络后重试。") from None

    events = _jsonl_events(completed.stdout)
    outline_event = next((event for event in events if event.get("event") == "outline"), None)
    items = outline_event.get("items") if isinstance(outline_event, dict) else None
    if completed.returncode or not isinstance(items, list):
        category = next((event.get("category") for event in events if event.get("event") == "error"), "outline_error")
        category = category if isinstance(category, str) and category.isalnum() else "outline_error"
        logger.warning("智慧树课程目录读取失败（task=%s exit=%s category=%s）", job_id, completed.returncode, category)
        raise CourseAuthRequired(f"读取课程目录失败（{category}），请检查登录状态或网络后重试。")
    result = {"items": items, "truncated": bool(outline_event.get("truncated", False))}
    with _LOCK:
        _OUTLINES[job_id] = (signature, result)
    return result


def shutdown_all() -> None:
    with _LOCK:
        processes = [value.get("process") for value in _SESSIONS.values()]
        _COOKIE_JARS.clear()
        _OUTLINES.clear()
    for process in processes:
        if process is not None and process.poll() is None:
            process.terminate()


def _cookies_for_bridge(site_id: str) -> dict[str, str]:
    from core.credentials import LocalCredentialProvider
    try:
        cookies = LocalCredentialProvider().get_site_cookies(site_id)
        preflight.check_cookies(cookies)
        return cookies
    except Exception:
        raise CourseAuthRequired("智慧树登录已过期，请重新扫码登录。") from None


def _jsonl_events(text: str | None) -> list[dict[str, Any]]:
    result = []
    for line in (text or "").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            result.append(value)
    return result
