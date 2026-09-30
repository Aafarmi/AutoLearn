"""M4-2 限速三档：**一律带随机抖动**，且并发闸包住整个请求生命周期。"""

from __future__ import annotations

import asyncio

import pytest

from core.config import RunConfig
from core.ratelimit import (
    ConcurrencyGate,
    jitter_ms,
    sleep_click_gap,
    sleep_gap,
    sleep_submit_gap,
)


# --------------------------------------------------------------------------- #
# 抖动
# --------------------------------------------------------------------------- #
def test_jitter_stays_inside_the_window() -> None:
    values = {jitter_ms((200, 600)) for _ in range(200)}
    assert values
    assert min(values) >= 200
    assert max(values) <= 600


def test_jitter_actually_varies() -> None:
    """固定间隔等于没有限速策略 —— 一眼就会被平台认出来。"""
    assert len({jitter_ms((200, 600)) for _ in range(50)}) > 1


def test_degenerate_window_returns_that_value() -> None:
    assert jitter_ms((40, 40)) == 40


async def test_sleep_gap_waits_within_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await sleep_gap((200, 600))

    assert len(slept) == 1
    assert 0.2 <= slept[0] <= 0.6


async def test_click_gap_uses_the_run_config_window(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    cfg = RunConfig()
    await sleep_click_gap(cfg)

    low, high = cfg.rate.click_gap_ms
    assert low / 1000 <= slept[0] <= high / 1000


async def test_submit_gap_uses_seconds_not_milliseconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """提交级是 5~15 **秒**，写成毫秒就是 1000 倍的限速缺口。"""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await sleep_submit_gap(RunConfig())

    assert 5.0 <= slept[0] <= 15.0


# --------------------------------------------------------------------------- #
# 并发闸
# --------------------------------------------------------------------------- #
async def test_gate_limits_concurrency() -> None:
    gate = ConcurrencyGate(2)
    active = 0
    peak = 0

    async def job() -> None:
        nonlocal active, peak
        async with gate:
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*[job() for _ in range(8)])

    assert peak <= 2, "并发闸没有生效"


async def test_gate_releases_on_exception() -> None:
    """请求抛异常也必须释放配额 —— 否则一次限流会把并发永久锁死。"""
    gate = ConcurrencyGate(1)

    with pytest.raises(RuntimeError):
        async with gate:
            raise RuntimeError("boom")

    async with gate:  # 还能再进去
        pass


async def test_gate_limit_must_be_positive() -> None:
    with pytest.raises(ValueError):
        ConcurrencyGate(0)


async def test_gate_is_created_lazily() -> None:
    """构造期还没有事件循环，不能在那时建信号量。"""
    gate = ConcurrencyGate(3)
    assert gate.locked() is False
    async with gate:
        assert gate.limit == 3


async def test_gate_can_be_shared_across_providers() -> None:
    """一次运行一个闸：塞给多个 Provider 也只有一个配额池。"""
    gate = ConcurrencyGate(1)
    order: list[str] = []

    async def first() -> None:
        async with gate:
            order.append("a-in")
            await asyncio.sleep(0.02)
            order.append("a-out")

    async def second() -> None:
        async with gate:
            order.append("b-in")

    await asyncio.gather(first(), second())

    assert order == ["a-in", "a-out", "b-in"], "两个使用者必须串行"
