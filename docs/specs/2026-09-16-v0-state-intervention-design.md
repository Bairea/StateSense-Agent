# StateSense-Agent V0 技术规格

| 项 | 值 |
|---|---|
| 日期 | 2026-09-16 |
| 状态 | **待评审**（评审通过后方可进入实现） |
| 范围 | 整体架构定位 + V0 可实施规格 |
| 上游输入 | [`prd1.md`](../../prd1.md)、[`ref1.md`](../../ref1.md)、[`docs/project-understanding.md`](../project-understanding.md) |
| 评审人 | @Bairea |

---

## 1. 背景与问题

在电脑上本来只想娱乐一会儿，结果刷 B 站刷了两小时，正事一件没干。

关键洞察（`prd1.md`）：

> 你在某个状态里时，往往已经失去了主动性，被困在娱乐状态了。

所以需要的是一个持续感知状态、判断是否需要介入、并在正确时机轻推一下的系统。核心循环：

```
感知 → 状态推断 → 判断是否介入 → 给出极低成本动作 → 观察结果 → 更新对你的认识
```

系统要识别的不是「我此刻打开了 B 站」，而是**「我进入被动消费状态了吗」**。

---

## 2. 成功判据

V0 只验证一个问题：

> **这种提醒到底有没有价值？**

具体判据（沿用 `ref1.md`）：

连续使用若干天后，如果出现过「它真的有几次在我正要继续刷的时候把我拽出来了」的感受，V0 即算成功，值得投入做完整的 State Agent。

可观测的量化代理指标：`outcomes` 表中 `outcome = 'disengaged'` 的占比。

---

## 3. 非目标（V0 明确不做）

- 不做 LLM。文案由动作池 + 模板生成，接入点预留为 `Wording` 接口。
- 不做个性化策略学习。
- 不做跨设备、不做云同步、不做多用户。
- 不消费屏幕文本（OCR / 聊天记录 / 邮件正文 / 密码页面）。
- 不替换或重造 Screenpipe 的采集能力。

---

## 4. 架构

### 4.1 分层

```
┌─────────────────────────────────────┐
│ Screenpipe Recorder（外部，已有）    │  app / window / browser_url / 时长
└──────────────┬──────────────────────┘
               │ REST  GET /activity-summary   (Bearer, 最小字段)
               ▼
┌─────────────────────────────────────────────────────────┐
│ StateSense Agent（本项目）                               │
│                                                          │
│  Scheduler ──每 N 分钟──┐                                │
│                          ▼                                │
│  ① ActivityReader   ── 唯一接触 Screenpipe 的组件         │
│  ② StateEngine      ── 活动快照 → 状态（纯函数）          │
│  ③ InterventionDecider ── 状态+历史 → 是否介入（纯函数）  │
│  ④ Notifier         ── 投递（V0: 原生前台弹窗）           │
│  ⑤ OutcomeTracker   ── T+10min 复查 → 行为回执            │
│  ⑥ Store (SQLite)   ── evaluations / interventions /      │
│                        outcomes                           │
└─────────────────────────────────────────────────────────┘
```

### 4.2 组件职责边界

| 组件 | 做什么 | 明确不做什么 |
|---|---|---|
| `Scheduler` | 每分钟 tick，按配置决定何时评估、何时查回执 | 不含任何判定逻辑 |
| `ActivityReader` | 封装 base URL / token / 最小字段参数 / 防御式解析 / `data_status` 校验 | 不解释数据含义 |
| `StateEngine` | `(ActivitySnapshot, Config) → StateVerdict`，**纯函数** | 无 IO、不读时钟（时间注入） |
| `InterventionDecider` | `(StateVerdict, History, Config) → Decision`，**纯函数** | 不负责表现层文案 |
| `Notifier` | `notify(Intervention) → DeliveryResult`，接口化 | 失败必须返回结构化错误，不许静默 |
| `OutcomeTracker` | 干预后 T+delay 复查，算被动消费变化 | 不改写原始评估数据 |

### 4.3 一次 tick 的数据流

```
tick(t)
 ├─ 1. OutcomeTracker.due(t)         → 给到期的旧干预补写 outcomes 行
 ├─ 2. ActivityReader.read(t-60m, t) → ActivitySnapshot
 ├─ 3. StateEngine.classify(snap)    → StateVerdict
 ├─ 4. Decider.decide(verdict, hist) → Decision{intervene, actionId, reason, gateTrace}
 ├─ 5. 写 evaluations 行（每次都写）
 ├─ 6. 若 intervene → Wording.render() → Notifier.notify()
 │                     → 写 interventions 行（outcome_due_at = t + delay）
 └─ 7. state 与 prev_state 不同时即为一次状态迁移（不单独建表）
```

