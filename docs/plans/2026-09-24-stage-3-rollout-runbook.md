# 阶段 3 上线操作手册：影子 + 文案 → daemon 常驻

> 分支 `feat/v2-llm-shadow`（本地领先 main 12 提交，未推送）。
> 本手册是**这台机器**的操作序列；命令里的绝对路径来自本机实测
> （uv：`C:\Users\baizhicong\.local\bin\uv.exe`；Screenpipe CLI：
> `C:\Users\baizhicong\.bun\bin\screenpipe.exe`）。

## 第 0 步：推送与合并（不阻塞本地试跑）

守护进程跑的是**本地工作区**，合并可以并行做：

```
git push -u origin feat/v2-llm-shadow
gh pr create --base main --fill
gh pr merge --rebase --delete-branch
```

合并后注意（历史踩坑）：`gh pr merge --delete-branch` 会切回本地分支并可能出现
工作区假删除——先 `git status` 看一眼，有 ` D` 就 `git restore .`。

## 第 1 步：启用影子（本地 config/config.toml，不入库）

本地配置还是 09-22 的版本，**没有** `[shadow]` / `[wording]` 节（缺节安全 =
默认关）。在文件末尾追加：

```toml
[shadow]
# 真实远端影子：每轮把六个聚合统计量发给模型，候选只落库不改行为。
enabled = true
provider = "http"
timeout_seconds = 5

[wording]
# 文案先保持内置模板——3.3 的验收线是「样本证明有收益才常开 http」。
# 想试候选文案就把 template 改成 http：失败/空文案/超长自动回退模板。
provider = "template"
timeout_seconds = 3
```

要点：
- `timeout_seconds = 5`：真实网络调用比离线慢，给足余量。一个旋钮管两层
  （传输层掐断，结果层丢迟到答复）。
- 频率与成本：每 5 分钟一轮 → 每天 ~288 次影子调用（每次输入约 1 KB），
  投递时另有至多 1 次文案调用。失败只记 `shadow_error` 运行事件，
  **不影响判定与投递**。

## 第 2 步：起 Screenpipe（数据源，3131 当前没有监听）

```
C:\Users\baizhicong\.bun\bin\screenpipe.exe record --port 3131 --disable-audio
```

**首次运行会弹「connect your AI: found Claude Code — add screenpipe MCP +
supported skills? [Y/n]」**：按 `n` 回车（**默认是 Y**，直接回车就会接受）。
这是 screenpipe 往检测到的 AI 工具里装集成的推广提示，与 StateSense 无关——
daemon 直连 `http://localhost:3131` 的 REST API，不需要任何 MCP；且该工具的
postinstall 有过遥测与提示注入前科（2026-09-22 实测），这类「帮你连接 AI」
一律拒绝。

验证：`uv run python -m statesense --check --config config/config.toml` 的
`data_status` 变成 ok。若 recorder 在跑却仍 unreachable → token 可能失效，
重跑 `screenpipe auth token` 并 `setx SCREENPIPE_LOCAL_API_KEY`。

**计划任务的前瞻坑**：这个提示若在无交互终端的 ONLOGON 场景再次弹出，可能
卡住后台任务。注册 `StateSense screenpipe` 任务后注销重登一次，检查
`netstat -ano | findstr 3131` 有没有监听；卡住的话改用 `echo n| <record 命令>`
或预置其配置文件（届时再定）。

## 第 3 步：自检（连 .env 完整性一起验）

```
uv run python -m statesense --check --config config/config.toml
```

预期逐行：`影子 http model=glm-5.3-flash`、`文案 template(内置模板)`、
`data_status ok`。若报「远端配置错误」→ `.env` 缺项，按提示补。

## 第 4 步：单轮试跑（不弹窗），看影子落库

```
uv run python -m statesense --once --config config/config.toml --dry-run
uv run python -m statesense --report --config config/config.toml --views 9 --since 1d
```

预期：视图 9 的「被问轮次」≥ 1、结局分布有数（data_status=ok 的轮次才会问模型）。

## 第 5 步：真弹窗单轮（可选）

`--once`（去掉 `--dry-run`）：前台 MessageBox 弹窗，180 秒不理会自动关。
确认真实弹窗体验后再上常驻。

## 第 6 步：注册任务计划程序（沙箱禁用 schtasks，两条都必须你自己执行）

启动器已备好：`tmp\run-daemon.cmd`（gitignored；内容 = cd 到仓库根 +
绝对路径 uv + 日志重定向到 `daemon.out.log`——daemon 的日志走 stdout，
这是仓库注释里定下的名正言顺去处）。

```
schtasks /Create /F /TN "StateSense screenpipe" /SC ONLOGON /RL LIMITED /TR "C:\Users\baizhicong\.bun\bin\screenpipe.exe record --port 3131 --disable-audio"
schtasks /Create /F /TN "StateSense daemon" /SC ONLOGON /RL LIMITED /TR "D:\Desktopfile\chores\StateSense-Agent\tmp\run-daemon.cmd"
schtasks /Run /TN "StateSense screenpipe"
schtasks /Run /TN "StateSense daemon"
```

- 两个任务都是 ONLOGON + 当前用户；建议再在任务计划程序 GUI 里勾
  「失败后重启任务」（daemon 本身无自动重启）。
- 验证：`netstat -ano | findstr 3131` 有监听；`daemon.out.log` 每轮一行
  「常驻启动 … 影子=http model=glm-5.3-flash」。
- 停止：`schtasks /End /TN "StateSense daemon"`；删除：`schtasks /Delete /TN ...`。

## 第 7 步：日常观测

- 概览：`uv run python -m statesense --report --config config/config.toml`
- 影子分歧：`... --report --config config/config.toml --views 9 --since 7d`
  ——看覆盖度（被问/评估）、结局分布（timeout / provider_error 多不多）、
  分歧四档与首次可提醒时间；模型版本分组保证换模型前后不混算。
- `daemon.out.log` 里 `shadow_error` 行 = 模型调用失败（投递不受影响）；
  stderr 里出现 traceback 才是真异常。

## 第 8 步：几天后的决策点（3.5 的入口）

真实样本攒够（建议 ≥ 3 天）后按计划走：
1. 读视图 9：分歧集中在哪类窗口？覆盖度多少？延迟/失败率可接受吗？
2. 有稳定信号 → 写 3.4 判定规格（模型候选只能作用于哪些模糊窗口、
   如何与闸门组合）；没有 → 维持纯规则（计划原文：收益不稳定、成本超限
   或输入边界不清，一律停止扩量）。
3. 判定规格另行评审后才会让模型影响投递——在那之前影子永远只是记录。

## 回退

- 影子关：`config/config.toml` 里 `[shadow] enabled = false`（或删掉整节，
  缺节 = 默认关）。
- 文案回模板：`[wording] provider = "template"`。
- 任务删除：`schtasks /Delete /TN "StateSense daemon" /F` 等。
- 数据：`config/statesense.db` 是 sqlite，schema v8 向后兼容，旧行不受影响。
