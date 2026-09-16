"""命令行入口：--check 自检 / --once 单轮 / --daemon 常驻。"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from statesense.activity.reader import ActivityReader
from statesense.clock import Clock, SystemClock
from statesense.config import Config, ConfigError, load_config
from statesense.notify.base import Notifier, RecordingNotifier
from statesense.notify.foreground_popup import ForegroundPopupNotifier
from statesense.scheduler import Scheduler
from statesense.store.db import Store


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
    parser.add_argument("--dry-run", action="store_true", help="不真弹窗，只记录")
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
