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
from statesense.perception import default_probe, describe
from statesense.replay.scenarios import SCENARIOS, check_scenario
from statesense.report import queries, render
from statesense.report.models import LeakWindowDetail, ReportData
from statesense.scheduler import Scheduler
from statesense.store.db import SCHEMA_VERSION, Store

#: 固定的 logger 名。用 `__name__` 的话，`python -m statesense` 下它是 `__main__`，
#: 日志前缀看起来像个内部模块名；而换个启动方式前缀又会变。
log = logging.getLogger("statesense.cli")

#: 只有 `--report` 认的参数，写在 help 里以免被当成通用参数。
_REPORT_ONLY = "（仅 --report 有效）"
_DRIVEN_ONLY = "（仅 --once / --daemon 有效；--replay 恒为不弹窗）"


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
    parser.add_argument("--dry-run", action="store_true", help=f"不真弹窗，只记录{_DRIVEN_ONLY}")
    parser.add_argument(
        "--since", default="7d", help=f"report 回看区间：7d / 24h / ISO 时刻{_REPORT_ONLY}"
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help=f"输出格式 text / json{_REPORT_ONLY}",
    )
    parser.add_argument(
        "--views",
        default="",
        help=f"report 视图：{'/'.join(render.VIEW_IDS)} 的逗号组合，或 all{_REPORT_ONLY}",
    )
    parser.add_argument(
        "--trace", default=None, metavar="ISO时刻", help=f"打印该时刻附近的轨迹{_REPORT_ONLY}"
    )
    parser.add_argument(
        "--from-screenpipe",
        action="store_true",
        help=f"漏判明细回查 Screenpipe（需 API key）{_REPORT_ONLY}",
    )
    parser.add_argument(
        "--keep-db",
        action="store_true",
        help="replay 跑完保留库文件（默认写临时库并删除，绝不污染生产库）",
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


def _current_branch(start: Path | None = None) -> str | None:
    """从 `.git/HEAD` 直接读分支名，**不起子进程**。

    分支落进启动日志有具体理由：实测从错误的分支起过一次 daemon，它把库的
    `user_version` 降级回 3、并写了一堆 `entries_minutes=0` 的假行。当时是靠
    事后对库才发现。启动行里带分支名，这种事一眼就能看见。

    读不到就返回 `None`（非仓库、`.git` 形态不认识、权限不足）—— 这只是一行日志，
    不能因为它让 daemon 起不来。

    `start` 只为测试而存在：真实调用永远从本文件所在目录往上找。
    """
    root = start if start is not None else Path(__file__).resolve().parent
    head: Path | None = None
    for parent in (root, *root.parents):
        candidate = parent / ".git"
        if candidate.is_dir():
            head = candidate / "HEAD"
            break
        if candidate.is_file():
            # worktree：`.git` 是一个指向真实 gitdir 的文件
            try:
                text = candidate.read_text(encoding="utf-8").strip()
            except OSError:
                return None
            if text.startswith("gitdir:"):
                head = Path(text.split(":", 1)[1].strip()) / "HEAD"
            break
    if head is None:
        return None
    try:
        content = head.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    prefix = "ref: refs/heads/"
    if content.startswith(prefix):
        return content[len(prefix) :]
    # detached HEAD：给出短哈希，总比写「未知」有用。
    return f"detached@{content[:12]}"


def log_startup(config: Config, *, dry_run: bool) -> None:
    """常驻启动行。**它存在的唯一理由是让重启可见。**

    daemon 在健康的一轮里什么都不打印（记录落在库里，不重复存两遍），
    所以一份空的日志既可能是「一切正常」，也可能是「刚被重启过」——
    这两件事完全不同，却长得一样。加上这一行之后，每次重启都会留下一条
    带时间戳和 pid 的启动记录，重启历史变成可读的。
    """
    log.info(
        "常驻启动 pid=%d 分支=%s schema=v%d 库=%s screenpipe=%s 通道=%s "
        "每 %d 分钟一轮/窗口 %d 分钟",
        os.getpid(),
        _current_branch() or "未知",
        SCHEMA_VERSION,
        config.store_path,
        config.screenpipe.base_url,
        "recording(dry-run)" if dry_run else config.notify.channel,
        config.schedule.evaluate_every_minutes,
        config.schedule.window_minutes,
    )


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
    # §4.5 的全屏信号也在这里露一次：它是唯一一处自动推断，自检时要能看见
    # 「这台机器上它现在读到什么」，否则配置写对了也可能整条信号静默失效。
    fullscreen = default_probe().state()
    print(f"全屏信号    {fullscreen}  {describe(fullscreen)}")
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
    """解析 `--views`。**未知编号报错，不静默忽略。**

    这里曾经只认 1–4，而规格 §11 与配置注释都写着 `--views 5`。结果是
    用户敲下 `--views 5` 得到的是一片空白 —— 看起来像「这个视图没有数据」，
    而不是「这个编号不存在」。排查方向从一开始就是歪的。
    """
    text = spec.strip()
    if not text:
        return ()
    if text == "all":
        return render.VIEW_IDS
    views = tuple(part.strip() for part in text.split(",") if part.strip())
    unknown = [view for view in views if view not in render.VIEW_IDS]
    if unknown:
        raise ConfigError(
            f"--views 含未知视图编号 {unknown}；可用：{list(render.VIEW_IDS)} 或 all"
        )
    return views


def _leak_details(config: Config, anchors) -> tuple[LeakWindowDetail, ...]:
    """漏判二级视图的回查半边：逐锚点问 Screenpipe 要窗口明细。

    Screenpipe 本来就是那份数据的 owner，且有留存期 —— 按需去查而不是抄一份
    存起来，同时满足数据最小化与「不重造采集能力」。

    这里只做 IO 与提示语；分类、排序、截断在 `queries.aggregate_leak_details`，
    组文本在 `render._leak_lines`（spec §4.2：入口层不做聚合渲染）。
    明细只打印到 stdout，绝不落库。
    """
    try:
        reader = make_reader(config)
    except ConfigError as exc:
        # 没有锚点也要把这句话说出口：用户正是**因为**看不到明细才加的
        # `--from-screenpipe`，「没锚点所以什么都不打印」会让他以为参数没生效。
        print(f"漏判明细跳过：{exc}", file=sys.stderr)
        return ()
    if not anchors:
        print("漏判明细：本次区间内没有锚点，无需回查 Screenpipe。", file=sys.stderr)
        return ()

    window = config.schedule.window_minutes
    snapshots = tuple(
        reader.read(
            anchor.at - timedelta(minutes=window), anchor.at, window, anchor.at
        )
        for anchor in anchors
    )
    return queries.aggregate_leak_details(
        anchors,
        snapshots,
        taxonomy=config.taxonomy,
        top_n=config.report.leak_top_n,
    )


def run_report(config: Config, args: argparse.Namespace, clock: Clock) -> int:
    """只读汇报。除 migrate 外不构造任何写入语句 —— 而这里连 migrate 都不做。"""
    db_path = Path(config.store_path)
    if not db_path.is_file():
        print(f"库不存在：{db_path}；先跑 --once 或 --daemon 生成数据", file=sys.stderr)
        return 2

    store = Store(db_path)
    try:
        version = store.user_version()
        if version < SCHEMA_VERSION:
            print(
                f"库 schema 为 v{version}，需要 v{SCHEMA_VERSION}；"
                "请先跑一次 --once 完成迁移（--report 不做任何写入）",
                file=sys.stderr,
            )
            return 2

        now = clock.now()
        since = _parse_since(args.since, now)
        # 参数错误先炸，不要等读完库才告诉用户 --views 写错了。
        views = _requested_views(args.views)
        evaluations = store.list_evaluations(since=since)
        run_events = store.list_run_events(since=since)
        interventions = store.list_interventions(since=since)
        outcomes = store.list_outcomes(since=since)

        gates = queries.build_gate_breakdown(evaluations)
        data = ReportData(
            overview=queries.build_overview(
                evaluations,
                run_events,
                evaluate_every_minutes=config.schedule.evaluate_every_minutes,
                gap_threshold_minutes=config.report.gap_threshold_minutes,
            ),
            liveness=queries.build_liveness(
                evaluations,
                run_events,
                now,
                gap_threshold_minutes=config.report.gap_threshold_minutes,
            ),
            verdicts=queries.build_verdict_breakdown(evaluations),
            gates=gates,
            # 闸门统计传进去而不是重算：cooldown / daily_cap 的阻挡次数
            # 只能有一个来源。
            interventions=queries.build_intervention_breakdown(interventions, gates),
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

        views = _requested_views(args.views)
        if args.format == "json":
            # JSON 里所有视图的字段都在，`--views` 无从省略任何东西。
            # 与其静默忽略它，不如说清楚 —— JSON 消费方按字段取用即可。
            if views:
                print(
                    "提示：--format json 输出全部视图字段，--views 对它无效。",
                    file=sys.stderr,
                )
            print(render.render_json(data))
        else:
            print(render.render_text(data, views=views))
        return 0
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    finally:
        store.close()


def run_replay(name: str, config: Config, *, keep_db: bool) -> int:
    """在独立的库上跑剧本。绝不触碰配置指向的生产库。

    默认写临时库并在跑完后删除（spec §11）——「保留」是显式动作，
    因此给了 `--keep-db`。保留路径是 `<store 所在目录>/replay/<剧本>/`，
    可以用 `--report --db <那里的库>` 继续读。
    """
    if name != "all" and name not in SCENARIOS:
        available = ", ".join(sorted(SCENARIOS))
        print(f"未知剧本 {name!r}；可用：{available} 或 all", file=sys.stderr)
        return 2

    names = list(SCENARIOS) if name == "all" else [name]
    failed = False
    for scenario_name in names:
        failures = check_scenario(scenario_name, config, keep_db=keep_db)
        print(f"{'FAIL' if failures else 'PASS'}  {scenario_name}")
        for reason in failures:
            print(f"      {reason}")
        failed = failed or bool(failures)
    if keep_db and name == "all":
        root = Path(config.store_path).parent / "replay"
        print(f"库文件保留在：{root}")
    return 1 if failed else 0


def _pin_utf8_output() -> None:
    """把 stdout/stderr 钉成 UTF-8。渲染层有 GBK 编码不了的字符（⚠），
    中文 Windows 控制台上 `--report` 会死在中途 —— 无论 launcher 有没有设
    PYTHONIOENCODING。日志设置不该反过来杀死进程，所以失败一律咽下。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _pin_utf8_output()
    args = build_parser().parse_args(argv)
    # 常驻模式的日志走 **stdout**，其余模式走 stderr。两条理由：
    #   · daemon 的日志就是它的输出，落在 daemon.out.log 里名正言顺；落在
    #     .err.log 里会让人以为「err 是空的就没事」，而它其实是唯一的日志。
    #   · 其余模式必须在 stderr 上：`--report --format json` 的 stdout 是给 jq 的，
    #     往里混一行日志就是一份非法 JSON。
    # 于是 stderr 在常驻模式下变成纯粹的「意外通道」—— Python 层的 traceback
    # 会落在那儿。它空着，就等于「没有未捕获的异常」。
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout if args.daemon else sys.stderr,
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
            return run_replay(args.replay, config, keep_db=args.keep_db)
        scheduler = build_scheduler(config, make_notifier(config, args.dry_run, clock), clock)
        if args.once:
            report = scheduler.run_once()
            print(
                f"state={report.state} intervened={report.intervened} "
                f"outcomes_closed={report.outcomes_closed} note={report.note}"
            )
            return 0
        log_startup(config, dry_run=args.dry_run)
        scheduler.run_forever()
        return 0
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
