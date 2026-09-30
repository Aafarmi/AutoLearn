"""UI 侧投影（P5）。

职责边界：**写操作只有一个** —— :meth:`TaskStore.set_state`，服务于人工确认/否决。
机器人的状态推进全部由编排层（P7）写入，本模块不碰。

为什么不放进 ``core/``：这里的每个函数都是为了「界面怎么显示」而存在的
（比如分集序号靠 run 内的 video 条目顺序推导），换一个界面就不一样了。
真正的持久化契约（表结构）仍然在 ``core/db.py``。
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.db import DEFAULT_DB_PATH, connect
from core.enums import MediaState, QuestionState, TargetKind, TaskType
from core.models import SampleRecord
from core.trace import purge_run_cache, run_cache_entries, run_dir
from ui.schemas import ItemDetailOut, TaskDetailOut, TaskItemOut, TaskOut

__all__ = [
    "ARTIFACT_FILES",
    "DB_PATH_ENV",
    "DONE_STATES",
    "LOG_ROOT_ENV",
    "TaskStore",
    "db_file",
    "display_status",
    "log_root",
    "readback_mismatch",
]

#: 留痕根目录覆盖
LOG_ROOT_ENV = "AUTOLEARN_LOG_ROOT"

#: SQLite 库文件覆盖（测试与多实例部署用）
DB_PATH_ENV = "AUTOLEARN_DB_PATH"

#: 视为「已了结」的状态（题目态 + 媒体态）
DONE_STATES: frozenset[str] = frozenset(
    {
        QuestionState.VERIFIED.value,
        QuestionState.FAILED.value,
        QuestionState.SKIPPED.value,
        MediaState.ENDED.value,
        MediaState.IDLE.value,
    }
)

#: 允许通过接口读取的留痕文件名。
#:
#: 加新留痕必须**同步这里**（否则界面点不开它），而且要放进
#: :meth:`RunLogger` 真正会写的那一份清单里 —— 两处对不上时，
#: 症状是「文件明明在磁盘上，界面上却打不开」。
ARTIFACT_FILES: tuple[str, ...] = (
    "before.png",
    "after.png",
    "after_submit.png",
    "perception.json",
    "vision_read.json",
    "solve.json",
    "solve_raw.txt",
    "action.json",
    "verify.json",
)


def display_status(raw: str, *, active: bool) -> tuple[str, str]:
    """``run.status`` → ``(展示态 key, 人话)``。

    为什么要有这一层：库里那个 status 是**给程序看的**（``paused:needs_confirm``
    这种带原因后缀），界面上直接显示既难懂又难配色。展示态收敛成 6 个 key，
    前端按 key 选样式，文案在服务端拼（免得两边各写一份、迟早不一致）。

    ``running`` 但**没有活跃编排器**是「进程没了」这个真实状态 ——
    必须是「已中断」而不是「进行中」，否则用户会一直等一个已经死掉的运行。
    """
    if raw == "created":
        return "created", "待启动"
    if raw == "running":
        return ("running", "进行中") if active else ("stopped", "已中断（进程已退出）")
    if raw.startswith("paused"):
        reason = raw.split(":", 1)[1] if ":" in raw else ""
        return "paused", f"已暂停·待介入{'（' + reason + '）' if reason else ''}"
    if raw == "stopped":
        return "stopped", "已中断"
    if raw == "finished":
        return "finished", "已完成"
    if raw.startswith("error"):
        return "error", "出错"
    return "unknown", raw or "未知"


def log_root() -> Path:
    override = os.environ.get(LOG_ROOT_ENV)
    return Path(override) if override else Path("logs")


def db_file() -> Path:
    """SQLite 库文件位置：环境变量优先，其次 ``core.db`` 的默认值。"""
    override = os.environ.get(DB_PATH_ENV)
    return Path(override) if override else DEFAULT_DB_PATH


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return datetime.now(UTC)
    return datetime.now(UTC)


def _load_json(path: Path) -> dict[str, Any] | None:
    """读留痕 JSON。文件缺失或损坏一律返回 ``None`` —— 界面不该因为缺文件而崩。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def readback_mismatch(action: dict[str, Any] | None) -> bool:
    """这次动作的回读**有没有没验成**（详情页据此显示「动作回读不一致」）。

    判据只认结构化字段，**永远不比较文本**。

    为什么不能用字符串比较（2026-10-01 修的误报）
    --------------------------------------------
    这里原先写的是
    ``bool(action.get("readback") and action.get("readback") != action.get("expected"))``
    —— 拿两个**含义、来源都不同**的串比相等：

    * ``expected`` 是阈值表达式（``act/verifier.py`` 的
      ``REGION_CHANGE_EXPECTED``，形如 ``region_mad≥2.0``）；
    * 而成功动作写进 ``action.json`` 的 ``readback`` 是**人类可读串**：
      ``region_mad=5.32 state=changed aim=ink_centroid`` /
      ``weak_changed:1.20 state=weak aim=ink_centroid`` /
      ``no_change_on_ink:region_mad=0.00 aim=ink_centroid`` …

    而且 ``action.json`` 里**压根没有** ``expected`` 这个键（它只出现在
    ``verify.json`` 与事件流里），于是「文本不相等」恒为真 —— 每一次成功点击
    都在详情页显示「回读不一致」。用户报的「28、29 的选项与解题组给的一致，
    详情页却显示回读不一致」就是这一条。``readback`` 的文案是给人看的，
    措辞随时可以改，**不能当判据**。

    判据（按优先级）
    ----------------
    1. ``readback_ok``（``ActionResult`` 的结构化结论，由 ``act/actuator.py``
       在 ``_done`` / ``_exhausted`` 等收尾点显式填写）在 → 直接用它。
       ``True`` = 一致，或该动作本来就没有可校验的回读（``skipped:`` / 手势 /
       无候选点时的正常收工）；``False`` = 明确没验成（重放耗尽、提交超时…）。
    2. 它不在（2026-10-01 之前的**老留痕**）→ 退回同一条留痕里同样结构化的
       ``ok``：``ok is False`` 才算不一致。老的成功动作 ``ok=True``，
       照样显示「一致」—— **历史任务不会因为少一个字段就全变红**。
    3. 两个都没有（更老的留痕 / 空文件）→ 按「一致」处理：缺证据时宁可不说，
       也不能凭空报一次不一致。
    """
    if not isinstance(action, dict):
        return False
    explicit = action.get("readback_ok")
    if isinstance(explicit, bool):
        return not explicit
    return action.get("ok") is False


