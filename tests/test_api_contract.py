"""做题（homework）侧 API 契约快照（M3）。

为什么需要：M3 把 `/api/overview` 改成"做题 + 课程"混排，做题侧的 JSON 形状
必须一个字段都不变——少一个字段，前端卡片就空一格；多一个字段，脱敏就成了
筛子。所以这里在**隔离的 LOCALAPPDATA**（指向 pytest 的临时目录，绝不碰真实
`%LOCALAPPDATA%\\AutoLearn`）里起一个真服务，造 2 条做题草稿，把 5 个接口的
响应归一化后与 `tests/snapshots/homework_*.json` 逐键比对。

归一化只抹掉"每次都不同"的东西：端口、UUID、任务 id、ISO 时间戳。数组按
task_id 排序后再比，避免两次创建落在同一毫秒时顺序漂移（顺序另有专门断言）。

有意变更契约时才刷新快照：:

    $env:AUTOLEARN_SNAPSHOT_REFRESH = "1"
    .\\.venv\\Scripts\\python.exe -m pytest tests/test_api_contract.py -q
    Remove-Item Env:\\AUTOLEARN_SNAPSHOT_REFRESH

刷新后必须在 review 里说明改了什么、为什么前端能接受。
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots"
REFRESH = os.environ.get("AUTOLEARN_SNAPSHOT_REFRESH") == "1"
STARTUP_TIMEOUT = 60.0

#: 本机服务不能被系统代理劫持（否则 127.0.0.1 会被代理吞成 502）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_ISO_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")
_HEX32 = re.compile(r"[0-9a-f]{32}")

#: 快照里绝不能出现的字样（脱敏底线）。
_FORBIDDEN = ("cookie\"", "CASLOGC", "api_key", "http://", "https://")


# --------------------------------------------------------------------------- 服务


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http(base: str, method: str, path: str, body: Any = None) -> tuple[int, Any]:
    """同源 JSON 请求；写请求带 Origin（服务端的 CSRF 校验要求它匹配端口）。"""
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
        request.add_header("Origin", base)
    try:
        with _OPENER.open(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = raw
        return error.code, payload


def _wait_ready(base: str, proc: subprocess.Popen, log_path: Path) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"服务提前退出（code={proc.returncode}）：\n{_tail(log_path)}"
            )
        try:
            status, _payload = _http(base, "GET", "/api/meta")
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.1)
            continue
        if status == 200:
            return
        time.sleep(0.1)
    raise AssertionError(f"服务在 {STARTUP_TIMEOUT:.0f}s 内未就绪：\n{_tail(log_path)}")


def _tail(path: Path, limit: int = 4000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return "<无日志>"


@pytest.fixture(scope="module")
def live_server(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """隔离数据目录 + 真服务；退出时优雅终止。"""
    data_root = tmp_path_factory.mktemp("localappdata")
    log_path = data_root / "server.log"
    port = _free_port()
    env = {
        **os.environ,
        "LOCALAPPDATA": str(data_root),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    log = log_path.open("w", encoding="utf-8", newline="")
    proc = subprocess.Popen(
        [sys.executable, "-m", "core.webui", "--port", str(port)],
        cwd=str(ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_ready(base, proc, log_path)
        yield {"base": base, "port": port, "data_root": data_root}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        log.close()


@pytest.fixture(scope="module")
def drafts(live_server: dict[str, Any]) -> list[str]:
    """2 条做题草稿（课程任务不参与，课程侧另有 dto 测试）。"""
    created: list[str] = []
    for title in ("契约快照 · 草稿一", "契约快照 · 草稿二"):
        status, payload = _http(
            live_server["base"],
            "POST",
            "/api/tasks",
            {"site_id": "chaoxing", "title": title},
        )
        assert status == 200, payload
        assert payload["task_type"] == "homework"
        created.append(payload["task_id"])
    return created


@pytest.fixture(scope="module")
def captured(live_server: dict[str, Any], drafts: list[str]) -> dict[str, Any]:
    """五个接口的原始响应（未归一化），多个用例共用一次抓取。"""
    base = live_server["base"]
    detail_path = f"/api/tasks/{drafts[0]}"
    cases = {
        "meta": "/api/meta",
        "overview": "/api/overview",
        "tasks": "/api/tasks",
        "detail": detail_path,
        "runtime_events": f"{detail_path}/runtime-events",
    }
    result: dict[str, Any] = {}
    for name, path in cases.items():
        status, payload = _http(base, "GET", path)
        assert status == 200, f"{path} → {status}: {payload}"
        result[name] = payload
    return result


# ----------------------------------------------------------------------- 归一化


def _normalize(value: Any, labels: dict[str, str], port: int) -> Any:
    if isinstance(value, dict):
        return {key: _normalize(item, labels, port) for key, item in value.items()}
    if isinstance(value, list):
        items = [_normalize(item, labels, port) for item in value]
        if items and all(isinstance(item, dict) and "task_id" in item for item in items):
            items.sort(key=lambda item: str(item["task_id"]))
        return items
    if isinstance(value, str):
        text = value
        for raw, label in labels.items():
            text = text.replace(raw, label)
        text = _ISO_UTC.sub("<TIMESTAMP>", text)
        text = _HEX32.sub("<HEX32>", text)
        return "<PORT>" if text == str(port) else text
    if isinstance(value, int) and not isinstance(value, bool) and value == port:
        return "<PORT>"
    return value


def _snapshot_path(name: str) -> Path:
    return SNAPSHOT_DIR / f"homework_{name}.json"


def _dump(payload: Any) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def _check_snapshot(name: str, payload: Any) -> None:
    path = _snapshot_path(name)
    text = _dump(payload)
    if REFRESH:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return
    assert path.exists(), (
        f"缺少快照 {path.name}。首次生成："
        "$env:AUTOLEARN_SNAPSHOT_REFRESH='1'; 跑一次本文件；再删掉该环境变量"
    )
    expected = json.loads(path.read_text(encoding="utf-8"))
    if payload != expected:
        diff = "\n".join(
            difflib.unified_diff(
                _dump(expected).splitlines(),
                text.splitlines(),
                fromfile=f"{path.name}（基线）",
                tofile="实测响应",
                lineterm="",
            )
        )
        pytest.fail(f"做题契约被改动：{path.name}\n{diff}", pytrace=False)


# ------------------------------------------------------------------------- 用例


@pytest.mark.parametrize(
    "name", ["meta", "overview", "tasks", "detail", "runtime_events"]
)
def test_homework_contract_snapshot(
    name: str, live_server: dict[str, Any], drafts: list[str], captured: dict[str, Any]
) -> None:
    """除 task_type 外，结构与值必须与快照一致（时间/UUID/端口已归一化）。"""
    labels = {drafts[0]: "<TASK_1>", drafts[1]: "<TASK_2>"}
    normalized = _normalize(captured[name], labels, live_server["port"])
    _check_snapshot(name, normalized)


def test_overview_merges_types_in_updated_at_desc(
    live_server: dict[str, Any], drafts: list[str]
) -> None:
    """合并后倒序；queue/active_task_id 仍只表示做题侧。"""
    status, payload = _http(live_server["base"], "GET", "/api/overview")
    assert status == 200
    tasks = payload["tasks"]
    assert len(tasks) == 2, tasks
    assert {task["task_type"] for task in tasks} == {"homework"}
    stamps = [task["updated_at"] for task in tasks]
    assert stamps == sorted(stamps, reverse=True), stamps
    assert payload["queue"] == []
    assert payload["active_task_id"] is None


def test_detail_keeps_five_shell_keys(captured: dict[str, Any]) -> None:
    assert set(captured["detail"]) == {"task", "cookie_status", "queue", "events"}
    assert set(captured["detail"]["cookie_status"]) == {"captured", "configured"}


def test_snapshots_have_no_secrets(captured: dict[str, Any], live_server: dict[str, Any],
                                   drafts: list[str]) -> None:
    """摘要/详情不得带 Cookie、API key 或完整 URL（题目网址只在草稿编辑接口给）。"""
    labels = {drafts[0]: "<TASK_1>", drafts[1]: "<TASK_2>"}
    for name in ("meta", "overview", "tasks", "detail", "runtime_events"):
        text = json.dumps(_normalize(captured[name], labels, live_server["port"]),
                          ensure_ascii=False)
        for token in _FORBIDDEN:
            assert token not in text, f"{name} 里出现敏感字样：{token}"


def test_course_overview_degrades_when_store_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """课程存储整体炸掉时只丢课程列表，绝不让做题列表一起 500。"""
    from core.webui import app as webui

    class ExplodingStore:
        def __init__(self) -> None:
            raise RuntimeError("courses 目录不可读")

    monkeypatch.setattr(webui, "CourseJobStore", ExplodingStore)
    assert webui._course_overview(False) == []


def test_overview_shows_both_types(
    live_server: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """M3 的主验收：一个列表里同时看到做题与课程，且按 updated_at 倒序。

    课程任务直接写进服务进程用的那个数据目录（临时 LOCALAPPDATA），
    这样它和做题库里那两条草稿同源，才是真的"混合列表"。
    """
    from core.common.clock import timestamp
    from core.courses.domain import (
        CourseJob,
        CourseMode,
        CourseOptions,
        CoursePhase,
        CourseStatus,
        job_id,
    )
    from core.courses.store import CourseJobStore

    monkeypatch.setenv("LOCALAPPDATA", str(live_server["data_root"]))
    store = CourseJobStore()
    now = timestamp()
    job = CourseJob(
        job_id=job_id(),
        site_id="zhihuishu",
        title="契约快照 · 网课",
        mode=CourseMode.SHARED,
        course_id=None,
        class_id=None,
        status=CourseStatus.DRAFT,
        phase=CoursePhase.SETUP,
        current_title=None,
        progress_percent=0.0,
        completed_count=0,
        total_count=0,
        failed_count=0,
        run_id=None,
        options=CourseOptions(),
        created_at=now,
        updated_at=now,
        last_safe_error_category=None,
    )
    store.create(job)
    try:
        status, payload = _http(live_server["base"], "GET", "/api/overview")
        assert status == 200
        tasks = payload["tasks"]
        assert {task["task_type"] for task in tasks} == {"homework", "course"}
        stamps = [task["updated_at"] for task in tasks]
        assert stamps == sorted(stamps, reverse=True), stamps
        course = next(task for task in tasks if task["task_type"] == "course")
        homework = next(task for task in tasks if task["task_type"] == "homework")
        # 两类任务共用 §5.1 的 18 键——前端卡片才能用同一个渲染器。
        assert set(course) == set(homework)
        assert course["task_id"] == job.job_id
        assert course["run_id"] is None
        assert course["queue_state"] is None and course["queue_position"] is None
        assert course["interaction_retry_authorized"] is False
        # 刚建的课程任务更新最晚，应排在所有做题任务之前。
        assert tasks[0]["task_type"] == "course"
        # 队列与活动任务仍只表示做题 worker。
        assert payload["queue"] == []
        assert payload["active_task_id"] is None
    finally:
        store.delete(job.job_id)


def test_overview_survives_broken_courses_path(live_server: dict[str, Any]) -> None:
    """把 courses 目录换成同名文件（比改名更狠），接口仍 200 且只剩做题任务。"""
    courses = live_server["data_root"] / "AutoLearn" / "courses"
    backup = courses.with_name("courses.bak")
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    restored = False
    try:
        if courses.exists():
            shutil.move(str(courses), str(backup))
            restored = True
        courses.write_text("not a directory", encoding="utf-8")
        status, payload = _http(live_server["base"], "GET", "/api/overview")
        assert status == 200, payload
        assert {task["task_type"] for task in payload["tasks"]} == {"homework"}
    finally:
        if courses.is_file():
            courses.unlink()
        if restored and backup.exists():
            shutil.move(str(backup), str(courses))