### 4.4 三条边界原则

1. **只有 `ActivityReader` 知道 Screenpipe 存在** → 换数据源只改一处。
2. **`StateEngine` / `InterventionDecider` 是纯函数** → 判定逻辑可回归测试，不依赖真实录制。这是本规格最重要的可测性保证。
3. **`Notifier` / `Wording` 是接口** → 换投递通道或将来接 LLM，判定逻辑一行不用动。

---

## 5. 状态模型

### 5.1 输入契约

`ActivityReader` 的产物，也是可测性的锚点：

```python
@dataclass(frozen=True)
class Entry:
    app: str      # 进程名，如 "chrome.exe"
    title: str    # 窗口标题，如 "Usage - Command Code - Google Chrome"
    url: str      # 可为空字符串
    minutes: float

@dataclass(frozen=True)
class ActivitySnapshot:
    window_start: datetime
    window_end: datetime
    window_minutes: int
    total_active_minutes: float
    entries: tuple[Entry, ...]
    data_status: str          # ok | empty_but_recording | no_capture_in_range | not_recording | unreachable
    captured_at: datetime
```

`entries` 只用 `app` / `title` / `url` 三个文本字段做匹配，**不含任何屏幕文本**。

关于 `data_status` 的两条硬约束：

1. **它是封闭枚举。** 读到枚举外的值（含字段缺失）一律按 `unreachable` 处理 —— 不认识的字段意味着响应结构与预期不符，此时任何「没有活动」的结论都不成立。
2. **`total_active_minutes` 只取服务端字段，绝不用 `entries` 之和估算。** `entries` 是窗口粒度记录，拿它反推时长正是 §15.1 明令禁止的做法。

`entries` 只从响应的 `windows` 字段构造。**不提供 `apps` 回退**：`apps` 没有 `title` / `url`，用它顶替只能靠进程名匹配，属于静默降质（例如 `chrome.exe` 完全无法区分刷视频与查文档）。明细缺失时留空并记日志，让缺口可见。

### 5.2 分类

三档，清单全部走配置：

| 档 | 用途 | 默认命中示例 |
|---|---|---|
| `ENTERTAINMENT` | 计入被动消费 | bilibili / 哔哩哔哩 / B站、抖音、youtube、爱奇艺、优酷、腾讯视频、芒果TV、netflix、disney+、hbo、twitch、快手、小红书、微博、steam、epic games、battle.net、wegame、明日方舟、网易云音乐、QQ音乐、spotify、酷狗、酷我 |
| `GRAY` | **单独统计，不计入任何一档** | 知乎、豆瓣、贴吧、reddit、news |
| `WORK` | 仅作上下文与 ratio 解释 | github、gitlab、stackoverflow、docs.*、readthedocs、Code / Zed / PyCharm / Cursor / Trae、terminal / powershell / bash / cmd.exe、figma / blender / unity / godot |

匹配方式：配置项为 Python 正则表达式（编译时统一加 `re.IGNORECASE`），对 `app`、`title`、`url` 三者做 OR 匹配，命中即归类。纯关键词本身就是合法正则，无需额外语法。

`GRAY` 是刻意留的：`ref1.md` 已指出「知乎可能阅读、也可能刷」。硬塞进任一档都会污染判定；单独计数后可以先观察它的真实分布，再决定归属——**这本身就是 V0 要产出的认识之一**。

### 5.3 状态定义

```
ent   = ENTERTAINMENT 档在窗口内的分钟数
total = total_active_minutes
ratio = ent / total        (total == 0 时 ratio = 0)

NORMAL                          ent < 20
WORKING                         ——     V0 不判定（见 §5.5）
WATCH                           20 ≤ ent < 40
PASSIVE_CONSUMPTION             ent ≥ 40
HIGH_RISK_PASSIVE_CONSUMPTION   ent ≥ 65
```

阈值来自 `ref1.md` 的刻度（5 / 20 / 40 / 65），全部可配。

### 5.4 `late_night` 是正交标记，不是状态

`ref1.md` 把 `LATE_NIGHT` 与其他状态并列。本规格**有意偏离**：把它做成独立布尔字段，可与任意 `state` 共存。

理由：凌晨 3 点刷 B 站，「凌晨」和「被动消费」是两个正交事实。若压成一个枚举，必须二选一，必然丢一个：

