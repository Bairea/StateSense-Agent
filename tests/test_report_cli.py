from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from statesense.__main__ import _parse_since, _requested_views, build_parser, main
from statesense.config import ConfigError
from statesense.report.render import LEAK_VIEW, VIEW_IDS

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
    """spec §11 写的是 `all|0,1,2,3,4,5`，一个都不能少。"""
    assert VIEW_IDS == ("0", "1", "2", "3", "4", "5")
    assert LEAK_VIEW in VIEW_IDS


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


# ── 端到端 ──────────────────────────────────────────────────

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
        'base_url = "http://localhost:3030"', 'base_uri = "http://localhost:3030"'
    )
    cfg.write_text(text, encoding="utf-8")
    assert main(["--report", "--config", str(cfg)]) == 2
    err = capsys.readouterr().err
    assert "配置错误" in err
    assert "[screenpipe]" in err
    assert "Traceback" not in err
