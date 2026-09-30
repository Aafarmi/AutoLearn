"""P2/P3 流水线集成验收（真跑靶场）。

v0.2.0 起题目链只剩**视觉**一条，本文件覆盖：
    - 探针链恒为 ``[vision]``，媒体探针挂在流水线上但不在题目链里；
    - 端到端截到当前视口（这是读题的前半段，读题归 Tier2 模型）；
    - 裁不出图时**暂停留档**，绝不静默跳过；
    - 媒体流程独立于题目链；
    - **零模型调用**（M0 验收要求）。
"""

from __future__ import annotations

import sys

from core.config import ProbeName, RunConfig
from perception.media_probe import MediaProbe
from perception.pipeline import PerceptionContext, PerceptionPipeline
from perception.vision_probe import VisionProbe
from tests.helpers import Q_BASE, course_url, open_quiz, quiz_url


def _pipeline(cfg: RunConfig) -> PerceptionPipeline:
    return PerceptionPipeline([VisionProbe()], cfg)


def test_chain_is_always_vision_only() -> None:
    assert _pipeline(RunConfig()).chain() == [ProbeName.VISION]


def test_media_probe_is_attached_but_out_of_question_chain() -> None:
    pipeline = _pipeline(RunConfig())
    assert pipeline.probe(ProbeName.MEDIA) is not None
    assert ProbeName.MEDIA not in pipeline.chain()


async def test_run_crops_the_viewport(page, mock_base, adapter, pipeline):
    """跑一遍题目链：产出的是**一张图**，不是题面。

    这是 v0.2.0 的分工：感知层只负责把画面交出来，
    「图里是什么」由 ``solve/reader.py`` 调视觉模型得到。
    """
    await open_quiz(page, quiz_url(mock_base, Q_BASE))
    result = await pipeline.run(page, adapter, PerceptionContext(timeout_s=6))

    assert result.question is None, "探针只出图，题面归 Tier2"
    assert result.channel_used is ProbeName.VISION
    assert "vision:crop_ok" in result.warnings
    assert result.trace is not None
    assert result.trace.reason == "vision_only"
    assert result.trace.needs_vision is True
    assert result.review_required is False


async def test_run_pauses_for_dump_when_vision_unavailable(page, mock_base, adapter, run_config):
    """没有可截的画面 → 暂停留档，绝不静默跳过。

    注意「视觉不可用」的门槛已经被压到最低（只要有 ``body`` 就能截），
    所以这里必须真的把探针弄死才能命中这条分支。
    """

    class _DeadVision(VisionProbe):
        async def is_available(self, page, adapter) -> bool:
            return False

    pipeline = PerceptionPipeline([_DeadVision()], run_config)
    await page.set_content("<html><body><p>什么都读不到的页面</p></body></html>")

    result = await pipeline.run(page, adapter, PerceptionContext(timeout_s=1))

    assert result.question is None
    assert result.review_required is True
    assert result.trace is not None
    assert result.trace.paused_for_dump is True


async def test_vision_probe_needs_no_page_anchors(page, run_config):
    """**真实站点形态**：页面没有任何靶场锚点时，视觉通道照样必须能出手。

    这一条钉的是 P11 修掉的一个把整条链路堵死的缺陷：``VisionProbe.is_available``
    原先去查题目锚点（靶场的 ``data-quiz``），真实站点上一个都没有，
    于是**视觉通道永远不可用**。v0.2.0 已经没有别的通道可退，
    这个判据更不能再收紧。
    """
    pipeline = PerceptionPipeline([VisionProbe()], run_config)
    await page.set_content("<html><body><h1>第 1 题</h1><p>一道没有锚点的题</p></body></html>")

    result = await pipeline.run(page, "unused-adapter", PerceptionContext(timeout_s=1))

    assert result.trace is not None
    assert result.trace.reason == "vision_only", "有画面就必须走视觉"
    assert result.trace.paused_for_dump is False


async def test_run_video_returns_state(page, mock_base, adapter, pipeline):
    await page.goto(course_url(mock_base, dur=20), wait_until="domcontentloaded", timeout=15000)
    await page.wait_for_selector('[data-media="video"]', state="attached", timeout=8000)
    # 等元数据就绪再读。``state="attached"`` 只保证元素挂进文档，
    # 此时 ``duration`` 可能还是 0（媒体还没 loadedmetadata）——
    # 实测过这一条会偶发红，红的是测试没等够，不是探针读错。
    await page.wait_for_function(
        "() => { const v = document.querySelector('[data-media=\"video\"]');"
        " return !!v && v.readyState >= 1; }",
        timeout=8000,
    )

    state = await pipeline.run_video(page, adapter, PerceptionContext())
    assert state.paused is True
    assert state.duration > 0
    assert state.episode_total > 0


async def test_media_probe_is_bound_by_pipeline(page, mock_base, adapter, pipeline):
    await page.goto(course_url(mock_base), wait_until="domcontentloaded", timeout=15000)
    await page.wait_for_selector('[data-media="video"]', state="attached", timeout=8000)
    await pipeline.run_video(page, adapter, PerceptionContext())

    media = pipeline.probe(ProbeName.MEDIA)
    assert isinstance(media, MediaProbe)
    assert media._video_selector == adapter.media_anchors.video


async def test_zero_model_calls_across_full_perception_run(page, mock_base, adapter, pipeline):
    """M0 验收：整轮感知**零模型调用**。

    结构上感知层不依赖 ``solve``（由 ``test_guardrails`` 卡住），运行期也不该在
    感知过程中**新引入**任何模型模块。这里比对「感知前 / 感知后」的模块集合，
    只看新增 —— 测试进程自身在收集阶段就会加载 UI 夹具（连带 ``solve``），
    拿全集比对会误判。
    """
    before = set(sys.modules)
    await open_quiz(page, quiz_url(mock_base, Q_BASE))
    result = await pipeline.run(page, adapter, PerceptionContext(timeout_s=6))
    assert result.channel_used is ProbeName.VISION

    blockers = {"solve", "openai", "anthropic"}
    introduced = sorted(m for m in set(sys.modules) - before if m.split(".")[0] in blockers)
    assert not introduced, f"感知过程引入了模型模块：{introduced}"
