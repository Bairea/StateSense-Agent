from statesense.activity.base import ActivitySource
from statesense.activity.reader import ActivityReader


def test_activity_reader_satisfies_activity_source():
    """reader 是系统里唯一没有抽象的外部依赖缝隙，回放器要靠它注入脚本数据源。"""
    reader = ActivityReader("http://localhost:3030", "k", 10.0, lambda *a: (200, b"{}"))
    assert isinstance(reader, ActivitySource)
