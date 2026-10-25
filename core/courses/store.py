"""课程任务的 JSON 持久化（T2-02）。

第一版刻意不上 SQLite：课程任务量小、字段还会长，目录 + 单文件更好排查与手工修复。
代价是要自己解决两件事，本模块的全部复杂度都在这里：

1. **半截文件**：任何写入都走 `_atomic_write`——同名 `.tmp` → write → `flush` →
   `os.fsync` → `os.replace`。进程在任何时刻被杀，目录里要么是旧的完整文件，
   要么是新的完整文件，不会出现被截断的 JSON。
2. **并发写坏**：进程内 `threading.RLock` 串行化全部读写。本项目是单进程
   （Uvicorn 单实例、单 worker），因此**不做跨进程文件锁**；`runtime.json`
   的单实例约束在 T2-05，用 pid 存活判断。

目录布局（全部落在 `data_path("courses")` 下，见 core/paths.py）：

```
courses/
├── index.json                 任务摘要索引，overview / 列表**只读它**
├── runtime.json               站点活跃槽（T2-05：每站点一条 {job_id,pid,since}）
└── jobs/<job_id>/
    ├── job.json               单个任务的全部字段
    ├── events.jsonl           业务事件（详情页 events，T2-03）
    └── runtime-events.jsonl   运行时事件（1.6s 轮询增量，T2-03）
```

删除不是就地 rmtree：先 rename 成 `jobs/.trash-<ts>-<job_id>`，再删；`.trash-*`
以 `.` 开头，重建索引时会被跳过（删到一半的目录不会变成幽灵任务）。

两个 JSONL 都**不是**事实源，删掉只丢历史：`job.json` 里的 `status/phase/progress`
永远是当前值。它们各自封顶 `EVENT_LOG_MAX_LINES` 行，超了保留最后 N 行——所以
`event_id` / `sequence` 只会越来越大，不会因为截断而回到 1。

`index.json` 是可再生的派生数据：任何读写失败（缺失、截断、字段缺失、版本不符）
都当作"索引不可用"→ 从 `jobs/*/job.json` 重建，绝不把索引错误抛给上层。
`job.json` 才是事实源；索引与它冲突时永远以 `job.json` 为准（T2-07 也依赖这点）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import weakref
from dataclasses import fields
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping

from ..common.clock import parse_timestamp as _parse_timestamp
from ..common.clock import timestamp as _timestamp
from ..common.fsio import atomic_write_json, atomic_write_text
from ..paths import data_path
from .domain import (
    CourseJob,
    CourseMode,
    CourseOptions,
    CoursePhase,
    CourseStatus,
    ensure_transition,
)
from .errors import CourseBusy, CourseError, CourseNotFound, CourseTransitionError
from .protocol import validate_event


INDEX_VERSION = 1

#: task_id 直接参与路径拼接，必须先过格式校验，杜绝 `..` / 分隔符穿越。
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

#: `job.json` 的全部字段（= CourseJob 字段），顺序即落盘顺序。
JOB_FIELDS: tuple[str, ...] = tuple(field.name for field in fields(CourseJob))

#: `options` 字段，嵌套落盘，避免把 CourseOptions 对象直接扔进 JSON。
OPTION_FIELDS: tuple[str, ...] = tuple(field.name for field in fields(CourseOptions))

#: 可由 `update()` 修改的字段；job_id / created_at / updated_at 不可改。
UPDATABLE_FIELDS: frozenset[str] = frozenset(JOB_FIELDS) - {
    "job_id",
    "created_at",
    "updated_at",
}

#: 索引里保存的摘要字段。overview 需要进度信息，但不该拿 course_id / options 这类
#: 详情字段去撑列表响应；加字段前先确认 T2-04 的 DTO 真的需要。
SUMMARY_FIELDS: tuple[str, ...] = (
    "job_id",
    "site_id",
    "title",
    "mode",
    "status",
    "phase",
    "current_title",
    "progress_percent",
    "completed_count",
    "total_count",
    "failed_count",
    "run_id",
    "created_at",
    "updated_at",
    "last_safe_error_category",
    "model_profile_id",
)

# 多个请求/服务实例可以打开同一根目录，必须共享同一把读写锁。
_ROOT_LOCKS: weakref.WeakValueDictionary[str, Any] = weakref.WeakValueDictionary()
_ROOT_LOCKS_GUARD = threading.Lock()

#: 两个 JSONL 各自的行数上限（T2-03）。超出时保留最后 N 行重写。
EVENT_LOG_MAX_LINES = 500

#: 同一 stage 的连续 progress 事件在这个窗口内合并，只留最后一条（毫秒）。
EVENT_THROTTLE_MS = 500

#: 业务事件文件名与运行时事件文件名（落在任务目录下）。
BUSINESS_EVENT_FILE = "events.jsonl"
RUNTIME_EVENT_FILE = "runtime-events.jsonl"

#: 删除中间态目录前缀。以 `.` 开头，`_rebuild_index` 会跳过。
TRASH_PREFIX = ".trash-"

#: 这些状态意味着"还有子进程可能在跑"；进程没了就是异常终局（§4.4）。
INTERRUPTED_STATUSES: frozenset[str] = frozenset(
    {
        CourseStatus.AUTHENTICATING.value,
        CourseStatus.RUNNING.value,
        CourseStatus.STOPPING.value,
    }
)

#: 崩溃恢复写进 `last_safe_error_category` 的分类（§4.6 安全文案约定）。
RECOVERY_ERROR_CATEGORY = "process_exit"

# Windows：GetExitCodeProcess 返回 STILL_ACTIVE(259) 表示进程还活着。
_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _pid_alive(pid: Any) -> bool:
    """pid 是否仍存活；非法 pid 一律 False。只用标准库。

    Windows 上**不能**用 `os.kill(pid, 0)`：CPython 的 win32 实现把非 CTRL_*
    的信号值直接交给 `TerminateProcess`，传 0 等于"用退出码 0 杀掉进程"，
    轻则误杀、重则把别的进程干掉。这里改走 OpenProcess +
    `PROCESS_QUERY_LIMITED_INFORMATION`（只查状态，不需要终止权限），
    POSIX 下才走 `os.kill(pid, 0)`。
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 存在但不属于当前用户：算活着，不要抢占。
        return True
    except OSError:
        return False
    return True


