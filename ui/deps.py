"""服务容器（P5）。

四个访问器把「谁持有什么」集中到一处，路由层不自己 new 对象。
全部走 FastAPI 依赖注入，测试里可用 ``dependency_overrides`` 整体替换；
:func:`reset_state` 供测试在用例之间清干净单例。

运行态一共三样东西需要跨请求共享：运行配置、模型注册表、事件总线，
外加「当前编排器 + 当前 run_id」。其余一律不进这里。
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.config import RunConfig, load_run_config, save_run_config
from core.model_registry import DEFAULT_MODELS_PATH, ModelRegistry
from core.trace import EventBus
from ui.store import TaskStore

if TYPE_CHECKING:  # pragma: no cover
    from core.orchestrator import Orchestrator, RunDeps

__all__ = [
    "MODELS_PATH_ENV",
    "OrchestratorNotRunningError",
    "get_current_run_id",
    "get_event_bus",
    "get_orchestrator",
    "get_registry",
    "get_run_config",
    "get_run_deps_factory",
    "get_store",
    "is_running",
    "reset_state",
    "set_current_run_id",
    "set_orchestrator",
    "set_run_config",
    "set_run_deps_factory",
]

#: 覆盖 ``models.yaml`` 路径的环境变量（测试与多实例部署用）
MODELS_PATH_ENV = "AUTOLEARN_MODELS_PATH"


class OrchestratorNotRunningError(RuntimeError):
    """当前没有活跃运行。路由层统一转成 409，而不是 500。"""

    def __init__(self) -> None:
        super().__init__("当前没有进行中的运行")


_lock = threading.Lock()
_registry: ModelRegistry | None = None
_event_bus: EventBus | None = None
_run_config: RunConfig | None = None
_orchestrator: Orchestrator | None = None
_current_run_id: str | None = None
_store: TaskStore | None = None
#: 运行依赖装配工厂。默认走 ``ui.assembly.build_run_deps``；
#: 测试可替换成「不碰浏览器」的版本 —— 否则每个 ``POST /api/run/start``
#: 的用例都会真的拉起一个浏览器进程。
_run_deps_factory: Callable[..., Any] | None = None


def _models_path() -> Path:
    override = os.environ.get(MODELS_PATH_ENV)
    return Path(override) if override else DEFAULT_MODELS_PATH


def get_registry() -> ModelRegistry:
    """进程唯一 :class:`ModelRegistry`。惰性 ``load()``，首次访问才读盘。"""
    global _registry
    with _lock:
        if _registry is None:
            registry = ModelRegistry(path=_models_path())
            registry.load()
            _registry = registry
        return _registry


def get_event_bus() -> EventBus:
    """进程唯一 :class:`EventBus`，SSE 的事件源。"""
    global _event_bus
    with _lock:
        if _event_bus is None:
            _event_bus = EventBus()
        return _event_bus


def get_store() -> TaskStore:
    """进程唯一 :class:`TaskStore`（SQLite + 留痕目录的只读投影）。"""
    global _store
    with _lock:
        if _store is None:
            _store = TaskStore()
        return _store


def get_run_config() -> RunConfig:
    """当前运行配置。**运行中只读**，由路由层判断是否 409。"""
    global _run_config
    with _lock:
        if _run_config is None:
            _run_config = load_run_config()
        return _run_config


def set_run_config(cfg: RunConfig) -> None:
    """更新内存配置并落盘。"""
    global _run_config
    with _lock:
        _run_config = cfg
    save_run_config(cfg)


def get_orchestrator() -> Orchestrator:
    """当前编排器。未启动时抛 :class:`OrchestratorNotRunningError`（→ 409）。"""
    with _lock:
        if _orchestrator is None:
            raise OrchestratorNotRunningError()
        return _orchestrator


def set_orchestrator(orchestrator: Orchestrator | None) -> None:
    """``POST /api/run/start`` / ``stop`` 时挂载或卸载编排器。"""
    global _orchestrator, _current_run_id
    with _lock:
        _orchestrator = orchestrator
        if orchestrator is None:
            _current_run_id = None


def is_running() -> bool:
    with _lock:
        return _orchestrator is not None


def get_current_run_id() -> str | None:
    with _lock:
        return _current_run_id


def set_current_run_id(run_id: str | None) -> None:
    global _current_run_id
    with _lock:
        _current_run_id = run_id


def reset_state() -> None:
    """清空全部单例。**仅测试与 ``--reload`` 使用**。"""
    global _registry, _event_bus, _run_config, _orchestrator, _current_run_id, _store
    global _run_deps_factory
    with _lock:
        _registry = None
        _event_bus = None
        _run_config = None
        _orchestrator = None
        _current_run_id = None
        _store = None
        _run_deps_factory = None


def get_run_deps_factory() -> Callable[..., RunDeps]:
    """取运行依赖装配工厂（默认 ``ui.assembly.build_run_deps``）。

    惰性 import ``ui.assembly``：它要拉感知 / 求解 / 执行三个包，
    而只有「真启动一次运行」才需要它们 —— 让 ``/api/health`` 这类路由
    不必为此多付一次导入。
    """
    with _lock:
        if _run_deps_factory is not None:
            return _run_deps_factory
    from ui.assembly import build_run_deps

    return build_run_deps


def set_run_deps_factory(factory: Callable[..., RunDeps] | None) -> None:
    """替换装配工厂。**仅测试与嵌入式部署使用**。"""
    global _run_deps_factory
    with _lock:
        _run_deps_factory = factory
