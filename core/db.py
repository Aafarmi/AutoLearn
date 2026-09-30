"""SQLite 连接、建表与编排层读写（P0 冻结表结构，P7 落实现）。

六张表覆盖：运行元信息 / 任务队列 / 作答 / 挂起栈 / 降级统计 / 媒体断点。
表结构在 P0 冻结；P7 只补编排层的读写实现，**不得改动列名**。

读写边界
--------
本模块是**编排层唯一的持久化出口**：界面侧的投影在 ``ui/store.py``（它只读，
外加一个人工确认的 ``set_state``），执行层不碰库。两边共用同一份表结构，
谁都不许自己拼 SQL 建表。

三处刻意的设计
--------------
1. **``task_item.state`` 一个列装两种状态机**（题目九态 / 媒体六态），
   靠 ``type`` 列区分。读回来时按 ``type`` 还原成对应的枚举 —— 混着放会
   让 ``require_transition`` 拿到一个语义错误的枚举。
2. **时间一律 ISO-8601 字符串**（``datetime.now(UTC).isoformat()``），
   排序即时间序，且不依赖 SQLite 的时间函数。
3. **``upsert`` 而不是 ``insert``**：续跑会对同一 ``item_id`` 反复写，
   用 ``INSERT OR REPLACE`` 会连带清掉别的列（比如 ``suspended``）。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from core.enums import MediaState, QuestionState, SolvePath, TaskType
from core.models import Answer, TaskItem, VideoState
from core.tasks import SuspendFrame

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_DB_PATH",
    "SCHEMA_SQL",
    "DbTaskStackStore",
    "RowLike",
    "connect",
    "create_run",
    "delete_run",
    "finish_run",
    "get_run",
    "init_db",
    "list_runs",
    "load_answer",
    "load_level_stats",
    "load_media_position",
    "load_suspend_frames",
    "load_task_items",
    "make_task_stack_store",
    "next_task_name",
    "record_level_stat",
    "rename_run",
    "save_answer",
    "save_media_position",
    "save_suspend_frames",
    "set_run_status",
    "upsert_run",
    "upsert_task_item",
    "utcnow_iso",
]

#: 默认库文件落位
DEFAULT_DB_PATH = Path("state/autolearn.db")

#: 运行级信箱的键名：**「这是不是最后一题」的答案**（P14）。
#:
#: 放在这里而不是两边各写一份字符串字面量：界面写、编排层读，键名必须一致，
#: 写成两个常量迟早漂移 —— 而漂移的症状是「点了按钮毫无反应」，最难查的那种。

#: 六张表的建表语句。全部 ``IF NOT EXISTS``，可反复执行。
#:
#: ``run`` 表就是**任务表**（一个 run = 用户界面上的一条「任务」）：
#: ``name`` 是用户可见的任务名（默认「任务1」），``task_item`` 才是它下面的题目 /
#: 分集条目。新库直接带上 ``name``；老库由 :func:`_migrate` 补列。
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS run (
    run_id        TEXT PRIMARY KEY,
    name          TEXT,                 -- 任务名（用户可见，默认「任务N」）
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL,
    config_json   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_item (
    item_id     TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    type        TEXT NOT NULL,          -- video | quiz
    qid         TEXT,
    vid         TEXT,
    state       TEXT NOT NULL,          -- QuestionState | MediaState
    attempts    INTEGER NOT NULL DEFAULT 0,
    suspended   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_item_run ON task_item (run_id, state);
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_item_qid ON task_item (run_id, qid)
    WHERE qid IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_item_vid ON task_item (run_id, vid)
    WHERE vid IS NOT NULL;

CREATE TABLE IF NOT EXISTS answer (
    qid                TEXT PRIMARY KEY,
    chosen_labels_json TEXT NOT NULL,
    confidence         REAL NOT NULL,
    tier_used          TEXT NOT NULL,
    review_flag        INTEGER NOT NULL DEFAULT 0,
    model_name         TEXT,
    stem_hash          TEXT                -- M3-7 执行前重校验
);

CREATE TABLE IF NOT EXISTS suspend_frame (
    frame_id          TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL,
    parent_item_id    TEXT NOT NULL,
    child_item_id     TEXT NOT NULL,
    media_state_json  TEXT NOT NULL,
    reason            TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL
);
-- 栈顶优先：按 created_at 升序读出，取末条即栈顶
CREATE INDEX IF NOT EXISTS idx_suspend_frame_run ON suspend_frame (run_id, created_at);

CREATE TABLE IF NOT EXISTS level_stat (
    item_id     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    level_used  TEXT NOT NULL,
    ok          INTEGER NOT NULL DEFAULT 0,
    elapsed_ms  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (item_id, kind)
);

CREATE TABLE IF NOT EXISTS media_position (
    vid            TEXT NOT NULL,
    episode_index  INTEGER NOT NULL,
    last_position  REAL NOT NULL DEFAULT 0.0,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (vid, episode_index)
);
"""


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """打开库连接。父目录不存在时自动创建。"""
    path = Path(db_path)
    if path.parent != Path():
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """建表并返回连接。幂等，可重复调用。"""
    conn = connect(db_path)
    conn.executescript(SCHEMA_SQL)
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """把老库补齐到当前表结构。

    ``CREATE TABLE IF NOT EXISTS`` **不会**给已存在的表加列，所以新列必须自己补。
    只做「加列」这类向后兼容的迁移 —— 改列名 / 改类型一律不做（P0 冻结契约）。
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(run)")}
    if "name" not in columns:
        conn.execute("ALTER TABLE run ADD COLUMN name TEXT")
        logger.info("已为 run 表补上 name 列（任务名）")


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
#: 一行查询结果（``connect()`` 已把 ``row_factory`` 设成 ``sqlite3.Row``）
RowLike = sqlite3.Row


def utcnow_iso() -> str:
    """统一的时间戳写法。**全模块只用这一个**，便于排序与比对。"""
    return datetime.now(UTC).isoformat()


def _parse_dt(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.now(UTC)


def _state_for(row_type: str, raw: str) -> QuestionState | MediaState:
    """按任务类型把 ``state`` 列还原成正确的枚举。"""
    if row_type == TaskType.VIDEO.value:
        return MediaState(raw)
    return QuestionState(raw)


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
def upsert_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    started_at: datetime,
    config: object,
    status: str = "running",
) -> None:
    """写入 / 更新一次运行的元信息。

    ``config`` 走 ``model_dump(mode="json")``（``RunConfig`` 是 pydantic 模型），
    **不存密钥** —— 密钥从来只在 ``CredentialStore`` 里。
    """
    payload = (
        json.dumps(config.model_dump(mode="json"), ensure_ascii=False)
        if hasattr(config, "model_dump")
        else json.dumps(config, ensure_ascii=False, default=str)
    )
    conn.execute(
        """
        INSERT INTO run (run_id, started_at, finished_at, status, config_json)
        VALUES (?, ?, NULL, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET status = excluded.status,
                                          config_json = excluded.config_json
        """,
        (run_id, started_at.isoformat(), status, payload),
    )
    conn.commit()


def finish_run(conn: sqlite3.Connection, run_id: str, status: str = "finished") -> None:
    """收尾。``finished_at`` 只在第一次收尾时写，续跑不会把它刷成新时间。"""
    conn.execute(
        """
        UPDATE run SET status = ?, finished_at = COALESCE(finished_at, ?)
        WHERE run_id = ?
        """,
        (status, utcnow_iso(), run_id),
    )
    conn.commit()


def set_run_status(conn: sqlite3.Connection, run_id: str, status: str) -> None:
    """只改状态（启动 / 出错都用它）。**不碰 finished_at** —— 那是收尾的语义。"""
    conn.execute("UPDATE run SET status = ? WHERE run_id = ?", (status, run_id))
    conn.commit()


def create_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    name: str,
    config: object,
    started_at: datetime | None = None,
    status: str = "created",
) -> None:
    """新建一条任务（``run`` 行）。**此时还没跑**，只是把配置快照存下来。

    配置快照是刻意的：运行配置是跨会话的「草稿」，而任务一旦建出来就该
    **自带一份当时的配置** —— 否则用户改一次全局配置，历史任务显示的就全变了。
    """
    payload = (
        json.dumps(config.model_dump(mode="json"), ensure_ascii=False)
        if hasattr(config, "model_dump")
        else json.dumps(config, ensure_ascii=False, default=str)
    )
    conn.execute(
        """
        INSERT INTO run (run_id, name, started_at, finished_at, status, config_json)
        VALUES (?, ?, ?, NULL, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET name = excluded.name,
                                          config_json = excluded.config_json
        """,
        (run_id, name, (started_at or datetime.now(UTC)).isoformat(), status, payload),
    )
    conn.commit()


def rename_run(conn: sqlite3.Connection, run_id: str, name: str) -> None:
    conn.execute("UPDATE run SET name = ? WHERE run_id = ?", (name, run_id))
    conn.commit()


def delete_run(conn: sqlite3.Connection, run_id: str) -> None:
    """删任务：``run`` + 它的全部 ``task_item`` + 该运行的降级统计。

    ``level_stat`` 按 ``item_id`` 存，而 ``item_id`` 形如 ``{run_id}-{qid}`` ——
    它是**这个运行专属**的数据，留着只会变成永远没人认领的孤儿行。
    ``answer`` 表按 ``qid`` **全局**存（跨运行共享同一道题的作答），所以**不动它** ——
    那是这道题的事实，不属于某一次运行。
    """
    conn.execute(
        "DELETE FROM level_stat WHERE item_id IN "
        "(SELECT item_id FROM task_item WHERE run_id = ?)",
        (run_id,),
    )
    conn.execute("DELETE FROM task_item WHERE run_id = ?", (run_id,))
    conn.execute("DELETE FROM suspend_frame WHERE run_id = ?", (run_id,))
    conn.execute("DELETE FROM run WHERE run_id = ?", (run_id,))
    conn.commit()


def get_run(conn: sqlite3.Connection, run_id: str) -> dict[str, object] | None:
    row = conn.execute("SELECT * FROM run WHERE run_id = ?", (run_id,)).fetchone()
    return _run_row_to_dict(row) if row is not None else None


def list_runs(conn: sqlite3.Connection, *, limit: int | None = None) -> list[dict[str, object]]:
    """任务列表，**新的在前**。"""
    sql = "SELECT * FROM run ORDER BY started_at DESC, run_id DESC"
    params: tuple[object, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    return [_run_row_to_dict(row) for row in conn.execute(sql, params)]


def next_task_name(conn: sqlite3.Connection) -> str:
    """下一个默认任务名：``任务N``，N = 已有名字里最大的编号 + 1。

    认不出编号的名字（用户自己改过的）不参与计算，但仍然占位 —— 所以
    「任务1、我的语文作业」之后再建是「任务2」。
    """
    highest = 0
    for row in conn.execute("SELECT name FROM run WHERE name IS NOT NULL"):
        match = re.fullmatch(r"任务\s*(\d+)", str(row["name"]).strip())
        if match:
            highest = max(highest, int(match.group(1)))
    return f"任务{highest + 1}"


def _run_row_to_dict(row: sqlite3.Row) -> dict[str, object]:
    keys = row.keys()
    config_raw = row["config_json"]
    try:
        config = json.loads(config_raw) if config_raw else {}
    except json.JSONDecodeError:  # pragma: no cover - 库被手工改坏
        config = {}
    return {
        "run_id": row["run_id"],
        "name": row["name"] if "name" in keys else None,
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "status": row["status"],
        "config": config,
    }


# --------------------------------------------------------------------------- #
# task_item
# --------------------------------------------------------------------------- #
def upsert_task_item(conn: sqlite3.Connection, run_id: str, item: TaskItem) -> None:
    """写入 / 更新一条任务。**按列更新，不用 REPLACE**（见模块文档第 3 条）。"""
    conn.execute(
        """
        INSERT INTO task_item
            (item_id, run_id, type, qid, vid, state, attempts, suspended, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(item_id) DO UPDATE SET
            state = excluded.state,
            attempts = excluded.attempts,
            suspended = excluded.suspended,
            qid = excluded.qid,
            vid = excluded.vid,
            updated_at = excluded.updated_at
        """,
        (
            item.item_id,
            run_id,
            str(item.type),
            item.qid,
            item.vid,
            str(item.state),
            item.attempts,
            int(item.suspended),
            item.created_at.isoformat(),
            item.updated_at.isoformat(),
        ),
    )
    conn.commit()


def load_task_items(conn: sqlite3.Connection, run_id: str) -> list[TaskItem]:
    """按创建顺序读回一次运行的全部任务（续跑的入口）。"""
    rows = conn.execute(
        "SELECT * FROM task_item WHERE run_id = ? ORDER BY created_at, item_id",
        (run_id,),
    ).fetchall()
    return [
        TaskItem(
            item_id=row["item_id"],
            type=TaskType(row["type"]),
            qid=row["qid"],
            vid=row["vid"],
            state=_state_for(row["type"], row["state"]),
            attempts=int(row["attempts"] or 0),
            suspended=bool(row["suspended"]),
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )
        for row in rows
    ]


# --------------------------------------------------------------------------- #
# answer
# --------------------------------------------------------------------------- #
def save_answer(conn: sqlite3.Connection, answer: Answer) -> None:
    """落一条作答。同 ``qid`` 覆盖 —— 复算结果以最新一次为准。"""
    conn.execute(
        """
        INSERT INTO answer
            (qid, chosen_labels_json, confidence, tier_used, review_flag, model_name, stem_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(qid) DO UPDATE SET
            chosen_labels_json = excluded.chosen_labels_json,
            confidence = excluded.confidence,
            tier_used = excluded.tier_used,
            review_flag = excluded.review_flag,
            model_name = excluded.model_name,
            stem_hash = excluded.stem_hash
        """,
        (
            answer.qid,
            json.dumps(answer.chosen_labels, ensure_ascii=False),
            answer.confidence,
            str(answer.solve_path),
            int(answer.review_flag),
            answer.model_name,
            answer.stem_hash,
        ),
    )
    conn.commit()


def load_answer(conn: sqlite3.Connection, qid: str) -> dict[str, object] | None:
    """读一条作答的**原始记录**。

    返回 dict 而不是 :class:`Answer`：表里没有存 ``chosen_texts``（它可以从
    ``chosen_labels`` + 题干还原），硬凑一个 ``Answer`` 会伪造出一个
    「正文为空」的答案，比没有更危险。
    """
    row = conn.execute("SELECT * FROM answer WHERE qid = ?", (qid,)).fetchone()
    if row is None:
        return None
    labels = json.loads(row["chosen_labels_json"] or "[]")
    return {
        "qid": row["qid"],
        "chosen_labels": list(labels),
        "confidence": float(row["confidence"]),
        # 列名仍是 ``tier_used``（表结构冻结），语义已收敛成 :class:`SolvePath`。
        # 老库里存着的 ``tier1`` / ``tier2`` 由 ``SolvePath.parse`` 兼容读回 ``single``。
        "solve_path": SolvePath.parse(row["tier_used"]),
        "review_flag": bool(row["review_flag"]),
        "model_name": row["model_name"],
        "stem_hash": row["stem_hash"],
    }


# --------------------------------------------------------------------------- #
# 挂起栈（P8 消费，表结构 P0 冻结）
# --------------------------------------------------------------------------- #
class DbTaskStackStore:
    """:class:`core.tasks.TaskStack` 的 SQLite 持久化后端（P8 / M5-2）。

    实现 ``core.tasks.TaskStackStore`` 协议，直接落到上面那张 ``suspend_frame``
    表。**放在本模块而不是 ``core/tasks.py``**：``tasks`` 必须保持零依赖
    （``config → tasks → models → enums`` 这条链不能倒过来），而本模块本来就
    依赖 ``tasks`` 的 ``SuspendFrame``。

    每次压 / 弹栈都是**整表覆盖式**落盘（``save_suspend_frames`` 先 DELETE 再
    INSERT）。栈深度通常只有 1~2 层，覆盖式比增量同步简单得多，也不会出现
    「内存与库漂移」那种只在重启时才暴露的问题。
    """

    def __init__(self, conn: sqlite3.Connection, run_id: str) -> None:
        self._conn = conn
        self._run_id = run_id

    def save(self, frames: list[SuspendFrame]) -> None:
        save_suspend_frames(self._conn, self._run_id, frames)

    def load(self) -> list[SuspendFrame]:
        return load_suspend_frames(self._conn, self._run_id)


def make_task_stack_store(conn: sqlite3.Connection, run_id: str) -> DbTaskStackStore:
    """给一次运行造一个挂起栈持久化后端。"""
    return DbTaskStackStore(conn, run_id)


def save_suspend_frames(
    conn: sqlite3.Connection,
    run_id: str,
    frames: list[SuspendFrame],
) -> None:
    """整体覆盖式落盘。**列表尾即栈顶**，读回来顺序必须一致。"""
    conn.execute("DELETE FROM suspend_frame WHERE run_id = ?", (run_id,))
    conn.executemany(
        """
        INSERT INTO suspend_frame
            (frame_id, run_id, parent_item_id, child_item_id, media_state_json, reason, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                frame.frame_id,
                run_id,
                frame.parent_item_id,
                frame.child_item_id,
                frame.media_state_at_suspend.model_dump_json(),
                frame.reason,
                frame.created_at.isoformat(),
            )
            for frame in frames
        ],
    )
    conn.commit()


def load_suspend_frames(conn: sqlite3.Connection, run_id: str) -> list[SuspendFrame]:
    """按 ``created_at`` 升序读回（**末条即栈顶**）。"""
    rows = conn.execute(
        "SELECT * FROM suspend_frame WHERE run_id = ? ORDER BY created_at, frame_id",
        (run_id,),
    ).fetchall()
    return [
        SuspendFrame(
            frame_id=row["frame_id"],
            parent_item_id=row["parent_item_id"],
            child_item_id=row["child_item_id"],
            media_state_at_suspend=VideoState.model_validate_json(row["media_state_json"]),
            reason=row["reason"] or "",
            created_at=_parse_dt(row["created_at"]),
        )
        for row in rows
    ]


# --------------------------------------------------------------------------- #
# 降级统计（热力图数据源）
# --------------------------------------------------------------------------- #
def record_level_stat(
    conn: sqlite3.Connection,
    *,
    item_id: str,
    kind: str,
    level_used: str,
    ok: bool,
    elapsed_ms: int = 0,
) -> None:
    """记一条动作的降级落点。主键是 ``(item_id, kind)``，重复动作取最新一次。"""
    conn.execute(
        """
        INSERT INTO level_stat (item_id, kind, level_used, ok, elapsed_ms)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(item_id, kind) DO UPDATE SET
            level_used = excluded.level_used,
            ok = excluded.ok,
            elapsed_ms = excluded.elapsed_ms
        """,
        (item_id, kind, level_used, int(ok), elapsed_ms),
    )
    conn.commit()


def load_level_stats(
    conn: sqlite3.Connection,
    run_id: str | None = None,
) -> list[dict[str, object]]:
    """读降级统计。传 ``run_id`` 时按该次运行的任务过滤。"""
    sql = "SELECT * FROM level_stat"
    params: tuple[object, ...] = ()
    if run_id is not None:
        sql = (
            "SELECT s.* FROM level_stat s "
            "JOIN task_item t ON t.item_id = s.item_id WHERE t.run_id = ?"
        )
        params = (run_id,)
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "item_id": row["item_id"],
            "kind": row["kind"],
            "level_used": row["level_used"],
            "ok": bool(row["ok"]),
            "elapsed_ms": int(row["elapsed_ms"] or 0),
        }
        for row in rows
    ]


# --------------------------------------------------------------------------- #
# 媒体断点
# --------------------------------------------------------------------------- #
def save_media_position(
    conn: sqlite3.Connection,
    *,
    vid: str,
    episode_index: int,
    last_position: float,
) -> None:
    conn.execute(
        """
        INSERT INTO media_position (vid, episode_index, last_position, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(vid, episode_index) DO UPDATE SET
            last_position = excluded.last_position,
            updated_at = excluded.updated_at
        """,
        (vid, episode_index, float(last_position), utcnow_iso()),
    )
    conn.commit()


def load_media_position(
    conn: sqlite3.Connection,
    vid: str,
    episode_index: int = 0,
) -> float | None:
    row = conn.execute(
        "SELECT last_position FROM media_position WHERE vid = ? AND episode_index = ?",
        (vid, episode_index),
    ).fetchone()
    return float(row["last_position"]) if row is not None else None
