"""Explicit locations for bundled, read-only resources and per-user data."""

from __future__ import annotations

import os
from pathlib import Path
import sys


def _resource_root() -> Path:
    bundled_root = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False):
        if isinstance(bundled_root, (str, os.PathLike)):
            return Path(bundled_root).resolve()
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def _data_root() -> Path:
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data and Path(local_app_data).is_absolute():
            return Path(local_app_data) / "AutoLearn"
        return Path.home() / "AppData" / "Local" / "AutoLearn"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "AutoLearn"
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home and Path(xdg_data_home).is_absolute():
        return Path(xdg_data_home) / "autolearn"
    return Path.home() / ".local" / "share" / "autolearn"


def _joined_path(root: Path, parts: tuple[str | os.PathLike[str], ...]) -> Path:
    root = root.resolve()
    candidate = root.joinpath(*(Path(part) for part in parts)).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("path must stay within its configured root")
    return candidate


def resource_path(*parts: str | os.PathLike[str]) -> Path:
    """Return a path inside the application's bundled, read-only resources."""
    return _joined_path(_resource_root(), parts)


def data_path(*parts: str | os.PathLike[str]) -> Path:
    """Return a path inside the current user's writable AutoLearn data area."""
    return _joined_path(_data_root(), parts)


def _legacy_data_root() -> Path:
    """Locate the old source/install directory for one-time data migration."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return _resource_root()


__all__ = ["data_path", "resource_path"]
