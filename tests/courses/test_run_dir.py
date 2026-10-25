"""T4-01 / T4-08 验收：vendor 复制（指纹比对）+ 终端补丁。

铁律：**任何用例都不许在源码树的 vendor 里 import 或写文件**。需要 import 的
用例先把 vendor 复制到 `tmp_path`，再从副本 import——这正是被测代码要做的事，
顺便证明"跑完 vendor 目录里没有新增文件"。
"""

from __future__ import annotations

import importlib
import os
import shutil
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from core.courses.errors import CourseError
from core.courses.providers.zhihuishu import preflight

REAL_VENDOR = Path(preflight._vendor_root())
_VENDOR_MODULES = ("fucker", "utils", "logger", "ObjDict", "zd_utils", "sign", "push")


@pytest.fixture()
def vendor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """一份可写的 vendor 副本（不含 __pycache__ / logs）。"""
    copy = tmp_path / "vendor"
    shutil.copytree(
        REAL_VENDOR, copy, ignore=shutil.ignore_patterns("__pycache__", "logs")
    )
    monkeypatch.setattr(preflight, "_vendor_root", lambda: copy)
    return copy


# --- materialize_vendor -----------------------------------------------------


def test_first_call_copies_all_second_call_copies_nothing(
    vendor: Path, tmp_path: Path
) -> None:
    run = tmp_path / "run"

    first = preflight.materialize_vendor(run)
    second = preflight.materialize_vendor(run)

    assert first == len(preflight.VENDOR_FILES) == 10
    assert second == 0
    assert sorted(p.name for p in (run / "upstream").iterdir()) == sorted(
        preflight.VENDOR_FILES + (preflight.VENDOR_FINGERPRINT_FILE,)
    )


def test_materialize_skips_private_and_user_files(vendor: Path, tmp_path: Path) -> None:
    """上游的 config/cookies/execution/main/images/decrypt 一个都不能进来。"""
    for name in ("config.json", "cookies.json", "execution.json", "main.py"):
        (vendor / name).write_text("{}", encoding="utf-8")
    (vendor / "images").mkdir()
    (vendor / "__pycache__").mkdir()
    run = tmp_path / "run"

    preflight.materialize_vendor(run)

    names = {p.name for p in (run / "upstream").iterdir()}
    assert names == set(preflight.VENDOR_FILES) | {preflight.VENDOR_FINGERPRINT_FILE}


def test_source_change_triggers_recopy(vendor: Path, tmp_path: Path) -> None:
    run = tmp_path / "run"
    preflight.materialize_vendor(run)

    target = vendor / "fucker.py"
    target.write_text(target.read_text(encoding="utf-8") + "\n# touched\n", encoding="utf-8")
    os.utime(target, ns=(target.stat().st_atime_ns, target.stat().st_mtime_ns + 10_000_000))

    assert preflight.materialize_vendor(run) == len(preflight.VENDOR_FILES)
    assert "# touched" in (run / "upstream" / "fucker.py").read_text(encoding="utf-8")


def test_missing_target_file_forces_recopy(vendor: Path, tmp_path: Path) -> None:
    run = tmp_path / "run"
    preflight.materialize_vendor(run)
    (run / "upstream" / "utils.py").unlink()

    assert preflight.materialize_vendor(run) == len(preflight.VENDOR_FILES)
    assert (run / "upstream" / "utils.py").is_file()


def test_prune_removes_junk_even_when_fingerprint_matches(
    vendor: Path, tmp_path: Path
) -> None:
    run = tmp_path / "run"
    preflight.materialize_vendor(run)
    upstream = run / "upstream"
    (upstream / "logs").mkdir()
    (upstream / "__pycache__").mkdir()
    (upstream / "stale.py").write_text("x", encoding="utf-8")

    assert preflight.materialize_vendor(run) == 0

    names = {p.name for p in upstream.iterdir()}
    assert "logs" not in names and "__pycache__" not in names and "stale.py" not in names


def test_missing_vendor_file_raises(vendor: Path, tmp_path: Path) -> None:
    (vendor / "zd_utils.py").unlink()

    with pytest.raises(CourseError) as excinfo:
        preflight.materialize_vendor(tmp_path / "run")
    assert "zd_utils.py" in str(excinfo.value)


# --- prepare_run_dir --------------------------------------------------------


def test_prepare_run_dir_rebuilds_from_scratch(
    vendor: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preflight, "data_path", lambda *parts: tmp_path.joinpath(*parts))
    stale = tmp_path / "courses" / "jobs" / "course-abc" / "run"
    stale.mkdir(parents=True)
    (stale / "logs").mkdir()
    (stale / "leftover.json").write_text("{}", encoding="utf-8")

    run = preflight.prepare_run_dir("course-abc")

    assert run == stale
    assert not (run / "leftover.json").exists() and not (run / "logs").exists()
    assert (run / "upstream" / "fucker.py").is_file()


