"""训练模式（2026-09-29 加）。

它做什么
--------
把**一次已经成功跑完的任务**的运行记录，压缩成几条**可复用的判读经验**，
写进对应的 Markdown（视觉组 / 解题组 / 下一题方式库）的「经验库」区。

    运行记录（events.jsonl + 逐题留痕） ──摘要──▶ 模型 ──JSON──▶ 三个 md 的经验区

为什么不直接把原始留痕喂给模型
------------------------------
一次 40 题的任务，逐题留痕有几十 MB（含截图与完整采样明细）。直接喂既贵又没用：
模型需要的不是「第 7 题的原始回复」，而是「这次哪些环节出过问题、怎么解决的」。
所以 :func:`build_digest` 先把记录压成一份**有界的**文本，再交给模型。

为什么只对**成功**的任务训练
----------------------------
失败的任务里，「经验」很可能是「这条路走不通」——那会把提示词越改越保守。
成功的任务才包含「这样做成了」的正面信号。调用方负责这个判断
（``ui/routes/training.py`` 会先检查运行状态）。

写盘纪律
--------
* 只改**被明确登记**的那三份 md（见 :data:`TARGETS`），不碰别的文件；
* 只改 ``<!-- AUTOTRAIN:BEGIN -->`` 与 ``<!-- AUTOTRAIN:END -->`` 之间的区块，
  **人工写的内容一个字都不动**；区块不存在时追加到文件末尾；
* 写盘失败（只读介质 / 权限）如实返回错误，**绝不假装写成功**。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from solve.providers.base import LLMProvider, LLMRequest, ProviderError

logger = logging.getLogger(__name__)

__all__ = [
    "BAND_END",
    "BAND_START",
    "TARGETS",
    "TrainingResult",
    "apply_experience",
    "build_digest",
    "parse_training_payload",
    "read_experience",
    "train_run",
]

#: 经验区在 md 里的两条界标。**必须成对出现**，训练模式只动它们之间的内容。
BAND_START = "<!-- AUTOTRAIN:BEGIN"
BAND_END = "<!-- AUTOTRAIN:END -->"

#: 训练目标：``key → (md 文件名, 人话名字, 经验区标题)``。
TARGETS: dict[str, tuple[str, str, str]] = {
    "vision": ("10-视觉组.md", "视觉组", "视觉组经验"),
    "solver": ("20-解题组.md", "解题组", "解题组经验"),
    "advance": ("33-推进方式库.md", "下一题方式库", "推进方式经验"),
}

#: 摘要文本的字符上限。有界是硬要求：它决定训练一次花多少钱。
_MAX_DIGEST_CHARS = 6000
#: 事件流最多取多少条（取**尾部** —— 收尾与推进异常都在后面）。
_MAX_EVENTS = 60
#: 逐题留痕最多看几题。
_MAX_ITEMS = 25
#: 经验条目上限（与 ``prompts/34-训练总结.md`` 的约定一致）。
_MAX_NOTES_PER_TARGET = 3
_MAX_NOTE_CHARS = 60


@dataclass
class TrainingResult:
    """一次训练的结果。**如实回报**：没做成的原因必须能看见。"""

    run_id: str
    ok: bool
    #: 各目标实际写入的条数：``{target: n}``
    written: dict[str, int] = field(default_factory=dict)
    #: 写入的 md 文件（绝对路径的字符串形式）
    files: list[str] = field(default_factory=list)
    #: 模型原文 / 解析结果留痕（排障用）
    raw: str = ""
    #: 没做成时的说明（没有 provider / 记录不足 / 解析失败 / 写盘失败）
    error: str | None = None
    #: 模型自己说「记录不足以总结」时的说明
    skipped: str | None = None


# --------------------------------------------------------------------------- #
# 记录 → 摘要
# --------------------------------------------------------------------------- #
def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def build_digest(log_root: Path | str, run_id: str) -> str:
    """把一次运行的留痕压成一份**有界的**文本摘要。

    取三样东西（都只看该 run 自己的目录）：

    1. ``events.jsonl`` 的**尾部若干条** —— 推进方式、标定结果、收尾确认、暂停原因；
    2. 每个条目的 ``vision_read.json`` / ``solve.json`` —— 读题质量字段与作答路径；
    3. 目录清单 —— 大致知道做了几题。

    **不含截图字节**，只含文字。取不到的部分静默跳过（摘要少一点没关系），
    但会在末尾如实写明「缺了哪一段」。
    """
    from core.trace import run_dir

    root = Path(log_root)
    directory = run_dir(root, run_id)
    parts: list[str] = [f"# 运行 {run_id} 的记录摘要"]
    missing: list[str] = []

    events_path = directory / "events.jsonl"
    if events_path.is_file():
        lines: list[str] = []
        try:
            for line in events_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                name = str(payload.get("event") or "")
                if name.startswith(("log.", "advance.", "solve.", "run.", "task.", "training.")):
                    body = json.dumps(payload.get("payload") or {}, ensure_ascii=False)
                    lines.append(f"{name} {body[:220]}")
        except OSError as exc:  # pragma: no cover - 权限 / 磁盘
            missing.append(f"events.jsonl（{exc}）")
        else:
            tail = lines[-_MAX_EVENTS:]
            parts.append(f"## 事件流（尾部 {len(tail)} 条）")
            parts.extend(tail or ["（这次运行没有留下事件）"])
    else:
        missing.append("events.jsonl")

    # 逐题留痕：**只读目录里的三大件**，不碰截图
    item_dirs = []
    if directory.is_dir():
        try:
            item_dirs = sorted(p for p in directory.iterdir() if p.is_dir())
        except OSError:  # pragma: no cover
            item_dirs = []
    if not item_dirs:
        missing.append("逐题留痕目录")
    else:
        parts.append(f"## 逐题留痕（{len(item_dirs)} 个条目，取前 {_MAX_ITEMS} 个）")
        for item in item_dirs[:_MAX_ITEMS]:
            read = _read_json(item / "vision_read.json") or {}
            solved = _read_json(item / "solve.json") or {}
            question = (read.get("read") or {})
            row: dict[str, Any] = {
                "item": item.name[-12:],
                "num_text": question.get("num_text"),
                "qtype": question.get("qtype"),
                "options": len(question.get("options") or []),
                "clipped": question.get("clipped") or [],
                "uncertain": question.get("uncertain") or [],
                "more_below": question.get("more_below"),
                "submit_scope": question.get("submit_scope"),
                "chosen_labels": solved.get("chosen_labels"),
                "confidence": solved.get("confidence"),
                "solve_path": solved.get("solve_path"),
                "review_flag": solved.get("review_flag"),
            }
            parts.append(json.dumps(row, ensure_ascii=False))

    if missing:
        parts.append("## 摘要缺口（如实标注，不是错误）")
        parts.append("；".join(missing))

    text = "\n".join(parts)
    if len(text) > _MAX_DIGEST_CHARS:
        text = text[:_MAX_DIGEST_CHARS] + "\n…（摘要已截断）"
    return text


# --------------------------------------------------------------------------- #
# 模型回复 → 经验
# --------------------------------------------------------------------------- #
def parse_training_payload(text: str) -> dict[str, Any] | None:
    """解析训练总结的回复。**认不出来返回 ``None``**（由调用方如实报失败）。"""
    from solve.reader import _extract_json  # 同一套「从散文里抠 JSON」的做法

    payload = _extract_json(text)
    if payload is None:
        return None
    if not any(key in payload for key in (*TARGETS, "skipped")):
        return None
    return payload


def _clean_notes(raw: Any) -> list[str]:
    """把模型给的经验条目规整成 ``list[str]``：去空、限量、限长、去重。"""
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, list):
        items = [str(entry) for entry in raw]
    else:
        return []
    notes: list[str] = []
    for item in items:
        note = " ".join(item.split()).strip(" -·。")
        if not note:
            continue
        if len(note) > _MAX_NOTE_CHARS:
            note = note[:_MAX_NOTE_CHARS].rstrip() + "…"
        if note not in notes:
            notes.append(note)
        if len(notes) >= _MAX_NOTES_PER_TARGET:
            break
    return notes


# --------------------------------------------------------------------------- #
# 经验区读写
# --------------------------------------------------------------------------- #
_BAND_RE = re.compile(
    re.escape(BAND_START) + r".*?" + re.escape(BAND_END),
    re.DOTALL,
)


def read_experience(prompt_dir: Path | str, target: str) -> str:
    """读某个目标当前的经验区正文（不含界标）。没有区块就返回空串。"""
    name = TARGETS[target][0]
    path = Path(prompt_dir) / name
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = _BAND_RE.search(text)
    if match is None:
        return ""
    body = match.group(0)
    body = body[len(BAND_START) :]
    body = body.split("-->", 1)[1] if "-->" in body else body
    return body.replace(BAND_END, "").strip()


def apply_experience(prompt_dir: Path | str, target: str, notes: list[str]) -> Path:
    """把经验写进目标 md 的经验区。返回被改写的文件路径。

    三条纪律：

    1. 有区块 → **只替换区块内部**，区块外一个字不动；
    2. 没有区块 → 追加到文件末尾；
    3. 空 ``notes`` → **保留原区块**（不把已有经验清空）。
    """
    name, label, title = TARGETS[target]
    path = Path(prompt_dir) / name
    text = path.read_text(encoding="utf-8") if path.exists() else f"# {label}\n"

    if notes:
        body_lines = ["\n<!-- AUTOTRAIN:BEGIN 训练模式自动写入，请勿手改 -->", f"## {title}"]
        body_lines.append("")
        body_lines.extend(f"- {note}" for note in notes)
        body_lines.append("<!-- AUTOTRAIN:END -->")
        block = "\n".join(body_lines)
    else:
        block = (
            "\n<!-- AUTOTRAIN:BEGIN 训练模式自动写入，请勿手改 -->\n"
            f"## {title}\n\n_（暂无经验条目。）_\n<!-- AUTOTRAIN:END -->\n"
        )

    if _BAND_RE.search(text):
        new_text = _BAND_RE.sub(block.rstrip("\n"), text)
    else:
        new_text = text.rstrip("\n") + "\n" + block

    path.write_text(new_text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
async def train_run(
    run_id: str,
    *,
    log_root: Path | str,
    prompt_dir: Path | str,
    providers: list[LLMProvider],
    max_tokens: int = 1024,
) -> TrainingResult:
    """跑一次训练：总结这次运行 → 写进三份 md 的经验区。

    **绝不抛异常**：它是一步「锦上添花」的后处理，炸掉不该影响任务本身的状态。
    所有失败路径都落成 ``TrainingResult.error``，调用方如实展示。
    """
    if not providers:
        return TrainingResult(run_id=run_id, ok=False, error="provider_unavailable")

    digest = build_digest(log_root, run_id)
    from solve.prompt_files import training_system_prompt

    try:
        system = training_system_prompt()
    except Exception as exc:  # 提示词文件缺失 —— 大声失败，但不当成运行故障
        return TrainingResult(run_id=run_id, ok=False, error=f"prompt_missing: {exc}")

    last_error = "provider_unavailable"
    for provider in providers:
        request = LLMRequest(
            model=provider.model_for() or "",
            system=system,
            user=digest,
            temperature=0.2,
            max_tokens=max_tokens,
            force_json=True,
        )
        try:
            response = await provider.complete(request)
        except ProviderError as exc:
            last_error = exc.code
            continue
        payload = parse_training_payload(response.text)
        if payload is None:
            last_error = "training_parse_failed"
            continue
        written: dict[str, int] = {}
        files: list[str] = []
        try:
            for target in TARGETS:
                notes = _clean_notes(payload.get(target))
                written[target] = len(notes)
                path = apply_experience(prompt_dir, target, notes)
                if notes:
                    files.append(str(path))
        except OSError as exc:
            return TrainingResult(
                run_id=run_id,
                ok=False,
                written=written,
                files=files,
                raw=(response.text or "")[:2000],
                error=f"write_failed: {exc}",
            )
        skipped = payload.get("skipped")
        return TrainingResult(
            run_id=run_id,
            ok=True,
            written=written,
            files=files,
            raw=(response.text or "")[:2000],
            skipped=str(skipped) if skipped else None,
        )
    return TrainingResult(run_id=run_id, ok=False, error=last_error)
