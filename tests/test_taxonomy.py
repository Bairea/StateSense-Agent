import re

import pytest

from statesense.activity.models import Entry
from statesense.config import TaxonomyConfig
from statesense.state.taxonomy import Category, bucket_minutes, classify


def _tax(**kw) -> TaxonomyConfig:
    def comp(items):
        return tuple(re.compile(i, re.IGNORECASE) for i in items)

    return TaxonomyConfig(
        entertainment=comp(kw.get("entertainment", ["bilibili", "哔哩哔哩", "youtube"])),
        gray=comp(kw.get("gray", ["知乎"])),
        work=comp(kw.get("work", ["github", r"\bcode\b"])),
    )


def _entry(title="", app="", url="", minutes=1.0) -> Entry:
    return Entry(app=app, title=title, url=url, minutes=minutes)


@pytest.mark.parametrize(
    "entry,expected",
    [
        (_entry(title="【某视频】_哔哩哔哩_bilibili"), Category.ENTERTAINMENT),
        (_entry(title="Some video - YouTube", app="chrome.exe"), Category.ENTERTAINMENT),
        (_entry(title="随便什么", url="https://www.bilibili.com/video/BV1"), Category.ENTERTAINMENT),
        (_entry(title="某问题 - 知乎"), Category.GRAY),
        (_entry(title="repo - GitHub", app="chrome.exe"), Category.WORK),
        (_entry(title="engine.py - Code", app="Code.exe"), Category.WORK),
        (_entry(title="记事本", app="notepad.exe"), Category.OTHER),
    ],
)
def test_classify_matches_on_app_title_or_url(entry, expected):
    assert classify(entry, _tax()) is expected


def test_entertainment_wins_over_work_when_both_match():
    """同时命中工作与娱乐时按娱乐处理 —— 宁可多打扰，也不要漏掉真正被困住。"""
    assert classify(_entry(title="github 上的 bilibili 视频"), _tax()) is Category.ENTERTAINMENT


def test_work_wins_over_gray():
    assert classify(_entry(title="知乎上看到的 github 项目"), _tax()) is Category.WORK


def test_bucket_minutes_sums_per_category():
    buckets = bucket_minutes(
        (
            _entry(title="哔哩哔哩_bilibili", minutes=40.0),
            _entry(title="某问题 - 知乎", minutes=5.0),
            _entry(title="repo - GitHub", minutes=12.0),
            _entry(title="记事本", minutes=3.0),
        ),
        _tax(),
    )
    assert buckets[Category.ENTERTAINMENT] == 40.0
    assert buckets[Category.GRAY] == 5.0
    assert buckets[Category.WORK] == 12.0
    assert buckets[Category.OTHER] == 3.0


def test_bucket_minutes_returns_zero_for_absent_category():
    buckets = bucket_minutes((_entry(title="记事本"),), _tax())
    assert buckets[Category.ENTERTAINMENT] == 0.0
    assert buckets[Category.GRAY] == 0.0
    assert buckets[Category.WORK] == 0.0
    assert len(buckets) == len(Category)


def test_empty_entries_bucket_all_zero():
    buckets = bucket_minutes((), _tax())
    assert all(v == 0.0 for v in buckets.values())
