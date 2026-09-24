# 阶段 3 实现记录：影子模型信号与文案接口（任务 3.1 / 3.2 / 3.3 / 3.4 的离线部分）

> 分支 `feat/v2-llm-shadow`（自 `main@54b367e` 切出）。对应计划：
> `docs/plans/2026-09-22-stage-3-v2-llm.md`。本文只记已验证的事实；
> 做不到的事写「未做」，不用「机制已实现」替代「效果已证明」。

## 已落地

### 任务 3.2：隐私与模型输入契约

- `shadow/models.py`：
  - `StateShadowInput` —— 送去模型的**输入白名单**，六个原子量
    （`ent_minutes` / `gray_minutes` / `work_minutes` / `total_active_minutes` /
    `window_minutes` / `late_night`）。`ALLOWED_FIELDS` 顺序即契约，
    镜像测试逐字断言；只发原子量，不发规则算出的比值与汇总
    （那等于把 `classify` 的口径复制到第二处）。
  - `PRIVATE_FIELDS`（窗口标题 / URL / 屏幕正文 / app）**永不离开本机**；
    `LEAKY_FIELDS`（`state`）刻意不作为输入——把答案塞进输入，
    影子对比会退化成「抄一遍规则」。
  - `ModelCandidate` 有限枚举：四档状态候选 + `uncertain` / `refused`
    两档「明确不下结论」；`ShadowOutcome` 五档结局
    （ok / timeout / invalid_output / provider_error / no_data）。
    「模型判为正常」与「模型没答上来」是两件事，绝不塌缩成一个「没有候选」。
  - `ModelSignal` 不变式（构造时强制）：`outcome=ok` ⇔ 有候选；
    `latency_ms` 为空 ⇔ `no_data`（没调用就没有耗时，写 0 会让
    「极快的成功调用」与「根本没调用」在报表里同值）。
- 超时分两层各管一段：**传输层**由 provider 实现负责（抛 `TimeoutError`）；
  **结果层**在 collector——返回时已超过 `shadow.timeout_seconds` 的答复
  一律丢弃记成 timeout。迟到的答案既不是成功，也不该进影子记录。
- 存储（schema v7 → v8）：
  - 新增 `shadow_signals` 表。`evaluation_id` 既是主键又是外键：一轮评估
    最多一个候选，候选不能脱离评估行存在；表里**刻意没有 `at` 列**——
    「什么时候」只能有一个来源，就在 `evaluations.at`。
  - 旧行不回填：「没问」绝不写成「模型判为否」。
  - 运行事件新增 `shadow_error` 单独一类，不并进 `tick_error`：
    影子落库失败不影响投递，那一轮的判定与投递其实都成功了。
- `ActivityReader` 仍是唯一知道 Screenpipe 的模块；影子层不新增任何采集，
  只从已算好的聚合量里取数。

### 任务 3.4：影子运行（离线部分）

- `Scheduler._collect_shadow`：影子候选在**判定之前**产生——放到判定之后，
  `verdict.state` 就在手边，「顺手带上它」会让对比失去意义。归类一次共用
  （`classify` 新增可选 `buckets` 参数，两种取值下的 `StateVerdict` 必然相同，
  只省掉重复计算）。
- 影子对真实行为的隔离是**结构性**的：采样与落库的任何失败都只落
  `shadow_error` 运行事件，不抛出；候选不进 `Decision`、不进闸门、不进投递。
- 视图 9（`--report --views 9`）：覆盖度（被问轮次/评估轮次）、结局分布、
  分歧四档（一致 / 候选更重 / 候选更轻 / 候选不下结论）、够档差异、
  首次可提醒时间、延迟（均值/中位/最大，只算真的调用过的行）、
  拒答率（分母是被问轮次）、模型版本分组（换模型前后不混算）。
  **刻意不使用「漏判/误报」**：那是与人工真值比出来的结论，真值还不存在。
