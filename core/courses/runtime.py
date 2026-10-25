"""课程子进程运行时。

三部分：

1. `spawn()` / `iter_jsonl()`（T0-05 骨架）——所有后续模块拉起子进程、读 JSONL
   的唯一入口，签名保持向后兼容，谁都不许另写一份 subprocess 参数。
2. `CourseRuntime`（M7/T3-01/02/06/07）——起停、环境变量、终止、关机清理。
3. 双通道事件泵（M8/T3-03/04）——stdout 走协议、stderr 走脱敏日志，两个 daemon
   读线程只入有界队列，唯一的消费者负责内存态、`job.json` 与事件落盘。

边界：本模块做"事件进来到落盘"这一段，**不做终局裁定**（M9：completed/partial/
failed 的判定与槽释放顺序在 `finalize`）。所以 `_watch` 只排空、不写状态。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .domain import CourseJob, CoursePhase, CourseStatus, ensure_transition
from .errors import CourseAuthRequired, CourseBusy, CourseError
from .protocol import (
    MAX_LINE_BYTES,
    PROGRESS_MIN_INTERVAL_MS,
    is_frontend_event,
    validate_event,
)
from .store import CourseJobStore

logger = logging.getLogger(__name__)

#: Windows 下不弹控制台窗口；其它平台没有这个常量。
CREATE_NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

#: terminate 后等这么久再 kill（§4.6：先礼后兵，5s 足够上游收尾）。
STOP_GRACE_SECONDS = 5.0
#: kill 之后最多再等这么久，仍未退出即判 `process_kill_failed`。
KILL_GRACE_SECONDS = 2.0
PROCESS_TIMEOUT_BUFFER_SECONDS = 10 * 60

#: 桥接脚本默认位置（M10 落地）。不存在时 `start()` 直接 500，不会拉起空进程。
DEFAULT_BRIDGE = Path(__file__).resolve().parent / "providers" / "zhihuishu" / "bridge.py"

# --------------------------------------------------------------- 事件泵常量（M8）

#: 事件泵队列上限（行）。读线程只投递、不落盘；队列满时读线程阻塞等待，
#: 形成对子进程的反压——宁可让它写慢一点，也不丢事件。
PUMP_QUEUE_SIZE = 4096

#: 进程退出后等消费者排空的上限（秒）。超时只记日志，随后强制停泵，
#: 免得关机流程被一个卡住的落盘拖死。
PUMP_DRAIN_SECONDS = 10.0

#: 读线程投递 EOF 哨兵的上限（秒）；只在消费者已死/关机时才会走到。
ENQUEUE_GRACE_SECONDS = 5.0

#: 状态落盘节拍。**复用** §4.3 的进度节流口径（500ms），不另造常量：
#: 每个事件都 fsync 一次 job.json，10 万行洪水下会把 1.6s 轮询和磁盘打满。
FLUSH_INTERVAL_SECONDS = PROGRESS_MIN_INTERVAL_MS / 1000.0

#: 这几种事件一到就立即落盘（数量稀少且语义关键：阶段切换、终局 result）。
FLUSH_NOW_TYPES = frozenset({"hello", "phase", "current", "result"})

#: stderr 脱敏日志（§4.3：stderr 一行都不许进前端事件）。
DIAGNOSTICS_FILE = "diagnostics.log"
DIAGNOSTICS_MAX_BYTES = 2 * 1024 * 1024
DIAGNOSTICS_FLUSH_BYTES = 64 * 1024
LOG_TRUNCATED_MARKER = "log truncated"

#: 脱敏占位。文案固定，方便安全测试直接断言。
REDACTED = "<redacted>"

_PHASE_VALUES: frozenset[str] = frozenset(phase.value for phase in CoursePhase)



def spawn(
    argv: Sequence[str],
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    creation_flags: int = 0,
) -> subprocess.Popen[str]:
    """启动桥接子进程：三条管道全部接通，文本模式、UTF-8 解码。

    - `env=None` 时继承当前进程环境；传入则**整体替换**（不是合并），
      需要合并的调用方自己 `{**os.environ, **extra}`。
    - 解码用 `errors="replace"`：非法 UTF-8 字节变成 U+FFFD，
      随后由 `protocol.validate_event()` 判为违约丢弃（§4.3「非法 UTF-8 丢弃」），
      这样一行坏字节不会打死整条读循环。
    - `creation_flags` 是**追加**的关键字参数（默认 0，老调用方行为不变），
      给 M7 传 `CREATE_NO_WINDOW` 用：WebUI 以无控制台方式启动时不弹黑窗。
    """
    return subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=dict(env) if env is not None else None,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creation_flags,
    )


def iter_jsonl(
    stream: Iterable[str],
    *,
    on_invalid: Callable[[int], None] | None = None,
) -> Iterator[dict[str, Any]]:
    """逐行解析 JSONL，只产出 JSON 对象。

    丢弃（不抛异常）：空行、非 JSON、JSON 但不是对象、超过 `MAX_LINE_BYTES`
    的行。这里不做字段校验，契约校验在 `protocol.validate_event()`。

    `on_invalid` 是 M8 加的**可选**回调：每丢弃一行就带着这一行的字节长度调
    一次，给事件泵累计 `protocol_errors` 用。默认 `None` 时行为与 M7 完全一致
    （静默丢弃）。回调里拿不到原文——长度足够定位"有脏行"，又不会把非法内容
    带进日志。空行不算脏行，不触发回调。
    """
    for raw in stream:
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        try:
            size = len(line.encode("utf-8"))
        except UnicodeEncodeError:
            # 行内有孤立代理项：长度按字符数报，仍然只记数不记内容。
            _notify_invalid(on_invalid, len(line))
            continue
        if size > MAX_LINE_BYTES:
            _notify_invalid(on_invalid, size)
            continue
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            _notify_invalid(on_invalid, size)
            continue
        if isinstance(value, dict):
            yield value
        else:
            _notify_invalid(on_invalid, size)


def _notify_invalid(on_invalid: Callable[[int], None] | None, size: int) -> None:
    """回调抛错不能打死读循环：坏回调只是少一个计数，坏流才是真事故。"""
    if on_invalid is None:
        return
    try:
        on_invalid(size)
    except Exception:  # pragma: no cover - 只防调用方回调写错
        logger.debug("iter_jsonl 的 on_invalid 回调抛错", exc_info=True)


def _bump_protocol_errors(run: "CourseRun") -> None:
    """解析失败与字段违约统一计入 `protocol_errors`（只记数，不留原文）。

    两个线程会同时加（读线程记解析失败、消费者记字段违约），`+=` 在 CPython
    下不会丢更新；这里的用途是计数展示与测试断言，不需要强一致。
    """
    run.protocol_errors += 1


# --------------------------------------------------------------- stderr 脱敏（M8）

#: 顺序有意义：先擦"具名凭据"（能保留字段名，日志还可读），再擦裸长串，
#: 最后处理 URL 的 query。反过来做的话，CASLOGC 会先被当成裸长串擦掉。
_SANITIZERS: tuple[tuple[re.Pattern[str], str], ...] = (
    # 智慧树会话票据：`CASLOGC=xxx` / `'CASLOGC': 'xxx'` / `CASLOGC: xxx`
    (re.compile(r"(?i)\bCASLOGC['\"]?\s*[:=]\s*['\"]?[^\s;,}\"']*"), "CASLOGC=" + REDACTED),
    # 通用 Cookie 头：`Cookie: a=1; b=2` / `cookie=...` / `set-cookie: ...`
    (re.compile(r"(?i)\b(?:set-)?cookie['\"]?\s*[:=]\s*['\"]?[^\s;,}\"']*"), "Cookie=" + REDACTED),
    # 认证头：`Authorization: Bearer xxx`
    (
        re.compile(r"(?i)\bauthorization['\"]?\s*[:=]\s*['\"]?(?:bearer\s+)?[^\s,}\"']*"),
        "Authorization: " + REDACTED,
    ),
    # 裸 Bearer token（可能出现在 requests/urllib3 的调试输出里）
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-+/=]{8,}"), "Bearer " + REDACTED),
    # ≥32 位的 base64 / hex 串：cookie 值、token、签名、hash 通杀
    (re.compile(r"[A-Za-z0-9+/=_\-]{32,}"), REDACTED),
)


def _scrub_url(match: "re.Match[str]") -> str:
    """只留 scheme + host + path，query/fragment 一律替换。

    上游经常把 `?uuid=...&token=...` 拼在 print 里；query 里很少有诊断价值，
    而凭据经常就在那儿。
    """
    raw = match.group(0)
    cut = len(raw)
    for mark in ("?", "#"):
        index = raw.find(mark)
        if index != -1 and index < cut:
            cut = index
    if cut == len(raw):
        return raw
    return raw[:cut] + REDACTED


def sanitize_diagnostic(text: Any) -> str:
    """把一行 stderr / 诊断文案里的凭据擦干净（M8；M16 的错误收口直接复用）。

    覆盖：`CASLOGC`、`cookie=`、`Authorization`、`Bearer <token>`、
    ≥32 位 base64/hex 串、http(s) URL 的 query。只做替换，不改行数与缩进——
    diagnostics.log 要能对着行号看上游输出。
    """
    if text is None:
        return ""
    result = text if isinstance(text, str) else str(text)
    if not result:
        return ""
    for pattern, replacement in _SANITIZERS:
        result = pattern.sub(replacement, result)
    return re.sub(r"(?i)https?://[^\s\"'<>]+", _scrub_url, result)


# --------------------------------------------------------------- 运行实例（M7）


@dataclass
class CourseRun:
    """一次运行的进程侧状态（任务级进度仍在 `job.json` 里）。

    `threads` / `event_seq` / `counts` 是 M7 就预留的占位；M8 补齐了事件泵需要的
    内存态（phase/current_title/计数/percent/protocol_errors）与队列句柄。
    M9 的 `finalize` 直接读 `had_result` / `result_payload` 判终局，不用再解析事件。
    """

    job_id: str
    site_id: str
    proc: subprocess.Popen[str]
    run_id: str
    started_at: str
    threads: list[threading.Thread] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    event_seq: int = 0
    counts: dict[str, int] = field(default_factory=lambda: {"completed": 0, "failed": 0})
    returncode: int | None = None
    time_limit_minutes: int = 0
    process_timed_out: bool = False
    finalized_event: threading.Event = field(default_factory=threading.Event)
    finalize_lock: threading.Lock = field(default_factory=threading.Lock)

    # -- M8 事件泵（读线程只入队，消费者更新内存并落盘）-------------------
    pump_queue: "queue.Queue[tuple[str | None, Any]] | None" = None
    consumer: threading.Thread | None = None
    readers: list[threading.Thread] = field(default_factory=list)
    pump_stop: threading.Event = field(default_factory=threading.Event)
    protocol_errors: int = 0
    dropped_queue_items: int = 0
    had_result: bool = False
    result_payload: dict[str, Any] | None = None
    phase: str | None = None
    current_title: str | None = None
    percent: float = 0.0
    pending_changes: dict[str, Any] = field(default_factory=dict)
    pending_events: "deque[dict[str, Any]]" = field(default_factory=deque)
    pending_phases: list[str] = field(default_factory=list)
    last_flush_at: float = 0.0
    diagnostics_bytes: int = 0
    diagnostics_truncated: bool = False
    diagnostics_buffer: list[str] = field(default_factory=list)
    diagnostics_buffered_bytes: int = 0

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    @property
    def pid(self) -> int:
        return int(self.proc.pid)


#: 能进入 `running` 的起始状态：`ready` 是正常路径，四个终态是"重新开始学习"。
#: `draft`（没登录）、`authenticating`、`running`、`stopping`、`archived` 都不行。
STARTABLE: frozenset[CourseStatus] = frozenset(
    {
        CourseStatus.READY,
        CourseStatus.COMPLETED,
        CourseStatus.PARTIAL,
        CourseStatus.FAILED,
        CourseStatus.CANCELLED,
    }
)


class CourseRuntime:
    """课程子进程的生命周期（起、停、关机清理）。

    为什么是"每个实例一份注册表"而不是纯函数：`stop` 要找到那个 `Popen`
    才能发信号，而 `Popen` 只活在进程内存里。WebUI 用模块级 `shared()` 拿同一个
    实例，测试直接 new 一个传 `store` / `run_dir_factory` / `bridge`。

    全部阻塞动作（等进程退出、复制 vendor）都在调用方线程里做——`app.py` 用
    `asyncio.to_thread` 包，事件循环不许被 7 秒的停止流程卡住。
    """

    def __init__(
        self,
        store: CourseJobStore | None = None,
        *,
        bridge: str | os.PathLike[str] | None = None,
        run_dir_factory: Any = None,
    ) -> None:
        self._store = store if store is not None else CourseJobStore()
        self._bridge = Path(bridge) if bridge is not None else DEFAULT_BRIDGE
        self._run_dir_factory = run_dir_factory
        self._runs: dict[str, CourseRun] = {}
        self._lock = threading.RLock()

    # -- 查询 ---------------------------------------------------------------

    def run_of(self, job_id: str) -> CourseRun | None:
        with self._lock:
            return self._runs.get(job_id)

    def active_job_ids(self) -> list[str]:
        with self._lock:
            return [job_id for job_id, run in self._runs.items() if run.alive]

    # -- 启动 ---------------------------------------------------------------

    def start(
        self,
        job: CourseJob,
        params: Mapping[str, Any] | None = None,
        *,
        bridge: str | os.PathLike[str] | None = None,
    ) -> CourseRun:
        """拉起一次运行：`ready|终态 → running`，占槽，把请求写进 stdin。

        顺序是有讲究的（任何一步失败都不能留下"status=running 但没有进程"）：
        校验状态 → 准备运行目录 → 取 Cookie → spawn → 占槽 → 写状态与事件 →
        写 stdin → 挂旁观线程。其中只有 spawn/占槽/登记在锁内。
        """
        if not isinstance(job, CourseJob):
            raise TypeError("job must be a CourseJob")
        if self.run_of(job.job_id) is not None:
            # 先判"本进程里已经有活进程"，再判状态迁移：不然连点两次会得到
            # "running → running 不是合法迁移"，语义上答非所问。
            raise CourseBusy("该课程任务已经在运行，请先停止。")
        current = self._store.get_job(job.job_id)
        if current.status not in STARTABLE:
            # 复用 domain 的迁移表给出统一 409 文案，而不是自己写 if 链。
            ensure_transition(current.status, CourseStatus.RUNNING)

        run_dir = Path(self._prepare_run_dir(current.job_id))
        run_dir.mkdir(parents=True, exist_ok=True)
        bridge_path = Path(bridge) if bridge is not None else self._bridge
        if not bridge_path.is_file():
            raise CourseError("课程运行组件（bridge）尚未安装，无法开始学习。", 500)
        cookies = _site_cookies(current.site_id)
        if not cookies:
            raise CourseAuthRequired("还没有登录凭据，请先登录后再开始学习。")
        run_id = uuid.uuid4().hex
        request = self._request_payload(current, run_id, params, cookies)
        env = self._run_env(run_dir)
        argv = [sys.executable, "-X", "utf8", str(bridge_path)]

        with self._lock:
            if current.job_id in self._runs:
                raise CourseBusy("该课程任务已经在运行，请先停止。")
            proc = self._spawn(argv, run_dir, env)
            try:
                self._store.acquire_slot(current.site_id, current.job_id, proc.pid)
            except CourseError:
                # 站点已被别的任务占用：刚拉起的进程必须立刻收掉，否则成了孤儿。
                _terminate_process(proc, STOP_GRACE_SECONDS)
                raise
            run = CourseRun(
                job_id=current.job_id,
                site_id=current.site_id,
                proc=proc,
                run_id=run_id,
                started_at=_now(),
                # 起点就是落盘里的 phase（setup）：桥接第一条 phase 事件报 setup
                # 时不该被当成"阶段变化"，否则每次开课都多一条假事件。
                phase=current.phase.value,
                current_title=current.current_title,
                percent=float(current.progress_percent),
                counts={
                    "completed": current.completed_count,
                    "failed": current.failed_count,
                },
                time_limit_minutes=current.options.time_limit_minutes,
            )
            self._runs[current.job_id] = run

        try:
            # 读线程必须先于子进程的第一行输出起来：桥接一启动就会吐 hello，
            # 管道缓冲（64KB）写满后子进程会阻塞在 print 上。
            self._start_pumps(run)
            self._store.update(
                current,
                status=CourseStatus.RUNNING,
                phase=CoursePhase.SETUP,
                run_id=run_id,
            )
            self._store.append_business_event(
                current.job_id,
                "course_started",
                current.status,
                CourseStatus.RUNNING,
                operation=current.mode.value,
            )
            self._write_request(run, request)
        except Exception as error:
            self._abort(run, "start_failed")
            if isinstance(error, CourseError):
                raise
            raise CourseError("课程任务启动失败。", 500) from None

        watcher = threading.Thread(
            target=self._watch,
            args=(run,),
            name=f"course-watch-{current.job_id[:8]}",
            daemon=True,
        )
        run.threads.append(watcher)
        watcher.start()
        return run

    # -- 停止 ---------------------------------------------------------------

    def stop(
        self,
        job_id: str,
        *,
        reason: str = "user_stop",
        timeout_seconds: float = STOP_GRACE_SECONDS,
    ) -> bool:
        """停一次运行，返回"是否确认已退出"。

        `running → stopping`（写盘 + 事件）→ terminate → 等 5s → kill → 等 2s。
        确认退出：`stopping → cancelled` + 释放槽，返回 True；仍活着：
        `failed(process_kill_failed)`，返回 False（前端据此提示"进程异常"）。

        幂等：终态再调只返回 True，不写事件；`stopping` 重入直接续杀。
        """
        run = self.run_of(job_id)
        current = self._store.get_job(job_id)

        if current.status is CourseStatus.RUNNING:
            current = self._store.update(current, status=CourseStatus.STOPPING)
            self._store.append_business_event(
                job_id,
                "course_stopping",
                CourseStatus.RUNNING,
                CourseStatus.STOPPING,
                operation=reason,
            )
        elif current.status is not CourseStatus.STOPPING:
            # 终态/草稿：没有"运行中"可停。内存里若还挂着活进程，仍然收掉。
            if run is not None and run.alive:
                _terminate_process(run.proc, timeout_seconds)
                self._drop_run(job_id)
            return True

        if run is not None:
            # _watch 是唯一的终态裁定者；stop 只表达取消并等它完成排空与收尾。
            run.cancel_event.set()
            exited = not _terminate_process(run.proc, timeout_seconds)
            if exited:
                if run.finalized_event.wait(PUMP_DRAIN_SECONDS + KILL_GRACE_SECONDS + 1):
                    return self._store.get_job(job_id).status is CourseStatus.CANCELLED
                logger.error("课程任务 %s 退出后未能完成终态收尾", job_id)
                return False

            self._store.update(
                self._store.get_job(job_id),
                status=CourseStatus.FAILED,
                run_id=None,
                last_safe_error_category="process_kill_failed",
            )
            self._store.append_business_event(
                job_id,
                "course_failed",
                CourseStatus.STOPPING,
                CourseStatus.FAILED,
                operation=reason,
                safe_error_category="process_kill_failed",
            )
            self._release_slot(current)
            self._drop_run(job_id)
            return False

        if run is None:
            # 服务重启后残留的 running（进程早没了）：直接收口，别让状态卡住。
            logger.warning("课程任务 %s 没有对应进程，直接置为 cancelled", job_id)
            exited = True
        else:
            exited = not _terminate_process(run.proc, timeout_seconds)

        if exited:
            self._store.update(
                self._store.get_job(job_id),
                status=CourseStatus.CANCELLED,
                run_id=None,
            )
            self._store.append_business_event(
                job_id,
                "course_cancelled",
                CourseStatus.STOPPING,
                CourseStatus.CANCELLED,
                operation=reason,
            )
            self._release_slot(current)
            self._drop_run(job_id)
            return True

        self._store.update(
            self._store.get_job(job_id),
            status=CourseStatus.FAILED,
            run_id=None,
            last_safe_error_category="process_kill_failed",
        )
        self._store.append_business_event(
            job_id,
            "course_failed",
            CourseStatus.STOPPING,
            CourseStatus.FAILED,
            operation=reason,
            safe_error_category="process_kill_failed",
        )
        self._release_slot(current)
        self._drop_run(job_id)
        return False

    def shutdown_all(self, timeout_seconds: float = 8.0) -> dict[str, int]:
        """服务退出钩子：逐个停止，终态 cancelled 并记 `service_shutdown`。

        单个任务失败不影响其它任务（也绝不让异常冒到 lifespan）；返回计数供
        日志与测试断言。单任务最长耗时 = 5s terminate + 2s kill = 7s < 8s。
        """
        job_ids = self.active_job_ids()
        stopped = 0
        failed = 0
        for job_id in job_ids:
            try:
                ok = self.stop(
                    job_id, reason="service_shutdown", timeout_seconds=timeout_seconds
                )
            except Exception:
                logger.exception("关闭课程任务失败：%s", job_id)
                failed += 1
                continue
            if not ok:
                failed += 1
                continue
            stopped += 1
            try:
                self._store.append_business_event(
                    job_id,
                    "service_shutdown",
                    None,
                    CourseStatus.CANCELLED,
                )
            except Exception:
                logger.warning("写 service_shutdown 事件失败：%s", job_id)
        return {"stopped": stopped, "failed": failed, "remaining": len(self.active_job_ids())}

    # -- 内部 ---------------------------------------------------------------

    @staticmethod
    def _spawn(
        argv: list[str], run_dir: Path, env: Mapping[str, str]
    ) -> subprocess.Popen[str]:
        """包一层 `spawn()`：把 OSError 折成安全文案（路径/权限细节不外泄）。"""
        try:
            return spawn(argv, cwd=str(run_dir), env=env, creation_flags=CREATE_NO_WINDOW)
        except OSError:
            logger.warning("拉起课程子进程失败", exc_info=True)
            raise CourseError("课程进程启动失败，请检查运行环境。", 500) from None

    def _watch(self, run: CourseRun) -> None:
        """等待子进程（按需限时），排空两条通道，再裁定唯一终态。

        这里**不改任务状态**：子进程退出后该判 completed/partial/failed 是 M9 的
        终局裁定。M8 只负责"退出前把已经吐出来的东西消化完"：读线程到 EOF 各投
        一个哨兵，消费者读到两个哨兵才退出（退出前强制落盘一次），watcher 在这里
        限时 join。因此 `run.had_result` / `run.result_payload` 在 watcher 返回时
        已经是最新值，M9 可以直接拿来裁定，不必重读事件文件。
        """
        try:
            timeout = _process_timeout_seconds(run.time_limit_minutes)
            try:
                run.returncode = run.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                run.process_timed_out = True
                still_alive = _terminate_process(run.proc, STOP_GRACE_SECONDS)
                if still_alive:
                    logger.error("课程任务 %s 超时后仍未退出，继续等待进程确认退出", run.job_id)
                    run.returncode = run.proc.wait()
                else:
                    run.returncode = run.proc.poll()
        except Exception:  # pragma: no cover - wait() 本身很少抛，兜底不打死线程
            logger.exception("等待课程进程退出时出错：%s", run.job_id)
        finally:
            self._drain_pumps(run)
            if run.proc.poll() is not None:
                try:
                    self.finalize(
                        run,
                        run.returncode if run.returncode is not None else run.proc.poll(),
                        run.had_result,
                        run.result_payload,
                    )
                except Exception:
                    logger.exception("课程任务 %s 终局裁定失败", run.job_id)
                    run.finalized_event.set()

    def finalize(
        self,
        run: CourseRun,
        exit_code: int | None,
        had_result: bool,
        result_payload: Mapping[str, Any] | None,
    ) -> CourseJob:
        """在进程与事件泵均收尾后裁定状态、释放槽并记录终局事件。"""
        with run.finalize_lock:
            if run.finalized_event.is_set():
                return self._store.get_job(run.job_id)
            try:
                self._drain_pumps(run)
                current = self._store.get_job(run.job_id)

                # stop() 的 kill 超时已落盘失败；不要让 watcher 再覆盖该结论。
                if current.status not in {CourseStatus.RUNNING, CourseStatus.STOPPING}:
                    return current

                result_status = None
                result_category = None
                if had_result and isinstance(result_payload, Mapping):
                    candidate = result_payload.get("status")
                    if candidate in {"completed", "partial", "failed"}:
                        result_status = candidate
                        result_category = result_payload.get("safe_error_category")

                # 裁定优先级：用户取消 > 超时 > 最后一个合法 result > 退出码兜底。
                if run.cancel_event.is_set():
                    target = CourseStatus.CANCELLED
                    category = None
                elif run.process_timed_out:
                    target = CourseStatus.FAILED
                    category = "process_timeout"
                elif result_status == "completed":
                    target = CourseStatus.COMPLETED
                    category = None
                elif result_status == "partial":
                    target = CourseStatus.PARTIAL
                    category = result_category or "time_limit"
                elif result_status == "failed":
                    target = CourseStatus.FAILED
                    category = result_category
                elif exit_code == 0:
                    target = CourseStatus.FAILED
                    category = "no_result"
                else:
                    target = CourseStatus.FAILED
                    category = "process_exit"

                # 合法状态机没有 running→cancelled 直达边。
                if target is CourseStatus.CANCELLED and current.status is CourseStatus.RUNNING:
                    ensure_transition(current.status, CourseStatus.STOPPING)
                    current = self._store.update(current, status=CourseStatus.STOPPING)
                    self._store.append_business_event(
                        run.job_id,
                        "course_stopping",
                        CourseStatus.RUNNING,
                        CourseStatus.STOPPING,
                        operation="user_stop",
                    )

                ensure_transition(current.status, target)
                # 槽和运行目录必须等两条 reader、消费者全部收尾后再释放/清理。
                self._release_slot(current)
                completed = run.counts.get("completed", 0)
                failed = run.counts.get("failed", 0)
                total = current.total_count
                if result_status is not None:
                    completed = int(result_payload.get("completed_count", completed))  # type: ignore[union-attr]
                    failed = int(result_payload.get("failed_count", failed))  # type: ignore[union-attr]
                    total = int(result_payload.get("total_count", total))  # type: ignore[union-attr]
                if result_status == "completed":
                    percent = 100.0
                elif result_status is not None and total > 0:
                    percent = min(100.0, completed / total * 100.0)
                else:
                    # 没有 result 时保留最后一条合法 progress，退出码不代表进度。
                    percent = run.percent

                updated = self._store.update(
                    current,
                    status=target,
                    phase=CoursePhase.RESULT,
                    progress_percent=percent,
                    completed_count=completed,
                    total_count=total,
                    failed_count=failed,
                    run_id=None,
                    last_safe_error_category=category,
                )
                self._store.append_business_event(
                    run.job_id,
                    "course_finished",
                    current.status,
                    target,
                    safe_error_category=category,
                )
                return updated
            finally:
                self._cleanup_run_upstream(run.job_id)
                self._drop_run(run.job_id)
                run.finalized_event.set()

    def _cleanup_run_upstream(self, job_id: str) -> None:
        """清除本次运行的 vendor 副本；保留诊断日志和两类事件文件。"""
        try:
            upstream = self._store.job_dir(job_id) / "run" / "upstream"
            if upstream.is_dir():
                shutil.rmtree(upstream)
        except OSError:
            logger.warning("课程任务 %s 的上游运行副本清理失败", job_id, exc_info=True)

    # -- 事件泵（M8）--------------------------------------------------------

    def _start_pumps(self, run: CourseRun) -> None:
        """起 1 个消费者 + 2 个读线程。读线程只 `put`，一切落盘都在消费者里。"""
        tag = run.job_id[:8]
        pump: queue.Queue[tuple[str | None, Any]] = queue.Queue(maxsize=PUMP_QUEUE_SIZE)
        run.pump_queue = pump
        run.consumer = threading.Thread(
            target=self._consume, args=(run, pump), name=f"course-pump-{tag}", daemon=True
        )
        run.readers = [
            threading.Thread(
                target=self._read_stream,
                args=(run, pump, "stdout"),
                name=f"course-stdout-{tag}",
                daemon=True,
            ),
            threading.Thread(
                target=self._read_stream,
                args=(run, pump, "stderr"),
                name=f"course-stderr-{tag}",
                daemon=True,
            ),
        ]
        run.threads.append(run.consumer)
        run.threads.extend(run.readers)
        # 消费者先起来，读线程第一次 put 才不会白等一轮超时。
        run.consumer.start()
        for reader in run.readers:
            reader.start()

    def _read_stream(
        self, run: CourseRun, pump: "queue.Queue[tuple[str | None, Any]]", channel: str
    ) -> None:
        """读一条管道。stdout 走 `iter_jsonl`（回调只记长度），stderr 原样入队。

        **不在这里落盘、不在这里判字段**：读线程一慢，子进程就写不动。
        """
        stream = run.proc.stdout if channel == "stdout" else run.proc.stderr
        try:
            if stream is None:
                return
            if channel == "stdout":
                for event in iter_jsonl(
                    stream,
                    on_invalid=lambda _size, target=run: _bump_protocol_errors(target),
                ):
                    if not self._enqueue(run, pump, ("stdout", event)):
                        break
            else:
                for raw in stream:
                    if not self._enqueue(run, pump, ("stderr", raw)):
                        break
        except Exception:  # 管道被强杀的读异常不该把整个运行拖下水
            logger.exception("读取课程子进程 %s 出错：%s", channel, run.job_id)
        finally:
            # 哨兵必须送到，否则消费者会一直等第二个 EOF。
            self._enqueue(run, pump, (None, None), final=True)

    def _enqueue(
        self,
        run: CourseRun,
        pump: "queue.Queue[tuple[str | None, Any]]",
        item: tuple[str | None, Any],
        *,
        final: bool = False,
    ) -> bool:
        """入队；队列满就等（对子进程形成反压），停泵后才丢弃并计数。

        不丢事件的前提是"消费者活着"——只要它在，`put` 迟早成功。`final` 是 EOF
        哨兵，即使已经停泵也要尽力投递，但最多等 `ENQUEUE_GRACE_SECONDS`。
        """
        deadline = time.monotonic() + (ENQUEUE_GRACE_SECONDS if final else 0.0)
        while True:
            try:
                pump.put(item, timeout=0.2)
                return True
            except queue.Full:
                if run.pump_stop.is_set() and not final:
                    run.dropped_queue_items += 1
                    return False
                if final and time.monotonic() > deadline:
                    run.dropped_queue_items += 1
                    return False

    def _consume(self, run: CourseRun, pump: "queue.Queue[tuple[str | None, Any]]") -> None:
        """唯一的落盘点：stdout → 内存态 + job.json + 事件文件；stderr → 脱敏日志。

        节拍：progress 每 `FLUSH_INTERVAL_SECONDS` 最多写一次盘，其余事件立即写，
        退出前强制写一次。合并只发生在"还没落盘的这几条"上；store 里那层 500ms
        合并（`_should_merge_progress`）照旧生效，两层不重复实现同一件事。
        """
        eofs = 0
        try:
            while eofs < 2:
                try:
                    channel, payload = pump.get(timeout=0.2)
                except queue.Empty:
                    if run.pump_stop.is_set() and run.proc.poll() is not None and pump.empty():
                        break  # 兜底：哨兵没送达时也别永远卡着
                    self._flush(run, force=False)
                    continue
                if channel is None:
                    eofs += 1
                    continue
                if channel == "stdout":
                    kind = self._handle_event(run, payload)
                    self._flush(run, force=kind in FLUSH_NOW_TYPES)
                else:
                    self._queue_diagnostic(run, payload)
                    self._flush(run, force=False)
        except Exception:  # pragma: no cover - 消费者死掉等于静默丢事件，必须留痕
            logger.exception("课程事件泵异常退出：%s", run.job_id)
        finally:
            run.pump_stop.set()
            try:
                self._flush(run, force=True)
            except Exception:  # pragma: no cover
                logger.exception("课程事件泵收尾落盘失败：%s", run.job_id)

    def _handle_event(self, run: CourseRun, event: Any) -> str | None:
        """校验并吸收一条 stdout 事件，返回通过校验的 type（违约返回 None）。

        字段判定全部交给 `protocol.validate_event()`；这里只负责"合法之后怎么更新
        内存态"。违约只计数、不留原文（§4.3 的脱敏约定）。
        """
        normalized = validate_event(event)
        if normalized is None:
            _bump_protocol_errors(run)
            return None

        kind = normalized["type"]
        # diagnostic 只进日志：它可能带上游报错细节，按 §4.3 不进前端事件流。
        if not is_frontend_event(normalized):
            self._queue_diagnostic(
                run, f"diagnostic:{normalized['category']}:{normalized['message']}"
            )
            return kind

        # 只在"尚未落盘的连续同 stage 进度"上合并，保留最后一个快照。
        # 不能把不同事件的先后顺序改掉；store 的 500ms 合并仍负责已落盘部分。
        if (
            kind == "progress"
            and run.pending_events
            and run.pending_events[-1].get("type") == "progress"
            and run.pending_events[-1].get("stage") == normalized["stage"]
        ):
            run.pending_events[-1] = normalized
        else:
            run.pending_events.append(normalized)
        if kind == "phase":
            self._apply_phase(run, normalized)
        elif kind == "current":
            title = normalized["title"]
            run.current_title = title
            run.pending_changes["current_title"] = title
        elif kind == "progress":
            self._apply_progress(run, normalized)
        elif kind == "result":
            run.had_result = True
            run.result_payload = dict(normalized)
            run.counts["completed"] = normalized["completed_count"]
            run.counts["failed"] = normalized["failed_count"]
            run.pending_changes["completed_count"] = normalized["completed_count"]
            run.pending_changes["failed_count"] = normalized["failed_count"]
            # result 里没有 percent，进度条按"全做完"收口；不许把总数归零。
            if normalized["total_count"]:
                run.pending_changes["total_count"] = normalized["total_count"]
        return kind

    @staticmethod
    def _apply_phase(run: CourseRun, event: Mapping[str, Any]) -> None:
        """阶段切换：内存 + 落盘字段 + 一条业务事件（详情页时间线用）。"""
        stage = event["stage"]
        if stage == run.phase:
            return
        run.phase = stage
        if stage in _PHASE_VALUES:
            run.pending_changes["phase"] = CoursePhase(stage)
        # 事件名固定 "course_phase_changed"，阶段名放 operation：
        # from/to_status 对阶段变化没有意义（不是任务状态迁移）。
        run.pending_phases.append(stage)

    @staticmethod
    def _apply_progress(run: CourseRun, event: Mapping[str, Any]) -> None:
        """进度：只覆盖本条带来的字段，缺的沿用上一条，绝不归零。"""
        percent = float(event["percent"])
        run.percent = percent
        run.pending_changes["progress_percent"] = percent
        completed = event["completed_count"]
        total = event["total_count"]
        run.counts["completed"] = completed
        run.pending_changes["completed_count"] = completed
        run.pending_changes["total_count"] = total

    def _flush(self, run: CourseRun, *, force: bool) -> None:
        """按节拍落盘。`force` 只在非 progress 事件、收尾和排空时用。"""
        now = time.monotonic()
        if force or (now - run.last_flush_at) >= FLUSH_INTERVAL_SECONDS:
            self._write_state(run)
            run.last_flush_at = time.monotonic()
        self._drain_diagnostics(run, final=force)

    def _write_state(self, run: CourseRun) -> None:
        """把内存态写回 `job.json` + 两个事件文件。

        顺序固定：先 job.json（详情页主体：phase/current_title/进度），再运行时
        事件，最后阶段业务事件。`updated_at` 由 `store.update()` 自动 +1ms 单调
        推进（§5.6 第 3 条），这里不自己拼时间戳。
        """
        changes = run.pending_changes
        run.pending_changes = {}
        if changes:
            try:
                self._store.update(self._store.get_job(run.job_id), **changes)
            except (CourseError, ValueError):
                logger.warning("课程任务 %s 已不可更新，停止事件泵", run.job_id, exc_info=True)
                run.pump_stop.set()
                return

        while run.pending_events:
            event = run.pending_events.popleft()
            try:
                record = self._store.append_runtime_event(run.job_id, event)
            except (CourseError, ValueError):
                logger.warning("课程任务 %s 无法写入运行时事件", run.job_id, exc_info=True)
                run.pump_stop.set()
                return
            sequence = record.get("sequence")
            if isinstance(sequence, int) and not isinstance(sequence, bool):
                run.event_seq = sequence

        while run.pending_phases:
            stage = run.pending_phases.pop(0)
            try:
                self._store.append_business_event(
                    run.job_id, "course_phase_changed", None, None, operation=stage
                )
            except CourseError:
                logger.warning("课程任务 %s 无法写入阶段事件", run.job_id, exc_info=True)
                run.pump_stop.set()
                return

    # -- stderr 通道（M8）---------------------------------------------------

    def _diagnostics_path(self, run: CourseRun) -> Path:
        return self._store.job_dir(run.job_id) / DIAGNOSTICS_FILE

    def _queue_diagnostic(self, run: CourseRun, raw: Any) -> None:
        """脱敏后进内存缓冲；真正写盘在 `_drain_diagnostics()`。"""
        if run.diagnostics_truncated:
            return
        line = sanitize_diagnostic(raw).strip("\r\n")
        if not line.strip():
            return
        line = line + "\n"
        run.diagnostics_buffer.append(line)
        run.diagnostics_buffered_bytes += len(line.encode("utf-8", "replace"))

    def _drain_diagnostics(self, run: CourseRun, *, final: bool) -> None:
        """批量写 diagnostics.log：凑够 64KB 或收尾时落一次，避免每行一次 open。"""
        if not run.diagnostics_buffer:
            return
        if not final and run.diagnostics_buffered_bytes < DIAGNOSTICS_FLUSH_BYTES:
            return
        buffered = "".join(run.diagnostics_buffer)
        run.diagnostics_buffer.clear()
        run.diagnostics_buffered_bytes = 0

        payload = buffered.encode("utf-8", "replace")
        remaining = DIAGNOSTICS_MAX_BYTES - run.diagnostics_bytes
        marker = (LOG_TRUNCATED_MARKER + "\n").encode("utf-8")
        if len(payload) >= remaining:
            # 截断：能塞多少塞多少（按 UTF-8 边界回退），末尾补一行固定标记。
            keep = max(0, remaining - len(marker))
            payload = payload[:keep].decode("utf-8", "ignore").encode("utf-8") + marker
            run.diagnostics_truncated = True
        try:
            path = self._diagnostics_path(run)
            with open(path, "ab") as handle:
                handle.write(payload)
        except (OSError, ValueError):
            logger.warning("写入课程诊断日志失败：%s", run.job_id, exc_info=True)
            return
        run.diagnostics_bytes += len(payload)

    # -- 排空 ---------------------------------------------------------------

    def _drain_pumps(self, run: CourseRun) -> None:
        """进程已退出：先限时 join 两个 reader，再等消费者清空队列。

        reader EOF 哨兵到齐后，消费者会排空并终写。任一线程超时都留日志并
        继续有界收尾，不能让关机/lifespan 被一个卡住的管道或落盘拖死。
        """
        for thread in run.readers:
            if thread.is_alive():
                thread.join(2.0)
            if thread.is_alive():
                logger.warning("课程任务 %s 的输出 reader 未及时结束：%s", run.job_id, thread.name)

        consumer = run.consumer
        if consumer is not None and consumer.is_alive():
            consumer.join(PUMP_DRAIN_SECONDS)
            if consumer.is_alive():
                logger.warning(
                    "课程事件泵 %s 未在 %.0fs 内排空，强制停止", run.job_id, PUMP_DRAIN_SECONDS
                )
        run.pump_stop.set()
        if consumer is not None and consumer.is_alive():
            consumer.join(2.0)

    def _shutdown_pumps(self, run: CourseRun) -> None:
        """启动失败/放弃运行时的快停：置停泵标志，最多再等 2s 各线程退出。"""
        run.pump_stop.set()
        for thread in [run.consumer, *run.readers]:
            if thread is not None and thread.is_alive():
                thread.join(2.0)

    def _write_request(self, run: CourseRun, payload: Mapping[str, Any]) -> None:
        """把启动参数写成一行 JSON 到 stdin（Cookie 只走这里，不进环境/命令行）。"""
        stdin = run.proc.stdin
        if stdin is None:
            raise CourseError("课程进程没有可写的输入通道。", 500)
        stdin.write(json.dumps(dict(payload), ensure_ascii=False) + "\n")
        stdin.flush()

    def _abort(self, run: CourseRun, category: str) -> None:
        """启动中途失败：杀进程、停泵、写 failed、释放槽，不留 running 假象。"""
        try:
            _terminate_process(run.proc, STOP_GRACE_SECONDS)
        finally:
            self._shutdown_pumps(run)
            self._drop_run(run.job_id)
            try:
                self._store.release_slot(run.site_id, run.job_id)
            except Exception:
                logger.warning("启动失败后释放课程槽失败：%s", run.job_id, exc_info=True)
            try:
                self._store.update(
                    self._store.get_job(run.job_id),
                    status=CourseStatus.FAILED,
                    run_id=None,
                    last_safe_error_category=category,
                )
                self._store.append_business_event(
                    run.job_id,
                    "course_failed",
                    CourseStatus.RUNNING,
                    CourseStatus.FAILED,
                    safe_error_category=category,
                )
            except Exception:
                logger.exception("启动失败后写入 failed 状态时出错：%s", run.job_id)

    def _release_slot(self, job: CourseJob) -> None:
        try:
            self._store.release_slot(job.site_id, job.job_id)
        except Exception:
            logger.warning("释放课程槽失败：%s", job.job_id, exc_info=True)

    def _drop_run(self, job_id: str) -> None:
        with self._lock:
            self._runs.pop(job_id, None)

    def _prepare_run_dir(self, job_id: str) -> Path:
        if self._run_dir_factory is not None:
            return Path(self._run_dir_factory(job_id))
        return _prepare_run_dir(job_id)

    @staticmethod
    def _run_env(run_dir: Path) -> dict[str, str]:
        """子进程环境：继承父进程，只覆盖与上游强相关的四个变量。"""
        return {
            **os.environ,
            "PYTHONPATH": str(run_dir / "upstream"),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "COLUMNS": "80",
            "LINES": "24",
        }

    @staticmethod
    def _request_payload(
        job: CourseJob,
        run_id: str,
        params: Mapping[str, Any] | None,
        cookies: Mapping[str, str],
    ) -> dict[str, Any]:
        """stdin 里的启动参数（§4.3 的 `request` 行）。

        `params` 允许调用方补 `ai_config` 之类的运行期参数，但**不许改 cookies**：
        凭据只有一个来源（本机 config.json），否则就成了越权入口。
        """
        options = job.options
        payload: dict[str, Any] = {
            "job_id": job.job_id,
            "run_id": run_id,
            "mode": job.mode.value,
            "course_id": job.course_id,
            "class_id": job.class_id,
            "speed": options.speed,
            "threshold": options.threshold,
            "time_limit_minutes": options.time_limit_minutes,
            "process_exam": options.process_exam,
            "ai_config": None,
            "cookies": dict(cookies),
        }
        for key, value in (params or {}).items():
            if key in {"cookies", "job_id", "run_id"}:
                continue
            payload[key] = value
        return payload


# --------------------------------------------------------------- 进程与外部依赖


def _terminate_process(proc: subprocess.Popen[str], timeout_seconds: float) -> bool:
    """terminate → 等 → kill → 再等；返回**是否仍然活着**。

    已退出的进程直接返回 False（幂等），`stop()` 因此可以重复调用。
    """
    if proc.poll() is not None:
        _close_stdin(proc)
        return False
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=max(0.1, timeout_seconds))
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            return True
    finally:
        _close_stdin(proc)
    return proc.poll() is None


def _process_timeout_seconds(time_limit_minutes: int) -> float | None:
    """返回运行上限与十分钟缓冲；0 代表不设业务时限。"""
    if isinstance(time_limit_minutes, bool) or not isinstance(time_limit_minutes, int):
        raise ValueError("time_limit_minutes must be an integer")
    if time_limit_minutes <= 0:
        return None
    return time_limit_minutes * 60 + PROCESS_TIMEOUT_BUFFER_SECONDS


def _close_stdin(proc: subprocess.Popen[str]) -> None:
    """关掉 stdin：让还活着的桥接读到 EOF，而不是永远等下一行。"""
    stdin = proc.stdin
    if stdin is None or stdin.closed:
        return
    try:
        stdin.close()
    except OSError:
        pass


def _prepare_run_dir(job_id: str) -> Path:
    """默认运行目录：`courses/jobs/<id>/run` + vendor 副本。"""
    from .providers.zhihuishu.preflight import prepare_run_dir

    return prepare_run_dir(job_id)


def _site_cookies(site_id: str) -> dict[str, str]:
    """读本机 Cookie（只做读取与错误映射，空集判断留给调用方）。"""
    from ..credentials import CredentialProviderError, LocalCredentialProvider

    try:
        return dict(LocalCredentialProvider().get_site_cookies(site_id))
    except CredentialProviderError:
        raise CourseAuthRequired("读取本机登录凭据失败，请重新登录后重试。") from None


def _now() -> str:
    from ..common.clock import timestamp

    return timestamp()


# --------------------------------------------------------------- 进程内共享实例

_shared_lock = threading.Lock()
_shared: CourseRuntime | None = None


def shared() -> CourseRuntime:
    """WebUI 侧的唯一实例：路由与 lifespan 必须看到同一批运行。"""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = CourseRuntime()
        return _shared


def reset_shared() -> None:
    """丢掉共享实例（测试用；生产没有调用点）。"""
    global _shared
    with _shared_lock:
        _shared = None


def shutdown_all(timeout_seconds: float = 8.0) -> dict[str, int]:
    """lifespan 退出钩子：异常只记日志，绝不让服务退不出去。"""
    try:
        return shared().shutdown_all(timeout_seconds=timeout_seconds)
    except Exception:
        logger.exception("课程进程关机清理失败")
        return {"stopped": 0, "failed": 0, "remaining": 0}