| 情形 | `state` | `late_night` | 干预 |
|---|---|---|---|
| 下午刷 40 min | `PASSIVE_CONSUMPTION` | `false` | 普通轻推 |
| 凌晨 3 点刷 40 min | `PASSIVE_CONSUMPTION` | `true` | 更强——明天要还债 |
| **凌晨 3 点写代码** | `WORKING` | `true` | **也该提醒，但理由完全不同** |

第三行是「当成状态」做不到的：它的 `state` 是 `WORKING` 而非 `LATE_NIGHT`，但「凌晨」这个事实依然值得介入，所以它必须能独立存在。

定义：`late_night = (local_hour ∈ [1, 6)) and (total_active_minutes ≥ 10)`。

### 5.5 `WORKING` 在 V0 不判定

V0 的判定目标是「是否被困在被动消费」，`WORKING` 不参与状态机。`WORK` 分类只用于报告与 ratio 解释。这样避免在没有可靠正向证据时误判工作状态。留待 V1。

---

## 6. 介入闸门（InterventionGate）

**状态回答「我在什么状态」，闸门回答「现在该不该打扰」。两者分离。**

```
intervene = AND(
  ① state ∈ { PASSIVE_CONSUMPTION, HIGH_RISK_PASSIVE_CONSUMPTION }
  ② ratio ≥ gate.ratio_min
  ③ now − last_intervention_at ≥ gate.cooldown_minutes
  ④ 今日干预次数 < gate.daily_cap
)
```

### 6.1 可插拔条件

闸门实现为**条件列表**，每个条件返回：

```python
@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    value: float
    threshold: float
```

`Decision.gateTrace` 收集全部结果并入库。**这是硬要求**：日志必须能看出是被哪条闸门挡下的，否则无法判断阈值调得对不对。

V0 启用四个条件：`state_min`、`ratio_min`、`cooldown`、`daily_cap`。

### 6.2 占比定义：V0 只启用「窗口占比」

| # | 定义 | 含义 | 状态 |
|---|---|---|---|
| 1 | `ent / total_active` | 这段时间里多大比例在被动消费 | **V0 启用** |
| 2 | `ent / window_minutes` | 窗口内实打实刷了多久 | 未启用（与绝对阈值 `ent` 高度重合） |
| 3 | `ent / (total_active − work)` | 排除工作后的被动消费占比 | 未启用（V1 可选，避免误伤「边工作边刷」） |
| 4 | 连续段长度 | 最近一次不间断被动消费的分钟数 | 未启用（V1 可选，语义最贴「被困住」，需跨 tick 累积状态） |

**`gate.ratio_min` 不预设值，由项目所有者填写。** 设计上支持将来加入 #3 / #4 作为新条件，届时状态模型一行都不用改。

---

## 7. 干预动作

### 7.1 动作池

配置驱动，示例：

```toml
[[actions]]
id = "walk5"
text = "离开电脑走 5 分钟"
applies_to = ["PASSIVE_CONSUMPTION", "HIGH_RISK_PASSIVE_CONSUMPTION"]

[[actions]]
id = "calligraphy"
text = "练字 5 分钟"
applies_to = ["PASSIVE_CONSUMPTION", "HIGH_RISK_PASSIVE_CONSUMPTION"]

[[actions]]
id = "reading"
text = "读一段《道德经》"
applies_to = ["HIGH_RISK_PASSIVE_CONSUMPTION"]
```

### 7.2 选择策略：轮转，不用随机

`applies_to` 命中当前状态的候选中，按轮转（round-robin）选取，游标持久化在 `store`。

**为什么不用随机**：随机会让「哪个动作最有效」无法归因。轮转保证每个动作拿到大致均衡的样本，正好喂给 V1 的「什么干预最有效」。

### 7.3 文案渲染（`Wording` 接口）

```python
class Wording(Protocol):
    def render(self, verdict: StateVerdict, action: Action, top_label: str | None) -> str: ...
```

V0 实现 `TemplateWording`，输出形如：

```
最近 60 分钟里有 47 分钟在 B 站（占 78%）。起来走 5 分钟？
```

`ref1.md` 把 LLM 放在 V2 处理模糊状态与个性化策略；V0 预留此接口，接入 LLM 时不动其他组件。

---

## 8. 投递（Notifier）

### 8.1 契约

```python
@dataclass(frozen=True)
class DeliveryResult:
    status: str                        # "delivered" | "failed"
    channel: str                       # "foreground_popup"
    error: str | None
    delivered_at: datetime | None
    user_response: str | None = None   # "accepted" | "declined" | None（没理会/超时）
```

