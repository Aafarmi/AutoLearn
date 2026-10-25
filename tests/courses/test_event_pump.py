"""M8：双通道与事件泵。

三条验收线，全部用假桥接跑（不碰 vendor / 网络 / 真实 LOCALAPPDATA）：

1. stdout：`iter_jsonl → validate_event → 内存态 → job.json / runtime-events`，
   坏行只进 `protocol_errors`，不进事件文件。
2. stderr：脱敏后进 `diagnostics.log`，**一行都不许**进前端事件；超 2MB 截断。
3. 洪水：10 万行 stdout（含 100 行坏 JSON）+ 1 万行带假 Cookie 的 stderr，
   不死锁、不丢终局 result、事件文件里没有畸形项。
"""

from __future__ import annotations

import json
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
from core.courses.protocol import validate_event
from core.courses.runtime import (
    REDACTED,
    CourseRuntime,
    iter_jsonl,
    sanitize_diagnostic,
)
from core.courses.store import CourseJobStore

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ECHO_BRIDGE = FIXTURES / "echo_bridge.py"
FLOOD_BRIDGE = FIXTURES / "flood_bridge.py"


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
    monkeypatch.setattr(runtime_module, "_site_cookies", lambda site_id: {"CASLOGC": "stub"})


def make_job(store: CourseJobStore) -> CourseJob:
    now = timestamp()
    job = CourseJob(
        job_id=job_id(),
        site_id="zhihuishu",
        title="事件泵测试 · 共享课",
        mode=CourseMode.SHARED,
        course_id="123456",
        class_id=None,
        status=CourseStatus.READY,
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
        store, bridge=bridge, run_dir_factory=lambda item: _mkdir(run_root / item)
    )


