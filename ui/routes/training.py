"""训练模式路由（2026-09-29 加）。

两条接口，一读一写：

======================  ==========================================================
``GET  /api/training``  看「训练会写到哪几份文件、现在里面有什么经验」（**只读**）
``POST /api/training/{run_id}``  对**一次跑成功的任务**跑一遍训练总结
======================  ==========================================================

为什么要做成接口而不是「跑完自动偷偷改提示词」
------------------------------------------------
训练模式会**改动磁盘上的提示词文件**（视觉组 / 解题组 / 下一题方式库的经验区）。
那是一个有后果的动作，必须：

1. 由用户显式发起（或者由 ``RunConfig.training`` 明确开启）；
2. 能被看见（``GET`` 列出目标文件与当前经验）；
3. 只追加带自动标记的区块，**人工内容一个字不动**（见 ``solve/training.py``）。

「成功的任务才能训练」这条闸门也在这里 —— 失败任务里的「经验」往往是
「这条路走不通」，把它写进提示词只会让下一次更保守。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status

from core.events import Event
from solve.prompt_files import prompts_dir
from solve.training import TARGETS, read_experience, train_run
from ui.assembly import build_active_chain
from ui.deps import get_event_bus, get_registry, get_run_config, get_store
from ui.store import log_root

__all__ = ["router"]

router = APIRouter(prefix="/api/training", tags=["training"])

#: 可以跑训练的任务状态。**只有真正跑完的**才算「成功任务」。
#: ``stopped`` / ``error`` / ``paused`` 都不在其中 —— 它们的记录里混着
#: 「这次没走通的路径」，拿来当经验会把提示词越改越保守。
_TRAINABLE = frozenset({"finished"})


@router.get("")
async def list_training() -> dict[str, Any]:
    """训练目标一览（只读）：写到哪份文件、现在有哪些经验。

    ``files`` 是真在用的那几份 md 的**绝对路径** —— 用户改完提示词要重启服务
    才生效，这份路径就是「去哪儿改」的答案。
    """
    directory = prompts_dir()
    targets = []
    for key, (name, label, title) in TARGETS.items():
        targets.append(
            {
                "key": key,
                "label": label,
                "file": str(directory / name),
                "section": title,
                "experience": read_experience(directory, key),
            }
        )
    return {"prompts_dir": str(directory), "targets": targets}


@router.post("/{run_id}")
async def run_training(run_id: str) -> dict[str, Any]:
    """对一次**跑成功**的任务跑一遍训练总结。

    「成功」的判据是任务展示态为 ``finished`` —— 停止 / 出错 / 还没跑完都拒绝，
    并给出可读原因（而不是默默跑一遍、写进一堆由半途记录推出的「经验」）。
    """
    store = get_store()
    task = store.task(run_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "task_not_found", "message": f"任务 {run_id} 不存在"},
        )
    if task.status not in _TRAINABLE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "task_not_trainable",
                "message": (
                    f"任务当前是「{task.status_label}」，只有**跑成功**的任务才能训练："
                    "失败 / 中断的记录里混着走不通的路径，写进提示词会让下一次更保守"
                ),
            },
        )

    cfg = get_run_config()
    providers = build_active_chain(get_registry(), cfg)
    result = await train_run(
        run_id,
        log_root=log_root(),
        prompt_dir=prompts_dir(),
        providers=providers,
    )
    if result.ok:
        get_event_bus().emit(
            Event.TRAINING_DONE,
            {
                "run_id": run_id,
                "written": result.written,
                "files": result.files,
                "skipped": result.skipped,
            },
        )
    return {
        "ok": result.ok,
        "run_id": result.run_id,
        "written": result.written,
        "files": result.files,
        "error": result.error,
        "skipped": result.skipped,
        # 模型原文留痕：解析失败时它才是第一手证据
        "raw": result.raw,
    }
