import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from statesense.activity.reader import ActivityReader
from statesense.clock import FrozenClock
from statesense.notify.base import RecordingNotifier
from statesense.scheduler import Scheduler
from statesense.store.db import RUN_EVENT_KINDS, SCHEMA_VERSION, Store

T0 = datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    s.migrate()
    yield s
    s.close()


# ── schema 版本与迁移 ───────────────────────────────────────

def test_user_version_is_current(store):
    assert store.user_version() == SCHEMA_VERSION


def test_entries_minutes_column_exists(store):
    columns = {r["name"] for r in store._conn.execute("PRAGMA table_info(evaluations)")}
    assert "entries_minutes" in columns


def test_intervention_channel_column_is_nullable(store):
    """NULL 表示「迁移前写入的行，通道未知」，必须允许。

    `--dry-run` 走 RecordingNotifier，它同样返回 delivered —— 不记通道，
    排练出来的干预与真实弹窗在库里就完全一样。
    """
    columns = {
        r["name"]: r for r in store._conn.execute("PRAGMA table_info(interventions)")
    }
    assert "channel" in columns
    assert columns["channel"]["notnull"] == 0


def test_fullscreen_state_column_is_nullable(store):
    """NULL 表示「无法判定」，必须允许 —— 不能拿 0 假装「当时不是全屏」。"""
    columns = {
        r["name"]: r for r in store._conn.execute("PRAGMA table_info(evaluations)")
    }
    assert "fullscreen_state" in columns
    assert columns["fullscreen_state"]["notnull"] == 0


def test_insert_run_event_roundtrips(store):
    store.insert_run_event(T0, "sleep_gap", "200.0")
    row = store._conn.execute("SELECT * FROM run_events").fetchone()
    assert row["kind"] == "sleep_gap"
    assert row["detail"] == "200.0"


def test_unknown_kind_is_rejected(store):
    """kind 是封闭枚举 —— 不认识的取值说明写入方与 schema 已漂移，必须炸。"""
    with pytest.raises(ValueError, match="未知的运行事件类型"):
        store.insert_run_event(T0, "typo_gap", "x")


def test_all_declared_kinds_are_accepted(store):
    # at 是主键且用 INSERT OR REPLACE，两条必须落在不同时刻，否则互相覆盖。
    for offset, kind in enumerate(RUN_EVENT_KINDS):
        store.insert_run_event(T0 + timedelta(minutes=offset), kind, "d")
    count = store._conn.execute("SELECT COUNT(*) AS n FROM run_events").fetchone()["n"]
    assert count == len(RUN_EVENT_KINDS)


def test_same_moment_replaces_instead_of_duplicating(store):
    """同一时刻重复写入是幂等的 —— 常驻进程重启后重跑同一轮不应产生两行。"""
    store.insert_run_event(T0, "sleep_gap", "1.0")
    store.insert_run_event(T0, "sleep_gap", "2.0")
    rows = store._conn.execute("SELECT * FROM run_events").fetchall()
    assert len(rows) == 1
    assert rows[0]["detail"] == "2.0"


