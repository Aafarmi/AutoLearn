"""P6 / M4-3（留痕部分）：``RunLogger`` 落盘验收。

目录结构即契约（``logs/<run_id>/<item_id>/{before.png, action.json, …}``），
P7 的批量留痕直接消费它，所以这里把「写哪儿、叫什么、返回什么引用串」钉死。

另外两条容易忽略但会咬人的：
    - 留痕失败**不能**把一次动作判成失败（磁盘满 / 路径被占）；
    - ``item_id`` 来自运行数据，不该有机会跳出 ``root``（路径穿越）。
"""

from __future__ import annotations

import json
from pathlib import Path

from core.enums import ActionKind, ActLevel
from core.models import ActionResult
from core.trace import DEFAULT_LOG_ROOT, MODEL_RAW_FILENAME, RunLogger


def test_item_dir_creates_and_is_idempotent(tmp_path: Path) -> None:
    logger = RunLogger("run-1", tmp_path)
    directory = logger.item_dir("abc123")
    assert directory == tmp_path / "run-1" / "abc123"
    assert directory.is_dir()
    assert logger.item_dir("abc123").is_dir(), "重复调用不得报错"


def test_save_screenshot_writes_and_returns_reference(tmp_path: Path) -> None:
    logger = RunLogger("run-1", tmp_path)
    ref = logger.save_screenshot("abc123", "before", b"\x89PNG\r\n\x1a\npayload")

    assert ref.endswith("run-1/abc123/before.png")
    assert "\\" not in ref, "引用串统一用 / 分隔，别把 Windows 反斜杠写进 JSON"
    assert (tmp_path / "run-1" / "abc123" / "before.png").read_bytes().endswith(b"payload")


def test_save_json_accepts_pydantic_models(tmp_path: Path) -> None:
    logger = RunLogger("run-1", tmp_path)
    payload = ActionResult(
        kind=ActionKind.SELECT_OPTION,
        target="[data-quiz='option']",
        level_used=ActLevel.L1_LOCATOR,
        ok=True,
    )
    ref = logger.save_json("abc123", "action", payload)

    assert ref.endswith("run-1/abc123/action.json")
    on_disk = json.loads((tmp_path / "run-1" / "abc123" / "action.json").read_text("utf-8"))
    assert on_disk["level_used"] == "l1_locator"
    assert on_disk["ok"] is True


def test_save_json_accepts_plain_dicts_and_survives_odd_values(tmp_path: Path) -> None:
    """遇到序列化不了的对象走 ``default=str`` —— 宁可留字符串，也别丢掉整份证据。"""
    logger = RunLogger("run-1", tmp_path)
    ref = logger.save_json("abc123", "perception", {"warnings": ["a"], "weird": object()})

    assert ref.endswith("perception.json")
    on_disk = json.loads((tmp_path / "run-1" / "abc123" / "perception.json").read_text("utf-8"))
    assert on_disk["warnings"] == ["a"]
    assert isinstance(on_disk["weird"], str)


def test_save_model_raw_keeps_broken_json_verbatim(tmp_path: Path) -> None:
    """模型吐了半截 JSON 时，``solve.json`` 可能压根写不出来 —— 全文必须另存一份。"""
    raw = '{"chosen_labels": ["A"], "thought": "被截断的响应…'
    ref = RunLogger("run-1", tmp_path).save_model_raw("abc123", raw)

    assert ref.endswith(f"run-1/abc123/{MODEL_RAW_FILENAME}")
    assert (tmp_path / "run-1" / "abc123" / MODEL_RAW_FILENAME).read_text("utf-8") == raw


def test_hostile_item_id_cannot_escape_root(tmp_path: Path) -> None:
    """路径穿越防守：``item_id`` 是运行数据，不能拿它去写 ``root`` 之外。"""
    logger = RunLogger("run-1", tmp_path)
    for hostile in ("../../evil", "..", "/etc/passwd", "a/b/c", ""):
        directory = logger.item_dir(hostile)
        assert directory.resolve().is_relative_to(tmp_path.resolve()), f"{hostile} 逃出了 root"
        assert directory.is_dir()


def test_write_failure_does_not_raise(tmp_path: Path, monkeypatch) -> None:
    """留痕是证据不是流程控制：写不进去只记 warning，绝不把动作判成失败。"""
    logger = RunLogger("run-1", tmp_path)

    def boom(self: Path, data: bytes) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_bytes", boom)
    ref = logger.save_screenshot("abc123", "error", b"x")  # 不抛即通过
    assert ref.endswith("error.png")


def test_default_root_is_the_contract_value() -> None:
    assert DEFAULT_LOG_ROOT.as_posix() == "logs"
    assert RunLogger("run-1").root.as_posix() == "logs"
