"""One-time, rollback-safe migration of legacy project data to user storage."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid

from core.paths import _legacy_data_root, data_path


_MIGRATION_VERSION = 1
_MAX_CONFIG_BYTES = 4 * 1024 * 1024


class DataMigrationError(RuntimeError):
    """Safe migration error that does not include user configuration values."""


class _MigrationLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.stream = None

    def __enter__(self) -> "_MigrationLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        try:
            self.stream.seek(0, os.SEEK_END)
            if self.stream.tell() == 0:
                self.stream.write(b"\0")
                self.stream.flush()
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            self.stream.close()
            self.stream = None
            raise DataMigrationError("另一个 AutoLearn 实例正在迁移本地数据。") from None
        return self

    def __exit__(self, *_: object) -> None:
        if self.stream is None:
            return
        try:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self.stream.close()
            self.stream = None


def _validate_legacy_config(path: Path) -> None:
    try:
        if path.stat().st_size > _MAX_CONFIG_BYTES:
            raise DataMigrationError("旧配置文件过大；迁移未修改任何数据。")
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8-sig"))
    except DataMigrationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise DataMigrationError("旧配置不是有效 UTF-8 JSON；迁移未修改任何数据。") from None
    if not isinstance(value, dict):
        raise DataMigrationError("旧配置顶层必须是 JSON 对象；迁移未修改任何数据。")
    for section_name in ("chaoxing", "cjyuketang"):
        section = value.get(section_name)
        if section is not None and not isinstance(section, dict):
            raise DataMigrationError("旧配置字段类型无效；迁移未修改任何数据。")
        if isinstance(section, dict):
            for field in ("url", "cookie"):
                if field in section and not isinstance(section[field], str):
                    raise DataMigrationError("旧配置字段类型无效；迁移未修改任何数据。")
    model = value.get("model")
    if model is not None and not isinstance(model, dict):
        raise DataMigrationError("旧配置字段类型无效；迁移未修改任何数据。")
    if "model_profiles" in value and not isinstance(value["model_profiles"], list):
        raise DataMigrationError("旧配置字段类型无效；迁移未修改任何数据。")
    try:
        from core.settings.model_profiles import ModelProfileError, ModelProfileStore

        ModelProfileStore(path).list_profiles()
    except ModelProfileError:
        raise DataMigrationError("旧配置中的模型档案无效；迁移未修改任何数据。") from None
    except Exception:
        raise DataMigrationError("无法校验旧配置；迁移未修改任何数据。") from None


def _reject_special_files(source: Path) -> None:
    if source.is_symlink() or getattr(source, "is_junction", lambda: False)():
        raise DataMigrationError("旧数据目录包含符号链接；迁移未修改任何数据。")
    if source.is_dir():
        for root, directories, files in os.walk(source, followlinks=False):
            directories.sort()
            files.sort()
            current = Path(root)
            for name in (*directories, *files):
                entry = current / name
                if entry.is_symlink() or getattr(entry, "is_junction", lambda: False)():
                    raise DataMigrationError("旧数据目录包含链接；迁移未修改任何数据。")
                if not entry.is_dir() and not entry.is_file():
                    raise DataMigrationError("旧数据目录包含不支持的文件类型。")
    elif not source.is_file():
        raise DataMigrationError("旧数据项不是普通文件或目录。")


def _tree_digest(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_file():
        digest.update(b"F\0")
        digest.update(path.read_bytes())
        return digest.hexdigest()
    for root, directories, files in os.walk(path, followlinks=False):
        directories.sort()
        files.sort()
        current = Path(root)
        relative_root = current.relative_to(path).as_posix()
        for name in sorted(directories):
            digest.update(f"D\0{relative_root}/{name}\0".encode("utf-8"))
        for name in sorted(files):
            file_path = current / name
            relative = file_path.relative_to(path).as_posix()
            digest.update(f"F\0{relative}\0".encode("utf-8"))
            with file_path.open("rb") as stream:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
    return digest.hexdigest()


def _same_tree(first: Path, second: Path) -> bool:
    try:
        return _tree_digest(first) == _tree_digest(second)
    except OSError:
        return False


def _copy_to_stage(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination, copy_function=shutil.copy2)
    else:
        shutil.copy2(source, destination)


def _secure_config(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        if os.name != "nt":
            raise


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _component_pairs(legacy_root: Path) -> list[tuple[str, Path, Path]]:
    return [
        ("config", legacy_root / "config.json", data_path("config.json")),
        ("runs", legacy_root / "runs", data_path("runs")),
        ("logs", legacy_root / "logs", data_path("logs")),
    ]


def ensure_user_data_migrated() -> None:
    """Back up and move legacy config, run packages, and logs exactly once.

    Original files remain in place as a rollback source. New destinations are
    installed atomically; any error removes only paths created by this attempt.
    Conflicting destinations are never merged or overwritten.
    """
    user_root = data_path()
    user_root.mkdir(parents=True, exist_ok=True)
    lock_path = data_path(".migration.lock")
    marker_path = data_path(".migration-v1.json")
    with _MigrationLock(lock_path):
        if marker_path.exists():
            try:
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                raise DataMigrationError("本地迁移状态损坏；原数据未修改。") from None
            if not isinstance(marker, dict) or marker.get("version") != _MIGRATION_VERSION:
                raise DataMigrationError("本地迁移状态版本无效；原数据未修改。")
            return

        legacy_root = _legacy_data_root()
        pairs = _component_pairs(legacy_root)
        active: list[tuple[str, Path, Path]] = []
        for name, source, target in pairs:
            if not source.exists():
                continue
            _reject_special_files(source)
            if name == "config":
                _validate_legacy_config(source)
            if target.exists():
                _reject_special_files(target)
                if not _same_tree(source, target):
                    raise DataMigrationError(
                        f"用户数据目录中的 {name} 与旧数据冲突；请先手动备份并处理冲突。"
                    )
            active.append((name, source, target))

        attempt_id = uuid.uuid4().hex
        stage_root = data_path(".migration-stage", attempt_id)
        backup_root = data_path("backups", "legacy-v1")
        created_targets: list[Path] = []
        created_backups: list[Path] = []
        committed_marker = False
        try:
            stage_root.mkdir(parents=True, exist_ok=False)
            for name, source, target in active:
                backup = backup_root / name
                if backup.exists():
                    if not _same_tree(source, backup):
                        raise DataMigrationError("旧数据备份目录存在冲突；迁移已回滚。")
                else:
                    backup_stage = stage_root / f"backup-{name}"
                    _copy_to_stage(source, backup_stage)
                    if not _same_tree(source, backup_stage):
                        raise DataMigrationError("旧数据备份校验失败；迁移已回滚。")
                    if name == "config":
                        _secure_config(backup_stage)
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    os.rename(backup_stage, backup)
                    created_backups.append(backup)

                if target.exists():
                    continue
                staged = stage_root / f"data-{name}"
                _copy_to_stage(source, staged)
                if not _same_tree(source, staged):
                    raise DataMigrationError("迁移数据校验失败；迁移已回滚。")
                if name == "config":
                    _secure_config(staged)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.rename(staged, target)
                created_targets.append(target)

            marker_stage = stage_root / "migration-marker.json"
            marker_stage.write_text(
                json.dumps(
                    {
                        "version": _MIGRATION_VERSION,
                        "completed_at": datetime.now(timezone.utc)
                        .isoformat(timespec="seconds")
                        .replace("+00:00", "Z"),
                    },
                    ensure_ascii=True,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            os.rename(marker_stage, marker_path)
            committed_marker = True
        except DataMigrationError:
            raise
        except Exception:
            raise DataMigrationError("迁移失败；本次新增内容已回滚，旧数据仍保留。") from None
        finally:
            if not committed_marker:
                for path in reversed(created_targets):
                    try:
                        _remove_path(path)
                    except OSError:
                        pass
                for path in reversed(created_backups):
                    try:
                        _remove_path(path)
                    except OSError:
                        pass
            try:
                shutil.rmtree(stage_root, ignore_errors=True)
                stage_parent = stage_root.parent
                if stage_parent.exists() and not any(stage_parent.iterdir()):
                    stage_parent.rmdir()
            except OSError:
                pass


__all__ = ["DataMigrationError", "ensure_user_data_migrated"]
