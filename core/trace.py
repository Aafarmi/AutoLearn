"""留痕与事件总线（M4-3）。

:class:`RunLogger` 负责 ``logs/<run_id>/<item_id>/`` 的落盘，实现归 P6/P7；
:class:`EventBus` 是 SSE 的事件源，P5 的 ``GET /api/events`` 直接消费它，
故在 P0 一并实现（纯内存、无外部依赖）。

留痕目录约定（P7 验收口径）::

    logs/<run_id>/<item_id>/before.png
                            after.png
                            perception.json
                            solve.json      ← 含模型原始响应全文
                            action.json
                            verify.json
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import shutil
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from core.events import is_known_event

__all__ = [
    "DEFAULT_LOG_ROOT",
    "MODEL_RAW_FILENAME",
    "EventBus",
    "EventStream",
    "RunLogger",
    "purge_run_cache",
    "run_cache_entries",
    "run_dir",
]

logger = logging.getLogger(__name__)

#: 留痕根目录
DEFAULT_LOG_ROOT = Path("logs")

#: 单连接事件积压上限；超出时丢弃最旧事件，避免慢消费者拖垮内存。
DEFAULT_QUEUE_SIZE = 1000

#: 模型原始响应全文的落盘文件名（P6）。``solve.json`` 存结构化明细，
#: 全文单独一份 —— 模型吐了半截 JSON 时，``solve.json`` 可能压根写不出来，
#: 而那半截响应正是排查要的东西。
MODEL_RAW_FILENAME = "solve_raw.txt"

#: 目录名安全白名单。``item_id`` 来自运行数据，理论上可能是 ``../../etc/passwd``。
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class RunLogger:
    """运行留痕。目录结构即契约，不得改名。

    目录布局（P7 验收口径）::

        logs/<run_id>/<item_id>/before.png
                               after.png
                               perception.json
                               solve.json      ← 结构化采样明细
                               solve_raw.txt   ← 模型原始响应全文
                               action.json
                               verify.json
        logs/<run_id>/events.jsonl   ← **run 级**事件流（不属于任何条目）

    ⚠️ ``logs/<run_id>/`` 下**既有目录也有文件**：条目一律是**子目录**，
    ``events.jsonl`` 是 run 级文件。任何"遍历条目"的代码都要按 ``is_dir()`` 筛。

    P6 落地说明
    -----------
    - **只写文件，不改状态**：留痕失败（磁盘满、路径被占）只记 warning，
      绝不把一次动作判成失败 —— 留痕是证据，不是流程控制。
    - ``save_screenshot`` / ``save_json`` 返回的引用串是**相对路径**（``logs/...``），
      直接塞得进 ``ActionResult.screenshot_ref`` / ``VerifyResult.screenshot_ref``，
      界面上点开就是那张图。
    - ``item_id`` / ``stage`` 会先过一遍白名单收敛（``_safe_name``）：
      它们是运行数据，不该有机会跳出 ``root``。
    """

    def __init__(self, run_id: str, root: Path = DEFAULT_LOG_ROOT) -> None:
        self.run_id = run_id
        self.root = Path(root)

    def item_dir(self, item_id: str) -> Path:
        """返回（并按需创建）该条目的留痕目录。"""
        directory = run_dir(self.root, self.run_id) / _safe_name(item_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def save_screenshot(self, item_id: str, stage: str, data: bytes) -> str:
        """落盘截图，返回可放进 ``screenshot_ref`` 的引用串。"""
        path = self.item_dir(item_id) / f"{_safe_name(stage)}.png"
        return self._write(path, data)

    def append_event(self, event: str, payload: dict[str, Any]) -> None:
        """按行追加一条事件到 ``<root>/<run_id>/events.jsonl``。

        这是「**这次运行到底发生了什么**」的第一手记录 —— 题号从几跳到几、
        推了几步、为什么停下，都能直接读出来，不必再从各条目的留痕反推。

        为什么需要它（2026-09-28 实测）：排查「滚过头」时，**推进动作完全没有留痕**
        （`_record_level_stat` 只统计不落盘，`action.json` 只记条目动作），
        只能从四份 `vision_read.json` 的题号（2 → 3 → **16** → 17）反推出
        「滚了 4.8 屏」，绕了一大圈。

        与同类的既有纪律一致：**落盘失败只记 warning**，绝不打断运行 ——
        观测是证据，不是流程控制。
        """
        directory = run_dir(self.root, self.run_id)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            line = json.dumps(
                {"event": event, "payload": payload}, ensure_ascii=False, default=str
            )
            with (directory / "events.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            logger.warning("事件流落盘失败 %s：%s", directory, exc)

    def save_json(self, item_id: str, kind: str, payload: BaseModel | dict) -> str:
        """落盘结构化留痕（``perception.json`` / ``solve.json`` / ``action.json`` …）。

        支持 pydantic 模型与普通 ``dict``；遇到序列化不了的对象走 ``default=str``，
        **宁可在留痕里留个字符串，也不要因为一个字段丢掉整份证据**。
        """
        path = self.item_dir(item_id) / f"{_safe_name(kind)}.json"
        if isinstance(payload, BaseModel):
            body = payload.model_dump_json(indent=2)
        else:
            body = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        return self._write(path, body)

    def save_model_raw(self, item_id: str, raw: str) -> str:
        """模型原始响应全文必须落盘（``solve.json`` 之外单留一份明文）。"""
        path = self.item_dir(item_id) / MODEL_RAW_FILENAME
        return self._write(path, raw)

    # ------------------------------------------------------------------ 内部

    def _write(self, path: Path, body: str | bytes) -> str:
        """写文件并返回引用串；失败只记 warning，**不打断流程**。"""
        try:
            if isinstance(body, bytes):
                path.write_bytes(body)
            else:
                path.write_text(body, encoding="utf-8")
        except OSError as exc:
            logger.warning("留痕写入失败 %s：%s", path, exc)
        return path.as_posix()


def _safe_name(name: str) -> str:
    """把一段名字收敛成安全的单层目录 / 文件名词干。

    ``qid`` / ``vid`` 本来就是 ``sha1[:16]``，这里过一遍只为挡住
    「有人把 ``../../x`` 当 ``item_id`` 传进来」这种路径穿越。
    """
    cleaned = _UNSAFE_NAME.sub("_", str(name)).strip("._")
    return cleaned or "unknown"


# --------------------------------------------------------------------------- #
# 任务缓存：**每个任务一个专属目录**
# --------------------------------------------------------------------------- #
def run_dir(root: Path | str, run_id: str) -> Path:
    """一个运行的**专属缓存目录**：``<root>/<run_id>/``。

    「每个任务的缓存是否专门存储」这个问题，答案的**唯一定义点就是这里**：

        <root>/<run_id>/<item_id>/{before.png, after.png, after_submit.png,
                                  perception.json, vision_read.json, solve.json,
                                  solve_raw.txt, action.json, verify.json}
        <root>/<run_id>/events.jsonl      ← 该运行的操作日志（按行追加）

    边界要说清（免得被理解成「磁盘上只有它」）：

    * **在这里的**：截图、逐题留痕、模型原始回复、该运行的事件流 —— 删任务时一并清掉；
    * **不在这里、也删不掉的**：``state/autolearn.db`` 里按 ``qid`` **全局**存的 ``answer``
      （同一道题跨运行共享）、``state/browser_profile``（浏览器登录态）、
      ``media_position``（课程级进度，设计上跨运行继承）。
      它们不是「某一次任务的缓存」，删掉会伤到别的任务。
    """
    return Path(root) / _safe_name(run_id)


def run_cache_entries(root: Path | str, run_id: str) -> list[str]:
    """该运行缓存目录下的条目名（子目录 + 文件）。

    ⚠️ ``logs/<run_id>/`` 下**既有目录也有文件**（条目是子目录，``events.jsonl`` 是文件），
    所以调用方若想「逐条遍历」，必须自己按 ``is_dir()`` 筛。
    """
    directory = run_dir(root, run_id)
    if not directory.is_dir():
        return []
    try:
        return sorted(entry.name for entry in directory.iterdir())
    except OSError as exc:  # pragma: no cover - 权限 / 磁盘故障
        logger.warning("读取运行缓存目录失败 %s：%s", directory, exc)
        return []


def purge_run_cache(root: Path | str, run_id: str) -> tuple[bool, int]:
    """**彻底删除**一个运行的全部缓存。返回 ``(目录是否删掉, 删掉的文件数)``。

    删除范围严格限定在 ``<root>/<run_id>/`` 这一层之内：

    * 图片（``before.png`` / ``after.png`` / ``after_submit.png`` / ``*__vision.png``）
    * 逐题留痕（``perception.json`` / ``vision_read.json`` / ``solve.json`` / ``solution_raw`` …）
    * **操作日志**（run 级 ``events.jsonl``）与任何模型原始回复全文

    三条纪律（与全项目的删除口径一致）：

    1. **只删这一个 run 的目录** —— 不是 ``root`` 全清，也不递归到 ``root`` 之上；
    2. 删之前再校验一次路径确实在 ``root`` 之下（``_safe_name`` 之外的兜底）；
    3. 删失败**只记 warning 并如实返回 False**，绝不当作「已删除」上报。
    """
    directory = run_dir(root, run_id)
    try:
        directory.resolve().relative_to(Path(root).resolve())
    except (ValueError, OSError) as exc:
        logger.warning("拒绝删除 root 之外的路径 %s：%s", directory, exc)
        return False, 0
    if not directory.exists():
        return False, 0
    try:
        files = sum(1 for path in directory.rglob("*") if path.is_file())
    except OSError as exc:  # pragma: no cover - 目录正在被写
        logger.warning("统计运行缓存文件数失败 %s：%s", directory, exc)
        files = 0
    try:
        shutil.rmtree(directory)
    except OSError as exc:
        logger.warning("删除运行缓存失败 %s：%s", directory, exc)
        return False, 0
    return True, files


class EventStream(AsyncIterator[tuple[str, dict[str, Any]]]):
    """单个订阅者的事件流。

    刻意做成显式对象而不是裸异步生成器：生成器如果**在启动前**就被
    ``aclose()``，其 ``finally`` 不会执行，订阅者会永久泄漏在
    :class:`EventBus` 里。SSE 路由在客户端秒断时正好会走到这条路径。
    """

    def __init__(self, bus: EventBus, queue: asyncio.Queue[tuple[str, dict[str, Any]]]) -> None:
        self._bus = bus
        self._queue = queue
        self._closed = False

    def __aiter__(self) -> EventStream:
        return self

    async def __anext__(self) -> tuple[str, dict[str, Any]]:
        if self._closed:
            raise StopAsyncIteration
        return await self._queue.get()

    async def aclose(self) -> None:
        """注销订阅。可重复调用。"""
        if not self._closed:
            self._closed = True
            self._bus._detach(self._queue)

    @property
    def closed(self) -> bool:
        return self._closed


class EventBus:
    """进程内发布订阅，``GET /api/events`` 的事件源。

    ``emit()`` 是**同步**接口（契约如此），供编排与执行层随处调用；
    ``subscribe()`` 返回 :class:`EventStream`，每个订阅者独立队列，互不阻塞。

    **线程安全**：``emit()`` 可能在事件循环线程之外的线程被调用（例如一个
    后台日志线程、或测试线程）。订阅时记录事件循环与其线程，若 ``emit()``
    来自别的线程，就把投递动作 ``call_soon_threadsafe`` 切回循环线程执行 ——
    ``asyncio.Queue`` 的 ``put_nowait`` 跨线程调用会唤醒另一条循环上的等待者，
    属于未定义行为。
    """

    def __init__(self, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        self._queue_size = queue_size
        self._subscribers: set[asyncio.Queue[tuple[str, dict[str, Any]]]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread_id: int | None = None

    # -- 发布 -------------------------------------------------------------- #
    def emit(self, event: str, payload: dict[str, Any]) -> None:
        """向所有订阅者投递一个事件。事件名不在白名单时记警告但不抛错。"""
        if not is_known_event(event):
            logger.warning("emit 未知事件名 %r，请同步 core/events.py 与接口变更记录", event)
        loop = self._loop
        for queue in list(self._subscribers):
            message = (event, payload)
            if (
                loop is not None
                and loop.is_running()
                and threading.get_ident() != self._loop_thread_id
            ):
                loop.call_soon_threadsafe(self._offer, queue, message)
            else:
                self._offer(queue, message)

    def _offer(
        self,
        queue: asyncio.Queue[tuple[str, dict[str, Any]]],
        message: tuple[str, dict[str, Any]],
    ) -> None:
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            # 慢消费者：丢最旧的一条，保新事件能进来。
            with contextlib.suppress(asyncio.QueueEmpty):  # 竞态兜底
                queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(message)

    # -- 订阅 -------------------------------------------------------------- #
    def subscribe(self) -> EventStream:
        """订阅事件流。返回后应立即开始消费；用完调 ``aclose()``。

        首次订阅时记录事件循环，此后跨线程的 ``emit`` 会切回该循环投递。
        """
        queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
                self._loop_thread_id = threading.get_ident()
            except RuntimeError:  # pragma: no cover - 正常都在循环内订阅
                pass
        return EventStream(self, queue)

    def _detach(self, queue: asyncio.Queue[tuple[str, dict[str, Any]]]) -> None:
        self._subscribers.discard(queue)

    # -- 观测 -------------------------------------------------------------- #
    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