def test_v3_database_upgrades_in_place(tmp_path):
    """v3 老库（有 skipped，无 entries_minutes，无 run_events）必须原地升级。"""
    path = tmp_path / "v3.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE evaluations (id INTEGER PRIMARY KEY, at TEXT NOT NULL,
          window_minutes INTEGER NOT NULL, total_active_minutes REAL NOT NULL,
          ent_minutes REAL NOT NULL, gray_minutes REAL NOT NULL, work_minutes REAL NOT NULL,
          ent_ratio REAL NOT NULL, state TEXT NOT NULL, late_night INTEGER NOT NULL,
          data_status TEXT NOT NULL, skipped INTEGER NOT NULL DEFAULT 0,
          prev_state TEXT, decision TEXT NOT NULL, gate_trace TEXT NOT NULL);
        CREATE TABLE interventions (id INTEGER PRIMARY KEY,
          evaluation_id INTEGER NOT NULL REFERENCES evaluations(id), at TEXT NOT NULL,
          state TEXT NOT NULL, late_night INTEGER NOT NULL, action_id TEXT NOT NULL,
          action_text TEXT NOT NULL, delivery_status TEXT NOT NULL,
          outcome_due_at TEXT NOT NULL, user_response TEXT);
        CREATE TABLE outcomes (intervention_id INTEGER PRIMARY KEY,
          checked_at TEXT NOT NULL, outcome TEXT NOT NULL, ent_before REAL NOT NULL,
          ent_after REAL NOT NULL, after_window_minutes REAL NOT NULL);
        CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO evaluations (at, window_minutes, total_active_minutes, ent_minutes,
          gray_minutes, work_minutes, ent_ratio, state, late_night, data_status, skipped,
          prev_state, decision, gate_trace)
          VALUES ('2026-09-16T04:00:00+00:00', 60, 59.9, 0.0, 0.0, 0.0, 0.0, 'NORMAL', 0,
                  'ok', 0, NULL, 'skip', '[]');
        PRAGMA user_version = 3;
        """
    )
    conn.commit()
    conn.close()

    s = Store(path)
    s.migrate()
    assert s.user_version() == SCHEMA_VERSION

    columns = {r["name"] for r in s._conn.execute("PRAGMA table_info(evaluations)")}
    assert "entries_minutes" in columns
    assert "fullscreen_state" in columns
    intervention_columns = {
        r["name"] for r in s._conn.execute("PRAGMA table_info(interventions)")
    }
    assert "channel" in intervention_columns
    tables = {
        r["name"] for r in s._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "run_events" in tables

    # 旧行保留，新列取 DEFAULT 0 / NULL —— 不伪造数据。
    row = s._conn.execute(
        "SELECT state, entries_minutes, fullscreen_state FROM evaluations"
    ).fetchone()
    assert row["state"] == "NORMAL"
    assert row["entries_minutes"] == 0.0
    # 旧行没观测过全屏状态，只能是 NULL（无法判定），不能是 0 或 5。
    assert row["fullscreen_state"] is None
    s.close()


# ── 调度器两处落行（V0.5 健康信号）──────────────────────────

def _body(ent_minutes: float, total: float = 60.0, status: str = "ok") -> bytes:
    return json.dumps(
        {
            "total_active_minutes": total,
            "data_status": status,
            "windows": [
                {
                    "app_name": "chrome.exe",
                    "window_name": "哔哩哔哩",
                    "browser_url": "https://www.bilibili.com/video/BV1",
                    "minutes": ent_minutes,
                }
            ],
        },
        ensure_ascii=False,
    ).encode("utf-8")


@pytest.fixture()
def cfg_store(config):
    s = Store(config.store_path)
    s.migrate()
    yield s
    s.close()


def _scheduler(config, store, bodies, clock) -> Scheduler:
    queue = list(bodies)

    def fake_get(url, headers, timeout):
        return 200, (queue.pop(0) if queue else _body(0.0, 0.0))

    return Scheduler(
        config=config,
        clock=clock,
        reader=ActivityReader("http://localhost:3030", "k", 10.0, fake_get),
        store=store,
        notifier=RecordingNotifier(),
    )


def _run_events(store) -> list[sqlite3.Row]:
    return store._conn.execute("SELECT * FROM run_events ORDER BY at").fetchall()


def test_sleep_gap_lands_in_run_events(config, cfg_store):
    """休眠跳过原本静默不落行，导致与「进程死了」在表里无法区分。"""
    clock = FrozenClock(T0)
    sch = _scheduler(config, cfg_store, [_body(45.0), _body(45.0)], clock)
    sch.run_once()
    clock.advance(minutes=200)
    report = sch.run_once()
    assert report.evaluation_id is None
    rows = _run_events(cfg_store)
    assert len(rows) == 1
    assert rows[0]["kind"] == "sleep_gap"
    assert float(rows[0]["detail"]) == pytest.approx(200.0)
    # 有意跳过：确实没有 evaluations 行，但有 run_events 行 → 可辨。
    assert (
        cfg_store._conn.execute("SELECT COUNT(*) AS n FROM evaluations").fetchone()["n"] == 1
    )


def test_gap_within_limit_writes_no_run_event(config, cfg_store):
    clock = FrozenClock(T0)
    sch = _scheduler(config, cfg_store, [_body(45.0), _body(45.0)], clock)
    sch.run_once()
    clock.advance(minutes=110)
    assert sch.run_once().evaluation_id is not None
    assert _run_events(cfg_store) == []


def test_tick_error_lands_in_run_events(config, cfg_store):
    """run_forever 的 except 原本只写日志 —— 崩在日志里与进程死了长得一样。"""

    class Exploding:
        def read(self, *args, **kwargs):
            raise RuntimeError("boom")

    sch = Scheduler(
        config=config,
        clock=FrozenClock(T0),
        reader=Exploding(),
        store=cfg_store,
        notifier=RecordingNotifier(),
    )
    assert sch.run_tick_guarded(T0) is None
    rows = _run_events(cfg_store)
    assert len(rows) == 1
    assert rows[0]["kind"] == "tick_error"
    assert "RuntimeError" in rows[0]["detail"]
    assert "boom" in rows[0]["detail"]


def test_tick_error_detail_is_truncated(config, cfg_store):
    """异常消息可能很长，落库前截断，避免一行撑爆表。"""

    class Exploding:
        def read(self, *args, **kwargs):
            raise RuntimeError("x" * 5000)

    sch = Scheduler(
        config=config,
        clock=FrozenClock(T0),
        reader=Exploding(),
        store=cfg_store,
        notifier=RecordingNotifier(),
    )
    sch.run_tick_guarded(T0)
    assert len(_run_events(cfg_store)[0]["detail"]) <= 200


def test_tick_error_detail_keeps_only_the_first_line(config, cfg_store):
    """spec §7.2 要的是「异常类名 + 消息首行」。

    包装过的异常消息里往往重复堆叠同一条信息，换行还会把报告缺口视图
    「一行一条运行事件」的版式冲掉。
    """

    class Exploding:
        def read(self, *args, **kwargs):
            raise RuntimeError("第一行说明问题\n第二行是包装出来的重复内容\n第三行也是")

    sch = Scheduler(
        config=config,
        clock=FrozenClock(T0),
        reader=Exploding(),
        store=cfg_store,
        notifier=RecordingNotifier(),
    )
    sch.run_tick_guarded(T0)
    detail = _run_events(cfg_store)[0]["detail"]
    assert detail == "RuntimeError: 第一行说明问题"


def test_dry_run_intervention_records_its_channel(config, cfg_store):
    """`--dry-run` 必须留下痕迹，否则「到底有没有真的弹过窗」永远答不上来。

    RecordingNotifier 与真弹窗返回的都是 `delivered`，通道是唯一能分开两者的东西。
    """
    clock = FrozenClock(T0)
    sch = _scheduler(config, cfg_store, [_body(45.0)], clock)
    report = sch.run_once()
    assert report.intervened is True

    row = cfg_store._conn.execute("SELECT * FROM interventions").fetchone()
    assert row["channel"] == "recording"
    assert row["delivery_status"] == "delivered"
    # note 里也要能一眼看出这是排练，而不是真弹了窗。
    assert "recording" in report.note


def test_tick_error_does_not_leak_into_interventions(config, cfg_store):
    """运维信号与干预信号必须分离：告警绝不能占用 daily_cap 或 cooldown。"""

    class Exploding:
        def read(self, *args, **kwargs):
            raise RuntimeError("boom")

    sch = Scheduler(
        config=config,
        clock=FrozenClock(T0),
        reader=Exploding(),
        store=cfg_store,
        notifier=RecordingNotifier(),
    )
    sch.run_tick_guarded(T0)
    assert cfg_store._conn.execute("SELECT COUNT(*) AS n FROM interventions").fetchone()["n"] == 0


def test_successful_tick_writes_no_run_event(config, cfg_store):
    """只有异常才落行 —— 正常存活由 evaluations.at 派生，同一事实不存两遍。"""
    sch = _scheduler(config, cfg_store, [_body(5.0)], FrozenClock(T0))
    assert sch.run_tick_guarded(T0) is not None
    assert _run_events(cfg_store) == []
