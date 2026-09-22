from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from statesense.__main__ import (
    _REPORT_ONLY,
    _parse_since,
    _requested_views,
    build_parser,
    main,
)
from statesense.config import ConfigError
from statesense.report.render import COHORT_VIEW, LEAK_VIEW, VIEW_IDS

T0 = datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)


# ── 参数解析 ────────────────────────────────────────────────

def test_report_flag_parses_with_documented_defaults():
    args = build_parser().parse_args(["--report"])
    assert args.report is True
    assert args.since == "7d"
    assert args.format == "text"


def test_report_accepts_views_trace_and_screenpipe():
    args = build_parser().parse_args(
        [
            "--report",
            "--since", "3d",
            "--format", "json",
            "--views", "1,2",
            "--trace", "2026-09-16T04:00:00",
            "--from-screenpipe",
        ]
    )
    assert args.since == "3d"
    assert args.format == "json"
    assert args.views == "1,2"
    assert args.trace == "2026-09-16T04:00:00"
    assert args.from_screenpipe is True


def test_report_is_mutually_exclusive_with_once():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--report", "--once"])


# ── 区间解析 ────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("7d", T0 - timedelta(days=7)),
        ("24h", T0 - timedelta(hours=24)),
        ("1d", T0 - timedelta(days=1)),
    ],
)
def test_parse_since_relative_forms(text, expected):
    assert _parse_since(text, T0) == expected


def test_parse_since_accepts_iso_moment():
    assert _parse_since("2026-09-01T00:00:00+00:00", T0) == datetime(
        2026, 9, 1, tzinfo=timezone.utc
    )


def test_parse_since_rejects_garbage():
    with pytest.raises(ConfigError, match="无法解析"):
        _parse_since("上周", T0)


# ── 视图编号 ────────────────────────────────────────────────

def test_requested_views_expands_all():
    assert _requested_views("all") == VIEW_IDS
    assert _requested_views("") == ()
    assert _requested_views("1, 3") == ("1", "3")


def test_view_ids_cover_the_specs_documented_set():
    """spec §11 的 0–5 一个都不能少；阶段 2 起在其后追加新视图。

    只锁前缀而不是整串：新增视图是常态，但**改动既有编号**会让所有历史
    调用与文档一起失效，那才是要挡住的事。
    """
    assert VIEW_IDS[:6] == ("0", "1", "2", "3", "4", "5")
    assert LEAK_VIEW in VIEW_IDS
    assert COHORT_VIEW in VIEW_IDS


def test_unknown_view_id_is_rejected_not_ignored():
    """曾经 `--views 5` 被静默丢掉，用户看到的是空白，而不是「参数写错了」。

    一个被忽略的参数比一个报错的参数危险得多：它让人往「这个视图没有数据」
    而不是「这个编号不存在」的方向排查。
    """
    with pytest.raises(ConfigError, match="未知视图编号"):
        _requested_views("1,9")
    with pytest.raises(ConfigError, match="未知视图编号"):
        _requested_views("abc")


def test_unknown_view_id_exits_2_at_the_cli(make_config, tmp_path, capsys):
    _empty_migrated_db(tmp_path)
    assert main(["--report", "--config", str(make_config()), "--views", "9"]) == 2
    assert "未知视图编号" in capsys.readouterr().err


# ── §11 help 标注与输出编码 ─────────────────────────────────

@pytest.mark.parametrize("flag", ["--since", "--format", "--views", "--trace", "--from-screenpipe"])
def test_report_only_flags_are_marked_in_help(flag):
    """五个「仅 --report 有效」的旗标必须在 --help 里就自报身份，
    否则用户会在 --once 上敲 --views 并期待生效。"""
    actions = {a.option_strings[0]: a for a in build_parser()._actions if a.option_strings}
    assert _REPORT_ONLY in actions[flag].help


