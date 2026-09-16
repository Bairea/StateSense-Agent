# 项目理解与上游文档核对

> 本文档整理自 `prd1.md`、`ref1.md`，以及对本项目技术底座 Screenpipe 上游文档（`screenpipe/screenpipe` main 分支的两份 `SKILL.md`）的逐条核对。
> 核对时间：2026-09。上游文档会变化，**引用前请重新核对**。

---

## 1. 项目定位

**一句话：** 一个持续感知电脑活动、推断用户所处状态、并在正确时机以极低成本动作进行干预的个人状态智能体。

**核心问题不是「检测到打开了 B 站」，而是「识别出用户已进入被动消费状态」。**

原因（`prd1.md`）：

> 你在某个状态里时，往往已经失去了主动性，被困在娱乐状态了。

这决定了设计取向：

- 需要**历史与聚合**，而不是瞬时窗口检测。
- 需要**状态机 + 规则**，而不是单点阈值触发器。
- 拦截时机比拦截精度更重要——在「即将继续刷」的瞬间介入，成本最低。

**核心循环：**

```
感知 → 状态推断 → 判断是否介入 → 给出极低成本动作 → 观察结果 → 更新对你的认识
```

注意最后一步「更新对你的认识」——这意味着**反馈闭环与个性化**是本项目的长期价值所在，而不是采集能力。

---

## 2. 架构主张

**不自建屏幕采集器。** 用 Screenpipe 承担「世界发生了什么」，本项目只承担「这些活动意味着什么」。

```
Screenpipe Recorder  →  localhost API  →  Activity Reader  →  State Engine  →  Rule Engine  →  Notification
```

**职责边界（ref1 的核心论证）：**

| 组件 | 回答的问题 | 是否需要自研 |
| --- | --- | --- |
| Screenpipe | 世界发生了什么 | 否，直接复用 |
| StateSense-Agent | 这意味着我处于什么状态、现在该做什么 | 是，这是项目价值 |

**状态模型（v1）：** `NORMAL` / `WORKING` / `PASSIVE_CONSUMPTION` / `LATE_NIGHT`

**自研数据库只需要存三类东西**（原始活动 Screenpipe 已经存了）：

```sql
interventions     -- timestamp, state, reason, suggestion, response, success
user_feedback
state_transition
```

---

## 3. 关键技术选择与理由

### 3.1 读数据用本地 REST API，不用 CLI

上游 API 文档明确：

> **Prefer this over the CLI for reads.** A `curl` against the local API returns in ~0.02s; a `screenpipe` CLI call costs ~0.15s at best and ~4s when it has to resolve `screenpipe@latest` from npm.

**结论：** CLI 只用于三件事——安装常驻（`doctor` / `service install` / `status`）、开发期人工探索数据、启动时健康检查。**Python Agent 的高频读取一律走 REST API。**

### 3.2 用 `/activity-summary` 做屏幕时间，绝不用帧数

上游文档：

> **Never use frame counts for time estimates** — frames are event-driven; use `/activity-summary` for screen time.

这正对本项目需求：我们关心的是「最近 60 min 实际刷视频多少分钟」，而不是「最近有多少帧来自 B 站」。

`/activity-summary` 是广义行为分析的默认入口，返回 `total_active_minutes` 与 per-app / per-window 的 `minutes` 聚合。

### 3.3 采样频率不需要高

ref1 的判断：行为干预不是毫秒级实时系统。

```
每 1 min  ：查询最近 5~10 min 活动（轻量）
每 5 min  ：重新评估最近 30~60 min 状态
```

### 3.4 第一版不用 LLM、也不用 Screenpipe Pipes 承载正式实现

- **V0 用 Pipe** 快速验证「提醒有没有价值」——几十行 prompt 就够。
- **正式 MVP 用 Python service**——因为核心价值是状态模型、规则系统、干预 cooldown、用户反馈、状态转移效果、个性化策略，这些用代码表达清楚得多。
- **LLM 留到 V2**，用于处理模糊状态与个性化策略。

---

## 4. 迭代路线

| 版本 | 内容 | 要验证的问题 |
| --- | --- | --- |
| **V0** | Screenpipe + 一个 `pipe.md`，每 10 min 检查最近 1 小时，超阈值就通知 | 这种提醒有没有价值？ |
| **V0.5** | Python + Screenpipe API + Rule Engine | 什么时候提醒最好？ |
| **V1** | + feedback + state transitions + SQLite | 什么干预最有效？ |
| **V2** | + LLM | 模糊状态与个性化策略 |