- 首次可提醒时间按规则侧消费事件切分（与视图 8 同源的 `_intervenable_runs`）；
  每段事件看「从上一段事件结束到本段结束」的全部影子行——段起点就是规则
  第一次够到档的那一轮，只看段内的话「模型更早」这一档结构上不可达；
  跨段不重复归因（一段行只服务它之后最近的一段事件，有测试专门拦截
  「窗口下界失效」的回归）。
- 回放剧本 `shadow`（挂在 `--replay all` 下）：控制组（影子关）与实验组
  （影子开）在同一条时间线上**逐行比对**判定 / 闸门留痕 / 投递完全一致；
  同档 / 不确定 / 拒答 / 超时 / 畸形输出 / 调用失败六种情形全部落到对应档；
  另验「输入不可信时不调用模型」（答复池为空，真调了会自动暴露）。
- 配置：`[shadow] enabled = false`（默认关闭），`provider` 封闭枚举只有
  `offline`（确定性替身，**它不是模型**），未知取值在配置层就被拒绝；
  `timeout_seconds` 必须为正。影子节不参与 `rulebook.rule_version`——
  它不改变任何判定规则。
- 状态深浅新增全仓唯一顺序 `SEVERITY_ORDER` / `severity_rank`
  （`state/models.py`），影子分歧的「更重 / 更轻」靠它排序。

## 验证（2026-09-24，本机实测）

- `uv run pytest -q`：**520 passed**（main 上 445）。
- `--replay all`：六个剧本全部 PASS（ladder / outcome / gates / degraded /
  sleep_gap / shadow）。
- `--replay shadow --keep-db` 后 `--report --views 9 --since 60d` 实跑：
  被问 19 / 19 轮（覆盖 100%）；结局 ok=16 / timeout=1 / invalid_output=1 /
  provider_error=1；拒答 1 次（5.3%）；分歧 一致 1 / 更重 2 / 更轻 11 /
  不下结论 2；首次可提醒 候选更早 1 个（平均提前 10.0 分钟）。
  **以上全部来自离线替身**（模型版本 `offline-scripted`），只证明记录链路通，
  不能用来判断模型的增量价值——报表自己对这类行打了免责说明。

## 顺手修复（与影子无关，但挡住「全绿」验证）

- `tests/test_report_cli.py` 两条时间炸弹：数据钉在 T0=2026-09-16，默认 7d
  回看区间自 2026-09-23 起把数据滚出窗口——main 上即失败。改为显式
  `--since` 锚定到数据本身，不再依赖「今天几号」。
- `tests/test_report_queries.py` 的 lead-time「一行不服务两段」测试：
  原断言自相矛盾（注释称模型在段首够档，却断言「更早 120 分钟」），
  且按原摆法，无论窗口下界是否失效结果都一样——根本检测不到它声称要防的
  缺陷。重写为「唯一够档的行落在第一段窗口内、第二段窗口全程不够档」的摆法，
  下界一旦失效第二段就会凭空得到「提前 120 分钟」，断言随即失败。

## 任务 3.3（2026-09-24 补）：文案接口拆分与离线评估

- `WordingContext`（wording-context@v1）：候选文案的**传输白名单**——state /
  late_night / 四个分钟量 / ent_ratio / top_category / action_id / action_text，
  镜像测试对着 `CONTEXT_FIELDS` 逐字断言。与 shadow 输入白名单的两点刻意差异
  写在类型文档里：`state` 在（文案描述判定结果，不是预测它）；`ent_ratio` 在
  （它是模板会显示的数字，读自 verdict，不构成第二处口径）。
- `top_category` 是 top_label 的隐私安全替身（3.1 文档 W3）：哪一类分钟最多，
  并列取更重的一类——与弹窗正文的口径一致，不自造第二套。
- `RemoteWording` 协议 + `ScriptedRemoteWording` 离线替身（与影子替身同一套
  纪律：预设用尽即抛错、异常条目模拟失败、`calls` 记录传输面）。
