-- evaluations：每次评估都写，调阈值与复盘全靠它
CREATE TABLE IF NOT EXISTS evaluations (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  window_minutes INTEGER NOT NULL,
  total_active_minutes REAL NOT NULL,
  ent_minutes REAL NOT NULL,
  gray_minutes REAL NOT NULL,
  work_minutes REAL NOT NULL,
  ent_ratio REAL NOT NULL,
  state TEXT NOT NULL,
  late_night INTEGER NOT NULL,
  data_status TEXT NOT NULL,
  prev_state TEXT,
  decision TEXT NOT NULL,
  gate_trace TEXT NOT NULL
);

-- interventions：每次真正发出的干预
CREATE TABLE IF NOT EXISTS interventions (
  id INTEGER PRIMARY KEY,
  evaluation_id INTEGER NOT NULL REFERENCES evaluations(id),
  at TEXT NOT NULL,
  state TEXT NOT NULL,
  late_night INTEGER NOT NULL,
  action_id TEXT NOT NULL,
  action_text TEXT NOT NULL,
  delivery_status TEXT NOT NULL,
  outcome_due_at TEXT NOT NULL
);

-- outcomes：一次干预对应一行回执
CREATE TABLE IF NOT EXISTS outcomes (
  intervention_id INTEGER PRIMARY KEY REFERENCES interventions(id),
  checked_at TEXT NOT NULL,
  outcome TEXT NOT NULL,
  ent_before REAL NOT NULL,
  ent_after REAL NOT NULL,
  after_window_minutes REAL NOT NULL
);

-- kv：少量运行期状态（动作池轮转游标等）
CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evaluations_at ON evaluations(at);
CREATE INDEX IF NOT EXISTS idx_interventions_due ON interventions(outcome_due_at);
CREATE INDEX IF NOT EXISTS idx_interventions_at ON interventions(at);