**V0 的成功判据（ref1 原话）：** 连续用几天后，如果发现「它真的有几次在我要继续刷的时候把我拽出来了」，才值得投入做完整的 Personal State Agent。

---

## 5. 必须提前注意的坑（ref1 已列出，此处保留结论）

1. **数据最小化。** Screenpipe 能力远超 MVP 所需。第一版只消费 `app` / `window` / `browser_url` / `focused` / `timestamps`；**不要把 OCR 文本、聊天内容、密码页面、邮件正文送进 Agent**。
2. **Prompt injection。** 屏幕文本是不可信证据，绝不能当指令执行。将来接 LLM 后尤其致命。
3. **不要用 frame count 当时间。** 见 3.2。
4. **不要高频轮询。** 见 3.3。
5. **不要用 Pipes 承载正式 Agent。** 见 3.4。

---

## 6. 上游文档核对结果 ⚠️

> 对 `ref1.md` 中引用的 Screenpipe 行为做了逐条核对。**以下偏差在动手实现前必须修正。**

### 6.1 文档支持的说法 ✅

| ref1 的说法 | 核对结论 |
| --- | --- |
| 本地 REST API 默认端口 3030 | ✅ 支持（但见 6.3 第 2 条） |
| 存在 `/activity-summary` 并返回 per-app/window 聚合分钟 | ✅ 支持 |
| 文档推荐 activity-summary 优于数帧 | ✅ 支持 |
| `/search` 支持 `start_time` / `end_time` / `app_name` / `window_name` / `focused` | ✅ 支持 |
| CLI 有 `doctor` / `service install` / `status` / `search --app --focused --start --json` / `status --json` | ✅ 支持（但调用形式需修正，见 6.3 第 5 条） |
| 不要用帧数估算时间 | ✅ 支持 |
| 捕获内容须视为不可信证据 | ✅ 支持，且范围比 ref1 写的更广 |
| API ~0.02s vs CLI ~0.15s | ✅ 支持 |
| Pipes 是 markdown + schedule frontmatter | ✅ 支持 |

### 6.2 文档**不支持**的说法 ❌

| ref1 位置 | 问题 |
| --- | --- |
| L79–88：把 `browser_url` 列为 `/search` 的过滤器 | ❌ `/search` 参数表中**没有** `browser_url`。它只出现在 `/raw_sql` 暴露的 `frames` / `ui_events` 表结构里，以及 CLI 的 `--browser-url`。**REST `/search` 是否实际接受该参数需实机验证后才能写进设计。** |
| L28：「API 文档明确列出了 `frames` 的 `app_name` / `window_name` / `browser_url` / `focused`」 | ⚠️ 表述不准——那是 `/raw_sql` 的**表列**，不是 `/search` 的过滤参数。 |
| L50–66：`/activity-summary` 的 JSON 示例 | ⚠️ 文档**未固化**该 schema，示例是示意。只有 `total_active_minutes`、嵌套 `minutes`、以及 `data_status` / `query_status` / `guidance` 三元组被点名。**实现必须做防御式解析。** |
| L92–98 / L202–206：CLI 片段写作 `screenpipe search …` | ⚠️ 上游要求写成 `cd "$(mktemp -d)" && ${SCREENPIPE_CLI:-bun x screenpipe@latest} <cmd>`，且传路径必须绝对。裸写不符合文档口径。 |

### 6.3 相对 ref1 的**新增必改项** 🔴

1. **【必改】补鉴权。** ref1 完全没提。上游 API 文档：每个请求必须带
   `Authorization: Bearer $SCREENPIPE_LOCAL_API_KEY`（无则 403），另需归因头 `X-Screenpipe-Client: api` 与 `X-Screenpipe-Agent: <name>`。
   Token 获取：`bun x screenpipe@latest auth token`。
   免鉴权端点仅 `/health`、`/ws/health`、`/audio/device/status`、`/connections/oauth/callback`、`/frames/*`、`/notify`、`/pipes/store/*`。
   **架构图与伪码都要补上这一步，否则所有读取都是 403。**