def test_report_survives_a_gbk_only_stdout(make_config, tmp_path):
    """中文 Windows 控制台上 stdout 只能写 GBK，而视图 2 的告警行带 ⚠。

    曾经这不是乱码而是**崩溃**：UnicodeEncodeError 死在半路，一份只读命令
    连自己的输出编码都保不住，谈不上可观测。
    """
    from statesense.intervention.models import Decision, GateResult
    from statesense.state.models import State, StateVerdict
    from statesense.store.db import Store

    db = tmp_path / "statesense.db"
    store = Store(db)
    store.migrate()

    verdict = StateVerdict(
        state=State.PASSIVE_CONSUMPTION, late_night=False,
        total_active_minutes=60.0, ent_minutes=45.0, gray_minutes=0.0,
        work_minutes=0.0, ent_ratio=0.75, entries_minutes=60.0,
        fullscreen_state=None, window_minutes=60, data_status="ok",
        skipped=False, skip_reason=None,
    )
    decision = Decision(
        intervene=False, action_id=None, reason="t",
        gate_trace=(GateResult("ratio_min", False, 0.7, 0.75),),
    )
    store.insert_evaluation(T0, verdict, decision, rule_version="t")
    with store._conn:
        store._conn.execute(
            "UPDATE evaluations SET gate_trace = 'not json' WHERE at = ?",
            (store.list_evaluations()[0]["at"],),
        )
    store.close()

    out = tmp_path / "gbk_stdout.txt"
    with open(out, "w", encoding="gbk") as fake_stdout, \
            redirect_stdout(fake_stdout):
        assert main([
            "--report", "--config", str(make_config()), "--views", "2"
        ]) == 0
    text = out.read_text(encoding="utf-8")
    assert "闸门数据损坏 1 轮" in text


# ── --views 5 --from-screenpipe 的输出金样例 ────────────────
#
# B 项重构（`_leak_details` 下沉 report 层）前先给现状拍照：
# 输出文本一个字都不许变，重构只许动代码位置。

def _leak_anchor_row(store, at):
    """总活跃 60、三类归类全 0、明细 60 —— 未归类比例 1.0，必成锚点。"""
    from statesense.intervention.models import Decision
    from statesense.state.models import State, StateVerdict

    verdict = StateVerdict(
        state=State.NORMAL, late_night=False,
        total_active_minutes=60.0, ent_minutes=0.0, gray_minutes=0.0,
        work_minutes=0.0, ent_ratio=0.0, entries_minutes=60.0,
        fullscreen_state=None, window_minutes=60, data_status="ok",
        skipped=False, skip_reason=None,
    )
    store.insert_evaluation(
        at, verdict,
        Decision(intervene=False, action_id=None, reason="t", gate_trace=()),
        rule_version="t",
    )


def _snapshot(end, *, status="ok", entries=()):
    from statesense.activity.models import ActivitySnapshot

    return ActivitySnapshot(
        window_start=end - timedelta(minutes=60), window_end=end,
        window_minutes=60, total_active_minutes=60.0,
        entries=entries, data_status=status, captured_at=end,
    )


def test_views5_from_screenpipe_output_is_golden(make_config, tmp_path, capsys, monkeypatch):
    from statesense import __main__ as cli
    from statesense.activity.models import Entry
    from statesense.store.db import Store

    moments = [T0, T0 + timedelta(minutes=5), T0 + timedelta(minutes=10)]
    store = Store(tmp_path / "statesense.db")
    store.migrate()
    for moment in moments:
        _leak_anchor_row(store, moment)
    store.close()

    by_end = {
        moments[0]: _snapshot(moments[0], entries=(
            Entry(app="神秘软件", title="神秘窗口", url="", minutes=12.0),
            Entry(app="另一个", title="另一个窗口", url="", minutes=35.5),
            Entry(app="cursor", title="写代码", url="", minutes=10.0),  # WORK：不进明细
            Entry(app="还有", title="还有它", url="", minutes=3.0),
            Entry(app="更小的", title="更小窗口", url="", minutes=1.0),
        )),
        moments[1]: _snapshot(moments[1], status="unreachable"),
        moments[2]: _snapshot(moments[2], entries=(
            Entry(app="terminal", title="powershell", url="", minutes=50.0),
        )),
    }

    class StubReader:
        def read(self, start, end, window_minutes, captured_at):
            return by_end[end]

    monkeypatch.setenv("SCREENPIPE_LOCAL_API_KEY", "test-key")
    monkeypatch.setattr(cli, "make_reader", lambda config: StubReader())

    assert main([
        "--report", "--config", str(make_config()),
        "--views", "5", "--from-screenpipe",
    ]) == 0
    out = capsys.readouterr().out

    # 锚点行本身（视图 5 既有产物），以及三窗口的明细各按各的分支输出。
    # top_n=10 不截断这里；排序必须按分钟降序；WORK 条目不得出现。
    lines = out.splitlines()
    detail = [line for line in lines if "分钟 " in line]
    for line in detail[:4]:
        # 渲染走 astimezone()，断言也要落到本地时区再比。
        assert line.startswith(f"    {moments[0].astimezone()}  ")
    assert " 35.5 分钟  另一个窗口" in detail[0]
    assert " 12.0 分钟  神秘窗口" in detail[1]
    assert "  3.0 分钟  还有它" in detail[2]
    assert "  1.0 分钟  更小窗口" in detail[3]
    assert not any("写代码" in line for line in lines)
    assert f"    {moments[1].astimezone()}  明细不可用（data_status=unreachable）" in lines
    assert any("未命中条目为空（更可能是明细缺失，不是漏判）" in line for line in lines)
    assert "窗口明细（仅打印，不落库）：" in out


