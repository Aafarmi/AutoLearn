"""模型配置注册表、凭据层与能力实测（M2-6 / M2-7）。

分工说明
--------
按规划书 P4 的交付清单，本模块归属**泳道 B**。但 P5 的模型配置面板必须消费它 ——
否则界面上第一次点击就撞上 ``NotImplementedError``，P5 验收（新增/编辑/删除/排序、
[测试连接] 实测）一条都过不了。因此由泳道 D 先行落地（与 P0 代铺同一处理方式），
**已在 ``README.md`` 登记，请泳道 B 评审**。

安全红线
--------
- ``models.yaml`` 只存非敏感字段，密钥以 ``api_key_ref`` 引用，**零明文**；
- 密钥由用户在软件内 UI 表单填写，持久化至 OS 凭据管理器（``keyring`` → WinCred）；
- 读取优先级：环境变量 > ``keyring`` > 不提供；
- 出参一律经 ``SecretStr`` 脱敏，日志与 UI 均不回显明文。
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import uuid
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, SecretStr

from core.models import CapabilityReport

__all__ = [
    "DEFAULT_MODELS_PATH",
    "SECRET_BACKEND_ENV",
    "CredentialStore",
    "InMemorySecretBackend",
    "KeyringSecretBackend",
    "ModelProfile",
    "ModelRegistry",
    "ProfileIdList",
    "ProfileList",
    "SecretBackend",
    "build_api_key_ref",
    "migrate_profile_payload",
    "test_connection",
]

#: 模型配置清单默认落位
DEFAULT_MODELS_PATH = Path("state/models.yaml")

#: keyring 里的服务名
KEYRING_SERVICE = "autolearn"

#: 强制指定密钥后端的开关。唯一取值 ``memory``（见 :func:`_default_backend`）。
SECRET_BACKEND_ENV = "AUTOLEARN_SECRET_BACKEND"
_MEMORY_BACKEND = "memory"

#: 环境变量前缀。``autolearn/openai/main`` → ``AUTOLEARN_API_KEY_AUTOLEARN_OPENAI_MAIN``
ENV_PREFIX = "AUTOLEARN_API_KEY_"

#: QPS 采样次数（真实 QPS 必须实测，不接受手填）
QPS_SAMPLES = 3

#: 视觉探针图的边长。太小会被厂商拒绝（见 :func:`_probe_image` 的说明）。
_PROBE_IMAGE_SIDE = 256

#: 探针图里画的数字，以及**回答里应当出现的字符串**。
#:
#: 用数字而不是颜色/形状：数字是**语言无关**的 —— 问「圆形什么颜色」会得到
#: 「红色」或 "red" 两种答案，而「图中的数字是多少」恒为 ``7``。
_PROBE_DIGIT = "7"


@lru_cache(maxsize=1)
def _probe_image() -> bytes:
    """生成视觉探针用的 PNG：白底 + 一个大黑数字。

    **必须是一张有内容的正常图片。** 这里踩过一个很贵的坑：原先用的是一张
    **1×1 透明 PNG**，结果厂商直接判为非法图像并回 400 ——

        "You have uploaded an unsupported image. Please make sure your image
         is valid and has one of the following formats: webp, png, jpeg, and gif."

    于是「测试连接」把**明明支持视觉**的模型（实测 DeepSeek 4.1 系列两个 tier
    都能正确读出图中的数字）报成 ``supports_vision = false``。这类假阴性比
    「漏测」更糟：它会让人去改本来没问题的配置。

    做法：用 PIL 缺省位图字体先画在 16×16 上，再用 ``NEAREST`` 放大 16 倍。
    缺省字体不可缩放，但放大后是清晰的块状字形，模型认得很准。
    **不引入二进制资源文件** —— 探针图应当在代码里生成，可复现、可审阅。
    """
    from PIL import Image, ImageDraw

    glyph = Image.new("L", (16, 16), 255)
    ImageDraw.Draw(glyph).text((3, 2), _PROBE_DIGIT, fill=0)
    enlarged = glyph.resize(
        (_PROBE_IMAGE_SIDE, _PROBE_IMAGE_SIDE), Image.Resampling.NEAREST
    ).convert("RGB")

    buffer = BytesIO()
    enlarged.save(buffer, format="PNG")
    return buffer.getvalue()


def _probe_image_data_uri() -> str:
    return "data:image/png;base64," + base64.b64encode(_probe_image()).decode()

_REF_SANITIZE = re.compile(r"[^A-Za-z0-9]+")


def _env_key_ref(ref: str) -> str:
    return ENV_PREFIX + _REF_SANITIZE.sub("_", ref).strip("_").upper()


def build_api_key_ref(profile_id: str) -> str:
    """由 ``profile_id`` 派生凭据条目名。"""
    return f"{KEYRING_SERVICE}/{profile_id}"


class ModelProfile(BaseModel):
    """一套模型配置。**不含任何密钥明文**。

    **一套配置只有一个模型**（2026-09-28 起）。原先的 ``tier1_model`` +
    ``tier2_model`` 双模型设计被用户否决：「模型库每次添加只需要一个模型，
    不需要 t1，t2」。语义上 Tier 仍然存在（升级复算走同一个模型），
    只是不再由配置指定两个不同的模型名。

    兼容：老的 ``models.yaml`` 里只有 ``tier1_model`` / ``tier2_model``，
    加载时由 :func:`migrate_profile_payload` **自动迁移**（取 ``tier1_model``），
    用户不需要重新配置。
    """

    model_config = ConfigDict(extra="forbid")

    profile_id: str
    name: str
    base_url: str
    #: 这套配置用的模型名（唯一）。
    model: str
    temperature: float = 0.2
    timeout_s: int = 60
    concurrency: int = 2
    #: 指向 ``CredentialStore`` 中的条目名，形如 ``autolearn/<profile_id>``
    api_key_ref: str
    capabilities: CapabilityReport | None = None
    enabled: bool = True
    order: int = 0


#: 老字段名 → 新字段名。**只用于读取**（写回一律用新格式）。
LEGACY_MODEL_FIELDS = ("tier1_model", "tier2_model")


def migrate_profile_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """把老格式的模型配置字典迁移成新格式（单模型）。

    两处要迁移：

    1. **模型名**：取 ``tier1_model`` 作为唯一模型，丢掉 ``tier2_model``。
       为什么取 tier1：它是**必填**字段，任何一份老配置都一定有它；
       ``tier2_model`` 可能为空，且在用户「只调一次模型」的新用法下本来就多余。
    2. **已存的实测报告**（``capabilities``，上一次[测试连接]的结果）：
       老报告里是 ``tier1_ok`` / ``tier2_ok`` 两个勾，新契约只有一个
       ``model_ok``。映射口径取 **``tier1_ok``**（那正是保留下来的那个模型），
       缺了才回落 ``tier2_ok``。**不清空整份报告** —— 否则用户升个版本
       就得把所有模型重新测一遍，而那份报告里的视觉/结构化结论仍然有效。

    迁移是**补新键、删旧键**：兼容旧文件的同时保证写回时是干净的新格式
    （`ModelProfile` 是 ``extra="forbid"``，留着旧键会导致下次加载直接失败）。
    """
    if not isinstance(payload, dict):
        return payload
    migrated = dict(payload)

    if not migrated.get("model"):
        legacy = migrated.get("tier1_model")
        if isinstance(legacy, str) and legacy.strip():
            migrated["model"] = legacy.strip()
    for key in LEGACY_MODEL_FIELDS:
        migrated.pop(key, None)
    migrated.pop("vision_model", None)

    capabilities = migrated.get("capabilities")
    if isinstance(capabilities, dict):
        report = dict(capabilities)
        if not report.get("model_ok"):
            report["model_ok"] = bool(
                report.get("tier1_ok") or report.get("tier2_ok")
            )
        report.pop("tier1_ok", None)
        report.pop("tier2_ok", None)
        migrated["capabilities"] = report
    return migrated


#: 契约要求方法名为 ``list``，它会遮蔽内建 ``list``，导致类体内无法写 ``list[X]``。
#: 因此把用到的容器类型提前取别名。
ProfileList = list[ModelProfile]
ProfileIdList = list[str]


# --------------------------------------------------------------------------- #
# 凭据层
# --------------------------------------------------------------------------- #
class SecretBackend(Protocol):
    """密钥存储后端。"""

    def set(self, ref: str, secret: str) -> None: ...

    def get(self, ref: str) -> str | None: ...

    def delete(self, ref: str) -> None: ...


class KeyringSecretBackend:
    """OS 凭据管理器。Windows 落到 WinCred。"""

    def set(self, ref: str, secret: str) -> None:
        import keyring

        keyring.set_password(KEYRING_SERVICE, ref, secret)

    def get(self, ref: str) -> str | None:
        import keyring

        try:
            return keyring.get_password(KEYRING_SERVICE, ref)
        except Exception:
            return None

    def delete(self, ref: str) -> None:
        import keyring
        import keyring.errors

        try:
            keyring.delete_password(KEYRING_SERVICE, ref)
        except keyring.errors.PasswordDeleteError:
            # 条目本来就不存在 —— 幂等删除，不算失败
            return


class InMemorySecretBackend:
    """进程内存后端。

    用途有二：**测试**，以及**没有可用 keyring 的环境**（CI、容器、精简系统）。
    不持久化，进程退出即丢 —— 因此生产路径上永远优先走 keyring。
    """

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def set(self, ref: str, secret: str) -> None:
        self._store[ref] = secret

    def get(self, ref: str) -> str | None:
        return self._store.get(ref)

    def delete(self, ref: str) -> None:
        self._store.pop(ref, None)


def _default_backend() -> SecretBackend:
    """选一个密钥后端。**优先级：显式开关 > keyring > 内存**。

    ``AUTOLEARN_SECRET_BACKEND=memory`` 强制走内存后端，用于两种场景：

    1. **测试**：走真实 keyring 的用例会把 ``autolearn/<profile_id>`` 写进
       用户的 Windows 凭据管理器，而 profile_id 每次都是新 uuid ——
       于是**跑一轮测试就在真实凭据库里留一批垃圾**，跑几十轮会把凭据库写满
       （``CredWrite`` 报 ``WinError 8``），连用户自己存密钥都会失败。
       测试夹具必须显式打开这个开关。
    2. **无凭据库的部署**（容器 / 精简系统 / CI）：不想让密码进 OS 钥匙串。

    不给这个开关而依赖「keyring 探测失败才降级」是不够的：在**装了** keyring 的
    开发机上探测一定成功，测试必然污染真实凭据库。
    """
    if os.environ.get(SECRET_BACKEND_ENV, "").strip().lower() == _MEMORY_BACKEND:
        return InMemorySecretBackend()
    try:
        import keyring

        keyring.get_keyring()
    except Exception:
        return InMemorySecretBackend()
    return KeyringSecretBackend()


class CredentialStore:
    """密钥存取。**优先级：环境变量 > 后端（keyring）> 不提供**。"""

    def __init__(self, backend: SecretBackend | None = None) -> None:
        self._backend: SecretBackend = backend if backend is not None else _default_backend()

    def put(self, ref: str, secret: SecretStr) -> None:
        self._backend.set(ref, secret.get_secret_value())

    def get(self, ref: str) -> SecretStr | None:
        from_env = os.environ.get(_env_key_ref(ref))
        if from_env:
            return SecretStr(from_env)
        stored = self._backend.get(ref)
        return SecretStr(stored) if stored else None

    def delete(self, ref: str) -> None:
        self._backend.delete(ref)

    def has(self, ref: str) -> bool:
        return self.get(ref) is not None


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #
class ModelRegistry:
    """多套配置的增删改查 + 排序即降级链。

    ``state/models.yaml`` 是唯一持久化目标；``active_chain()`` 的顺序
    **必须**立刻反映 :meth:`reorder` 的结果。
    """

    def __init__(
        self,
        path: Path = DEFAULT_MODELS_PATH,
        credentials: CredentialStore | None = None,
    ) -> None:
        self._path = Path(path)
        self._credentials = credentials if credentials is not None else CredentialStore()
        self._profiles: ProfileList = []
        self._loaded = False

    # -- 持久化 ------------------------------------------------------------ #
    def load(self) -> None:
        """读 ``models.yaml``。文件不存在视为空注册表，**不报错**。

        老格式（``tier1_model`` / ``tier2_model``）会经
        :func:`migrate_profile_payload` **自动迁移**成单模型，用户无需重配。
        """
        self._loaded = True
        if not self._path.exists():
            self._profiles = []
            return
        raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        entries = raw.get("profiles", []) if isinstance(raw, dict) else (raw or [])
        profiles = [
            ModelProfile.model_validate(migrate_profile_payload(entry)) for entry in entries
        ]
        self._profiles = sorted(profiles, key=lambda p: p.order)

    def save(self) -> None:
        """落盘。只写非敏感字段。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "profiles": [p.model_dump(mode="json") for p in self._profiles],
        }
        self._path.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    # -- 查询 -------------------------------------------------------------- #
    def list(self) -> ProfileList:
        self._ensure_loaded()
        return sorted(self._profiles, key=lambda p: p.order)

    def get(self, profile_id: str) -> ModelProfile | None:
        self._ensure_loaded()
        return next((p for p in self._profiles if p.profile_id == profile_id), None)

    def active_chain(self) -> ProfileList:
        """按 ``order`` 升序返回**启用中**的配置，即降级链。"""
        return [p for p in self.list() if p.enabled]

    def credential(self, profile_id: str) -> SecretStr | None:
        profile = self.get(profile_id)
        if profile is None:
            return None
        return self._credentials.get(profile.api_key_ref)

    def has_credential(self, profile_id: str) -> bool:
        return self.credential(profile_id) is not None

    def credentials(self) -> CredentialStore:
        return self._credentials

    # -- 变更 -------------------------------------------------------------- #
    def add(self, profile: ModelProfile) -> ModelProfile:
        self._ensure_loaded()
        if not profile.profile_id:
            profile = profile.model_copy(update={"profile_id": uuid.uuid4().hex[:12]})
        if any(p.profile_id == profile.profile_id for p in self._profiles):
            raise ValueError(f"profile_id 已存在：{profile.profile_id}")
        if not profile.api_key_ref:
            profile = profile.model_copy(
                update={"api_key_ref": build_api_key_ref(profile.profile_id)}
            )
        profile = profile.model_copy(update={"order": self._next_order()})
        self._profiles.append(profile)
        self.save()
        return profile

    def update(self, profile_id: str, patch: dict[str, Any]) -> ModelProfile:
        """按字段打补丁。

        **语义**：``patch`` 里出现的键才会被应用（显式给 ``None`` 表示「清空该项」）；
        没出现的键保持不变。调用方若不打算改某项，就别把键放进来 ——
        路由层用的是 ``model_dump(exclude_none=True)``。
        """
        self._ensure_loaded()
        current = self.get(profile_id)
        if current is None:
            raise KeyError(profile_id)
        cleaned = {
            key: value
            for key, value in patch.items()
            if key in ModelProfile.model_fields and key != "profile_id"
        }
        updated = current.model_copy(update=cleaned)
        self._profiles = [updated if p.profile_id == profile_id else p for p in self._profiles]
        self.save()
        return updated

    def remove(self, profile_id: str) -> None:
        """删配置，**同步删凭据**，不留孤儿。"""
        self._ensure_loaded()
        current = self.get(profile_id)
        if current is None:
            raise KeyError(profile_id)
        self._profiles = [p for p in self._profiles if p.profile_id != profile_id]
        self._credentials.delete(current.api_key_ref)
        self._renumber()
        self.save()

    def reorder(self, profile_ids: ProfileIdList) -> None:
        """排序即降级链。顺序变更后 :meth:`active_chain` 必须立刻反映。"""
        self._ensure_loaded()
        known = {p.profile_id for p in self._profiles}
        unknown = [pid for pid in profile_ids if pid not in known]
        if unknown:
            raise KeyError(f"未知的 profile_id：{unknown}")
        ranking = {pid: index for index, pid in enumerate(profile_ids)}
        # 未出现在入参里的排在最后，保持其既有相对顺序
        tail = len(ranking)
        self._profiles = [
            p.model_copy(update={"order": ranking.get(p.profile_id, tail)})
            for p in self._profiles
        ]
        self._profiles.sort(key=lambda p: (p.order, p.profile_id))
        self._renumber()
        self.save()

    def mark_disabled(self, profile_id: str, reason: str) -> None:
        """把某套配置摘出降级链，并把原因写进能力报告，便于界面说明。"""
        self._ensure_loaded()
        current = self.get(profile_id)
        if current is None:
            raise KeyError(profile_id)
        report = current.capabilities or CapabilityReport(
            auth_ok=False,
            model_ok=False,
            supports_vision=False,
            supports_structured_output=False,
            error_code=reason,
        )
        updated = current.model_copy(
            update={
                "enabled": False,
                "capabilities": report.model_copy(update={"error_code": reason}),
            }
        )
        self._profiles = [updated if p.profile_id == profile_id else p for p in self._profiles]
        self.save()

    def set_capabilities(self, profile_id: str, report: CapabilityReport) -> ModelProfile:
        """把实测结果回写配置（M2-6：不接受手填）。"""
        return self.update(profile_id, {"capabilities": report})

    # -- 内部 -------------------------------------------------------------- #
    def _next_order(self) -> int:
        return max((p.order for p in self._profiles), default=-1) + 1

    def _renumber(self) -> None:
        self._profiles = [p.model_copy(update={"order": i}) for i, p in enumerate(self._profiles)]


