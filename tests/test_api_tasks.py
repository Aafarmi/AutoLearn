"""P13 任务化接口验收（``/api/tasks`` = 任务，``/api/items`` = 条目）。

**全部是纯内存用例** —— 用 ``client`` 夹具（``tests/conftest.py`` 里把装配工厂换成
内存替身），不起靶场、不起浏览器。

为什么必须有这个文件
--------------------
P13 把「任务」这个词从条目手里收回到 run 手里，``/api/tasks`` 的语义**整个换了**：
以前它返回``条目``列表，现在返回``任务``列表。这种同名不同物的改动最容易出
「接口还是那个路径，行为已经变了，但没人卡住」的静默漂移 —— 所以这里把新契约钉死：

- 任务 CRUD（自动命名 / 改名 / 删除 / 404）；
- 任务快照：建任务时的配置**存进任务**，之后改全局配置不影响它；
- **显式 ``null`` 能清掉上次的选择**（``exclude_unset`` 那处修复，否则界面上写着
  「Mock」而任务快照里留着上次选的模型）；
- 启动前检查：桌面窗口目标、**一条模型配置都没有** → 400 + 错误码 + ``next_action``；
- 条目一族搬到 ``/api/items``，且 ``submitted`` 态确认一律 409。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.db import init_db
from ui import deps
from ui.store import db_file, log_root


def _seed_item(run_id: str, item_id: str, state: str) -> None:
    """直接往库里塞一条条目 —— 编排层是唯一会推进状态的，这里只做投影验收。"""
    conn = init_db(db_file())
    try:
        now = "2026-09-27T00:00:00+00:00"
        conn.execute(
            "INSERT INTO task_item (item_id, run_id, type, state, attempts, suspended,"
            " created_at, updated_at) VALUES (?,?,?,?,0,0,?,?)",
            (item_id, run_id, "quiz", state, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _create(client: TestClient, **payload: object) -> dict[str, object]:
    response = client.post("/api/tasks", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _add_model(client: TestClient) -> dict[str, object]:
    """塞一套模型配置 —— 启动前检查现在要求「至少有一条可用模型链」。

    少了它，用例拦到的会是「没有模型」而不是它真正想验的那一条，
    于是「断言过了但验的是别的东西」。
    """
    response = client.post(
        "/api/models",
        json={"name": "启动用", "base_url": "https://invalid.example.com/v1", "model": "m1"},
    )
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# 任务列表 / 建任务
# --------------------------------------------------------------------------- #
def test_a_fresh_install_has_no_tasks(client: TestClient) -> None:
    payload = client.get("/api/tasks").json()
    assert payload["tasks"] == []
    assert payload["active_run_id"] is None
    assert payload["running"] is False


def test_task_names_are_auto_numbered(client: TestClient) -> None:
    """留空任务名 → 「任务1」「任务2」。用户不需要先想名字再动手。"""
    first = _create(client)
    second = _create(client)
    assert first["name"] == "任务1"
    assert second["name"] == "任务2"


def test_auto_numbering_survives_a_deletion_in_the_middle(client: TestClient) -> None:
    """删掉「任务1」之后再建，应该叫「任务2」而不是又冒出一个「任务1」。

    名字是用户唯一的辨认依据，撞名会让任务列表没法用。
    """
    first = _create(client)
    _create(client)
    assert client.delete(f"/api/tasks/{first['run_id']}").status_code == 204
    assert _create(client)["name"] == "任务3"


def test_explicit_task_name_is_kept(client: TestClient) -> None:
    assert _create(client, name="语文作业")["name"] == "语文作业"


def test_a_new_task_starts_out_created_not_running(client: TestClient) -> None:
    """**建任务与起任务是两件事**：这样失败时能分清「配置不对」还是「跑起来炸了」。"""
    task = _create(client)
    assert task["status"] == "created"
    assert task["status_label"] == "待启动"
    assert task["status_raw"] == "created"
    assert task["active"] is False


def test_list_puts_the_newest_task_first(client: TestClient) -> None:
    first = _create(client)
    second = _create(client)
    tasks = client.get("/api/tasks").json()["tasks"]
    assert [t["run_id"] for t in tasks] == [second["run_id"], first["run_id"]]


def test_task_progress_starts_at_zero(client: TestClient) -> None:
    task = _create(client)
    assert (task["total"], task["done"], task["failed"], task["pending_confirm"]) == (0, 0, 0, 0)
    assert task["current_item_id"] is None


# --------------------------------------------------------------------------- #
# 配置快照：任务跑的是它自己那一套
# --------------------------------------------------------------------------- #
def test_config_is_snapshotted_into_the_task(client: TestClient) -> None:
    task = _create(
        client,
        config={
            "task_sequence": ["quiz", "video"],
            "auto_apply": True,
            "sample_n": 7,
        },
    )
    assert task["task_sequence"] == ["quiz", "video"]
    assert task["auto_apply"] is True


def test_two_model_choices_are_snapshotted_separately(client: TestClient) -> None:
    """判题与视觉识别**两次选择**都要落进任务，且显示的是模型库里的名字。"""
    judge = client.post(
        "/api/models",
        json={
            "name": "判题模型",
            "base_url": "https://a.example.com/v1",
            "model": "m1",
            "api_key": "sk-1",
        },
    ).json()
    vision = client.post(
        "/api/models",
        json={
            "name": "读图模型",
            "base_url": "https://b.example.com/v1",
            "model": "v1",
            "api_key": "sk-2",
        },
    ).json()

    task = _create(
        client,
        config={
            "task_sequence": ["quiz"],
            "model_profile_id": judge["profile_id"],
            "vision_profile_id": vision["profile_id"],
        },
    )
    assert task["judge_model"] == "判题模型"
    assert task["vision_model"] == "读图模型"


def test_vision_model_falls_back_to_the_judge_model_name(client: TestClient) -> None:
    """视觉留空 = 与判题共用，界面上要如实说「共用」而不是留一片空白。"""
    judge = client.post(
        "/api/models",
        json={
            "name": "判题模型",
            "base_url": "https://a.example.com/v1",
            "model": "m1",
            "api_key": "sk-1",
        },
    ).json()
    task = _create(
        client,
        config={"task_sequence": ["quiz"], "model_profile_id": judge["profile_id"]},
    )
    assert task["vision_model"] is None


def test_explicit_null_clears_the_previous_choice(client: TestClient) -> None:
    """**这是一处刻意的行为选择**，见 CHANGELOG §P13.5。

    ``POST /api/tasks`` 用 ``exclude_unset`` 合并配置补丁：界面上「两处模型都留空」
    是一个明确的决定，必须能覆盖草稿里以前选过的模型。用 ``exclude_none`` 的话，
    显式传进来的 ``null`` 会被丢掉 → 任务快照里悄悄留着上次的模型 ——
    **界面上写着「Mock」，跑起来却在调真实模型**。
    """
    model = client.post(
        "/api/models",
        json={
            "name": "上一次选的",
            "base_url": "https://a.example.com/v1",
            "model": "m1",
            "api_key": "sk-1",
        },
    ).json()

    first = _create(
        client,
        config={"task_sequence": ["quiz"], "model_profile_id": model["profile_id"]},
    )
    assert first["judge_model"] == "上一次选的"

    second = _create(client, config={"task_sequence": ["quiz"], "model_profile_id": None})
    assert second["judge_model"] is None, "显式 null 没清掉，就会跑出跟界面不一致的结果"


def test_omitted_fields_keep_the_draft_value(client: TestClient) -> None:
    """``exclude_unset`` 的另一半：**压根没传**的字段沿用草稿，不会被清空。"""
    _create(client, config={"task_sequence": ["quiz"], "sample_n": 9})
    task = _create(client, config={"task_sequence": ["quiz"]})
    assert task["auto_apply"] is False  # 草稿默认值仍在
    draft = client.get("/api/run/config").json()
    assert draft["sample_n"] == 9


# --------------------------------------------------------------------------- #
# 详情 / 改名 / 删除
# --------------------------------------------------------------------------- #
def test_detail_carries_items_and_state_counts(client: TestClient) -> None:
    task = _create(client)
    run_id = str(task["run_id"])
    _seed_item(run_id, f"{run_id}-q1", "verified")
    _seed_item(run_id, f"{run_id}-q2", "pending_confirm")

    detail = client.get(f"/api/tasks/{run_id}").json()
    assert detail["task"]["run_id"] == run_id
    assert len(detail["items"]) == 2
    assert detail["by_state"]["verified"] == 1
    assert detail["by_state"]["pending_confirm"] == 1
    assert detail["task"]["done"] == 1
    assert detail["task"]["pending_confirm"] == 1
    assert detail["stack_depth"] == 0
    assert detail["skill_diagnostics"] == []


def test_task_detail_includes_skill_mapping_diagnostics(ui_paths: Path, client: TestClient) -> None:
    task = _create(client, name="技能诊断")
    run_id = str(task["run_id"])
    item_id = f"{run_id}-q-skill"
    _seed_item(run_id, item_id, "skipped")
    evidence_dir = ui_paths / "logs" / run_id / item_id
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "vision_read.json").write_text(
        json.dumps(
            {
                "read": {
                    "stem": "测试题",
                    "qtype": "single",
                    "reported_qtype": "single",
                    "reported_skill_id": None,
                    "skill_id": "single_choice",
                    "skill_error": None,
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    detail = client.get(f"/api/tasks/{run_id}").json()
    assert detail["skill_diagnostics"] == [], "成功从 qtype 补全技能不是异常"

    (evidence_dir / "vision_read.json").write_text(
        json.dumps(
            {
                "read": {
                    "stem": "测试题",
                    "qtype": "single",
                    "reported_qtype": "single",
                    "reported_skill_id": "bad-skill",
                    "skill_id": None,
                    "skill_error": "unknown_skill_id",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    detail = client.get(f"/api/tasks/{run_id}").json()
    diagnostic = detail["skill_diagnostics"][0]
    assert diagnostic["reported_skill_id"] == "bad-skill"
    assert diagnostic["skill_error"] == "unknown_skill_id"
    assert diagnostic["title"] == "测试题"


def test_rename_task(client: TestClient) -> None:
    task = _create(client)
    response = client.patch(f"/api/tasks/{task['run_id']}", json={"name": "改名后的任务"})
    assert response.status_code == 200
    assert response.json()["name"] == "改名后的任务"
    assert client.get(f"/api/tasks/{task['run_id']}").json()["task"]["name"] == "改名后的任务"


def test_rename_refuses_a_blank_name(client: TestClient) -> None:
    task = _create(client)
    assert client.patch(f"/api/tasks/{task['run_id']}", json={"name": ""}).status_code == 422


def test_delete_task_removes_its_artifacts_too(client: TestClient) -> None:
    """只删库会让 ``logs/`` 里留下永远没人认领的孤儿目录，久而久之没人敢清。"""
    task = _create(client)
    run_id = str(task["run_id"])
    artifact_dir = log_root() / run_id / f"{run_id}-q1"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "before.png").write_bytes(b"png")

    assert client.delete(f"/api/tasks/{run_id}").status_code == 204
    assert client.get(f"/api/tasks/{run_id}").status_code == 404
    assert not (log_root() / run_id).exists()


def test_unknown_task_is_404(client: TestClient) -> None:
    assert client.get("/api/tasks/nope").status_code == 404
    assert client.patch("/api/tasks/nope", json={"name": "x"}).status_code == 404
    assert client.delete("/api/tasks/nope").status_code == 404
    assert client.post("/api/tasks/nope/start").status_code == 404


# --------------------------------------------------------------------------- #
# 启动前检查：能拦的都拦在这里，别返回一个 run_id 再在后台默默失败
# --------------------------------------------------------------------------- #
def test_desktop_window_target_is_refused_at_start(client: TestClient) -> None:
    """窗口枚举 / 截图 / 系统级点击都已可用，但**识别链路尚未接入**，启动必然跑空。

    明确拒绝 + 说明原因，比让它跑起来再报一个离原因十万八千里的错要好。
    先配好模型，是为了证明拦下它的确实是**目标**本身，而不是「没有模型」那条 ——
    顺序反过来（先报缺模型）会让用户白配一套模型再被拒。
    """
    _add_model(client)
    task = _create(
        client,
        config={
            "task_sequence": ["quiz"],
            "target_kind": "desktop_window",
            "target_id": "hwnd-1",
        },
    )
    response = client.post(f"/api/tasks/{task['run_id']}/start")
    assert response.status_code == 400
    detail = response.json()["detail"]
    # v0.2.0 删掉了「通道不支持」这个码（通道选择本身没了），改用「目标不可用」
    assert detail["error_code"] == "target_unavailable"
    assert "视觉识别链路" in detail["message"]


def test_target_gate_is_reported_before_the_model_gate(client: TestClient) -> None:
    """**两条闸门的先后是有意义的**：目标不支持时先报目标。

    反过来的症状很具体：用户看到「没有模型配置」，照做配了一套、点「测试连接」，
    再点启动 —— 才被告知「这个目标根本跑不了」。配多少模型都救不回来的事，
    必须第一个说。
    """
    task = _create(
        client,
        config={
            "task_sequence": ["quiz"],
            "target_kind": "desktop_window",
            "target_id": "hwnd-1",
        },
    )
    detail = client.post(f"/api/tasks/{task['run_id']}/start").json()["detail"]
    assert detail["error_code"] == "target_unavailable"


def test_start_without_any_model_is_refused(client: TestClient) -> None:
    """**任何**运行都要求至少一套模型配置 → 400 + 错误码 + **下一步动作**。

    v0.2.0 起页面只由模型读，没有模型就完全跑不动，所以这条不再挂条件在
    「模型优先」上（那个开关本身已经删掉了）。错误码是给界面分流引导卡用的；
    混成一个笼统的「启动失败」就等于没分流。
    """
    task = _create(client, config={"task_sequence": ["quiz"]})
    response = client.post(f"/api/tasks/{task['run_id']}/start")
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["error_code"] == "no_config"
    assert detail["next_action"], "引导卡没给下一步动作，用户就卡在这儿了"


def test_control_actions_need_an_active_task(client: TestClient) -> None:
    """``pause`` / ``resume`` / ``stop`` 只对**当前活跃**任务有效。"""
    task = _create(client)
    for action in ("pause", "resume", "stop"):
        response = client.post(f"/api/tasks/{task['run_id']}/{action}")
        assert response.status_code == 409, action
        assert response.json()["detail"]["error_code"] == "run_not_active"


def test_unknown_control_action_is_404(client: TestClient) -> None:
    task = _create(client)
    response = client.post(f"/api/tasks/{task['run_id']}/explode")
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "unknown_action"


def test_creating_a_task_does_not_start_it(client: TestClient) -> None:
    """建完不能悄悄跑起来 —— 否则用户分不清「谁动的手」。"""
    _create(client, config={"task_sequence": ["quiz"]})
    assert deps.is_running() is False


# --------------------------------------------------------------------------- #
# 运行配置：两次模型选择都要能读回来
# --------------------------------------------------------------------------- #
def test_run_config_exposes_all_four_model_choices(client: TestClient) -> None:
    """``GET /api/run/config`` 必须把**四个选择**都露出来。

    曾经漏过 ``vision_profile_id``：症状是「视觉识别模型选了、存了，刷新页面又
    变回空白」—— 字段声明了、默认 ``None``，路由组装时忘了传，属于最难发现的那种
    契约漏字段。2026-09-28 又多了两个备用组，同一个坑不能再踩一次。
    """
    payload = client.get("/api/run/config").json()
    for field in (
        "vision_profile_id",
        "model_profile_id",
        "backup_profile_ids",
        "vision_backup_profile_ids",
    ):
        assert field in payload, f"{field} 没露出来"
    assert payload["backup_profile_ids"] == []
    assert payload["vision_backup_profile_ids"] == []

    client.put("/api/run/config", json={"vision_profile_id": "abc123"})
    assert client.get("/api/run/config").json()["vision_profile_id"] == "abc123"


def test_backup_groups_are_accepted_and_persisted(client: TestClient, ui_paths: Path) -> None:
    """两个备用组要能存下来、**顺序就是降级顺序**，而且 ``GET`` 必须回显。

    .. note::
       「回显」那半条曾经写不出来：``ui/routes/run.py::_config_out()`` 漏传
       ``backup_profile_ids`` / ``vision_backup_profile_ids``，于是 PUT 存得下、
       ``GET`` 却永远回空列表 —— 界面上表现为「选了备用、刷新一下就变空白」，
       而落盘文件里明明有。与 ``vision_profile_id`` 当年漏传是同一个坑，已补上。

       **留给以后：给 ``RunConfig`` 加字段时，务必同时检查 ``_config_out()``
       有没有把它回显出去** —— 这条用例就是那个检查。
    """
    response = client.put(
        "/api/run/config",
        json={
            "model_profile_id": "judge1",
            "backup_profile_ids": ["b1", "b2"],
            "vision_backup_profile_ids": ["v1"],
        },
    )
    assert response.status_code == 200

    stored = deps.get_run_config()
    assert stored.model_profile_id == "judge1"
    assert stored.backup_profile_ids == ["b1", "b2"], "备用组的**顺序**就是降级顺序"
    assert stored.vision_backup_profile_ids == ["v1"]

    on_disk = json.loads((ui_paths / "run_config.json").read_text(encoding="utf-8"))
    assert on_disk["backup_profile_ids"] == ["b1", "b2"], "重开界面也得还在"
    assert on_disk["vision_backup_profile_ids"] == ["v1"]

    echoed = client.get("/api/run/config").json()
    assert echoed["backup_profile_ids"] == ["b1", "b2"], "GET 必须回显解题备用组"
    assert echoed["vision_backup_profile_ids"] == ["v1"], "GET 必须回显视觉备用组"


def test_switching_target_kind_clears_the_stale_target_id(client: TestClient) -> None:
    """换了目标类型 = 换了一套「身份证」，CDP target id 与窗口句柄互不通用。

    不清掉的话，切到「应用程序」后启动会拿着一个标签页 ID 当窗口句柄用，
    报出来的错离真正原因十万八千里。
    """
    client.put("/api/run/config", json={"target_kind": "browser_page", "target_id": "tab-1"})
    assert client.get("/api/run/config").json()["target_id"] == "tab-1"

    client.put("/api/run/config", json={"target_kind": "desktop_window"})
    assert client.get("/api/run/config").json()["target_id"] is None


def test_run_config_has_no_channel_choices(client: TestClient) -> None:
    """``GET /api/run/config`` 不再回通道优先级 / 通道模式 / 可用模式。

    v0.2.0 只有视觉一条题目通道，「能选哪些通道」这个由 ``core.targets``
    裁决的能力矩阵连同两个开关一起删除。字段要是冒回来，前端就会照着渲染出
    一个已经不存在的能力。
    """
    payload = client.get("/api/run/config").json()
    for gone in ("probe_order", "probe_mode", "available_modes"):
        assert gone not in payload, f"{gone} 已经删掉了，不该再出现"

    client.put("/api/run/config", json={"target_kind": "desktop_window"})
    assert "available_modes" not in client.get("/api/run/config").json()


# --------------------------------------------------------------------------- #
# 条目一族（``/api/items``）
# --------------------------------------------------------------------------- #
def test_items_default_to_the_latest_run(client: TestClient) -> None:
    """不加过滤会把历次运行的条目全倒出来，界面上就是几百条乱七八糟的东西。"""
    task = _create(client)
    run_id = str(task["run_id"])
    _seed_item(run_id, f"{run_id}-q1", "pending")

    items = client.get("/api/items").json()
    assert [item["item_id"] for item in items] == [f"{run_id}-q1"]


def test_items_can_be_filtered_by_run_and_state(client: TestClient) -> None:
    first = _create(client)
    second = _create(client)
    _seed_item(str(first["run_id"]), f"{first['run_id']}-q1", "pending")
    _seed_item(str(second["run_id"]), f"{second['run_id']}-q2", "failed")

    only_first = client.get("/api/items", params={"run_id": first["run_id"]}).json()
    assert len(only_first) == 1
    assert client.get("/api/items", params={"state": "failed"}).json()[0]["item_id"].endswith("q2")


def test_unknown_item_is_404(client: TestClient) -> None:
    assert client.get("/api/items/nope").status_code == 404
    assert client.get("/api/items/nope/artifacts").status_code == 404


def test_confirm_moves_a_pending_item_to_applied(client: TestClient) -> None:
    task = _create(client)
    run_id = str(task["run_id"])
    item_id = f"{run_id}-q1"
    _seed_item(run_id, item_id, "pending_confirm")

    response = client.post(f"/api/items/{item_id}/confirm", json={"decision": "confirm"})
    assert response.status_code == 200
    assert response.json()["state"] == "applied"


def test_reject_skips_the_item(client: TestClient) -> None:
    task = _create(client)
    run_id = str(task["run_id"])
    item_id = f"{run_id}-q1"
    _seed_item(run_id, item_id, "pending_confirm")

    response = client.post(f"/api/items/{item_id}/confirm", json={"decision": "reject"})
    assert response.json()["state"] == "skipped"


def test_submitted_items_cannot_be_re_decided(client: TestClient) -> None:
    """``submitted`` 是唯一危险态：只能回读结果，**人工点按钮也不行**。

    前端会把按钮置灰，但置灰只是体验 —— 后端这条 409 才是保障。
    """
    task = _create(client)
    run_id = str(task["run_id"])
    item_id = f"{run_id}-q1"
    _seed_item(run_id, item_id, "submitted")

    response = client.post(f"/api/items/{item_id}/confirm", json={"decision": "confirm"})
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "danger_state"


def test_illegal_transition_is_refused(client: TestClient) -> None:
    """``pending`` **没有入边**（T0-2）：还没读到题就谈不上「确认」。"""
    task = _create(client)
    run_id = str(task["run_id"])
    item_id = f"{run_id}-q1"
    _seed_item(run_id, item_id, "pending")

    response = client.post(f"/api/items/{item_id}/confirm", json={"decision": "confirm"})
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "illegal_transition"


def test_item_detail_exposes_the_review_artifact_projection(client: TestClient) -> None:
    """单题详情要能回答「它当时看到/选了什么」—— 截图 URL、采样明细、执行层级。"""
    task = _create(client)
    run_id = str(task["run_id"])
    item_id = f"{run_id}-q1"
    _seed_item(run_id, item_id, "pending_confirm")

    detail = client.get(f"/api/items/{item_id}").json()
    assert detail["item"]["item_id"] == item_id
    assert detail["samples"] == []
    assert detail["is_danger_state"] is False
    assert detail["before_screenshot_url"] is None

    artifact_dir = log_root() / run_id / item_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "before.png").write_bytes(b"png")
    again = client.get(f"/api/items/{item_id}").json()
    assert again["before_screenshot_url"] == f"/api/items/{item_id}/artifacts/before.png"


def test_artifact_route_uses_a_whitelist(client: TestClient) -> None:
    """走白名单而不是黑名单 —— 拼接以外的名字一律 404，路径穿越在第一步就死掉。"""
    task = _create(client)
    run_id = str(task["run_id"])
    item_id = f"{run_id}-q1"
    _seed_item(run_id, item_id, "pending")

    assert client.get(f"/api/items/{item_id}/artifacts/secret.txt").status_code == 404
    assert client.get(f"/api/items/{item_id}/artifacts").json() == {"files": []}


def test_task_and_item_namespaces_do_not_collide(client: TestClient) -> None:
    """``GET /api/tasks/{run_id}`` 与 ``GET /api/items/{item_id}`` 各指各的。

    P5 时代 ``/api/tasks/{id}`` 指**条目**；改成任务之后如果两边路由抢起来，
    界面会拿到形状完全不同的 JSON —— 这种错误只在真跑起来时才炸。
    """
    task = _create(client)
    run_id = str(task["run_id"])
    _seed_item(run_id, f"{run_id}-q1", "pending")

    task_payload = client.get(f"/api/tasks/{run_id}").json()
    item_payload = client.get(f"/api/items/{run_id}-q1").json()
    assert "task" in task_payload and "items" in task_payload
    assert "item" in item_payload and "stem" in item_payload
    assert client.get(f"/api/tasks/{run_id}-q1").status_code == 404
    assert client.get(f"/api/items/{run_id}").status_code == 404


def test_artifacts_live_under_the_log_root_only(client: TestClient, ui_paths: Path) -> None:
    """留痕读取必须落在本次运行的目录里，不能靠 ``..`` 走出去。"""
    task = _create(client)
    run_id = str(task["run_id"])
    item_id = f"{run_id}-q1"
    _seed_item(run_id, item_id, "pending")

    outside = ui_paths / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    assert client.get(f"/api/items/{item_id}/artifacts/..%2F..%2Foutside.txt").status_code == 404


# --------------------------------------------------------------------------- #
# 批量删除（2026-09-28 加）
# --------------------------------------------------------------------------- #
def test_bulk_delete_removes_several_tasks_at_once(client: TestClient) -> None:
    """一次删多个，**逐条回报**。"""
    first = _create(client)
    second = _create(client)
    keep = _create(client)

    response = client.post(
        "/api/tasks/bulk-delete",
        json={"run_ids": [first["run_id"], second["run_id"]]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted"] == [first["run_id"], second["run_id"]]
    assert body["skipped"] == []
    assert [t["run_id"] for t in client.get("/api/tasks").json()["tasks"]] == [keep["run_id"]]


def test_bulk_delete_skips_the_running_task_and_says_why(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**正在跑的那条不许删，但其余照删** —— 而且要说清是哪个、为什么。

    整批放弃等于逼用户自己去找出哪个在跑、再勾一遍；只回一句「部分失败」
    等于什么都没说。
    """
    running = _create(client)
    other = _create(client)
    monkeypatch.setattr("ui.routes.tasks.is_running", lambda: True)
    monkeypatch.setattr("ui.routes.tasks.get_current_run_id", lambda: running["run_id"])

    body = client.post(
        "/api/tasks/bulk-delete",
        json={"run_ids": [running["run_id"], other["run_id"]]},
    ).json()

    assert body["deleted"] == [other["run_id"]]
    assert [item["run_id"] for item in body["skipped"]] == [running["run_id"]]
    assert "正在运行" in body["skipped"][0]["reason"]
    # 没删掉的那条**还在**（`is_running` 是假的，但删除判定必须一致地尊重它）
    listed = [t["run_id"] for t in client.get("/api/tasks").json()["tasks"]]
    assert listed == [running["run_id"]]


