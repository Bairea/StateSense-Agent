"""把一条活动记录归类。纯函数，无 IO。"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from re import Pattern

from statesense.activity.models import Entry
from statesense.config import TaxonomyConfig


class Category(StrEnum):
    ENTERTAINMENT = "ENTERTAINMENT"
    GRAY = "GRAY"
    WORK = "WORK"
    OTHER = "OTHER"


def _matches(entry: Entry, patterns: tuple[Pattern[str], ...]) -> bool:
    haystack = f"{entry.app}\n{entry.title}\n{entry.url}"
    return any(p.search(haystack) for p in patterns)


def classify(entry: Entry, taxonomy: TaxonomyConfig) -> Category:
    """优先级：娱乐 > 工作 > 灰色 > 其他。

    娱乐优先是刻意的：同时命中工作与娱乐时（例如在 GitHub 页面上打开的视频），
    宁可多打扰一次，也不要漏掉真正被困住的情形。
    灰色放在工作之后，因为「在知乎」与「在写代码」同时成立时，工作证据更硬。
    """
    if _matches(entry, taxonomy.entertainment):
        return Category.ENTERTAINMENT
    if _matches(entry, taxonomy.work):
        return Category.WORK
    if _matches(entry, taxonomy.gray):
        return Category.GRAY
    return Category.OTHER


def bucket_minutes(entries: Iterable[Entry], taxonomy: TaxonomyConfig) -> dict[Category, float]:
    buckets = {c: 0.0 for c in Category}
    for entry in entries:
        buckets[classify(entry, taxonomy)] += entry.minutes
    return buckets


def entertainment_minutes(entries: Iterable[Entry], taxonomy: TaxonomyConfig) -> float:
    """被动消费分钟数。

    状态判定与行为回执必须共用这一个口径 —— 审查发现两处各写了一份
    `round(bucket_minutes(...)[ENTERTAINMENT], 2)`，一旦漂移，
    「干预前 vs 干预后」就不是同一个量了，回执会失真。
    """
    return round(bucket_minutes(entries, taxonomy)[Category.ENTERTAINMENT], 2)
