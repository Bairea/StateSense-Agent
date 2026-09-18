"""测试共用脚手架。

`write_config` 本来在近十个测试文件里各抄了一份（读 `config.example.toml`、
把 `store.path` 换成本次 `tmp_path`、写回一个临时配置文件）。抄到第七份的时候，
任何一次对示例配置的改动都要改七处 —— 而且很容易漏掉一处，让某个测试
静默地继续跑在旧配置上这种「看起来在测新东西」的假象。
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest

from statesense.config import Config, load_config

REPO = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = REPO / "config" / "config.example.toml"

#: 示例配置里 store.path 的原文。替换它才能把库指到 tmp_path 下，
#: 否则测试会往仓库目录里写库文件。
STORE_PATH_LINE = 'path = "statesense.db"'


def write_config(
    tmp_path: Path,
    *,
    db_name: str = "statesense.db",
    replace: Iterable[tuple[str, str]] = (),
) -> Path:
    """把示例配置写成一份指向 `tmp_path` 的临时配置，返回它的路径。

    `replace` 是若干组「原文 → 新文」的字符串替换，供个别测试改某个阈值。
    """
    src = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    if STORE_PATH_LINE not in src:
        raise AssertionError(
            f"示例配置里找不到 {STORE_PATH_LINE!r}；"
            "它被改过的话，所有测试都会写到仓库目录里去"
        )
    src = src.replace(STORE_PATH_LINE, f'path = "{(tmp_path / db_name).as_posix()}"')
    for old, new in replace:
        if old not in src:
            raise AssertionError(f"要替换的片段在示例配置里不存在：{old!r}")
        src = src.replace(old, new)
    target = tmp_path / "config.toml"
    target.write_text(src, encoding="utf-8")
    return target


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    """示例配置的副本，库指向本次 tmp_path。"""
    return load_config(write_config(tmp_path))


@pytest.fixture()
def make_config(tmp_path: Path):
    """需要自定义库名或改某个阈值的测试用这个。

    不要直接 `from conftest import write_config` —— 那要求测试目录在
    `sys.path` 上，是 pytest 导入模式的副作用，不是可以依赖的契约。
    """

    def _make(**kwargs) -> Path:
        return write_config(tmp_path, **kwargs)

    return _make
