# StateSense-Agent

> 一个持续感知你的电脑活动、推断你处于什么状态、并在正确时机用极低成本动作把你「轻推」出来的个人状态智能体。

## 要解决的问题

在电脑上本来只想娱乐一会儿，结果刷 B 站刷了两小时，正事一件没干。

关键洞察是：**当你身处某个状态里时，你往往已经失去了主动性。** 指望「靠自己想起来」是不成立的——你需要一个外部的、持续运行的感知与介入系统。

## 核心循环

```
感知 → 状态推断 → 判断是否介入 → 给出极低成本动作 → 观察结果 → 更新对你的认识
```

系统要识别的不是「我此刻是不是打开了 B 站」，而是：

> 我进入「被动消费状态」了吗？

例如：

| 最近窗口内 B 站时长 | 状态 |
| --- | --- |
| 5 min | `NORMAL` |
| 20 min | 观察 |
| 40 / 最近 50 min | `PASSIVE_CONSUMPTION` |
| 65 / 最近 75 min | `HIGH_RISK_PASSIVE_CONSUMPTION` |

## 架构主张

**不自己造屏幕采集器。** 用 [Screenpipe](https://github.com/screenpipe/screenpipe) 作感知基础设施，本项目只负责「状态」这一层。

```
Screenpipe Recorder   ← 记录世界发生了什么（app / window / browser_url / focused / 时间）
        ↓  localhost API
Activity Reader       ← 每 1~3 min 查询最近 30~60 min 活动
        ↓
State Engine          ← NORMAL / WORKING / PASSIVE_CONSUMPTION / LATE_NIGHT
        ↓
Rule Engine           ← 是否介入、介入成本、cooldown
        ↓
Notification          ← 轻推一下
```

职责边界：

| 组件 | 负责回答 |
| --- | --- |
| Screenpipe | 世界发生了什么 |
| StateSense-Agent | 这些活动意味着我处于什么状态，以及现在应该做什么 |

## 当前状态

**V0 已实现并通过端到端验证**（详见验证日志）。

| 文档 | 内容 |
| --- | --- |
| [`prd1.md`](./prd1.md) | 项目背景与核心循环（问题陈述） |
| [`ref1.md`](./ref1.md) | 技术方案讨论：为什么用 Screenpipe、v1 架构、迭代路线、坑位 |
| [`docs/project-understanding.md`](./docs/project-understanding.md) | 项目理解整理 + 对上游 Screenpipe 文档的核对结果与偏差修正 |
| [`docs/specs/2026-09-16-v0-state-intervention-design.md`](./docs/specs/2026-09-16-v0-state-intervention-design.md) | **V0 技术规格**：架构、状态模型、介入闸门、投递与回执、数据模型、测试与部署 |
| [`docs/plans/2026-09-16-v0-implementation.md`](./docs/plans/2026-09-16-v0-implementation.md) | V0 实现计划（11 任务 / 56 步 / TDD） |
| [`docs/plans/2026-09-16-v0-verification-log.md`](./docs/plans/2026-09-16-v0-verification-log.md) | **V0 验证日志**：端到端实测结果、投递通道实测矩阵、实施中发现的缺陷 |
| [`CONTRIBUTING.md`](./CONTRIBUTING.md) | 协作仓库布局、分支策略与 PR 流程 |

## 迭代路线

| 版本 | 内容 | 要验证的问题 |
| --- | --- | --- |
| **V0** | Screenpipe + 一个 `pipe.md`，每 10 min 检查最近 1 小时，超阈值就通知 | 这种提醒到底有没有价值？ |
| **V0.5** | Python + Screenpipe API + Rule Engine | 什么时候提醒最好？ |
| **V1** | + feedback + state transitions + SQLite | 什么干预最有效？ |
| **V2** | + LLM | 模糊状态与个性化策略 |

> V0 的目标是**先真的让自己用起来**。如果连续几天后发现「它真的有几次在我要继续刷的时候把我拽出来了」，再投入做完整的 State Agent。

## 参与开发

见 [`CONTRIBUTING.md`](./CONTRIBUTING.md)。
