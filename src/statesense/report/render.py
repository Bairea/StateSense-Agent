"""report 的渲染。纯函数：输入 dataclass，输出字符串，不碰 IO。

默认输出只有概览 + 结论 + 缺口（spec §5.4）。六个视图的表人不会看第二遍，
其余靠 `--views` 显式索取。

唯一一处「默认就给」的例外是**漏判锚点数量的一行提示**：它是可执行的结论，
不是可选装饰。但它只给一行摘要，明细仍然要 `--views 5` —— 两者兼顾。
"""

from __future__ import annotations

import json
import math
from collections.abc import Collection
from dataclasses import asdict
from typing import Any

from statesense.report.models import (
    ContinuityGap,
    LeakDetailStatus,
    Liveness,
    ReportData,
    RunEvent,
    VerdictBreakdown,
)

#: 视图编号的封闭枚举。`--views` 只接受这些值，未知编号在解析层就报错，
#: 而不是被静默丢掉 —— 静默丢掉会让 `--views 5` 看起来「没输出」而不是「参数错了」。
VIEW_IDS: tuple[str, ...] = ("0", "1", "2", "3", "4", "5")
LEAK_VIEW = "5"
#: `skipped` 占比到这个数就高亮：它高说明「看起来正常」是假的。
SKIPPED_ALERT_RATIO = 0.2


def _events_phrase(events: Collection[RunEvent]) -> str:
    """把一组运行事件写成「kind(detail)、…」。缺口与掉线描述的是同一类东西，
    措辞必须出自一处，否则两处会在某次只改一边的改动后漂移。"""
    return "、".join(f"{e.kind}({e.detail})" for e in events)


def _gap_reason(gap: ContinuityGap) -> str:
    """缺口旁边写清它是什么 —— 这正是本版本要消灭的二义。"""
    if gap.events:
        return f"有运行事件记录：{_events_phrase(gap.events)}"
    return "无运行事件记录 → 进程当时不在运行"


def _pairs(items: Collection[tuple[str, int]]) -> str:
    return "  ".join(f"{key}={count}" for key, count in items) or "（无）"


def _pairs_pct(items: Collection[tuple[str, int]], total: int) -> str:
    """计数 + 占比。占比是「这个分布说明了什么」的一半 —— 只给计数看不出严重程度。"""
    if not items:
        return "（无）"
    if total <= 0:
        return _pairs(items)
    return "  ".join(f"{key}={count}（{count / total:.1%}）" for key, count in items)


def _liveness_lines(lv: Liveness) -> list[str]:
    if lv.last_at is None:
        return ["  此刻        区间内没有任何评估记录"]
    if not lv.offline:
        return [
            f"  此刻        最后一次评估在 {lv.silent_minutes:g} 分钟前"
            f"（阈值 {lv.threshold_minutes:g} 分钟）—— 仍在运行"
        ]
    lines = [
        f"  此刻        最后一次评估在 {lv.silent_minutes:g} 分钟前，"
        f"已超过阈值 {lv.threshold_minutes:g} 分钟"
    ]
    if lv.events:
        lines.append(f"              之后有运行事件：{_events_phrase(lv.events)}")
    else:
        lines.append("              之后没有运行事件记录 → 进程大概率已不在运行")
    return lines


def _overview_lines(data: ReportData) -> list[str]:
    ov = data.overview
    lv = data.liveness
    lines = ["概览"]

    if ov.evaluations == 0:
        lines.append("  区间内无任何评估记录。")
        lines.extend(_liveness_lines(lv))
        lines.append("")
        lines.append("结论：进程从未跑起来，或库路径不对。")
        return lines

    lines.append(f"  区间        {ov.first_at} → {ov.last_at}")
    lines.append(f"  评估轮数    {ov.evaluations}（应约 {ov.expected_evaluations}）")
    lines.append(f"  覆盖率      {ov.coverage:.1%}")

    if ov.gaps:
        lines.append(f"  缺口        {len(ov.gaps)} 处")
        for gap in ov.gaps:
            lines.append(f"    {gap.start} → {gap.end}   {gap.minutes} 分钟")
            lines.append(f"      {_gap_reason(gap)}")
    else:
        lines.append("  缺口        无")

    lines.extend(_liveness_lines(lv))

    total = data.verdicts.total
    skipped_pct = (data.verdicts.skipped / total) if total else 0.0
    if skipped_pct:
        lines.append(f"  skipped     {data.verdicts.skipped} 轮（{skipped_pct:.1%}）")

    lines.append("")
    # 掉线压过其余一切结论：其余结论都建立在「进程还在跑」这个前提上。
    if lv.offline:
        tail = (
            "之后有运行事件记录，成因见上方"
            if lv.events
            else "之后没有运行事件记录 —— 进程大概率已不在运行"
        )
        lines.append(
            f"结论：**此刻已掉线** —— 最后一次评估距今 {lv.silent_minutes:g} 分钟，"
            f"超过阈值 {lv.threshold_minutes:g} 分钟；{tail}。"
        )
    elif ov.gaps:
        lines.append(f"结论：数据有过 {len(ov.gaps)} 处中断，逐条成因见上方。")
    elif skipped_pct >= SKIPPED_ALERT_RATIO:
        lines.append(f"结论：连续性正常，但有 {skipped_pct:.1%} 的轮次取不到数据。")
    else:
        lines.append("结论：数据可信。")
    return lines


