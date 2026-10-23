"""Single-user localhost WebUI and API for AutoLearn."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
import importlib
import ipaddress
import json
import mimetypes
import re
import threading
import webbrowser
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core.credentials import CredentialProviderError, LocalCredentialProvider
from core.data_migration import ensure_user_data_migrated
from core.paths import resource_path
from core.settings import ModelProfileError, ModelProfileStore
from core.solver.answer_review import (
    AnswerReviewError,
    load_task_answer_review,
    resolve_local_asset,
    save_task_answers,
)
from core.solver.runner import SolverError, probe_model_endpoint
from core.tasks import (
    QueueState,
    TaskStatus,
    TaskStore,
    TaskStoreBusy,
    TaskStoreError,
    TaskTransitionError,
)
from core.tasks.runtime import TaskRuntimeController


DEFAULT_PORT = 8877
_MAX_JSON_BYTES = 16 * 1024 * 1024
_URL_IN_TITLE = re.compile(r"https?://\S+", re.IGNORECASE)
_SITE_LABELS = {"chaoxing": "超星学习通", "cjyuketang": "长江雨课堂"}


class WebUIRequestError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@asynccontextmanager
async def _lifespan(application: FastAPI):
    ensure_user_data_migrated()
    task_store = TaskStore()
    try:
        runtime = TaskRuntimeController(task_store)
    except Exception:
        task_store.close()
        raise
    application.state.task_store = task_store
    application.state.runtime = runtime
    try:
        yield
    finally:
        if runtime.shutdown(timeout_seconds=None):
            task_store.close()


app = FastAPI(
    title="AutoLearn WebUI",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=_lifespan,
)


def _store(request: Request) -> TaskStore:
    return request.app.state.task_store


def _runtime(request: Request) -> TaskRuntimeController:
    return request.app.state.runtime


def _enum(value: Any) -> str | None:
    return value.value if value is not None else None


def _safe_title(value: str) -> str:
    return _URL_IN_TITLE.sub("网址已隐藏", value).strip() or "未命名任务"


def _cookie_status(store: TaskStore, task: Any) -> dict[str, bool]:
    try:
        configured = bool(LocalCredentialProvider().get_site_cookies(task.site_id))
    except CredentialProviderError:
        configured = False
    return {
        "captured": store.has_completed_authentication(task.task_id),
        "configured": configured,
    }


def _authentication_hint(site_id: str) -> str | None:
    """Adapter-declared hint shown before the login browser opens (optional)."""
    if not isinstance(site_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", site_id):
        return None
    try:
        adapter = importlib.import_module(f"core.adapters.{site_id}.auth")
    except Exception:
        return None
    hint = getattr(adapter, "CAPTURE_HINT", None)
    if not isinstance(hint, str):
        return None
    return " ".join(hint.split())[:300] or None


def _task_json(task: Any) -> dict[str, Any]:
    """Whitelist public task fields; omit URL and confirmation capability."""
    return {
        "task_id": task.task_id,
        "site_id": task.site_id,
        "title": _safe_title(task.title),
        "authentication_hint": _authentication_hint(task.site_id),
        "model_profile_id": task.model_profile_id,
        "run_id": task.run_id,
        "current_step": _enum(task.current_step),
        "status": _enum(task.status),
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "last_safe_error_category": task.last_safe_error_category,
        "last_operation": _enum(getattr(task, "last_operation", None)),
        "interrupted_operation": _enum(getattr(task, "interrupted_operation", None)),
        "pending_operation": _enum(task.pending_operation),
        "interaction_retry_authorized": bool(
            getattr(task, "interaction_retry_authorized", False)
        ),
        "queue_state": _enum(task.queue_state),
        "queue_position": task.queue_position,
    }


def _profile_json(profile: Any, default_id: str | None) -> dict[str, Any]:
    settings = profile.settings
    return {
        "profile_id": profile.profile_id,
        "name": profile.name,
        "version": profile.version,
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
        "is_default": profile.profile_id == default_id,
        "api_key_configured": profile.api_key_configured,
        "settings": {
            "provider": settings.get("provider", ""),
            "base_url": settings.get("base_url", ""),
            "model": settings.get("model", ""),
            "temperature": settings.get("temperature", 0),
            "max_output_tokens": settings.get("max_output_tokens", 1),
            "timeout_seconds": settings.get("timeout_seconds", 1),
        },
    }


def _parse_json_object(request: Request, raw: bytes) -> dict[str, Any]:
    if len(raw) > _MAX_JSON_BYTES:
        raise WebUIRequestError(413, "请求内容过大。")
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise WebUIRequestError(415, "请求格式必须是 JSON。")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise WebUIRequestError(400, "请求 JSON 无效。") from None
    if not isinstance(value, dict):
        raise WebUIRequestError(400, "请求内容格式无效。")
    return value


async def _json_body(request: Request) -> dict[str, Any]:
    length = request.headers.get("content-length")
    if length:
        try:
            if int(length) > _MAX_JSON_BYTES:
                raise WebUIRequestError(413, "请求内容过大。")
        except ValueError:
            raise WebUIRequestError(400, "请求长度无效。") from None
    return _parse_json_object(request, await request.body())


def _required_text(body: dict[str, Any], key: str, *, allow_empty: bool = False) -> str:
    value = body.get(key)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise WebUIRequestError(422, "请求字段无效。")
    return value


def _content_view(content: Any, task_id: str) -> dict[str, Any]:
    if not isinstance(content, dict):
        return {"text": "", "assets": []}
    asset_paths = content.get("asset_paths")
    assets = []
    if isinstance(asset_paths, list):
        for path in asset_paths:
            if isinstance(path, str):
                assets.append(f"/api/tasks/{task_id}/assets/{path}")
    return {"text": content.get("text", ""), "assets": assets}


def _review_json(document: Any) -> dict[str, Any]:
    questions = []
    for question in document.questions:
        options = []
        for option in question.get("options", []):
            options.append({
                "option_id": option.get("option_id"),
                "label": option.get("label"),
                "content": _content_view(option.get("content"), document.task_id),
            })
        questions.append({
            "question_id": question.get("question_id"),
            "number": question.get("number"),
            "question_type": question.get("question_type"),
            "source_type": question.get("source_type"),
            "stem": _content_view(question.get("stem"), document.task_id),
            "options": options,
            "answer_slots": question.get("answer_slots", []),
        })
    return {
        "task_id": document.task_id,
        "site_id": document.site_id,
        "run_id": document.run_id,
        "questions": questions,
        "answers": document.answers,
        "answers_revision": document.answers_revision,
    }


def _task_events_json(store: TaskStore, task_id: str) -> list[dict[str, Any]]:
    return [
        {
            "event_id": item.event_id,
            "task_id": item.task_id,
            "occurred_at": item.occurred_at,
            "event_type": item.event_type,
            "from_status": _enum(item.from_status),
            "to_status": _enum(item.to_status),
            "operation": _enum(item.operation),
            "safe_error_category": item.safe_error_category,
        }
        for item in store.list_events(task_id, limit=100)
    ]


@app.middleware("http")
async def _local_only_and_security_headers(request: Request, call_next):
    client_host = request.client.host if request.client else ""
    try:
        if not ipaddress.ip_address(client_host).is_loopback:
            return JSONResponse({"detail": "仅允许本机访问。"}, status_code=403)
    except ValueError:
        return JSONResponse({"detail": "仅允许本机访问。"}, status_code=403)

    server = request.scope.get("server") or ()
    expected_port = (
        server[1]
        if len(server) > 1 and isinstance(server[1], int)
        else request.url.port
    )
    host_header = request.headers.get("host", "")
    try:
        host_parts = urlsplit(f"//{host_header}")
        valid_host = (
            host_parts.hostname in {"127.0.0.1", "localhost"}
            and host_parts.port == expected_port
        )
    except ValueError:
        valid_host = False
    if not valid_host:
        return JSONResponse({"detail": "请求主机无效。"}, status_code=400)

    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin", "")
        allowed_origins = {
            f"http://127.0.0.1:{expected_port}",
            f"http://localhost:{expected_port}",
        }
        if origin not in allowed_origins:
            return JSONResponse({"detail": "请求来源无效。"}, status_code=403)

    try:
        response = await call_next(request)
    except Exception:
        response = JSONResponse({"detail": "本地服务暂时无法处理此请求。"}, status_code=500)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    )
    # 本机单用户工具会随代码更新：页面与静态资源一律不走缓存，
    # 否则改完界面后浏览器仍会渲染旧的 HTML / CSS / JS。
    if (
        request.url.path.startswith("/api/")
        or request.url.path == "/"
        or request.url.path.startswith("/static/")
    ):
        response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@app.exception_handler(WebUIRequestError)
async def _request_error(_request: Request, error: WebUIRequestError):
    return JSONResponse({"detail": error.message}, status_code=error.status_code)


@app.exception_handler(TaskTransitionError)
async def _transition_error(_request: Request, error: TaskTransitionError):
    return JSONResponse({"detail": str(error)}, status_code=409)


@app.exception_handler(ModelProfileError)
async def _profile_error(_request: Request, error: ModelProfileError):
    return JSONResponse({"detail": str(error)}, status_code=422)


@app.exception_handler(AnswerReviewError)
async def _answer_review_error(_request: Request, error: AnswerReviewError):
    return JSONResponse({"detail": error.safe_message}, status_code=409)


@app.exception_handler(TaskStoreBusy)
async def _store_busy(_request: Request, _error: TaskStoreBusy):
    return JSONResponse({"detail": "任务数据库正被其他操作占用，请稍后重试。"}, status_code=503)


@app.exception_handler(TaskStoreError)
async def _store_error(_request: Request, _error: TaskStoreError):
    return JSONResponse({"detail": "本地任务数据暂不可用。"}, status_code=409)


@app.exception_handler(KeyError)
async def _not_found(_request: Request, _error: KeyError):
    return JSONResponse({"detail": "任务不存在。"}, status_code=404)


@app.get("/")
def index():
    return FileResponse(resource_path("core", "webui", "static", "index.html"))


@app.get("/api/meta")
def get_meta(request: Request):
    store = _store(request)
    return {
        "adapters": [
            {"site_id": site_id, "label": _SITE_LABELS.get(site_id, site_id)}
            for site_id in sorted(store.supported_site_ids)
        ],
        "port": request.url.port,
    }


def _stop_uvicorn_server(server: Any) -> None:
    server.should_exit = True


@app.post("/api/shutdown")
def shutdown_webui(request: Request):
    server = getattr(request.app.state, "uvicorn_server", None)
    if server is None:
        raise WebUIRequestError(503, "当前启动方式不支持从页面关闭服务。")
    timer = threading.Timer(0.25, _stop_uvicorn_server, args=(server,))
    timer.daemon = True
    timer.start()
    return {"shutting_down": True}


@app.get("/api/overview")
def get_overview(request: Request, include_archived: bool = False):
    store = _store(request)
    return {
        "tasks": [
            _task_json(task)
            for task in store.list_tasks(include_archived=include_archived)
        ],
        "queue": [
            {
                "task_id": item.task_id,
                "operation": _enum(item.operation),
                "state": _enum(item.state),
                "queue_position": item.queue_position,
                "overwrite_result": item.overwrite_result,
            }
            for item in store.list_queue()
        ],
        "active_task_id": _runtime(request).active_task_id,
    }


@app.get("/api/tasks")
def list_tasks(
    request: Request,
    search: str = "",
    site_id: str | None = None,
    status: str | None = None,
    include_archived: bool = False,
):
    store = _store(request)
    if status:
        try:
            TaskStatus(status)
        except ValueError:
            raise WebUIRequestError(422, "任务状态筛选无效。") from None
    return {
        "tasks": [
            _task_json(task)
            for task in store.list_tasks(
                include_archived=include_archived,
                search=search,
                site_id=site_id,
                status=status,
            )
        ]
    }


@app.post("/api/tasks")
async def create_task(request: Request):
    body = await _json_body(request)
    site_id = _required_text(body, "site_id")
    title = body.get("title")
    page_url = body.get("page_url")
    profile_id = body.get("model_profile_id")
    if title is not None and not isinstance(title, str):
        raise WebUIRequestError(422, "任务名称无效。")
    if page_url is not None and not isinstance(page_url, str):
        raise WebUIRequestError(422, "作业网址无效。")
    if profile_id is not None and not isinstance(profile_id, str):
        raise WebUIRequestError(422, "模型档案无效。")
    task = _store(request).create_task(
        site_id,
        title=title,
        page_url=page_url or None,
        model_profile_id=profile_id or None,
    )
    return _task_json(task)


@app.get("/api/tasks/{task_id}")
def get_task(request: Request, task_id: str):
    store = _store(request)
    task = store.get_task(task_id)
    try:
        queue_item = store.get_queue_item(task_id)
    except KeyError:
        queue_item = None
    return {
        "task": _task_json(task),
        "cookie_status": _cookie_status(store, task),
        "queue": (
            {
                "operation": _enum(queue_item.operation),
                "state": _enum(queue_item.state),
                "queue_position": queue_item.queue_position,
                "overwrite_result": queue_item.overwrite_result,
            }
            if queue_item
            else None
        ),
        "events": _task_events_json(store, task_id),
    }


@app.get("/api/tasks/{task_id}/draft-settings")
def get_draft_settings(request: Request, task_id: str):
    task = _store(request).get_task(task_id)
    if task.status != TaskStatus.DRAFT or task.queue_state is not None:
        raise TaskTransitionError("只有未排队的草稿可以修改设置。")
    return {"task_id": task.task_id, "page_url": task.page_url or ""}


@app.put("/api/tasks/{task_id}")
async def update_task(request: Request, task_id: str):
    body = await _json_body(request)
    allowed = {"title", "site_id", "page_url", "model_profile_id"}
    if set(body) - allowed:
        raise WebUIRequestError(422, "请求字段无效。")
    task = _store(request).update_task_settings(task_id, **body)
    return _task_json(task)


@app.get("/api/tasks/{task_id}/review")
def get_review(request: Request, task_id: str):
    return _review_json(load_task_answer_review(_store(request), task_id))


@app.get("/api/tasks/{task_id}/assets/{asset_path:path}")
def get_review_asset(request: Request, task_id: str, asset_path: str):
    document = load_task_answer_review(_store(request), task_id)
    path = resolve_local_asset(document.package_directory, asset_path)
    media_type, _encoding = mimetypes.guess_type(path.name)
    if not media_type or not media_type.startswith("image/"):
        raise WebUIRequestError(404, "附件不存在。")
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Content-Disposition": "inline", "Cache-Control": "no-store"},
    )


@app.put("/api/tasks/{task_id}/answers")
async def update_answers(request: Request, task_id: str):
    body = await _json_body(request)
    answers = body.get("answers")
    revision = body.get("revision")
    if not isinstance(answers, list) or (revision is not None and not isinstance(revision, str)):
        raise WebUIRequestError(422, "答案格式无效。")
    document = save_task_answers(
        _store(request),
        task_id,
        answers,
        expected_revision=revision,
    )
    return {"answers_revision": document.answers_revision}


@app.post("/api/tasks/{task_id}/authenticate")
async def enqueue_authentication(request: Request, task_id: str):
    raw = await request.body()
    body = _parse_json_object(request, raw) if raw else {}
    retry_failed = body.get("retry_failed", False)
    if not isinstance(retry_failed, bool):
        raise WebUIRequestError(422, "重试选项无效。")
    item = _runtime(request).enqueue_authentication(task_id, retry_failed=retry_failed)
    return {"task_id": item.task_id, "operation": _enum(item.operation), "state": _enum(item.state)}


@app.post("/api/tasks/{task_id}/crawl")
async def enqueue_crawl(request: Request, task_id: str):
    body = await _json_body(request)
    retry_failed = body.get("retry_failed", False)
    if not isinstance(retry_failed, bool):
        raise WebUIRequestError(422, "重试选项无效。")
    item = _runtime(request).enqueue_crawl(task_id, retry_failed=retry_failed)
    return {"task_id": item.task_id, "operation": _enum(item.operation), "state": _enum(item.state)}


@app.get("/api/tasks/{task_id}/model-authorization")
def get_pending_model_authorization(request: Request, task_id: str):
    pending = _runtime(request).get_pending_model_authorization(task_id)
    return {"pending": asdict(pending) if pending is not None else None}


@app.post("/api/tasks/{task_id}/model-authorization")
async def request_model_authorization(request: Request, task_id: str):
    body = await _json_body(request)
    version = body.get("profile_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise WebUIRequestError(422, "模型档案版本无效。")
    options = {}
    for key in ("profile_update_confirmed", "retry_failed", "overwrite_existing_result", "resume_paused_solve"):
        value = body.get(key, False)
        if not isinstance(value, bool):
            raise WebUIRequestError(422, "模型授权选项无效。")
        options[key] = value
    summary = _runtime(request).request_model_authorization(
        task_id,
        confirmed_profile_version=version,
        **options,
    )
    return asdict(summary)


@app.post("/api/tasks/{task_id}/model-authorization/response")
async def respond_model_authorization(request: Request, task_id: str):
    body = await _json_body(request)
    accepted = body.get("accepted")
    if not isinstance(accepted, bool):
        raise WebUIRequestError(422, "模型授权决定无效。")
    if not _runtime(request).respond_model_authorization(task_id, accepted):
        raise TaskTransitionError("模型授权请求已取消或失效。")
    return {"accepted": accepted}


@app.post("/api/tasks/{task_id}/approve-answers")
def approve_answers(request: Request, task_id: str):
    return _task_json(_store(request).approve_answer_review(task_id))


@app.post("/api/tasks/{task_id}/interaction")
async def enqueue_interaction(request: Request, task_id: str):
    body = await _json_body(request)
    overwrite = body.get("overwrite_existing_result", False)
    if not isinstance(overwrite, bool):
        raise WebUIRequestError(422, "交互选项无效。")
    item = _runtime(request).enqueue_interaction(
        task_id,
        overwrite_existing_result=overwrite,
    )
    return {"task_id": item.task_id, "operation": _enum(item.operation), "state": _enum(item.state)}


@app.post("/api/tasks/{task_id}/interaction/retry")
async def retry_interaction(request: Request, task_id: str):
    body = await _json_body(request)
    if body.get("page_inspected") is not True or body.get("overwrite_existing_result") is not True:
        raise TaskTransitionError("恢复交互前必须检查当前网页，并明确确认覆盖旧结果。")
    _store(request).authorize_interaction_retry(task_id, page_inspected=True)
    item = _runtime(request).enqueue_interaction(
        task_id,
        overwrite_existing_result=True,
    )
    return {"task_id": item.task_id, "operation": _enum(item.operation), "state": _enum(item.state)}


@app.post("/api/tasks/{task_id}/resume")
async def resume_task(request: Request, task_id: str):
    body = await _json_body(request)
    profile_version = body.get("profile_version")
    profile_update_confirmed = body.get("profile_update_confirmed", False)
    if profile_version is not None and (isinstance(profile_version, bool) or not isinstance(profile_version, int)):
        raise WebUIRequestError(422, "模型档案版本无效。")
    if not isinstance(profile_update_confirmed, bool):
        raise WebUIRequestError(422, "模型授权选项无效。")
    resumed = _runtime(request).resume_paused_operation(
        task_id,
        confirmed_profile_version=profile_version,
        profile_update_confirmed=profile_update_confirmed,
    )
    if hasattr(resumed, "profile_id"):
        return asdict(resumed)
    return {"task_id": resumed.task_id, "operation": _enum(resumed.operation), "state": _enum(resumed.state)}


@app.get("/api/tasks/{task_id}/submit-confirmation")
def get_submit_confirmation(request: Request, task_id: str):
    pending = _runtime(request).get_pending_confirmation(task_id)
    if pending is None:
        return {"pending": None}
    return {
        "pending": {
            "task_id": pending.task_id,
            "run_id": pending.run_id,
            "site_id": pending.site_id,
            "confirmation_id": pending.confirmation_id,
            "expires_at": pending.expires_at,
            "question_count": pending.question_count,
        }
    }


@app.post("/api/tasks/{task_id}/submit-confirmation")
async def respond_submit_confirmation(request: Request, task_id: str):
    body = await _json_body(request)
    run_id = _required_text(body, "run_id")
    confirmation_id = _required_text(body, "confirmation_id")
    decision = body.get("decision")
    if decision not in {"confirm", "cancel"}:
        raise WebUIRequestError(422, "提交决定无效。")
    accepted = _runtime(request).respond_submit_confirmation(
        task_id,
        run_id,
        confirmation_id,
        decision,
    )
    if not accepted:
        raise TaskTransitionError("提交确认已过期、已处理或与当前任务不匹配。")
    return {"accepted": True, "decision": decision}


@app.get("/api/tasks/{task_id}/runtime-events")
def get_runtime_events(request: Request, task_id: str, after: int = 0):
    events = _runtime(request).events_after(after, task_id=task_id)
    return {"events": events, "latest_sequence": _runtime(request).latest_event_sequence}


@app.post("/api/tasks/{task_id}/stop")
def stop_task(request: Request, task_id: str):
    store = _store(request)
    item = store.get_queue_item(task_id)
    if item.state in {QueueState.QUEUED, QueueState.PAUSED}:
        task = store.cancel_queued_operation(task_id)
        return {"task": _task_json(task), "stopped": True}
    if item.state == QueueState.ACTIVE:
        store.request_cancel_active_operation(task_id)
        _runtime(request).request_stop(task_id)
        return {"task": _task_json(store.get_task(task_id)), "stopped": False}
    raise TaskTransitionError("当前操作不能停止。")


@app.post("/api/tasks/{task_id}/archive")
def archive_task(request: Request, task_id: str):
    return _task_json(_store(request).archive_task(task_id))


@app.delete("/api/tasks/{task_id}")
async def delete_task(request: Request, task_id: str):
    body = await _json_body(request)
    mode = body.get("mode")
    store = _store(request)
    if mode in {"record", "draft"}:
        # 没有题包的任务只删记录；老客户端发的 "draft" 仍然接受
        store.delete_task_record(task_id)
    elif mode == "package" and body.get("confirmation") == "DELETE":
        store.delete_task_and_package(task_id)
    else:
        raise WebUIRequestError(422, "删除确认无效。")
    return {"deleted": True}


@app.get("/api/profiles")
def list_profiles():
    profile_store = ModelProfileStore()
    default_id = profile_store.default_profile_id()
    return {
        "default_profile_id": default_id,
        "profiles": [
            _profile_json(profile, default_id)
            for profile in profile_store.list_profiles()
        ],
    }


@app.put("/api/profiles")
async def save_profile(request: Request):
    body = await _json_body(request)
    profile_id = body.get("profile_id")
    if profile_id is not None and not isinstance(profile_id, str):
        raise WebUIRequestError(422, "模型档案 ID 无效。")
    name = _required_text(body, "name")
    settings = body.get("settings")
    if not isinstance(settings, dict):
        raise WebUIRequestError(422, "模型档案配置无效。")
    allowed_settings = {
        "provider", "base_url", "model", "temperature",
        "max_output_tokens", "timeout_seconds",
    }
    if set(settings) - allowed_settings:
        raise WebUIRequestError(422, "模型档案配置无效。")
    api_key = body.get("api_key")
    if api_key is not None and not isinstance(api_key, str):
        raise WebUIRequestError(422, "API 密钥格式无效。")
    clear_key = body.get("clear_api_key", False)
    if not isinstance(clear_key, bool):
        raise WebUIRequestError(422, "API 密钥选项无效。")
    if clear_key:
        api_key = ""
    elif not api_key:
        api_key = None
    profile = ModelProfileStore().save_profile(
        profile_id,
        name=name,
        settings=settings,
        api_key=api_key,
    )
    return _profile_json(profile, ModelProfileStore().default_profile_id())


@app.post("/api/profiles/{profile_id}/default")
def set_default_profile(profile_id: str):
    profile = ModelProfileStore().set_default_profile(profile_id)
    return _profile_json(profile, profile.profile_id)


@app.delete("/api/profiles/{profile_id}")
def delete_profile(profile_id: str):
    profile_store = ModelProfileStore()
    default_id = profile_store.delete_profile(profile_id)
    return {"deleted": True, "default_profile_id": default_id}


_PROBE_FAILURE_MESSAGES = {
    "config": "配置不完整或格式不正确：请检查完整请求 URL、模型名和超时设置。",
    "network": "无法连接该地址：请检查完整请求 URL、网络连通性或超时设置。",
    "authentication": "API 密钥被服务端拒绝（HTTP 401），请检查密钥是否正确。",
    "permission_denied": "该密钥或账号无权访问此模型（HTTP 403）。",
    "not_found": "接口路径或模型不存在（HTTP 404），请检查完整请求 URL 与模型名。",
    "rate_limited": "触发限流或额度不足（HTTP 429），请稍后重试。",
    "bad_request": "服务端拒绝了请求（HTTP 400），端点与模型可能不匹配。",
    "payload_too_large": "服务端认为请求过大（HTTP 413）。",
    "upstream": "模型服务返回 5xx 错误，请稍后重试。",
    "http": "模型服务返回了异常状态码。",
    "context_limit": "请求超出模型上下文限制。",
    "invalid_api_response": "返回内容不是有效 JSON，可能不是兼容的模型接口。",
    "response_too_large": "返回内容超出允许的大小。",
}


@app.post("/api/profiles/test")
async def test_profile(request: Request):
    """Send one minimal request to an unsaved or saved profile (UI only)."""
    body = await _json_body(request)
    profile_id = body.get("profile_id")
    if profile_id is not None and not isinstance(profile_id, str):
        raise WebUIRequestError(422, "模型档案 ID 无效。")
    submitted = body.get("settings")
    if submitted is not None and not isinstance(submitted, dict):
        raise WebUIRequestError(422, "模型档案配置无效。")
    if submitted:
        allowed_settings = {
            "provider", "base_url", "model", "temperature",
            "max_output_tokens", "timeout_seconds",
        }
        if set(submitted) - allowed_settings:
            raise WebUIRequestError(422, "模型档案配置无效。")
    api_key = body.get("api_key")
    if api_key is not None and not isinstance(api_key, str):
        raise WebUIRequestError(422, "API 密钥格式无效。")
    clear_key = body.get("clear_api_key", False)
    if not isinstance(clear_key, bool):
        raise WebUIRequestError(422, "API 密钥选项无效。")

    stored = ModelProfileStore().get_profile(profile_id) if profile_id else None
    if submitted is None and stored is None:
        raise WebUIRequestError(422, "请先填写模型配置。")
    section: dict[str, Any] = dict(stored.settings) if stored is not None else {}
    if submitted:
        section.update(submitted)
    if clear_key:
        section["api_key"] = ""
    elif api_key:
        section["api_key"] = api_key
    elif stored is not None:
        section["api_key"] = stored.settings.get("api_key", "")

    try:
        result = await asyncio.to_thread(probe_model_endpoint, section)
    except SolverError as error:
        return {
            "ok": False,
            "category": error.category,
            "message": _PROBE_FAILURE_MESSAGES.get(error.category, "连接测试失败。"),
        }
    return {"ok": True, **result}


app.mount(
    "/static",
    StaticFiles(directory=resource_path("core", "webui", "static")),
    name="static",
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the AutoLearn localhost WebUI")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    address = f"http://127.0.0.1:{args.port}"
    print(f"AutoLearn WebUI: {address}")
    if args.open_browser:
        timer = threading.Timer(0.8, webbrowser.open, args=(address,))
        timer.daemon = True
        timer.start()
    import uvicorn

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=args.port,
        workers=1,
        access_log=False,
        log_level="info",
    )
    server = uvicorn.Server(config)
    app.state.uvicorn_server = server
    server.run()


__all__ = ["app", "main"]


