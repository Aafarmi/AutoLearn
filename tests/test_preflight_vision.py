"""启动预检：**模型不支持视觉时必须拦在启动前**。

实测事故（2026-09-28）：预检只查「有没有模型配置」，不查「那套配置能不能看图」。
于是选了纯文本模型的运行被放行、照常起跑，直到读题才失败 ——
界面上只有一句笼统的 ``perception_failed``（那还是兜底用的），
排查方向被引到「通道读不到」上，而真正的原因是**模型根本收不了图**。

引导卡片 ``GUIDE_VISION_UNSUPPORTED`` 其实早就写好了，只是一直**没有调用方**。
这组用例把那条漏接的线接上并钉住。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.config import RunConfig
from core.enums import ErrorCode
from ui.runner import TaskStartError, preflight


def _registry(*supports: bool | None) -> Any:
    """造一条假的活动链。``None`` 表示「还没测过」（``capabilities`` 为空）。"""
    profiles = [
        SimpleNamespace(
            profile_id=f"p{index}",
            capabilities=None if cap is None else SimpleNamespace(supports_vision=cap),
        )
        for index, cap in enumerate(supports)
    ]
    return SimpleNamespace(active_chain=lambda: profiles)


def test_preflight_rejects_a_chain_that_cannot_see_images() -> None:
    """**明确知道**都不支持视觉 → 拦，并且必须给出下一步动作。"""
    with pytest.raises(TaskStartError) as excinfo:
        preflight(RunConfig(), _registry(False, False))

    assert excinfo.value.code == ErrorCode.VISION_UNSUPPORTED.value
    assert excinfo.value.next_action, "引导卡没给下一步，用户就卡在这儿了"


def test_preflight_allows_a_chain_with_at_least_one_vision_model() -> None:
    """链里有一套支持视觉就够（降级链本来就是这个语义）。"""
    preflight(RunConfig(), _registry(False, True))


def test_preflight_does_not_block_models_that_were_never_tested() -> None:
    """``capabilities`` 为空 = **还没测过**，不能当成"不支持"。

    否则第一个装好软件、还没点过「测试连接」的用户会被拦在门外 ——
    而他的配置很可能本来就是好的。
    """
    preflight(RunConfig(), _registry(None))
    preflight(RunConfig(), _registry(None, False))


def test_preflight_rejects_desktop_windows_before_touching_models() -> None:
    """顺序是有讲究的：**先看目标，再看模型**。

    目标类型根本跑不了时，配多少模型都没用 —— 反过来先报「模型不支持视觉」，
    用户会去换一套模型、再点一次，才被告知这个目标根本不行，白忙一场。
    """
    cfg = RunConfig(target_kind="desktop_window", target_id="win:1")
    with pytest.raises(TaskStartError) as excinfo:
        preflight(cfg, _registry(False))

    assert excinfo.value.code == ErrorCode.TARGET_UNAVAILABLE.value
