"""SQLite 持久化。schema 版本用 PRAGMA user_version 管理。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from statesense.intervention.models import Decision
from statesense.outcome.models import OutcomeVerdict
from statesense.state.models import StateVerdict

SCHEMA_VERSION = 3
_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _iso(moment: datetime) -> str:
    return moment.astimezone(tz=None).isoformat()


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


@dataclass(frozen=True)
class DueIntervention:
    id: int
    at: datetime


class Store:
    def __init__(self, db_path: Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")

    # ── 生命周期 ────────────────────────────────────────────

    def migrate(self) -> None:
        with self._conn:
            self._conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
            self._apply_incremental_migrations()
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _apply_incremental_migrations(self) -> None:
        """按列是否存在逐项补齐，老库原地升级。"""
        # v1 → v2：interventions 增加 user_response（弹窗按钮回执）
        if "user_response" not in _column_names(self._conn, "interventions"):
            self._conn.execute("ALTER TABLE interventions ADD COLUMN user_response TEXT")
        # v2 → v3：evaluations 增加 skipped（区分「真的正常」与「根本没采到数据」）
        if "skipped" not in _column_names(self._conn, "evaluations"):
            self._conn.execute(
                "ALTER TABLE evaluations ADD COLUMN skipped INTEGER NOT NULL DEFAULT 0"
            )

    def user_version(self) -> int:
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def close(self) -> None:
        self._conn.close()

    # ── 写入 ────────────────────────────────────────────────

    def _previous_state(self) -> str | None:
        row = self._conn.execute(
            "SELECT state FROM evaluations ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row["state"] if row else None

    def insert_evaluation(self, at: datetime, verdict: StateVerdict, decision: Decision) -> int:
        trace = json.dumps(
            [
                {"name": g.name, "passed": g.passed, "value": g.value, "threshold": g.threshold}
                for g in decision.gate_trace
            ],
            ensure_ascii=False,
        )
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO evaluations (
                  at, window_minutes, total_active_minutes, ent_minutes, gray_minutes,
                  work_minutes, ent_ratio, state, late_night, data_status, skipped,
                  prev_state, decision, gate_trace
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _iso(at),
                    verdict.window_minutes,
                    verdict.total_active_minutes,
                    verdict.ent_minutes,
                    verdict.gray_minutes,
                    verdict.work_minutes,
                    verdict.ent_ratio,
                    str(verdict.state),
                    int(verdict.late_night),
                    verdict.data_status,
                    int(verdict.skipped),
                    self._previous_state(),
                    "intervene" if decision.intervene else "skip",
                    trace,
                ),
            )
            return int(cur.lastrowid)

    def insert_intervention(
        self,
        evaluation_id: int,
        at: datetime,
        state: str,
        late_night: bool,
        action_id: str,
        action_text: str,
        delivery_status: str,
        outcome_due_at: datetime,
        user_response: str | None = None,
    ) -> int:
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO interventions (
                  evaluation_id, at, state, late_night, action_id, action_text,
                  delivery_status, outcome_due_at, user_response
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation_id,
                    _iso(at),
                    state,
                    int(late_night),
                    action_id,
                    action_text,
                    delivery_status,
                    _iso(outcome_due_at),
                    user_response,
                ),
            )
            return int(cur.lastrowid)

    def fetch_intervention(self, intervention_id: int) -> sqlite3.Row:
        return self._conn.execute(
            "SELECT * FROM interventions WHERE id = ?", (intervention_id,)
        ).fetchone()

    def insert_outcome(
        self, intervention_id: int, checked_at: datetime, verdict: OutcomeVerdict
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO outcomes (
                  intervention_id, checked_at, outcome, ent_before, ent_after,
                  after_window_minutes
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    intervention_id,
                    _iso(checked_at),
                    verdict.outcome,
                    verdict.ent_before,
                    verdict.ent_after,
                    verdict.after_window_minutes,
                ),
            )

    def set_kv(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value)
            )

    # ── 读取 ────────────────────────────────────────────────

    def get_kv(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def fetch_evaluation(self, evaluation_id: int) -> sqlite3.Row:
        return self._conn.execute(
            "SELECT * FROM evaluations WHERE id = ?", (evaluation_id,)
        ).fetchone()

    def fetch_outcome(self, intervention_id: int) -> sqlite3.Row:
        return self._conn.execute(
            "SELECT * FROM outcomes WHERE intervention_id = ?", (intervention_id,)
        ).fetchone()

    def due_interventions(self, now: datetime) -> list[DueIntervention]:
        rows = self._conn.execute(
            """
            SELECT i.id AS id, i.at AS at
            FROM interventions i
            LEFT JOIN outcomes o ON o.intervention_id = i.id
            WHERE o.intervention_id IS NULL AND i.outcome_due_at <= ?
            ORDER BY i.id
            """,
            (_iso(now),),
        ).fetchall()
        return [DueIntervention(id=r["id"], at=_parse(r["at"])) for r in rows]

    def last_intervention_at(self) -> datetime | None:
        row = self._conn.execute(
            "SELECT at FROM interventions WHERE delivery_status = 'delivered' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return _parse(row["at"]) if row else None

    def intervention_count_since(self, moment: datetime) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM interventions WHERE delivery_status = 'delivered' AND at >= ?",
            (_iso(moment),),
        ).fetchone()
        return int(row["n"])
