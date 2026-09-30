"""契约完整性与引导文案独立性守卫。

两件事：

1. **§2.2 的每一个签名都必须可 import、可取到** —— 骨架冻结的意义就在于此，
   下游才能并行开工。少一个名字，就有人要停下来问。
2. **四类引导文案必须各自独立**（M2-8 明令禁止共用一句泛化错误）。
"""

from __future__ import annotations

import importlib

import pytest

from ui.guide import (
    GUIDE_AUTH_FAILED,
    GUIDE_EMPTY,
    GUIDE_FIRST_RUN,
    GUIDE_MODEL_NOT_FOUND,
    GUIDE_RATE_LIMITED,
    guide_for,
)

#: §2.2 契约清单：``模块路径 -> 必须存在的成员``
CONTRACT: dict[str, tuple[str, ...]] = {
    # ---- core ----
    "core.config": (
        "GuardThresholds",
        "RateLimits",
        "RunConfig",
        "load_run_config",
        "save_run_config",
        "active_probe_chain",
        "ProbeName",
        "TierUsed",
    ),
    "core.models": (
        "Option",
        "Question",
        "Answer",
        "ActionResult",
        "VideoState",
        "VideoTask",
        "PerceptionResult",
        "VoteResult",
        "VerifyResult",
        "TaskItem",
        "CapabilityReport",
        "SampleRecord",
    ),
    "core.qid": ("normalize_text", "make_qid", "make_stem_hash"),
    "core.vid": ("make_vid",),
    "core.states": (
        "TRANSITIONS",
        "DANGER_STATES",
        "can_transition",
        "require_transition",
        "resume_entry",
    ),
    "core.tasks": ("TaskType", "TaskItem", "SuspendFrame", "TaskStack", "build_task_sequence"),
    "core.model_registry": ("ModelProfile", "ModelRegistry", "CredentialStore", "test_connection"),
    "core.arbiter": ("decide_channel", "arbitrate", "DecisionTrace"),
    "core.orchestrator": ("RunContext", "Orchestrator"),
    "core.events": ("Event",),
    "core.trace": ("RunLogger", "EventBus"),
    "core.db": ("SCHEMA_SQL", "connect", "init_db"),
    "core.ratelimit": ("jitter_ms", "sleep_gap", "ConcurrencyGate"),
    "core.enums": (
        "ProbeName",
        "TierUsed",
        "SolvePath",
        "QuestionState",
        "MediaState",
        "TaskType",
        "QType",
        "ActionKind",
        "ActLevel",
        "VerifyKind",
        "ProviderName",
        "ErrorCode",
    ),
    # ---- perception ----
    "perception.base": ("BaseProbe",),
    "perception.media_probe": (
        "MediaProbe",
        "read_video_state",
        "wait_for_playback",
        "wait_for_ended",
        "wait_for_interrupt",
        "read_episode_index",
    ),
    "perception.vision_probe": ("VisionProbe",),
    "perception.pipeline": ("PerceptionPipeline", "PerceptionContext"),
    # ---- solve ----
    "solve.providers.base": (
        "LLMRequest",
        "LLMResponse",
        "LLMProvider",
        "ProviderError",
        "AuthError",
        "RateLimitError",
        "ModelNotFoundError",
        "VisionNotSupportedError",
        "StructuredNotSupportedError",
        "ProviderName",
    ),
    "solve.providers.openai_compat": ("OpenAICompatProvider",),
    "solve.providers.mock": ("MockProvider",),
    "solve.providers.factory": (
        "build_provider",
        "build_provider_chain",
        "provider_name_of",
    ),
    "solve.voting": ("VotingEngine",),
    "solve.solver": (
        "SELF_REF_PATTERN",
        "is_self_referential",
        "allows_shuffle",
        "Solver",
    ),
    "solve.cache": ("SolveCache",),
    # ---- act ----
    "act.actuator": ("ActionKind", "ActLevel", "MEDIA_LEVELS", "Actuator"),
    "act.verifier": ("VerifyKind", "Verifier"),
    # ---- adapters ----
    "adapters.base": ("MediaAnchorSet", "BaseAdapter"),
    "adapters.mock_exam.adapter": ("MockExamAdapter",),
    # ---- ui ----
    "ui.schemas": (
        "RunConfigIn",
        "RunConfigOut",
        "TaskItemOut",
        "TaskDetailOut",
        "ModelProfileIn",
        "ModelProfileOut",
        "ConfirmIn",
        "GuideCardOut",
        "PresetOut",
    ),
    "ui.guide": (
        "GUIDE_FIRST_RUN",
        "GUIDE_EMPTY",
        "GUIDE_AUTH_FAILED",
        "GUIDE_MODEL_NOT_FOUND",
        "GUIDE_RATE_LIMITED",
        "guide_for",
    ),
    "ui.deps": ("get_registry", "get_orchestrator", "get_event_bus", "get_run_config"),
    "ui.server": ("create_app",),
    "ui.routes.run": ("router",),
    "ui.routes.tasks": ("router",),
    "ui.routes.models": ("router",),
    "ui.routes.events": ("router",),
    "ui.routes.artifacts": ("router",),
}