- `RemoteWordingAdapter`：唯一适配层。构造上下文（无 top_label）→ 调候选 →
  失败或空文案回退模板并计数。**失败回退是硬要求**：文案异常若外逃会被
  tick 容错吃成 tick_error，那一轮的真实投递就丢了——失败的代价必须只是
  一句模板，不多、不少。
- `run_scenario` 新增 `wording` 参数（与 shadow 参数同款纪律：回放绝不碰真实远端）。
- 验证：新增 14 条测试——契约镜像、隐私边界（含「模板自己带标题」的反向对照）、
  失败/空文案回退、回放等价性（候选与基线的触发时刻 / 动作 ID / 状态逐行一致、
  正文不同；全程失败的候选正文与基线逐字一致）、比较指标（泄露扫描 / 长度 /
  重复双向 / 计时）。全量 **543 passed**，`--replay all` 六剧本 PASS。
- W1（回执上下文字段）刻意未进 v1 契约：需要 Scheduler 先把提醒次数与回执
  状态传进文案层，等真实 provider 落地时随字段一起加并升版本号。

## 任务 3.4 补充（2026-09-24）：真实远端供应器（http）

部署方式由用户拍板：OpenAI 兼容的 `chat/completions` 端点 + glm 模型，
配置走 `.env`（**`.env` 不入库**；仓库提交 `.env.example`，只含端点与模型名，
不含密钥）。

- `llm.py`：`.env` 装载（解析子集：KEY=VALUE / 整行注释 / `export ` 前缀 /
  成对引号）。查找顺序 CWD/.env → 配置目录/.env（先找到的整体生效），
  已存在的环境变量优先。缺项 fail closed——启动与 `--check` 即报错，
  只指名变量、绝不回显值；URL 强制 https（带密钥的请求头不许走明文）。
- `shadow/http_provider.py`：stdlib urllib 实现，**零新依赖**。传输层超时
  还原成 `TimeoutError`（含包在 URLError 里的情形）；应答**结构**损坏 →
  抛错 → provider_error，应答**文本**不成话 → 原样上交 → invalid_output
  ——「调用失败」与「模型答了但答得不成话」分档，影子对比的分母才真实。
  Markdown 围栏剥除；非 JSON 内容整体当候选交给枚举校验，原文截断留档。
- 配置：`KNOWN_SHADOW_PROVIDERS = ("offline", "http")`；`Config` 新增
  `config_dir`（.env 兜底查找用，只存目录不存内容——密钥绝不进 Config）；
  `--check` 增加 fail-fast；`shadow_phrase` 区分替身与真实模型。
- 测试隔离坑：仓库根的真实 `.env` 会被 CWD 查找捞进测试（实测 model 读回了
  glm-5.3-flash 而不是桩值）——`test_llm_env` 的 autouse 夹具把 CWD 切到
  tmp_path，并清空三条环境变量。
- 验证：18 条新测试（**不打真网**：urlopen 桩 + 假密钥）——env 解析 / 优先级 /
  fail-closed；请求形状（Bearer + 白名单载荷逐字段相等）；超时翻译（裸抛与
  URLError 包裹两态）；401 / 坏结构 → provider_error 且 detail 无密钥；
  乱文本 / 近义缩写 → invalid_output；围栏剥除。全量 **561 passed**，
  六剧本 PASS。
- **真实冒烟**（glm-5.3-flash，2026-09-24，两次调用均只含白名单字段）：
  (45, 0, 10, 60) → ok / PASSIVE_CONSUMPTION / ~1.0s，理由引用了 75% 占比；
  (0, 0, 0, 60)（F2 指纹）→ ok / **uncertain**——模型对「满窗未归类」
  如实不下结论，与 3.1 的输入可分性分析一致。
- 仍未做：W1 回执上下文字段、文案侧真实 provider（等 3.3 样本证明收益）、
  3.5 有限上线（等真实影子样本）。

