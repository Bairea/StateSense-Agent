import re
from datetime import datetime, timedelta, timezone

from statesense.activity.models import ActivitySnapshot, Entry
from statesense.config import TaxonomyConfig, ThresholdConfig
from statesense.state.engine import classify, is_late_night
from statesense.state.models import State

T0 = datetime(2026, 9, 16, 6, 0, tzinfo=timezone.utc)
TAX = TaxonomyConfig(
    entertainment=(re.compile("bilibili|哔哩哔哩", re.IGNORECASE),),
    gray=(re.compile("知乎", re.IGNORECASE),),
    work=(re.compile("github", re.IGNORECASE),),
)
TH = ThresholdConfig()


def _snap(entries, total, data_status="ok") -> ActivitySnapshot:
    return ActivitySnapshot(
        window_start=T0 - timedelta(minutes=60),
        window_end=T0,
        window_minutes=60,
        total_active_minutes=total,
        entries=tuple(entries),
        data_status=data_status,
        captured_at=T0,
    )


def _ent(minutes, title="哔哩哔哩_bilibili") -> Entry:
    return Entry(app="chrome.exe", title=title, url="", minutes=minutes)


def _work(minutes) -> Entry:
    return Entry(app="Code.exe", title="repo - GitHub", url="", minutes=minutes)


def _other(minutes) -> Entry:
    return Entry(app="notepad.exe", title="记事本", url="", minutes=minutes)


# ── 状态阶梯（ref1 的 20 / 40 / 65 刻度）────────────────────

def test_below_watch_threshold_is_normal():
    v = classify(_snap([_ent(5.0), _work(50.0)], 55.0), TAX, TH)
    assert v.state is State.NORMAL
    assert v.ent_minutes == 5.0


def test_at_watch_threshold_is_watch():
    assert classify(_snap([_ent(20.0)], 60.0), TAX, TH).state is State.WATCH


def test_at_passive_threshold_is_passive():
    assert classify(_snap([_ent(40.0)], 60.0), TAX, TH).state is State.PASSIVE_CONSUMPTION


def test_at_high_risk_threshold_is_high_risk():
    assert classify(_snap([_ent(65.0)], 60.0), TAX, TH).state is State.HIGH_RISK_PASSIVE_CONSUMPTION


def test_just_below_passive_stays_watch():
    assert classify(_snap([_ent(39.9)], 60.0), TAX, TH).state is State.WATCH


# ── 分类拆分 ────────────────────────────────────────────────

def test_buckets_are_reported_separately():
    v = classify(
        _snap(
            [_ent(42.0), Entry("chrome.exe", "某问题 - 知乎", "", 6.0), _work(9.0), _other(3.0)],
            60.0,
        ),
        TAX,
        TH,
    )
    assert v.ent_minutes == 42.0
    assert v.gray_minutes == 6.0
    assert v.work_minutes == 9.0
    assert v.total_active_minutes == 60.0
    assert v.ent_ratio == 0.7


def test_gray_never_counts_toward_passive_consumption():
    """知乎只单独统计，不抬高 ent —— 否则会把「可能在学习」误判成被困。"""
    v = classify(_snap([Entry("chrome.exe", "某问题 - 知乎", "", 50.0)], 50.0), TAX, TH)
    assert v.ent_minutes == 0.0
    assert v.gray_minutes == 50.0
    assert v.state is State.NORMAL


def test_window_minutes_is_carried_through():
    assert classify(_snap([_ent(45.0)], 60.0), TAX, TH).window_minutes == 60


# ── ratio 与除零 ─────────────────────────────────────────────

def test_ratio_is_ent_over_total_active():
    assert classify(_snap([_ent(30.0), _other(30.0)], 60.0), TAX, TH).ent_ratio == 0.5


def test_zero_total_active_gives_zero_ratio_without_dividing_by_zero():
    v = classify(_snap([], 0.0), TAX, TH)
    assert v.ent_ratio == 0.0
    assert v.state is State.NORMAL


# ── data_status 闸门 ────────────────────────────────────────

def test_non_ok_data_status_skips_without_concluding():
    """最关键的一条：没在采集 ≠ 没有活动。"""
    for status in ("empty_but_recording", "no_capture_in_range", "not_recording", "unreachable"):
        v = classify(_snap([_ent(0.0)], 0.0, data_status=status), TAX, TH)
        assert v.skipped is True, status
        assert v.skip_reason == status
        assert v.state is State.NORMAL


def test_skipped_verdict_still_reports_measured_buckets():
    v = classify(_snap([_ent(50.0)], 60.0, data_status="no_capture_in_range"), TAX, TH)
    assert v.skipped is True
    assert v.ent_minutes == 50.0


# ── late_night 正交标记 ──────────────────────────────────────

def test_late_night_uses_local_hour():
    assert is_late_night(datetime(2026, 9, 16, 3, 0).astimezone(), 30.0, TH) is True
    assert is_late_night(datetime(2026, 9, 16, 15, 0).astimezone(), 30.0, TH) is False


def test_late_night_requires_minimum_activity():
    assert is_late_night(datetime(2026, 9, 16, 3, 0).astimezone(), 5.0, TH) is False


def test_late_night_boundaries():
    th = ThresholdConfig(late_night_start_hour=1, late_night_end_hour=6)
    for hour, expected in ((0, False), (1, True), (5, True), (6, False), (23, False)):
        instant = datetime(2026, 9, 16, hour, 0).astimezone()
        assert is_late_night(instant, 30.0, th) is expected, f"hour={hour}"


def test_late_night_is_independent_of_state():
    """凌晨写代码：state 是 NORMAL，但 late_night 必须为真。"""
    night = datetime(2026, 9, 16, 3, 0).astimezone()
    snap = ActivitySnapshot(
        window_start=night - timedelta(minutes=60),
        window_end=night,
        window_minutes=60,
        total_active_minutes=60.0,
        entries=(_work(50.0), _other(10.0)),
        data_status="ok",
        captured_at=night,
    )
    v = classify(snap, TAX, TH)
    assert v.state is State.NORMAL
    assert v.late_night is True


# ── entries_minutes（区分「未命中规则」与「明细缺失」）────────

def test_entries_minutes_sums_all_entries_including_other():
    """未命中任何规则的条目也算进去 —— 它正是漏判视图要找的东西。"""
    v = classify(_snap([_ent(20.0), _work(30.0), _other(9.9)], 59.9), TAX, TH)
    assert v.entries_minutes == 59.9
    assert v.total_active_minutes == 59.9


def test_entries_minutes_smaller_than_total_means_detail_missing():
    """total 声称有活动、条目却只覆盖一部分 → 差额是「明细缺失」，不是漏判。"""
    v = classify(_snap([_ent(10.0)], 59.9), TAX, TH)
    assert v.entries_minutes == 10.0
    assert v.total_active_minutes == 59.9


def test_entries_minutes_is_present_on_skipped_verdict_too():
    """skipped 分支走的是同一个 shared dict，不能漏掉这个字段。"""
    v = classify(_snap([], 0.0, data_status="unreachable"), TAX, TH)
    assert v.skipped is True
    assert v.entries_minutes == 0.0