def test_bulk_delete_reports_unknown_ids_without_failing_the_batch(
    client: TestClient,
) -> None:
    """不存在的 id 归到 ``skipped``，其余照删 —— 一条坏 id 不该让整批失败。"""
    real = _create(client)

    body = client.post(
        "/api/tasks/bulk-delete",
        json={"run_ids": [real["run_id"], "nope00000000"]},
    ).json()

    assert body["deleted"] == [real["run_id"]]
    assert [item["run_id"] for item in body["skipped"]] == ["nope00000000"]
    assert body["skipped"][0]["reason"] == "任务不存在"


def test_bulk_delete_deduplicates_and_accepts_an_empty_list(client: TestClient) -> None:
    """重复勾选不该删两次；空列表是**合法输入**（什么也不做，如实回空结果）。"""
    task = _create(client)

    once = client.post(
        "/api/tasks/bulk-delete",
        json={"run_ids": [task["run_id"], task["run_id"]]},
    ).json()
    assert once["deleted"] == [task["run_id"]], "同一个 id 只算一次"

    empty = client.post("/api/tasks/bulk-delete", json={"run_ids": []})
    assert empty.status_code == 200
    assert empty.json() == {"deleted": [], "skipped": []}


# --------------------------------------------------------------------------- #
# 重试（2026-09-28 加）
# --------------------------------------------------------------------------- #
def test_retry_does_not_get_swallowed_by_the_action_wildcard(client: TestClient) -> None:
    """``/retry`` 必须走它自己的路由，不能被 ``/{run_id}/{action}`` 吃掉。

    路径顺序错了的表现是「以未知动作 404 出来」—— 而那条信息会把人引向完全
    错误的方向（``run.py`` 的 ``/models/order`` 栽过同一个坑，见维护指南）。
    """
    _add_model(client)
    task = _create(client)

    response = client.post(f"/api/tasks/{task['run_id']}/retry")

    assert response.status_code == 200, response.text


def test_retry_restarts_the_same_task_instead_of_creating_a_new_one(
    client: TestClient,
) -> None:
    """重试 = 再起**同一个**任务（断点续跑），不是新建一个。

    这条是「已完成的题不重做、提交绝不重放」的接口侧投影：真的新建任务的话，
    条目会重新认领一遍，那就不是续跑了。
    """
    _add_model(client)
    task = _create(client, name="失败过的任务")

    body = client.post(f"/api/tasks/{task['run_id']}/retry").json()

    assert body["run_id"] == task["run_id"]
    assert body["name"] == "失败过的任务"
    assert len(client.get("/api/tasks").json()["tasks"]) == 1, "不该多出一个任务"


def test_retry_refuses_while_a_task_is_running(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已经有任务在跑 → 409（与「启动」共用同一条前置检查）。"""
    _add_model(client)
    task = _create(client)
    monkeypatch.setattr("ui.routes.tasks.is_running", lambda: True)

    response = client.post(f"/api/tasks/{task['run_id']}/retry")

    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "run_active"