def test_views5_without_from_screenpipe_has_no_detail_section(make_config, tmp_path, capsys):
    """不加 --from-screenpipe 就不该有任何回查痕迹。"""
    from statesense.store.db import Store

    store = Store(tmp_path / "statesense.db")
    store.migrate()
    _leak_anchor_row(store, T0)
    store.close()
    assert main(["--report", "--config", str(make_config()), "--views", "5"]) == 0
    out = capsys.readouterr().out
    assert "窗口明细" not in out
    assert "神秘" not in out

def test_missing_database_returns_2_with_actionable_message(make_config, capsys):
    assert main(["--report", "--config", str(make_config(db_name="nope.db"))]) == 2
    assert "库不存在" in capsys.readouterr().err


def _empty_migrated_db(tmp_path) -> None:
    """空库 = 文件存在、schema 最新、零行。与「库不存在」是两回事。

    库路径必须与 `make_config()` 默认的库名一致（statesense.db）——
    两者对不上时读到的是「库不存在」，测试会以另一个理由通过。
    """
    from statesense.store.db import Store

    store = Store(tmp_path / "statesense.db")
    store.migrate()
    store.close()


def test_empty_database_reports_no_records_and_exits_0(make_config, tmp_path, capsys):
    """空库不是错误 —— 「进程从未跑起来」本身就是观测结论。"""
    _empty_migrated_db(tmp_path)
    assert main(["--report", "--config", str(make_config())]) == 0
    assert "无任何评估记录" in capsys.readouterr().out


