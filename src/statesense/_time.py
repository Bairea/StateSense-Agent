"""时间字符串的唯一实现。

reader 与 store 各写一份 _iso 是审查发现的重复代码；两份一旦漂移，
查询参数与落库格式就会不一致。
"""

from __future__ import annotations

from datetime import datetime


def iso(moment: datetime, *, seconds_only: bool = False) -> str:
    """统一转本地时区后序列化。

    seconds_only=True 用于查询参数（屏掉微秒，避免 URL 里出现无意义的精度）；
    落库用默认值（保留微秒，便于按字典序排序与精确比较）。
    """
    local = moment.astimezone(tz=None)
    return local.isoformat(timespec="seconds") if seconds_only else local.isoformat()


def parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
