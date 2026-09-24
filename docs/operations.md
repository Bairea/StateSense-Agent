# StateSense 运维手册：影子与文案的上线执行序列

> 这是**独立的快速执行文档**：从零把「影子模型信号 + 提醒文案」跑起来的
> 完整顺序。背景与设计细节见
> [`docs/plans/2026-09-24-stage-3-rollout-runbook.md`](./plans/2026-09-24-stage-3-rollout-runbook.md)
> 与 [`docs/plans/2026-09-24-stage-3-shadow-implementation-log.md`](./plans/2026-09-24-stage-3-shadow-implementation-log.md)。
>
> **当前状态（2026-09-24）**：开发机不常驻运行——未注册任何计划任务，
> 影子默认关闭。本文是可执行的完整序列，在哪台机器跑都适用；
> 命令里的绝对路径以这台开发机为示例。

## 前置条件

| 项 | 状态 / 获取方式 |
| --- | --- |
| 代码 | `main` 已含全部阶段 3 实现（PR #9 已合并，schema v8） |
| `.env` | 从 `.env.example` 复制到项目根，填入真实 API key（`.env` 不入库） |
| Screenpipe CLI | `%USERPROFILE%\.bun\bin\screenpipe.exe`（0.4.50） |
| uv | `C:\Users\<你>\.local\bin\uv.exe` |

环境变量三项（写在 `.env` 里即可）：`STATESENSE_LLM_URL` /
`STATESENSE_LLM_MODEL` / `STATESENSE_LLM_API_KEY`。查找顺序：CWD/.env →
配置文件同目录/.env；已存在的环境变量优先。

## 第 1 步：启用影子（编辑本地 config/config.toml，末尾追加）

```toml
[shadow]
enabled = true
provider = "http"
timeout_seconds = 5

[wording]
provider = "template"
timeout_seconds = 3
```

- 缺节安全：不写这两节 = 影子关、模板文案，行为与从前完全一样。
- 文案先保持 `template`——3.3 验收线「样本证明有收益才常开 http」；
  想试候选就改成 `http`，失败/空文案/超长（300 字）自动回退模板。
- 频率与成本：每 5 分钟一轮 → 每天约 288 次影子调用（输入约 1 KB/次）。
  失败只记 `shadow_error` 运行事件，不影响判定与投递。

## 第 2 步：起 Screenpipe（数据源）

```
C:\Users\<你>\.bun\bin\screenpipe.exe record --port 3131 --disable-audio
```

- **首跑会弹「connect your AI … [Y/n]」推广提示：按 `n` 回车**
  （默认是 Y，直接回车就会接受）。StateSense 直连 REST，不需要任何 MCP；
  这类「帮你连接 AI」一律拒绝。
- 验证：下一步的 `--check` 里 `data_status` 变 ok。

## 第 3 步：自检

```
uv run python -m statesense --check --config config/config.toml
```

预期：`影子 http model=<模型名>`、`文案 template(内置模板)`、
`data_status ok`。报「远端配置错误」→ `.env` 缺项；recorder 在跑却
unreachable → token 失效（重跑 `screenpipe auth token` 并 setx）。

## 第 4 步：单轮试跑（不弹窗）+ 看影子落库

```
uv run python -m statesense --once --config config/config.toml --dry-run
uv run python -m statesense --report --config config/config.toml --views 9 --since 1d
```

预期视图 9：被问轮次 ≥ 1、结局分布有数（`data_status=ok` 的轮次才会问模型）。

## 第 5 步：真弹窗单轮（可选）

`--once`（去掉 `--dry-run`）：前台 MessageBox 弹窗，180 秒不理会自动关。

## 第 6 步：注册计划任务（常驻）

daemon 启动器（`tmp/run-daemon.cmd`，gitignored；工作目录必须是仓库根）：

```bat
@echo off
cd /d <项目根>
C:\Users\<你>\.local\bin\uv.exe run python -m statesense --config config/config.toml --daemon >> daemon.out.log 2>&1
```

```
schtasks /Create /F /TN "StateSense screenpipe" /SC ONLOGON /RL LIMITED /TR "<screenpipe.exe 的绝对路径> record --port 3131 --disable-audio"
schtasks /Create /F /TN "StateSense daemon" /SC ONLOGON /RL LIMITED /TR "<启动器 .cmd 的绝对路径>"
schtasks /Run /TN "StateSense screenpipe"
schtasks /Run /TN "StateSense daemon"
```

- daemon 日志走 stdout → 重定向到 `daemon.out.log`；stderr 出现 traceback
  才是真异常。
- 验证：`netstat -ano | findstr 3131` 有监听；日志每轮一行「常驻启动 …
  影子=http model=<模型名>」。
- **注册后注销重登一次**：screenpipe 首跑的 `[Y/n]` 推广提示若在无终端
  场景再次弹出可能卡住任务；卡住改用 `echo n| <命令>` 或预置其配置。
- 停止 / 删除：`schtasks /End` / `schtasks /Delete /TN ...`。

## 第 7 步：日常观测

```
uv run python -m statesense --report --config config/config.toml
uv run python -m statesense --report --config config/config.toml --views 9 --since 7d
```

视图 9 看四件事：覆盖度（被问/评估）、结局分布（timeout / provider_error
多不多）、分歧四档与首次可提醒时间、模型版本分组（换模型前后不混算）。

## 第 8 步：数日后的决策点（3.5 入口）

样本攒够（建议 ≥ 3 天）后：分歧有稳定信号 → 写 3.4 判定规格（模型候选只
作用于哪些模糊窗口）；没有 → 维持纯规则。**在判定规格评审通过之前，
影子永远只记录、不影响投递。**

## 回退

- 影子关：`[shadow] enabled = false`（或删整节，缺节 = 默认关）。
- 文案回模板：`[wording] provider = "template"`。
- 常驻停：`schtasks /End` + `schtasks /Delete`；数据是 sqlite，向后兼容。
