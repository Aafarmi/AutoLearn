"""课程站点能力表。

**本文件是课程站点的唯一事实源**：哪些站点能做课程任务、支持哪些任务类型、
哪些学习模式、登录提示与允许域名，全部在这里声明。

做题站点不在这里：它们仍由 `TaskStore.supported_site_ids`
（即 `core.tasks.store._discover_site_ids()` 扫描 `core/adapters/` 得出）提供。

硬约束（docs/TASKS.md §4.6 第 1 条）：`core/adapters/` 的站点探测要求
auth.py / crawler.py / solver.py / interaction.py 四文件齐全，因此**课程站点
绝不能注册进 `core/adapters/`**，否则会出现在做题站点下拉里并被
`POST /api/tasks` 当作做题任务接受。新增课程站点 = 只在本文件的 `SITES` 里加一条。

`site_id` 字面量只允许出现在本文件，其它模块一律通过下面的函数取。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SiteCapability:
    """一个课程站点的静态能力声明（不可变，可安全共享）。"""

    site_id: str
    label: str
    task_types: tuple[str, ...]
    modes: tuple[str, ...]
    auth_hint: str
    allowed_domains: tuple[str, ...]


SITES: dict[str, SiteCapability] = {
    "zhihuishu": SiteCapability(
        site_id="zhihuishu",
        label="智慧树",
        task_types=("course",),
        modes=("shared", "hike", "ai"),
        auth_hint="使用智慧树 App 扫描二维码登录。",
        allowed_domains=("zhihuishu.com",),
    ),
}


def sites_for(task_type: str) -> list[SiteCapability]:
    """返回支持该任务类型的课程站点，按 site_id 升序。"""
    if not isinstance(task_type, str):
        return []
    return [
        capability
        for _, capability in sorted(SITES.items())
        if task_type in capability.task_types
    ]


def is_course_site(site_id: str) -> bool:
    """该 site_id 是否是已登记的课程站点（不查做题站点）。"""
    return site_id in SITES


def auth_hint(site_id: str) -> str | None:
    """登录提示；未登记的站点返回 None（调用方负责降级）。"""
    capability = SITES.get(site_id) if isinstance(site_id, str) else None
    return capability.auth_hint if capability is not None else None


def modes_for(site_id: str) -> tuple[str, ...]:
    """该站点支持的学习模式；未登记返回空 tuple。"""
    capability = SITES.get(site_id) if isinstance(site_id, str) else None
    return capability.modes if capability is not None else ()
