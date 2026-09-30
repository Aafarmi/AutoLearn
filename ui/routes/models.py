"""模型配置与凭据路由（P5 / M2-6 / M2-7）。

安全约束（逐条对应任务书 §7.2）：

- 新增 / 编辑时密钥写入 ``CredentialStore``（``keyring`` → WinCred），
  **绝不落进 ``models.yaml``**；
- ``PUT`` 时 ``api_key`` 留空表示不改动既有密钥；
- ``DELETE`` 同步删除凭据，不留孤儿；
- 出参一律不含密钥明文（``ModelProfileOut`` 只有 ``has_api_key``）；
- ``[测试连接]`` 必须实测并回写，不接受手填。

路由顺序注意：``/models/order`` 必须声明在 ``/models/{profile_id}`` **之前**，
否则 ``order`` 会被当成 profile_id 吞掉。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import SecretStr

from core.model_registry import ModelProfile, build_api_key_ref, test_connection
from core.models import CapabilityReport
from ui.deps import get_registry
from ui.schemas import ModelProfileIn, ModelProfileOut, PresetOut

__all__ = ["PRESETS", "router"]

router = APIRouter(prefix="/api", tags=["models"])

#: 表单预设。**不写任何「免费额度」承诺** —— 厂商政策会变，写死了就是错误信息。
#: 只提供 base_url 与一个常见模型名，其余引导用户看厂商文档。
#:
#: **一套预设一个模型**（2026-09-28 起）：原先同时给 tier1/tier2 两个建议型号，
#: 现在只给一个 —— 用户要更强的模型自己改这一格即可。
PRESETS: tuple[PresetOut, ...] = (
    PresetOut(
        preset_id="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini",
        docs_url="https://platform.openai.com/docs",
        free_quota_note="额度与计费以厂商官网为准",
    ),
    PresetOut(
        preset_id="deepseek",
        label="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        docs_url="https://platform.deepseek.com/api-docs",
        free_quota_note="额度与计费以厂商官网为准",
    ),
    PresetOut(
        preset_id="dashscope",
        label="阿里云百炼（OpenAI 兼容）",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="qwen-plus",
        docs_url="https://help.aliyun.com/zh/model-studio/",
        free_quota_note="额度与计费以厂商官网为准",
    ),
    PresetOut(
        preset_id="zhipu",
        label="智谱 GLM",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        model="glm-4-flash",
        docs_url="https://open.bigmodel.cn/dev/api",
        free_quota_note="额度与计费以厂商官网为准",
    ),
    PresetOut(
        preset_id="moonshot",
        label="月之暗面 Kimi",
        base_url="https://api.moonshot.cn/v1",
        model="moonshot-v1-8k",
        docs_url="https://platform.moonshot.cn/docs",
        free_quota_note="额度与计费以厂商官网为准",
    ),
    PresetOut(
        preset_id="ollama",
        label="本地 Ollama",
        base_url="http://127.0.0.1:11434/v1",
        model="qwen2.5:7b",
        docs_url="https://ollama.com/",
        free_quota_note="本地推理，无外部额度限制",
    ),
)

#: 改了这些字段就意味着「配置已经不是实测时的那套了」，能力报告必须作废
_INVALIDATING_FIELDS = ("base_url", "model")


def _to_out(profile: ModelProfile) -> ModelProfileOut:
    return ModelProfileOut(
        profile_id=profile.profile_id,
        name=profile.name,
        base_url=profile.base_url,
        model=profile.model,
        temperature=profile.temperature,
        timeout_s=profile.timeout_s,
        concurrency=profile.concurrency,
        enabled=profile.enabled,
        order=profile.order,
        has_api_key=get_registry().has_credential(profile.profile_id),
        capabilities=profile.capabilities,
    )


def _profile_or_404(profile_id: str) -> ModelProfile:
    profile = get_registry().get(profile_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "profile_not_found", "message": f"配置 {profile_id} 不存在"},
        )
    return profile


@router.get("/models", response_model=list[ModelProfileOut])
async def list_models() -> list[ModelProfileOut]:
    return [_to_out(profile) for profile in get_registry().list()]


@router.post("/models", response_model=ModelProfileOut, status_code=status.HTTP_201_CREATED)
async def create_model(payload: ModelProfileIn) -> ModelProfileOut:
    registry = get_registry()
    profile_id = uuid.uuid4().hex[:12]
    profile = ModelProfile(
        profile_id=profile_id,
        name=payload.name,
        base_url=payload.base_url,
        model=payload.model,
        temperature=payload.temperature,
        timeout_s=payload.timeout_s,
        concurrency=payload.concurrency,
        api_key_ref=build_api_key_ref(profile_id),
        enabled=payload.enabled,
    )
    registry.add(profile)
    if payload.api_key:
        try:
            registry.credentials().put(profile.api_key_ref, SecretStr(payload.api_key))
        except Exception as exc:
            # 密钥没存进去就不能把配置留在 models.yaml 里 —— 否则界面上会出现一张
            # 「配置存在、密钥不存在」的卡片，用户点 [测试连接] 只会看到 auth_failed，
            # 而且没有任何入口告诉他密钥压根没保存成功。回滚到「什么都没发生」。
            registry.remove(profile_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "error_code": "credential_store_failed",
                    "message": f"密钥未能写入凭据管理器，配置已回滚：{exc}",
                },
            ) from exc
    return _to_out(_profile_or_404(profile_id))


@router.put("/models/order")
async def reorder_models(payload: dict) -> dict[str, bool]:
    """``{profile_ids: []}``。**排序即降级链**，改完 ``active_chain()`` 立即生效。"""
    profile_ids = payload.get("profile_ids")
    if not isinstance(profile_ids, list) or not profile_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error_code": "invalid_payload", "message": "需要非空的 profile_ids 列表"},
        )
    try:
        get_registry().reorder([str(pid) for pid in profile_ids])
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "profile_not_found", "message": str(exc)},
        ) from exc
    return {"ok": True}


@router.put("/models/{profile_id}", response_model=ModelProfileOut)
async def update_model(profile_id: str, payload: ModelProfileIn) -> ModelProfileOut:
    """``api_key`` 留空表示不改。"""
    registry = get_registry()
    before = _profile_or_404(profile_id)

    patch = payload.model_dump(exclude_none=True)
    secret = patch.pop("api_key", None)
    # 换了接入点或模型名，旧的实测结论就不成立了
    if any(getattr(before, field) != patch.get(field, getattr(before, field))
           for field in _INVALIDATING_FIELDS):
        patch["capabilities"] = None

    try:
        registry.update(profile_id, patch)
    except KeyError as exc:  # pragma: no cover - 已先 404 过
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from exc

    if secret:
        registry.credentials().put(before.api_key_ref, SecretStr(secret))
    return _to_out(_profile_or_404(profile_id))


@router.delete("/models/{profile_id}")
async def delete_model(profile_id: str) -> dict[str, bool]:
    """同步删除凭据。"""
    _profile_or_404(profile_id)
    get_registry().remove(profile_id)
    return {"ok": True}


@router.post("/models/{profile_id}/test", response_model=CapabilityReport)
async def test_model(profile_id: str) -> CapabilityReport:
    """实测鉴权 / Tier1 / Tier2 / 视觉 / 结构化输出 / image_payload / 真实 QPS。"""
    registry = get_registry()
    profile = _profile_or_404(profile_id)
    # 密钥必须从**存它的那个 store** 读（见 test_connection docstring）——
    # 另起 CredentialStore() 在真实 keyring 上侥幸能过，在内存后端上必然 auth_failed
    report = await test_connection(profile, registry.credentials())
    registry.set_capabilities(profile_id, report)
    return report


@router.get("/providers/presets", response_model=list[PresetOut])
async def list_presets() -> list[PresetOut]:
    """表单预设模板，降低首次配置门槛。"""
    return list(PRESETS)