## 任务 3.3 补充（2026-09-24）：W1 回执上下文（契约 v2）与文案侧真实 provider

用户拍板把剩余项做掉后，同日完成两件事：

- **契约 v1 → v2**：`WordingContext` 新增 `reminders_today`（当日已提醒次数，
  不含本次）与 `last_receipt`（最近一次**已结算**回执标签：continued / partial /
  disengaged / no_data，None = 没有先例）。两者都不私密——一个来自干预表
  计数，一个来自回执标签。构造点移进 `Scheduler._wording_context`
  （与 `StateShadowInput` 同一纪律：数字由调度层显式递进来）；
  本地协议签名随之改为 `render(context, top_label)`，模板改从 context 取数，
  **输出逐字不变**（模板刻意不消费回执上下文——它是等价性里的常量，有测试钉死）。
  传输层重构出 `llm.LlmClient`（OpenAI 兼容 chat/completions 的唯一实现），
  影子与文案共用，超时翻译与错误语义不再有两份。
- **文案侧真实 provider**：`wording_http.HttpWordingProvider`（RemoteWording 协议），
  与影子同一 `.env` 三项。配置新增 `[wording]` 节（封闭枚举 template / http，
  默认 template；`timeout_seconds` 独立于影子——文案调用在投递路径上，
  慢模型会拖住弹窗）。适配层新增 `MAX_BODY_CHARS = 300` 硬上限：超长按失败
  回退，「模型话多」不能变成一次糟糕的投递。回执读取失败按「没有先例」写
  并记日志——素材缺失不能变成丢投递。
- 验证：新增 17 条测试（契约 v2 镜像、回执上下文流进载荷、**回执读取炸了
  投递一次不少**、http 文案供应器的清理与结构错误回退、配置接线含未知
  provider 启动期拒绝）。模板输出与 v1 逐字一致有测试锁。
- **真实冒烟**（glm-5.3-flash，reminders_today=1 + last_receipt=disengaged）：
  53 字正文「过去一小时娱乐占了45分钟、约75%，和上次一样没理会提醒。
  建议现在离开电脑走5分钟，让眼睛和大脑歇一歇。」——含数据、含回执呼应、
  含动作，无 markdown 无引号。全量 **570 passed**，六剧本 PASS。
- 3.5（有限上线）**仍然受阻**：前置条件是 3.4 在真实影子样本上的稳定增益，
  而这需要 daemon 常驻产出连续数据（本机尚未常驻运行）；且「模型影响投递」
  必须先有 3.4 的判定规格。解锁路径：用户以任务计划程序常驻 `--daemon`
  （影子开），积累数日真实样本后用 `--report --views 9` 复查分歧与覆盖度，
  再决定是否写判定规格。

## 未做（按计划顺序，需后续单独推进）

- **任务 3.1（固定失败样本集）**：已完成（2026-09-24 补）——失败类基线、
  目标指标、输入可分性与规则处置见
  `docs/plans/2026-09-24-stage-3-failure-samples.md`，读数钉在
  `tests/test_stage3_failure_samples.py`。关键结论：F3（全屏文档误报）与
  真全屏游戏在 v1 影子输入契约下指纹相同，结构性不可及；人工真值仍不存在，
  合成样本只钉口径性质。
- **任务 3.3（文案评估）**：离线部分与真实远端 provider 均已落地（见上文
  任务 3.3 两节）；「常开 http」仍等样本证据——计划 3.3 验收线，
  默认 template。
- **真实模型 provider**：`http` 供应器已落地（2026-09-24 补，见上文「任务 3.4
  补充」节），配置走 `.env`（不入库）。文案侧真实 provider 仍待 3.3 样本证明收益。
- **任务 3.5（有限上线）**：前置条件（3.4 在真实影子样本上的稳定增益）不存在。
- **阶段出口**：「LLM 的具体收益、代价和数据流有证据支持」**未达成**——
  本阶段当前只完成了「分歧可被记录、可被观测、可被复盘」这一基础设施。
