"""异常消息的收敛写法。

**只此一处。** 运行事件 (`tick_error`) 与影子调用失败 (`shadow_error`) 都要把
异常压成一行可入库的文本，各写一份的话，两边的截断长度与「取首行还是取全篇」
迟早会漂移 —— 而这类字段的用途正是并排比较，口径不一致就等于没法比。

取**首行**而不是整条消息：包装过的异常，消息里往往重复堆叠同一条信息，
换行还会把 report 缺口视图里「一行一条运行事件」的版式冲掉。
"""

from __future__ import annotations

#: 入库文本的上限。够长到能看清原因，短到不会把一行日志撑爆。
DETAIL_LIMIT = 200


def clip(text: str, *, limit: int = DETAIL_LIMIT) -> str:
    """把一段外部文本压到可入库的长度。

    只按长度截断，不做转义或过滤：这些列**只写不读**（没有代码路径把它们
    当指令或当判定输入），所以这里的职责只是别让一行被撑爆。
    """
    text = text.strip()
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def error_detail(exc: BaseException, *, limit: int = DETAIL_LIMIT) -> str:
    """异常类名 + 消息首行，截断至 `limit` 字符。"""
    message = str(exc).strip()
    first_line = message.splitlines()[0] if message else ""
    return clip(f"{type(exc).__name__}: {first_line}", limit=limit)
