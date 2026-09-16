from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from statesense.__main__ import _parse_since, _requested_views, build_parser, main

REPO = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)


def _config_file(tmp_path, db_name="statesense.db") -> Path:
    src = (REPO / "config" / "config.example.toml").read_text(encoding="utf-8")
    db_path = (tmp_path / db_name).as_posix()
    src = src.replace('path = "statesense.db"', f'path = "{db_path}"')
    target = tmp_path / "config.toml"
    target.write_text(src, encoding="utf-8")
    return target


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
    from statesense.config import ConfigError

    with pytest.raises(ConfigError, match="无法解析"):
        _parse_since("上周", T0)


def test_requested_views_expands_all():
    assert _requested_views("all") == ("1", "2", "3", "4")
    assert _requested_views("") == ()
    assert _requested_views("1, 3") == ("1", "3")


# ── 端到端 ──────────────────────────────────────────────────

def test_missing_database_returns_2_with_actionable_message(tmp_path, capsys):
    cfg = _config_file(tmp_path, db_name="nope.db")
    assert main(["--report", "--config", str(cfg)]) == 2
    assert "库不存在" in capsys.readouterr().err


def _empty_migrated_db(tmp_path) -> Path:
    """空库 = 文件存在、schema 最新、零行。与「库不存在」是两回事。"""
    from statesense.store.db import Store

    store = Store(tmp_path / "statesense.db")
    store.migrate()
    store.close()
    return tmp_path / "statesense.db"


def test_empty_database_reports_no_records_and_exits_0(tmp_path, capsys):
    """空库不是错误 —— 「进程从未跑起来」本身就是观测结论。"""
    _empty_migrated_db(tmp_path)
    cfg = _config_file(tmp_path)
    assert main(["--report", "--config", str(cfg)]) == 0
    assert "无任何评估记录" in capsys.readouterr().out


def test_report_json_format_works_on_empty_database(tmp_path, capsys):
    import json

    _empty_migrated_db(tmp_path)
    cfg = _config_file(tmp_path)
    assert main(["--report", "--config", str(cfg), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["overview"]["evaluations"] == 0


def test_outdated_schema_is_refused_without_writing(tmp_path, capsys):
    """--report 绝不写库，所以它不能顺手迁移 —— 只能提示先跑 --once。"""
    import sqlite3

    sqlite3.connect(tmp_path / "statesense.db").executescript("PRAGMA user_version = 3;")
    cfg = _config_file(tmp_path)
    assert main(["--report", "--config", str(cfg)]) == 2
    assert "先跑一次 --once" in capsys.readouterr().err
