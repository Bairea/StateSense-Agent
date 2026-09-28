# 阶段 3 验收复跑记录（2026-09-28）

> `main@41a2bc3`（与 origin 同步，工作区干净）。目的：PR #9 合入后，把
> 实现记录（`2026-09-24-stage-3-shadow-implementation-log.md`）里声称的
> 待验收事项逐项实跑复核。本文只记实测事实，所有数字可按随附命令复现。

## 结论

五项全过：全量回归、配置与 `.env` 完整性、真实数据链路、影子回放数字复现、
真实 provider 冒烟。实测 glm-5.3-flash 延迟 3.1–6.8s，据此把本地
`[shadow] timeout_seconds` 从 5 提到 8（config.toml 不入库，本机生效）。
仍未解锁的只有 3.5（有限上线）：前置是 daemon 常驻攒 ≥3 天真实影子样本。

## 1. 全量回归

- `uv run pytest -q` → **570 passed**（189s）。
- `uv run python -m statesense --config config/config.example.toml --replay all`
  → 六剧本全 PASS（ladder / outcome / gates / degraded / sleep_gap / shadow）。
  输出里的 `safe-delete` 行是 pytest 临时目录清理告警（trash 在本机映射盘
  失败的无关噪音），不影响结果。

## 2. 配置与 `.env` 完整性（runbook 第 1/3 步）

- `config/config.toml`：`[shadow]` enabled/http（timeout 起初 5，见 §6）；
  `[wording]` template/3s。
- `.env` 三项变量名齐全（`STATESENSE_LLM_URL` / `STATESENSE_LLM_MODEL` /
  `STATESENSE_LLM_API_KEY`；核验只看名字，值不回显）；`.env` 被 `.gitignore`
  覆盖，`.env.example` 已入库。
- `--check` 逐行符合预期：`影子 http model=glm-5.3-flash`、
  `文案 template(内置模板)`。recorder 未跑时 `data_status unreachable`
  且 `--check` 退出码 1——属预期降级，不是缺陷。

## 3. 真实数据链路（runbook 第 2/4 步）

- 手动拉起 recorder（`echo n| screenpipe.exe record --port 3131
  --disable-audio`），约 40 秒内 3131 监听，`data_status ok`；全屏信号
  实测到「全屏应用运行中（判定为游戏）」。
- `--once --dry-run` 两轮 + `--report --views 9 --since 1d`：被问 2/2
  （覆盖 100%）、结局 ok=1 / timeout=1、分歧 一致 1、模型版本
  glm-5.3-flash=2。影子未影响判定与投递（闸门留痕照常）。
- 概览「覆盖率 200%」：手动 tick 间隔 1 分钟 vs 应约 5 分钟所致，非缺陷。

## 4. 影子回放复验

- `--replay shadow --keep-db` → `config/replay/shadow/{control,shadow,degraded}.db`；
  报告用 `--db` 指库。
- `--report --db config/replay/shadow/shadow.db --views 9 --since 60d`
  与实现记录**逐项吻合**：被问 19/19；结局 ok=16 / timeout=1 /
  invalid_output=1 / provider_error=1；拒答 1（5.3%）；分歧
  一致 1 / 更重 2 / 更轻 11 / 不下结论 2；首次可提醒 候选更早 1 个
  （平均提前 10.0 分钟）；`offline-scripted=19` 且报表自带免责说明。

## 5. 真实 provider 冒烟（`tmp/smoke_providers.py`，gitignored，可复跑）

- 影子 `(45, 0, 10, 60)` → `PASSIVE_CONSUMPTION`（4250ms），理由引用 75% 占比。
- 文案 `reminders_today=1` + `last_receipt=disengaged` → 53 字正文，
  与 09-24 实现记录**逐字一致**；无 markdown、无包裹引号、未超长。
  冒烟脚本的 timeout 给 8s，把「契约对不对」与「延迟抖动」分开验证。

## 6. 真弹窗路径与 timeout 处置（runbook 第 5 步）

- 真实 tick（`--once`，不带 `--dry-run`）干净跑通（exit 0）：当时活跃
  以工作为主（3.2 分钟，终端 / VS Code / DeepSeek），`state=NORMAL`，
  闸门未到档、弹窗未触发——「该弹才弹」是预期行为，真实投递通道待
  真实场景首弹。
- **延迟实测与处置**：当日三次真实影子调用 3125 / 5274 / 6750ms。
  其中 5274ms 与 6750ms 都超过旧上限 5s——5274ms 在旧配置下被结果层
  如实记成 timeout（双层超时设计首次实战生效）；6750ms 那次已在新配置
  （8s）下跑，如实记成 ok。影子不在投递路径上，放宽只让影子样本更完整，
  故本地 `[shadow] timeout_seconds = 8`（`[wording]` 保持 3s——文案
  调用在投递路径上，慢模型必须让位给回退模板）。

## 环境备注

- recorder 验证完即 taskkill 停止、3131 释放；本日共 3 轮真实 dry-run/实跑
  评估与 3 行真实影子记录进入生产库（`--once` 属 runbook 第 4/5 步的
  预期产物）。
- Screenpipe 自身两条 WARN（snapshot compaction os error 231 /
  frame_linker stale entries）不影响 `/activity-summary` 供数，暂不处理。

## 仍未解锁

- **3.5（有限上线）与阶段出口**：前置 = daemon 常驻攒 ≥3 天真实影子样本。
  runbook 第 6 步两条 `schtasks` 注册命令须用户手动执行；样本攒够后按
  runbook 第 8 步读 `--views 9` 走决策点（有稳定信号才写 3.4 判定规格）。
