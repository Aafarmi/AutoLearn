"""P5 模型配置路由验收（M2-6 / M2-7）。

覆盖 P5 验收口径里的：
- 多套配置可新增 / 编辑 / 删除 / 排序；**排序结果即降级链**
- 密钥保存后**不回显明文**
- [测试连接] 必须实测（用本地假 OpenAI 兼容端点验证，零密钥可跑）
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from core.model_registry import migrate_profile_payload
from ui.deps import get_registry
from ui.routes.models import PRESETS

SECRET = "sk-super-secret-value"


def create_profile(
    client: TestClient,
    base_url: str,
    *,
    name: str = "假厂商",
    model: str = "fake-model",
    api_key: str | None = SECRET,
) -> dict:
    """建一套配置。**一套配置只有一个模型**（2026-09-28 起，不再分 tier1 / tier2）。"""
    body: dict[str, object] = {"name": name, "base_url": base_url, "model": model}
    if api_key is not None:
        body["api_key"] = api_key
    response = client.post("/api/models", json=body)
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# 预设与列表
# --------------------------------------------------------------------------- #
def test_presets_are_usable(client: TestClient) -> None:
    payload = client.get("/api/providers/presets").json()
    assert len(payload) == len(PRESETS)
    for preset in payload:
        assert preset["base_url"].startswith("http")
        # 预设给的是**一个**模型名（原先同时给 tier1/tier2 两个建议型号）
        assert preset["model"]
        assert "tier1_model" not in preset
        assert "tier2_model" not in preset


def test_empty_registry_is_not_an_error(client: TestClient) -> None:
    """首次运行没有配置 —— 返回空列表，不报错、不阻塞。"""
    assert client.get("/api/models").json() == []


def test_list_never_echoes_secret(client: TestClient, provider: Callable[[str], str]) -> None:
    create_profile(client, provider("ok"))
    raw = client.get("/api/models").text
    assert SECRET not in raw
    assert "api_key" not in raw.replace("has_api_key", "")


# --------------------------------------------------------------------------- #
# 增删改
# --------------------------------------------------------------------------- #
def test_create_without_key_is_allowed(client: TestClient, provider: Callable[[str], str]) -> None:
    """不填密钥也能存 —— 首次运行不阻塞。"""
    profile = create_profile(client, provider("ok"), api_key=None)
    assert profile["has_api_key"] is False
    assert profile["capabilities"] is None


def test_create_writes_key_to_credential_store_not_yaml(
    client: TestClient, provider: Callable[[str], str], ui_paths: Path
) -> None:
    create_profile(client, provider("ok"))
    yaml_text = (ui_paths / "models.yaml").read_text(encoding="utf-8")
    assert SECRET not in yaml_text, "密钥落进了 models.yaml —— 安全事故"
    assert "api_key_ref" in yaml_text
    assert get_registry().has_credential(get_registry().list()[0].profile_id) is True


def test_create_generates_unique_ids(client: TestClient, provider: Callable[[str], str]) -> None:
    first = create_profile(client, provider("ok"), name="A")
    second = create_profile(client, provider("ok"), name="B")
    assert first["profile_id"] != second["profile_id"]
    assert [p["name"] for p in client.get("/api/models").json()] == ["A", "B"]


def test_update_blank_key_keeps_existing(
    client: TestClient, provider: Callable[[str], str], ui_paths: Path
) -> None:
    profile = create_profile(client, provider("ok"))
    before = get_registry().credential(profile["profile_id"])

    response = client.put(
        f"/api/models/{profile['profile_id']}",
        json={
            "name": "改名了",
            "base_url": profile["base_url"],
            "model": profile["model"],
        },
    )
    assert response.status_code == 200
    assert response.json()["name"] == "改名了"
    assert get_registry().credential(profile["profile_id"]).get_secret_value() == (
        before.get_secret_value() if before else None
    )


def test_update_with_new_key_replaces_it(
    client: TestClient, provider: Callable[[str], str]
) -> None:
    profile = create_profile(client, provider("ok"))
    client.put(
        f"/api/models/{profile['profile_id']}",
        json={
            "name": profile["name"],
            "base_url": profile["base_url"],
            "model": profile["model"],
            "api_key": "sk-rotated",
        },
    )
    stored = get_registry().credential(profile["profile_id"])
    assert stored is not None and stored.get_secret_value() == "sk-rotated"


def test_delete_removes_credential_too(
    client: TestClient, provider: Callable[[str], str]
) -> None:
    profile = create_profile(client, provider("ok"))
    ref = get_registry().list()[0].api_key_ref

    assert client.delete(f"/api/models/{profile['profile_id']}").status_code == 200
    assert client.get("/api/models").json() == []
    assert get_registry().credentials().get(ref) is None, "凭据成了孤儿"


def test_unknown_profile_returns_404(client: TestClient) -> None:
    assert client.put(
        "/api/models/nope",
        json={"name": "x", "base_url": "http://x/v1", "model": "m"},
    ).status_code == 404
    assert client.delete("/api/models/nope").status_code == 404
    assert client.post("/api/models/nope/test").status_code == 404


# --------------------------------------------------------------------------- #
# 排序即降级链
# --------------------------------------------------------------------------- #
def test_reorder_changes_active_chain_immediately(
    client: TestClient, provider: Callable[[str], str]
) -> None:
    ids = [
        create_profile(client, provider("ok"), name=name)["profile_id"]
        for name in ("一号", "二号", "三号")
    ]
    assert [p.name for p in get_registry().active_chain()] == ["一号", "二号", "三号"]

    response = client.put("/api/models/order", json={"profile_ids": list(reversed(ids))})
    assert response.status_code == 200
    assert [p.name for p in get_registry().active_chain()] == ["三号", "二号", "一号"]
    # 接口读出来的顺序也要跟着变
    assert [p["name"] for p in client.get("/api/models").json()] == ["三号", "二号", "一号"]


def test_reorder_rejects_unknown_ids(client: TestClient, provider: Callable[[str], str]) -> None:
    create_profile(client, provider("ok"))
    response = client.put("/api/models/order", json={"profile_ids": ["ghost"]})
    assert response.status_code == 404


@pytest.mark.parametrize("payload", [{}, {"profile_ids": []}, {"profile_ids": "x"}])
def test_reorder_rejects_bad_payload(
    client: TestClient, payload: dict[str, object]
) -> None:
    assert client.put("/api/models/order", json=payload).status_code == 422


# --------------------------------------------------------------------------- #
# [测试连接] 实测
# --------------------------------------------------------------------------- #
def test_connection_ok_reports_every_capability(
    client: TestClient, provider: Callable[[str], str]
) -> None:
    profile = create_profile(client, provider("ok"))
    report = client.post(f"/api/models/{profile['profile_id']}/test").json()

    assert report["auth_ok"] is True
    assert report["model_ok"] is True
    assert report["supports_vision"] is True
    assert report["supports_structured_output"] is True
    assert report["image_payload"] == "base64"
    assert report["max_qps"] > 0, "真实 QPS 必须实测出来"
    assert report["latency_ms"] >= 0
    assert report["error_code"] is None


def test_connection_result_is_written_back(
    client: TestClient, provider: Callable[[str], str], ui_paths: Path
) -> None:
    profile = create_profile(client, provider("ok"))
    client.post(f"/api/models/{profile['profile_id']}/test")
    # 出参里带上实测结果（不接受手填）
    listed = client.get("/api/models").json()[0]
    assert listed["capabilities"]["supports_vision"] is True
    assert listed["capabilities"]["auth_ok"] is True
    assert "supports_vision: true" in (ui_paths / "models.yaml").read_text(encoding="utf-8")


def test_connection_auth_failure(client: TestClient, provider: Callable[[str], str]) -> None:
    profile = create_profile(client, provider("unauthorized"))
    report = client.post(f"/api/models/{profile['profile_id']}/test").json()
    assert report["auth_ok"] is False
    assert report["error_code"] == "auth_failed"


def test_connection_reports_a_model_the_endpoint_does_not_have(
    client: TestClient, provider: Callable[[str], str]
) -> None:
    """配置里的模型名不在端点清单里 → 明确报 ``model_not_found``。

    「一套配置一个模型」之后，「模型名对不对」这件事只剩**一个**要校验的对象，
    所以更要如实报出来：混成一个笼统的 ``provider_error``，用户会去查网络与密钥，
    而真正的原因是自己填错了模型名（实测最常见）。
    """
    profile = create_profile(client, provider("nomodel"), model="fake-model-2")
    report = client.post(f"/api/models/{profile['profile_id']}/test").json()
    assert report["auth_ok"] is False
    assert report["model_ok"] is False
    assert report["error_code"] == "model_not_found"


def test_connection_without_vision(client: TestClient, provider: Callable[[str], str]) -> None:
    profile = create_profile(client, provider("novision"))
    report = client.post(f"/api/models/{profile['profile_id']}/test").json()
    assert report["supports_vision"] is False
    assert report["image_payload"] is None
    assert report["vision_evidence"] is None
    # 「模型优先」要不要置灰看的是实测结论，所以这个字段必须真的反映出来
    assert client.get("/api/run/config").json()["vision_ready"] is False


# --------------------------------------------------------------------------- #
# 视觉探针的测试方式（实测踩过的假阴性）
# --------------------------------------------------------------------------- #
def test_probe_image_is_not_degenerate() -> None:
    """**回归守卫**：视觉探针图必须是一张有内容的正常图。

    原先用的是 **1×1 透明 PNG**，厂商直接判为非法图像回 400 ——

        "You have uploaded an unsupported image. Please make sure your image
         is valid and has one of the following formats: webp, png, jpeg, and gif."

    于是「测试连接」把**明明支持视觉**的模型（实测 DeepSeek 4.1 的两个 tier
    都能正确读出图中的数字）报成不支持。假阴性比漏测更糟：它会让人去改
    本来没问题的配置。这条用例把「不许退回退化图」钉死。
    """
    from io import BytesIO

    from PIL import Image

    from core.model_registry import _PROBE_DIGIT, _probe_image

    with Image.open(BytesIO(_probe_image())) as image:
        assert image.width >= 64 and image.height >= 64, "探针图太小，会被厂商拒绝"
        grey = image.convert("L")
        low, high = grey.getextrema()
    assert low < 64, "探针图里得有深色笔画（否则等于空白图）"
    assert high > 192, "探针图得是浅底，才能看清笔画"
    assert _PROBE_DIGIT == "7", "探针图的预期答案变了，校验逻辑必须同步改"


def test_probe_image_is_stable() -> None:
    """同一进程内探针图必须稳定 —— 判定依赖它画的是什么。"""
    from core.model_registry import _probe_image

    assert _probe_image() == _probe_image()


def test_vision_probe_targets_the_only_model_in_the_profile(
    client: TestClient, provider: Callable[[str], str]
) -> None:
    """视觉能力必须探**图片真正会发去的那个模型**。

    2026-09-28 起一套配置只有一个模型，2026-09-29 连档位参数也去掉了：
    ``model_for()`` 恒返回 ``profile.model``，图片也发给它。
    这条用例把「探的就是它」钉住 —— 老的双模型实现里，探 tier1 而图片发去
    tier2，会得出与事实不符的结论（探针说「支持」，跑起来图发给另一个模型）。
    """
    profile = create_profile(client, provider("ok"), model="fake-model")
    report = client.post(f"/api/models/{profile['profile_id']}/test").json()

    assert report["supports_vision"] is True
    assert report["vision_model"] == profile["model"], "探的必须是这套配置里唯一的模型"


def test_vision_evidence_is_strong_when_model_reads_the_image(
    client: TestClient, provider: Callable[[str], str]
) -> None:
    """只看状态码不够：有的厂商 200 却把图片丢掉。

    所以还要**校验回答内容** —— 探针图里画的是 7，答对了才算强证据。
    """
    profile = create_profile(client, provider("vision"))
    report = client.post(f"/api/models/{profile['profile_id']}/test").json()

    assert report["supports_vision"] is True
    assert report["vision_evidence"] == "read_digit"


def test_vision_evidence_is_weak_when_answer_cannot_be_verified(
    client: TestClient, provider: Callable[[str], str]
) -> None:
    """服务端收下了图但没答对 → 结论仍然成立，但证据只到「弱」，要如实标出来。

    （推理类模型在 token 预算不足时正文会为空，正是这种情形。）
    """
    profile = create_profile(client, provider("ok"))
    report = client.post(f"/api/models/{profile['profile_id']}/test").json()

    assert report["supports_vision"] is True
    assert report["vision_evidence"] == "accepted"


def test_connection_without_structured_output(
    client: TestClient, provider: Callable[[str], str]
) -> None:
    profile = create_profile(client, provider("nostruct"))
    report = client.post(f"/api/models/{profile['profile_id']}/test").json()
    assert report["supports_structured_output"] is False
    # 不支持结构化输出只是降级为文本解析，不该让整次测试判失败
    assert report["auth_ok"] is True


def test_connection_without_key(client: TestClient, provider: Callable[[str], str]) -> None:
    profile = create_profile(client, provider("ok"), api_key=None)
    report = client.post(f"/api/models/{profile['profile_id']}/test").json()
    assert report["auth_ok"] is False
    assert report["error_code"] == "no_config"


def test_changing_endpoint_invalidates_measured_capabilities(
    client: TestClient, provider: Callable[[str], str]
) -> None:
    """换了接入点**或换了模型名**，旧的实测结论就不成立了，必须作废。

    ``_INVALIDATING_FIELDS = ("base_url", "model")`` —— 单模型设计之后，
    模型名与接入点一样是「配置身份」的一部分：改了它，上一次那份
    「支持视觉 / 支持结构化输出」的报告说的已经是另一个模型了。
    """
    profile = create_profile(client, provider("ok"))
    client.post(f"/api/models/{profile['profile_id']}/test")
    assert client.get("/api/models").json()[0]["capabilities"] is not None

    # ① 换接入点
    client.put(
        f"/api/models/{profile['profile_id']}",
        json={
            "name": profile["name"],
            "base_url": provider("novision"),
            "model": profile["model"],
        },
    )
    assert client.get("/api/models").json()[0]["capabilities"] is None

    # ② 换模型名（同一份报告不能跨模型复用）
    client.post(f"/api/models/{profile['profile_id']}/test")
    assert client.get("/api/models").json()[0]["capabilities"] is not None
    client.put(
        f"/api/models/{profile['profile_id']}",
        json={
            "name": profile["name"],
            "base_url": profile["base_url"],
            "model": "另一个模型",
        },
    )
    assert client.get("/api/models").json()[0]["capabilities"] is None


# --------------------------------------------------------------------------- #
# 老 ``models.yaml`` 的自动迁移（一套配置一个模型）
# --------------------------------------------------------------------------- #
def test_legacy_two_model_payload_is_migrated() -> None:
    """纯函数口径：``tier1_model``/``tier2_model`` → ``model``（取 tier1）。

    为什么取 tier1：它是老格式里的**必填**字段，任何一份老配置都一定有它；
    ``tier2_model`` 可能为空，且在「只调一次模型」的新用法下本来就多余。
    """
    migrated = migrate_profile_payload(
        {
            "profile_id": "p1",
            "name": "老配置",
            "base_url": "https://api.example.com/v1",
            "tier1_model": "old-small",
            "tier2_model": "old-big",
            "api_key_ref": "autolearn/p1",
        }
    )
    assert migrated["model"] == "old-small"
    assert "tier1_model" not in migrated and "tier2_model" not in migrated


def test_legacy_capability_flags_are_merged_into_model_ok() -> None:
    """老报告里的两个勾合并成一个 ``model_ok``，**不整份清空**。

    清空整份报告意味着用户升个版本就得把所有模型重新测一遍，
    而那份报告里的视觉 / 结构化结论（以及 QPS、延迟）仍然有效。
    """
    merged = migrate_profile_payload(
        {
            "model": "m",
            "capabilities": {
                "auth_ok": True,
                "tier1_ok": True,
                "tier2_ok": False,
                "supports_vision": True,
                "supports_structured_output": True,
                "max_qps": 2.0,
            },
        }
    )
    report = merged["capabilities"]
    assert report["model_ok"] is True, "口径取 tier1（保留下来的就是那个模型）"
    assert report["supports_vision"] is True and report["max_qps"] == 2.0
    assert "tier1_ok" not in report and "tier2_ok" not in report


def test_legacy_capability_falls_back_to_tier2_flag_when_tier1_missing() -> None:
    """只有 ``tier2_ok`` 时也认 —— 缺了 tier1 就回落 tier2，别把报告判成失败。"""
    merged = migrate_profile_payload({"model": "m", "capabilities": {"tier2_ok": True}})
    assert merged["capabilities"]["model_ok"] is True


def test_legacy_yaml_is_loaded_and_migrated_by_the_registry(
    client: TestClient, ui_paths: Path
) -> None:
    """老 ``models.yaml`` 直接放到盘上，接口读出来就是新格式 —— 用户不必重配。"""
    (ui_paths / "models.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "profiles": [
                    {
                        "profile_id": "legacy1",
                        "name": "升级前存的",
                        "base_url": "https://api.example.com/v1",
                        "tier1_model": "old-small",
                        "tier2_model": "old-big",
                        "api_key_ref": "autolearn/legacy1",
                        "order": 0,
                        "capabilities": {
                            "auth_ok": True,
                            "tier1_ok": True,
                            "tier2_ok": True,
                            "supports_vision": False,
                            "supports_structured_output": True,
                        },
                    }
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    listed = client.get("/api/models").json()
    assert len(listed) == 1
    assert listed[0]["model"] == "old-small", "老配置的模型名必须被认出来"
    assert listed[0]["capabilities"]["model_ok"] is True


# --------------------------------------------------------------------------- #
# 与运行配置的联动
# --------------------------------------------------------------------------- #
def test_model_ready_flips_after_adding_a_profile(
    client: TestClient, provider: Callable[[str], str]
) -> None:
    assert client.get("/api/run/config").json()["model_ready"] is False
    create_profile(client, provider("ok"))
    assert client.get("/api/run/config").json()["model_ready"] is True


def test_start_becomes_possible_once_model_exists(
    client: TestClient, provider: Callable[[str], str]
) -> None:
    """模型库为空 → 启动被拦；加一套支持视觉的配置之后就能起跑。

    v0.2.0 起页面只由模型读，所以这条闸门对**任何**运行都成立
    （原先只有「模型优先」才要求模型，默认还能退回 Mock 空跑）。
    """
    assert client.post("/api/run/start").status_code == 400

    create_profile(client, provider("ok"))
    assert client.post("/api/run/start").status_code == 200
