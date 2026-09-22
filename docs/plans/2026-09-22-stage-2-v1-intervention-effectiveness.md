# 阶段 2：V1 干预时机与动作效果

> 在阶段 1 的分类和阈值基线稳定后执行。先修正效果数据的口径，再比较动作与时机。
> 本阶段不重新实现弹窗、按钮回执或 T+10 分钟行为回执。

## 代码现状

| 已实现 | 具体位置 | 当前分析能力与边界 |
| --- | --- | --- |
| 动作轮转 | `intervention/actions.py::pick`；游标保存在 `store.kv` | 能避免永远选择同一个动作，但轮转顺序不是随机分配，不能天然给出因果对照 |
| 双轨回执 | `notify/foreground_popup.py` 保存 accepted/declined；`outcome/tracker.py` 生成 disengaged/partial/continued/no_data | 已有原始 `ent_before/ent_after`，但 disengaged 可能只是离开电脑 |
| 观测视图 | `report/queries.py::build_intervention_breakdown` 按天、动作、状态、通道、投递结果计数；`build_outcome_breakdown` 按按钮回执分层，给前后娱乐分钟均值/中位数 | 目前没有“动作 × 时机 × 回执”的联合表，也没有对应的有效分母 |
| 持久化 | `interventions` 记 `evaluation_id/action_id/channel/delivery_status/user_response`，`outcomes` 通过干预 ID 关联 | 字段足够做第一轮关联分析；历史 `channel=NULL` 的行须保持未知 |

## 在分析前必须处理的三个具体数据问题

1. **排练与真实数据混算。** `build_outcome_breakdown(outcomes, interventions)` 只按用户回应分层，没有按 `channel` 和 `delivery_status` 过滤；`recording` 的排练回执可能被读成真实效果。现有视图 3 只是分别展示通道计数，不会自动清洗视图 4。
2. **回看区间边界不对齐。** `Store.list_interventions(since)` 按干预时间过滤，`Store.list_outcomes(since)` 按检查时间过滤。临近区间起点的 outcome 可能进来，但对应 intervention 被排除，当前报告会出现 `orphan` 层。要按干预发生时间建立同一批受试干预，不能用两个不同时间轴拼分母。
3. **历史全屏取值不精确。** `Scheduler._close_due_outcomes` 将“到期这一轮”的全屏状态同时用于干预前、后两个历史窗口；`state/engine.py::effective_entertainment_minutes` 已把这一限制写成注释。游戏退出或状态切换时可能使 `ent_before` 偏低甚至成为 `no_data`。在比较动作前要量化受影响的回执，不可把这类行当精确效果。

## 任务 2.1：建立真实干预的同一批记录

- **改动位置：** 在 `store/db.py` 增加按干预 `at` 过滤的只读关联查询；`report/queries.py` 只聚合查询结果；`report/models.py` 和 `render.py` 增加分母、排除原因与通道分层。遵守 report 不写库、不写 SQL 的现有边界。
- **主分析口径：** `channel=foreground_popup` 且 `delivery_status=delivered` 的干预；`recording`、旧 `channel=NULL`、投递失败、缺少 outcome、`no_data` 各自计数，不混入行为改善比例。原始全量视图仍可展示运行事实。
- **前台可见性：** 现有 `ForegroundPopupNotifier.notify` 在 `force_front` 失败时仅写 warning，仍可能返回 `delivered`。先核查这类日志；若出现可见性不确定的真实案例，再把“抢前台成功与否”作为新的可空事实入库，旧行保留未知，不回填为成功。
- **验收：** 同一查询区间中，各层计数之和等于干预总数；区间起点前投递、区间内检查的回执不再被误记为孤儿。

## 任务 2.2：校正并审计行为回执

- **先审计：** 按 `evaluations.fullscreen_state`、干预前后原始娱乐分钟及 `outcome` 找出游戏/全屏切换样本，计算受影响的比例和 `no_data` 分布。
- **若影响明确：** 修改 `scheduler.py` 的回执取值逻辑，让前后窗口各使用可归属其时段的证据；无法取得历史全屏状态时保留未知或 `no_data`，不拿当前值伪造历史。需要新增字段时按 `store/db.py` 增量迁移、升级 schema，旧行相关事实留 `NULL`。
- **验证：** 在 `tests/test_outcome.py`、`tests/test_scheduler.py` 和回放剧本中加入“游戏退出”“全屏状态切换”“一侧无数据”的有意义场景，证明前后口径一致且不夸大成功。

## 任务 2.3：做动作与时机的联合分析

- **现有数据可支持：** 按 `action_id`、PASSIVE/HIGH_RISK、`late_night`、触发时 `ent_minutes/ent_ratio`、时间段分层，比较接受/拒绝、`disengaged/partial/continued` 及前后娱乐分钟变化。每层给真实投递数、有效回执数、缺失数、跨越天数。
- **需新增的只是聚合：** `report/queries.py` 加纯函数形成联合分组，`report/models.py` 定义结果，`render.py` 给 text/json 表述；先复用 `interventions.evaluation_id` 连接触发时 `evaluations`，不复制快照到干预表。
- **避免伪样本量：** 五分钟重叠窗口与同一次消费中的多次提醒并非独立观察；按消费事件和自然日同时呈现。低样本层只列数据，不给“最佳动作”排名。
- **时机解释：** 现有闸门轨迹可数“状态达标但 ratio/cooldown/daily_cap 阻挡”的机会；这些机会与真实投递组选择条件不同，只用于描述漏掉的时机，不能直接充当因果对照。

## 任务 2.4：决定是否改策略

先用现有 `--replay all` 检查候选动作池或时间策略对 state_min、cooldown、daily_cap 的影响。历史数据只能支持关联判断；若要宣称某动作造成更好结果，需事先定义受控比较的分配规则、持续时间、主要结果和打扰过多时的停止条件，并记录分配事实。没有充分样本或明确增益时保留当前轮转与阈值。

## 阶段出口

报告能逐层回答“哪次真实弹出、用户按了什么、后续行为怎样”，且动作和时机的每个结论有同一批干预记录作分母。V2 只能在这个基线之上证明新增价值。
