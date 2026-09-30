"""限速（M4-2，实现归 P7）。

三档限速，**一律带随机抖动**：

==================  ==========================================
动作级              相邻 click 间 200~600ms 随机
提交级              相邻提交间 5~15s 随机
请求级              模型 API 并发 ≤2（免费档）/ ≤3（付费档）
==================  ==========================================

``jitter_ms`` / ``sleep_gap`` 是纯函数与小工具，P0 直接给实现——M4-2 的
「必须带抖动」是常量语义的一部分，留给 P7 反而容易写出固定间隔。
"""

from __future__ import annotations

import asyncio
import random

from core.config import RunConfig

__all__ = [
    "ConcurrencyGate",
    "jitter_ms",
    "jitter_seconds",
    "sleep_click_gap",
    "sleep_gap",
    "sleep_submit_gap",
]


def jitter_ms(gap_ms: tuple[int, int]) -> int:
    """在闭区间内取一个整数毫秒。区间退化为等值时原样返回。"""
    low, high = gap_ms
    return low if low >= high else random.randint(low, high)


def jitter_seconds(gap_s: tuple[int, int]) -> float:
    """在闭区间内取一个浮点秒数。区间退化为等值时原样返回。"""
    low, high = gap_s
    return float(low) if low >= high else random.uniform(float(low), float(high))


async def sleep_gap(gap_ms: tuple[int, int]) -> None:
    """按**毫秒**区间随机休眠。"""
    await asyncio.sleep(jitter_ms(gap_ms) / 1000.0)


async def sleep_click_gap(cfg: RunConfig) -> None:
    """动作级限速：两次 click 之间（200~600 **毫秒**）。"""
    await sleep_gap(cfg.rate.click_gap_ms)


async def sleep_submit_gap(cfg: RunConfig) -> None:
    """提交级限速：两次提交之间（5~15 **秒**）。

    .. warning::
       ``rate.submit_gap_s`` 的单位是**秒**，而 :func:`sleep_gap` 吃的是**毫秒**。
       早先这里错手把秒值喂给了 ``sleep_gap``，于是「5~15 秒」实际只睡了
       5~15 **毫秒** —— 限速形同虚设，而且没有任何测试会失败（P7 修正，
       见 ``README.md`` 与本轮验收报告）。
    """
    await asyncio.sleep(jitter_seconds(cfg.rate.submit_gap_s))


class ConcurrencyGate:
    """请求级并发闸（``asyncio.Semaphore`` 包装）。

    .. warning::
       ``Semaphore`` 必须包住**整个请求生命周期**（含流式读取），
       否则并发限制形同虚设。禁止裸 ``asyncio.gather`` 直连 Provider。
    """

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError(f"并发上限至少为 1，得到 {limit}")
        self._limit = limit
        self._semaphore: asyncio.Semaphore | None = None

    @property
    def limit(self) -> int:
        return self._limit

    async def __aenter__(self) -> ConcurrencyGate:
        await self._gate().acquire()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self._gate().release()

    def _gate(self) -> asyncio.Semaphore:
        """惰性建信号量。

        ``asyncio.Semaphore`` 在构造时会尝试绑定当前事件循环，而编排层的
        装配时机（导入期、进程启动期）往往还没有循环。延迟到第一次真正
        进入时再建，避免「在错误的循环上建锁」这种只在生产偶发的问题。
        """
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._limit)
        return self._semaphore

    def locked(self) -> bool:
        """配额是否已用尽。**观测用**，不要拿它做同步。"""
        return self._semaphore is not None and self._semaphore.locked()
