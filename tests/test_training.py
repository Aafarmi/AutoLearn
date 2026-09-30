"""训练模式（``solve/training.py`` + ``/api/training``）的验收。

它做三件事：把一次成功任务的留痕**压成摘要**、让模型**总结成经验**、
**写进三份 md 的经验区**（只动带自动标记的区块）。这里钉住的是：

1. 摘要**有界**（不会把几十 MB 截图喂给模型）；
2. 经验区读写**幂等**、且**不碰区块外的内容**（人工写的一个字不动）；
3. 只有**跑成功**的任务才允许训练（失败记录里混着走不通的路径）。
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from core.trace import run_dir
from solve.providers.base import LLMProvider, LLMRequest, LLMResponse
from solve.training import (
    TARGETS,
    apply_experience,
    build_digest,
    parse_training_payload,
    read_experience,
    train_run,
)


# --------------------------------------------------------------------------- #
# 摘要
# --------------------------------------------------------------------------- #
def test_build_digest_is_bounded_and_reads_events(tmp_path: Path) -> None:
    directory = run_dir(tmp_path, "run-x")
    directory.mkdir(parents=True)
    (directory / "events.jsonl").write_text(
        "\n".join(
            json.dumps({"event": "advance.calibrated", "payload": {"method": "scroll"}},
                       ensure_ascii=False)
            for _ in range(200)  # 超过 _MAX_EVENTS，只取尾部
        )
        + "\n",
        encoding="utf-8",
    )

    digest = build_digest(tmp_path, "run-x")

    assert "run-x" in digest
    assert "advance.calibrated" in digest
    assert len(digest) <= 7000, "摘要必须有界 —— 这是训练花多少钱的上限"


def test_build_digest_reports_missing_parts(tmp_path: Path) -> None:
    digest = build_digest(tmp_path, "run-empty")
    assert "run-empty" in digest
    assert "摘要缺口" in digest, "没有留痕时也要如实说明缺了什么，而不是编"


# --------------------------------------------------------------------------- #
# 经验区读写
# --------------------------------------------------------------------------- #
def test_apply_and_read_experience_are_idempotent_and_preserve_the_rest(tmp_path: Path) -> None:
    (tmp_path / TARGETS["advance"][0]).write_text(
        "# 推进方式库\n\n人工写的正文\n\n<!-- AUTOTRAIN:BEGIN -->\n旧经验\n<!-- AUTOTRAIN:END -->\n\n尾部\n",
        encoding="utf-8",
    )

    apply_experience(tmp_path, "advance", ["新经验一", "新经验二"])
    text = (tmp_path / TARGETS["advance"][0]).read_text(encoding="utf-8")

    assert "人工写的正文" in text, "区块外一个字不动"
    assert "新经验一" in text and "新经验二" in text
    assert "旧经验" not in text, "整段替换，不叠罗汉"
    assert "尾部" in text
    assert read_experience(tmp_path, "advance").count("新经验") == 2

    # 再写一次（空列表）也不该清掉已有经验
    apply_experience(tmp_path, "advance", [])
    assert read_experience(tmp_path, "advance") != "", "空列表不该把已有经验抹掉"


def test_apply_experience_appends_when_no_band(tmp_path: Path) -> None:
    (tmp_path / TARGETS["solver"][0]).write_text("# 解题组\n\n正文\n", encoding="utf-8")
    apply_experience(tmp_path, "solver", ["经验"])
    text = (tmp_path / TARGETS["solver"][0]).read_text(encoding="utf-8")
    assert "正文" in text
    assert "经验" in text
    assert "AUTOTRAIN:END" in text


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def test_parse_training_payload_accepts_and_rejects() -> None:
    good = parse_training_payload('{"vision":["经验"],"solver":[],"advance":[],"skipped":null}')
    assert good is not None and good["vision"] == ["经验"]
    assert parse_training_payload("这不是 JSON") is None
    assert parse_training_payload('{"other": 1}') is None, "没有目标字段 = 答非所问"


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
class _ScriptedProvider(LLMProvider):
    name = "openai_compat"

    def __init__(self, payload: dict | None, *, fail: bool = False) -> None:
        self.payload = payload
        self.fail = fail

    def model_for(self) -> str:
        return "fake-model"

    async def complete(self, req: LLMRequest) -> LLMResponse:
        if self.fail:
            from solve.providers.base import ProviderError

            raise ProviderError("boom", code="provider_error")
        return LLMResponse(
            text=json.dumps(self.payload or {}, ensure_ascii=False),
            raw=json.dumps(self.payload or {}, ensure_ascii=False),
        )

    async def aclose(self) -> None:
        return


async def test_train_run_writes_into_all_three_targets(tmp_path: Path) -> None:
    directory = run_dir(tmp_path, "run-train")
    directory.mkdir(parents=True)
    (directory / "events.jsonl").write_text(
        json.dumps({"event": "run.finished", "payload": {}}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    for name in TARGETS.values():
        (tmp_path / name[0]).write_text(f"# {name[1]}\n\n正文\n", encoding="utf-8")

    provider = _ScriptedProvider(
        {"vision": ["经验 V"], "solver": ["经验 S"], "advance": ["经验 A"], "skipped": None}
    )
    result = await train_run(
        "run-train", log_root=tmp_path, prompt_dir=tmp_path, providers=[provider]
    )

    assert result.ok is True
    assert result.written == {"vision": 1, "solver": 1, "advance": 1}
    assert len(result.files) == 3


async def test_train_run_without_provider_reports_error(tmp_path: Path) -> None:
    result = await train_run("run-x", log_root=tmp_path, prompt_dir=tmp_path, providers=[])
    assert result.ok is False
    assert result.error == "provider_unavailable"


async def test_train_run_survives_provider_failure(tmp_path: Path) -> None:
    result = await train_run(
        "run-x",
        log_root=tmp_path,
        prompt_dir=tmp_path,
        providers=[_ScriptedProvider(None, fail=True)],
    )
    assert result.ok is False
    assert result.error == "provider_error", "失败如实上报，绝不假装写成功"


# --------------------------------------------------------------------------- #
# API 闸门
# --------------------------------------------------------------------------- #
def test_training_list_is_read_only(client: TestClient) -> None:
    payload = client.get("/api/training").json()
    assert set(payload["targets"][0].keys()) >= {"key", "label", "file", "section", "experience"}
    assert {t["key"] for t in payload["targets"]} == set(TARGETS)


def test_training_rejects_a_not_finished_task(client: TestClient) -> None:
    created = client.post("/api/tasks", json={"name": "还没跑"}).json()
    response = client.post(f"/api/training/{created['run_id']}")
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "task_not_trainable"


def test_training_unknown_task_is_404(client: TestClient) -> None:
    assert client.post("/api/training/does-not-exist").status_code == 404
