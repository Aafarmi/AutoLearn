"""运行配置：v0.2.0 起只剩模型一条读题通道，配置里不再有通道开关。"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from core.config import RunConfig, active_probe_chain, load_run_config, save_run_config
from core.enums import ProbeName, TaskType


def test_chain_is_always_vision() -> None:
    """题目通道恒为视觉一条 —— 与配置无关，也不受目标类型影响。"""
    assert active_probe_chain(RunConfig()) == [ProbeName.VISION]


def test_config_has_no_channel_switches() -> None:
    """``probe_order`` / ``probe_mode`` 已删除。

    它们的存在意味着「还有第二条通道可选」；程序只使用模型之后，
    留着这两个字段只会让人以为能切回 DOM 读页面。
    """
    fields = set(RunConfig.model_fields)
    assert "probe_order" not in fields
    assert "probe_mode" not in fields


def test_legacy_channel_switch_keys_are_ignored(tmp_path, monkeypatch) -> None:
    """旧配置里的 ``probe_order`` / ``probe_mode`` 不该把加载搞崩。"""
    monkeypatch.setenv("AUTOLEARN_RUN_CONFIG", str(tmp_path / "run_config.json"))
    path = tmp_path / "run_config.json"
    path.write_text(
        json.dumps({"probe_order": "dom-first", "probe_mode": "dom-only", "sample_n": 3}),
        encoding="utf-8",
    )
    cfg = load_run_config()
    assert cfg.sample_n == 3, "旧字段不该连带把有效设置一起丢掉"


def test_defaults_per_spec() -> None:
    cfg = RunConfig()
    assert cfg.task_sequence == [TaskType.QUIZ]


def test_sample_n_below_minimum_rejected() -> None:
    """2026-09-28 起默认每题只核对一次：``MIN_SAMPLE_N == 1``，下限跟到 1。

    「只核对一次」是用户的要求，但**投票机制还在** —— 显式调大 ``sample_n``
    仍然多重采样（见 ``tests/test_solver.py``）。
    """
    with pytest.raises(ValidationError):
        RunConfig(sample_n=0)
    assert RunConfig().sample_n == 1
    assert RunConfig(sample_n=5).sample_n == 5


def test_backup_profile_ids_default_to_empty_and_roundtrip(tmp_path, monkeypatch) -> None:
    """「主选 + 备用组」是 2026-09-28 新增的字段：默认空 = 不降级。

    默认空列表（而不是 ``None``）是刻意的：编排层与界面都按「可以没有备用」
    直接迭代，不必到处写 ``or []``。
    """
    monkeypatch.setenv("AUTOLEARN_RUN_CONFIG", str(tmp_path / "run_config.json"))
    default = RunConfig()
    assert default.backup_profile_ids == []
    assert default.vision_backup_profile_ids == []
    assert default.backup_profile_ids is not default.vision_backup_profile_ids

    save_run_config(
        RunConfig(backup_profile_ids=["b1", "b2"], vision_backup_profile_ids=["v1"])
    )
    reloaded = load_run_config()
    assert reloaded.backup_profile_ids == ["b1", "b2"]
    assert reloaded.vision_backup_profile_ids == ["v1"]


def test_empty_task_sequence_rejected() -> None:
    with pytest.raises(ValidationError):
        RunConfig(task_sequence=[])


def test_is_model_ready_follows_model_profile_id() -> None:
    assert RunConfig().is_model_ready is False
    assert RunConfig(model_profile_id="p1").is_model_ready is True


def test_llm_concurrency_follows_model_readiness() -> None:
    assert RunConfig().llm_concurrency == 2
    assert RunConfig(model_profile_id="p1").llm_concurrency == 3


def test_config_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AUTOLEARN_RUN_CONFIG", str(tmp_path / "run_config.json"))
    cfg = RunConfig(
        sample_n=7,
        task_sequence=[TaskType.QUIZ, TaskType.VIDEO],
        model_profile_id="p1",
    )
    save_run_config(cfg)
    reloaded = load_run_config()
    assert reloaded.sample_n == 7
    assert reloaded.task_sequence == [TaskType.QUIZ, TaskType.VIDEO]
    assert active_probe_chain(reloaded) == active_probe_chain(cfg)


def test_missing_config_file_yields_defaults(tmp_path, monkeypatch) -> None:
    """首次运行没有配置文件，必须返回默认值而不是报错。"""
    monkeypatch.setenv("AUTOLEARN_RUN_CONFIG", str(tmp_path / "absent.json"))
    assert load_run_config().task_sequence == [TaskType.QUIZ]


def test_saved_config_contains_no_secret_fields(tmp_path, monkeypatch) -> None:
    """M2-7：运行配置里不许出现密钥字段。"""
    monkeypatch.setenv("AUTOLEARN_RUN_CONFIG", str(tmp_path / "run_config.json"))
    save_run_config(RunConfig())
    text = (tmp_path / "run_config.json").read_text(encoding="utf-8")
    for forbidden in ("api_key", "secret", "token", "password"):
        assert forbidden not in text, f"运行配置落盘时泄漏了 {forbidden}"


# --------------------------------------------------------------------------- #
# 陈旧配置不得让接口 500（2026-09-28 线上事故回归）
# --------------------------------------------------------------------------- #
def _stale_payload(**overrides) -> dict:
    """一份「旧版本存盘、新版本代码读」的配置：嵌套 guards 里带已删除的字段。"""
    payload = RunConfig().model_dump(mode="json")
    payload["guards"].update(
        {
            "advance_scroll_probe": True,
            "advance_scroll_settle_polls": 2,
            "end_vision_check": True,
            "end_progress_max_total": 500,
            "end_unknown_action": "pause",
            "end_decision_timeout_s": 180.0,
        }
    )
    payload.update(overrides)
    return payload


def test_stale_guard_fields_do_not_break_loading(tmp_path, monkeypatch) -> None:
    """事故本体：盘上留着 P14 时代的 ``guards.end_*`` 键。

    嵌套模型是 ``extra="forbid"``，旧字段会让整份配置校验失败；而
    ``load_run_config()`` 的异常会一路冒到 ``/api/run/config``，
    症状是**每次打开控制台都是 HTTP 500**。
    正确行为是：忽略那些字段，**其余设置照用**（用户的模型选择不能丢）。
    """
    monkeypatch.setenv("AUTOLEARN_RUN_CONFIG", str(tmp_path / "run_config.json"))
    path = tmp_path / "run_config.json"
    path.write_text(
        json.dumps(_stale_payload(model_profile_id="keep-me", sample_n=4)),
        encoding="utf-8",
    )

    cfg = load_run_config()
    assert cfg.model_profile_id == "keep-me", "旧字段不该连带把有效设置一起丢掉"
    assert cfg.sample_n == 4
    assert not hasattr(cfg.guards, "end_vision_check")


def test_stale_config_file_self_heals(tmp_path, monkeypatch) -> None:
    """读一次之后盘上的文件应被清理，下次启动不再报同一条警告。"""
    monkeypatch.setenv("AUTOLEARN_RUN_CONFIG", str(tmp_path / "run_config.json"))
    path = tmp_path / "run_config.json"
    path.write_text(json.dumps(_stale_payload()), encoding="utf-8")

    load_run_config()
    healed = json.loads(path.read_text(encoding="utf-8"))
    assert "end_vision_check" not in healed["guards"]
    assert healed["guards"]["agreement_accept"] == RunConfig().guards.agreement_accept


def test_broken_config_file_falls_back_to_defaults(tmp_path, monkeypatch) -> None:
    """坏 JSON / 顶层不是对象 / 取值非法 —— 一律回落默认值，**绝不抛异常**。"""
    monkeypatch.setenv("AUTOLEARN_RUN_CONFIG", str(tmp_path / "run_config.json"))
    path = tmp_path / "run_config.json"

    path.write_text("{ 这不是 JSON", encoding="utf-8")
    assert load_run_config().task_sequence == [TaskType.QUIZ]

    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_run_config().task_sequence == [TaskType.QUIZ]

    # 取值非法（sample_n 低于下限）是**真错误**，同样不许冒到调用方
    path.write_text(json.dumps({"sample_n": 0}), encoding="utf-8")
    assert load_run_config().sample_n == RunConfig().sample_n


def test_real_stale_field_is_still_rejected_by_schema() -> None:
    """容忍发生在**读取处**；模型本身仍然是 ``extra="forbid"``，不放宽契约。"""
    with pytest.raises(ValidationError):
        RunConfig(guards={"end_vision_check": True})
