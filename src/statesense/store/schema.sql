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
  -- 1 = 本轮因采集中断未下结论。必须显式存，否则 evaluations.state 里的 NORMAL
  -- 分不清「真的正常」和「根本没采到数据」。
  skipped INTEGER NOT NULL DEFAULT 0,
  -- 条目分钟数之和。与 total_active_minutes 的差额是「明细缺失」，
  -- 与 (ent+gray+work) 的差额是「未命中任何规则」。V0.5 漏判视图靠它区分两者。
  entries_minutes REAL NOT NULL DEFAULT 0,
  -- SHQueryUserNotificationState 的原始返回值；NULL = 无法判定。
  -- 记原始值而不是布尔：这条「自动推断」需要事后审计准确率，
  -- 只存 true/false 就再也答不上「它当时到底看到了什么」。
  fullscreen_state INTEGER,
  -- 判定这套规则（分类清单 + 状态阈值 + 全屏提权语义）的标识，见 rulebook.py。
  -- 结果行里没有规则本身，跨版本比较阈值时必须靠它把样本分组；
  -- NULL = 迁移前写入的行，**版本未知**，不得与已知版本混算。
  rule_version TEXT,
  prev_state TEXT,
  decision TEXT NOT NULL,
  gate_trace TEXT NOT NULL
);

-- interventions：每次真正发出的干预（含 --dry-run 的排练，靠 channel 区分）
CREATE TABLE IF NOT EXISTS interventions (
  id INTEGER PRIMARY KEY,
  evaluation_id INTEGER NOT NULL REFERENCES evaluations(id),
  at TEXT NOT NULL,
  state TEXT NOT NULL,
  late_night INTEGER NOT NULL,
  action_id TEXT NOT NULL,
  action_text TEXT NOT NULL,
  delivery_status TEXT NOT NULL,
  -- 投递通道（foreground_popup / recording）。`--dry-run` 走 recording，
  -- 它同样返回 delivered —— 不记通道，排练与真实干预在库里就完全一样，
  -- 「到底有没有真的弹过窗」将永远答不上来。NULL = 迁移前写入的行，通道未知。
  channel TEXT,
  outcome_due_at TEXT NOT NULL,
  -- 弹窗上用户点的按钮：accepted / declined / NULL（没理会或超时）
  user_response TEXT
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

-- run_events：只记录异常轮次，正常存活由 evaluations.at 派生。
-- 补上这两类事件后，「无记录」只剩「进程死了」一个解释。
--
-- `at` 是主键（spec §7.2 的规定），因此同一秒内只能留下一行 —— 写入用的是
-- INSERT OR REPLACE，同一时刻的第二条会**覆盖**第一条而不是并存。
-- 实际上睡眠跳过与单轮异常不可能发生在同一秒（前者当轮提前 return），
-- 所以这是有意的取巧而非缺陷；但它确实是「已知边界」，见 spec §7.6。
CREATE TABLE IF NOT EXISTS run_events (
  at     TEXT PRIMARY KEY,
  kind   TEXT NOT NULL,   -- 封闭枚举：sleep_gap | tick_error
  detail TEXT NOT NULL    -- sleep_gap: 空档分钟数；tick_error: 异常类名 + 消息首行
);
