"""留痕文件路由（P5）。

``GET /api/logs/{run_id}/{item_id}/{kind}`` —— ``kind`` ∈
``perception`` / ``solve`` / ``action`` / ``verify``，对应 ``<kind>.json``。

路径穿越防护：三个参数都会被拼进文件路径，因此**逐个走字符集白名单**。
只要有一个字符不在 ``[A-Za-z0-9._-]`` 里就直接 404，``../../`` 在第一步就死掉；
拼好之后再做一次「解析后的绝对路径必须仍在留痕根目录内」的复核。
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import PlainTextResponse

from ui.store import log_root

__all__ = ["ALLOWED_KINDS", "SAFE_SEGMENT", "router"]

#: 允许读取的留痕种类
ALLOWED_KINDS: frozenset[str] = frozenset({"perception", "solve", "action", "verify"})

#: 路径段白名单。**不含斜杠与反斜杠**，也不含 ``%``（避免二次解码绕过）。
SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

router = APIRouter(prefix="/api/logs", tags=["artifacts"])


def _safe_segment(value: str, field: str) -> str:
    if not SAFE_SEGMENT.match(value) or value in {".", ".."}:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "bad_path", "message": f"{field} 含有非法字符"},
        )
    return value


@router.get("/{run_id}/{item_id}/{kind}", response_class=PlainTextResponse)
async def read_log(run_id: str, item_id: str, kind: str) -> PlainTextResponse:
    """返回 ``text/plain`` 的留痕 JSON 原文。"""
    _safe_segment(run_id, "run_id")
    _safe_segment(item_id, "item_id")
    if kind not in ALLOWED_KINDS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "bad_kind", "message": f"不支持的留痕种类 {kind}"},
        )

    root = log_root().resolve()
    target = (root / run_id / item_id / f"{kind}.json").resolve()
    if not str(target).startswith(str(root)) or not target.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "log_not_found", "message": "留痕文件不存在"},
        )
    return PlainTextResponse(
        target.read_text(encoding="utf-8", errors="replace"),
        media_type="text/plain; charset=utf-8",
    )