def test_report_json_format_works_on_empty_database(make_config, tmp_path, capsys):
    import json

    _empty_migrated_db(tmp_path)
    assert main(["--report", "--config", str(make_config()), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["overview"]["evaluations"] == 0


def test_outdated_schema_is_refused_without_writing(make_config, tmp_path, capsys):
    """--report 绝不写库，所以它不能顺手迁移 —— 只能提示先跑 --once。"""
    import sqlite3

    db = tmp_path / "statesense.db"
    sqlite3.connect(db).executescript("PRAGMA user_version = 3;")
    assert main(["--report", "--config", str(make_config())]) == 2
    assert "先跑一次 --once" in capsys.readouterr().err
    # 拒绝之后 schema 版本必须原封不动 —— 一个只读命令不该顺手动库。
    assert sqlite3.connect(db).execute("PRAGMA user_version").fetchone()[0] == 3


def test_malformed_config_exits_2_without_a_traceback(make_config, capsys):
    """配置语法错误必须是干净的「配置错误：…」+ 退出码 2，而不是 traceback + 1。"""
    cfg = make_config()
    cfg.write_text(cfg.read_text(encoding="utf-8") + "\nthis is not toml\n", encoding="utf-8")
    assert main(["--report", "--config", str(cfg)]) == 2
    err = capsys.readouterr().err
    assert "配置错误" in err
    assert "Traceback" not in err


def test_unknown_config_key_exits_2_without_a_traceback(make_config, capsys):
    """键名拼错也必须被翻译成 ConfigError。

    否则构造 dataclass 时抛的是 TypeError —— main 接不住，用户看到 traceback
    加退出码 1，与「配置非法」应有的干净报错完全不同。这与 BOM 那次是同一类缺陷。
    """
    cfg = make_config()
    text = cfg.read_text(encoding="utf-8").replace(
        'base_url = "http://localhost:3131"', 'base_uri = "http://localhost:3131"'
    )
    cfg.write_text(text, encoding="utf-8")
    assert main(["--report", "--config", str(cfg)]) == 2
    err = capsys.readouterr().err
    assert "配置错误" in err
    assert "[screenpipe]" in err
    assert "Traceback" not in err


# ── 常驻启动行：让重启可见 ──────────────────────────────────

def test_current_branch_reads_a_normal_git_dir(tmp_path):
    from statesense.__main__ import _current_branch

    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text(
        "ref: refs/heads/feat/cool\n", encoding="utf-8"
    )
    assert _current_branch(tmp_path / "src") == "feat/cool"


def test_current_branch_reads_a_worktree_git_file(tmp_path):
    """worktree 里 `.git` 是个文件（`gitdir: …`），不是目录。"""
    from statesense.__main__ import _current_branch

    real = tmp_path / "real-gitdir"
    real.mkdir()
    (real / "HEAD").write_text("ref: refs/heads/wt\n", encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {real}\n", encoding="utf-8")
    assert _current_branch(wt) == "wt"


def test_current_branch_reports_a_detached_head(tmp_path):
    from statesense.__main__ import _current_branch

    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("a" * 40 + "\n", encoding="utf-8")
    assert _current_branch(tmp_path) == "detached@" + "a" * 12


def test_current_branch_is_none_outside_a_repository(tmp_path):
    """读不到就返回 None —— 一行日志不能因为读不到分支就让 daemon 起不来。"""
    from statesense.__main__ import _current_branch

    assert _current_branch(tmp_path) is None


def test_log_startup_records_what_is_running(config, caplog):
    """启动行存在的唯一理由是**让重启可见**。

    daemon 在健康的一轮里什么都不打印（记录落在库里），所以一份空日志既可能是
    「一切正常」，也可能是「刚被重启过」—— 这两件事必须能分开。
    """
    import logging
    import os

    from statesense.__main__ import log_startup

    with caplog.at_level(logging.INFO):
        log_startup(config, dry_run=True)

    text = caplog.text
    assert "常驻启动" in text
    assert f"pid={os.getpid()}" in text
    assert "分支=" in text          # 曾经从错误分支起过 daemon，分支必须可见
    assert "schema=v7" in text
    assert "规则版本=" in text  # 判定规则换了没有，启动行要能看见
    assert "recording(dry-run)" in text
    assert config.screenpipe.base_url in text


def test_log_startup_reports_the_real_channel_when_not_dry_run(config, caplog):
    """不 dry-run 时必须报出真实通道 —— 这正是区分排练与真事的那一项。"""
    import logging

    from statesense.__main__ import log_startup

    with caplog.at_level(logging.INFO):
        log_startup(config, dry_run=False)

    assert "foreground_popup" in caplog.text
    assert "recording(dry-run)" not in caplog.text


def test_main_writes_the_startup_line_before_entering_the_loop(
    make_config, monkeypatch, caplog
):
    """接线测试：启动行必须在进入死循环**之前**写出来。

    只让 run_forever 打日志是不够的 —— 那样「进程起来了」与「第一轮评估」
    之间仍有一段什么都没有的窗口，而重启恰好就发生在那里。
    """
    import logging

    from statesense import __main__ as cli
    from statesense.scheduler import Scheduler

    monkeypatch.setenv("SCREENPIPE_LOCAL_API_KEY", "test-key")
    monkeypatch.setattr(Scheduler, "run_forever", lambda self: None)

    with caplog.at_level(logging.INFO, logger="statesense.cli"):
        assert cli.main(["--daemon", "--dry-run", "--config", str(make_config())]) == 0

    assert "常驻启动" in caplog.text