# --------------------------------------------------------------------------- #
# 能力实测
# --------------------------------------------------------------------------- #
def _join(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + "/" + path.lstrip("/")


def _code_of(status: int) -> str:
    if status in (401, 403):
        return "auth_failed"
    if status == 404:
        return "model_not_found"
    if status == 429:
        return "rate_limited"
    return "provider_error"


def _failure(error_code: str, latency_ms: int = 0) -> CapabilityReport:
    return CapabilityReport(
        auth_ok=False,
        model_ok=False,
        supports_vision=False,
        supports_structured_output=False,
        image_payload=None,
        max_qps=0.0,
        latency_ms=latency_ms,
        error_code=error_code,
    )


def _looks_like_vision_rejection(status: int, body: str) -> bool:
    if status not in (400, 422):
        return False
    lowered = body.lower()
    return any(token in lowered for token in ("image", "vision", "visual", "multimodal", "图片"))


def _looks_like_structured_rejection(status: int, body: str) -> bool:
    if status not in (400, 422):
        return False
    lowered = body.lower()
    return any(
        token in lowered
        for token in ("response_format", "json_schema", "json mode", "structured")
    )


def _chat_body(profile: ModelProfile, content: Any, *, json_mode: bool = False) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": profile.model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 16,
        "temperature": 0,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    return body


def _vision_body(profile: ModelProfile, model: str, prompt: str) -> dict[str, Any]:
    """视觉探针的请求体（**不复用** :func:`_chat_body`）。

    两处刻意不同：

    ``max_tokens``
        :func:`_chat_body` 用 16，那是因为它只探「通不通」。视觉探针要**读回答**，
        16 个 token 会被截断成空串 —— 实测某些模型在 16 下返回的 content 就是 ``""``，
        于是「内容校验」形同虚设。**推理类模型还要更多**：它们先花掉预算做思维链，
        留给正文的可能一个 token 都不剩（实测 ``deepseek-reasoner`` 在 64 下正文为空、
        在 256 下正常答出 ``7``）。所以给 256。
    ``model``
        显式传**视觉实际会用的那个模型**，不吃 ``_chat_body`` 的 tier1 默认值。
    """
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": _probe_image_data_uri()}},
                ],
            }
        ],
        "max_tokens": 256,
        "temperature": 0,
    }


