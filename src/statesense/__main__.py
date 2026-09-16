"""命令行入口：--check 自检 / --once 单轮 / --daemon 常驻 / --report 只读汇报。"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from statesense.activity.reader import ActivityReader
from statesense.clock import Clock, SystemClock
from statesense.config import Config, ConfigError, load_config
from statesense.notify.base import Notifier, RecordingNotifier
from statesense.notify.foreground_popup import ForegroundPopupNotifier
from statesense.replay.scenarios import SCENARIOS, check_scenario, structural_findings
from statesense.report import queries, render
from statesense.report.models import ReportData
from statesense.scheduler import Scheduler
from statesense.state.taxonomy import Category, classify
from statesense.store.db import SCHEMA_VERSION, Store


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="statesense", description="StateSense-Agent V0")
    parser.add_argument("--config", type=Path, default=Path("config/config.toml"))
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="覆盖配置里的 store.path（相对当前工作目录解析）",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="校验配置与 Screenpipe 连通性后退出")
    group.add_argument("--once", action="store_true", help="跑一轮评估")
    group.add_argument("--daemon", action="store_true", help="常驻运行")
    group.add_argument("--report", action="store_true", help="只读汇报历史评估（绝不写库）")
    group.add_argument(
        "--replay", metavar="剧本", default=None, help="回放剧本：ladder/outcome/gates/degraded/sleep_gap/all"
    )
    parser.add_argument("--dry-run", action="store_true", help="不真弹窗，只记录")
    parser.add_argument("--since", default="7d", help="report 回看区间：7d / 24h / ISO 时刻")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--views", default="", help="report 视图：1,2,3,4 或 all")
    parser.add_argument("--trace", default=None, metavar="ISO时刻", help="打印该时刻附近的轨迹")
    parser.add_argument(
        "--from-screenpipe", action="store_true", help="漏判明细回查 Screenpipe（需 API key）"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def resolve_api_key(config: Config) -> str:
    key = os.environ.get(config.screenpipe.api_key_env, "").strip()
    if not key:
        raise ConfigError(
            f"环境变量 {config.screenpipe.api_key_env} 未设置；"
            "可用 `screenpipe auth token` 获取后写入该变量"
        )
    return key


def make_reader(config: Config) -> ActivityReader:
    return ActivityReader(
        base_url=config.screenpipe.base_url,
        api_key=resolve_api_key(config),
        timeout=config.screenpipe.request_timeout_sec,
    )


def make_notifier(config: Config, dry_run: bool, clock: Clock) -> Notifier:
    if dry_run:
        return RecordingNotifier(clock=clock)
    if config.notify.channel != "foreground_popup":
        raise ConfigError(f"不支持的 notify.channel: {config.notify.channel}")
    return ForegroundPopupNotifier(config.notify, clock)


def build_scheduler(config: Config, notifier: Notifier, clock: Clock) -> Scheduler:
    store = Store(config.store_path)
    store.migrate()
    return Scheduler(
        config=config,
        clock=clock,
        reader=make_reader(config),
        store=store,
        notifier=notifier,
    )


def check(config: Config) -> int:
    """自检：真实取一次数，确认配置与 Screenpipe 都可用。"""
    clock = SystemClock()
    reader = make_reader(config)
    now = clock.now()
    window = config.schedule.window_minutes
    snapshot = reader.read(now - timedelta(minutes=window), now, window, now)

    print(f"配置        OK（store={config.store_path}）")
    print(f"通道        {config.notify.channel}")
    print(f"ratio_min   {config.gate.ratio_min}")
    print(f"回看窗口    最近 {window} 分钟")
    print(f"data_status {snapshot.data_status}")
    print(f"取到 {len(snapshot.entries)} 条窗口记录，总活跃 {snapshot.total_active_minutes} 分钟")
    for entry in sorted(snapshot.entries, key=lambda e: e.minutes, reverse=True)[:5]:
        print(f"  {entry.minutes:>6.1f} 分钟  {entry.title or entry.app}")

    if snapshot.data_status != "ok":
        print(
            "警告：data_status 不是 ok，此时任何状态结论都不成立。"
            "请确认 recorder 正在运行且 token 有效。",
            file=sys.stderr,
        )
        return 1
    return 0


def _parse_since(text: str, now: datetime) -> datetime:
    """支持 `7d` / `24h` / ISO 时刻。解析失败按配置错误处理 —— 不猜。"""
    text = text.strip()
    if text.endswith("d") and text[:-1].isdigit():
        return now - timedelta(days=int(text[:-1]))
    if text.endswith("h") and text[:-1].isdigit():
        return now - timedelta(hours=int(text[:-1]))
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ConfigError(f"--since 无法解析：{text!r}（支持 7d / 24h / ISO 时刻）") from exc
    return moment if moment.tzinfo else moment.astimezone()


def _requested_views(spec: str) -> tuple[str, ...]:
    if not spec.strip():
        return ()
    if spec.strip() == "all":
        return ("1", "2", "3", "4")
    return tuple(part.strip() for part in spec.split(",") if part.strip())


def _leak_details(config: Config, anchors) -> tuple[str, ...]:
    """漏判二级视图：按需回查 Screenpipe 拿窗口明细。

    Screenpipe 本来就是那份数据的 owner，且有留存期 —— 按需去查而不是抄一份
    存起来，同时满足数据最小化与「不重造采集能力」。

    明细只打印到 stdout，绝不落库。
    """
    if not anchors:
        return ()
    try:
        reader = make_reader(config)
    except ConfigError as exc:
        print(f"漏判明细跳过：{exc}", file=sys.stderr)
        return ()

    window = config.schedule.window_minutes
    lines: list[str] = []
    for anchor in anchors:
        snapshot = reader.read(
            anchor.at - timedelta(minutes=window), anchor.at, window, anchor.at
        )
        if snapshot.data_status != "ok":
            lines.append(f"    {anchor.at}  明细不可用（data_status={snapshot.data_status}）")
            continue
        others = sorted(
            (e for e in snapshot.entries if classify(e, config.taxonomy) is Category.OTHER),
            key=lambda e: e.minutes,
            reverse=True,
        )[: config.report.leak_top_n]
        if not others:
            lines.append(f"    {anchor.at}  未命中条目为空（更可能是明细缺失，不是漏判）")
            continue
        for entry in others:
            lines.append(
                f"    {anchor.at}  {entry.minutes:>6.1f} 分钟  {entry.title or entry.app}"
            )
    return tuple(lines)


def run_report(config: Config, args: argparse.Namespace, clock: Clock) -> int:
    """只读汇报。除 migrate 外不构造任何写入语句 —— 而这里连 migrate 都不做。"""
    db_path = Path(config.store_path)
    if not db_path.is_file():
        print(f"库不存在：{db_path}；先跑 --once 或 --daemon 生成数据", file=sys.stderr)
        return 2

    store = Store(db_path)
    try:
        if store.user_version() < SCHEMA_VERSION:
            print(
                f"库 schema 为 v{store.user_version()}，需要 v{SCHEMA_VERSION}；"
                "请先跑一次 --once 完成迁移（--report 不做任何写入）",
                file=sys.stderr,
            )
            return 2

        since = _parse_since(args.since, clock.now())
        evaluations = store.list_evaluations(since=since)
        run_events = store.list_run_events(since=since)
        interventions = store.list_interventions(since=since)
        outcomes = store.list_outcomes(since=since)

        data = ReportData(
            overview=queries.build_overview(
                evaluations,
                run_events,
                evaluate_every_minutes=config.schedule.evaluate_every_minutes,
                gap_threshold_minutes=config.report.gap_threshold_minutes,
            ),
            verdicts=queries.build_verdict_breakdown(evaluations),
            gates=queries.build_gate_breakdown(evaluations),
            interventions=queries.build_intervention_breakdown(evaluations, interventions),
            outcomes=queries.build_outcome_breakdown(outcomes, interventions),
            leaks=queries.find_leak_anchors(
                evaluations,
                min_active_minutes=config.report.leak_min_active_minutes,
                min_unclassified_ratio=config.report.leak_min_unclassified_ratio,
            ),
        )

        if args.trace:
            try:
                moment = datetime.fromisoformat(args.trace)
            except ValueError as exc:
                raise ConfigError(f"--trace 无法解析：{args.trace!r}（需要 ISO 时刻）") from exc
            if not moment.tzinfo:
                moment = moment.astimezone()
            data = replace(data, trace=queries.build_trace(evaluations, moment))

        if args.from_screenpipe:
            data = replace(data, leak_details=_leak_details(config, data.leaks))

        if args.format == "json":
            print(render.render_json(data))
        else:
            print(render.render_text(data, views=_requested_views(args.views)))
        return 0
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    finally:
        store.close()


def run_replay(name: str, config: Config) -> int:
    """在独立的库上跑剧本。绝不触碰配置指向的生产库。

    `structural_findings` 打印为 WARN 而不是 FAIL：它描述的是配置本身能让
    哪段代码成为死路，不是剧本断言失败。
    """
    findings = structural_findings(config)

    if name == "all":
        failed = False
        for scenario_name in SCENARIOS:
            failures = check_scenario(scenario_name, config)
            print(f"{'FAIL' if failures else 'PASS'}  {scenario_name}")
            for reason in failures:
                print(f"      {reason}")
            failed = failed or bool(failures)
        for finding in findings:
            print(f"WARN  {finding}")
        return 1 if failed else 0

    if name not in SCENARIOS:
        available = ", ".join(sorted(SCENARIOS))
        print(f"未知剧本 {name!r}；可用：{available} 或 all", file=sys.stderr)
        return 2

    failures = check_scenario(name, config)
    print(f"{'FAIL' if failures else 'PASS'}  {name}")
    for reason in failures:
        print(f"      {reason}")
    for finding in findings:
        print(f"WARN  {finding}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        config = load_config(args.config)
        if args.db is not None:
            config = replace(config, store_path=args.db.resolve())
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    try:
        clock = SystemClock()
        if args.check:
            return check(config)
        if args.report:
            return run_report(config, args, clock)
        if args.replay is not None:
            return run_replay(args.replay, config)
        scheduler = build_scheduler(config, make_notifier(config, args.dry_run, clock), clock)
        if args.once:
            report = scheduler.run_once()
            print(
                f"state={report.state} intervened={report.intervened} "
                f"outcomes_closed={report.outcomes_closed} note={report.note}"
            )
            return 0
        scheduler.run_forever()
        return 0
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
