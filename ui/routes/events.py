"""SSE 事件路由（P5）。

三项必备处理，缺一不可（规划书 §2.5）：

1. 响应头带 ``X-Accel-Buffering: no`` —— 否则反向代理会缓冲，事件不实时；
2. **每 15s 发一行注释心跳**（``: keep-alive``）—— 否则中间层会掐掉空闲连接；
3. 客户端**重连后必须主动拉 ``/api/run/progress`` 全量对账** ——
   断线期间的事件是真的丢了，SSE 不补发。后端这边只负责把进度接口做对。

事件格式：``event: <domain>.<object>.<action>\\ndata: {...}\\n\\n``
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ui.deps import get_event_bus

__all__ = ["HEARTBEAT_S", "SSE_HEADERS", "router"]

#: 心跳间隔（秒）。空闲超过这个时间就发一行注释，证明连接还活着。
HEARTBEAT_S = 15.0

#: 必备响应头
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
    "Content-Encoding": "identity",
}

router = APIRouter(prefix="/api", tags=["events"])


def _frame(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    """``text/event-stream``。消费 ``EventBus.subscribe()``。

    .. important::
       **不要在这里调 ``request.is_disconnected()``。** Starlette 的
       ``StreamingResponse`` 已经起了独立的 ``listen_for_disconnect`` 任务，
       客户端一断就把整个流任务 cancel 掉，本函数的 ``finally`` 随之执行、
       订阅被摘除。

       手动调 ``is_disconnected()`` 会在当前任务上反复 ``cancel()`` 再吸收，
       把 Starlette 那一次真正的外层取消搅乱 —— 实测结果是**订阅永久泄漏**，
       断开十秒后 ``EventBus`` 里还挂着僵尸。少写这一行反而正确。

    ``request`` 参数保留给将来的接入日志 / 指纹识别，当前不参与流程。
    """
    bus = get_event_bus()

    async def stream():
        yield ": connected\n\n"
        subscription = bus.subscribe()
        try:
            while True:
                try:
                    event, payload = await asyncio.wait_for(
                        subscription.__anext__(), timeout=HEARTBEAT_S
                    )
                except TimeoutError:
                    # 注释行：SSE 规范里以 ':' 开头的行会被客户端忽略
                    yield ": keep-alive\n\n"
                    continue
                except StopAsyncIteration:  # pragma: no cover - 订阅被外部关闭
                    break
                yield _frame(event, payload)
        finally:
            await subscription.aclose()

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)
