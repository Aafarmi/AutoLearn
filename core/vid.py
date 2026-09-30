"""视频标识（T0-7）。

``sha1(课程ID + 集数 + 标题)[:16]``

视频不是题目，**不得复用 ``qid``**：``qid`` 由题干与选项决定，而视频没有选项。
``vid`` 参与断点续跑与去重（M5-5）。
"""

from __future__ import annotations

import hashlib

from core.qid import normalize_text

__all__ = ["make_vid"]

_VID_LEN = 16


def make_vid(course_id: str, episode_index: int, title: str) -> str:
    """由课程 ID、集数与标题构造视频 ID。"""
    payload = "|".join(
        (
            normalize_text(course_id),
            str(int(episode_index)),
            normalize_text(title),
        )
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:_VID_LEN]
