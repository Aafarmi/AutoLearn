"""执行层的「画面 + 坐标」底座（v0.2.0 新增）。

题目侧不再解析页面结构之后，「点哪儿」只剩一种答案：**模型给的归一化框乘上
截图尺寸**。本模块把这条换算与「怎么截一张可信的视口图」收在一处，
让 :mod:`act.actuator`（负责点）与 :mod:`act.verifier`（负责判）用同一套口径。

两条不可动摇的口径
------------------
1. **图像像素 == 视口 CSS 像素**：截图一律 ``scale="css"``，所以归一化框
   **只乘一次**图像尺寸。``devicePixelRatio`` 那一层被刻意省掉 ——
   它正是「在缩放显示器上必然偏一半」的根源。
2. **一律视口截图，禁用全页截图**（有守门单测卡住）。全页截图在长页面上成本塌方，
   而且它与模型看到的那一帧不是同一个东西：模型给的框是相对**当前视口**的。

截图**不是**在这里重新实现的
----------------------------
:func:`shot_viewport` 直接转调 ``perception.vision_probe.VisionProbe.shot_viewport``：
``scale="css"``、不截全页、**重试几次、单次等多久**（这几个数字刻意不在这里复述，
免得又成一处分叉点）合起来是「多久才敢说这一页截不下来」这一个产品决定，
只该有一个定义点。执行层与感知层若各写一份，迟早会在重试预算上分叉 ——
而分叉的表现是「有的路径偶发拿不到图」，查起来离现场很远。
（``act`` 依赖 ``perception`` 是既有方向、不成环：``perception`` 从不 import ``act``。）

区域差分为什么放在这里
----------------------
「点前后比一比选项区域」既属于校验（判据），又需要与坐标同源（裁的是同一个框）。
把纯像素运算放在这里、把判据与阈值留在 :mod:`act.verifier`，
是为了让「怎么裁」只有一个定义点，而「多大算变了」可以单独讨论与调参。
"""

from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING

from PIL import Image, ImageChops, ImageStat

from perception.vision_probe import VisionProbe

if TYPE_CHECKING:  # pragma: no cover
    from playwright.async_api import Page

__all__ = [
    "INK_MAX_LUMA",
    "REGION_PAD_MIN_PX",
    "REGION_PAD_RATIO",
    "CandidatePoint",
    "ImageSize",
    "NormBox",
    "candidate_points",
    "norm_box_center",
    "region_bounds",
    "region_ink_centroid",
    "region_mean_abs_diff",
    "short",
    "shot_viewport",
]

logger = logging.getLogger(__name__)

#: 归一化包围框 ``(x, y, w, h)``，取值 ``0..1``，相对**整张截图**左上角。
NormBox = tuple[float, float, float, float]

#: 截图尺寸 ``(宽, 高)``，单位是**图像像素**（``scale="css"`` 下即视口 CSS 像素）。
ImageSize = tuple[int, int]

#: 一个候选落点：视口 CSS 像素坐标 + 它是怎么来的（只进留痕，不参与判定）。
type CandidatePoint = tuple[float, float, str]

#: 选项区域往外扩的 padding：比例（占图像对应边长）+ 下限（像素）。
#:
#: 为什么要外扩：模型给的框通常**贴着文字**，而选中态的变化往往画在框边上 ——
#: 单选框本体、1px 描边、圆角外的一圈底色。只裁文字那块，真选中了也可能测出 0。
REGION_PAD_RATIO = 0.01
REGION_PAD_MIN_PX = 4

#: 「墨迹」的亮度上限（灰度 0~255）。低于它算内容（文字 / 描边 / 图标），
#: 高于它算背景。取 180 是**留了余量**的：浅灰描边也要算进来，
#: 而页面底色（白 / 极浅灰）要排除在外。
INK_MAX_LUMA = 180


