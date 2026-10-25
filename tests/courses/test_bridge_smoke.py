"""T0-05 冒烟：父进程 spawn 子进程 → 逐行读 JSONL → stderr 分流。

只验证"管子通了"，不验证节流/落盘/生命周期（T3-04 / T3-01）。
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from core.courses.protocol import EVENT_TYPES, MAX_LINE_BYTES, validate_event
from core.courses.runtime import iter_jsonl, spawn

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "echo_bridge.py"


def _run_fixture() -> tuple[list[dict], str, str]:
    """拉起假桥接，喂一行请求，收回 (事件列表, stdout 原文, stderr 文本)。"""
    proc = spawn([sys.executable, "-u", str(_FIXTURE)], cwd=str(_FIXTURE.parent))
    payload = json.dumps({"mode": "course", "total_count": 2}) + "\n"
    stdout, stderr = proc.communicate(payload, timeout=30)
    assert proc.returncode == 0, f"子进程异常退出: {proc.returncode}, stderr={stderr!r}"
    return list(iter_jsonl(io.StringIO(stdout))), stdout, stderr


def test_reads_three_events_from_child_stdout() -> None:
    events, _, _ = _run_fixture()

    assert [event["type"] for event in events] == ["hello", "progress", "result"]
    assert len(events) == 3
    assert all(event["type"] in EVENT_TYPES for event in events)

    hello, progress, result = events
    assert hello["mode"] == "course"
    assert (progress["stage"], progress["percent"]) == ("learning", 50)
    assert progress["completed_count"] == 1
    assert result["status"] == "completed"
    assert result["failed_count"] == 0


def test_events_match_contract() -> None:
    events, _, _ = _run_fixture()

    for event in events:
        assert validate_event(event) is not None, f"不合契约: {event!r}"


def test_stderr_noise_never_reaches_event_stream() -> None:
    events, stdout, stderr = _run_fixture()

    assert "noise" in stderr, "fixture 的噪声没有落到 stderr，分流断言失效"
    # stdout 是事件专线：只有 3 行，噪声一行都不能混进来
    assert len([line for line in stdout.splitlines() if line.strip()]) == 3
    assert "noise" not in stdout
    assert "noise" not in json.dumps(events, ensure_ascii=False)
    assert all("noise" not in str(event) for event in events)


def test_iter_jsonl_drops_blank_garbage_non_object_and_oversized_lines() -> None:
    oversize = json.dumps(
        {"type": "current", "stage": "learning", "title": "中" * 30000},
        ensure_ascii=False,
    )
    assert len(oversize.encode("utf-8")) > MAX_LINE_BYTES

    lines = [
        "",
        "   \n",
        "not json\n",
        "[1, 2]\n",  # 合法 JSON 但不是对象
        '{"type": "hello"}\n',
        oversize + "\n",
        '{"type": "result", "status": "failed"}',  # 末行无换行也要读出来
    ]

    assert list(iter_jsonl(lines)) == [
        {"type": "hello"},
        {"type": "result", "status": "failed"},
    ]


@pytest.mark.parametrize("bad", [None, 0, 1.5])
def test_iter_jsonl_rejects_non_iterable_input(bad: object) -> None:
    with pytest.raises(TypeError):
        list(iter_jsonl(bad))  # type: ignore[arg-type]
