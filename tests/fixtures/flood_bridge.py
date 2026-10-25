"""压力用假桥接（M8）：按请求里的行数灌 stdout，同时往 stderr 灌带假凭据的噪声。

请求行（stdin）支持四个可选字段，全部有默认值：

- `flood_lines`：合法 progress 行数（默认 2000）
- `flood_bad`：夹在中间的坏 JSON 行数（默认 0）
- `flood_stderr`：stderr 行数（默认 0）
- `flood_phase`：是否先发一条 phase（默认 true）

刻意做的事：

1. stdout 只放协议事件，噪声一律走 stderr——父进程若把两路混了，测试立刻能看出来。
2. 坏行是"非 JSON 文本"，不是缺字段的事件：用来验证 `protocol_errors` 的解析失败分支。
3. stderr 每行都带假 `CASLOGC` / `Cookie` / `Authorization`，用来验证脱敏。

不依赖 vendor，不做网络/文件访问。
"""

from __future__ import annotations

import json
import sys
from typing import Any

#: 32 位以上的假凭据：正好落在"≥32 位 base64/hex 串"规则上。
FAKE_TOKEN = "0123456789abcdef0123456789abcdef0123456789abcdef"


def emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def noise(index: int) -> None:
    sys.stderr.write(
        f"[{index}] loading https://zhihuishu.example/learn?uuid={FAKE_TOKEN}&x=1 "
        f"CASLOGC={FAKE_TOKEN}; Cookie: SESSION={FAKE_TOKEN}; "
        f"Authorization: Bearer {FAKE_TOKEN}\n"
    )
    sys.stderr.flush()


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

    def count(key: str, default: int) -> int:
        value = request.get(key, default)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default

    lines = count("flood_lines", 2000)
    bad = count("flood_bad", 0)
    stderr_lines = count("flood_stderr", 0)

    emit(
        {
            "type": "hello",
            "version": "flood-1",
            "mode": request.get("mode", "shared"),
            "upstream_version": "0.0-fixture",
        }
    )
    if request.get("flood_phase", True):
        emit({"type": "phase", "stage": "learning", "status": "started"})

    # stderr 均匀铺在 stdout 里：任何一个方向堵住都会立刻卡死（死锁检测点）。
    step = max(1, lines // stderr_lines) if stderr_lines else 0
    next_noise = 0
    bad_every = max(1, lines // bad) if bad else 0
    bad_left = bad

    total = max(lines, 1)
    for index in range(1, lines + 1):
        emit(
            {
                "type": "progress",
                "stage": "learning",
                "current": index,
                "total": total,
                "percent": round(index * 100.0 / total, 2),
                "completed_count": index,
                "total_count": total,
            }
        )
        if bad_left and index % bad_every == 0:
            sys.stdout.write(f"<not-json line {index}>\n")
            sys.stdout.flush()
            bad_left -= 1
        if step and index % step == 0 and next_noise < stderr_lines:
            noise(next_noise)
            next_noise += 1

    # 坏行没被 bad_every 铺完时补齐，位置固定在 result 之前。
    while bad_left > 0:
        sys.stdout.write("{broken json\n")
        sys.stdout.flush()
        bad_left -= 1
    while next_noise < stderr_lines:
        noise(next_noise)
        next_noise += 1

    emit(
        {
            "type": "result",
            "status": "completed",
            "completed_count": lines,
            "total_count": total,
            "failed_count": 0,
            "safe_error_category": None,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