def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def wait_for(predicate, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def drain(runtime: CourseRuntime, run, timeout: float = 30.0) -> bool:
    """等进程退出、消费者排空和 M9 终态落盘。"""
    assert wait_for(lambda: run.proc.poll() is not None, timeout)
    assert wait_for(lambda: run.consumer is not None and not run.consumer.is_alive(), timeout)
    return run.finalized_event.wait(timeout)


def diagnostics(runtime: CourseRuntime, job: CourseJob) -> str:
    path = runtime._store.job_dir(job.job_id) / runtime_module.DIAGNOSTICS_FILE
    return path.read_text(encoding="utf-8") if path.exists() else ""


# --------------------------------------------------------------- iter_jsonl


def test_iter_jsonl_default_behaviour_is_unchanged() -> None:
    lines = ["", "  \n", "not json\n", "[1]\n", '{"type":"hello"}\n']

    assert list(iter_jsonl(lines)) == [{"type": "hello"}]


def test_iter_jsonl_reports_invalid_line_lengths_only() -> None:
    sizes: list[int] = []
    lines = ["", "not json\n", "[1]\n", '{"type":"hello"}\n']

    events = list(iter_jsonl(lines, on_invalid=sizes.append))

    assert events == [{"type": "hello"}]
    assert len(sizes) == 2, "空行不算脏行，坏 JSON 与非对象各记一次"
    assert sizes[0] == len("not json")


def test_iter_jsonl_callback_failure_does_not_break_the_stream() -> None:
    def boom(_size: int) -> None:
        raise RuntimeError("callback broken")

    assert list(iter_jsonl(["junk\n", '{"type":"hello"}\n'], on_invalid=boom)) == [
        {"type": "hello"}
    ]


# ------------------------------------------------------------------ 脱敏


@pytest.mark.parametrize(
    "raw",
    [
        "CASLOGC=abcdef123456; path=/",
        "'CASLOGC': 'abcdef123456'",
        "Cookie: SESSION=abcdef123456; Path=/",
        "cookie=abcdef123456",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig",
        "authorization=abcdef123456",
        "header Bearer abcdef1234567890",
        f"token {('a' * 32)}",
        f"uuid={('0f' * 16)}",
    ],
)
def test_sanitize_diagnostic_redacts_credentials(raw: str) -> None:
    cleaned = sanitize_diagnostic(raw)

    assert "abcdef123456" not in cleaned or "abcdef123456" in REDACTED
    assert "eyJhbGciOiJIUzI1NiJ9" not in cleaned
    assert REDACTED in cleaned


def test_sanitize_diagnostic_keeps_url_path_but_drops_query() -> None:
    cleaned = sanitize_diagnostic(
        "GET https://zhihuishu.example/learn/v2?uuid=deadbeef&token=cafe HTTP/1.1"
    )

    assert "zhihuishu.example/learn/v2" in cleaned
    assert "deadbeef" not in cleaned
    assert "token=" not in cleaned


def test_sanitize_diagnostic_is_harmless_on_plain_text() -> None:
    text = "Traceback (most recent call last): 42 lines, exit 1"

    assert sanitize_diagnostic(text) == text
    assert sanitize_diagnostic(None) == ""


# ------------------------------------------------------------- stdout 事件泵


def test_stdout_pump_updates_job_and_events(
    store: CourseJobStore, run_root: Path
) -> None:
    job = make_job(store)
    before = store.get_job(job.job_id)
    runtime = runner(store, run_root, ECHO_BRIDGE)

    run = runtime.start(job, {"total_count": 2})
    assert drain(runtime, run)

    after = store.get_job(job.job_id)
    # echo_bridge 只发 hello/progress/result，没有 phase 事件；phase 不随 progress 猜。
    assert after.phase is CoursePhase.RESULT
    assert after.status is CourseStatus.COMPLETED
    assert after.progress_percent == 100.0
    assert (after.completed_count, after.total_count) == (2, 2)
    assert after.updated_at > before.updated_at, "进度变化必须刷新 updated_at（§5.6-3）"

    events = store.read_runtime_events(job.job_id)
    assert [event["type"] for event in events] == ["hello", "progress", "result"]
    assert all(validate_event(event) is not None for event in events)
    assert run.had_result and run.result_payload is not None
    assert run.result_payload["status"] == "completed"
    assert run.protocol_errors == 0

    kinds = [event["event_type"] for event in store.read_business_events(job.job_id)]
    assert "course_phase_changed" not in kinds, "没有 phase 事件就不能凭空造阶段变化"


def test_phase_change_is_recorded_once(store: CourseJobStore, run_root: Path) -> None:
    """起点 phase=setup，桥接只报 learning：只该有一条阶段事件。"""
    job = make_job(store)
    runtime = runner(store, run_root, FLOOD_BRIDGE)

    run = runtime.start(job, {"flood_lines": 1})
    assert drain(runtime, run)

    phases = [
        event
        for event in store.read_business_events(job.job_id)
        if event["event_type"] == "course_phase_changed"
    ]
    assert [event["operation"] for event in phases] == ["learning"]
    finalized = store.get_job(job.job_id)
    assert finalized.phase is CoursePhase.RESULT
    assert finalized.status is CourseStatus.COMPLETED


def test_progress_does_not_reset_counts_when_absent(
    store: CourseJobStore, run_root: Path, tmp_path: Path
) -> None:
    """current 事件不带计数：它不许把 progress 写好的计数清零。"""
    bridge = tmp_path / "counts_bridge.py"
    bridge.write_text(
        "import json, sys\n"
        "sys.stdin.readline()\n"
        "def emit(p): sys.stdout.write(json.dumps(p) + '\\n'); sys.stdout.flush()\n"
        "emit({'type':'progress','stage':'learning','current':1,'total':4,'percent':25,"
        "'completed_count':1,'total_count':4})\n"
        "emit({'type':'current','stage':'learning','title':'第一章'})\n"
        "emit({'type':'result','status':'completed','completed_count':3,'total_count':4,"
        "'failed_count':1,'safe_error_category':None})\n",
        encoding="utf-8",
    )
    job = make_job(store)
    runtime = runner(store, run_root, bridge)

    run = runtime.start(job)
    assert drain(runtime, run)

    after = store.get_job(job.job_id)
    assert after.current_title == "第一章"
    assert after.progress_percent == 100.0
    assert after.total_count == 4
    assert after.failed_count == 1


# ------------------------------------------------------------- stderr 通道


def test_stderr_never_reaches_runtime_events_and_is_redacted(
    store: CourseJobStore, run_root: Path
) -> None:
    job = make_job(store)
    runtime = runner(store, run_root, FLOOD_BRIDGE)

    run = runtime.start(job, {"flood_lines": 20, "flood_stderr": 20})
    assert drain(runtime, run)

    log = diagnostics(runtime, job)
    assert log.strip(), "stderr 必须落到 diagnostics.log"
    assert runtime_module.sanitize_diagnostic("CASLOGC=" + "a" * 40) in log
    assert "0123456789abcdef0123456789abcdef" not in log, "假 Cookie 必须已脱敏"
    assert "CASLOGC=" + "0123456789abcdef" not in log
    assert "Bearer 0123456789" not in log

    events = store.read_runtime_events(job.job_id)
    assert all(event["type"] in {"hello", "phase", "current", "progress", "result"} for event in events)
    dumped = json.dumps(events, ensure_ascii=False)
    assert "CASLOGC" not in dumped and "0123456789abcdef" not in dumped
    assert "loading" not in dumped, "stderr 一行都不许混进前端事件"


def test_diagnostics_log_is_truncated_at_cap(
    store: CourseJobStore, run_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_module, "DIAGNOSTICS_MAX_BYTES", 4096)
    monkeypatch.setattr(runtime_module, "DIAGNOSTICS_FLUSH_BYTES", 512)
    job = make_job(store)
    runtime = runner(store, run_root, FLOOD_BRIDGE)

    run = runtime.start(job, {"flood_lines": 10, "flood_stderr": 500})
    assert drain(runtime, run)

    log = diagnostics(runtime, job)
    assert runtime_module.LOG_TRUNCATED_MARKER in log
    assert len(log.encode("utf-8")) <= 4096
    assert run.diagnostics_truncated


# ------------------------------------------------------------------ 洪水验收


def test_flood_100k_stdout_and_10k_stderr_has_no_deadlock(
    store: CourseJobStore, run_root: Path
) -> None:
    """验收：10 万行 stdout（含 100 行坏 JSON）+ 1 万行 stderr，无死锁。"""
    job = make_job(store)
    runtime = runner(store, run_root, FLOOD_BRIDGE)
    baseline = threading.active_count()

    started = time.monotonic()
    run = runtime.start(
        job, {"flood_lines": 100_000, "flood_bad": 100, "flood_stderr": 10_000}
    )
    assert drain(runtime, run, timeout=180.0)
    elapsed = time.monotonic() - started

    assert elapsed < 120.0, f"洪水没跑完或被反压卡死：{elapsed:.1f}s"
    assert run.protocol_errors == 100, run.protocol_errors
    assert run.had_result and run.result_payload["status"] == "completed"

    events = store.read_runtime_events(job.job_id)
    assert events, "洪水之后事件文件不能是空的"
    assert all(validate_event(event) is not None for event in events)
    sequences = [event["sequence"] for event in events]
    assert sequences == sorted(sequences), "sequence 必须有序"
    assert len(events) <= 500, "事件文件按 §4.3 封顶 500 行"

    log = diagnostics(runtime, job)
    assert "0123456789abcdef0123456789abcdef" not in log

    after = store.get_job(job.job_id)
    assert after.progress_percent == 100.0
    assert after.completed_count == 100_000

    # 泵线程全部退出：4 个（1 消费者 + 2 读线程 + 1 watcher）不许残留。
    assert wait_for(lambda: threading.active_count() <= baseline, timeout=15.0), (
        threading.active_count(),
        baseline,
    )