def _extract_probe_text(raw: str) -> str:
    """从探针响应里取模型正文。取不到就返回空串（判定会退成弱证据）。"""
    try:
        payload = json.loads(raw)
        choices = payload.get("choices") or []
        message = choices[0].get("message") or {}
        content = message.get("content")
        # 少数实现把思考过程塞进 reasoning_content，正文在 content
        return content if isinstance(content, str) else ""
    except (ValueError, AttributeError, IndexError, TypeError):
        return ""


async def _probe_models(client: httpx.AsyncClient, profile: ModelProfile) -> set[str] | None:
    """取模型清单。端点不存在时返回 ``None``（不算失败）。"""
    response = await client.get(_join(profile.base_url, "models"))
    if response.status_code in (404, 405):
        return None
    response.raise_for_status()
    payload = response.json()
    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return None
    return {
        str(entry.get("id")) for entry in entries if isinstance(entry, dict) and entry.get("id")
    }


async def test_connection(
    profile: ModelProfile,
    credentials: CredentialStore | None = None,
) -> CapabilityReport:
    """[测试连接] 实测能力。**不接受手填**。

    实测项：鉴权 · 模型名 · ``supports_vision`` ·
    ``supports_structured_output`` · ``image_payload`` · **真实 QPS**。
    （2026-09-28 起模型名只有一个，不再分 Tier1 / Tier2 两项。）

    全部请求都打到 ``profile.base_url``，因此可以用本地假端点完整验证，零密钥可跑。

    ``credentials``
        密钥从哪儿读。**调用方应当把 ``ModelRegistry.credentials()`` 传进来** ——
        配置是存进那个 store 的，这里若另起一个 ``CredentialStore()`` 就是「存的
        和读的不是同一个」。真实 keyring 恰好是进程级共享，所以这个错配一直被掩盖；
        换成内存后端（无凭据库的部署 / 测试）就会**每次都报 ``auth_failed``**。
    """
    store = credentials if credentials is not None else CredentialStore()
    key = store.get(profile.api_key_ref)
    if key is None:
        return _failure("no_config")

    headers = {"Authorization": f"Bearer {key.get_secret_value()}"}
    timeout = httpx.Timeout(profile.timeout_s)
    latencies: list[float] = []

    async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
        # ---- 1. 鉴权 + 模型清单 ------------------------------------------ #
        listed: set[str] | None = None
        started = time.perf_counter()
        try:
            listed = await _probe_models(client, profile)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status not in (404, 405):
                # 401/403 与 429 必须原样上报；404/405 只是「没这个端点」
                return _failure(_code_of(status), int((time.perf_counter() - started) * 1000))
        except httpx.HTTPError:
            return _failure("provider_error", int((time.perf_counter() - started) * 1000))
        latencies.append((time.perf_counter() - started) * 1000)

        # ---- 2. 模型可用性：用一次最小 chat 探针实测 ---------------------- #
        started = time.perf_counter()
        try:
            response = await client.post(
                _join(profile.base_url, "chat/completions"),
                json=_chat_body(profile, "ping"),
            )
        except httpx.HTTPError:
            return _failure("provider_error", int((time.perf_counter() - started) * 1000))
        latencies.append((time.perf_counter() - started) * 1000)

        if response.status_code >= 400:
            code = _code_of(response.status_code)
            if listed is not None and profile.model not in listed:
                code = "model_not_found"
            return _failure(code, int(latencies[-1]))

        # /models 清单常常不全（部分厂商只列部分模型），所以**以 chat 探针为准**
        model_ok = True

        # ---- 3. 结构化输出 ---------------------------------------------- #
        structured = True
        started = time.perf_counter()
        try:
            probe = await client.post(
                _join(profile.base_url, "chat/completions"),
                json=_chat_body(profile, "只返回一个 JSON 对象", json_mode=True),
            )
            if probe.status_code >= 400 and _looks_like_structured_rejection(
                probe.status_code, probe.text
            ):
                structured = False
        except httpx.HTTPError:
            structured = False
        latencies.append((time.perf_counter() - started) * 1000)

        # ---- 4. 视觉能力 ------------------------------------------------ #
        # 2026-09-28 起一套配置只有一个模型，所以「图片会发给哪个模型」不再有疑问：
        # 就是 ``profile.model``。原先那段「必须探 tier2_model，否则探针说支持、
        # 跑起来图却发给另一个模型」的顾虑随之消失。
        vision = False
        image_payload: str | None = None
        vision_model = profile.model
        vision_evidence: str | None = None
        started = time.perf_counter()
        try:
            probe = await client.post(
                _join(profile.base_url, "chat/completions"),
                json=_vision_body(
                    profile,
                    vision_model,
                    "只回答图中的数字，不要任何其他文字。",
                ),
            )
            if probe.status_code < 400:
                vision = True
                image_payload = "base64"
                # 只看状态码不够：有的厂商 200 却把图片丢掉，于是「支持视觉」
                # 这句话没有依据。所以再**校验回答内容** —— 探针图里画的是
                # `7`，答对了才算强证据。
                vision_evidence = (
                    "read_digit"
                    if _PROBE_DIGIT in _extract_probe_text(probe.text)
                    else "accepted"
                )
        except httpx.HTTPError:
            vision = False
        latencies.append((time.perf_counter() - started) * 1000)

        # ---- 5. 真实 QPS（串行采样，取保守口径） ------------------------ #
        for _ in range(QPS_SAMPLES):
            started = time.perf_counter()
            try:
                await client.post(
                    _join(profile.base_url, "chat/completions"),
                    json=_chat_body(profile, "ping"),
                )
            except httpx.HTTPError:
                break
            latencies.append((time.perf_counter() - started) * 1000)

    measured = latencies[1:] or latencies  # 第 1 个是 /models，不参与延迟统计
    avg_ms = sum(measured) / len(measured)
    return CapabilityReport(
        auth_ok=True,
        model_ok=model_ok,
        supports_vision=vision,
        supports_structured_output=structured,
        image_payload=image_payload,
        vision_model=vision_model if vision else None,
        vision_evidence=vision_evidence,
        max_qps=round(1000.0 / avg_ms, 3) if avg_ms > 0 else 0.0,
        latency_ms=int(avg_ms),
        error_code=None,
    )
