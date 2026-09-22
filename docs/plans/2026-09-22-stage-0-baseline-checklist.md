# 阶段 0 基线采集清单（2026-09-22）

> 依据 `docs/plans/2026-09-22-stage-0-v0.5-acceptance.md` 的任务 0.1。
> 本文件记两件事：**今天在这台机器上实测到什么**，以及**要拿到基线还差哪几步**。
> 按计划，产物是一份带日期的基线记录 —— 下面的表格就是它，空缺的格子等待填入。

## 一、2026-09-22 实测到的状态

在 `D:\Desktopfile\chores\StateSense-Agent`（`main@451b724`）上逐项核对：

| 核对项 | 实测结果 |
| --- | --- |
| `config/config.toml` | **不存在**（只有 `config.example.toml`） |
| 生产库 `statesense.db` | **不存在**：仓库内、`D:\Desktopfile`、用户目录（深度 4）全都没有 |
| `SCREENPIPE_LOCAL_API_KEY` | **未设置** |
| recorder 监听 3131 | **没有监听者**（`netstat` 无 3131 记录） |
| `statesense` / `screenpipe` 进程 | 无 |
| Screenpipe 数据目录 | `C:\Users\baizhicong\.screenpipe` 存在，211 MB，最后的 app 日志是 **2026-09-15 14:05** |
| `D:\Screenpipe`（文档里写的路径） | **不存在** |
| `screenpipe` CLI | 不在 PATH；常见安装目录（`AppData\Local`、`Programs`、`Program Files`、`D:\DevTools`）里也找不到 `screenpipe*.exe`；只有桌面版 `screenpipe-app.*.log` |
| 任务计划程序 | **未能核对**：本会话的沙箱把 `schtasks.exe` 列入黑名单，禁止启动。这一项必须由人在本机执行 |

结论：**这台机器当前既没有采集，也没有历史库** —— 阶段 0 的基线无从谈起。
它还缺三样东西：可用的 recorder（或确认桌面版在监听哪个端口）、一张 API token、一份 `config/config.toml`。

## 二、把环境点起来（按顺序，每步都有可核对的产出）

### 1. 确认 recorder 与端口

先判断该不该加 `--data-dir`：`C:\Users\<用户>\.screenpipe` 已经存在且有历史数据，
而 `CONTRIBUTING.md` 旧文里的 `D:\Screenpipe` 在这台机器上并不存在。
**照旧文执行会新建一个空目录**，现象很像「一直在跑、只是没数据」。不确定就不加这个参数。

```bash
screenpipe record --data-dir "<实际数据目录>" --disable-audio --retention-days 14 --port 3131
Get-NetTCPConnection -LocalPort 3131 -State Listen | Select-Object OwningProcess
```

产出：端口在听，且 `OwningProcess` 指向 Screenpipe 而不是别的软件（3030 被 Docker 占过，见 `CONTRIBUTING` §7）。

### 2. 取 token 并放进环境变量

```bash
screenpipe auth token          # 或从桌面版的设置里取
setx SCREENPIPE_LOCAL_API_KEY "<token>"
```

产出：**新开的**终端里 `echo %SCREENPIPE_LOCAL_API_KEY%` 有值。token 只进环境变量，绝不进仓库（§8）。

### 3. 建本地配置

```bash
cp config/config.example.toml config/config.toml
```

`config/config.toml` 已在 `.gitignore` 里。若 recorder 的端口不是 3131，改 `[screenpipe] base_url`。

### 4. 冒烟：一次真实取数

```bash
uv run python -m statesense --check --config config/config.toml
```

产出：`data_status=ok`（不是 ok 就说明 recorder 或 token 有问题，此时任何状态结论都不成立）；
`全屏信号` 一行有具体取值，而不是「无法判定」。

### 5. 单轮：确认建库与迁移

```bash
uv run python -m statesense --once --config config/config.toml --dry-run
```

产出：`config/statesense.db` 出现，`PRAGMA user_version = 7`，`evaluations` 多一行且
`rule_version` 是 12 位标识而不是 NULL。

### 6. 常驻与自启

在任务计划程序里注册「登录时触发 + 失败重启」，启动命令带上 `--config` 的**绝对路径**。
**先决定用不用真实弹窗**：`--dry-run` 走 `RecordingNotifier`，库里通道记为 `recording`，
不会打扰你，但也拿不到真实的按钮回执；不加 `--dry-run` 就会真的弹窗。

产出：任务计划程序里的条目名与触发器（连同这一步一起记到下面的基线表里）。

### 7. 采集基线（24 小时后）

```bash
uv run python -m statesense --report --config config/config.toml --since 24h --views all
```

产出：填满下面的表。

## 三、基线记录（待填）

> 采集时间：`__________`　分支：`__________`　库路径：`__________`

| 项 | 值 |
| --- | --- |
| 实际启动方式（任务计划程序条目 / 手动） | |
| 最近一次评估时刻 | |
| schema / 规则版本 | |
| `data_status` 分布 | |
| `skipped` 轮数 | |
| 缺口处数与成因（逐条） | |
| `run_events`（`sleep_gap` / `tick_error`） | |
| 干预计数：真实 `foreground_popup` / 排练 `recording` | |
| 视图 6 主分析口径（真实弹出 + 已投递 + 有可用回执） | |
| 视图 7 三层回执（gaming / not_gaming / unknown） | |

**这一步要能区分四件事**（计划的完成条件）：代码存在 ≠ 任务已注册 ≠ 进程正在运行 ≠
Screenpipe 确实返回可信数据。表里的每一行都要能指认它属于哪一件。

## 四、本文件的用法

采集完把本文件改名为 `docs/plans/<日期>-stage-0-baseline.md` 并提交 —— 与
`2026-09-16-v0-verification-log.md`、`2026-09-17-v0.5-verification-log.md` 同一体例：
逐项写证据，未通过的保留为未通过。
