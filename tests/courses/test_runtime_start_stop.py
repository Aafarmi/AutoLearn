"""M7：CourseRuntime 起停、环境、终止与关机清理。

用两个**假桥接**跑，不碰 vendor、不碰网络、不碰真实 LOCALAPPDATA：

- `tests/fixtures/echo_bridge.py`：读完一行参数立刻退出（模拟"跑完就结束"）。
- 临时生成的慢桥接：读完一行后 sleep 30s（模拟"正在学习"，用来验证停止路径）。

凡是要落盘的东西（store、运行目录）都指到 `tmp_path`；Cookie 读取打桩。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from core.common.clock import timestamp
from core.courses import runtime as runtime_module
from core.courses.domain import (
    CourseJob,
    CourseMode,
    CourseOptions,
    CoursePhase,
    CourseStatus,
    job_id,
)
from core.courses.errors import CourseAuthRequired, CourseBusy, CourseError
from core.courses.runtime import CourseRuntime
from core.courses.store import CourseJobStore

ECHO_BRIDGE = Path(__file__).resolve().parents[1] / "fixtures" / "echo_bridge.py"

SLOW_BRIDGE_SOURCE = '''\
import json
import sys
import time

request = sys.stdin.readline()
sys.stdout.write(json.dumps({"type": "hello", "version": "slow-1", "mode": "shared",
                             "upstream_version": "0.0-fixture"}) + "\\n")
sys.stdout.flush()
time.sleep(30)
'''


@pytest.fixture()
def store(tmp_path: Path) -> CourseJobStore:
    return CourseJobStore(tmp_path / "courses")


@pytest.fixture()
def run_root(tmp_path: Path) -> Path:
    root = tmp_path / "runs"
    root.mkdir()
    return root


@pytest.fixture(autouse=True)
def stub_cookies(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有真 config.json 也要能起：Cookie 只影响 payload，不影响进程管理。"""
    monkeypatch.setattr(
        runtime_module, "_site_cookies", lambda site_id: {"CASLOGC": "stub"}
    )


@pytest.fixture()
def slow_bridge(tmp_path: Path) -> Path:
    path = tmp_path / "slow_bridge.py"
    path.write_text(SLOW_BRIDGE_SOURCE, encoding="utf-8")
    return path


