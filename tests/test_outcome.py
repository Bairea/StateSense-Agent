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


def _evaluate(before: ActivitySnapshot, after: ActivitySnapshot) -> "object":
    """两侧用同一个口径的便捷入口。**接口本身要求两侧各传一个可调用对象** ——
    便捷入口是为了让「两侧本来就该同源」的用例读起来短，不是为了把它变回一个参数。"""
    return evaluate(
        before,
        after,
        ent_before_minutes=_ent,
        ent_after_minutes=_ent,
        config=CFG,
    )


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
    verdict = _evaluate(_snap(45.0), _snap(12.0))
    assert verdict.outcome == "disengaged"
    assert verdict.ent_before == 45.0
    assert verdict.ent_after == 12.0
    assert verdict.after_window_minutes == 10


def test_evaluate_returns_no_data_when_after_snapshot_is_untrustworthy():
    verdict = _evaluate(_snap(45.0), _snap(0.0, data_status="no_capture_in_range"))
    assert verdict.outcome == "no_data"
    assert verdict.ent_before == 45.0


def test_evaluate_returns_no_data_when_before_snapshot_is_untrustworthy():
    verdict = _evaluate(_snap(45.0, data_status="unreachable"), _snap(12.0))
    assert verdict.outcome == "no_data"


def test_zero_before_is_no_data_not_disengaged():
    """理论上前提是 ent>=40，但真出现 0 时不能当成「干预成功」。"""
    assert _evaluate(_snap(0.0), _snap(0.0)).outcome == "no_data"


def test_zero_before_also_warns(caplog):
    """spec §9.2 要求这种情况记一条内部告警 —— 它意味着判定与取数不一致。"""
    import logging

    with caplog.at_level(logging.WARNING, logger="statesense.outcome.tracker"):
        _evaluate(_snap(0.0), _snap(0.0))
    assert "回执不可信" in caplog.text


def test_raw_values_are_recorded_even_when_no_data():
    verdict = _evaluate(_snap(45.0), _snap(12.0, data_status="unreachable"))
    assert verdict.ent_before == 45.0
    assert verdict.ent_after == 12.0


# ── 两侧各用可归属其时段的证据（阶段 2.2）────────────────────


def _snap_other(minutes: float) -> ActivitySnapshot:
    """未命中任何规则的条目 —— 游戏窗口的样子（进程名就是游戏自身）。"""
    return ActivitySnapshot(
        window_start=T0,
        window_end=T0 + timedelta(minutes=10),
        window_minutes=10,
        total_active_minutes=10.0,
        entries=(Entry("Brotato.exe", "Brotato", "", minutes),),
        data_status="ok",
        captured_at=T0 + timedelta(minutes=10),
    )


def _ent_with(fullscreen: int | None):
    """给定全屏取值时的娱乐分钟口径 —— 与 `Scheduler.ent_minutes` 同一算法。"""
    import re

    from statesense.config import TaxonomyConfig
    from statesense.perception import is_gaming
    from statesense.state.engine import effective_entertainment_minutes
    from statesense.state.taxonomy import bucket_minutes

    taxonomy = TaxonomyConfig(
        entertainment=(re.compile("bilibili", re.IGNORECASE),), gray=(), work=()
    )

    def inner(snapshot: ActivitySnapshot) -> float:
        return effective_entertainment_minutes(
            bucket_minutes(snapshot.entries, taxonomy),
            trustworthy=snapshot.is_trustworthy,
            gaming=is_gaming(fullscreen),
        )

    return inner


def test_each_side_uses_its_own_fullscreen_evidence():
    """打游戏时触发、随后退出游戏：前侧按「游戏还在跑」算，后侧按「已退出」算。

    同一批未归类条目，两侧的取值不同不是矛盾，而是各自时段的事实。
    """
    from statesense.perception import QUNS_ACCEPTS_NOTIFICATIONS, QUNS_BUSY

    verdict = evaluate(
        _snap_other(45.0),
        _snap_other(30.0),
        ent_before_minutes=_ent_with(QUNS_BUSY),
        ent_after_minutes=_ent_with(QUNS_ACCEPTS_NOTIFICATIONS),
        config=CFG,
    )
    assert verdict.ent_before == 45.0  # 前侧：游戏在跑，未归类条目算娱乐
    assert verdict.ent_after == 0.0  # 后侧：游戏退出，同样的条目不再算娱乐
    assert verdict.outcome == "disengaged"


def test_shared_evidence_understates_the_before_side_and_loses_the_receipt():
    """把两侧绑成同一个取值（旧写法）会把前侧清零，回执直接落成 no_data。

    这条测的是缺陷本身，留着它有两个作用：证明上面的修正确实改了行为，
    以及提醒后来者「共用一个取值」为什么会把「退出游戏」读成「干预有效」。
    """
    from statesense.perception import QUNS_ACCEPTS_NOTIFICATIONS

    shared = _ent_with(QUNS_ACCEPTS_NOTIFICATIONS)
    verdict = evaluate(
        _snap_other(45.0),
        _snap_other(0.0),
        ent_before_minutes=shared,
        ent_after_minutes=shared,
        config=CFG,
    )
    assert verdict.ent_before == 0.0
    assert verdict.outcome == "no_data"
