"""测试用假桥接：读一行请求，往 stdout 吐 hello / progress / result 三条 JSONL。

刻意做两件"上游会做的事"，用来验证父进程的分流：

1. 往 **stderr** 打一行 `noise`（真实上游满屏 `print`，桥接会把 stdout 导向
   stderr，见 §4.6 第 4 条）——stderr 的内容绝不能混进事件流。
2. 事件之间插入一次 `time.sleep` 之外的无关输出不做，保持逐行可复现。

不依赖真实 vendor，也不做任何网络/文件访问。
"""

from __future__ import annotations

import json
import sys
from typing import Any


def emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    line = sys.stdin.readline()
    request: dict[str, Any] = {}
    if line.strip():
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            request = parsed

    mode = request.get("mode")
    mode = mode if isinstance(mode, str) and mode else "course"
    total = request.get("total_count")
    total = total if isinstance(total, int) and total > 0 else 2

    emit(
        {
            "type": "hello",
            "version": "echo-1",
            "mode": mode,
            "upstream_version": "0.0-fixture",
        }
    )

    # 分流测试点：这行只能出现在 stderr，不能进事件流。
    print("noise", file=sys.stderr)
    sys.stderr.flush()

    emit(
        {
            "type": "progress",
            "stage": "learning",
            "current": 1,
            "total": total,
            "percent": 50,
            "completed_count": 1,
            "total_count": total,
        }
    )
    emit(
        {
            "type": "result",
            "status": "completed",
            "completed_count": total,
            "total_count": total,
            "failed_count": 0,
            "safe_error_category": None,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