METHODS: dict[str, tuple[str, ...]] = {
    "core.model_registry.ModelRegistry": (
        "load",
        "save",
        "list",
        "add",
        "update",
        "remove",
        "reorder",
        "active_chain",
        "mark_disabled",
    ),
    "core.model_registry.CredentialStore": ("put", "get", "delete"),
    "core.orchestrator.Orchestrator": (
        "run",
        "pause",
        "resume",
        "stop",
        "_step_quiz",
        "_step_video",
        "_on_interrupt",
        "_persist",
        "_restore",
    ),
    "core.trace.RunLogger": ("item_dir", "save_screenshot", "save_json", "save_model_raw"),
    "core.trace.EventBus": ("emit", "subscribe"),
    "perception.base.BaseProbe": ("attach", "is_available", "probe"),
    "perception.pipeline.PerceptionPipeline": ("run", "run_video", "chain"),
    "solve.providers.base.LLMProvider": ("complete", "aclose"),
    "solve.voting.VotingEngine": ("build_index_map", "vote", "majority_ratio"),
    "solve.solver.Solver": ("solve", "build_sampling_batch", "mark_review"),
    "solve.cache.SolveCache": ("get", "put", "size"),
    "act.actuator.Actuator": (
        "select_option",
        "click",
        "submit",
        "play_media",
        "pause_media",
        "seek_media",
        "next_episode",
        "swipe",
        "_restore_scroll",
    ),
    "act.verifier.Verifier": (
        "verify_region_changed",
        "verify_playing",
        "verify_paused",
        "verify_resume_continuous",
        "verify_episode_advance",
        "should_escalate",
    ),
    "adapters.base.BaseAdapter": ("from_yaml", "matches", "media_locator"),
    "ui.deps": ("set_orchestrator",),
}


def _resolve(dotted: str) -> object:
    module_path, _, name = dotted.rpartition(".")
    return getattr(importlib.import_module(module_path), name)


@pytest.mark.parametrize("module_path", sorted(CONTRACT))
def test_module_members_exist(module_path: str) -> None:
    module = importlib.import_module(module_path)
    missing = [n for n in CONTRACT[module_path] if not hasattr(module, n)]
    assert not missing, f"{module_path} 缺：{missing}"


@pytest.mark.parametrize("dotted", sorted(METHODS))
def test_class_methods_exist(dotted: str) -> None:
    target = _resolve(dotted)
    missing = [n for n in METHODS[dotted] if not hasattr(target, n)]
    assert not missing, f"{dotted} 缺方法：{missing}"


# --------------------------------------------------------------------------- #
# 引导文案独立性（M2-8）
# --------------------------------------------------------------------------- #
def test_four_guide_cards_are_distinct() -> None:
    cards = [GUIDE_EMPTY, GUIDE_AUTH_FAILED, GUIDE_MODEL_NOT_FOUND, GUIDE_RATE_LIMITED]
    titles = [c.title for c in cards]
    bodies = [c.body_md for c in cards]
    actions = [c.next_action for c in cards]
    assert len(set(titles)) == 4, "四类情形标题重复"
    assert len(set(bodies)) == 4, "四类情形正文重复"
    assert len(set(actions)) == 4, "四类情形下一步动作重复"


def test_every_card_gives_a_next_action() -> None:
    for card in (
        GUIDE_FIRST_RUN,
        GUIDE_EMPTY,
        GUIDE_AUTH_FAILED,
        GUIDE_MODEL_NOT_FOUND,
        GUIDE_RATE_LIMITED,
    ):
        assert card.next_action.strip(), f"{card.title} 没有给出下一步动作（置灰是引导，不是静默失败）"


def test_first_run_card_is_not_an_error() -> None:
    """首次运行要展示引导卡而不是报错；且必须说清「现在跑不起来」。

    v0.2.0：读题只有「模型看截图」一条路，所以「一条模型配置都没有」不再是
    「退化成 Mock 空跑」的软状态，而是**硬拦**。这张卡必须把这件事说明白，
    否则用户会以为是软件坏了。
    """
    card = guide_for(None)
    assert card is GUIDE_FIRST_RUN
    assert "no_config" in card.body_md, "要说清拦下时给的是哪个错误码"
    assert "Mock" in card.body_md, "要交代「不会退回 Mock 空跑」这件事"
    assert "模型" in card.body_md and "图片" in card.body_md, "要说明缺的是支持图片的模型"


def test_guide_lookup_by_error_code() -> None:
    assert guide_for("auth_failed") is GUIDE_AUTH_FAILED
    assert guide_for("model_not_found") is GUIDE_MODEL_NOT_FOUND
    assert guide_for("rate_limited") is GUIDE_RATE_LIMITED
    assert guide_for("no_config") is GUIDE_FIRST_RUN


def test_unknown_error_code_returns_none_not_a_generic_message() -> None:
    """未登记的码必须返回 None，逼调用方显式处理，而不是悄悄弹泛化错误。"""
    assert guide_for("some_brand_new_error") is None