def norm_box_center(box: NormBox, size: ImageSize) -> tuple[float, float]:
    """归一化框 → 视口 CSS 像素中心。

    ``scale="css"`` 截图下**图像像素 == 视口 CSS 像素**，所以这里**只乘一次**
    图像尺寸，不再除 ``devicePixelRatio``。图像尺寸为 0（截图拿不到尺寸）时
    抛 :class:`ValueError` —— 点 ``(0, 0)`` 是一次真实的误点，宁可停下来。
    """
    x, y, width_ratio, height_ratio = (float(value) for value in box)
    image_width, image_height = (float(value) for value in size)
    if image_width <= 0 or image_height <= 0:
        raise ValueError(f"截图尺寸非法：{size}（无法把归一化框换算成坐标）")
    return (
        (x + width_ratio / 2.0) * image_width,
        (y + height_ratio / 2.0) * image_height,
    )


def region_bounds(box: NormBox, size: ImageSize) -> tuple[int, int, int, int]:
    """归一化框 → 裁剪矩形 ``(left, top, right, bottom)``（像素，含外扩）。

    截到图像边界为止；框完全落在图外时返回一个空矩形（``right <= left``），
    由调用方按「裁不出区域」处理 —— **不要**在这里悄悄挪到图内：
    挪出来的区域不是模型指的那一块，用它做的差分没有意义。
    """
    image_width, image_height = int(size[0]), int(size[1])
    pad_x = max(REGION_PAD_MIN_PX, round(image_width * REGION_PAD_RATIO))
    pad_y = max(REGION_PAD_MIN_PX, round(image_height * REGION_PAD_RATIO))

    left = round(box[0] * image_width) - pad_x
    top = round(box[1] * image_height) - pad_y
    right = round((box[0] + box[2]) * image_width) + pad_x
    bottom = round((box[1] + box[3]) * image_height) + pad_y

    left = max(0, min(left, image_width))
    top = max(0, min(top, image_height))
    right = max(0, min(right, image_width))
    bottom = max(0, min(bottom, image_height))
    return left, top, right, bottom


def region_mean_abs_diff(
    before_png: bytes,
    after_png: bytes,
    box: NormBox,
    size: ImageSize,
) -> float | None:
    """两帧 PNG 在 ``box`` 区域内的平均绝对差（灰度 ``0~255``）。

    返回 ``None`` 表示**这次差分没做成**，只有两种情形，且都必须与
    「区域没变（差值 0）」区分开：

    - 图不是能解码的 PNG（拿到的根本不是截图）；
    - 图的像素尺寸与 ``size`` 对不上 —— 说明这两帧不是同一个视口
      （窗口被缩放、页面被导航过），按旧尺寸裁出来的区域指向别的东西。

    为什么用灰度而不是逐通道：选中态的变化无非底色、描边、单选框由空变实，
    这些在亮度上都有明确体现；逐通道最大差反而会被彩色描边与抗锯齿噪声主导。
    """
    first = _load_region(before_png, box, size)
    second = _load_region(after_png, box, size)
    if first is None or second is None:
        return None
    if first.size != second.size:  # pragma: no cover - 同一尺寸与同一框必然同尺寸
        return None
    difference = ImageChops.difference(first, second)
    return float(ImageStat.Stat(difference).mean[0])


def _load_region(png: bytes, box: NormBox, size: ImageSize) -> Image.Image | None:
    """解码 PNG 并裁出区域（灰度）。任何一步不成，返回 ``None``。"""
    try:
        with Image.open(io.BytesIO(png)) as source:
            if (source.width, source.height) != (int(size[0]), int(size[1])):
                return None
            gray = source.convert("L")
    except Exception as exc:  # 拿到的不是图 / 图被截断
        logger.debug("区域差分：PNG 解码失败（%s）", exc)
        return None

    bounds = region_bounds(box, size)
    if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
        return None
    return gray.crop(bounds)


