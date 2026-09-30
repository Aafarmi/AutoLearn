"""引导与空状态文案（M2-8 / 任务书 §3.2）。

硬性要求：**空状态 / 鉴权失败 / 模型名不存在 / 限流 四类情形，各自独立文案，
不得共用一句泛化错误提示。**

每张卡都必须给出**明确的下一步动作**——启动键置灰是引导，不是静默失败。

v0.2.0 起读题只有「模型看截图」一条路，所以卡片里的建议一律是**模型侧可执行**
的动作（新增/启用一套支持视觉的配置、降低采样数、检查目标能不能截屏）；
原先「关掉模型优先改用 DOM 优先」这类建议已不成立，因为它们描述的那条路没有了。
"""

from __future__ import annotations

from core.enums import ErrorCode
from ui.schemas import GuideCardOut

__all__ = [
    "GUIDE_AUTH_FAILED",
    "GUIDE_EMPTY",
    "GUIDE_FIRST_RUN",
    "GUIDE_MODEL_NOT_FOUND",
    "GUIDE_RATE_LIMITED",
    "GUIDE_STRUCTURED_UNSUPPORTED",
    "GUIDE_VISION_UNSUPPORTED",
    "guide_for",
]

GUIDE_FIRST_RUN = GuideCardOut(
    title="还没有模型配置 —— 现在不能启动",
    body_md=(
        "v0.2.0 起读题只有一条路：**截当前视口 → 交给模型看**。"
        "一条可用配置都没有时，启动检查会直接拦下（`no_config` + 400），"
        "**不会**退回 Mock 空跑 —— 发一个跑不动的 run，只会让人以为是自己选错了目标。\n\n"
        "（历史对照：早期版本在没有模型时会落 **MockProvider**，读靶场地面真值把流程跑通。"
        "那条路现在只留给自检脚本与单测，不再是产品路径。）\n\n"
        "要接真实模型，需要准备三样东西：\n\n"
        "1. 一个 OpenAI 兼容的 `base_url`（多数厂商都兼容这个协议）\n"
        "2. 一个 API Key\n"
        "3. 一个**支持图片输入**的模型名 —— 读题必须看图，纯文本模型跑不起来\n\n"
        "填完点 **[测试连接]**，系统会实测鉴权、模型名可用性、是否支持视觉与"
        "结构化输出、以及真实 QPS —— 这些结果由实测回写，不接受手填。"
    ),
    next_action="打开②模型配置面板 → 点「新增配置」→ 填完后点「测试连接」",
    doc_url=None,
)

GUIDE_EMPTY = GuideCardOut(
    title="还没有可运行的任务",
    body_md=(
        "任务列表是空的。运行前需要在①运行配置区选定任务序列"
        "（刷题 / 网课 / 两者混合）。\n\n"
        "如果是刚刚结束了一轮运行，列表为空说明本次运行没有产出任何条目 —— "
        "先去看⑤日志流里有没有 `run.error`。"
    ),
    next_action="回到①运行配置 → 勾选任务序列 → 点「启动」",
    doc_url=None,
)

GUIDE_AUTH_FAILED = GuideCardOut(
    title="鉴权失败（401 / 403）",
    body_md=(
        "厂商拒绝了这次请求。按可能性从高到低排查：\n\n"
        "1. **API Key 打错了或已失效** —— 重新复制一次，注意别带多余空格或换行\n"
        "2. **Key 与 base_url 不匹配** —— 比如拿 A 厂商的 Key 去请求 B 厂商的地址\n"
        "3. **Key 没有该模型的权限** —— 部分厂商需要单独开通模型\n\n"
        "系统只在 OS 凭据管理器里保存密钥，界面上不会回显明文；"
        "重新保存会直接覆盖旧值。"
    ),
    next_action="打开②模型配置 → 编辑该配置 → 重填 API Key → 点「测试连接」",
    doc_url=None,
)

GUIDE_MODEL_NOT_FOUND = GuideCardOut(
    title="模型名不存在（404）",
    body_md=(
        "鉴权通过了，但厂商说找不到这个模型。常见原因：\n\n"
        "- **模型名写错**：多数厂商要求带前缀，例如 `qwen-plus` 而不是 `Qwen-Plus`\n"
        "- **该模型未在这个账号下开通**\n"
        "- **base_url 少了或多了 `/v1`** —— 两种写法都常见，切换试一次往往就对了\n\n"
        "点 [测试连接] 会实测**这一套配置里的那一个**模型名（一套配置一个模型）"
        "与 base_url 是否对得上，坏在哪一环会直接指出。"
    ),
    next_action="编辑该配置 → 核对模型名与 base_url → 点「测试连接」",
    doc_url=None,
)