class TaskStore:
    """SQLite + 留痕目录的只读投影。"""

    def __init__(self, db_path: Path | str | None = None, logs: Path | None = None) -> None:
        # 路径**延迟到构造时**解析，环境变量才能对测试与多实例生效
        self._db_path = Path(db_path) if db_path is not None else db_file()
        self._logs = logs if logs is not None else log_root()

    # -- 行 → 输出模型 ------------------------------------------------------ #
    def _rows(self, run_id: str | None, task_type: str | None, state: str | None) -> list[Any]:
        if not Path(self._db_path).exists():
            return []
        where: list[str] = []
        params: list[Any] = []
        if run_id:
            where.append("run_id = ?")
            params.append(run_id)
        if task_type:
            where.append("type = ?")
            params.append(task_type)
        if state:
            where.append("state = ?")
            params.append(state)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        sql = f"SELECT * FROM task_item{clause} ORDER BY created_at, item_id"
        with connect(self._db_path) as conn:
            return list(conn.execute(sql, params).fetchall())

    @staticmethod
    def _episode_meta(rows: list[Any], item_id: str) -> tuple[int | None, int | None]:
        """分集序号靠 run 内 video 条目的顺序推导 —— 表结构里没有这一列。"""
        videos = [row for row in rows if row["type"] == "video"]
        total = len(videos) or None
        for position, row in enumerate(videos, start=1):
            if row["item_id"] == item_id:
                return position, total
        return None, total

    def _title(self, run_id: str, item_id: str) -> str | None:
        perception = _load_json(self._logs / run_id / item_id / "perception.json")
        if not perception:
            return None
        question = perception.get("question") or {}
        stem = question.get("stem")
        if isinstance(stem, str) and stem:
            return stem[:60]
        video = perception.get("video_state") or {}
        index = video.get("episode_index")
        return f"第 {index} 集" if index else None

    def _to_out(self, row: Any, index: int | None, total: int | None) -> TaskItemOut:
        run_id = row["run_id"]
        return TaskItemOut(
            item_id=row["item_id"],
            type=row["type"],
            qid=row["qid"],
            vid=row["vid"],
            state=row["state"],
            attempts=int(row["attempts"] or 0),
            suspended=bool(row["suspended"]),
            title=self._title(run_id, row["item_id"]),
            episode_index=index,
            episode_total=total,
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )

    # -- 列表与详情 --------------------------------------------------------- #
    def list_items(
        self,
        run_id: str | None = None,
        task_type: str | None = None,
        state: str | None = None,
    ) -> list[TaskItemOut]:
        rows = self._rows(run_id, task_type, state)
        return [
            self._to_out(row, *self._episode_meta(rows, row["item_id"])) for row in rows
        ]

    def get(self, item_id: str) -> TaskItemOut | None:
        if not Path(self._db_path).exists():
            return None
        with connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT * FROM task_item WHERE item_id = ?", (item_id,)
            ).fetchone()
            if row is None:
                return None
            siblings = list(
                conn.execute(
                    "SELECT * FROM task_item WHERE run_id = ? ORDER BY created_at, item_id",
                    (row["run_id"],),
                ).fetchall()
            )
        return self._to_out(row, *self._episode_meta(siblings, item_id))

    def item_dir(self, run_id: str, item_id: str) -> Path:
        return self._logs / run_id / item_id

    def artifacts(self, run_id: str, item_id: str) -> list[str]:
        directory = self.item_dir(run_id, item_id)
        if not directory.is_dir():
            return []
        return sorted(
            name for name in ARTIFACT_FILES if (directory / name).is_file()
        )

    # -- 任务级（``run`` = 主页面上的「一条任务」） --------------------------- #
    def list_tasks(
        self,
        *,
        model_names: dict[str, str] | None = None,
        active_run_id: str | None = None,
        running: bool = False,
        limit: int | None = None,
    ) -> list[TaskOut]:
        """任务列表，新的在前。**每次都从库里现读** —— 任务可能在另一个进程里跑。"""
        if not Path(self._db_path).exists():
            return []
        from core import db as core_db

        with connect(self._db_path) as conn:
            runs = core_db.list_runs(conn, limit=limit)
        return [
            self._to_task(
                run, model_names=model_names, active_run_id=active_run_id, running=running
            )
            for run in runs
        ]

    def task(
        self,
        run_id: str,
        *,
        model_names: dict[str, str] | None = None,
        active_run_id: str | None = None,
        running: bool = False,
    ) -> TaskOut | None:
        if not Path(self._db_path).exists():
            return None
        from core import db as core_db

        with connect(self._db_path) as conn:
            run = core_db.get_run(conn, run_id)
        if run is None:
            return None
        return self._to_task(
            run, model_names=model_names, active_run_id=active_run_id, running=running
        )

    def task_detail(
        self,
        run_id: str,
        *,
        model_names: dict[str, str] | None = None,
        active_run_id: str | None = None,
        running: bool = False,
    ) -> TaskDetailOut | None:
        """任务详情：任务本身 + 它下面的条目（题目 / 分集）。"""
        task = self.task(
            run_id, model_names=model_names, active_run_id=active_run_id, running=running
        )
        if task is None:
            return None
        return TaskDetailOut(
            task=task,
            items=self.list_items(run_id=run_id),
            by_state=self.progress(run_id)["by_state"],
            stack_depth=self.stack_depth(run_id),
            skill_diagnostics=self._skill_diagnostics(run_id),
        )

    def _skill_diagnostics(self, run_id: str) -> list[dict[str, Any]]:
        """从逐题视觉留痕汇总最近的技能映射结果，详情页可直接排障。"""
        rows: list[dict[str, Any]] = []
        items = self.list_items(run_id=run_id, task_type="quiz")
        for item in items:
            directory = self.item_dir(run_id, item.item_id)
            payload = _load_json(directory / "vision_read.json") or {}
            read = payload.get("read") if isinstance(payload, dict) else None
            if not isinstance(read, dict):
                continue
            reported = read.get("reported_skill_id")
            resolved = read.get("skill_id")
            error = read.get("skill_error")
            if error is None and reported == resolved:
                continue
            if error is None and resolved is not None:
                # 模型省略 skill_id 且本地按 qtype 成功补全，不视为异常诊断。
                continue
            rows.append(
                {
                    "item_id": item.item_id,
                    "title": read.get("stem") or item.title,
                    "state": item.state,
                    "qtype": read.get("qtype"),
                    "reported_qtype": read.get("reported_qtype"),
                    "reported_skill_id": reported,
                    "skill_id": resolved,
                    "skill_error": error,
                }
            )
        return rows[-20:]

    def delete_task(self, run_id: str) -> bool:
        """删任务：库里的行 + 它的**全部缓存**（截图 / 逐题留痕 / 操作日志 / 事件流）。

        **缓存一并删**是刻意的 —— 只删库会让 ``logs/`` 里留下永远没人认领的
        孤儿目录，久而久之没人敢清。

        缓存目录**只删这一个 run 自己的**（``core.trace.purge_run_cache`` 会把
        路径校验在 ``logs/`` 之下），它同时删掉 run 级的 ``events.jsonl``
        （那就是这个任务的「操作日志」）。详细边界见 ``core.trace.run_dir`` 的说明。
        """
        if not Path(self._db_path).exists():
            return False
        from core import db as core_db

        with connect(self._db_path) as conn:
            if core_db.get_run(conn, run_id) is None:
                return False
            # 先删库（成功才动磁盘）—— 反过来会出现「库还在、缓存没了」，
            # 界面上一堆打不开的截图链接。
            core_db.delete_run(conn, run_id)
        purge_run_cache(self._logs, run_id)
        return True

    def run_cache(self, run_id: str) -> dict[str, Any]:
        """这个任务的缓存**长什么样**（只读预览，删之前看得见）。

        ``entries`` 是缓存目录下的条目（子目录是逐题留痕，``events.jsonl`` 是操作日志），
        ``bytes`` 是它们加起来的体积。删任务会把 ``directory`` 整个删掉。
        """
        directory = run_dir(self._logs, run_id)
        entries = run_cache_entries(self._logs, run_id)
        size = 0
        if directory.is_dir():
            try:
                size = sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())
            except OSError:  # pragma: no cover - 目录正被写
                size = 0
        return {
            "run_id": run_id,
            "directory": str(directory),
            "exists": directory.is_dir(),
            "entries": entries,
            "bytes": size,
        }

    def _to_task(
        self,
        run: dict[str, Any],
        *,
        model_names: dict[str, str] | None,
        active_run_id: str | None,
        running: bool,
    ) -> TaskOut:
        run_id = str(run["run_id"])
        active = bool(running) and run_id == active_run_id
        key, label = display_status(str(run["status"]), active=active)
        progress = self.progress(run_id)
        by_state = progress["by_state"]
        config = run.get("config") or {}
        names = model_names or {}

        def model_name(profile_id: Any) -> str | None:
            if not profile_id:
                return None
            return names.get(str(profile_id), str(profile_id))

        return TaskOut(
            run_id=run_id,
            name=str(run["name"] or f"任务·{run_id[:6]}"),
            status=key,
            status_label=label,
            status_raw=str(run["status"]),
            active=active,
            created_at=_parse_dt(run["started_at"]),
            finished_at=_parse_dt(run["finished_at"]) if run.get("finished_at") else None,
            total=int(progress["total"]),
            done=int(progress["done"]),
            failed=int(by_state.get(QuestionState.FAILED.value, 0)),
            pending_confirm=int(by_state.get(QuestionState.PENDING_CONFIRM.value, 0)),
            current_item_id=progress["current_item_id"],
            target_kind=TargetKind(config.get("target_kind") or TargetKind.BROWSER_PAGE),
            target_id=config.get("target_id"),
            task_sequence=[TaskType(item) for item in (config.get("task_sequence") or [])],
            auto_apply=bool(config.get("auto_apply")),
            judge_model=model_name(config.get("model_profile_id")),
            vision_model=model_name(config.get("vision_profile_id")),
        )

    def detail(self, item_id: str) -> ItemDetailOut | None:
        """**单题/单集详情**（旧名保留为别名，见 :meth:`item_detail`）。"""
        return self.item_detail(item_id)

    def item_detail(self, item_id: str) -> ItemDetailOut | None:
        """单题/单集详情：截图 URL、采样明细、``level_used``。"""
        item = self.get(item_id)
        if item is None:
            return None

        run_id = self._run_id_of(item_id)
        directory = self.item_dir(run_id, item_id) if run_id else Path()
        perception = _load_json(directory / "perception.json") or {}
        solve = _load_json(directory / "solve.json") or {}
        action = _load_json(directory / "action.json") or {}

        question = perception.get("question") or {}
        samples = [
            SampleRecord.model_validate(entry)
            for entry in (solve.get("samples") or [])
            if isinstance(entry, dict)
        ]

        def shot_url(name: str) -> str | None:
            return (
                f"/api/items/{item_id}/artifacts/{name}"
                if (directory / name).is_file()
                else None
            )

        return ItemDetailOut(
            item=item,
            stem=question.get("stem") or item.title,
            qtype=question.get("qtype"),
            options=[opt.get("text", "") for opt in (question.get("options") or [])],
            chosen_labels=list(solve.get("chosen_labels") or []),
            confidence=solve.get("confidence"),
            solve_path=solve.get("solve_path"),
            review_flag=bool(solve.get("review_flag")),
            samples=samples,
            level_used=action.get("level_used"),
            # 判据是结构化字段（理由见 :func:`readback_mismatch`），不做文本比较
            readback_mismatch=readback_mismatch(action),
            before_screenshot_url=shot_url("before.png"),
            after_screenshot_url=shot_url("after.png"),
            # submitted 是唯一危险态：界面据此置灰确认按钮，后端同时返回 409
            is_danger_state=item.state == QuestionState.SUBMITTED.value,
        )

    def _run_id_of(self, item_id: str) -> str | None:
        if not Path(self._db_path).exists():
            return None
        with connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT run_id FROM task_item WHERE item_id = ?", (item_id,)
            ).fetchone()
        return str(row["run_id"]) if row else None

    def run_id_of(self, item_id: str) -> str | None:
        """该条目所属的 run_id。取留痕目录与截图 URL 都要用。"""
        return self._run_id_of(item_id)

    def set_state(self, item_id: str, state: QuestionState) -> None:
        """人工确认 / 否决写回。

        **本模块唯一的写操作。** 机器人的状态推进全归编排层（P7）——
        这里只处理「用户点了按钮」这一类决策。合法性由调用方先用
        ``core.states.require_transition`` 校验。
        """
        if not Path(self._db_path).exists():
            raise KeyError(item_id)
        with connect(self._db_path) as conn:
            cursor = conn.execute(
                "UPDATE task_item SET state = ?, updated_at = ? WHERE item_id = ?",
                (state.value, datetime.now(UTC).isoformat(), item_id),
            )
            conn.commit()
            if cursor.rowcount == 0:
                raise KeyError(item_id)

    # -- 进度 --------------------------------------------------------------- #
    def latest_run_id(self) -> str | None:
        """最近一次运行的 run_id。页面刷新后没有内存态时用它兜底。"""
        if not Path(self._db_path).exists():
            return None
        with connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT run_id FROM run ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        return str(row["run_id"]) if row else None

    def stack_depth(self, run_id: str | None = None) -> int:
        """挂起栈深度（弹题压了几层）。

        M5-6 要求界面上「弹题状态」可见，而栈深是**重连后唯一无法从 SSE 补回**
        的东西（`stack.pushed` / `stack.popped` 是增量事件）。所以它跟进度一样
        走只读投影：对账时读一次库，之后再由事件增量维护。
        """
        if not Path(self._db_path).exists():
            return 0
        with connect(self._db_path) as conn:
            if run_id:
                row = conn.execute(
                    "SELECT COUNT(1) AS c FROM suspend_frame WHERE run_id = ?", (run_id,)
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(1) AS c FROM suspend_frame").fetchone()
        return int(row["c"]) if row else 0

    def progress(self, run_id: str | None = None) -> dict[str, Any]:
        """``{total, done, by_state, current_item_id, stack_depth}``。

        SSE 重连后的对账依据。
        """
        rows = self._rows(run_id, None, None)
        by_state: dict[str, int] = {}
        current: str | None = None
        for row in rows:
            by_state[row["state"]] = by_state.get(row["state"], 0) + 1
            if current is None and row["state"] not in DONE_STATES:
                current = str(row["item_id"])
        return {
            "total": len(rows),
            "done": sum(count for state, count in by_state.items() if state in DONE_STATES),
            "by_state": by_state,
            "current_item_id": current,
            "run_id": run_id,
            "stack_depth": self.stack_depth(run_id),
        }