@pytest.mark.parametrize("bad", ["../escape", "", "a/b", "course abc", None, 7])
def test_prepare_run_dir_rejects_bad_job_id(bad: object) -> None:
    with pytest.raises(ValueError):
        preflight.prepare_run_dir(bad)  # type: ignore[arg-type]


# --- 终端补丁 ---------------------------------------------------------------


def test_patch_terminal_size_survives_pipe() -> None:
    def boom(*args: object, **kwargs: object) -> object:
        raise OSError(22, "Bad file descriptor")

    fake_os = SimpleNamespace(
        get_terminal_size=boom,
        terminal_size=os.terminal_size,
        path=os.path,
        sep=os.sep,
    )
    module = ModuleType("fucker")
    module.os = fake_os  # type: ignore[attr-defined]

    shim = preflight.patch_terminal_size(module)

    assert module.os is shim  # type: ignore[attr-defined]
    assert module.os.get_terminal_size() == (80, 24)  # type: ignore[attr-defined]
    assert module.os.get_terminal_size().columns == 80  # type: ignore[attr-defined]
    # 其余属性必须原样转发，否则 fucker 的 os.path / os.environ 全会炸
    assert module.os.path is os.path  # type: ignore[attr-defined]
    assert module.os.sep == os.sep  # type: ignore[attr-defined]


def test_patch_terminal_size_keeps_real_size_when_available() -> None:
    fake_os = SimpleNamespace(
        get_terminal_size=lambda *args, **kwargs: os.terminal_size((120, 40)),
        terminal_size=os.terminal_size,
    )
    module = ModuleType("fucker")
    module.os = fake_os  # type: ignore[attr-defined]

    preflight.patch_terminal_size(module)

    assert module.os.get_terminal_size() == (120, 40)  # type: ignore[attr-defined]


def test_patch_never_touches_global_os() -> None:
    original = os.get_terminal_size
    module = ModuleType("fucker")
    module.os = os  # type: ignore[attr-defined]

    preflight.patch_terminal_size(module)

    assert os.get_terminal_size is original
    assert module.os is not os  # type: ignore[attr-defined]


# --- 真实 import（在副本里跑，证明源码树不被污染） --------------------------


@pytest.fixture()
def imported_fucker(vendor: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.syspath_prepend(str(vendor))
    for name in _VENDOR_MODULES:
        sys.modules.pop(name, None)
    try:
        module = importlib.import_module("fucker")
        yield module
    finally:
        for name in _VENDOR_MODULES:
            sys.modules.pop(name, None)


def test_vendor_copy_runs_with_patched_terminal(imported_fucker: ModuleType) -> None:
    try:
        real = os.get_terminal_size()
    except OSError:
        real = None

    preflight.patch_terminal_size(imported_fucker)
    size = imported_fucker.os.get_terminal_size()  # type: ignore[attr-defined]

    if real is None:
        assert (size.columns, size.lines) == (80, 24)
    else:
        assert size == real
    # 上游真的按 .columns 取值（fucker.py:448/953/1279）
    assert size.columns - 1 >= 0


def test_import_does_not_pollute_source_vendor(imported_fucker: ModuleType) -> None:
    """import 发生在副本里：源码 vendor 的目录内容一个不多一个不少。"""
    before = sorted(p.name for p in REAL_VENDOR.iterdir())
    preflight.patch_terminal_size(imported_fucker)

    assert sorted(p.name for p in REAL_VENDOR.iterdir()) == before
    assert not (REAL_VENDOR / "logs").exists()
    assert not (REAL_VENDOR / "__pycache__").exists()
    assert imported_fucker.__file__ is not None
    assert Path(imported_fucker.__file__).parent != REAL_VENDOR


def test_ai_course_width_line_works_under_pipe(
    imported_fucker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """复现 fucker.py:1279：真正把全局终端查询打成 OSError，模拟 PIPE。

    `w_lim = os.get_terminal_size().columns-1` 是 AI 课程拿到知识点之后的第一行，
    没有 try；补丁生效后这里必须拿到 79（80-1），而不是 OSError。
    """

    def pipe_error(*args: object, **kwargs: object) -> object:
        raise OSError(22, "Bad file descriptor")

    monkeypatch.setattr(os, "get_terminal_size", pipe_error)
    preflight.patch_terminal_size(imported_fucker)

    w_lim = imported_fucker.os.get_terminal_size().columns - 1  # type: ignore[attr-defined]
    assert w_lim == 79