失败必须返回结构化错误，**不许静默吞掉**。

### 8.2 通道选择：前台弹窗，不是 Windows Toast

原始设计用 Windows Toast。2026-09-16 的真机实测推翻了它的**每一条**前提：

| 实测事实 | 后果 |
|---|---|
| `HKCU\...\PushNotifications\ToastEnabled = 0` | 全局通知开关是关的，Toast 被系统整体丢弃。而 `show()` 仍返回成功 —— 典型的**静默失败** |
| Windows 在「全屏应用」下抑制通知 | 本项目核心场景正是全屏刷视频，Toast 恰在最该生效时失效 |
| Win10 的 Toast 需要已注册的 AUMID | `winotify` **并不**自动注册；自定义 `app_id` 的 Toast 不会显示 |
| `winotify` 用 `Popen(..., stdout=DEVNULL, stderr=DEVNULL)` | 失败信息被彻底丢弃，这正是「API 说成功、屏幕上什么都没有」的来源 |

改用**原生 MessageBox + 显式抢前台**：

```python
flags = MB_YESNO | MB_ICONWARNING | MB_TOPMOST | MB_SETFOREGROUND | MB_SYSTEMMODAL
```

```
弹窗在独立线程里创建（MessageBoxW 阻塞，不能挡住调度器）
        ↓
主流程轮询 FindWindowW(title) 拿到对话框句柄
        ↓
AttachThreadInput → BringWindowToTop → SetForegroundWindow
        ↓
SetWindowPos(HWND_TOPMOST) + FlashWindow
        ↓
winsound.MessageBeep
```

**为什么必须显式抢前台**：Windows 有「前台锁定」——后台进程不允许抢前台窗口。只加 `MB_TOPMOST | MB_SETFOREGROUND` 时，窗口会被创建却压在全屏应用后面（只闻其声、不见其形）。

实测矩阵（2026-09-16，本机 Windows 10 19045）：

| 场景 | 结果 |
|---|---|
| 窗口模式，仅 `MB_TOPMOST` | 可见 |
| **全屏模式，仅 `MB_TOPMOST`** | **不可见**，只听到提示音 |
| **全屏模式，加显式抢前台** | **可见**（`attached=True setforeground=True`） |

收益：运行期第三方依赖降为 **0 个**（只用 `ctypes` 标准库）。

代价：模态窗口会抢焦点、需要一次点击。对「把你从被动消费里拽出来」这个目的，这恰恰是想要的 —— 而且这一次点击顺带给出了显式回执（见 §9）。

### 8.3 一次只留一个窗口

- 显示前先关掉同标题的残留（用户没理会时它会一直挂着）
- 用户完全不理会时，`answer_timeout_seconds`（默认 180s）后由程序自动关闭

关闭顺序如下，每试一种都要确认窗口是否**真的**消失：

```
WM_COMMAND(IDNO) → WM_COMMAND(IDCANCEL) → WM_CLOSE → WM_SYSCOMMAND(SC_CLOSE)
```

只用 `WM_CLOSE` 不够：`MB_YESNO` 没有取消按钮时，关闭按钮会被禁用。

> **超时被程序关闭时，`user_response` 必须是 `None`。**
> 对话框被程序关闭后返回的按钮 id 不是用户的意思，绝不能当成回执。
> 这是实测中真实踩到的缺陷（第一版把「程序关闭」记成了用户点了「否」）。

---

## 9. 回执：显式按钮 + 行为推断（双轨）

### 9.1 显式回执（弹窗按钮）

弹窗带【是】【否】两个按钮，点击结果直接入库到 `interventions.user_response`：

| 值 | 含义 |
|---|---|
| `accepted` | 用户点了「是」（愿意去做那个极小动作） |
| `declined` | 用户点了「否」 |
| `NULL` | 没理会 / 超时被程序关闭 |

**为什么后来加上了按钮**：原设计不做按钮，理由是「按钮需要注册 AUMID + COM 激活器，成本高」，因而选了纯行为推断。改用前台弹窗后**这个成本消失了**（模态对话框天然能拿到点击结果），于是原取舍的前提不再成立。

按钮回执直接补上了行为推断最大的漏洞（见 §16 风险 6）。

### 9.2 行为回执（OutcomeTracker）

干预发生在 `t`，在 `t + delay_minutes`（默认 10）复查：

