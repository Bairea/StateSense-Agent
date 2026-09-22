"""SQLite 持久化。schema 版本用 PRAGMA user_version 管理。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from statesense._time import iso as _iso
from statesense._time import parse_iso as _parse
from statesense.intervention.models import Decision, dump_gate_trace
from statesense.outcome.models import OutcomeVerdict
from statesense.state.models import StateVerdict

SCHEMA_VERSION = 7
_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

#: 运行事件的封闭枚举。写入未知类型必须报错 —— 与 gate.enabled 的处理同一原则：
#: 不认识的取值意味着写入方与 schema 已漂移，静默接受会让观测结论失真。
RUN_EVENT_KINDS: tuple[str, ...] = ("sleep_gap", "tick_error")

#: report 的只读查询能读的表 → 该表的时间列。写成封闭字面量表而不是让调用方
#: 传表名：表名不来自外部输入，拼进 SQL 才是安全的。
#: 时间过滤走 SQL 字符串比较 —— `_iso()` 统一转本地时区后序列化，所有落库
#: 字符串的偏移量一致，字典序即时间序。`since` 必须先经 `_iso()` 转换。
_LISTABLE: dict[str, str] = {
    "evaluations": "at",
    "interventions": "at",
    "outcomes": "checked_at",
    "run_events": "at",
}


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


@dataclass(frozen=True)
class DueIntervention:
    id: int
    at: datetime
    #: 触发这条干预的评估行。回执要按「干预发生时那一轮的证据」来算，
    #: 而全屏取值、ent 都在那一行里 —— 不带它出来，回执只能去问当下，
    #: 那就成了用今天的全屏状态解释昨天的窗口（阶段 2.2 要修的正是这个）。
    evaluation_id: int


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
        # v3 → v4：evaluations 增加 entries_minutes（区分漏判与明细缺失）。
        # run_events 由 schema.sql 的 CREATE TABLE IF NOT EXISTS 建出，无需 ALTER 分支。
        if "entries_minutes" not in _column_names(self._conn, "evaluations"):
            self._conn.execute(
                "ALTER TABLE evaluations ADD COLUMN entries_minutes REAL NOT NULL DEFAULT 0"
            )
        # v4 → v5：evaluations 增加 fullscreen_state（全屏 D3D 信号）。
        # 旧行留 NULL = 「无法判定」—— 不伪造「当时不是全屏」。
        if "fullscreen_state" not in _column_names(self._conn, "evaluations"):
            self._conn.execute("ALTER TABLE evaluations ADD COLUMN fullscreen_state INTEGER")
        # v5 → v6：interventions 增加 channel（投递通道）。
        # 旧行留 NULL = 「通道未知」。加这一列是因为 `--dry-run` 走 RecordingNotifier，
        # 它同样返回 status='delivered'，于是排练出来的干预与真实干预在库里一模一样 ——
        # 「今晚到底有没有真的弹过窗」这个最基本的问题将永远答不上来。
        # 通道本来就在 DeliveryResult 上，只是过去没有被落库。
        if "channel" not in _column_names(self._conn, "interventions"):
            self._conn.execute("ALTER TABLE interventions ADD COLUMN channel TEXT")
        # v6 → v7：evaluations 增加 rule_version（判定规则标识）。
        # 旧行留 NULL = 「版本未知」。**不回填成当前版本** —— 那时的判定是用
        # 当时的规则做的，填上今天的版本等于伪造历史，而跨版本比较正是靠这一列
        # 分组；一行错标就能把两套规则混成一份样本。
        if "rule_version" not in _column_names(self._conn, "evaluations"):
            self._conn.execute("ALTER TABLE evaluations ADD COLUMN rule_version TEXT")

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

    def insert_evaluation(
        self,
        at: datetime,
        verdict: StateVerdict,
        decision: Decision,
        *,
        rule_version: str,
    ) -> int:
        """`rule_version` 与被记录的事实同列，因此不给默认值、且强制写成关键字。

        一个「忘了传就取当前版本」的默认值，会在有人写第二处调用点时悄悄生效，
        而后果是历史行被贴上错误的规则版本 —— 跨版本比较随即失真，
        且从数据上查不出来。这与 `channel` 当初的处理是同一个原则。
        """
        trace = dump_gate_trace(decision.gate_trace)
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO evaluations (
                  at, window_minutes, total_active_minutes, ent_minutes, gray_minutes,
                  work_minutes, ent_ratio, entries_minutes, fullscreen_state, rule_version,
                  state, late_night, data_status, skipped, prev_state, decision, gate_trace
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _iso(at),
                    verdict.window_minutes,
                    verdict.total_active_minutes,
                    verdict.ent_minutes,
                    verdict.gray_minutes,
                    verdict.work_minutes,
                    verdict.ent_ratio,
                    verdict.entries_minutes,
                    verdict.fullscreen_state,
                    rule_version,
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
        *,
        channel: str,
        user_response: str | None = None,
    ) -> int:
        """`channel` 与 `user_response` 一样是**记录下来的事实**，因此不给默认值，
        并且强制写成关键字 —— 位置参数里夹一个会悄悄取默认值的通道，
        正是让「排练」与「真弹窗」混在一起的那种写法。

        少了 channel，`--dry-run` 排练出的干预与真实干预在库里无法区分，
        视图 3 / 视图 4 的结论会同时被两种数据污染。
        """
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO interventions (
                  evaluation_id, at, state, late_night, action_id, action_text,
                  delivery_status, channel, outcome_due_at, user_response
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation_id,
                    _iso(at),
                    state,
                    int(late_night),
                    action_id,
                    action_text,
                    delivery_status,
                    channel,
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

    def insert_run_event(self, at: datetime, kind: str, detail: str) -> None:
        """记录一次异常轮次（休眠跳过 / 本轮异常）。

        绝不写 interventions —— 运维信号与干预信号混在一起会占用 daily_cap、
        消耗 cooldown，并污染 outcomes 的效果分析。
        """
        if kind not in RUN_EVENT_KINDS:
            raise ValueError(f"未知的运行事件类型 {kind!r}；已知：{list(RUN_EVENT_KINDS)}")
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO run_events (at, kind, detail) VALUES (?, ?, ?)",
                (_iso(at), kind, detail),
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
            SELECT i.id AS id, i.at AS at, i.evaluation_id AS evaluation_id
            FROM interventions i
            LEFT JOIN outcomes o ON o.intervention_id = i.id
            WHERE o.intervention_id IS NULL AND i.outcome_due_at <= ?
            ORDER BY i.id
            """,
            (_iso(now),),
        ).fetchall()
        return [
            DueIntervention(id=r["id"], at=_parse(r["at"]), evaluation_id=r["evaluation_id"])
            for r in rows
        ]

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

    # ── 只读查询（report 用） ────────────────────────────────
    # 一律以 list_ 开头，便于评审时一眼确认 report 路径不写库。

    def _list(self, table: str, since: datetime | None) -> list[sqlite3.Row]:
        column = _LISTABLE[table]
        if since is None:
            return self._conn.execute(
                f"SELECT * FROM {table} ORDER BY {column}"
            ).fetchall()
        return self._conn.execute(
            f"SELECT * FROM {table} WHERE {column} >= ? ORDER BY {column}", (_iso(since),)
        ).fetchall()

    def list_evaluations(self, since: datetime | None = None) -> list[sqlite3.Row]:
        return self._list("evaluations", since)

    def list_interventions(self, since: datetime | None = None) -> list[sqlite3.Row]:
        return self._list("interventions", since)

    def list_outcomes(self, since: datetime | None = None) -> list[sqlite3.Row]:
        return self._list("outcomes", since)

    def list_run_events(self, since: datetime | None = None) -> list[sqlite3.Row]:
        return self._list("run_events", since)

    def list_intervention_cohort(self, since: datetime | None = None) -> list[sqlite3.Row]:
        """同一批受试干预：一条干预一行，带上它的回执与触发时那一轮的评估。

        **过滤轴是干预发生的时刻，只有一个。** 之前 report 用两个时间轴拼分母：
        干预按 `interventions.at` 过滤、回执按 `outcomes.checked_at` 过滤 ——
        于是「区间起点前投递、区间内检查」的回执进得来，对应的干预却进不来，
        报告里凭空多出一层 `orphan`。那不是数据有问题，是取数口径有问题。

        左连接是刻意的：回执缺失、触发评估行缺失本身就是分析要数出来的层，
        用内连接会把它们静默丢掉，而丢掉的部分正是「分母为什么变小」的答案。
        缺失由 report 层显式分成不同层，不在这里兜底。

        `rows` 的列是 interventions 的全集 + outcomes 的 outcome/checked_at/
        ent_before/ent_after + 触发评估的 `eval_*`。给「判定规则版本」留了
        `eval_rule_version`：跨版本比较时按它分组，旧行是 NULL = 版本未知。
        """
        sql = """
            SELECT
              i.*,
              o.outcome          AS outcome,
              o.checked_at       AS outcome_checked_at,
              o.ent_before       AS ent_before,
              o.ent_after        AS ent_after,
              e.state            AS eval_state,
              e.late_night       AS eval_late_night,
              e.ent_minutes      AS eval_ent_minutes,
              e.ent_ratio        AS eval_ent_ratio,
              e.rule_version     AS eval_rule_version,
              e.fullscreen_state AS eval_fullscreen_state
            FROM interventions i
            LEFT JOIN outcomes o ON o.intervention_id = i.id
            LEFT JOIN evaluations e ON e.id = i.evaluation_id
        """
        if since is None:
            return self._conn.execute(f"{sql} ORDER BY i.at").fetchall()
        return self._conn.execute(
            f"{sql} WHERE i.at >= ? ORDER BY i.at", (_iso(since),)
        ).fetchall()
