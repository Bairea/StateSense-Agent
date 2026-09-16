# StateSense-Agent V0 验证日志

| 项 | 值 |
|---|---|
| 日期 | 2026-09-16 |
| 分支 | `feat/v0-implementation` |
| 结论 | V0 全链路端到端跑通 |
| 对应 spec | [`docs/specs/2026-09-16-v0-state-intervention-design.md`](../specs/2026-09-16-v0-state-intervention-design.md) |
| 对应计划 | [`docs/plans/2026-09-16-v0-implementation.md`](./2026-09-16-v0-implementation.md) |

---

## 1. 运行环境

| 组件 | 版本 / 值 |
|---|---|
| OS | Windows 10 家庭中文版 Build 19045（22H2） |
| Python | 3.12.10（uv 0.11.6 管理虚拟环境） |
| Screenpipe | CLI 0.4.50，recorder 运行中（`--disable-audio --retention-days 14`） |
| 数据目录 | `D:\Screenpipe`（经 junction 映射到 `~/.screenpipe`） |
| 运行期第三方依赖 | **0 个**（只用标准库；`winotify` 已在通道切换后移除） |
| 测试 | 137 passed |

---

## 2. `--check` 自检

```
配置        OK（store=...\config\statesense.db）
通道        foreground_popup
ratio_min   0.75
回看窗口    最近 60 分钟
data_status ok
取到 30 条窗口记录，总活跃 56.1 分钟
    16.8 分钟  ...无法访问此文件 - Google Chrome
    11.3 分钟  ...spec02 补充材料 — DSH 本地构建
     7.2 分钟  ...三角洲烽火联赛#9 - Google Chrome
     6.3 分钟  阅读架构与检测当前订阅旗舰 — DSH 本地构建
     5.8 分钟  拉取https://github.com/Bairea/StateSen — DSH 本地构建
```

`data_status = ok` 说明 recorder 在采、token 有效、字段解析正常。

---

## 3. 真实评估记录（生产配置，`--once --dry-run`）

```
state=WATCH  intervened=False
note=state_min 未通过（0.0 vs 阈值 1.0）；ratio_min 未通过（0.47 vs 阈值 0.75）
```

`evaluations` 表里同一条记录的 `gate_trace`：

```json
[1] 2026-09-16T19:19:09  WATCH  ent=26.4  ratio=0.47  late_night=0 → skip
      data_status=ok  prev=None
      BLOCK state_min  value=0.0   threshold=1.0
      BLOCK ratio_min  value=0.47  threshold=0.75
      pass  cooldown   value=inf   threshold=30
      pass  daily_cap  value=0.0   threshold=8.0
```

**这是设计里最想要的效果**：事后能一眼看出「为什么这次没打扰我」——被 `state_min` 和 `ratio_min` 两条挡下，且两条的实际值与阈值都在。

---

## 4. 全链路干预验证（临时低阈值配置强制触发）

为避免等待真实的长时段被动消费，用一份临时配置把阈值压低（`watch=3 / passive=5 / ratio_min=0.2 / answer_timeout=45s`），强制走一次真实投递。

进程输出：

```
state=PASSIVE_CONSUMPTION  intervened=True  outcomes_closed=0
note=delivered response=accepted
```

`force.db` 里的落库结果：

```
=== evaluations ===  1 行
[1] 2026-09-16T19:20:52  PASSIVE_CONSUMPTION  ent=26.4  ratio=0.45  → intervene
      pass state_min  value=1.0  threshold=1.0
      pass ratio_min  value=0.45 threshold=0.2
      pass cooldown   value=inf  threshold=30
      pass daily_cap  value=0.0  threshold=8.0

=== interventions ===  1 行
[1] eval=1  state=PASSIVE_CONSUMPTION  action=walk5
    delivery=delivered  user_response=accepted
    文案：最近 60 分钟里有 26 分钟在被娱乐内容占用
         （三角洲烽火联赛夏季赛 - Google Chrome），占 45%。离开电脑走 5 分钟？

=== outcomes ===  0 行（该记录在本次运行中尚未到 10 分钟复查时刻）
```

链路 `判定 → 闸门 → 文案 → 弹窗 → 按钮回执 → 落库` 全部走通。

### 4.1 一个意外的额外收获

这条记录里分类**是靠 `browser_url` 命中的**：窗口标题「三角洲烽火联赛夏季赛」不含任何娱乐关键词，是 URL 中的 `bilibili` 命中了规则。

这修正了 spec §15.1 的一条推论。原先基于 115 帧样本得出的结论是「`browser_url` 很稀疏，`window_name` 才是主力」。实测补充：**`browser_url` 稀疏但确实会在浏览器前台窗口上出现，且在标题无关键词时不可替代**。因此分类必须继续对 `app` + `title` + `url` 三者做 OR 匹配，三者缺一不可。

---

## 5. 投递通道验证（这是本次改动最大的一处）

### 5.1 原方案（Windows Toast）在本机全链路失效

