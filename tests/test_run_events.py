import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from statesense.store.db import RUN_EVENT_KINDS, Store

T0 = datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    s.migrate()
    yield s
    s.close()


# ── schema v4 ───────────────────────────────────────────────

def test_user_version_is_4(store):
    assert store.user_version() == 4


def test_entries_minutes_column_exists(store):
    columns = {r["name"] for r in store._conn.execute("PRAGMA table_info(evaluations)")}
    assert "entries_minutes" in columns


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
    assert s.user_version() == 4

    columns = {r["name"] for r in s._conn.execute("PRAGMA table_info(evaluations)")}
    assert "entries_minutes" in columns
    tables = {
        r["name"] for r in s._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "run_events" in tables

    # 旧行保留，新列取 DEFAULT 0 —— 不伪造数据。
    row = s._conn.execute("SELECT state, entries_minutes FROM evaluations").fetchone()
    assert row["state"] == "NORMAL"
    assert row["entries_minutes"] == 0.0
    s.close()