def region_ink_centroid(
    before_png: bytes,
    box: NormBox,
    size: ImageSize,
    *,
    ink_max: int = INK_MAX_LUMA,
) -> tuple[float, float] | None:
    """框内**内容（墨迹）的加权质心**，返回归一化坐标；框里找不到内容返回 ``None``。

    存在的唯一理由：**模型给的框常比可点内容大**。

    2026-09-28 的真实故障（留痕见 ``logs/a090315220c6``）：超星作业页的选项，
    文字只占框左侧一小段、右侧整行是空白，而执行层点的是**框的几何中心** ——
    四次尝试全落在空白上，``region_mad=0.00``（页面上除右上角计时器外
    一个像素都没动），题目以 ``action_failed`` 停下。

    改用「框内暗像素质心」后，**点的是字最密的地方**，框给多大都不怕 ——
    这条修复不依赖模型把框给准，是执行层自己的兜底。

    用「暗的程度」加权（而不是简单平均）是为了让质心更靠近笔画密集处：
    抗锯齿的淡边算轻权，笔芯算重权。整框无墨迹（纯色块、图片选项、或框本来就
    框错了位置）时返回 ``None``，由调用方回退到几何中心。
    """
    region = _load_region(before_png, box, size)
    if region is None:
        return None
    left, top = region_bounds(box, size)[:2]
    # 走 ``tobytes()`` 而不是 ``load()``：灰度图 ``"L"`` 每像素正好一个字节，
    # 索引拿到的是干净的 ``int``（``load()`` 的返回类型是宽泛的联合类型，
    # 类型检查会一路抱怨）。顺带也快一些 —— 这是每次点击都要跑的热路径。
    raw = region.tobytes()
    width, height = region.size
    total = 0.0
    sum_x = 0.0
    sum_y = 0.0
    for y in range(height):
        row = y * width
        for x in range(width):
            luma = raw[row + x]
            if luma <= ink_max:
                weight = float(ink_max - luma + 1)
                total += weight
                sum_x += weight * x
                sum_y += weight * y
    if total <= 0:
        return None
    return ((left + sum_x / total) / size[0], (top + sum_y / total) / size[1])


def candidate_points(
    before_png: bytes,
    box: NormBox,
    size: ImageSize,
) -> list[CandidatePoint]:
    """框内**按优先级**排好的候选落点（视口 CSS 像素 + 来由说明）。

    顺序即「先试哪个」：**内容质心 → 几何中心 → 左半中心 → 偏上中心**。
    后三个给「框里没有可辨认的内容」兜底 —— 文字通常靠左靠上，
    偏左偏上比正中间更可能落在可点区域里。

    为什么需要**多个候选**而不是重复点同一个位置：一次没生效有两种原因，
    处置正好相反 ——

    * **点对了但还没渲染** → 原地重放同一个点（``pre`` 校验已能识别这种情形）；
    * **点错位置了** → 重放同一个错坐标再多次也没用，**必须换点**。

    事故里执行层只会做前者，于是 4 次全打在同一个空白处。
    """
    x, y, width, height = box
    points: list[CandidatePoint] = []

    centroid = region_ink_centroid(before_png, box, size)
    if centroid is not None:
        points.append((centroid[0] * size[0], centroid[1] * size[1], "ink_centroid"))

    center = norm_box_center(box, size)
    points.append((center[0], center[1], "box_center"))
    half = norm_box_center((x, y, width / 2.0, height), size)
    points.append((half[0], half[1], "left_half"))
    upper = norm_box_center((x, y + height / 4.0, width, height / 2.0), size)
    points.append((upper[0], upper[1], "upper_half"))

    unique: list[CandidatePoint] = []
    for px, py, reason in points:
        if all(abs(px - qx) > 3 or abs(py - qy) > 3 for qx, qy, _ in unique):
            unique.append((px, py, reason))
    return unique


async def shot_viewport(page: Page) -> bytes:
    """截当前视口，返回 PNG 字节。

    **转调感知层的实现**（``VisionProbe.shot_viewport``），不在这里另写一份：
    ``scale="css"``、不截全页、重试几次、单次等多久 —— 这四件事合起来是
    「多久才敢说这一页截不下来」这一个产品决定，只该有一个定义点。
    """
    return await VisionProbe.shot_viewport(page)


def short(value: object, limit: int = 160) -> str:
    """把异常 / 对象压成一行短文本（只进留痕与事件，不参与判定）。"""
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= limit else f"{text[: limit - 3]}..."