def _trash_name(job_id: str) -> str:
    """`.trash-<UTC 时间戳>-<job_id>`；时间戳不能带 `:`（Windows 目录名非法）。"""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{TRASH_PREFIX}{stamp}-{job_id}"


def _slot(payload: Any) -> dict[str, Any] | None:
    """把 runtime.json 里的一条活跃槽规范化；不合形状返回 None。"""
    if not isinstance(payload, Mapping):
        return None
    job_id = payload.get("job_id")
    pid = payload.get("pid")
    since = payload.get("since")
    if not isinstance(job_id, str) or not job_id:
        return None
    return {
        "job_id": job_id,
        "pid": pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
        "since": since if isinstance(since, str) else None,
    }


def _next_timestamp(previous: str | None) -> str:
    """返回严格大于 `previous` 的时间戳。

    毫秒精度下同一毫秒内的连续更新会拿到相同的字符串，而验收要求
    `updated_at` 单调不减；这里直接在旧值上 +1ms，既保证单调，也不会因为
    系统时钟回拨把时间写回过去。旧值不可解析时退回当前时间。
    """
    current = _timestamp()
    if not previous:
        return current
    try:
        if _parse_timestamp(current) > _parse_timestamp(previous):
            return current
        return _timestamp(_parse_timestamp(previous) + timedelta(milliseconds=1))
    except (TypeError, ValueError):
        return current


def _enum_value(value: Any, enum_type: type, field: str) -> str:
    """接受枚举或字面量，统一落盘成字符串。"""
    if isinstance(value, enum_type):
        return value.value
    try:
        return enum_type(value).value
    except (TypeError, ValueError):
        raise ValueError(f"{field} is not a supported value") from None