def make_job(store: CourseJobStore, status: CourseStatus = CourseStatus.READY) -> CourseJob:
    now = timestamp()
    job = CourseJob(
        job_id=job_id(),
        site_id="zhihuishu",
        title="运行时测试 · 共享课",
        mode=CourseMode.SHARED,
        course_id="123456",
        class_id=None,
        status=status,
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
    return job


def runner(store: CourseJobStore, run_root: Path, bridge: Path) -> CourseRuntime:
    return CourseRuntime(
        store,
        bridge=bridge,
        run_dir_factory=lambda item: _mkdir(run_root / item),
    )


def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


# ------------------------------------------------------------------ 启动


def test_start_spawns_child_and_writes_state(
    store: CourseJobStore, run_root: Path
) -> None:
    job = make_job(store)
    runtime = runner(store, run_root, ECHO_BRIDGE)

    run = runtime.start(job, {"extra": 1})

    assert run.pid > 0
    assert run.alive or run.returncode is not None
    updated = store.get_job(job.job_id)
    assert updated.status is CourseStatus.RUNNING
    assert updated.phase is CoursePhase.SETUP
    assert updated.run_id == run.run_id
    slot = store.active_slot("zhihuishu")
    assert slot is not None and slot["job_id"] == job.job_id
    assert slot["pid"] == run.pid
    events = store.read_business_events(job.job_id)
    assert [event["event_type"] for event in events][-1] == "course_started"
    wait_for(lambda: not run.alive)


def test_start_uses_isolated_env_and_run_dir(
    store: CourseJobStore, run_root: Path, tmp_path: Path
) -> None:
    """环境和 cwd 是硬契约：上游必须能在 <run>/upstream 找到自己。"""
    probe = tmp_path / "probe_bridge.py"
    probe.write_text(
        "import json, os, sys\n"
        "sys.stdin.readline()\n"
        "json.dump({'cwd': os.getcwd(), 'pythonpath': os.environ.get('PYTHONPATH'),\n"
        "           'utf8': os.environ.get('PYTHONUTF8'),\n"
        "           'io': os.environ.get('PYTHONIOENCODING'),\n"
        "           'cols': os.environ.get('COLUMNS'), 'lines': os.environ.get('LINES')},\n"
        "          open(os.path.join(os.getcwd(), 'env.json'), 'w'))\n",
        encoding="utf-8",
    )
    job = make_job(store)
    runtime = runner(store, run_root, probe)

    run = runtime.start(job)
    proc = run.proc
    proc.wait(timeout=20)

    payload = json.loads((run_root / job.job_id / "env.json").read_text(encoding="utf-8"))
    assert Path(payload["cwd"]).resolve() == (run_root / job.job_id).resolve()
    assert payload["pythonpath"] == str(run_root / job.job_id / "upstream")
    assert payload["utf8"] == "1"
    assert payload["io"] == "utf-8"
    assert (payload["cols"], payload["lines"]) == ("80", "24")


def test_start_passes_cookies_on_stdin_only(
    store: CourseJobStore, run_root: Path, tmp_path: Path
) -> None:
    """Cookie 只能出现在 stdin 的请求行里，不能进环境变量或命令行。"""
    probe = tmp_path / "cookie_bridge.py"
    probe.write_text(
        "import json, os, sys\n"
        "request = json.loads(sys.stdin.readline())\n"
        "json.dump({'request': request, 'env': dict(os.environ), 'argv': sys.argv},\n"
        "          open(os.path.join(os.getcwd(), 'probe.json'), 'w'))\n",
        encoding="utf-8",
    )
    job = make_job(store)
    runtime = runner(store, run_root, probe)

    run = runtime.start(job, {"ai_config": {"k": 1}})
    run.proc.wait(timeout=20)

    payload = json.loads((run_root / job.job_id / "probe.json").read_text(encoding="utf-8"))
    assert payload["request"]["cookies"] == {"CASLOGC": "stub"}
    assert payload["request"]["job_id"] == job.job_id
    assert payload["request"]["run_id"] == run.run_id
    assert payload["request"]["ai_config"] == {"k": 1}
    assert "CASLOGC" not in json.dumps(payload["env"])
    assert "CASLOGC" not in json.dumps(payload["argv"])


def test_start_rejects_non_ready_status(store: CourseJobStore, run_root: Path) -> None:
    from core.courses.errors import CourseTransitionError

    job = make_job(store, status=CourseStatus.DRAFT)
    runtime = runner(store, run_root, ECHO_BRIDGE)

    with pytest.raises(CourseTransitionError):
        runtime.start(job)
    assert store.get_job(job.job_id).status is CourseStatus.DRAFT
    assert store.active_slot("zhihuishu") is None


def test_second_start_raises_busy(
    store: CourseJobStore, run_root: Path, slow_bridge: Path
) -> None:
    job = make_job(store)
    runtime = runner(store, run_root, slow_bridge)

    runtime.start(job)
    try:
        with pytest.raises(CourseBusy):
            runtime.start(job)
    finally:
        assert runtime.stop(job.job_id) is True


def test_start_without_bridge_raises(store: CourseJobStore, run_root: Path) -> None:
    job = make_job(store)
    runtime = runner(store, run_root, run_root / "missing_bridge.py")

    with pytest.raises(CourseError):
        runtime.start(job)
    assert store.get_job(job.job_id).status is CourseStatus.READY


def test_start_without_cookies_asks_for_login(
    store: CourseJobStore, run_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_module, "_site_cookies", lambda site_id: {})
    job = make_job(store)
    runtime = runner(store, run_root, ECHO_BRIDGE)

    with pytest.raises(CourseAuthRequired):
        runtime.start(job)
    assert store.get_job(job.job_id).status is CourseStatus.READY


# ------------------------------------------------------------------ 停止


@pytest.mark.parametrize("fail_state_write", [False, True])
def test_start_failure_kills_process_and_releases_slot(
    store: CourseJobStore, run_root: Path, slow_bridge: Path,
    monkeypatch: pytest.MonkeyPatch, fail_state_write: bool,
) -> None:
    job = make_job(store)
    runtime = runner(store, run_root, slow_bridge)
    runs = []
    original_update = store.update

    def fail_request(run, payload):
        runs.append(run)
        raise OSError("broken stdin")

    def update(current, **changes):
        if fail_state_write and changes.get("status") is CourseStatus.FAILED:
            raise OSError("disk unavailable")
        return original_update(current, **changes)

    monkeypatch.setattr(runtime, "_write_request", fail_request)
    monkeypatch.setattr(store, "update", update)
    with pytest.raises(CourseError):
        runtime.start(job)
    assert runs[0].proc.poll() is not None
    assert store.active_slot(job.site_id) is None
    assert runtime.run_of(job.job_id) is None
    if not fail_state_write:
        assert store.get_job(job.job_id).status is CourseStatus.FAILED


def test_stop_cancels_within_budget_and_releases_slot(
    store: CourseJobStore, run_root: Path, slow_bridge: Path
) -> None:
    job = make_job(store)
    runtime = runner(store, run_root, slow_bridge)
    baseline = threading.active_count()
    run = runtime.start(job)
    assert run.alive

    started = time.monotonic()
    assert runtime.stop(job.job_id) is True
    elapsed = time.monotonic() - started

    assert elapsed < 8.0, elapsed
    assert run.proc.poll() is not None, "子进程必须已经退出"
    updated = store.get_job(job.job_id)
    assert updated.status is CourseStatus.CANCELLED
    assert updated.run_id is None
    assert store.active_slot("zhihuishu") is None
    assert runtime.active_job_ids() == []
    kinds = [event["event_type"] for event in store.read_business_events(job.job_id)]
    assert kinds[-2:] == ["course_stopping", "course_finished"]
    assert wait_for(lambda: threading.active_count() <= baseline, timeout=5.0)


def test_stop_is_idempotent(store: CourseJobStore, run_root: Path, slow_bridge: Path) -> None:
    job = make_job(store)
    runtime = runner(store, run_root, slow_bridge)
    runtime.start(job)

    assert runtime.stop(job.job_id) is True
    events_after_first = len(store.read_business_events(job.job_id))
    assert runtime.stop(job.job_id) is True
    assert len(store.read_business_events(job.job_id)) == events_after_first
    assert store.get_job(job.job_id).status is CourseStatus.CANCELLED


def test_stop_handles_already_exited_process(
    store: CourseJobStore, run_root: Path
) -> None:
    """子进程自己跑完（echo_bridge）后再停：仍要收成 cancelled。"""
    job = make_job(store)
    runtime = runner(store, run_root, ECHO_BRIDGE)
    run = runtime.start(job)
    run.proc.wait(timeout=20)

    assert runtime.stop(job.job_id) is True
    assert store.get_job(job.job_id).status is CourseStatus.CANCELLED


def test_stop_running_without_process_still_finalises(
    store: CourseJobStore, run_root: Path
) -> None:
    """服务重启后残留的 running（进程早没了）也要能被停掉。"""
    job = make_job(store, status=CourseStatus.RUNNING)
    runtime = runner(store, run_root, ECHO_BRIDGE)

    assert runtime.stop(job.job_id) is True
    assert store.get_job(job.job_id).status is CourseStatus.CANCELLED


# --------------------------------------------------------------- 关机清理


def test_shutdown_all_cancels_and_records_event(
    store: CourseJobStore, run_root: Path, slow_bridge: Path
) -> None:
    job = make_job(store)
    runtime = runner(store, run_root, slow_bridge)
    run = runtime.start(job)

    result = runtime.shutdown_all(timeout_seconds=8)

    assert result == {"stopped": 1, "failed": 0, "remaining": 0}
    assert run.proc.poll() is not None
    assert store.get_job(job.job_id).status is CourseStatus.CANCELLED
    kinds = [event["event_type"] for event in store.read_business_events(job.job_id)]
    assert "service_shutdown" in kinds


def test_shutdown_all_is_noop_without_runs(store: CourseJobStore, run_root: Path) -> None:
    runtime = runner(store, run_root, ECHO_BRIDGE)
    assert runtime.shutdown_all() == {"stopped": 0, "failed": 0, "remaining": 0}


def test_module_shutdown_all_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """lifespan 钩子：连共享实例都建不起来时也只能返回零计数。"""
    runtime_module.reset_shared()

    def boom() -> CourseRuntime:
        raise RuntimeError("数据目录不可用")

    monkeypatch.setattr(runtime_module, "shared", boom)
    assert runtime_module.shutdown_all() == {"stopped": 0, "failed": 0, "remaining": 0}


def test_lifespan_stops_courses_before_closing_task_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """app 的退出钩子：先收课程子进程，再关做题库（顺序反了状态写不进盘）。"""
    import asyncio

    from core.webui import app as webui

    calls: list[str] = []

    class FakeTaskStore:
        def close(self) -> None:
            calls.append("task_store_closed")

    class FakeTaskRuntime:
        def __init__(self, store: object) -> None:
            pass

        def shutdown(self, timeout_seconds: float | None = None) -> bool:
            calls.append("task_runtime_shutdown")
            return True

    class FakeCourseStore:
        def recover_on_start(self) -> dict[str, int]:
            return {"recovered": 0, "cleared_slots": 0}

    monkeypatch.setattr(webui, "TaskStore", FakeTaskStore)
    monkeypatch.setattr(webui, "TaskRuntimeController", FakeTaskRuntime)
    monkeypatch.setattr(webui, "CourseJobStore", FakeCourseStore)
    monkeypatch.setattr(webui, "ensure_user_data_migrated", lambda: None)
    monkeypatch.setattr(
        webui.course_runtime,
        "shutdown_all",
        lambda *args, **kwargs: calls.append("course_shutdown"),
    )

    async def run() -> None:
        async with webui._lifespan(webui.app):
            pass

    asyncio.run(run())

    assert calls[:2] == ["course_shutdown", "task_runtime_shutdown"]
    assert calls[-1] == "task_store_closed"