def _verdict_lines(data: ReportData) -> list[str]:
    v: VerdictBreakdown = data.verdicts
    total = v.total
    skipped_pct = (v.skipped / total) if total else 0.0
    lines = [
        "判定面",
        f"  评估轮数    {v.total}",
        f"  状态分布    {_pairs_pct(v.states, total)}",
        f"  data_status {_pairs_pct(v.data_statuses, total)}",
    ]
    skipped_line = f"  skipped     {v.skipped} 轮（{skipped_pct:.1%}）"
    if skipped_pct >= SKIPPED_ALERT_RATIO:
        skipped_line += "  ⚠ 占比偏高 —— 这些轮的 NORMAL 不代表「真的正常」"
    lines.append(skipped_line)
    lines.append(f"  late_night  {v.late_night}（{v.late_night / total:.1%}）" if total else "  late_night  0")
    lines.append(f"  全屏信号    {_pairs_pct(v.fullscreen_states, total)}")
    lines.append("              （2/3=判为游戏；1/4/5/6/7=未判为游戏；unknown=无法判定）")
    lines.append(f"  规则版本    {_pairs_pct(v.rule_versions, total)}")
    # 分类清单与阈值不落在结果行里，所以「这些样本是哪套规则判的」只能从这一档读。
    # 混读的后果不是误差，是把两次变更的效果算成一次 —— 必须写在报告里，
    # 而不是留给读者去猜。
    if any(name == "unknown" for name, _ in v.rule_versions):
        lines.append("              （unknown=迁移前写入的行，规则版本未知，不能当作某一版）")
    if len(v.rule_versions) > 1:
        lines.append(
            "              ⚠ 区间内跨规则版本：逐条结论必须先按版本分组，"
            "否则「阈值调优的效果」与「规则换了」分辨不出来"
        )
    # 这是视图 5 的盲区，必须写在判定面上：它决定了「漏判结论覆盖了多少轮」。
    if v.entries_unknown:
        lines.append(
            f"  明细缺失    {v.entries_unknown} 轮（{v.entries_unknown / total:.1%}）"
            "  ⚠ 这些轮报活跃却没有条目明细，漏判判据对它们不成立"
        )
    return lines


def _gate_lines(data: ReportData) -> list[str]:
    g = data.gates
    lines = [
        "闸门面",
        f"  各闸门阻挡  {_pairs(g.blocked_by)}",
        "              （按「第一个未通过的闸门」计，那才是判定失败的主因）",
        f"  各闸门触达  {_pairs(g.blocked_any)}",
        "              （按「每一个未通过的闸门」计：一轮可能同时被 cooldown 与 "
        "daily_cap 挡下）",
        f"  ratio_min   达标 {g.ratio_min_passed} 轮 / 被挡 {g.ratio_min_blocked} 轮",
        f"  占比分布    {_pairs(g.ratio_histogram)}",
        f"  该提醒未提醒 {len(g.state_min_passed_then_blocked)} 轮"
        "（state_min 通过却被后续闸门挡下）",
    ]
    for block in g.state_min_passed_then_blocked:
        lines.append(
            f"    {block.at}  {block.gate}  value={block.value} 阈值={block.threshold}"
        )
    if g.corrupt_rows:
        lines.append(
            f"  闸门数据损坏 {g.corrupt_rows} 轮"
            "  ⚠ 这些行已从上面的统计中剔除，真实阻挡次数只会更多"
        )
    return lines


def _intervention_lines(data: ReportData) -> list[str]:
    i = data.interventions
    return [
        "干预面",
        f"  触发次数    {i.total}",
        f"  按天        {_pairs(i.per_day)}",
        f"  投递通道    {_pairs(i.channels)}"
        "（recording = --dry-run 的排练，foreground_popup = 真弹窗）",
        f"  动作分布    {_pairs(i.actions)}",
        f"  触发时状态  {_pairs(i.states)}",
        f"  投递结果    {_pairs(i.delivery_statuses)}",
        f"  用户回执    {_pairs(i.user_responses)}",
        f"  被 cooldown 挡 {i.cooldown_blocks} 轮   被 daily_cap 挡 {i.daily_cap_blocks} 轮",
    ]


