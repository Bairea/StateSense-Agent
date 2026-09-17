"""report 的渲染。纯函数：输入 dataclass，输出字符串，不碰 IO。

默认输出只有概览 + 结论 + 缺口。六个视图的表人不会看第二遍，
其余靠 `--views` 显式索取。
"""

from __future__ import annotations

import json
import math
from collections.abc import Collection
from dataclasses import asdict
from typing import Any

from statesense.report.models import ContinuityGap, ReportData


def _gap_reason(gap: ContinuityGap) -> str:
    """缺口旁边写清它是什么 —— 这正是本版本要消灭的二义。"""
    if gap.events:
        kinds = "、".join(f"{e.kind}({e.detail})" for e in gap.events)
        return f"有运行事件记录：{kinds}"
    return "无运行事件记录 → 进程当时不在运行"


def _pairs(items: Collection[tuple[str, int]]) -> str:
    return "  ".join(f"{key}={count}" for key, count in items) or "（无）"


def _overview_lines(data: ReportData) -> list[str]:
    ov = data.overview
    lines = ["概览"]

    if ov.evaluations == 0:
        lines.append("  区间内无任何评估记录。")
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

    total = data.verdicts.total
    skipped_pct = (data.verdicts.skipped / total) if total else 0.0
    if skipped_pct:
        lines.append(f"  skipped     {data.verdicts.skipped} 轮（{skipped_pct:.1%}）")

    lines.append("")
    if ov.gaps:
        lines.append(f"结论：数据有过 {len(ov.gaps)} 处中断，逐条成因见上方。")
    elif skipped_pct >= 0.2:
        lines.append(f"结论：连续性正常，但有 {skipped_pct:.1%} 的轮次取不到数据。")
    else:
        lines.append("结论：数据可信。")
    return lines


def _verdict_lines(data: ReportData) -> list[str]:
    v = data.verdicts
    return [
        "判定面",
        f"  评估轮数    {v.total}",
        f"  状态分布    {_pairs(v.states)}",
        f"  data_status {_pairs(v.data_statuses)}",
        f"  skipped     {v.skipped}（这些轮的 state 不代表「真的正常」）",
        f"  late_night  {v.late_night}",
        f"  全屏信号    {_pairs(v.fullscreen_states)}",
        "              （2/3=判为游戏；1/4/5/6/7=未判为游戏；unknown=无法判定）",
    ]


def _gate_lines(data: ReportData) -> list[str]:
    g = data.gates
    lines = [
        "闸门面",
        f"  各闸门阻挡  {_pairs(g.blocked_by)}",
        f"  占比分布    {_pairs(g.ratio_histogram)}",
        f"  该提醒未提醒 {len(g.state_min_passed_then_blocked)} 轮"
        "（state_min 通过却被后续闸门挡下）",
    ]
    for block in g.state_min_passed_then_blocked:
        lines.append(
            f"    {block.at}  {block.gate}  value={block.value} 阈值={block.threshold}"
        )
    return lines


def _intervention_lines(data: ReportData) -> list[str]:
    i = data.interventions
    return [
        "干预面",
        f"  触发次数    {i.total}",
        f"  按天        {_pairs(i.per_day)}",
        f"  动作分布    {_pairs(i.actions)}",
        f"  触发时状态  {_pairs(i.states)}",
        f"  投递结果    {_pairs(i.delivery_statuses)}",
        f"  用户回执    {_pairs(i.user_responses)}",
        f"  被 cooldown 挡 {i.cooldown_blocks} 轮   被 daily_cap 挡 {i.daily_cap_blocks} 轮",
    ]


def _outcome_lines(data: ReportData) -> list[str]:
    o = data.outcomes
    lines = [
        "效果面",
        f"  回执分布    {_pairs(o.outcomes)}",
        f"  no_data     {o.no_data}（窗口取不到数，不是「没影响」）",
        f"  干预前娱乐  {o.ent_before_mean}",
        f"  干预后娱乐  {o.ent_after_mean}",
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
            f"  未归类={anchor.unclassified_minutes}"
            f"（{anchor.unclassified_ratio:.1%}）"
            f"  明细缺失={anchor.missing_detail_minutes}"
        )
    if data.leak_details:
        lines.append("  窗口明细（仅打印，不落库）：")
        lines.extend(data.leak_details)
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
    """`views` 控制附加小节（1 判定 / 2 闸门 / 3 干预 / 4 效果）。

    漏判与轨迹只要非空就渲染 —— 它们是可执行的结论，不是可选装饰。
    """
    lines = _overview_lines(data)

    extra = {
        "1": _verdict_lines,
        "2": _gate_lines,
        "3": _intervention_lines,
        "4": _outcome_lines,
    }
    for key in ("1", "2", "3", "4"):
        if key in views:
            lines.append("")
            lines.extend(extra[key](data))

    if data.leaks or data.leak_details:
        lines.append("")
        lines.extend(_leak_lines(data))

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
