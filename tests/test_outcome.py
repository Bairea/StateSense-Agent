from datetime import datetime, timedelta, timezone

from statesense.activity.models import ActivitySnapshot, Entry
from statesense.config import OutcomeConfig
from statesense.outcome.tracker import evaluate, label

T0 = datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)
CFG = OutcomeConfig(delay_minutes=10, disengaged_ratio=0.5, continued_ratio=0.8)


def _snap(ent_minutes: float, data_status: str = "ok") -> ActivitySnapshot:
    return ActivitySnapshot(
        window_start=T0,
        window_end=T0 + timedelta(minutes=10),
        window_minutes=10,
        total_active_minutes=10.0,
        entries=(Entry("chrome.exe", "哔哩哔哩_bilibili", "", ent_minutes),),
        data_status=data_status,
        captured_at=T0 + timedelta(minutes=10),
    )


def _ent(snapshot: ActivitySnapshot) -> float:
    return sum(e.minutes for e in snapshot.entries)


# ── 标签边界（45 → 22.5 / 36.0）─────────────────────────────

def test_disengaged_when_drop_is_steep():
    assert label(45.0, 10.0, CFG) == "disengaged"


def test_boundary_at_exactly_half_is_partial():
    """0.5 * 45 = 22.5，按「严格小于」处理，恰好相等算 partial。"""
    assert label(45.0, 22.5, CFG) == "partial"


def test_partial_in_the_middle():
    assert label(45.0, 30.0, CFG) == "partial"


def test_boundary_at_continued_ratio_is_continued():
    """0.8 * 45 = 36.0，恰好相等算 continued。"""
    assert label(45.0, 36.0, CFG) == "continued"


def test_continued_when_activity_persists():
    assert label(45.0, 44.0, CFG) == "continued"


def test_more_activity_after_is_still_continued():
    assert label(45.0, 60.0, CFG) == "continued"


# ── 完整评估 ────────────────────────────────────────────────

def test_evaluate_returns_raw_values_and_label():
    verdict = evaluate(_snap(45.0), _snap(12.0), _ent, CFG)
    assert verdict.outcome == "disengaged"
    assert verdict.ent_before == 45.0
    assert verdict.ent_after == 12.0
    assert verdict.after_window_minutes == 10


def test_evaluate_returns_no_data_when_after_snapshot_is_untrustworthy():
    verdict = evaluate(_snap(45.0), _snap(0.0, data_status="no_capture_in_range"), _ent, CFG)
    assert verdict.outcome == "no_data"
    assert verdict.ent_before == 45.0


def test_evaluate_returns_no_data_when_before_snapshot_is_untrustworthy():
    verdict = evaluate(_snap(45.0, data_status="unreachable"), _snap(12.0), _ent, CFG)
    assert verdict.outcome == "no_data"


def test_zero_before_is_no_data_not_disengaged():
    """理论上前提是 ent>=40，但真出现 0 时不能当成「干预成功」。"""
    assert evaluate(_snap(0.0), _snap(0.0), _ent, CFG).outcome == "no_data"


def test_zero_before_also_warns(caplog):
    """spec §9.2 要求这种情况记一条内部告警 —— 它意味着判定与取数不一致。"""
    import logging

    with caplog.at_level(logging.WARNING, logger="statesense.outcome.tracker"):
        evaluate(_snap(0.0), _snap(0.0), _ent, CFG)
    assert "回执不可信" in caplog.text


def test_raw_values_are_recorded_even_when_no_data():
    verdict = evaluate(_snap(45.0), _snap(12.0, data_status="unreachable"), _ent, CFG)
    assert verdict.ent_before == 45.0
    assert verdict.ent_after == 12.0