def _outcome_lines(data: ReportData) -> list[str]:
    o = data.outcomes
    total = sum(count for _, count in o.outcomes)
    no_data_pct = (o.no_data / total) if total else 0.0
    lines = [
        "效果面",
        f"  回执分布    {_pairs_pct(o.outcomes, total)}",
        f"  no_data     {o.no_data}（{no_data_pct:.1%}）"
        "—— 窗口取不到数，不是「没影响」",
        f"  干预前娱乐  均值 {o.ent_before_mean}  中位数 {o.ent_before_median}",
        f"  干预后娱乐  均值 {o.ent_after_mean}  中位数 {o.ent_after_median}",
        "  按用户回执分层（V0 规格 §16 风险 6 要求的缓解措施）：",
    ]
    if o.by_response:
        for layer, outcomes in o.by_response:
            lines.append(f"    {layer:<10} {_pairs(outcomes)}")
    else:
        lines.append("    （无）")
    return lines


def _leak_lines(data: ReportData) -> list[str]:
    lines = ["疑似漏判（这一轮有活动，但规则一条都没命中）"]
    for anchor in data.leaks:
        lines.append(
            f"  {anchor.at}  活跃={anchor.total_active_minutes}"
            f"  娱乐={anchor.ent_minutes}  灰色={anchor.gray_minutes}"
            f"  工作={anchor.work_minutes}"
            f"  未归类={anchor.unclassified_minutes}"
            f"（{anchor.unclassified_ratio:.1%}）"
            f"  明细缺失={anchor.missing_detail_minutes}"
        )
    if not data.leaks:
        lines.append("  （无）")
    # 盲区必须写在漏判视图里而不是别处：读者在这一节下结论「没有漏判」，
    # 而这句话对明细缺失的轮次并不成立。
    unknown = data.verdicts.entries_unknown
    if unknown:
        lines.append(
            f"  注意：另有 {unknown} 轮报活跃却没有条目明细，"
            "上述判据对它们不成立 —— 它们既不能算漏判，也不能算没漏判。"
        )
    if data.leak_details:
        lines.append("  窗口明细（仅打印，不落库）：")
        for d in data.leak_details:
            if d.status is LeakDetailStatus.UNAVAILABLE:
                lines.append(f"    {d.at}  明细不可用（data_status={d.data_status}）")
            elif d.status is LeakDetailStatus.EMPTY:
                lines.append(f"    {d.at}  未命中条目为空（更可能是明细缺失，不是漏判）")
            else:
                for entry in d.entries:
                    lines.append(
                        f"    {d.at}  {entry.minutes:>6.1f} 分钟  {entry.label}"
                    )
    return lines


def _trace_lines(data: ReportData) -> list[str]:
    lines = ["轨迹"]
    for row in data.trace:
        gates = "  ".join(
            f"{name}={'通过' if passed else '挡下'}({value}/{threshold})"
            for name, passed, value, threshold in row.gates
        )
        mark = " [skipped]" if row.skipped else ""
        lines.append(
            f"  {row.at}  {row.state}{mark}  ratio={row.ent_ratio}"
            f"  ent={row.ent_minutes}/{row.total_active_minutes}  {row.decision}"
        )
        if gates:
            lines.append(f"      {gates}")
    return lines


def render_text(data: ReportData, *, views: Collection[str] = ()) -> str:
    """`views` 控制附加小节（0 概览 / 1 判定 / 2 闸门 / 3 干预 / 4 效果 / 5 漏判）。

    概览是默认输出的主体，因此 `--views 0` 是显式写下默认行为，不额外打印一遍。
    """
    lines = _overview_lines(data)

    extra = {
        "1": _verdict_lines,
        "2": _gate_lines,
        "3": _intervention_lines,
        "4": _outcome_lines,
        "5": _leak_lines,
    }
    for key in VIEW_IDS:
        if key != "0" and key in views:
            lines.append("")
            lines.extend(extra[key](data))

    # 漏判给了明细视图，但没要它的时候仍然报一句数量：这是可执行的结论，
    # 默认输出里看不到就等于这一版的核心产物被藏在参数后面。
    if data.leaks and LEAK_VIEW not in views:
        lines.append("")
        lines.append(
            f"疑似漏判    {len(data.leaks)} 轮有活动但规则一条都没命中"
            f"（--views {LEAK_VIEW} 看明细）"
        )

    if data.trace:
        lines.append("")
        lines.extend(_trace_lines(data))

    return "\n".join(lines)


def _jsonable(value: Any) -> Any:
    """把非有限浮点数换成 null。

    JSON 没有 inf / NaN 的表示。旧版本的代码往 `gate_trace` 里写过 `Infinity`
    （表示「从未干预过」），`json.dumps` 默认会把它原样输出成裸 `Infinity` ——
    那不是合法 JSON，`jq`、JavaScript、Go 的严格解析器都会直接拒绝。

    report 读的是库里的历史行，包括旧版本写的行，所以这里必须挡住了再输出。
    转成 null 也正好对应 `GateResult.value = None` 的既有语义：「这个概念此刻不适用」。
    """
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def render_json(data: ReportData) -> str:
    # allow_nan=False 是断言：上面已清干净，若还有非有限值漏过来，宁可当场炸。
    return json.dumps(
        _jsonable(asdict(data)), ensure_ascii=False, indent=2, default=str, allow_nan=False
    )