```
entBefore = [t − 10m, t)  的被动消费分钟
entAfter  = [t, t + 10m)  的被动消费分钟

outcome = disengaged   entAfter < 0.5 × entBefore
        | partial      0.5 × entBefore ≤ entAfter < 0.8 × entBefore
        | continued    entAfter ≥ 0.8 × entBefore
        | no_data      data_status ≠ ok
```

**原始值 `entBefore` / `entAfter` 必须入库**，`outcome` 标签只是派生。「多低才算有效」这个判断以后可能会改，原始数据不能丢。

边界：`entBefore == 0` 时不可能发生（干预的前提就是 `ent ≥ 40`），若出现则记 `no_data` 并记一条内部告警。

### 9.3 两轨的分工

行为回执衡量**实际发生了什么**，按钮回执衡量**用户当时怎么想**。两者不一致本身就是有价值的信息：

| `user_response` | `outcome` | 读法 |
|---|---|---|
| `accepted` | `disengaged` | 提醒有效，用户配合 |
| `accepted` | `continued` | 用户想停但停不下来 —— 要调的是**动作门槛**（V1 改动作池） |
| `declined` | `continued` | 提醒的时机或理由不被认可 —— 要调的是**判定阈值** |
| `NULL` | 任意 | 用户不在或没看到，**这条样本不能用来评价干预效果** |

---

## 10. 数据模型

SQLite，四张表（schema 版本由 `PRAGMA user_version` 管理，当前 v2）。

```sql
-- 每次评估都写：调阈值、复盘全靠它
CREATE TABLE evaluations (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,                 -- ISO8601
  window_minutes INTEGER NOT NULL,
  total_active_minutes REAL NOT NULL,
  ent_minutes REAL NOT NULL,
  gray_minutes REAL NOT NULL,
  work_minutes REAL NOT NULL,
  ent_ratio REAL NOT NULL,
  state TEXT NOT NULL,
  late_night INTEGER NOT NULL,
  data_status TEXT NOT NULL,
  skipped INTEGER NOT NULL DEFAULT 0,  -- 1 = 本轮因采集中断未下结论
  prev_state TEXT,
  decision TEXT NOT NULL,           -- intervene | skip
  gate_trace TEXT NOT NULL          -- JSON: [{name, passed, value, threshold}]
);

CREATE TABLE interventions (
  id INTEGER PRIMARY KEY,
  evaluation_id INTEGER NOT NULL REFERENCES evaluations(id),
  at TEXT NOT NULL,
  state TEXT NOT NULL,
  late_night INTEGER NOT NULL,
  action_id TEXT NOT NULL,
  action_text TEXT NOT NULL,        -- 实际投递的原文
  delivery_status TEXT NOT NULL,    -- delivered | failed:<原因>
  outcome_due_at TEXT NOT NULL,
  user_response TEXT                -- accepted | declined | NULL（没理会/超时）
);

CREATE TABLE outcomes (
  intervention_id INTEGER PRIMARY KEY REFERENCES interventions(id),
  checked_at TEXT NOT NULL,
  outcome TEXT NOT NULL,            -- disengaged | partial | continued | no_data
  ent_before REAL NOT NULL,
  ent_after REAL NOT NULL,
  after_window_minutes REAL NOT NULL
);

-- kv：少量运行期状态（动作池轮转游标等）
CREATE TABLE kv (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE INDEX idx_evaluations_at ON evaluations(at);
CREATE INDEX idx_interventions_due ON interventions(outcome_due_at);
CREATE INDEX idx_interventions_at ON interventions(at);
```

**对 `ref1.md` 的两处有意偏离**：

1. `ref1.md` 说 DB 存 `state_transitions`。本规格不单独建这张表——它完全由 `evaluations` 派生（`state` 与 `prev_state` 不同即为一次迁移）。同一事实存两遍迟早会不一致。
2. 增加了 `kv` 表，用于存放动作池轮转游标这类跨 tick 的运行期状态。轮转需要记住「上次用了哪个动作」，而它不属于上面任何一张事实表。

**迁移**：`migrate()` 必须幂等。v1 → v2 的迁移是给 `interventions` 补 `user_response` 列（`ALTER TABLE ... ADD COLUMN`），老库要能原地升级。

---

## 11. 错误与边界

