"""目标采集路由（P11）：扫描电脑上可抓的目标、接管启动浏览器。

两条路由刻意分开，因为它们**副作用不同**：

``GET /api/targets``
    纯只读。用户点一下「抓取网页」就会调它，必须可以反复调、不改变任何东西。
``POST /api/targets/launch``
    有副作用 —— 真的会拉起一个浏览器进程。所以是 POST，且在运行中拒绝。

为什么不在 ``/api/run/config`` 里顺手把目标列表带出来
---------------------------------------------------
配置是「运行前设定、运行中只读」的，而目标列表是**易变的外部状态**：
用户随时可能开个新标签页、关掉一个程序。把易变状态塞进配置响应，会让前端
缓存出一个「刚才有、现在没了」的列表，附加时才发现目标已经关了。分开取，语义清楚。

扫描按 ``kind`` 分派
--------------------
``browser_page`` → 枚举浏览器标签页（走 CDP）
``desktop_window`` → 枚举正在运行的程序窗口（走 Win32）

两者的「可用性」判定完全不同（前者要求调试端口活着，后者只要系统支持），
所以 :func:`_snapshot` 把差异收在一处，路由只管转述。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, status

from core.config import RunConfig
from core.enums import ErrorCode, TargetKind
from target.base import TargetSource, TargetUnavailableError
from target.browsers import BrowserTargetSource
from ui.assembly import make_browser_source, make_source
from ui.deps import get_run_config, is_running
from ui.schemas import TargetLaunchOut, TargetListOut

__all__ = ["router"]

router = APIRouter(prefix="/api/targets", tags=["targets"])

#: 端点不通时给的话。**逐字写清怎么做**，因为这是本功能最高频的初始状态。
_NO_ENDPOINT_HINT = (
    "调试端口 {port} 上没有浏览器。请点「接管启动浏览器」—— "
    "它会用**独立的专用 Profile** 拉起 {channel} 并开好调试端口。"
    "专用 Profile 是为了绕开 Chrome 136+ 的一条限制：为浏览器**默认**数据目录"
    "开调试端口时，Chrome 会**静默忽略**该开关（不报错，端口就是不开）。"
    "首次使用需要在这个专用浏览器里登录一次，之后长期保留。"
)

_DESKTOP_HINT = (
    "只列出**可见且未最小化**的窗口 —— 最小化的窗口截不到画面。"
    "如果找不到你的程序，把它从最小化恢复出来再点一次「抓取窗口」。"
)


async def _snapshot(source: TargetSource, cfg: RunConfig) -> TargetListOut:
    """取一份当前状态。取不到目标时**不报错**，而是回一份带 hint 的空列表。"""
    targets = await source.list_targets()
    alive = bool(targets)
    hint: str | None = None
    endpoint: str | None = None

    if isinstance(source, BrowserTargetSource):
        endpoint = source.endpoint
        endpoint_alive = await source.is_endpoint_alive()
        alive = endpoint_alive
        if not endpoint_alive:
            hint = _NO_ENDPOINT_HINT.format(port=source.port, channel=source.channel)
        elif not targets:
            hint = (
                f"已连上 {endpoint}，但里面没有可抓的网页标签页。"
                "请在那个浏览器窗口里打开你要抓的页面，再点一次「抓取网页」。"
            )
    elif not targets:
        hint = _DESKTOP_HINT

    return TargetListOut(
        kind=source.kind,
        endpoint=endpoint,
        alive=alive,
        targets=targets,
        hint=hint,
    )


@router.get("", response_model=TargetListOut)
async def list_targets(
    kind: TargetKind | None = Query(default=None, description="留空则用运行配置里的目标类型"),
) -> TargetListOut:
    """列出当前可抓的目标。**只读**，可反复调用。"""
    cfg = get_run_config()
    if kind is not None:
        cfg = cfg.model_copy(update={"target_kind": kind})
    return await _snapshot(make_source(cfg), cfg)


@router.post("/launch", response_model=TargetLaunchOut)
async def launch_browser(request: Request) -> TargetLaunchOut:
    """**一次做完接管启动的全部前置**：建 Profile → 起进程 → 等端口 → 等出标签页。

    只对浏览器目标有意义 —— 原生程序是用户自己开的，没有「启动前置」这回事。

    运行中拒绝：半路换浏览器会让正在跑的运行丢掉页面。
    """
    if is_running():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error_code": "run_locked", "message": "运行中不能切换目标，请先停止"},
        )

    cfg = get_run_config()
    if cfg.target_kind is TargetKind.DESKTOP_WINDOW:
        # v0.2.0 删掉了 ``TARGET_CHANNEL_UNSUPPORTED``（「通道不支持」这个概念随
        # 双通道一起消失），改用 ``TARGET_UNAVAILABLE``：对**这条接口**而言，
        # 程序窗口就是「没有可接管启动的目标」，语义比翻出一个已删的码准确。
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": ErrorCode.TARGET_UNAVAILABLE.value,
                "message": "当前目标类型是「应用程序窗口」，它由你自己启动，无需接管启动。",
            },
        )

    source = make_browser_source(cfg)
    launched = False
    # 已经有一个带端口的浏览器在跑就直接用 —— 反复点「接管启动」不该越开越多。
    if not await source.is_endpoint_alive():
        # 起始页用控制台自己域名下的引导页：保证「扫到的第一个标签页是**有意义**的」，
        # 而不是一个空白新标签页 —— 用户分不清「没扫到」和「我还没开页面」，
        # 正是之前反馈的问题之一。
        landing = str(request.base_url).rstrip("/") + "/static/landing.html"
        try:
            await source.launch_and_wait(start_urls=[landing])
        except TargetUnavailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error_code": ErrorCode.BROWSER_NO_DEBUG_PORT.value,
                    "message": str(exc),
                },
            ) from exc
        launched = True

    snapshot = await _snapshot(source, cfg)
    # 不要 ``TargetLaunchOut(**snapshot.model_dump())`` 来回 dump：``TargetInfo.channels``
    # 是 ``computed_field``，序列化时会带出来，但**反序列化回模型**时它不在字段表里，
    # 撞上 ``extra="forbid"`` 就是 ``targets.0.channels extra_forbidden``。
    # 直接把原对象递过去（复用同一批 ``TargetInfo``），绕开这轮无意义的重新校验。
    return TargetLaunchOut(
        kind=snapshot.kind,
        endpoint=snapshot.endpoint,
        alive=snapshot.alive,
        targets=snapshot.targets,
        hint=snapshot.hint,
        launched=launched,
    )
