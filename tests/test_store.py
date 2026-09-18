import json
from datetime import datetime, timedelta, timezone

import pytest

from statesense.intervention.models import Decision, GateResult
from statesense.outcome.models import OutcomeVerdict
from statesense.state.models import State, StateVerdict
from statesense.store.db import Store

T0 = datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    s.migrate()
    yield s
    s.close()


def _verdict(state=State.PASSIVE_CONSUMPTION) -> StateVerdict:
    return StateVerdict(
        state=state,
        late_night=False,
        total_active_minutes=60.0,
        ent_minutes=45.0,
        gray_minutes=5.0,
        work_minutes=10.0,
        ent_ratio=0.75,
        entries_minutes=60.0,
        fullscreen_state=None,
        window_minutes=60,
        data_status="ok",
        skipped=False,
        skip_reason=None,
    )


def _decision(intervene=True) -> Decision:
    return Decision(
        intervene=intervene,
        action_id="walk5" if intervene else None,
        reason="测试",
        gate_trace=(GateResult("ratio_min", True, 0.75, 0.75),),
    )


def test_migrate_is_idempotent(tmp_path):
    s = Store(tmp_path / "x.db")
    s.migrate()
    s.migrate()
    assert s.user_version() == 6
    s.close()


def test_migrates_v1_database_by_adding_user_response(tmp_path):
    """老库（user_version=1，interventions 没有 user_response）必须能原地升级。"""
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE evaluations (id INTEGER PRIMARY KEY, at TEXT NOT NULL,
          window_minutes INTEGER NOT NULL, total_active_minutes REAL NOT NULL,
          ent_minutes REAL NOT NULL, gray_minutes REAL NOT NULL, work_minutes REAL NOT NULL,
          ent_ratio REAL NOT NULL, state TEXT NOT NULL, late_night INTEGER NOT NULL,
          data_status TEXT NOT NULL, prev_state TEXT, decision TEXT NOT NULL,
          gate_trace TEXT NOT NULL);
        CREATE TABLE interventions (id INTEGER PRIMARY KEY,
          evaluation_id INTEGER NOT NULL REFERENCES evaluations(id), at TEXT NOT NULL,
          state TEXT NOT NULL, late_night INTEGER NOT NULL, action_id TEXT NOT NULL,
          action_text TEXT NOT NULL, delivery_status TEXT NOT NULL,
          outcome_due_at TEXT NOT NULL);
        CREATE TABLE outcomes (intervention_id INTEGER PRIMARY KEY,
          checked_at TEXT NOT NULL, outcome TEXT NOT NULL, ent_before REAL NOT NULL,
          ent_after REAL NOT NULL, after_window_minutes REAL NOT NULL);
        CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        PRAGMA user_version = 1;
        """
    )
    conn.commit()
    conn.close()

    s = Store(path)
    s.migrate()
    assert s.user_version() == 6
    intervention_columns = {r["name"] for r in s._conn.execute("PRAGMA table_info(interventions)")}
    evaluation_columns = {r["name"] for r in s._conn.execute("PRAGMA table_info(evaluations)")}
    assert "user_response" in intervention_columns
    assert "skipped" in evaluation_columns
    assert "entries_minutes" in evaluation_columns
    assert "fullscreen_state" in evaluation_columns
    s.close()


def test_creates_parent_directory(tmp_path):
    s = Store(tmp_path / "nested" / "deep" / "x.db")
    s.migrate()
    assert (tmp_path / "nested" / "deep" / "x.db").is_file()
    s.close()


def test_insert_evaluation_roundtrips(store):
    eid = store.insert_evaluation(T0, _verdict(), _decision())
    row = store.fetch_evaluation(eid)
    assert row["state"] == "PASSIVE_CONSUMPTION"
    assert row["ent_minutes"] == 45.0
    assert row["ent_ratio"] == 0.75
    assert row["window_minutes"] == 60
    assert row["decision"] == "intervene"
    trace = json.loads(row["gate_trace"])
    assert trace[0]["name"] == "ratio_min"
    assert trace[0]["passed"] is True


def test_skip_decision_is_recorded_as_skip(store):
    eid = store.insert_evaluation(T0, _verdict(), _decision(intervene=False))
    assert store.fetch_evaluation(eid)["decision"] == "skip"


def test_skipped_flag_distinguishes_no_data_from_truly_normal(store):
    """这条是审查发现的缺陷：单看 state=NORMAL 分不清「真正常」与「没采到数据」。"""
    normal = store.insert_evaluation(T0, _verdict(state=State.NORMAL), _decision(False))
    skipped_verdict = StateVerdict(
        state=State.NORMAL, late_night=False, total_active_minutes=0.0, ent_minutes=0.0,
        gray_minutes=0.0, work_minutes=0.0, ent_ratio=0.0, entries_minutes=0.0,
        fullscreen_state=None,
        window_minutes=60,
        data_status="no_capture_in_range", skipped=True,
        skip_reason="no_capture_in_range",
    )
    skipped = store.insert_evaluation(T0, skipped_verdict, _decision(False))

    normal_row = store.fetch_evaluation(normal)
    skipped_row = store.fetch_evaluation(skipped)
    assert normal_row["state"] == skipped_row["state"] == "NORMAL"
    assert normal_row["skipped"] == 0
    assert skipped_row["skipped"] == 1
    assert skipped_row["data_status"] == "no_capture_in_range"


def test_prev_state_chain(store):
    store.insert_evaluation(T0, _verdict(), _decision())
    eid = store.insert_evaluation(T0 + timedelta(minutes=5), _verdict(State.WATCH), _decision())
    assert store.fetch_evaluation(eid)["prev_state"] == "PASSIVE_CONSUMPTION"


def test_first_evaluation_has_null_prev_state(store):
    eid = store.insert_evaluation(T0, _verdict(), _decision())
    assert store.fetch_evaluation(eid)["prev_state"] is None


def test_intervention_and_due_lookup(store):
    eid = store.insert_evaluation(T0, _verdict(), _decision())
    due = T0 + timedelta(minutes=10)
    iid = store.insert_intervention(
        evaluation_id=eid,
        at=T0,
        state="PASSIVE_CONSUMPTION",
        late_night=False,
        action_id="walk5",
        action_text="离开电脑走 5 分钟",
        delivery_status="delivered",
        outcome_due_at=due,
        channel="foreground_popup",
    )
    assert store.due_interventions(T0 + timedelta(minutes=9)) == []
    pending = store.due_interventions(due)
    assert len(pending) == 1
    assert pending[0].id == iid
    assert pending[0].at == T0


def test_due_interventions_excludes_already_checked(store):
    eid = store.insert_evaluation(T0, _verdict(), _decision())
    due = T0 + timedelta(minutes=10)
    iid = store.insert_intervention(
        eid, T0, "PASSIVE_CONSUMPTION", False, "walk5", "走 5 分钟", "delivered", due,
        channel="foreground_popup",
    )
    store.insert_outcome(iid, due, OutcomeVerdict("disengaged", 45.0, 3.0, 10.0))
    assert store.due_interventions(due + timedelta(minutes=60)) == []


def test_outcome_roundtrip(store):
    eid = store.insert_evaluation(T0, _verdict(), _decision())
    iid = store.insert_intervention(
        eid,
        T0,
        "PASSIVE_CONSUMPTION",
        False,
        "walk5",
        "走 5 分钟",
        "delivered",
        T0 + timedelta(minutes=10),
        channel="foreground_popup",
    )
    store.insert_outcome(iid, T0 + timedelta(minutes=10), OutcomeVerdict("partial", 45.0, 30.0, 10.0))
    row = store.fetch_outcome(iid)
    assert row["outcome"] == "partial"
    assert row["ent_before"] == 45.0
    assert row["ent_after"] == 30.0


def test_last_intervention_at_ignores_failed_delivery(store):
    """冷却要按「用户真的被打扰过」来算，投递失败的不能算。"""
    eid = store.insert_evaluation(T0, _verdict(), _decision())
    store.insert_intervention(
        eid,
        T0,
        "PASSIVE_CONSUMPTION",
        False,
        "walk5",
        "走 5 分钟",
        "failed: toast unavailable",
        T0 + timedelta(minutes=10),
        channel="foreground_popup",
    )
    assert store.last_intervention_at() is None
    store.insert_intervention(
        eid,
        T0 + timedelta(minutes=1),
        "PASSIVE_CONSUMPTION",
        False,
        "walk5",
        "走 5 分钟",
        "delivered",
        T0 + timedelta(minutes=11),
        channel="foreground_popup",
    )
    assert store.last_intervention_at() == T0 + timedelta(minutes=1)


def test_intervention_count_since(store):
    eid = store.insert_evaluation(T0, _verdict(), _decision())
    for i in range(3):
        store.insert_intervention(
            eid,
            T0 + timedelta(minutes=i),
            "PASSIVE_CONSUMPTION",
            False,
            "walk5",
            "走 5 分钟",
            "delivered",
            T0 + timedelta(minutes=10 + i),
            channel="foreground_popup",
        )
    assert store.intervention_count_since(T0) == 3
    assert store.intervention_count_since(T0 + timedelta(minutes=1)) == 2


def test_kv_roundtrip_and_default(store):
    assert store.get_kv("action_cursor") is None
    store.set_kv("action_cursor", "calligraphy")
    assert store.get_kv("action_cursor") == "calligraphy"
    store.set_kv("action_cursor", "stretch")
    assert store.get_kv("action_cursor") == "stretch"


def test_user_response_roundtrip(store):
    eid = store.insert_evaluation(T0, _verdict(), _decision())
    iid = store.insert_intervention(
        eid, T0, "PASSIVE_CONSUMPTION", False, "walk5", "走 5 分钟", "delivered",
        T0 + timedelta(minutes=10), user_response="accepted",
        channel="foreground_popup",
    )
    assert store.fetch_intervention(iid)["user_response"] == "accepted"


def test_user_response_defaults_to_null(store):
    eid = store.insert_evaluation(T0, _verdict(), _decision())
    iid = store.insert_intervention(
        eid, T0, "PASSIVE_CONSUMPTION", False, "walk5", "走 5 分钟", "delivered",
        T0 + timedelta(minutes=10),
        channel="foreground_popup",
    )
    assert store.fetch_intervention(iid)["user_response"] is None