| 情况 | 处理 |
|---|---|
| Screenpipe 未运行 / 连接失败 | 记 `evaluations(data_status='unreachable')`，不介入、不弹错。连续 5 次失败才写一条告警日志 |
| token 失效（401 / 403） | 启动时校验一次；运行中遇到则记录并跳过本轮，不重试风暴 |
| `data_status != ok` | 记 `skipped`，**不下任何结论** |
| 休眠 / 唤醒 | 用 `captured_at` 检测 gap；超过 2× 窗口长度则跳过本轮并重置连续段状态（V1 用） |
| 弹窗投递失败 | `delivery_status = "failed:<原因>"`，**仍然落 `interventions` 行**（否则回执会错配），下个 tick 重试一次 |
| 弹窗探不到句柄（抢前台超时） | 仍视为已投递（窗口确实被创建了），但记一条 `log.warning`。这通常意味着前台锁定被更强的东西占住 |
| 弹窗未能自动关闭 | 记 `log.warning`。它会在下一次干预前被「清理残留」重试关闭 |
| 回执到期但无数据 | `outcome = 'no_data'`，不猜 |
| 配置非法 | **启动时 fail fast**，不要跑到半夜才发现阈值填错。特别是 `gate.ratio_min` 缺失时必须报错，绝不静默降级为 0 |

---

## 12. 配置

`config.toml`，随仓库提供 `config.example.toml`。

```toml
[screenpipe]
base_url            = "http://localhost:3030"   # 亦可由 SCREENPIPE_LOCAL_API_URL 覆盖
api_key_env         = "SCREENPIPE_LOCAL_API_KEY"
request_timeout_sec = 10

[schedule]
tick_seconds          = 60
evaluate_every_minutes = 5
window_minutes         = 60

[thresholds]
watch_minutes               = 20
passive_minutes             = 40
high_risk_minutes           = 65
late_night_start_hour       = 1
late_night_end_hour         = 6
late_night_min_active_minutes = 10

[gate]
enabled         = ["state_min", "ratio_min", "cooldown", "daily_cap"]
# ratio_min 为必填项，由项目所有者决定。未设置时启动即失败 —— 绝不静默降级为 0
# ratio_min = 0.0
cooldown_minutes = 30
daily_cap        = 8

[outcome]
delay_minutes    = 10
disengaged_ratio = 0.5
continued_ratio  = 0.8

[notify]
channel                   = "foreground_popup"
answer_timeout_seconds    = 180   # 用户不理会的自动关闭时限
foreground_timeout_seconds = 15   # 轮询句柄、抢前台的最长等待

[store]
# 相对 config.toml 所在目录解析；可用 --db 命令行参数覆盖
path = "statesense.db"

[taxonomy]
entertainment = [ /* §5.2 清单 */ ]
gray          = [ /* §5.2 清单 */ ]
work          = [ /* §5.2 清单 */ ]
```

**机器无关要求**：代码中不得出现任何盘符或用户目录硬编码。所有路径、阈值、清单、间隔、冷却、上限均来自本文件；密钥只从环境变量读取。

---

## 13. 测试策略

纯函数是主要测试面。

| 目标 | 方式 |
|---|---|
| `StateEngine` / `InterventionDecider` / `taxonomy` | 表驱动单测，输入固定 `ActivitySnapshot` fixture。**不需要真实录制、不需要 Screenpipe** |
| `ActivityReader` | 喂录制的 JSON 响应（覆盖 `data_status` 各分支、字段缺失、UTF-8 中文标题），验证防御式解析 |
| `OutcomeTracker` | 注入时间，验证窗口计算与边界 |
| `Notifier` | 假实现，验证失败路径与重试 |
| `Wording` | 快照测试，防止文案意外改动 |
| 端到端 | `--once` 手动跑一轮，人工核对三张表的行 |

时间通过 `clock.py` 注入，测试中不依赖真实时钟。

---

## 14. 部署与常驻

**Windows 没有 `service install`** —— Screenpipe 的 `service` 子命令明确只支持 systemd（Linux）与 launchd（macOS），在本机执行返回 `service status is supported on Linux and macOS only`。

因此：

- 本进程为**常驻进程，内部 tick**（不用「每 5 分钟触发一次」的方式——跨 tick 的状态如冷却、轮转游标、连续段没地方存）。
- 由**任务计划程序**在用户登录时拉起，并配置失败后重启。

---

## 15. 运行时前置条件

本节记录 V0 对运行环境的依赖，供协作者复现。

