# 代码评审：2026-09-22 当日改动（双轴）

- **定点**：`e3681a2`（2026-09-18 21:59，「docs: 按事实修订规格四处」）
- **范围**：`git diff e3681a2...HEAD`，10 个提交（`936d616` … `64ca180`）
- **规模**：27 文件；原始 `5866 insertions / 2568 deletions`，忽略空白后 `3406 / 108`（见 Standards 轴）
- **规格来源**：`docs/plans/2026-09-22-stage-{0,1,2,3}-*.md` + `docs/specs/2026-09-16-*.md`
- **标准来源**：`AGENTS.md`、`CONTRIBUTING.md`、`docs/project-understanding.md` + Fowler smell 基线
- **未能取得**：`docs/agents/issue-tracker.md` 不存在，提交信息无 `#NNN` 引用，故无法按问题单核对（skill 要求在缺失时提示这一点）

复核方式：两轴各由独立子代理跑，之后本次复核逐条重跑命令取证；下文凡标「已核实」的均由复核者本人执行命令确认。

---

## Standards

**核实**：线索成立，但你漏了 2 个文件。`4bd9a4b` 一次性把 8 个文件 LF→CRLF：你列的 6 个，**＋`tests/test_store.py`(325)、`tests/test_stage1_event_calibration.py`(620，cd8a084 建时是 LF)**。`report/queries.py` 仍 LF；`prd1.md`/`ref1.md` 一直是 CRLF（非本次）。raw 5866/2568 → `-w` 3406/108，噪声≈4900 行。

**(a) 无硬违规**——三份文档全文无行尾/`.gitattributes` 规则（已 grep）。合规项：schema 走 `db.py` 增量迁移＋`SCHEMA_VERSION` 6→7；`report/` 不写 SQL。附带：CONTRIBUTING §5 的 `rulebook` scope 是本次 `451b724` 才补的（**标准随变更移动**）；`AGENTS.md` 未被 git 跟踪。

**(b) 是**：应单开一个只归一化行尾的提交（或补 `.gitattributes`），否则 4900 行噪声全落到 `4bd9a4b` 的 blame。**(c)** 见上。

**smell（判断题）**
1. Duplicated Code＋死代码：`store/db.py:319`／`:358` 逐字重复 `list_intervention_cohort`，后者覆盖前者。
2. Mysterious Name：`render.py:29` 注释「7=动作×时机，8=回执审计」与 `:414-416`、`AUDIT_VIEW`、测试**全部相反**。
3. Duplicated Code：`_parse(row["at"]).astimezone().date().isoformat()` 在 queries.py 出现 5 次。
4. Repeated Switches：`is_real_delivery` 与 `cohort_layer` 对同一组合判两次。
5. Speculative Generality：`raw_ticks` 与 `total_deliveries` 恒等，渲染层不读。

### 复核者补正（不改排序，仅纠正事实）

- smell 3 的实际次数是 **4 次**（`queries.py:301, 445, 666, 710`），不是 5 次；全在 `report/queries.py` 内。
- smell 4 需要减弱：两处 docstring 明确写了「干预面的事实 vs 效果面的准入条件是两件事」，仓库文档覆盖基线，故这一条**基本被压制**；残留的真实风险只是「真实投递」的定义若变更，两处必须同改。
- smell 2 的判断成立，但方向要说清：`AUDIT_VIEW="7"`／`TIMING_VIEW="8"` 与实现记录（视图 7 审计、视图 8 动作×时机）一致，**错的是 `render.py:29` 那句注释**，它把 7/8 写反了。
- 行尾噪声的量级已复核：报告 diff 共 8434 行，`-w` 后 3514 行 → **约 4920 行（58%）是纯空白/行尾噪声**。翻转只发生在这 8 个文件，同一批次新建的 `tests/test_stage1_rule_boundaries.py`（`cd8a084` 建）仍是 LF —— 说明不是编辑器全局设置，而是 `4bd9a4b` 整文件重写了它碰过的那 8 个。

---

## Spec

## (a) 规格要求了、但缺失或只做一半

1. 阶段 2 §2.2 原文「在 `tests/test_outcome.py`、`tests/test_scheduler.py` **和回放剧本**中加入『游戏退出』『全屏状态切换』『一侧无数据』的有意义场景」——diff 里 `src/statesense/replay/` 与回放剧本零改动，回放剧本这半边未做。
2. 阶段 1 §1.1 验收原文「旧行『版本未知』与新版本不混算」——`render.py:_cohort_lines` 只对跨版本打警告，均值仍合并计算；`group_by_rule_version` 无生产调用点（仅 `tests/test_report_queries.py:291`），等于没生效。
3. 阶段 2 §2.1 验收原文「区间起点前投递、区间内检查的回执不再被误记为孤儿」——视图 4 仍用 `list_outcomes`/`list_interventions` 两个时间轴，`queries.py:63` 的 `ORPHAN_INTERVENTION` 仍在，只有新视图 6 修好。

## (b) diff 里做了、规格没要求

未发现越界：视图 0–5 编号未动，仅追加 6/7/8，符合「`--views` 的取值是封闭集合」只往后排号；未提前碰阶段 3 的 LLM/模型字段。

## (c) 疑似实现不对

- `store/db.py:319` 与 `db.py:358` **逐字节重复定义** `list_intervention_cohort`，后者静默覆盖前者——违反规格反复强调的「同一件事只能有一个实现」。
- `report/render.py:30` 注释「6=效果分母，**7=动作×时机，8=回执口径审计**」与实际 `AUDIT_VIEW="7"`、`TIMING_VIEW="8"`、`render_text` 分发表，以及实现记录「视图 7 审计、视图 8 动作×时机」正好相反，注释错位。

其余阈值/公式/唯一入口（`effective_entertainment_minutes`、`rulebook.version_of`、schema v7）与规格一致，未发现第二套等价实现。

### 复核者补正

- (a)3 已逐行核实：`build_outcome_breakdown`（`queries.py:321`）的签名就是 `(outcomes, interventions)` 两个序列、按 id 相互 join，落不到就记 `ORPHAN_INTERVENTION`（`:339`）。**视图 4 确实还是双时间轴。**
- (a)2 已核实：`group_by_rule_version` 定义在 `queries.py:167`，全仓唯一调用点是 `tests/test_report_queries.py:291` —— 生产代码零调用。
- (c) 第一条已用字节级比对确认：两处函数体 39 行 / 38 行，**唯一差别是末尾一个空行**，因此运行时行为不变；但第二处（`358`）是类内最后一个方法，第一处（`319`）是永不生效的死代码 —— 改它是不会有效果的陷阱。
- 独立验证：`uv run pytest -q` → **439 passed**，与实现记录声称的 439 一致。

---

## 汇总

- **Standards 轴 6 条**（1 条行尾/diff 噪声 + 5 条 smell，均属判断题）。本轴最严重：`store/db.py:319` 与 `:358` 逐字重复定义 `list_intervention_cohort`，前者是永不生效的死代码，违反仓库自己写的「同一件事只能有一个实现」。
- **Spec 轴 5 条**（3 缺失/半做、0 越界、2 疑似实现不对）。本轴最严重：阶段 2 §2.2 明文要求的**回放剧本场景未做**，而实现记录里没有把它列为未完成。
- 两条轴独立地都指到了 `db.py` 的重复定义；除此之外两轴无重叠，未做跨轴排序。
