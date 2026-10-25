"""Atomic whole-file writes shared by task stores, course runtime and reports.

写入策略：同目录临时文件 → `flush` → `os.fsync` → `os.replace`。同一卷上
`os.replace` 是原子的，因此进程在任意时刻被杀，目标路径要么是旧的完整文件，
要么是新的完整文件，不会是半截。

放在 `core.common` 而不是各模块自己写：仓库里已经有六处各自实现的
"tmp + replace"（`core/auth/cookie_manager.py`、`core/adapters/*`、
`core/settings/model_profiles.py`、`core/solver/*`、`core/tasks/store.py`）。
新代码一律 import 这里，不再复制；旧调用点按需迁移，不做一次性大改。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, text: str) -> None:
    """把 `text` 原子写到 `path`（UTF-8、LF、必要时建父目录）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_write_json(path: Path, payload: Any) -> None:
    """把 JSON 原子写到 `path`：缩进 2 空格、保留中文，便于人工排查。"""
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


__all__ = ["atomic_write_json", "atomic_write_text"]