GUIDE_RATE_LIMITED = GuideCardOut(
    title="触发限流（429）",
    body_md=(
        "厂商按分钟/按天限速，当前请求被挡下。\n\n"
        "系统已经做的事：自动降速、按指数退避重试，并保持请求级并发不超上限"
        "（免费档 ≤2、付费档 ≤3）。\n\n"
        "你可以做的：\n\n"
        "- 等一下再继续（倒计时结束后自动恢复）\n"
        "- 调小 `sample_n`（采样数越少，单位时间请求越少）\n"
        "- 在②模型配置里把更抗限流的那套配置排到降级链前面（↑ / ↓ 按钮）\n"
        "- 检查「视觉备用」是不是在替主视觉反复重试 —— 主模型被限流时备用组会顶上，"
        "一组限流就变成两组在打，等于请求翻倍"
    ),
    next_action="等待倒计时结束后重试；或调低采样数、把更抗限流的配置排到降级链前面",
    doc_url=None,
)

GUIDE_VISION_UNSUPPORTED = GuideCardOut(
    title="当前模型不支持图片输入",
    body_md=(
        "v0.2.0 起页面**只由模型读**：截图 → 模型 → 题干、选项与坐标。"
        "当前配置实测 `supports_vision = false`，这套模型拿到图也读不出题，"
        "跑起来只会一路停在「读不到题目」。\n\n"
        "三条路，任选一条：\n\n"
        "1. 换一套支持视觉的模型：在②模型配置里新增或编辑，点 [测试连接] 确认"
        "「支持视觉」这一项亮起\n"
        "2. 读图与解题分开：把这套纯文本模型选成**解题组**，把支持视觉的那套选成**视觉组**\n"
        "3. 给视觉组加**视觉备用**：主视觉整体失败时按勾选顺序顶上\n\n"
        "判据一律来自实测，不接受手填 —— 手填的「支持视觉」会在第一次读题时被打回。"
    ),
    next_action="新增或启用一套支持视觉的模型配置 → 选进「视觉组」→ 点「测试连接」确认",
    doc_url=None,
)

GUIDE_STRUCTURED_UNSUPPORTED = GuideCardOut(
    title="模型不支持 JSON Schema 结构化输出",
    body_md=(
        "已自动降级为**文本解析**：让模型按约定格式吐文本，再由本地解析。\n\n"
        "这不会中断运行，但解析失败率会略高。若发现「解析失败」频繁出现，"
        "建议换一个支持 `response_format` 的模型。"
    ),
    next_action="无需操作，可直接继续；若解析失败频繁再考虑换模型",
    doc_url=None,
)

#: 错误码 → 引导卡。**没有兜底通用文案** —— 未登记的码返回 ``None``，
#: 迫使调用方显式处理，而不是悄悄弹一句泛化的错误。
_GUIDES: dict[str, GuideCardOut] = {
    ErrorCode.NO_CONFIG.value: GUIDE_FIRST_RUN,
    ErrorCode.AUTH_FAILED.value: GUIDE_AUTH_FAILED,
    ErrorCode.MODEL_NOT_FOUND.value: GUIDE_MODEL_NOT_FOUND,
    ErrorCode.RATE_LIMITED.value: GUIDE_RATE_LIMITED,
    ErrorCode.VISION_UNSUPPORTED.value: GUIDE_VISION_UNSUPPORTED,
    ErrorCode.STRUCTURED_UNSUPPORTED.value: GUIDE_STRUCTURED_UNSUPPORTED,
}


def guide_for(error_code: str | None) -> GuideCardOut | None:
    """取对应引导卡。

    ``error_code`` 为 ``None`` 或无配置时返回 :data:`GUIDE_FIRST_RUN`；
    未登记的错误码返回 ``None``（调用方必须显式决定怎么展示）。
    """
    if error_code is None:
        return GUIDE_FIRST_RUN
    return _GUIDES.get(error_code)