| 实测 | 结果 |
|---|---|
| `HKCU\...\PushNotifications\ToastEnabled` | **0**（全局通知开关关闭） |
| 三种候选 AUMID（自定义 / `PowerShell` / PowerShell AUMID 全路径）各发 6 条 | **一条都不可见** |
| `winotify` 的 `show()` 返回值 | 三次全部返回成功 —— **静默失败** |

`winotify` 用 `Popen(..., stdout=DEVNULL, stderr=DEVNULL)`，失败信息被彻底丢弃，所以「API 说成功、屏幕上什么都没有」是必然结果。

### 5.2 改用原生 MessageBox 后的实测矩阵

| 场景 | 仅 `MB_TOPMOST \| MB_SETFOREGROUND` | 加显式抢前台 |
|---|---|---|
| 窗口模式 | 可见 | 可见 |
| **全屏模式** | **不可见**（只听到提示音） | **可见** |
| 抢前台返回值 | — | `attached=True setforeground=True` |

「显式抢前台」= `FindWindowW` → `ShowWindow(SW_SHOW)` → `SetWindowPos(HWND_TOPMOST)` → `AttachThreadInput` → `BringWindowToTop` → `SetForegroundWindow` → `FlashWindow`。

**为什么这一段不可省**：Windows 有「前台锁定」，后台进程不允许抢前台窗口；不挂上当前前台线程，窗口会被创建却压在全屏应用后面。这正是原 spec §16 风险 1 的根因，现在被彻底解决。

### 5.3 超时自动关闭

| 项 | 实测 |
|---|---|
| 用户不理会，`answer_timeout_seconds=10` | 10.4s 后窗口消失，**窗口句柄 = 0** |
| 此时 `user_response` | `None` |

窗口句柄为 0 是关键证据：关闭是程序化生效的，不是靠进程退出把窗口带走。

---

## 6. 实施中发现并修正的缺陷

| # | 缺陷 | 发现方式 | 修正 |
|---|---|---|---|
| 1 | plan 的 `check()` 把 `SystemClock().now()` 同时当作 start 与 end，会读一个零长度窗口 | 计划评审 | 改为读 `[now - window, now]` |
| 2 | plan 中 `data_status` 实现为 `unreachable: <原因>`，但测试断言 `== "unreachable"` | 测试失败 | 以 spec §5.1 为准保持封闭枚举，原因改走日志 |
| 3 | 超时被程序关闭时，对话框返回的按钮 id 被误当成用户点击（记成了 `declined`） | 测试失败 | `timed_out` 时强制 `user_response=None` |
| 4 | `winotify` 依赖的 `WM_COMMAND` 常量未定义，超时关闭路径直接 `NameError` 崩溃 | 真机端到端 | 补常量；关闭改为四种消息依次尝试并逐个验证 |
| 5 | 测试夹具把 Windows 路径直接拼进 TOML 基本字符串，反斜杠触发非法转义 | 测试失败 | 改用 `as_posix()` |
| 6 | 三处调度器测试的 fake 响应队列长度不足 —— 回执查询会先消耗队列 | 测试失败 | 补足队列，并在注释里写明消耗顺序 |

缺陷 3、4 都是**只有真机端到端才会暴露**的问题：单元测试里 fake 的行为掩盖了它们。这也是为什么 spec 把「全屏 Toast 可达性」列为必须实测项。

---

## 7. 尚未验证的部分（诚实清单）

| 项 | 状态 | 原因 |
|---|---|---|
| 真实长时段被动消费触发干预 | **未验证** | 需要真实刷 40+ 分钟，非本次可造 |
| T+10min 行为回执（`outcomes` 表实际落行） | **未验证** | 同上；纯函数逻辑已有表驱动测试覆盖 |
| 全屏状态下由**调度器**触发（而非手工脚本）的弹窗 | **已间接验证** | 手工脚本与调度器走的是同一个 `ForegroundPopupNotifier` |
| 常驻模式（`--daemon`）与任务计划程序注册 | **未验证** | 属部署动作，见 spec §14 |
| `GRAY` 档（知乎等）的真实分布 | **未验证** | 需要多天数据 |

---

## 8. 已知的非功能性瑕疵

| 项 | 说明 |
|---|---|
| `uv run python -m statesense` 在 stderr 打印若干 `libpng warning: iCCP: known incorrect sRGB profile` | **与项目代码无关**：单独 `import statesense.*` 与单独的 `uv run python -c` 都不产生该警告。不影响功能与退出码判断 |
| `--once` 会阻塞至多 `answer_timeout_seconds`（默认 180s） | 设计如此（模态弹窗）；期间不评估、不查回执。见 spec §16 风险 10 |

---

## 9. 下一步建议

1. **挂上常驻**：确认一天的 `--once --dry-run` 记录符合直觉后，再注册任务计划程序（命令见计划 Task 11 的说明）。
2. **观察 3~5 天**，重点看 `evaluations.gate_trace` 里 `ratio_min` 的阻挡频率，据此判断 0.75 是否过严。
3. **`GRAY` 档定夺**：用真实分布决定知乎这类站点归入娱乐还是保持中立。
4. 再决定是否值得投入 V0.5 / V1。
