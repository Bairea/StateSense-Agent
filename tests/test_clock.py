from datetime import datetime, timedelta, timezone

import pytest

from statesense.clock import FrozenClock, SystemClock


def test_system_clock_returns_aware_utc():
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_frozen_clock_starts_at_given_instant():
    t = datetime(2026, 9, 16, 3, 0, tzinfo=timezone.utc)
    assert FrozenClock(t).now() == t


def test_frozen_clock_advances():
    t = datetime(2026, 9, 16, 3, 0, tzinfo=timezone.utc)
    clock = FrozenClock(t)
    clock.advance(minutes=5)
    assert clock.now() == t + timedelta(minutes=5)


def test_frozen_clock_rejects_naive_datetime():
    with pytest.raises(ValueError, match="时区"):
        FrozenClock(datetime(2026, 9, 16, 3, 0))