| 依赖 | 说明 |
|---|---|
| Screenpipe Recorder | 需常驻运行并能提供 `GET /activity-summary` |
| 数据目录 | 可重定向（`screenpipe record --data-dir <path>`），建议放在非系统盘 |
| **鉴权** | ⚠️ `/activity-summary` **即使来自 localhost 也返回 403**，必须带 `Authorization: Bearer $SCREENPIPE_LOCAL_API_KEY`（`screenpipe auth token` 获取），并带归因头 `X-Screenpipe-Client: api`、`X-Screenpipe-Agent: statesense` |
| Python | 3.12+，依赖用 `uv` 管理 |
| 不需要 bun / Pipes | 本规格走 REST，不依赖 Screenpipe 的 pipe 机制或 pi agent |
| 前台弹窗 | 见 §8.2 |

### 15.1 已实测的 Screenpipe 行为（2026-09，v0.4.50）

这些事实推翻了若干常见假设，实现时必须按实测来：

| 事实 | 证据 |
|---|---|
| base URL 应由 `SCREENPIPE_LOCAL_API_URL` 覆盖，不要硬编码 3030 | 上游 API 文档明确存在 fallback port 情形 |
| **localhost 不豁免鉴权**，无 token 返回 403 | 本机实测 |
| `key_texts` / `snippets` 默认随响应返回，且包含屏幕上的正文 | 本机实测（响应里能看到窗口内的实际文本） |
| 可在请求中关闭：`include_key_texts=false`、`include_snippets=false`、`include_memories=false`、`include_guidance=false` | 上游 API 文档参数表 |
| **`browser_url` 稀疏但不可替代** | 115 帧样本中仅 1 帧非空；但后来实测到一条**只有 URL 能命中**的记录：窗口标题「三角洲烽火联赛夏季赛」不含任何娱乐关键词，是 URL 里的 `bilibili` 命中规则 |
| `window_name` 才是稳定信号，含页面标题（如 `Usage - Command Code - Google Chrome`） | 本机实测 |
| `data_status` 取值 `ok` / `empty_but_recording` / `no_capture_in_range` / `not_recording` | 上游 API 文档 |
| 不要用 frame count 估算时间 | 上游 API 文档明确警告 |

**`browser_url` 稀疏这一条直接决定分类策略**：域名级分类不能只靠 URL，必须对 `app` + `title` + `url` 三者做 OR 匹配，其中 `title` 是主力。

---

## 16. 已知风险与未决问题

| # | 风险 / 问题 | 影响 | 状态 / 处理 |
|---|---|---|---|
| 1 | ~~全屏下通知被抑制~~ | — | ✅ **已解决**。改用前台弹窗 + 显式抢前台，全屏实测可见（§8.2） |
| 2 | ~~Win10 需要 AUMID 才能投递 Toast~~ | — | ✅ **已消除**。不再使用 Toast，无 AUMID 依赖；运行期第三方依赖降为 0 |
| 3 | `browser_url` 稀疏 | 域名级分类受限 | 已通过 `title` + `app` 匹配缓解 |
| 4 | `GRAY` 档归属未定（知乎等） | 可能低估被动消费 | 先单独统计，用 V0 数据决定 |
| 5 | `WORK` 分类准确性未验证 | 影响 ratio 解释（#3 闸门未启用，影响有限） | V0 不依赖它做判定 |
| 6 | 行为回执把「离开电脑」也算作 `disengaged` | 高估干预效果 | ⚠️ **已缓解**。按钮回执可区分「用户配合」与「用户离开」；分析时应先按 `user_response` 分层 |
| 7 | `gate.ratio_min` 未定 | 闸门不生效 | ✅ **已定：0.75** |
| 8 | 凌晨干预的文案已随 `late_night` 变化，但**强度**未变 | 文案会加「凌晨了。」前缀；闸门与阈值不受影响 | 文案侧已实现；是否在凌晨收紧阈值留待 V1 |
| 9 | **弹窗是模态的，会抢焦点** | 比 Toast 更打断；若频繁触发会显著影响工作 | 由 `cooldown_minutes`(30) 与 `daily_cap`(8) 约束；V0 观察实际打扰感 |
| 10 | **弹窗期间调度器会阻塞** | `notify()` 最多阻塞 `answer_timeout_seconds`(180s)，期间不评估、不查回执 | 可接受（tick 间隔 5 分钟）；若实际影响明显，改为异步 + 回调 |
| 11 | **`ToastEnabled` 曾被关闭** | 说明该用户对通知打扰敏感 | 已尊重用户选择改用主动弹窗；若后续觉得被打扰，优先调 `daily_cap` 而非换通道 |
| 12 | **分类清单按「平台名」匹配，漏掉「窗口标题只有游戏名」的游戏** | 实测刷 `Brotato` 46.9 分钟被判成 `NORMAL`（`ent=0`）—— Steam 启动的游戏，进程名与窗口标题都是游戏自身，不含 `steam` 等关键词 | V0 先靠配置按需补游戏名；`evaluations` 表已如实记录这类漏判，处理方向见验证日志 §10.1 |

