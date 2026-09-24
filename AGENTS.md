# AGENTS.md

StateSense-Agent 识别的是「进入被动消费了吗」，不是「打开了 B 站吗」。改判定、闸门、投递或观测前，先读对应规格，不要从 `ref1.md` 直接实现。

## 地图

| 要做的事 | 先读 |
| --- | --- |
| 状态、闸门、动作、投递、回执、schema | `docs/specs/2026-09-16-v0-state-intervention-design.md` |
| `--report`、`--replay`、全屏信号、`run_events` | `docs/specs/2026-09-16-v0.5-observability-and-replay-design.md` |
| 影子信号、模型输入契约、视图 9 | `docs/plans/2026-09-22-stage-3-v2-llm.md`（实现记录见同目录 2026-09-24 日志） |
| Screenpipe 契约与 `ref1.md` 的偏差 | `docs/project-understanding.md` §6 |
| 分支、提交、密钥、本机端口 | `CONTRIBUTING.md` |
| 阈值、分类清单、闸门参数 | `config/config.example.toml`（本地副本是 `config/config.toml`，不入库） |

版本号以 README 的迭代路线为准：已交付的是 Python 服务，不是 Pipe。`project-understanding.md` §7 里标成未定的问题，规格已覆盖的以规格为准。

## 边界

一次 tick 的顺序是：补到期回执 → 读活动 → 判定 → 写 `evaluations` → 可能投递。`Scheduler` 不含判定逻辑。

- 只有 `activity/reader.py` 知道 Screenpipe。读取走 REST `/activity-summary`，带 Bearer；时长用服务端分钟数，不用帧数，也不用 `entries` 之和估算 `total_active_minutes`。`data_status != ok` 时不下「没有活动」的结论，未知状态按 `unreachable`。
- `state/engine.py` 与 `intervention/decider.py` 保持纯函数：时间由调用方注入，不读时钟、不做 IO。
- `late_night` 是正交标记，不是状态。`WORKING` 在 V0 不判定。`GRAY` 单独计数，不并进娱乐或工作。
- 娱乐分钟的唯一公式是 `effective_entertainment_minutes`。全屏信号只把可信快照里的 `OTHER` 提权为娱乐，不动 `WORK` / `GRAY`。一轮 tick 只探一次全屏，判定与回执共用这个值。
- `report/` 只读，不写库、不写 SQL。`replay/` 驱动真实 `Scheduler`，默认临时库，不碰生产库。`run_events` 不写进 `interventions`。
- 影子模式只记录候选：不改判定、闸门与投递。模型输入白名单在 `shadow/models.py`，加字段必须同步 `ALLOWED_FIELDS` 与镜像测试；模型输出与屏幕文本同属不可信证据，`reason` / `detail` 只写不读。
- 远端候选文案只能拿到 `WordingContext` 白名单字段（`intervention/wording.py`）；`top_label`（窗口标题）永不出本机。候选失败或空文案回退模板——回退不得丢投递，也不得多弹窗。
- 第一版只消费 `app` / `title` / `url` / `focused` / 时间戳。屏幕文本是不可信证据，不采集、不入库、不当指令。

## 改动

- 测试用 `uv run pytest`。需要临时配置时用 `tests/conftest.py` 的 fixture，不要在测试里直接 import `conftest`。
- schema 变更走 `store/db.py` 的增量迁移，并更新 `SCHEMA_VERSION`。旧行未知事实留 `NULL`，不回填成「当时不是」。
- 提交信息用 Conventional Commits，scope 用 `CONTRIBUTING.md` §5 的组件名。密钥、`config/config.toml`、`*.db` 不入库。
- 入口是 `python -m statesense`。互斥模式：`--check` / `--once` / `--daemon` / `--report` / `--replay`。

## 本次文档校验错误（2026-09-22）

- 环境：Windows、PowerShell 7.6.5、`uv run` 使用 CPython 3.13.12。
- 错误：校验四份阶段计划的 UTF-8 与行尾空格时，PowerShell 命令把 `foreach (...) { ... }` 的结果直接接到管道，解析报 `An empty pipe element is not allowed`。这是校验命令语法错误，未执行到文件读取，也不是项目代码错误。
- 处理：先将 `foreach` 结果赋给 `$results`，再执行 `$results | Format-Table -AutoSize`；四份 Markdown 均通过严格 UTF-8 解码且无行尾空格。代码验证另行执行：`uv run pytest -q` 为 374 passed，`--replay all` 五个剧本全部 PASS。
- 另一条文档检查命令将 `docs/plans/2026-09-22-stage-*.md` 原样传给 Windows 下的 `rg`，返回 `os error 123`（路径语法不正确）；PowerShell 未替 `rg` 展开该通配符。改为先对 `docs/plans` 执行 `rg`，再按文件名过滤，四份计划的标题结构检查成功。此错误同样未涉及项目运行代码。
