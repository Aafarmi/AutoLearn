"""UTC timestamps shared by task and course records.

口径（与 `core/tasks/store.py` 现有记录一致）：UTC、毫秒精度、以 `Z` 结尾。
这套口径原本在做题存储里实现过一次，课程存储又抄了一份——现在统一放这里，
新代码直接 import；做题存储仍保留自己的同名私有函数，迁移是一次
"删三个函数 + 加一行 import" 的独立改动，不与功能改动混在一起。
"""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    """当前 UTC 时间。"""
    return datetime.now(timezone.utc)


def timestamp(value: datetime | None = None) -> str:
    """把时间点格式化成记录用字符串（UTC、毫秒、`Z` 结尾）。"""
    current = value or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def parse_timestamp(value: str) -> datetime:
    """解析记录里的时间字符串，统一成带时区的 UTC 时间。"""
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


__all__ = ["parse_timestamp", "timestamp", "utc_now"]
