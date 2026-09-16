"""数据源契约。

reader 是系统里唯一可替换的外部依赖缝隙：Clock 与 Notifier 都已是 Protocol，
只有它一直挂着具体类。回放器要注入脚本数据源，这道缝隙就必须在类型上成立。

注意：本仓库没有配置类型检查器，因此这个 Protocol 的价值不在静态检查，
而在于把「可替换」这件事写进代码，让回放器有据可依。
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from statesense.activity.models import ActivitySnapshot


@runtime_checkable
class ActivitySource(Protocol):
    def read(
        self,
        start: datetime,
        end: datetime,
        window_minutes: int,
        captured_at: datetime,
    ) -> ActivitySnapshot: ...
