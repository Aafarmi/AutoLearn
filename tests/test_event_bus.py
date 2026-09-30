"""EventBus：SSE 的唯一事件源，投递必须不阻塞、不串号、慢消费者不拖垮内存。"""

from __future__ import annotations

import asyncio

import pytest

from core.events import Event
from core.trace import EventBus


async def test_emit_reaches_subscriber() -> None:
    bus = EventBus()
    stream = bus.subscribe()
    bus.emit(Event.RUN_STARTED, {"run_id": "r1"})
    async with asyncio.timeout(1.0):
        event, payload = await anext(stream)
    await stream.aclose()
    assert event == "run.started"
    assert payload == {"run_id": "r1"}


async def test_fanout_to_multiple_subscribers() -> None:
    bus = EventBus()
    a, b = bus.subscribe(), bus.subscribe()
    assert bus.subscriber_count == 2
    bus.emit(Event.LOG_LINE, {"line": "hi"})
    async with asyncio.timeout(1.0):
        assert (await anext(a))[0] == "log.line"
        assert (await anext(b))[0] == "log.line"
    await a.aclose()
    await b.aclose()
    assert bus.subscriber_count == 0


async def test_unsubscribe_on_close() -> None:
    bus = EventBus()
    stream = bus.subscribe()
    assert bus.subscriber_count == 1
    await stream.aclose()
    assert bus.subscriber_count == 0


async def test_event_order_is_preserved() -> None:
    bus = EventBus()
    stream = bus.subscribe()
    for i in range(5):
        bus.emit(Event.SOLVE_VOTE, {"i": i})
    got = []
    async with asyncio.timeout(1.0):
        for _ in range(5):
            got.append((await anext(stream))[1]["i"])
    await stream.aclose()
    assert got == [0, 1, 2, 3, 4]


async def test_slow_consumer_drops_oldest_not_newest() -> None:
    """慢消费者溢出时丢最旧的，保证「最新状态」永远拿得到。"""
    bus = EventBus(queue_size=3)
    bus.emit(Event.LOG_LINE, {"i": 0})  # 无订阅者，直接丢弃
    stream = bus.subscribe()
    for i in range(1, 6):
        bus.emit(Event.LOG_LINE, {"i": i})
    got = []
    async with asyncio.timeout(1.0):
        for _ in range(3):
            got.append((await anext(stream))[1]["i"])
    await stream.aclose()
    assert got == [3, 4, 5], f"应保留最新三条，实际 {got}"


async def test_close_without_reading_releases_subscriber() -> None:
    """回归：客户端秒断（订阅后一条没读就关）不得泄漏订阅者。"""
    bus = EventBus()
    stream = bus.subscribe()
    assert bus.subscriber_count == 1
    await stream.aclose()
    assert bus.subscriber_count == 0
    assert stream.closed


async def test_unknown_event_name_does_not_raise() -> None:
    """拼错事件名只记警告，不能把正在跑的流程打断。"""
    bus = EventBus()
    stream = bus.subscribe()
    bus.emit("totally.made.up", {})
    async with asyncio.timeout(1.0):
        event, _ = await anext(stream)
    await stream.aclose()
    assert event == "totally.made.up"


@pytest.mark.parametrize("name", [Event.MEDIA_INTERRUPT_DETECTED, Event.STACK_PUSHED])
async def test_media_and_stack_events_are_registered(name: str) -> None:
    bus = EventBus()
    stream = bus.subscribe()
    bus.emit(name, {"ok": True})
    async with asyncio.timeout(1.0):
        event, _ = await anext(stream)
    await stream.aclose()
    assert event == name