def _optional_str(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string or null")
    return value


def _optional_status(value: Any, field: str) -> str | None:
    """状态入参允许枚举、字面量或 None，统一落盘成字符串。"""
    if value is None:
        return None
    return _enum_value(value, CourseStatus, field)


def _required_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _count(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _percent(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("progress_percent must be a number")
    if not 0 <= float(value) <= 100:
        raise ValueError("progress_percent must be within 0-100")
    return float(value)


class CourseJobStore:
    """课程任务的持久化门面：一个实例 = 一个 `courses/` 根目录。"""

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        self._root = (Path(root) if root is not None else data_path("courses")).resolve()
        self._jobs_root = self._root / "jobs"
        self._index_path = self._root / "index.json"
        self._runtime_path = self._root / "runtime.json"
        with _ROOT_LOCKS_GUARD:
            key = os.path.normcase(str(self._root))
            self._lock = _ROOT_LOCKS.get(key)
            if self._lock is None:
                self._lock = threading.RLock()
                _ROOT_LOCKS[key] = self._lock
        # (job_id → (stage, monotonic 时刻))：仅用于 progress 事件尾行合并。
        self._progress_tail: dict[str, tuple[str, float]] = {}
        self._jobs_root.mkdir(parents=True, exist_ok=True)
        self._ensure_runtime_file()

    # ------------------------------------------------------------------ 路径

    @property
    def root(self) -> Path:
        """`courses/` 根目录。"""
        return self._root

    @property
    def runtime_path(self) -> Path:
        """`runtime.json` 路径；活跃槽的读写归 T2-05。"""
        return self._runtime_path

    def job_dir(self, job_id: str) -> Path:
        """返回任务目录（不保证存在）。job_id 非法时抛 `ValueError`。"""
        return self._jobs_root / self._validate_job_id(job_id)

    def _job_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "job.json"

    # ------------------------------------------------------------------ 写入

    def _atomic_write(self, path: Path, payload: Any) -> None:
        """原子写 JSON 文件（走 core.common.fsio，不再本地实现 tmp+replace）。"""
        atomic_write_json(path, payload)

    def _atomic_write_text(self, path: Path, text: str) -> None:
        """原子写文本（走 core.common.fsio，不再本地实现 tmp+replace）。"""
        atomic_write_text(path, text)

    def _ensure_runtime_file(self) -> None:
        """首次启动时补一个空的活跃槽文件；已存在（哪怕损坏）就不碰。"""
        with self._lock:
            if self._runtime_path.exists():
                return
            self._atomic_write(self._runtime_path, {"active": {}})

    # ------------------------------------------------------------------ 序列化

    def _job_payload(self, job: CourseJob) -> dict[str, Any]:
        """`CourseJob` → 白名单字典。显式逐字段写，避免 `asdict` 把新字段带出去。"""
        return {
            "job_id": job.job_id,
            "site_id": job.site_id,
            "title": job.title,
            "mode": _enum_value(job.mode, CourseMode, "mode") if job.mode is not None else None,
            "course_id": job.course_id,
            "class_id": job.class_id,
            "status": _enum_value(job.status, CourseStatus, "status"),
            "phase": _enum_value(job.phase, CoursePhase, "phase"),
            "current_title": job.current_title,
            "progress_percent": job.progress_percent,
            "completed_count": job.completed_count,
            "total_count": job.total_count,
            "failed_count": job.failed_count,
            "run_id": job.run_id,
            "options": {name: getattr(job.options, name) for name in OPTION_FIELDS},
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "last_safe_error_category": job.last_safe_error_category,
        }

    def _job_from_payload(self, payload: Any) -> CourseJob:
        """白名单字典 → `CourseJob`，逐字段校验；失败抛 `ValueError`。

        这里不做状态迁移校验：迁移策略属于服务层（`domain.ensure_transition`），
        存储层只负责"读回来的东西一定是合法值类型"。
        """
        if not isinstance(payload, dict):
            raise ValueError("job payload must be an object")
        missing = [name for name in JOB_FIELDS if name not in payload]
        if missing:
            raise ValueError(f"job payload is missing fields: {', '.join(missing)}")

        options = payload["options"]
        if not isinstance(options, dict):
            raise ValueError("options must be an object")
        unknown_options = set(options) - set(OPTION_FIELDS)
        if unknown_options:
            raise ValueError(f"options has unknown fields: {', '.join(sorted(unknown_options))}")

        return CourseJob(
            job_id=self._validate_job_id(payload["job_id"]),
            site_id=_required_str(payload["site_id"], "site_id"),
            title=_required_str(payload["title"], "title"),
            mode=(CourseMode(_enum_value(payload["mode"], CourseMode, "mode")) if payload["mode"] is not None else None),
            course_id=_optional_str(payload["course_id"], "course_id"),
            class_id=_optional_str(payload["class_id"], "class_id"),
            status=CourseStatus(_enum_value(payload["status"], CourseStatus, "status")),
            phase=CoursePhase(_enum_value(payload["phase"], CoursePhase, "phase")),
            current_title=_optional_str(payload["current_title"], "current_title"),
            progress_percent=_percent(payload["progress_percent"]),
            completed_count=_count(payload["completed_count"], "completed_count"),
            total_count=_count(payload["total_count"], "total_count"),
            failed_count=_count(payload["failed_count"], "failed_count"),
            run_id=_optional_str(payload["run_id"], "run_id"),
            options=CourseOptions(
                **{name: options[name] for name in OPTION_FIELDS if name in options}
            ),
            created_at=_required_str(payload["created_at"], "created_at"),
            updated_at=_required_str(payload["updated_at"], "updated_at"),
            last_safe_error_category=_optional_str(
                payload["last_safe_error_category"], "last_safe_error_category"
            ),
        )

    def _summary_payload(self, job: CourseJob) -> dict[str, Any]:
        """任务 → 索引条目（SUMMARY_FIELDS 白名单）。"""
        payload = self._job_payload(job)
        payload["model_profile_id"] = job.options.model_profile_id
        return {name: payload[name] for name in SUMMARY_FIELDS}

    @staticmethod
    def _validate_job_id(value: Any) -> str:
        if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
            raise ValueError("job_id has an invalid format")
        return value

    # ------------------------------------------------------------------ 索引

    def _read_index(self) -> list[dict[str, Any]] | None:
        """读索引；任何异常都返回 `None`（= 需要重建），不抛给上层。"""
        try:
            raw = json.loads(self._index_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict) or raw.get("version") != INDEX_VERSION:
            return None
        entries = raw.get("jobs")
        if not isinstance(entries, list):
            return None
        for entry in entries:
            if not isinstance(entry, dict):
                return None
            if any(name not in entry for name in SUMMARY_FIELDS):
                return None
            if not isinstance(entry.get("job_id"), str):
                return None
        return entries

    def _write_index(self, entries: list[dict[str, Any]]) -> None:
        ordered = sorted(
            entries,
            key=lambda entry: (entry.get("updated_at", ""), entry.get("job_id", "")),
            reverse=True,
        )
        self._atomic_write(
            self._index_path,
            {"version": INDEX_VERSION, "updated_at": _timestamp(), "jobs": ordered},
        )

    def _rebuild_index(self) -> list[dict[str, Any]]:
        """扫 `jobs/*/job.json` 重建索引，返回重建后的条目。

        单个 job.json 损坏/被截断时跳过它而不是让整张索引失败——宁可少一条
        记录，也不能让 overview 因为一个坏文件整页 500。`.trash-*` 这类
        T2-06 的删除中间态目录同样跳过。
        """
        entries: list[dict[str, Any]] = []
        for job_path in sorted(self._jobs_root.glob("*/job.json")):
            if job_path.parent.name.startswith("."):
                continue
            try:
                job = self._job_from_payload(json.loads(job_path.read_text(encoding="utf-8")))
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError, KeyError):
                continue
            entries.append(self._summary_payload(job))
        self._write_index(entries)
        return entries

    def _upsert_index_entry(self, summary: dict[str, Any]) -> None:
        """更新单条索引；索引不可用时整体重建（顺带把这条带进去）。"""
        entries = self._read_index()
        if entries is None:
            self._rebuild_index()
            return
        entries = [entry for entry in entries if entry.get("job_id") != summary["job_id"]]
        entries.append(summary)
        self._write_index(entries)

    # ------------------------------------------------------------------ 公开 API

    def create(self, job: CourseJob) -> CourseJob:
        """新建任务：写 `job.json` 并同步索引。ID 重复抛 409。"""
        if not isinstance(job, CourseJob):
            raise TypeError("job must be a CourseJob")
        with self._lock:
            payload = self._job_payload(job)
            path = self._job_path(job.job_id)
            if path.exists():
                raise CourseError("课程任务已存在。", 409)
            self._atomic_write(path, payload)
            self._upsert_index_entry(self._summary_payload(job))
            return job

    def get_job(self, job_id: str) -> CourseJob:
        """读单个任务（事实源是 `job.json`，不读索引）。"""
        with self._lock:
            path = self._job_path(job_id)
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                raise CourseNotFound("课程任务不存在。") from None
            except (OSError, UnicodeError, json.JSONDecodeError):
                raise CourseError("课程任务数据无法读取。", 500) from None
            try:
                return self._job_from_payload(raw)
            except ValueError:
                raise CourseError("课程任务数据格式不正确。", 500) from None

    def list_summaries(self, include_archived: bool = False) -> list[dict[str, Any]]:
        """列表/overview 数据源：**只读 index.json**，绝不遍历 job 目录。

        索引缺失或损坏时才触发一次重建。返回的是新字典副本，调用方改它不会
        影响存储。
        """
        with self._lock:
            entries = self._read_index()
            if entries is None:
                entries = self._rebuild_index()
            if include_archived:
                return [dict(entry) for entry in entries]
            archived = CourseStatus.ARCHIVED.value
            return [dict(entry) for entry in entries if entry.get("status") != archived]

    def update(self, job: CourseJob, **changes: Any) -> CourseJob:
        """按字段增量更新，并把 `updated_at` 刷成新时间戳。

        以磁盘上的最新值为基准（调用方手里的 `job` 只用来定位任务与字段），
        避免把并发期间别人写的进度覆盖回去。未知字段、不可变字段一律
        `ValueError`，不静默丢弃。
        """
        if not isinstance(job, CourseJob):
            raise TypeError("job must be a CourseJob")
        unknown = set(changes) - UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"不可更新的字段：{', '.join(sorted(unknown))}")
        with self._lock:
            current = self.get_job(job.job_id)
            payload = self._job_payload(current)
            payload.update(self._normalize_changes(changes))
            payload["updated_at"] = _next_timestamp(payload["updated_at"])
            updated = self._job_from_payload(payload)
            self._atomic_write(self._job_path(updated.job_id), payload)
            self._upsert_index_entry(self._summary_payload(updated))
            return updated

    @staticmethod
    def _normalize_changes(changes: dict[str, Any]) -> dict[str, Any]:
        """把枚举/`CourseOptions` 之类的入参折算成可落盘的值。"""
        normalized: dict[str, Any] = {}
        for name, value in changes.items():
            if name == "status":
                normalized[name] = _enum_value(value, CourseStatus, "status")
            elif name == "phase":
                normalized[name] = _enum_value(value, CoursePhase, "phase")
            elif name == "mode":
                normalized[name] = _enum_value(value, CourseMode, "mode") if value is not None else None
            elif name == "options":
                options = value
                if isinstance(options, CourseOptions):
                    normalized[name] = {
                        field: getattr(options, field) for field in OPTION_FIELDS
                    }
                elif isinstance(options, dict):
                    unknown = set(options) - set(OPTION_FIELDS)
                    if unknown:
                        raise ValueError(
                            f"options 含未知字段：{', '.join(sorted(unknown))}"
                        )
                    normalized[name] = dict(options)
                else:
                    raise ValueError("options 必须是 CourseOptions 或 dict")
            else:
                normalized[name] = value
        return normalized


    # ------------------------------------------------------------------ 事件日志

    def _event_path(self, job_id: str, filename: str) -> Path:
        return self.job_dir(job_id) / filename

    def _require_job(self, job_id: str) -> None:
        """写事件前确认任务存在，避免给不存在的 id 造出孤儿目录。"""
        if not self._job_path(job_id).exists():
            raise CourseNotFound("课程任务不存在。")

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        """逐行读 JSONL，坏行跳过而不是抛错。

        追加写可能被强杀在行中间留下半截 JSON；一条坏行不该让整段历史不可读，
        下次重写时它自然会被清掉。
        """
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeError):
            return []
        records: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(value, dict):
                records.append(value)
        return records

    @staticmethod
    def _append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
        """追加一行并刷盘。单行追加不需要临时文件：被截断也只坏这一行。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def _rewrite_jsonl(self, path: Path, records: list[dict[str, Any]]) -> None:
        """封顶重写：只保留最后 `EVENT_LOG_MAX_LINES` 行。"""
        kept = records[-EVENT_LOG_MAX_LINES:]
        text = "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            for record in kept
        )
        self._atomic_write_text(path, text)

    @staticmethod
    def _next_number(records: list[dict[str, Any]], key: str) -> int:
        """取已有记录里 `key` 的最大值 +1；无记录或全坏行时从 1 开始。

        因为封顶只保留**最后**若干行，最大值不会丢，重启后仍能接着递增。
        """
        values = [
            record[key]
            for record in records
            if isinstance(record.get(key), int) and not isinstance(record.get(key), bool)
        ]
        return max(values, default=0) + 1

    def append_business_event(
        self,
        job_id: str,
        event_type: str,
        from_status: CourseStatus | str | None,
        to_status: CourseStatus | str | None,
        operation: str | None = None,
        safe_error_category: str | None = None,
    ) -> dict[str, Any]:
        """追加一条业务事件到 `events.jsonl`（详情页 `events` 的来源）。

        `event_id` 单调整数；`task_id` 取 `job_id`，与做题侧 `TaskEvent` 的字段名
        对齐，前端不必为两种任务写两套时间线渲染。
        """
        event_value = _required_str(event_type, "event_type")
        from_value = _optional_status(from_status, "from_status")
        to_value = _optional_status(to_status, "to_status")
        operation_value = _optional_str(operation, "operation")
        category_value = _optional_str(safe_error_category, "safe_error_category")
        with self._lock:
            self._require_job(job_id)
            path = self._event_path(job_id, BUSINESS_EVENT_FILE)
            records = self._read_jsonl(path)
            record = {
                "event_id": self._next_number(records, "event_id"),
                "task_id": job_id,
                "occurred_at": _timestamp(),
                "event_type": event_value,
                "from_status": from_value,
                "to_status": to_value,
                "operation": operation_value,
                "safe_error_category": category_value,
            }
            records.append(record)
            if len(records) > EVENT_LOG_MAX_LINES:
                self._rewrite_jsonl(path, records)
            else:
                self._append_jsonl(path, record)
            return dict(record)

    def append_runtime_event(
        self, job_id: str, event: Mapping[str, Any]
    ) -> dict[str, Any]:
        """追加一条运行时事件（`runtime-events.jsonl`，供 1.6s 轮询增量读）。

        调用方保证事件已过 `protocol.validate_event`；这里再跑一次做**归一化**，
        白名单会丢掉未知键——即使上游偷偷塞了 URL 或原始报错也进不来。
        """
        normalized = validate_event(event)
        if normalized is None:
            raise ValueError("runtime event does not match the protocol contract")
        with self._lock:
            self._require_job(job_id)
            path = self._event_path(job_id, RUNTIME_EVENT_FILE)
            records = self._read_jsonl(path)
            now = time.monotonic()
            if self._should_merge_progress(job_id, normalized, records, now):
                merged = {**normalized, "sequence": records[-1]["sequence"]}
                records[-1] = merged
                self._progress_tail[job_id] = (normalized["stage"], now)
                self._rewrite_jsonl(path, records)
                return dict(merged)
            record = {**normalized, "sequence": self._next_number(records, "sequence")}
            records.append(record)
            if len(records) > EVENT_LOG_MAX_LINES:
                self._rewrite_jsonl(path, records)
            else:
                self._append_jsonl(path, record)
            if normalized["type"] == "progress":
                self._progress_tail[job_id] = (normalized["stage"], now)
            else:
                self._progress_tail.pop(job_id, None)
            return dict(record)

    def _should_merge_progress(
        self,
        job_id: str,
        event: Mapping[str, Any],
        records: list[dict[str, Any]],
        now: float,
    ) -> bool:
        """是否把这条 progress 合并进尾行。

        只合并**文件最后一行**：往前插入一条会打乱 `sequence` 的阅读顺序，
        也会让已经轮询过去的客户端看到"历史被改写"。合并时沿用旧 `sequence`，
        保证编号始终连续、`after` 增量游标不会被顶掉。
        """
        if event.get("type") != "progress" or not records:
            return False
        tail = records[-1]
        if tail.get("type") != "progress" or tail.get("stage") != event.get("stage"):
            return False
        if not isinstance(tail.get("sequence"), int):
            return False
        last = self._progress_tail.get(job_id)
        if last is None or last[0] != event.get("stage"):
            return False
        return (now - last[1]) * 1000 < EVENT_THROTTLE_MS

    def read_business_events(
        self, job_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        """按写入顺序返回**最近** `limit` 条业务事件。

        任务不存在或还没有事件时返回空列表：读侧不校验任务存在性，
        免得 1.6s 轮询每次都多读一次 `job.json`（404 由路由层用 `get_job` 判）。
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        self._validate_job_id(job_id)
        if limit == 0:
            return []
        with self._lock:
            records = self._read_jsonl(self._event_path(job_id, BUSINESS_EVENT_FILE))
        return [dict(record) for record in records[-limit:]]

    def read_runtime_events(self, job_id: str, after: int = 0) -> list[dict[str, Any]]:
        """返回 `sequence > after` 的运行时事件，按 `sequence` 升序。

        这就是 `GET /api/tasks/{id}/runtime-events?after=` 的增量契约：
        客户端把上一次拿到的最大 `sequence` 传回来，只取新增部分。
        """
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError("after must be a non-negative integer")
        self._validate_job_id(job_id)
        with self._lock:
            records = self._read_jsonl(self._event_path(job_id, RUNTIME_EVENT_FILE))
        return [
            dict(record)
            for record in records
            if isinstance(record.get("sequence"), int) and record["sequence"] > after
        ]


    # ------------------------------------------------------------------ 生命周期

    def archive(self, job_id: str) -> CourseJob:
        """归档：`ensure_transition` 放行才写，落盘 + 索引 + 业务事件一次做完。

        `running → archived` 不在迁移表里，所以正在跑的任务归档会抛
        `CourseTransitionError`（409），不用在这里另写 if。
        """
        with self._lock:
            job = self.get_job(job_id)
            ensure_transition(job.status, CourseStatus.ARCHIVED)
            updated = self.update(job, status=CourseStatus.ARCHIVED)
            self.append_business_event(
                job_id, "course_archived", job.status, CourseStatus.ARCHIVED
            )
            return updated

    def delete(self, job_id: str) -> None:
        """删除记录：rename 成 `.trash-*` 再 rmtree，失败先 rename 回来再报错。

        运行中的任务不允许删（§4.5：先停止再删除）：`job.json` 还在被事件泵
        和 1.6s 轮询读写，边跑边删只会造出半截目录。课程没有题包，删记录等于
        删干净。
        """
        with self._lock:
            job = self.get_job(job_id)
            if job.status.value in INTERRUPTED_STATUSES:
                raise CourseBusy("任务还在运行，请先停止再删除。")
            directory = self.job_dir(job_id)
            # 兼容旧版 preflight 的 courses/runs/<id>，先移入任务目录，
            # 与记录一起 rename→删除；失败时沿用原来的回滚路径。
            legacy_run = self._root / "runs" / job_id
            if legacy_run.exists():
                destination = directory / "legacy-run"
                if destination.exists():
                    raise CourseError("旧版课程运行目录清理冲突，请稍后重试。", 500)
                try:
                    legacy_run.rename(destination)
                except OSError:
                    raise CourseError("旧版课程运行目录无法清理，请稍后重试。", 500) from None
            trash = self._jobs_root / _trash_name(job_id)
            try:
                directory.rename(trash)
            except FileNotFoundError:
                # 目录已经不在（并发/手工删过）：索引已经是错的，重建对齐即可。
                self._rebuild_index()
                return
            except OSError:
                raise CourseError("课程任务删除失败，请稍后重试。", 500) from None
            try:
                shutil.rmtree(trash)
            except OSError:
                try:
                    trash.rename(directory)
                except OSError:
                    # 回滚也失败：留着 `.trash-*` 让下次启动/人工收拾，
                    # 但不能掩盖原始失败。
                    pass
                raise CourseError("课程任务删除未完成，请稍后重试。", 500) from None
            self._progress_tail.pop(job_id, None)
            # 非运行态才走到这里，但槽可能是崩溃前的僵尸：顺手释放。
            self.release_slot(job.site_id, job_id)
            self._rebuild_index()

    # ------------------------------------------------------------------ 单实例槽

    def acquire_slot(self, site_id: str, job_id: str, pid: int) -> dict[str, Any]:
        """占用站点的唯一活跃槽。

        同站点已有槽时：持有者 pid 还活着 → `CourseBusy`（409，前端提示稍后重试）；
        已死 → 直接接管并记一条 `stale_slot_reclaimed` 业务事件。
        同一个 job 重复调用是幂等的（保留原 `since`）。
        """
        site = _required_str(site_id, "site_id")
        self._validate_job_id(job_id)
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValueError("pid must be a positive integer")
        with self._lock:
            self._require_job(job_id)
            active = self._read_runtime()
            current = _slot(active.get(site))
            reclaimed = False
            if current is not None and current["job_id"] != job_id:
                if _pid_alive(current["pid"]):
                    raise CourseBusy("该站点正在被其他课程任务使用，请先停止它。")
                reclaimed = True
                current = None
            since = current["since"] if current is not None else None
            slot = {"job_id": job_id, "pid": pid, "since": since or _timestamp()}
            active[site] = slot
            self._write_runtime(active)
            if reclaimed:
                self.append_business_event(job_id, "stale_slot_reclaimed", None, None)
            return dict(slot)

    def release_slot(self, site_id: str, job_id: str | None = None) -> bool:
        """释放槽；传 `job_id` 时只释放自己那条（防止 A 停 B）。没有槽返回 False。"""
        site = _required_str(site_id, "site_id")
        with self._lock:
            active = self._read_runtime()
            current = _slot(active.get(site))
            if current is None:
                return False
            if job_id is not None and current["job_id"] != job_id:
                return False
            active.pop(site, None)
            self._write_runtime(active)
            return True

    def active_slot(self, site_id: str) -> dict[str, Any] | None:
        """读当前活跃槽（只读；起停前判占用用，不校验 pid 存活）。"""
        site = _required_str(site_id, "site_id")
        with self._lock:
            return _slot(self._read_runtime().get(site))

    def _read_runtime(self) -> dict[str, Any]:
        """读活跃槽；文件损坏/形状不符一律当"没有槽"（它是可再生状态）。"""
        try:
            raw = json.loads(self._runtime_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        entries = raw.get("active")
        if not isinstance(entries, dict):
            return {}
        active: dict[str, Any] = {}
        for site, payload in entries.items():
            slot = _slot(payload)
            if isinstance(site, str) and slot is not None:
                active[site] = slot
        return active

    def _write_runtime(self, active: Mapping[str, Any]) -> None:
        """整文件原子写回（活跃槽只有几条，不值得做增量）。"""
        self._atomic_write(self._runtime_path, {"active": dict(active)})

    # ------------------------------------------------------------------ 崩溃恢复

    def recover_on_start(self) -> dict[str, int]:
        """启动时把"上次没退出干净"的任务收成 `failed(process_exit)`。

        判据只有一条：`job.json` 里的状态落在 `INTERRUPTED_STATUSES`。进程都
        重启了，`running/stopping/authenticating` 只可能是崩溃残留，继续显示
        "学习中"会让用户对着一个永远不动的进度条等。

        同时清空 `runtime.json` 的活跃槽（持有者必死），并按 `job.json` 重建
        索引——索引是派生数据，冲突时永远以 `job.json` 为准。
        返回 `{"recovered": n, "cleared_slots": m}` 供启动日志记录。
        """
        recovered: list[str] = []
        with self._lock:
            for job_path in sorted(self._jobs_root.glob("*/job.json")):
                if job_path.parent.name.startswith("."):
                    continue
                try:
                    job = self._job_from_payload(
                        json.loads(job_path.read_text(encoding="utf-8"))
                    )
                except (OSError, UnicodeError, json.JSONDecodeError, ValueError, KeyError):
                    continue
                if job.status.value not in INTERRUPTED_STATUSES:
                    continue
                try:
                    ensure_transition(job.status, CourseStatus.FAILED)
                except CourseTransitionError:
                    # 迁移表没给这条边：留着原状态让人看见，比硬写更安全。
                    continue
                self.update(
                    job,
                    status=CourseStatus.FAILED,
                    run_id=None,
                    last_safe_error_category=RECOVERY_ERROR_CATEGORY,
                )
                self.append_business_event(
                    job.job_id,
                    "recovered_after_restart",
                    job.status,
                    CourseStatus.FAILED,
                    safe_error_category=RECOVERY_ERROR_CATEGORY,
                )
                recovered.append(job.job_id)
            cleared = len(self._read_runtime())
            if cleared:
                self._write_runtime({})
            self._rebuild_index()
        return {"recovered": len(recovered), "cleared_slots": cleared}


__all__ = [
    "BUSINESS_EVENT_FILE",
    "CourseJobStore",
    "EVENT_LOG_MAX_LINES",
    "EVENT_THROTTLE_MS",
    "INDEX_VERSION",
    "INTERRUPTED_STATUSES",
    "JOB_FIELDS",
    "OPTION_FIELDS",
    "RECOVERY_ERROR_CATEGORY",
    "RUNTIME_EVENT_FILE",
    "SUMMARY_FIELDS",
    "TRASH_PREFIX",
    "UPDATABLE_FIELDS",
]
