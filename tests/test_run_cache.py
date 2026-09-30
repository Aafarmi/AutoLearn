"""任务缓存：**每个任务一个专属目录**，删任务级联清掉它。

用户问「删了这个任务，到底会删掉什么」—— 答案是 ``logs/<run_id>/`` 这个目录里的
**截图 + 逐题留痕 + 操作日志（events.jsonl）**。这几条用例把三件事钉死：

1. 缓存目录的**唯一定义点**是 ``core.trace.run_dir()``，路径始终在 ``logs/`` 之下；
2. 删任务会**级联**清掉该目录（`DELETE /api/tasks/{run_id}`），且不碰别的运行；
3. 有一个**只读预览**接口（`GET /api/tasks/{run_id}/cache`）让人删之前看得见。
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from core import db
from core.trace import purge_run_cache, run_cache_entries, run_dir
from ui.store import db_file, log_root


def _write_cache(root: Path, run_id: str) -> Path:
    """往某个运行的缓存目录里塞一组典型的文件（截图 + 留痕 + 事件流）。"""
    directory = run_dir(root, run_id)
    (directory / "events.jsonl").parent.mkdir(parents=True, exist_ok=True)
    (directory / "events.jsonl").write_text(
        json.dumps({"event": "run.started", "payload": {}}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (directory / "item-x" / "before.png").parent.mkdir(parents=True, exist_ok=True)
    (directory / "item-x" / "before.png").write_bytes(b"\x89PNG-fake")
    (directory / "item-x" / "solve.json").write_text('{"solve_path": "single"}', encoding="utf-8")
    return directory


def test_run_dir_is_scoped_under_root() -> None:
    root = Path("logs")
    assert run_dir(root, "abc123") == Path("logs/abc123")
    # 路径穿越的 run_id 也要被收敛进 root 之内
    escaped = run_dir(root, "../../etc")
    assert escaped.parts[0] == "logs"
    assert ".." not in escaped.parts


def test_purge_run_cache_removes_only_that_run(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    _write_cache(root, "run-a")
    _write_cache(root, "run-b")

    ok, files = purge_run_cache(root, "run-a")

    assert ok is True
    assert files >= 3
    assert not run_dir(root, "run-a").exists(), "run-a 的缓存整个删掉"
    assert run_dir(root, "run-b").exists(), "别的运行一个字节都不碰"


def test_purge_missing_run_reports_false(tmp_path: Path) -> None:
    ok, files = purge_run_cache(tmp_path / "logs", "nope")
    assert ok is False and files == 0


def test_cache_entries_list_directories_and_files(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    _write_cache(root, "run-a")
    entries = run_cache_entries(root, "run-a")
    assert "events.jsonl" in entries
    assert "item-x" in entries


def _create_task(client: TestClient, *, name: str | None = None) -> str:
    response = client.post("/api/tasks", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["run_id"]


def test_delete_task_cascades_to_cache(client: TestClient, ui_paths: Path) -> None:
    run_id = _create_task(client, name="要删的任务")
    root = log_root()
    _write_cache(root, run_id)

    assert (root / run_id).is_dir(), "前置：缓存确实写进去了"

    response = client.delete(f"/api/tasks/{run_id}")
    assert response.status_code == 204

    assert not (root / run_id).exists(), "删任务必须连缓存一起删（截图 + 留痕 + 操作日志）"
    assert client.get(f"/api/tasks/{run_id}").status_code == 404


def test_cache_preview_endpoint_is_read_only(client: TestClient, ui_paths: Path) -> None:
    run_id = _create_task(client, name="有缓存的任务")
    root = log_root()
    _write_cache(root, run_id)

    payload = client.get(f"/api/tasks/{run_id}/cache").json()

    assert payload["run_id"] == run_id
    assert payload["exists"] is True
    assert "events.jsonl" in payload["entries"]
    assert payload["bytes"] > 0
    # 只读：预览之后缓存还在
    assert (root / run_id).exists()


def test_cache_preview_of_unknown_task_is_404(client: TestClient) -> None:
    assert client.get("/api/tasks/does-not-exist/cache").status_code == 404


def test_delete_task_also_removes_level_stat_rows(client: TestClient, ui_paths: Path) -> None:
    """降级统计按 ``item_id`` 存（含 run_id 前缀），是**这个运行专属**的，删任务应一并清掉。"""
    run_id = _create_task(client, name="带统计的任务")
    item_id = f"{run_id}-q1"
    conn = db.init_db(db_file())
    try:
        conn.execute(
            "INSERT INTO task_item (item_id, run_id, type, qid, state, attempts, suspended,"
            " created_at, updated_at) VALUES (?,?,?,?,?,0,0,?,?)",
            (item_id, run_id, "quiz", "q1", "verified", "2026-09-29T00:00:00+00:00",
             "2026-09-29T00:00:00+00:00"),
        )
        db.record_level_stat(conn, item_id=item_id, kind="select_option", level_used="l6_vision_xy", ok=True)
        conn.commit()
    finally:
        conn.close()

    assert client.delete(f"/api/tasks/{run_id}").status_code == 204

    conn = db.init_db(db_file())
    try:
        row = conn.execute("SELECT 1 FROM level_stat WHERE item_id = ?", (item_id,)).fetchone()
    finally:
        conn.close()
    assert row is None, "该运行的降级统计应随任务一并删除"