2. **【必改】base URL 不要硬编码。** 用 `${SCREENPIPE_LOCAL_API_URL:-http://localhost:3030}`。上游明确警告存在 fallback port / 开发态实例，"硬编码 3030 会打到另一个正在运行的 Screenpipe 实例"。

3. **【必改】通知层的真实端点是 `POST http://localhost:11435/notify`**（Tauri sidecar，**不是 3030**）。免鉴权，支持 markdown / priority / actions（`link` / `deeplink` / `pipe` / `chat` / `api`）。ref1 只写了「Windows / Screenpipe notification」。

4. **【必改】`/search` 的 `start_time` 是必填**，`limit` 默认 20 且**必须在 1–20** 之间（分页用 `offset`）。`content_type=all` **不包含** `memory` 与 `parsed`。
   另外 `today` / `yesterday` 是**本地日历语义**，不能用 `date -u` 算午夜。

5. **【必改】读路径要参数化裁剪。** `/search`、`/elements` 必须带 `fields=`（只取 `type` / `content.app_name` / `content.window_name` / `content.browser_url` / `content.timestamp` 等），否则会把每条 5–20KB 的 `text` / OCR 一起拉回本地——直接违背「第一版不消费 OCR」的设计意图。单 `content_type` 时加 `format=csv` 可以省 token。

6. **【建议】V0 的 `pipe.md` frontmatter 需要 `preset` 字段**（ref1 的示例缺了）。在 in-app chat 场景不要用裸 `pipe run`，改用认证 REST `POST /pipes/<name>/run` + `GET /pipes/<name>/logs`；且 `{"success":true}` **只代表已启动**，不代表执行成功。

7. **【建议】若走 `/raw_sql` 做 URL 分类**：`/raw_sql` 的时间戳是 RFC3339 字符串，不能直接与 `datetime('now')` 比较，要用
   `strftime('%Y-%m-%dT%H:%M:%f+00:00','now','-N hours')`；每条 `SELECT` 必须带 `LIMIT`。
   另外禁止直连 `db.sqlite` / `-wal` / `-shm`。

8. **【建议】更省的轮询方式**：只要 `total_active_minutes` 时，可传
   `include_key_texts=false&include_apps=false&include_windows=false`。

9. **【提示】`data_status` 必须被检查。** 取值 `ok` / `empty_but_recording` / `no_capture_in_range` / `not_recording`。
   当它不是 `ok` 时，**不能把「没有活动」当作结论**——那可能只是 recorder 没在采。这是很容易写出 bug 的地方。

10. **【提示】上游契约优先级：有 MCP 工具时优先 MCP，不要把它翻译成 curl。** 存储已演进为 SQLite + Parquet hybrid 模式，读取统一走 typed endpoints。

---

## 7. 尚未确认的问题（动手前需定）

| # | 问题 | 影响 |
| --- | --- | --- |
| 1 | REST `/search` 是否实际接受 `browser_url`？ | 决定 URL 级分类（`bilibili.com/video` vs `github.com`）能否直接用 `/search`，还是必须绕 `/raw_sql` 或 CLI |
| 2 | Screenpipe 在 Windows 上的 recorder 稳定性与资源占用如何？ | 决定它是常驻服务还是按需启动 |
| 3 | 干预动作的具体清单与「极低成本」的判定标准？ | Rule Engine 的输出契约 |
| 4 | `LATE_NIGHT` 的判定依据（系统时间 + 活动强度？） | 状态机定义 |
| 5 | cooldown 策略：固定间隔，还是按用户反馈动态调整？ | V1 反馈闭环设计 |
| 6 | 「成功介入」如何度量？用户点了「我起来了」算不算可信？ | 长期个性化策略的数据基础 |
| 7 | 与 Bairea 的分工与代码边界？ | 协作方式 |

---

## 8. 一句话总结

> **Screenpipe 非常适合作为这个项目 MVP 的底层感知层；它的 CLI 适合安装、诊断、探索和测试，但不适合作为 Python Agent 的高频数据接口。**

正式结构：

```
Screenpipe CLI      → 管理 Screenpipe
Screenpipe REST API → 读取活动
Python Agent        → 状态推断 + 干预
```

这样项目才能真正聚焦到想研究的问题——**人的状态与状态转移**——而不是把时间花在「怎么拿 Windows 前台窗口、怎么 OCR、怎么记录浏览器」这些已经被解决的问题上。
