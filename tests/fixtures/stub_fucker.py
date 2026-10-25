"""Deterministic JSONL bridge used to exercise the real CourseRuntime process path."""

from __future__ import annotations

import json
import sys


def emit(event: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(event, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> int:
    request = json.loads(sys.stdin.readline())
    scenario = request.get("scenario", "normal")
    emit({"type": "hello", "version": "stub-1", "mode": request.get("mode", "shared"), "upstream_version": "fixture"})

    if scenario in {"user_stop", "timeout"}:
        # Parent owns stdin until process termination, so this blocks without sleeping.
        sys.stdin.read()
        return 0
    if scenario == "malformed":
        sys.stdout.write('{"type":"progress","stage":"learning","current":1,"total":1,"percent":101,"completed_count":0,"total_count":1}\n')
        sys.stdout.write("not-json\n")
        sys.stdout.flush()
        return 0
    if scenario == "no_result":
        return 0

    result: dict[str, object]
    if scenario == "partial":
        result = {"type": "result", "status": "partial", "completed_count": 1, "total_count": 2, "failed_count": 1, "safe_error_category": "incomplete"}
    elif scenario == "time_limit":
        result = {"type": "result", "status": "partial", "completed_count": 1, "total_count": 2, "failed_count": 0, "safe_error_category": "time_limit"}
    else:
        result = {"type": "result", "status": "completed", "completed_count": 2, "total_count": 2, "failed_count": 0, "safe_error_category": None}
    emit(result)
    return 7 if scenario == "nonzero_exit" else 0


if __name__ == "__main__":
    raise SystemExit(main())
