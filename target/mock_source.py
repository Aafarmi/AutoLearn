"""靶场目标源：软件自带的题目 / 网课靶场（P1 交付物，P11 起**降级为测试专用**）。

为什么留着
----------
不是历史包袱，是**唯一的回归基础设施**：675 个测试全部建在靶场之上 ——
坑位覆盖（SPA / 懒加载 / Canvas / iframe / class 混淆 / 弹窗遮罩）、媒体三态、
弹题中断-恢复、断点续跑，真实站点上无法稳定复现这些场景。删掉靶场等于删掉
整条验收链。

为什么从产品 UI 摘掉
--------------------
用户要的是「抓电脑上正在运行的网页」。把「自建靶场」摆在运行配置里，
表达的是「我们只会玩自己搭的东西」—— 正是 2026-09-27 被指出的那个偏差。
所以它退到测试路径：``scripts/`` 下的验收工具与 ``tests/`` 继续用它，
产品界面不再出现。

实现上刻意**不复制**自启浏览器的逻辑，而是复用
:func:`core.orchestrator.default_page_factory` —— 那份逻辑同时被
``scripts/run_batch.py`` / ``run_course.py`` 依赖，摊成两份迟早会漂移。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from core.enums import TargetKind
from target.base import TargetHandle, TargetInfo, TargetSource

__all__ = ["MockTargetSource"]

#: 靶场目标在列表里的固定 ID。靶场只有一个入口，不需要动态发现。
MOCK_TARGET_ID = "mock-site"


class MockTargetSource(TargetSource):
    """自建靶场。**只在测试路径注册**，不进产品 UI。"""

    kind = TargetKind.BROWSER_PAGE

    def __init__(
        self,
        *,
        url: str,
        channel: str = "msedge",
        storage_state_path: Path | None = None,
        title: str = "自建靶场",
    ) -> None:
        self.url = url
        self.channel = channel
        self.storage_state_path = storage_state_path
        self.title = title

    async def list_targets(self) -> list[TargetInfo]:
        """靶场只有一个入口，直接返回它。

        **不检查靶场是否真的起着** —— 那是启动编排的事。这里返回空表会让
        调用方以为「没有目标」，而实际原因可能是「靶场没起」，两者处置完全不同。
        """
        return [
            TargetInfo(
                target_id=MOCK_TARGET_ID,
                kind=self.kind,
                title=self.title,
                url=self.url,
                app=self.channel,
            )
        ]

    @asynccontextmanager
    async def open(self, target_id: str) -> AsyncIterator[TargetHandle]:
        # 局部导入：``core.orchestrator`` 是上层，放在模块顶层会形成
        # target → core.orchestrator 的循环。此处只在真正附加时才需要它。
        from core.config import RunConfig
        from core.orchestrator import default_page_factory

        cfg = RunConfig(
            storage_state_path=self.storage_state_path or Path("state/storage_state.json")
        )
        async with default_page_factory(cfg, channel=self.channel, url=self.url) as page:
            yield TargetHandle(
                info=TargetInfo(
                    target_id=target_id or MOCK_TARGET_ID,
                    kind=self.kind,
                    title=self.title,
                    url=self.url,
                    app=self.channel,
                ),
                page=page,
            )