---

## 17. 与 `ref1.md` 的差异汇总

| 项 | `ref1.md` | 本规格 | 理由 |
|---|---|---|---|
| V0 形态 | Screenpipe Pipe + LLM | 独立 Python 服务 | 已确定投递与行为回执为必需项，这两者把 V0 从「一个 prompt」变成「一个真程序」；Pipe 形态下发不了通知、回执状态也没处放 |
| 投递通道 | 「Windows / Screenpipe notification」 | 原生前台弹窗 | Toast 在本机实测**全部失败**（全局开关关闭 + 全屏抑制 + AUMID 未注册 + winotify 丢弃错误）；弹窗绕开通知平台（§8.2） |
| `LATE_NIGHT` | 与其他状态并列 | 正交布尔标记 | 两个事实独立，压成一维必然丢信息（§5.4） |
| 占比条件 | 隐含在 `40/50`、`65/75` 里 | 显式化为可插拔闸门，取 0.75 | 判定与是否介入应当分离（§6） |
| `state_transitions` 表 | 单独存 | 由 `evaluations` 派生 | 避免同一事实存两遍（§10） |
| 用户回执 | 未提按钮 | 显式按钮 + 行为推断双轨 | 改用弹窗后按钮成本消失，且能补上行为推断的歧义（§9.1） |
| LLM | V0 就用于 Pipe | V0 不用，预留 `Wording` 接口 | §3 非目标 |
| CLI 用法 | 安装 / 诊断 / 探索 | 同 | 一致 |
| 读数据走 REST API | 是 | 是 | 一致 |

---

## 18. V0 之后的接口预留

| 预留点 | 为谁准备 |
|---|---|
| `Wording` 接口 | V2 的 LLM 措辞与个性化 |
| `GateResult` 条件列表 | V1 的连续段闸门、净占比闸门 |
| `Notifier` 接口 | 换投递通道（Telegram / 通知中心） |
| `outcomes` 原始值 | V1 的「什么干预最有效」分析 |
| `evaluations.gate_trace` | 阈值调优 |
| `ActivityReader` 边界 | 换数据源 |

---

## 19. 目录结构

```
src/statesense/
├─ __main__.py            # --once / --daemon / --check
├─ config.py              # TOML 加载 + 启动即校验
├─ clock.py               # 时间抽象（可注入）
├─ activity/
│  ├─ models.py           # ActivitySnapshot / Entry
│  └─ reader.py           # 唯一接触 Screenpipe
├─ state/
│  ├─ models.py           # StateVerdict
│  ├─ taxonomy.py         # 分类匹配，纯函数
│  └─ engine.py           # 纯函数
├─ intervention/
│  ├─ decider.py          # 纯函数
│  ├─ gates.py            # 可插拔闸门
│  ├─ actions.py          # 动作池 + 轮转
│  └─ wording.py          # Wording 接口 + 模板实现
├─ notify/
│  ├─ base.py             # Notifier 协议 + DeliveryResult
│  ├─ win32_popup.py      # ctypes 封装（MessageBox / 抢前台 / 关窗）
│  └─ foreground_popup.py # 投递实现（含超时与回执映射）
├─ outcome/
│  └─ tracker.py
├─ store/
│  ├─ db.py
│  └─ schema.sql
└─ scheduler.py
tests/
└─ test_*.py            # 固定样本以模块内联 dict 的形式写在各测试文件里
                        # （早期计划里的 tests/fixtures/*.json 未采用：
                        #  样本只在受访的测试里读得懂，抽成 JSON 反而更难维护）
config/
└─ config.example.toml
```

---

## 20. 实施顺序建议

1. `config.py` + `clock.py` + `store`（骨架与 schema）
2. `activity/`（含防御式解析，用录制响应做测试）
3. `state/`（纯函数 + 表驱动测试）——**这一步就能产出可评审的判定结果**
4. `intervention/`（闸门 + 动作池 + 模板文案）
5. `notify/`（原生前台弹窗；投递通道已在实施中由 Toast 改为弹窗，见 §8.2）
6. `outcome/` + `scheduler.py`
7. `--once` 端到端手动验证，观察一天，再决定是否挂任务计划程序
