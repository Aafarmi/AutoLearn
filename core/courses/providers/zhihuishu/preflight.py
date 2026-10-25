"""智慧树预检 + 运行目录准备（T4-01 / T4-04 / T4-08）。

两件事，都发生在"拉起子进程之前"：

1. **登录态预检** `check_cookies()`：判 CASLOGC 能不能用，失败信息只有"要重新
   登录"，**绝不回显 Cookie 值**。判据与上游 `Fucker.cookies` setter
   （`fucker.py:132-137`）逐条对齐：

   ```python
   self.uuid = json.loads(unquote(cookies["CASLOGC"]))["uuid"]
   ```

2. **运行目录** `materialize_vendor()` / `prepare_run_dir()` / `patch_terminal_size()`：
   把只读 vendor 复制到 `jobs/<job_id>/run/upstream` 再从**副本**import——
   上游 `logger.py` 在 import 时按 `utils.py` 所在目录建 `./logs`，直接在 vendor
   里 import 会把 `logs/` 写进源码树（§4.6 #5）。

为什么必须在副本里跑：上游模块互相 `from utils import ...` 是**顶层绝对导入**，
只能靠"把 vendor 目录放进 sys.path"满足，而 sys.path 一旦指向源码树，写文件也
就落回源码树。副本 + 指纹比对既隔离写入，又不必每次重拷 100KB。
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping
from urllib.parse import unquote_plus

from ....common.fsio import atomic_write_json
from ....paths import data_path, resource_path
from ...errors import CourseAuthRequired, CourseError
from .auth import LOGIN_COOKIE_NAME

#: 只有这几种情况值得区分文案；其余一律按"登录态不可用"处理。
_MISSING = "缺少智慧树登录凭据，请重新登录。"
_MALFORMED = "智慧树登录凭据已损坏或格式不受支持，请重新登录。"

#: 允许被复制进运行目录的 vendor 文件（**白名单**，不是排除表）。
#: config.json / cookies.json / execution.json / main.py / images / decrypt 一律不复制：
#: 前三个是用户产物、后三个是上游的交互入口与资源，课程链路用不到。
VENDOR_FILES: tuple[str, ...] = (
    "fucker.py",
    "ObjDict.py",
    "zd_utils.py",
    "sign.py",
    "utils.py",
    "logger.py",
    "push.py",
    "meta.json",
    "LICENSE",
    "UPSTREAM.md",
)

#: 复制进度的指纹文件（放在副本目录里，跟随副本一起被清理）。
VENDOR_FINGERPRINT_FILE = ".vendor_fingerprint"
VENDOR_FINGERPRINT_VERSION = 1

#: 运行目录里的 vendor 副本目录名。
UPSTREAM_DIR_NAME = "upstream"

#: 终端宽度兜底：80x24 是 POSIX 默认值，也是上游在 PyCharm 下的兜底宽度。
_FALLBACK_TERMINAL_SIZE = (80, 24)

#: job_id 直接参与路径拼接，必须先校验，杜绝 `..` / 分隔符穿越。
_JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def check_cookies(cookies: Mapping[str, str] | None) -> str:
    """校验 Cookie 映射，返回解出来的 uuid；不可用则抛 `CourseAuthRequired`。

    返回值只在后端流转（用于加入 `exitRecod_<uuid>`），**不要**写进事件、
    日志或响应。
    """
    if not isinstance(cookies, Mapping):
        raise CourseAuthRequired(_MISSING)
    value = cookies.get(LOGIN_COOKIE_NAME)
    if not isinstance(value, str) or not value.strip():
        raise CourseAuthRequired(_MISSING)
    try:
        payload = json.loads(unquote_plus(value))
    except (TypeError, ValueError):
        raise CourseAuthRequired(_MALFORMED) from None
    if not isinstance(payload, dict):
        raise CourseAuthRequired(_MALFORMED)
    uuid = payload.get("uuid")
    if not isinstance(uuid, str) or not uuid.strip():
        raise CourseAuthRequired(_MALFORMED)
    return uuid.strip()


# --- 运行目录 ---------------------------------------------------------------


def _vendor_root() -> Path:
    """只读 vendor 的绝对路径（源码树内，永不写入）。"""
    return resource_path(
        "core", "courses", "providers", "zhihuishu", "vendor", "fuckZHS"
    )


def vendor_fingerprint(vendor_root: Path | None = None) -> dict[str, list[int]]:
    """白名单文件的 `(size, mtime_ns)` 指纹；文件缺失时按空指纹处理。

    mtime 用 `st_mtime_ns` 而不是 `st_mtime`：后者是 float，秒级精度下同一秒内
    的两次改动会被判成"没变"，然后一直用旧副本。
    """
    root = vendor_root if vendor_root is not None else _vendor_root()
    fingerprint: dict[str, list[int]] = {}
    for name in VENDOR_FILES:
        try:
            stat = (root / name).stat()
        except OSError:
            continue
        fingerprint[name] = [stat.st_size, stat.st_mtime_ns]
    return fingerprint


def materialize_vendor(run_dir: str | Path) -> int:
    """把 vendor 白名单复制到 `<run_dir>/upstream`，返回本次复制的文件数。

    指纹（白名单文件的 size + mtime_ns）与副本里的 `.vendor_fingerprint` 一致、
    且副本文件齐全时**零复制**——第二次调用返回 0，这是验收口径。

    复制用 `copy2`（保留 mtime），因此副本文件的指纹与源一致，下次可以直接比源。
    `__pycache__` 之类不在白名单里的东西由 `_prune_upstream()` 顺手清掉。
    """
    run = Path(run_dir)
    upstream = run / UPSTREAM_DIR_NAME
    upstream.mkdir(parents=True, exist_ok=True)
    source = _vendor_root()
    if not source.is_dir():
        raise CourseError("课程运行环境（vendor）缺失，请重新安装。", 500)

    plan = vendor_fingerprint(source)
    missing = [name for name in VENDOR_FILES if name not in plan]
    if missing:
        raise CourseError(f"课程运行环境缺少文件：{', '.join(missing)}。", 500)

    if _read_fingerprint(upstream) == plan and all(
        (upstream / name).is_file() for name in VENDOR_FILES
    ):
        _prune_upstream(upstream)
        return 0

    for name in VENDOR_FILES:
        shutil.copy2(source / name, upstream / name)
    _prune_upstream(upstream)
    atomic_write_json(
        upstream / VENDOR_FINGERPRINT_FILE,
        {"version": VENDOR_FINGERPRINT_VERSION, "files": plan},
    )
    return len(VENDOR_FILES)


def _read_fingerprint(upstream: Path) -> dict[str, list[int]] | None:
    """读副本指纹；缺失/损坏/版本不符一律当作"要重拷"，不报错。"""
    try:
        raw = json.loads((upstream / VENDOR_FINGERPRINT_FILE).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("version") != VENDOR_FINGERPRINT_VERSION:
        return None
    files = raw.get("files")
    if not isinstance(files, dict):
        return None
    result: dict[str, list[int]] = {}
    for name, value in files.items():
        if (
            isinstance(name, str)
            and isinstance(value, list)
            and len(value) == 2
            and all(isinstance(item, int) and not isinstance(item, bool) for item in value)
        ):
            result[name] = [value[0], value[1]]
    return result


def _prune_upstream(upstream: Path) -> None:
    """删掉副本里不属于白名单的东西（`__pycache__`、旧版本残留）。

    只处理副本目录：源 vendor 是只读的，任何情况下都不动。
    """
    keep = set(VENDOR_FILES) | {VENDOR_FINGERPRINT_FILE}
    try:
        entries = list(upstream.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name in keep:
            continue
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except OSError:
            # 副本清不干净不影响运行，下次 materialize 还会再试。
            continue


def prepare_run_dir(job_id: str) -> Path:
    """准备一个干净的运行目录：`<data>/courses/jobs/<job_id>/run`，并复制 vendor。

    每次都整目录重建——上一次残留的 `logs/`、`*.json` 产物、半截文件不能带进
    下一次运行，否则"跑完目录里有什么"这件事就不可预测了。
    """
    if not isinstance(job_id, str) or not _JOB_ID_PATTERN.fullmatch(job_id):
        raise ValueError("job_id has an invalid format")
    run = data_path("courses", "jobs", job_id, "run")
    if run.exists():
        shutil.rmtree(run)
    run.mkdir(parents=True, exist_ok=True)
    materialize_vendor(run)
    return run


# --- 终端补丁 ---------------------------------------------------------------


class _TerminalOsShim:
    """只替换 `get_terminal_size` 的 os 代理，其余属性原样转发。

    `fucker.py:448/953/1279` 直接 `os.get_terminal_size().columns`，没有任何
    try；子进程的 stdout 是管道时必然 `OSError`。在 AI 课程里这行正好卡在拿到
    知识点之后、遍历之前，不补就是"永远进不去知识点遍历"。
    """

    __slots__ = ("_os",)

    def __init__(self, real_os: Any) -> None:
        object.__setattr__(self, "_os", real_os)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._os, name)

    def get_terminal_size(self, *args: Any, **kwargs: Any) -> Any:
        """有真终端就返回真实尺寸，管道下退回 80x24。"""
        try:
            return self._os.get_terminal_size(*args, **kwargs)
        except (OSError, ValueError):
            return self._os.terminal_size(_FALLBACK_TERMINAL_SIZE)


def patch_terminal_size(module: ModuleType | Any) -> Any:
    """把 `module.os` 换成终端代理，返回该代理。

    只改**这个模块**的名字绑定（`fucker.os`），因此：

    - `utils.py` 里的 `os.get_terminal_size()` 不受影响（它自己已有 OSError 兜底，
      而且真正的进度输出由 bridge 替换掉）；
    - 全局 `os.get_terminal_size` 一行未动，做题链路与其它模块的行为不变。

    必须在 `import fucker` **之后**调用：模块对象先存在，才能覆盖它的 `os`。
    """
    real_os = getattr(module, "os", os)
    shim = _TerminalOsShim(real_os)
    module.os = shim
    return shim
