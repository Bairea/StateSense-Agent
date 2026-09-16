# StateSense-Agent V0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 V0 状态介入系统 —— 每 5 分钟读取 Screenpipe 活动、判定是否处于被动消费状态、通过闸门时投递 Windows Toast 轻推，并在 10 分钟后自动复查干预效果。

**Architecture:** 六组件分层。`ActivityReader` 是唯一接触 Screenpipe 的组件；`StateEngine` / `InterventionDecider` / `OutcomeTracker` 是纯函数，时间与配置全部注入，因此可脱离真实录制做表驱动测试；`Notifier` / `Wording` 是接口，V0 给出 Windows Toast 与模板实现。

**Tech Stack:** Python 3.12、uv（依赖管理）、标准库 `tomllib` / `sqlite3` / `urllib`、`winotify`（仅 Windows）、`pytest`（仅开发依赖）。

**Spec:** `docs/specs/2026-09-16-v0-state-intervention-design.md`（已合并进 `main`）

## 修订记录

| 日期 | 改动 | 影响的任务 |
|---|---|---|
| 2026-09-16 | **投递通道从 Windows Toast 改为原生前台弹窗**（真机实测 Toast 全链路失效），并因此新增按钮回执 | Task 1（`NotifyConfig`）、Task 5（schema v2 增加 `user_response`）、Task 8（整节重写，见「Task 8 修订版」）、Task 10（`insert_intervention` 要传 `user_response`） |
| 2026-09-16 | Task 1 的提交额外纳入 `uv.lock`（依赖可复现） | Task 1 |
| 2026-09-16 | Task 2 的 `data_status` 保持封闭枚举 `unreachable`，失败原因改走日志（原计划写的是 `unreachable: <原因>`，与测试断言矛盾，以 spec §5.1 为准） | Task 2 |
| 2026-09-16 | Task 10 的 `check()` 存在缺陷：原计划把 `SystemClock().now()` 同时当作 start 与 end，会读一个零长度窗口 | Task 10 |

## Global Constraints

每个任务都隐含包含本节。

- Python 下限 `>=3.12`（使用 `tomllib` 与 `X | None`）。
- 运行时第三方依赖只允许 `winotify`，且仅 `sys_platform == 'win32'`。HTTP、SQLite、TOML、时间全部标准库。
- **代码中不得出现盘符或用户目录硬编码**（不得出现 `D:\`、`C:\Users`、`~/.screenpipe` 字面量）。路径一律来自配置或环境变量。
- `gate.ratio_min` 的确定值：**0.75**。缺失时**必须启动失败**，绝不静默降级为 0。
- `ActivityReader` 请求必须带 `Authorization: Bearer <key>`、`X-Screenpipe-Client: api`、`X-Screenpipe-Agent: statesense`，并必须带 `include_key_texts=false`、`include_snippets=false`、`include_memories=false`、`include_guidance=false`。
- `data_status != "ok"` 时**不得产出状态结论**，只能记 `skipped`。
- 判定与闸门保持纯函数：不读系统时钟，时间作参数传入。
- Conventional Commits，scope 用组件名（`config`/`reader`/`state`/`gate`/`store`/`notify`/`outcome`/`scheduler`）。
- 每个任务结束时提交一次，且提交后 `uv run pytest` 全绿。

---

## File Structure

| 文件 | 职责 |
|---|---|
| `pyproject.toml` | 工程与依赖声明 |
| `config/config.example.toml` | 配置模板（含全部默认值） |
| `src/statesense/clock.py` | 时间抽象（`SystemClock` / `FrozenClock`） |
| `src/statesense/config.py` | TOML 加载 + 启动即校验 |
| `src/statesense/activity/models.py` | `Entry` / `ActivitySnapshot` |
| `src/statesense/activity/reader.py` | 唯一接触 Screenpipe 的组件 |
| `src/statesense/state/models.py` | `State` / `StateVerdict` |
| `src/statesense/state/taxonomy.py` | 分类匹配（纯函数） |
| `src/statesense/state/engine.py` | 状态判定（纯函数） |
| `src/statesense/intervention/models.py` | `GateResult` / `Decision` |
| `src/statesense/intervention/gates.py` | 四条闸门条件 |
| `src/statesense/intervention/actions.py` | 动作池 + 轮转选择 |
| `src/statesense/intervention/wording.py` | `Wording` 接口 + 模板实现 |
| `src/statesense/intervention/decider.py` | 组合闸门与动作（纯函数） |
| `src/statesense/notify/base.py` | `Notifier` 协议 + `DeliveryResult` |
| `src/statesense/notify/windows_toast.py` | `winotify` 实现 |
| `src/statesense/outcome/models.py` | `OutcomeVerdict` |
| `src/statesense/outcome/tracker.py` | 回执判定（纯函数） |
| `src/statesense/store/schema.sql` | 建表语句 |
| `src/statesense/store/db.py` | `Store`（SQLite 读写） |
| `src/statesense/scheduler.py` | tick 主循环 |
| `src/statesense/__main__.py` | 命令行入口 |
| `tests/` | 各组件测试 |

**关于 `window_minutes`：** `StateVerdict` 从 Task 4 起就包含 `window_minutes: int` 字段（由 `snapshot.window_minutes` 传入）。`Store.insert_evaluation` 与 `TemplateWording` 都依赖它，后续任务不再重复说明。

---

## Task 1: 工程骨架、时钟抽象与配置加载

**Files:**
- Create: `pyproject.toml`, `src/statesense/__init__.py`, `src/statesense/clock.py`, `src/statesense/config.py`, `config/config.example.toml`
- Test: `tests/test_clock.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: 无
- Produces: `Clock` 协议；`SystemClock()`；`FrozenClock(instant)` 及 `advance(**delta)`；`ConfigError`；`Config` 及其子配置；`load_config(path: Path) -> Config`

- [ ] **Step 1: 写失败的测试**

`tests/test_clock.py`：

```python
from datetime import datetime, timedelta, timezone

from statesense.clock import FrozenClock, SystemClock


def test_system_clock_returns_aware_utc():
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_frozen_clock_starts_at_given_instant():
    t = datetime(2026, 9, 16, 3, 0, tzinfo=timezone.utc)
    assert FrozenClock(t).now() == t


def test_frozen_clock_advances():
    t = datetime(2026, 9, 16, 3, 0, tzinfo=timezone.utc)
    clock = FrozenClock(t)
    clock.advance(minutes=5)
    assert clock.now() == t + timedelta(minutes=5)


def test_frozen_clock_rejects_naive_datetime():
    import pytest

    with pytest.raises(ValueError, match="时区"):
        FrozenClock(datetime(2026, 9, 16, 3, 0))
```

`tests/test_config.py`：

```python
from pathlib import Path

import pytest

from statesense.config import ConfigError, load_config

REPO = Path(__file__).resolve().parents[1]

MINIMAL = """
[screenpipe]
base_url = "http://localhost:3030"

[gate]
ratio_min = 0.75

[taxonomy]
entertainment = ["bilibili"]

[[actions]]
id = "walk5"
text = "离开电脑走 5 分钟"
applies_to = ["PASSIVE_CONSUMPTION"]
"""


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(body, encoding="utf-8")
    return p


def test_loads_example_config_shipped_with_repo():
    cfg = load_config(REPO / "config" / "config.example.toml")
    assert cfg.screenpipe.base_url == "http://localhost:3030"
    assert cfg.gate.ratio_min == 0.75
    assert cfg.schedule.window_minutes == 60
    assert cfg.taxonomy.entertainment
    assert cfg.actions


def test_missing_ratio_min_fails_fast(tmp_path):
    with pytest.raises(ConfigError, match="ratio_min"):
        load_config(_write(tmp_path, MINIMAL.replace("ratio_min = 0.75\n", "")))


def test_ratio_min_out_of_range_fails(tmp_path):
    with pytest.raises(ConfigError, match="ratio_min"):
        load_config(_write(tmp_path, MINIMAL.replace("0.75", "1.5")))


def test_empty_action_pool_fails(tmp_path):
    with pytest.raises(ConfigError, match="actions"):
        load_config(_write(tmp_path, MINIMAL.split("[[actions]]")[0]))


def test_empty_entertainment_list_fails(tmp_path):
    with pytest.raises(ConfigError, match="entertainment"):
        load_config(_write(tmp_path, MINIMAL.replace('["bilibili"]', "[]")))


def test_invalid_regex_fails(tmp_path):
    with pytest.raises(ConfigError, match="正则"):
        load_config(_write(tmp_path, MINIMAL.replace('["bilibili"]', '["bili("]')))


def test_duplicate_action_id_fails(tmp_path):
    body = MINIMAL + """
[[actions]]
id = "walk5"
text = "重复"
applies_to = ["PASSIVE_CONSUMPTION"]
"""
    with pytest.raises(ConfigError, match="重复"):
        load_config(_write(tmp_path, body))


def test_defaults_are_applied(tmp_path):
    cfg = load_config(_write(tmp_path, MINIMAL))
    assert cfg.schedule.tick_seconds == 60
    assert cfg.schedule.evaluate_every_minutes == 5
    assert cfg.thresholds.passive_minutes == 40
    assert cfg.gate.cooldown_minutes == 30
    assert cfg.gate.daily_cap == 8
    assert cfg.outcome.delay_minutes == 10
    assert cfg.notify.channel == "windows_toast"
    assert cfg.store_path == (tmp_path / "statesense.db").resolve()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_clock.py tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'statesense'`

- [ ] **Step 3: 写最小实现**

`pyproject.toml`：

```toml
[project]
name = "statesense-agent"
version = "0.0.1"
description = "持续感知活动状态、在正确时机轻推一下的个人状态智能体"
requires-python = ">=3.12"
dependencies = ["winotify>=1.1.0; sys_platform == 'win32'"]

[dependency-groups]
dev = ["pytest>=8.0"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/statesense"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
```

`src/statesense/__init__.py`：

```python
"""StateSense-Agent：感知活动状态并在正确时机轻推。"""

__version__ = "0.0.1"
```

`src/statesense/clock.py`：

```python
"""时间抽象。判定逻辑一律通过它取时间，测试中可替换为 FrozenClock。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """返回带时区的 UTC 时间。"""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FrozenClock:
    """测试用：时间完全由测试控制。"""

    def __init__(self, instant: datetime) -> None:
        if instant.tzinfo is None:
            raise ValueError("FrozenClock 需要带时区的时间")
        self._instant = instant

    def now(self) -> datetime:
        return self._instant

    def advance(self, **delta: float) -> None:
        self._instant += timedelta(**delta)
```

`src/statesense/config.py`：

```python
"""配置加载。启动即校验，任何非法项都在进程启动时炸掉，不拖到运行中。"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    """配置非法。"""


@dataclass(frozen=True)
class ScreenpipeConfig:
    base_url: str = "http://localhost:3030"
    api_key_env: str = "SCREENPIPE_LOCAL_API_KEY"
    request_timeout_sec: float = 10.0


@dataclass(frozen=True)
class ScheduleConfig:
    tick_seconds: int = 60
    evaluate_every_minutes: int = 5
    window_minutes: int = 60


@dataclass(frozen=True)
class ThresholdConfig:
    watch_minutes: float = 20
    passive_minutes: float = 40
    high_risk_minutes: float = 65
    late_night_start_hour: int = 1
    late_night_end_hour: int = 6
    late_night_min_active_minutes: float = 10


@dataclass(frozen=True)
class GateConfig:
    enabled: tuple[str, ...] = ("state_min", "ratio_min", "cooldown", "daily_cap")
    ratio_min: float | None = None
    cooldown_minutes: float = 30
    daily_cap: int = 8


@dataclass(frozen=True)
class OutcomeConfig:
    delay_minutes: float = 10
    disengaged_ratio: float = 0.5
    continued_ratio: float = 0.8


@dataclass(frozen=True)
class NotifyConfig:
    channel: str = "windows_toast"
    app_id: str = "StateSense.Agent"
    toast_duration: str = "short"
    fallback_topmost_window: bool = False
    fallback_window_seconds: int = 8


@dataclass(frozen=True)
class TaxonomyConfig:
    entertainment: tuple[re.Pattern[str], ...] = ()
    gray: tuple[re.Pattern[str], ...] = ()
    work: tuple[re.Pattern[str], ...] = ()


@dataclass(frozen=True)
class Action:
    id: str
    text: str
    applies_to: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    screenpipe: ScreenpipeConfig
    schedule: ScheduleConfig
    thresholds: ThresholdConfig
    gate: GateConfig
    outcome: OutcomeConfig
    notify: NotifyConfig
    store_path: Path
    taxonomy: TaxonomyConfig
    actions: tuple[Action, ...] = field(default_factory=tuple)


def _compile(patterns: list[str], where: str) -> tuple[re.Pattern[str], ...]:
    compiled = []
    for raw in patterns:
        try:
            compiled.append(re.compile(raw, re.IGNORECASE))
        except re.error as exc:
            raise ConfigError(f"{where} 里的正则非法: {raw!r} ({exc})") from exc
    return tuple(compiled)


def load_config(path: Path) -> Config:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"配置文件不存在: {path}")

    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    screenpipe = ScreenpipeConfig(**raw.get("screenpipe", {}))

    schedule = ScheduleConfig(**raw.get("schedule", {}))
    if schedule.window_minutes <= 0 or schedule.evaluate_every_minutes <= 0:
        raise ConfigError("schedule.window_minutes 与 evaluate_every_minutes 必须为正数")

    thresholds = ThresholdConfig(**raw.get("thresholds", {}))
    if not (thresholds.watch_minutes <= thresholds.passive_minutes <= thresholds.high_risk_minutes):
        raise ConfigError("thresholds 必须满足 watch <= passive <= high_risk")

    gate_raw = dict(raw.get("gate", {}))
    if "enabled" in gate_raw:
        gate_raw["enabled"] = tuple(gate_raw["enabled"])
    gate = GateConfig(**gate_raw)
    # ratio_min 是必填项。缺失时绝不静默降级为 0 —— 静默失效的闸门比报错危险得多。
    if "ratio_min" in gate.enabled:
        if gate.ratio_min is None:
            raise ConfigError("gate.ratio_min 未设置；该项为必填，不接受默认值")
        if not (0.0 < gate.ratio_min <= 1.0):
            raise ConfigError(f"gate.ratio_min 必须在 (0, 1] 区间内，当前为 {gate.ratio_min}")

    outcome = OutcomeConfig(**raw.get("outcome", {}))
    notify = NotifyConfig(**raw.get("notify", {}))

    store_raw = raw.get("store", {})
    store_path = (path.parent / store_raw.get("path", "statesense.db")).resolve()

    tax_raw = raw.get("taxonomy", {})
    taxonomy = TaxonomyConfig(
        entertainment=_compile(tax_raw.get("entertainment", []), "taxonomy.entertainment"),
        gray=_compile(tax_raw.get("gray", []), "taxonomy.gray"),
        work=_compile(tax_raw.get("work", []), "taxonomy.work"),
    )
    if not taxonomy.entertainment:
        raise ConfigError("taxonomy.entertainment 不能为空，否则无法识别被动消费")

    actions_raw = raw.get("actions", [])
    if not actions_raw:
        raise ConfigError("actions 动作池不能为空")
    actions: list[Action] = []
    seen: set[str] = set()
    for item in actions_raw:
        if not item.get("id") or not item.get("text"):
            raise ConfigError("每个 action 必须有 id 与 text")
        if item["id"] in seen:
            raise ConfigError(f"action id 重复: {item['id']}")
        seen.add(item["id"])
        actions.append(
            Action(id=item["id"], text=item["text"], applies_to=tuple(item.get("applies_to", ())))
        )

    return Config(
        screenpipe=screenpipe,
        schedule=schedule,
        thresholds=thresholds,
        gate=gate,
        outcome=outcome,
        notify=notify,
        store_path=store_path,
        taxonomy=taxonomy,
        actions=tuple(actions),
    )
```

`config/config.example.toml`：

```toml
# StateSense-Agent V0 配置模板
# 复制为 config/config.toml 后按需修改。密钥不放这里，走环境变量。

[screenpipe]
base_url = "http://localhost:3030"
api_key_env = "SCREENPIPE_LOCAL_API_KEY"
request_timeout_sec = 10

[schedule]
tick_seconds = 60
evaluate_every_minutes = 5
window_minutes = 60

[thresholds]
watch_minutes = 20
passive_minutes = 40
high_risk_minutes = 65
late_night_start_hour = 1
late_night_end_hour = 6
late_night_min_active_minutes = 10

[gate]
enabled = ["state_min", "ratio_min", "cooldown", "daily_cap"]
# ratio_min 必填：娱乐分钟 / 总活跃分钟。
# 60 分钟窗口内 ent>=40 已隐含 ratio>=0.667，故有效区间是 (0.667, 1]。
# 0.75 约等于把生效门槛抬到「活跃满 60 分钟时娱乐 >= 45 分钟」。
ratio_min = 0.75
cooldown_minutes = 30
daily_cap = 8

[outcome]
delay_minutes = 10
disengaged_ratio = 0.5
continued_ratio = 0.8

[notify]
channel = "windows_toast"
app_id = "StateSense.Agent"
toast_duration = "short"
fallback_topmost_window = false
fallback_window_seconds = 8

[store]
path = "statesense.db"

[taxonomy]
entertainment = [
  "bilibili", "哔哩哔哩", "B站", "douyin", "抖音", "tiktok", "youtube",
  "iqiyi", "爱奇艺", "youku", "优酷", "v\\.qq\\.com", "腾讯视频", "mgtv", "芒果TV",
  "netflix", "disney\\+", "hbo", "twitch", "kuaishou", "快手",
  "xiaohongshu", "小红书", "weibo", "微博",
  "steam", "epic games", "battle\\.net", "wegame", "明日方舟",
  "网易云音乐", "qq音乐", "spotify", "酷狗", "酷我",
]
gray = ["知乎", "zhihu", "豆瓣", "douban", "贴吧", "tieba", "reddit", "news", "新闻"]
work = [
  "github", "gitlab", "stackoverflow", "docs\\.python", "readthedocs", "developer\\.mozilla",
  "\\bcode\\b", "zed", "pycharm", "cursor", "trae", "idea", "terminal", "powershell",
  "bash", "cmd\\.exe", "figma", "blender", "unity", "godot",
]

[[actions]]
id = "walk5"
text = "离开电脑走 5 分钟"
applies_to = ["PASSIVE_CONSUMPTION", "HIGH_RISK_PASSIVE_CONSUMPTION"]

[[actions]]
id = "calligraphy"
text = "练字 5 分钟"
applies_to = ["PASSIVE_CONSUMPTION", "HIGH_RISK_PASSIVE_CONSUMPTION"]

[[actions]]
id = "stretch"
text = "起来做一组拉伸"
applies_to = ["PASSIVE_CONSUMPTION", "HIGH_RISK_PASSIVE_CONSUMPTION"]

[[actions]]
id = "reading"
text = "读一段《道德经》"
applies_to = ["HIGH_RISK_PASSIVE_CONSUMPTION"]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_clock.py tests/test_config.py -v`
Expected: PASS，11 passed

- [ ] **Step 5: 提交**

```bash
git add pyproject.toml src/statesense/__init__.py src/statesense/clock.py src/statesense/config.py config/config.example.toml tests/test_clock.py tests/test_config.py
git commit -m "feat(config): 工程骨架、时钟抽象与配置加载（ratio_min 缺失即失败）"
```

---

## Task 2: ActivityReader（唯一接触 Screenpipe 的组件）

**Files:**
- Create: `src/statesense/activity/__init__.py`, `src/statesense/activity/models.py`, `src/statesense/activity/reader.py`
- Test: `tests/test_activity_reader.py`

**Interfaces:**
- Consumes: 无（纯输入参数）
- Produces:
  - `Entry(app: str, title: str, url: str, minutes: float)`
  - `ActivitySnapshot(window_start, window_end, window_minutes, total_active_minutes, entries, data_status, captured_at)`，属性 `is_trustworthy -> bool`
  - `HttpGet = Callable[[str, Mapping[str, str], float], tuple[int, bytes]]`
  - `urllib_get(url, headers, timeout) -> tuple[int, bytes]`
  - `ActivityReader(base_url, api_key, timeout, http_get=urllib_get)`，方法 `read(start, end, window_minutes, captured_at) -> ActivitySnapshot`

- [ ] **Step 1: 写失败的测试**

`tests/test_activity_reader.py`：

```python
import json
from datetime import datetime, timedelta, timezone

from statesense.activity.reader import ActivityReader

T0 = datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)

OK_BODY = {
    "total_active_minutes": 47.5,
    "data_status": "ok",
    "apps": [{"name": "chrome.exe", "minutes": 40.0}],
    "windows": [
        {
            "app_name": "chrome.exe",
            "window_name": "【某视频】_哔哩哔哩_bilibili",
            "browser_url": "https://www.bilibili.com/video/BV1xx",
            "minutes": 40.0,
        },
        {
            "app_name": "Code.exe",
            "window_name": "engine.py - Visual Studio Code",
            "browser_url": "",
            "minutes": 7.5,
        },
    ],
    "key_texts": [{"text": "本行绝不应该被读进系统"}],
    "snippets": [{"text": "同样不应该"}],
}


def _reader(body: dict, status: int = 200, captured: list | None = None) -> ActivityReader:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")

    def fake_get(url, headers, timeout):
        if captured is not None:
            captured.append((url, headers, timeout))
        return status, payload

    return ActivityReader("http://localhost:3030", "sp-test", 10.0, fake_get)


def test_parses_windows_into_entries():
    snap = _reader(OK_BODY).read(T0 - timedelta(minutes=60), T0, 60, T0)
    assert snap.total_active_minutes == 47.5
    assert snap.data_status == "ok"
    assert snap.is_trustworthy is True
    assert len(snap.entries) == 2
    bili = snap.entries[0]
    assert bili.app == "chrome.exe"
    assert "哔哩哔哩" in bili.title
    assert bili.url == "https://www.bilibili.com/video/BV1xx"
    assert bili.minutes == 40.0


def test_falls_back_to_apps_when_windows_missing():
    body = {k: v for k, v in OK_BODY.items() if k != "windows"}
    snap = _reader(body).read(T0 - timedelta(minutes=60), T0, 60, T0)
    assert len(snap.entries) == 1
    assert snap.entries[0].app == "chrome.exe"
    assert snap.entries[0].title == ""


def test_request_carries_auth_headers_and_disables_text_fields():
    seen: list = []
    _reader(OK_BODY, captured=seen).read(T0 - timedelta(minutes=60), T0, 60, T0)
    url, headers, _ = seen[0]
    assert headers["Authorization"] == "Bearer sp-test"
    assert headers["X-Screenpipe-Client"] == "api"
    assert headers["X-Screenpipe-Agent"] == "statesense"
    assert "include_key_texts=false" in url
    assert "include_snippets=false" in url
    assert "include_memories=false" in url
    assert "include_guidance=false" in url
    assert "/activity-summary" in url


def test_ocr_text_never_reaches_the_snapshot():
    snap = _reader(OK_BODY).read(T0 - timedelta(minutes=60), T0, 60, T0)
    blob = repr(snap)
    assert "本行绝不应该被读进系统" not in blob
    assert "同样不应该" not in blob


def test_non_ok_data_status_is_preserved_not_swallowed():
    snap = _reader(dict(OK_BODY, data_status="no_capture_in_range")).read(
        T0 - timedelta(minutes=60), T0, 60, T0
    )
    assert snap.data_status == "no_capture_in_range"
    assert snap.is_trustworthy is False


def test_http_error_becomes_unreachable_snapshot():
    snap = _reader({}, status=403).read(T0 - timedelta(minutes=60), T0, 60, T0)
    assert snap.data_status == "unreachable"
    assert snap.entries == ()
    assert snap.total_active_minutes == 0.0


def test_malformed_json_becomes_unreachable_snapshot():
    def fake_get(url, headers, timeout):
        return 200, b"<html>not json</html>"

    snap = ActivityReader("http://localhost:3030", "k", 10.0, fake_get).read(
        T0 - timedelta(minutes=60), T0, 60, T0
    )
    assert snap.data_status == "unreachable"


def test_transport_exception_becomes_unreachable_snapshot():
    def fake_get(url, headers, timeout):
        raise OSError("connection refused")

    snap = ActivityReader("http://localhost:3030", "k", 10.0, fake_get).read(
        T0 - timedelta(minutes=60), T0, 60, T0
    )
    assert snap.data_status == "unreachable"


def test_missing_numeric_fields_default_to_zero():
    body = {"data_status": "ok", "windows": [{"app_name": "x.exe", "window_name": "y"}]}
    snap = _reader(body).read(T0 - timedelta(minutes=60), T0, 60, T0)
    assert snap.total_active_minutes == 0.0
    assert snap.entries[0].minutes == 0.0


def test_window_bounds_are_recorded_verbatim():
    start, end = T0 - timedelta(minutes=60), T0
    snap = _reader(OK_BODY).read(start, end, 60, T0)
    assert snap.window_start == start
    assert snap.window_end == end
    assert snap.window_minutes == 60
    assert snap.captured_at == T0


def test_empty_api_key_is_rejected():
    import pytest

    with pytest.raises(ValueError, match="api_key"):
        ActivityReader("http://localhost:3030", "", 10.0, lambda *a: (200, b"{}"))
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_activity_reader.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'statesense.activity'`

- [ ] **Step 3: 写最小实现**

`src/statesense/activity/__init__.py`：空文件。

`src/statesense/activity/models.py`：

```python
"""活动快照的数据契约。只承载行为元数据，绝不承载屏幕文本。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

TRUSTWORTHY_STATUSES = frozenset({"ok"})


@dataclass(frozen=True)
class Entry:
    app: str
    title: str
    url: str
    minutes: float


@dataclass(frozen=True)
class ActivitySnapshot:
    window_start: datetime
    window_end: datetime
    window_minutes: int
    total_active_minutes: float
    entries: tuple[Entry, ...]
    data_status: str
    captured_at: datetime

    @property
    def is_trustworthy(self) -> bool:
        """data_status 不是 ok 时，任何「没有活动」的结论都不成立。"""
        return self.data_status in TRUSTWORTHY_STATUSES
```

`src/statesense/activity/reader.py`：

```python
"""唯一接触 Screenpipe 的组件。上层只认 ActivitySnapshot，不知道 Screenpipe 存在。"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime

from .models import ActivitySnapshot, Entry

HttpGet = Callable[[str, Mapping[str, str], float], "tuple[int, bytes]"]

_SKIP_TEXT_PARAMS = {
    "include_key_texts": "false",
    "include_snippets": "false",
    "include_memories": "false",
    "include_guidance": "false",
}


def urllib_get(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _iso(moment: datetime) -> str:
    return moment.astimezone(tz=None).isoformat(timespec="seconds")


def _num(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


class ActivityReader:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float,
        http_get: HttpGet = urllib_get,
    ) -> None:
        if not api_key:
            raise ValueError("api_key 不能为空")
        self._base = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._http_get = http_get

    def read(
        self,
        start: datetime,
        end: datetime,
        window_minutes: int,
        captured_at: datetime,
    ) -> ActivitySnapshot:
        params = {"start_time": _iso(start), "end_time": _iso(end), **_SKIP_TEXT_PARAMS}
        url = f"{self._base}/activity-summary?{urllib.parse.urlencode(params)}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "X-Screenpipe-Client": "api",
            "X-Screenpipe-Agent": "statesense",
            "Accept": "application/json",
        }

        try:
            status, body = self._http_get(url, headers, self._timeout)
        except Exception as exc:  # noqa: BLE001 - 任何异常都不能让调度器崩掉
            return self._degraded(start, end, window_minutes, captured_at, exc.__class__.__name__)

        if status != 200:
            return self._degraded(start, end, window_minutes, captured_at, f"http {status}")

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._degraded(start, end, window_minutes, captured_at, "malformed json")
        if not isinstance(payload, dict):
            return self._degraded(start, end, window_minutes, captured_at, "unexpected payload")

        windows = payload.get("windows") or []
        apps = payload.get("apps") or []
        entries: list[Entry] = []
        if isinstance(windows, list) and windows:
            for row in windows:
                if not isinstance(row, dict):
                    continue
                entries.append(
                    Entry(
                        app=_text(row.get("app_name")),
                        title=_text(row.get("window_name")),
                        url=_text(row.get("browser_url")),
                        minutes=_num(row.get("minutes")),
                    )
                )
        elif isinstance(apps, list):
            for row in apps:
                if not isinstance(row, dict):
                    continue
                entries.append(
                    Entry(
                        app=_text(row.get("name")),
                        title="",
                        url="",
                        minutes=_num(row.get("minutes")),
                    )
                )

        total = _num(payload.get("total_active_minutes"))
        if total == 0.0 and entries:
            total = round(sum(e.minutes for e in entries), 1)

        return ActivitySnapshot(
            window_start=start,
            window_end=end,
            window_minutes=window_minutes,
            total_active_minutes=total,
            entries=tuple(entries),
            data_status=_text(payload.get("data_status")) or "unknown",
            captured_at=captured_at,
        )

    @staticmethod
    def _degraded(
        start: datetime,
        end: datetime,
        window_minutes: int,
        captured_at: datetime,
        reason: str,
    ) -> ActivitySnapshot:
        return ActivitySnapshot(
            window_start=start,
            window_end=end,
            window_minutes=window_minutes,
            total_active_minutes=0.0,
            entries=(),
            data_status=f"unreachable: {reason}",
            captured_at=captured_at,
        )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_activity_reader.py -v`
Expected: PASS，12 passed

- [ ] **Step 5: 提交**

```bash
git add src/statesense/activity tests/test_activity_reader.py
git commit -m "feat(reader): ActivityReader 读取活动摘要并强制数据最小化"
```

---

## Task 3: taxonomy（分类匹配，纯函数）

**Files:**
- Create: `src/statesense/state/__init__.py`, `src/statesense/state/taxonomy.py`
- Test: `tests/test_taxonomy.py`

**Interfaces:**
- Consumes: `Entry`、`TaxonomyConfig`
- Produces: `Category`（`StrEnum`：`ENTERTAINMENT`/`GRAY`/`WORK`/`OTHER`）；`classify(entry, taxonomy) -> Category`；`bucket_minutes(entries, taxonomy) -> dict[Category, float]`

- [ ] **Step 1: 写失败的测试**

`tests/test_taxonomy.py`：

```python
import re

import pytest

from statesense.activity.models import Entry
from statesense.config import TaxonomyConfig
from statesense.state.taxonomy import Category, bucket_minutes, classify


def _tax(**kw) -> TaxonomyConfig:
    def comp(items):
        return tuple(re.compile(i, re.IGNORECASE) for i in items)

    return TaxonomyConfig(
        entertainment=comp(kw.get("entertainment", ["bilibili", "哔哩哔哩", "youtube"])),
        gray=comp(kw.get("gray", ["知乎"])),
        work=comp(kw.get("work", ["github", r"\bcode\b"])),
    )


def _entry(title="", app="", url="", minutes=1.0) -> Entry:
    return Entry(app=app, title=title, url=url, minutes=minutes)


@pytest.mark.parametrize(
    "entry,expected",
    [
        (_entry(title="【某视频】_哔哩哔哩_bilibili"), Category.ENTERTAINMENT),
        (_entry(title="Some video - YouTube", app="chrome.exe"), Category.ENTERTAINMENT),
        (_entry(title="随便什么", url="https://www.bilibili.com/video/BV1"), Category.ENTERTAINMENT),
        (_entry(title="某问题 - 知乎"), Category.GRAY),
        (_entry(title="repo - GitHub", app="chrome.exe"), Category.WORK),
        (_entry(title="engine.py - Code", app="Code.exe"), Category.WORK),
        (_entry(title="记事本", app="notepad.exe"), Category.OTHER),
    ],
)
def test_classify_matches_on_app_title_or_url(entry, expected):
    assert classify(entry, _tax()) is expected


def test_entertainment_wins_over_work_when_both_match():
    """同时命中工作与娱乐时按娱乐处理 —— 宁可多打扰，也不要漏掉真正被困住。"""
    assert classify(_entry(title="github 上的 bilibili 视频"), _tax()) is Category.ENTERTAINMENT


def test_work_wins_over_gray():
    assert classify(_entry(title="知乎上看到的 github 项目"), _tax()) is Category.WORK


def test_bucket_minutes_sums_per_category():
    buckets = bucket_minutes(
        (
            _entry(title="哔哩哔哩_bilibili", minutes=40.0),
            _entry(title="某问题 - 知乎", minutes=5.0),
            _entry(title="repo - GitHub", minutes=12.0),
            _entry(title="记事本", minutes=3.0),
        ),
        _tax(),
    )
    assert buckets[Category.ENTERTAINMENT] == 40.0
    assert buckets[Category.GRAY] == 5.0
    assert buckets[Category.WORK] == 12.0
    assert buckets[Category.OTHER] == 3.0


def test_bucket_minutes_returns_zero_for_absent_category():
    buckets = bucket_minutes((_entry(title="记事本"),), _tax())
    assert buckets[Category.ENTERTAINMENT] == 0.0
    assert buckets[Category.GRAY] == 0.0
    assert buckets[Category.WORK] == 0.0
    assert len(buckets) == len(Category)


def test_empty_entries_bucket_all_zero():
    buckets = bucket_minutes((), _tax())
    assert all(v == 0.0 for v in buckets.values())
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_taxonomy.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'statesense.state'`

- [ ] **Step 3: 写最小实现**

`src/statesense/state/__init__.py`：空文件。

`src/statesense/state/taxonomy.py`：

```python
"""把一条活动记录归类。纯函数，无 IO。"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from re import Pattern

from statesense.activity.models import Entry
from statesense.config import TaxonomyConfig


class Category(StrEnum):
    ENTERTAINMENT = "ENTERTAINMENT"
    GRAY = "GRAY"
    WORK = "WORK"
    OTHER = "OTHER"


def _matches(entry: Entry, patterns: tuple[Pattern[str], ...]) -> bool:
    haystack = f"{entry.app}\n{entry.title}\n{entry.url}"
    return any(p.search(haystack) for p in patterns)


def classify(entry: Entry, taxonomy: TaxonomyConfig) -> Category:
    """优先级：娱乐 > 工作 > 灰色 > 其他。

    娱乐优先是刻意的：同时命中工作与娱乐时（例如在 GitHub 页面上打开的视频），
    宁可多打扰一次，也不要漏掉真正被困住的情形。
    灰色放在工作之后，因为「在知乎」与「在写代码」同时成立时，工作证据更硬。
    """
    if _matches(entry, taxonomy.entertainment):
        return Category.ENTERTAINMENT
    if _matches(entry, taxonomy.work):
        return Category.WORK
    if _matches(entry, taxonomy.gray):
        return Category.GRAY
    return Category.OTHER


def bucket_minutes(entries: Iterable[Entry], taxonomy: TaxonomyConfig) -> dict[Category, float]:
    buckets = {c: 0.0 for c in Category}
    for entry in entries:
        buckets[classify(entry, taxonomy)] += entry.minutes
    return buckets
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_taxonomy.py -v`
Expected: PASS，12 passed

- [ ] **Step 5: 提交**

```bash
git add src/statesense/state/__init__.py src/statesense/state/taxonomy.py tests/test_taxonomy.py
git commit -m "feat(state): 活动分类匹配（娱乐优先，纯函数）"
```

---

## Task 4: StateEngine（状态判定，纯函数）

**Files:**
- Create: `src/statesense/state/models.py`, `src/statesense/state/engine.py`
- Test: `tests/test_state_engine.py`

**Interfaces:**
- Consumes: `ActivitySnapshot`、`TaxonomyConfig`、`ThresholdConfig`
- Produces:
  - `State`（`StrEnum`：`NORMAL`/`WATCH`/`PASSIVE_CONSUMPTION`/`HIGH_RISK_PASSIVE_CONSUMPTION`）
  - `StateVerdict(state, late_night, total_active_minutes, ent_minutes, gray_minutes, work_minutes, ent_ratio, window_minutes, data_status, skipped, skip_reason)`
  - `is_late_night(instant, total_active_minutes, thresholds) -> bool`
  - `classify(snapshot, taxonomy, thresholds) -> StateVerdict`

- [ ] **Step 1: 写失败的测试**

`tests/test_state_engine.py`：

```python
import re
from datetime import datetime, timedelta, timezone

from statesense.activity.models import ActivitySnapshot, Entry
from statesense.config import TaxonomyConfig, ThresholdConfig
from statesense.state.engine import classify, is_late_night
from statesense.state.models import State

T0 = datetime(2026, 9, 16, 6, 0, tzinfo=timezone.utc)
TAX = TaxonomyConfig(
    entertainment=(re.compile("bilibili|哔哩哔哩", re.IGNORECASE),),
    gray=(re.compile("知乎", re.IGNORECASE),),
    work=(re.compile("github", re.IGNORECASE),),
)
TH = ThresholdConfig()


def _snap(entries, total, data_status="ok") -> ActivitySnapshot:
    return ActivitySnapshot(
        window_start=T0 - timedelta(minutes=60),
        window_end=T0,
        window_minutes=60,
        total_active_minutes=total,
        entries=tuple(entries),
        data_status=data_status,
        captured_at=T0,
    )


def _ent(minutes, title="哔哩哔哩_bilibili") -> Entry:
    return Entry(app="chrome.exe", title=title, url="", minutes=minutes)


def _work(minutes) -> Entry:
    return Entry(app="Code.exe", title="repo - GitHub", url="", minutes=minutes)


def _other(minutes) -> Entry:
    return Entry(app="notepad.exe", title="记事本", url="", minutes=minutes)


# ── 状态阶梯（ref1 的 20 / 40 / 65 刻度）────────────────────

def test_below_watch_threshold_is_normal():
    v = classify(_snap([_ent(5.0), _work(50.0)], 55.0), TAX, TH)
    assert v.state is State.NORMAL
    assert v.ent_minutes == 5.0


def test_at_watch_threshold_is_watch():
    assert classify(_snap([_ent(20.0)], 60.0), TAX, TH).state is State.WATCH


def test_at_passive_threshold_is_passive():
    assert classify(_snap([_ent(40.0)], 60.0), TAX, TH).state is State.PASSIVE_CONSUMPTION


def test_at_high_risk_threshold_is_high_risk():
    assert classify(_snap([_ent(65.0)], 60.0), TAX, TH).state is State.HIGH_RISK_PASSIVE_CONSUMPTION


def test_just_below_passive_stays_watch():
    assert classify(_snap([_ent(39.9)], 60.0), TAX, TH).state is State.WATCH


# ── 分类拆分 ────────────────────────────────────────────────

def test_buckets_are_reported_separately():
    v = classify(
        _snap(
            [_ent(42.0), Entry("chrome.exe", "某问题 - 知乎", "", 6.0), _work(9.0), _other(3.0)],
            60.0,
        ),
        TAX,
        TH,
    )
    assert v.ent_minutes == 42.0
    assert v.gray_minutes == 6.0
    assert v.work_minutes == 9.0
    assert v.total_active_minutes == 60.0
    assert v.ent_ratio == 0.7


def test_gray_never_counts_toward_passive_consumption():
    """知乎只单独统计，不抬高 ent —— 否则会把「可能在学习」误判成被困。"""
    v = classify(_snap([Entry("chrome.exe", "某问题 - 知乎", "", 50.0)], 50.0), TAX, TH)
    assert v.ent_minutes == 0.0
    assert v.gray_minutes == 50.0
    assert v.state is State.NORMAL


def test_window_minutes_is_carried_through():
    assert classify(_snap([_ent(45.0)], 60.0), TAX, TH).window_minutes == 60


# ── ratio 与除零 ─────────────────────────────────────────────

def test_ratio_is_ent_over_total_active():
    assert classify(_snap([_ent(30.0), _other(30.0)], 60.0), TAX, TH).ent_ratio == 0.5


def test_zero_total_active_gives_zero_ratio_without_dividing_by_zero():
    v = classify(_snap([], 0.0), TAX, TH)
    assert v.ent_ratio == 0.0
    assert v.state is State.NORMAL


# ── data_status 闸门 ────────────────────────────────────────

def test_non_ok_data_status_skips_without_concluding():
    """最关键的一条：没在采集 ≠ 没有活动。"""
    for status in ("empty_but_recording", "no_capture_in_range", "not_recording", "unreachable"):
        v = classify(_snap([_ent(0.0)], 0.0, data_status=status), TAX, TH)
        assert v.skipped is True, status
        assert v.skip_reason == status
        assert v.state is State.NORMAL


def test_skipped_verdict_still_reports_measured_buckets():
    v = classify(_snap([_ent(50.0)], 60.0, data_status="no_capture_in_range"), TAX, TH)
    assert v.skipped is True
    assert v.ent_minutes == 50.0


# ── late_night 正交标记 ──────────────────────────────────────

def test_late_night_uses_local_hour():
    assert is_late_night(datetime(2026, 9, 16, 3, 0).astimezone(), 30.0, TH) is True
    assert is_late_night(datetime(2026, 9, 16, 15, 0).astimezone(), 30.0, TH) is False


def test_late_night_requires_minimum_activity():
    assert is_late_night(datetime(2026, 9, 16, 3, 0).astimezone(), 5.0, TH) is False


def test_late_night_boundaries():
    th = ThresholdConfig(late_night_start_hour=1, late_night_end_hour=6)
    for hour, expected in ((0, False), (1, True), (5, True), (6, False), (23, False)):
        instant = datetime(2026, 9, 16, hour, 0).astimezone()
        assert is_late_night(instant, 30.0, th) is expected, f"hour={hour}"


def test_late_night_is_independent_of_state():
    """凌晨写代码：state 是 NORMAL，但 late_night 必须为真。"""
    night = datetime(2026, 9, 16, 3, 0).astimezone()
    snap = ActivitySnapshot(
        window_start=night - timedelta(minutes=60),
        window_end=night,
        window_minutes=60,
        total_active_minutes=60.0,
        entries=(_work(50.0), _other(10.0)),
        data_status="ok",
        captured_at=night,
    )
    v = classify(snap, TAX, TH)
    assert v.state is State.NORMAL
    assert v.late_night is True
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_state_engine.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'statesense.state.models'`

- [ ] **Step 3: 写最小实现**

`src/statesense/state/models.py`：

```python
"""状态判定的产物。state 与 late_night 是两个正交维度。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class State(StrEnum):
    NORMAL = "NORMAL"
    WATCH = "WATCH"
    PASSIVE_CONSUMPTION = "PASSIVE_CONSUMPTION"
    HIGH_RISK_PASSIVE_CONSUMPTION = "HIGH_RISK_PASSIVE_CONSUMPTION"


@dataclass(frozen=True)
class StateVerdict:
    state: State
    late_night: bool
    total_active_minutes: float
    ent_minutes: float
    gray_minutes: float
    work_minutes: float
    ent_ratio: float
    window_minutes: int
    data_status: str
    skipped: bool
    skip_reason: str | None
```

`src/statesense/state/engine.py`：

```python
"""状态推断。纯函数：不读时钟、不做 IO，时间由参数传入。

late_night 刻意不做成状态：凌晨 3 点刷 B 站，「凌晨」与「被动消费」是两个
正交事实，压成一个枚举必须二选一、必然丢信息。凌晨 3 点写代码尤其能说明
问题 —— 它的 state 是 NORMAL，但「凌晨」依然值得介入。
"""

from __future__ import annotations

from datetime import datetime

from statesense.activity.models import ActivitySnapshot
from statesense.config import TaxonomyConfig, ThresholdConfig
from statesense.state.models import State, StateVerdict
from statesense.state.taxonomy import Category, bucket_minutes


def is_late_night(
    instant: datetime, total_active_minutes: float, thresholds: ThresholdConfig
) -> bool:
    hour = instant.astimezone().hour
    in_window = thresholds.late_night_start_hour <= hour < thresholds.late_night_end_hour
    return in_window and total_active_minutes >= thresholds.late_night_min_active_minutes


def _round2(value: float) -> float:
    return round(value, 2)


def classify(
    snapshot: ActivitySnapshot,
    taxonomy: TaxonomyConfig,
    thresholds: ThresholdConfig,
) -> StateVerdict:
    buckets = bucket_minutes(snapshot.entries, taxonomy)
    ent = _round2(buckets[Category.ENTERTAINMENT])
    gray = _round2(buckets[Category.GRAY])
    work = _round2(buckets[Category.WORK])
    total = _round2(snapshot.total_active_minutes)
    ratio = _round2(ent / total) if total > 0 else 0.0

    shared = dict(
        late_night=is_late_night(snapshot.captured_at, total, thresholds),
        total_active_minutes=total,
        ent_minutes=ent,
        gray_minutes=gray,
        work_minutes=work,
        ent_ratio=ratio,
        window_minutes=snapshot.window_minutes,
        data_status=snapshot.data_status,
    )

    # data_status 不是 ok 时不下结论：那可能只是 recorder 没在采。
    if not snapshot.is_trustworthy:
        return StateVerdict(
            state=State.NORMAL, skipped=True, skip_reason=snapshot.data_status, **shared
        )

    if ent >= thresholds.high_risk_minutes:
        state = State.HIGH_RISK_PASSIVE_CONSUMPTION
    elif ent >= thresholds.passive_minutes:
        state = State.PASSIVE_CONSUMPTION
    elif ent >= thresholds.watch_minutes:
        state = State.WATCH
    else:
        state = State.NORMAL

    return StateVerdict(state=state, skipped=False, skip_reason=None, **shared)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_state_engine.py -v`
Expected: PASS，18 passed

- [ ] **Step 5: 提交**

```bash
git add src/statesense/state/models.py src/statesense/state/engine.py tests/test_state_engine.py
git commit -m "feat(state): 状态判定引擎（late_night 正交化，data_status 不 ok 则不下结论）"
```

---

## Task 5: Store（SQLite 持久化）

**Files:**
- Create: `src/statesense/intervention/__init__.py`, `src/statesense/intervention/models.py`
- Create: `src/statesense/outcome/__init__.py`, `src/statesense/outcome/models.py`
- Create: `src/statesense/store/__init__.py`, `src/statesense/store/schema.sql`, `src/statesense/store/db.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `StateVerdict`、`Decision`、`OutcomeVerdict`
- Produces:
  - `GateResult(name, passed, value, threshold)`、`Decision(intervene, action_id, reason, gate_trace)`
  - `OutcomeVerdict(outcome, ent_before, ent_after, after_window_minutes)`
  - `Store(db_path)`：`migrate()`、`close()`、`user_version()`
  - `insert_evaluation(at, verdict, decision) -> int`
  - `insert_intervention(evaluation_id, at, state, late_night, action_id, action_text, delivery_status, outcome_due_at) -> int`
  - `insert_outcome(intervention_id, checked_at, verdict) -> None`
  - `due_interventions(now) -> list[DueIntervention]`（`DueIntervention(id, at)`）
  - `last_intervention_at() -> datetime | None`（只算 `delivered`）
  - `intervention_count_since(moment) -> int`（只算 `delivered`）
  - `get_kv(key)` / `set_kv(key, value)`
  - `fetch_evaluation(id)` / `fetch_outcome(intervention_id)`

- [ ] **Step 1: 写失败的测试**

`tests/test_store.py`：

```python
import json
from datetime import datetime, timedelta, timezone

import pytest

from statesense.intervention.models import Decision, GateResult
from statesense.outcome.models import OutcomeVerdict
from statesense.state.models import State, StateVerdict
from statesense.store.db import Store

T0 = datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    s.migrate()
    yield s
    s.close()


def _verdict(state=State.PASSIVE_CONSUMPTION) -> StateVerdict:
    return StateVerdict(
        state=state,
        late_night=False,
        total_active_minutes=60.0,
        ent_minutes=45.0,
        gray_minutes=5.0,
        work_minutes=10.0,
        ent_ratio=0.75,
        window_minutes=60,
        data_status="ok",
        skipped=False,
        skip_reason=None,
    )


def _decision(intervene=True) -> Decision:
    return Decision(
        intervene=intervene,
        action_id="walk5" if intervene else None,
        reason="测试",
        gate_trace=(GateResult("ratio_min", True, 0.75, 0.75),),
    )


def test_migrate_is_idempotent(tmp_path):
    s = Store(tmp_path / "x.db")
    s.migrate()
    s.migrate()
    assert s.user_version() == 1
    s.close()


def test_creates_parent_directory(tmp_path):
    s = Store(tmp_path / "nested" / "deep" / "x.db")
    s.migrate()
    assert (tmp_path / "nested" / "deep" / "x.db").is_file()
    s.close()


def test_insert_evaluation_roundtrips(store):
    eid = store.insert_evaluation(T0, _verdict(), _decision())
    row = store.fetch_evaluation(eid)
    assert row["state"] == "PASSIVE_CONSUMPTION"
    assert row["ent_minutes"] == 45.0
    assert row["ent_ratio"] == 0.75
    assert row["window_minutes"] == 60
    assert row["decision"] == "intervene"
    trace = json.loads(row["gate_trace"])
    assert trace[0]["name"] == "ratio_min"
    assert trace[0]["passed"] is True


def test_skip_decision_is_recorded_as_skip(store):
    eid = store.insert_evaluation(T0, _verdict(), _decision(intervene=False))
    assert store.fetch_evaluation(eid)["decision"] == "skip"


def test_prev_state_chain(store):
    store.insert_evaluation(T0, _verdict(), _decision())
    eid = store.insert_evaluation(T0 + timedelta(minutes=5), _verdict(State.WATCH), _decision())
    assert store.fetch_evaluation(eid)["prev_state"] == "PASSIVE_CONSUMPTION"


def test_first_evaluation_has_null_prev_state(store):
    eid = store.insert_evaluation(T0, _verdict(), _decision())
    assert store.fetch_evaluation(eid)["prev_state"] is None


def test_intervention_and_due_lookup(store):
    eid = store.insert_evaluation(T0, _verdict(), _decision())
    due = T0 + timedelta(minutes=10)
    iid = store.insert_intervention(
        evaluation_id=eid, at=T0, state="PASSIVE_CONSUMPTION", late_night=False,
        action_id="walk5", action_text="离开电脑走 5 分钟",
        delivery_status="delivered", outcome_due_at=due,
    )
    assert store.due_interventions(T0 + timedelta(minutes=9)) == []
    pending = store.due_interventions(due)
    assert len(pending) == 1
    assert pending[0].id == iid
    assert pending[0].at == T0


def test_due_interventions_excludes_already_checked(store):
    eid = store.insert_evaluation(T0, _verdict(), _decision())
    due = T0 + timedelta(minutes=10)
    iid = store.insert_intervention(
        eid, T0, "PASSIVE_CONSUMPTION", False, "walk5", "走 5 分钟", "delivered", due
    )
    store.insert_outcome(iid, due, OutcomeVerdict("disengaged", 45.0, 3.0, 10.0))
    assert store.due_interventions(due + timedelta(minutes=60)) == []


def test_outcome_roundtrip(store):
    eid = store.insert_evaluation(T0, _verdict(), _decision())
    iid = store.insert_intervention(
        eid, T0, "PASSIVE_CONSUMPTION", False, "walk5", "走 5 分钟", "delivered",
        T0 + timedelta(minutes=10),
    )
    store.insert_outcome(iid, T0 + timedelta(minutes=10), OutcomeVerdict("partial", 45.0, 30.0, 10.0))
    row = store.fetch_outcome(iid)
    assert row["outcome"] == "partial"
    assert row["ent_before"] == 45.0
    assert row["ent_after"] == 30.0


def test_last_intervention_at_ignores_failed_delivery(store):
    """冷却要按「用户真的被打扰过」来算，投递失败的不能算。"""
    eid = store.insert_evaluation(T0, _verdict(), _decision())
    store.insert_intervention(
        eid, T0, "PASSIVE_CONSUMPTION", False, "walk5", "走 5 分钟",
        "failed: toast unavailable", T0 + timedelta(minutes=10),
    )
    assert store.last_intervention_at() is None
    store.insert_intervention(
        eid, T0 + timedelta(minutes=1), "PASSIVE_CONSUMPTION", False, "walk5", "走 5 分钟",
        "delivered", T0 + timedelta(minutes=11),
    )
    assert store.last_intervention_at() == T0 + timedelta(minutes=1)


def test_intervention_count_since(store):
    eid = store.insert_evaluation(T0, _verdict(), _decision())
    for i in range(3):
        store.insert_intervention(
            eid, T0 + timedelta(minutes=i), "PASSIVE_CONSUMPTION", False, "walk5", "走 5 分钟",
            "delivered", T0 + timedelta(minutes=10 + i),
        )
    assert store.intervention_count_since(T0) == 3
    assert store.intervention_count_since(T0 + timedelta(minutes=1)) == 2


def test_kv_roundtrip_and_default(store):
    assert store.get_kv("action_cursor") is None
    store.set_kv("action_cursor", "calligraphy")
    assert store.get_kv("action_cursor") == "calligraphy"
    store.set_kv("action_cursor", "stretch")
    assert store.get_kv("action_cursor") == "stretch"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'statesense.store'`

- [ ] **Step 3: 写最小实现**

`src/statesense/intervention/__init__.py`、`src/statesense/outcome/__init__.py`、`src/statesense/store/__init__.py`：空文件。

`src/statesense/intervention/models.py`：

```python
"""闸门与决策的数据契约。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    value: float
    threshold: float


@dataclass(frozen=True)
class Decision:
    intervene: bool
    action_id: str | None
    reason: str
    gate_trace: tuple[GateResult, ...]
```

`src/statesense/outcome/models.py`：

```python
"""行为回执的数据契约。原始值必须留，标签只是派生。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OutcomeVerdict:
    outcome: str
    ent_before: float
    ent_after: float
    after_window_minutes: float
```

`src/statesense/store/schema.sql`：

```sql
-- evaluations：每次评估都写，调阈值与复盘全靠它
CREATE TABLE IF NOT EXISTS evaluations (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  window_minutes INTEGER NOT NULL,
  total_active_minutes REAL NOT NULL,
  ent_minutes REAL NOT NULL,
  gray_minutes REAL NOT NULL,
  work_minutes REAL NOT NULL,
  ent_ratio REAL NOT NULL,
  state TEXT NOT NULL,
  late_night INTEGER NOT NULL,
  data_status TEXT NOT NULL,
  prev_state TEXT,
  decision TEXT NOT NULL,
  gate_trace TEXT NOT NULL
);

-- interventions：每次真正发出的干预
CREATE TABLE IF NOT EXISTS interventions (
  id INTEGER PRIMARY KEY,
  evaluation_id INTEGER NOT NULL REFERENCES evaluations(id),
  at TEXT NOT NULL,
  state TEXT NOT NULL,
  late_night INTEGER NOT NULL,
  action_id TEXT NOT NULL,
  action_text TEXT NOT NULL,
  delivery_status TEXT NOT NULL,
  outcome_due_at TEXT NOT NULL
);

-- outcomes：一次干预对应一行回执
CREATE TABLE IF NOT EXISTS outcomes (
  intervention_id INTEGER PRIMARY KEY REFERENCES interventions(id),
  checked_at TEXT NOT NULL,
  outcome TEXT NOT NULL,
  ent_before REAL NOT NULL,
  ent_after REAL NOT NULL,
  after_window_minutes REAL NOT NULL
);

-- kv：少量运行期状态（动作池轮转游标等）
CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evaluations_at ON evaluations(at);
CREATE INDEX IF NOT EXISTS idx_interventions_due ON interventions(outcome_due_at);
CREATE INDEX IF NOT EXISTS idx_interventions_at ON interventions(at);
```

`src/statesense/store/db.py`：

```python
"""SQLite 持久化。schema 版本用 PRAGMA user_version 管理。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from statesense.intervention.models import Decision
from statesense.outcome.models import OutcomeVerdict
from statesense.state.models import StateVerdict

SCHEMA_VERSION = 1
_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _iso(moment: datetime) -> str:
    return moment.astimezone(tz=None).isoformat()


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass(frozen=True)
class DueIntervention:
    id: int
    at: datetime


class Store:
    def __init__(self, db_path: Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")

    # ── 生命周期 ────────────────────────────────────────────

    def migrate(self) -> None:
        with self._conn:
            self._conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def user_version(self) -> int:
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def close(self) -> None:
        self._conn.close()

    # ── 写入 ────────────────────────────────────────────────

    def _previous_state(self) -> str | None:
        row = self._conn.execute(
            "SELECT state FROM evaluations ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row["state"] if row else None

    def insert_evaluation(self, at: datetime, verdict: StateVerdict, decision: Decision) -> int:
        trace = json.dumps(
            [
                {"name": g.name, "passed": g.passed, "value": g.value, "threshold": g.threshold}
                for g in decision.gate_trace
            ],
            ensure_ascii=False,
        )
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO evaluations (
                  at, window_minutes, total_active_minutes, ent_minutes, gray_minutes,
                  work_minutes, ent_ratio, state, late_night, data_status, prev_state,
                  decision, gate_trace
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _iso(at),
                    verdict.window_minutes,
                    verdict.total_active_minutes,
                    verdict.ent_minutes,
                    verdict.gray_minutes,
                    verdict.work_minutes,
                    verdict.ent_ratio,
                    str(verdict.state),
                    int(verdict.late_night),
                    verdict.skip_reason or verdict.data_status,
                    self._previous_state(),
                    "intervene" if decision.intervene else "skip",
                    trace,
                ),
            )
            return int(cur.lastrowid)

    def insert_intervention(
        self,
        evaluation_id: int,
        at: datetime,
        state: str,
        late_night: bool,
        action_id: str,
        action_text: str,
        delivery_status: str,
        outcome_due_at: datetime,
    ) -> int:
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO interventions (
                  evaluation_id, at, state, late_night, action_id, action_text,
                  delivery_status, outcome_due_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation_id,
                    _iso(at),
                    state,
                    int(late_night),
                    action_id,
                    action_text,
                    delivery_status,
                    _iso(outcome_due_at),
                ),
            )
            return int(cur.lastrowid)

    def insert_outcome(
        self, intervention_id: int, checked_at: datetime, verdict: OutcomeVerdict
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO outcomes (
                  intervention_id, checked_at, outcome, ent_before, ent_after,
                  after_window_minutes
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    intervention_id,
                    _iso(checked_at),
                    verdict.outcome,
                    verdict.ent_before,
                    verdict.ent_after,
                    verdict.after_window_minutes,
                ),
            )

    def set_kv(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value)
            )

    # ── 读取 ────────────────────────────────────────────────

    def get_kv(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def fetch_evaluation(self, evaluation_id: int) -> sqlite3.Row:
        return self._conn.execute(
            "SELECT * FROM evaluations WHERE id = ?", (evaluation_id,)
        ).fetchone()

    def fetch_outcome(self, intervention_id: int) -> sqlite3.Row:
        return self._conn.execute(
            "SELECT * FROM outcomes WHERE intervention_id = ?", (intervention_id,)
        ).fetchone()

    def due_interventions(self, now: datetime) -> list[DueIntervention]:
        rows = self._conn.execute(
            """
            SELECT i.id AS id, i.at AS at
            FROM interventions i
            LEFT JOIN outcomes o ON o.intervention_id = i.id
            WHERE o.intervention_id IS NULL AND i.outcome_due_at <= ?
            ORDER BY i.id
            """,
            (_iso(now),),
        ).fetchall()
        return [DueIntervention(id=r["id"], at=_parse(r["at"])) for r in rows]

    def last_intervention_at(self) -> datetime | None:
        row = self._conn.execute(
            "SELECT at FROM interventions WHERE delivery_status = 'delivered' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return _parse(row["at"]) if row else None

    def intervention_count_since(self, moment: datetime) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM interventions WHERE delivery_status = 'delivered' AND at >= ?",
            (_iso(moment),),
        ).fetchone()
        return int(row["n"])
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_store.py -v`
Expected: PASS，12 passed

- [ ] **Step 5: 提交**

```bash
git add src/statesense/intervention/__init__.py src/statesense/intervention/models.py src/statesense/outcome/__init__.py src/statesense/outcome/models.py src/statesense/store tests/test_store.py
git commit -m "feat(store): SQLite 三表 + kv，state_transitions 由 evaluations 派生"
```

---

## Task 6: 闸门、动作池与决策合成（纯函数）

**Files:**
- Create: `src/statesense/intervention/gates.py`, `src/statesense/intervention/actions.py`, `src/statesense/intervention/decider.py`
- Test: `tests/test_gates.py`, `tests/test_actions.py`, `tests/test_decider.py`

**Interfaces:**
- Consumes: `StateVerdict`、`GateConfig`、`Action`、`State`
- Produces:
  - `GateContext(verdict, config, now, last_intervention_at, interventions_today)`
  - `evaluate_state_min(ctx)` / `evaluate_ratio_min(ctx)` / `evaluate_cooldown(ctx)` / `evaluate_daily_cap(ctx) -> GateResult`
  - `GATE_REGISTRY: dict[str, Callable[[GateContext], GateResult]]`
  - `run_gates(ctx) -> tuple[GateResult, ...]`
  - `candidates(actions, state) -> tuple[Action, ...]`
  - `pick(pool, last_action_id) -> Action | None`
  - `decide(ctx, actions, last_action_id) -> Decision`

- [ ] **Step 1: 写失败的测试**

`tests/test_actions.py`：

```python
from statesense.config import Action
from statesense.intervention.actions import candidates, pick
from statesense.state.models import State

POOL = (
    Action("walk5", "离开电脑走 5 分钟", ("PASSIVE_CONSUMPTION", "HIGH_RISK_PASSIVE_CONSUMPTION")),
    Action("calligraphy", "练字 5 分钟", ("PASSIVE_CONSUMPTION", "HIGH_RISK_PASSIVE_CONSUMPTION")),
    Action("reading", "读一段《道德经》", ("HIGH_RISK_PASSIVE_CONSUMPTION",)),
)


def test_candidates_filter_by_state():
    assert [a.id for a in candidates(POOL, State.PASSIVE_CONSUMPTION)] == ["walk5", "calligraphy"]
    assert [a.id for a in candidates(POOL, State.HIGH_RISK_PASSIVE_CONSUMPTION)] == [
        "walk5",
        "calligraphy",
        "reading",
    ]


def test_candidates_empty_for_normal_state():
    assert candidates(POOL, State.NORMAL) == ()


def test_pick_starts_at_first_when_no_history():
    assert pick(candidates(POOL, State.PASSIVE_CONSUMPTION), None).id == "walk5"


def test_pick_round_robins():
    pool = candidates(POOL, State.PASSIVE_CONSUMPTION)
    assert pick(pool, "walk5").id == "calligraphy"
    assert pick(pool, "calligraphy").id == "walk5"


def test_pick_wraps_around():
    pool = candidates(POOL, State.HIGH_RISK_PASSIVE_CONSUMPTION)
    assert pick(pool, "reading").id == "walk5"


def test_pick_falls_back_to_first_when_last_not_in_pool():
    """状态变化导致上次动作不在候选里时，从头发起，不能崩。"""
    assert pick(candidates(POOL, State.PASSIVE_CONSUMPTION), "reading").id == "walk5"


def test_pick_returns_none_for_empty_pool():
    assert pick((), "walk5") is None
```

`tests/test_gates.py`：

```python
from datetime import datetime, timedelta, timezone

from statesense.config import GateConfig
from statesense.intervention.gates import GateContext, run_gates
from statesense.state.models import State, StateVerdict

T0 = datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)


def _verdict(state=State.PASSIVE_CONSUMPTION, ratio=0.75) -> StateVerdict:
    return StateVerdict(
        state=state, late_night=False, total_active_minutes=60.0, ent_minutes=45.0,
        gray_minutes=5.0, work_minutes=10.0, ent_ratio=ratio, window_minutes=60,
        data_status="ok", skipped=False, skip_reason=None,
    )


def _ctx(verdict=None, *, now=T0, last=None, today=0, gate=None) -> GateContext:
    return GateContext(
        verdict=verdict or _verdict(),
        config=gate or GateConfig(ratio_min=0.75),
        now=now,
        last_intervention_at=last,
        interventions_today=today,
    )


def _named(results, name):
    return next(r for r in results if r.name == name)


def test_all_gates_pass_in_the_happy_path():
    results = run_gates(_ctx())
    assert all(r.passed for r in results)
    assert [r.name for r in results] == ["state_min", "ratio_min", "cooldown", "daily_cap"]


def test_state_min_blocks_normal():
    assert _named(run_gates(_ctx(_verdict(state=State.NORMAL))), "state_min").passed is False


def test_state_min_blocks_watch():
    assert _named(run_gates(_ctx(_verdict(state=State.WATCH))), "state_min").passed is False


def test_state_min_allows_high_risk():
    ctx = _ctx(_verdict(state=State.HIGH_RISK_PASSIVE_CONSUMPTION))
    assert _named(run_gates(ctx), "state_min").passed is True


def test_ratio_min_blocks_below_threshold():
    result = _named(run_gates(_ctx(_verdict(ratio=0.70))), "ratio_min")
    assert result.passed is False
    assert result.value == 0.70
    assert result.threshold == 0.75


def test_ratio_min_passes_exactly_at_threshold():
    assert _named(run_gates(_ctx(_verdict(ratio=0.75))), "ratio_min").passed is True


def test_cooldown_blocks_within_window():
    result = _named(run_gates(_ctx(last=T0 - timedelta(minutes=10))), "cooldown")
    assert result.passed is False
    assert result.value == 10.0
    assert result.threshold == 30


def test_cooldown_passes_after_window():
    assert _named(run_gates(_ctx(last=T0 - timedelta(minutes=31))), "cooldown").passed is True


def test_cooldown_passes_when_never_intervened():
    assert _named(run_gates(_ctx(last=None)), "cooldown").passed is True


def test_daily_cap_blocks_at_limit():
    assert _named(run_gates(_ctx(today=8)), "daily_cap").passed is False


def test_daily_cap_passes_below_limit():
    assert _named(run_gates(_ctx(today=7)), "daily_cap").passed is True


def test_gate_trace_has_all_entries_even_when_everything_is_blocked():
    """日志必须能看出被哪条挡下，所以无论通过与否都要有记录。"""
    ctx = _ctx(_verdict(state=State.NORMAL, ratio=0.1), last=T0, today=99)
    results = run_gates(ctx)
    assert len(results) == 4
    assert sum(1 for r in results if not r.passed) == 4


def test_disabled_gate_is_not_run():
    gate = GateConfig(enabled=("state_min",), ratio_min=0.75)
    assert [r.name for r in run_gates(_ctx(gate=gate))] == ["state_min"]
```

`tests/test_decider.py`：

```python
from datetime import datetime, timezone

from statesense.config import Action, GateConfig
from statesense.intervention.decider import decide
from statesense.intervention.gates import GateContext
from statesense.state.models import State, StateVerdict

T0 = datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)
POOL = (
    Action("walk5", "离开电脑走 5 分钟", ("PASSIVE_CONSUMPTION",)),
    Action("calligraphy", "练字 5 分钟", ("PASSIVE_CONSUMPTION",)),
)


def _ctx(state=State.PASSIVE_CONSUMPTION, ratio=0.75) -> GateContext:
    return GateContext(
        verdict=StateVerdict(
            state=state, late_night=False, total_active_minutes=60.0, ent_minutes=45.0,
            gray_minutes=5.0, work_minutes=10.0, ent_ratio=ratio, window_minutes=60,
            data_status="ok", skipped=False, skip_reason=None,
        ),
        config=GateConfig(ratio_min=0.75),
        now=T0,
        last_intervention_at=None,
        interventions_today=0,
    )


def test_decide_intervenes_when_all_gates_pass():
    d = decide(_ctx(), POOL, None)
    assert d.intervene is True
    assert d.action_id == "walk5"


def test_decide_records_gate_trace():
    d = decide(_ctx(), POOL, None)
    assert len(d.gate_trace) == 4


def test_decide_reason_names_the_blocking_gate():
    d = decide(_ctx(ratio=0.5), POOL, None)
    assert d.intervene is False
    assert "ratio_min" in d.reason


def test_decide_reason_names_every_blocking_gate():
    d = decide(_ctx(state=State.NORMAL, ratio=0.1), POOL, None)
    assert d.intervene is False
    assert "state_min" in d.reason
    assert "ratio_min" in d.reason


def test_decide_does_not_intervene_when_pool_is_empty():
    d = decide(_ctx(state=State.HIGH_RISK_PASSIVE_CONSUMPTION), (), None)
    assert d.intervene is False
    assert "没有可用动作" in d.reason


def test_decide_advances_rotation():
    assert decide(_ctx(), POOL, "walk5").action_id == "calligraphy"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_gates.py tests/test_actions.py tests/test_decider.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'statesense.intervention.gates'`

- [ ] **Step 3: 写最小实现**

`src/statesense/intervention/gates.py`：

```python
"""介入闸门。状态回答「我在什么状态」，闸门回答「现在该不该打扰」。

每个条件都返回 (通过?, 实际值, 阈值)，无论通过与否都记录 ——
否则日志里看不出是被哪一条挡下的，阈值就无从调起。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from statesense.config import GateConfig
from statesense.intervention.models import GateResult
from statesense.state.models import State, StateVerdict

INTERVENABLE = frozenset({State.PASSIVE_CONSUMPTION, State.HIGH_RISK_PASSIVE_CONSUMPTION})


@dataclass(frozen=True)
class GateContext:
    verdict: StateVerdict
    config: GateConfig
    now: datetime
    last_intervention_at: datetime | None
    interventions_today: int


def evaluate_state_min(ctx: GateContext) -> GateResult:
    passed = ctx.verdict.state in INTERVENABLE
    return GateResult(name="state_min", passed=passed, value=1.0 if passed else 0.0, threshold=1.0)


def evaluate_ratio_min(ctx: GateContext) -> GateResult:
    threshold = ctx.config.ratio_min if ctx.config.ratio_min is not None else 1.0
    return GateResult(
        name="ratio_min",
        passed=ctx.verdict.ent_ratio >= threshold,
        value=ctx.verdict.ent_ratio,
        threshold=threshold,
    )


def evaluate_cooldown(ctx: GateContext) -> GateResult:
    limit = ctx.config.cooldown_minutes
    if ctx.last_intervention_at is None:
        return GateResult(name="cooldown", passed=True, value=float("inf"), threshold=limit)
    elapsed = (ctx.now - ctx.last_intervention_at).total_seconds() / 60.0
    return GateResult(
        name="cooldown", passed=elapsed >= limit, value=round(elapsed, 1), threshold=limit
    )


def evaluate_daily_cap(ctx: GateContext) -> GateResult:
    return GateResult(
        name="daily_cap",
        passed=ctx.interventions_today < ctx.config.daily_cap,
        value=float(ctx.interventions_today),
        threshold=float(ctx.config.daily_cap),
    )


GATE_REGISTRY: dict[str, Callable[[GateContext], GateResult]] = {
    "state_min": evaluate_state_min,
    "ratio_min": evaluate_ratio_min,
    "cooldown": evaluate_cooldown,
    "daily_cap": evaluate_daily_cap,
}


def run_gates(ctx: GateContext) -> tuple[GateResult, ...]:
    return tuple(GATE_REGISTRY[name](ctx) for name in ctx.config.enabled if name in GATE_REGISTRY)
```

`src/statesense/intervention/actions.py`：

```python
"""动作池与轮转选择。

用轮转而非随机：随机会让「哪个动作最有效」无法归因。轮转保证每个动作拿到
大致均衡的样本，正好喂给 V1 的「什么干预最有效」分析。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from statesense.config import Action
from statesense.state.models import State


def candidates(actions: Iterable[Action], state: State) -> tuple[Action, ...]:
    return tuple(a for a in actions if str(state) in a.applies_to)


def pick(pool: Sequence[Action], last_action_id: str | None) -> Action | None:
    if not pool:
        return None
    if last_action_id is None:
        return pool[0]
    for index, action in enumerate(pool):
        if action.id == last_action_id:
            return pool[(index + 1) % len(pool)]
    return pool[0]
```

`src/statesense/intervention/decider.py`：

```python
"""把闸门结果与动作选择合成为一次决策。纯函数。"""

from __future__ import annotations

from collections.abc import Iterable

from statesense.config import Action
from statesense.intervention.actions import pick
from statesense.intervention.gates import GateContext, run_gates
from statesense.intervention.models import Decision


def _describe(result) -> str:
    return f"{result.name} 未通过（{result.value} vs 阈值 {result.threshold}）"


def decide(ctx: GateContext, actions: Iterable[Action], last_action_id: str | None) -> Decision:
    trace = run_gates(ctx)
    blocked = [r for r in trace if not r.passed]
    if blocked:
        return Decision(
            intervene=False,
            action_id=None,
            reason="；".join(_describe(r) for r in blocked),
            gate_trace=trace,
        )

    chosen = pick(tuple(actions), last_action_id)
    if chosen is None:
        return Decision(
            intervene=False,
            action_id=None,
            reason="闸门全过但当前状态没有可用动作",
            gate_trace=trace,
        )
    return Decision(
        intervene=True,
        action_id=chosen.id,
        reason=f"{ctx.verdict.state} 且全部闸门通过",
        gate_trace=trace,
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_gates.py tests/test_actions.py tests/test_decider.py -v`
Expected: PASS，25 passed

- [ ] **Step 5: 提交**

```bash
git add src/statesense/intervention/gates.py src/statesense/intervention/actions.py src/statesense/intervention/decider.py tests/test_gates.py tests/test_actions.py tests/test_decider.py
git commit -m "feat(gate): 四条可插拔闸门 + 动作轮转 + 决策合成"
```

---

## Task 7: Wording（模板文案，预留 LLM 接口）

**Files:**
- Create: `src/statesense/intervention/wording.py`
- Test: `tests/test_wording.py`

**Interfaces:**
- Consumes: `StateVerdict`、`Action`
- Produces: `Wording` 协议（`render(verdict, action, top_label) -> str`）；`TemplateWording()`

- [ ] **Step 1: 写失败的测试**

`tests/test_wording.py`：

```python
from statesense.config import Action
from statesense.intervention.wording import TemplateWording
from statesense.state.models import State, StateVerdict

WALK = Action("walk5", "离开电脑走 5 分钟", ("PASSIVE_CONSUMPTION",))


def _verdict(state=State.PASSIVE_CONSUMPTION, *, ent=45.0, total=60.0, ratio=0.75,
             late_night=False, window=60) -> StateVerdict:
    return StateVerdict(
        state=state, late_night=late_night, total_active_minutes=total, ent_minutes=ent,
        gray_minutes=5.0, work_minutes=10.0, ent_ratio=ratio, window_minutes=window,
        data_status="ok", skipped=False, skip_reason=None,
    )


def test_render_includes_numbers_and_action():
    text = TemplateWording().render(_verdict(), WALK, "【某视频】_哔哩哔哩_bilibili")
    assert "45" in text
    assert "75%" in text
    assert "离开电脑走 5 分钟" in text


def test_render_uses_configured_window_minutes():
    text = TemplateWording().render(_verdict(window=30), WALK, "B站")
    assert "30 分钟" in text


def test_render_truncates_long_window_titles():
    text = TemplateWording().render(_verdict(), WALK, "标题" * 100)
    assert len(text) < 200
    assert "…" in text


def test_render_prepends_late_night_warning():
    text = TemplateWording().render(_verdict(late_night=True), WALK, "B站")
    assert "凌晨" in text
    assert text.index("凌晨") < text.index("离开电脑")


def test_render_works_without_top_label():
    text = TemplateWording().render(_verdict(), WALK, None)
    assert "离开电脑走 5 分钟" in text
    assert "None" not in text


def test_render_high_risk_uses_stronger_wording():
    high = TemplateWording().render(
        _verdict(state=State.HIGH_RISK_PASSIVE_CONSUMPTION, ent=70.0, ratio=0.95), WALK, "B站"
    )
    normal = TemplateWording().render(_verdict(), WALK, "B站")
    assert "70" in high
    assert high != normal


def test_render_never_mentions_screen_content():
    """文案只由数字与动作组成，不接受任何屏幕文本作为输入。"""
    text = TemplateWording().render(_verdict(), WALK, "B站")
    assert "B站" in text  # top_label 是窗口标题，属行为元数据
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_wording.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'statesense.intervention.wording'`

- [ ] **Step 3: 写最小实现**

`src/statesense/intervention/wording.py`：

```python
"""提醒文案。V0 用模板；将来接 LLM 只需替换这一个类，其他组件不用动。"""

from __future__ import annotations

from typing import Protocol

from statesense.config import Action
from statesense.state.models import State, StateVerdict

MAX_TITLE_CHARS = 28


class Wording(Protocol):
    def render(self, verdict: StateVerdict, action: Action, top_label: str | None) -> str: ...


def _shorten(text: str, limit: int = MAX_TITLE_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


class TemplateWording:
    """把数字写成一句人话。刻意保留具体分钟数与占比 —— 模糊的提醒没有说服力。"""

    def render(self, verdict: StateVerdict, action: Action, top_label: str | None) -> str:
        ent = int(round(verdict.ent_minutes))
        ratio = int(round(verdict.ent_ratio * 100))
        where = f"（{_shorten(top_label)}）" if top_label else ""

        parts: list[str] = []
        if verdict.late_night:
            parts.append("凌晨了。")
        parts.append(
            f"最近 {verdict.window_minutes} 分钟里有 {ent} 分钟在被娱乐内容占用{where}，占 {ratio}%。"
        )
        if verdict.state is State.HIGH_RISK_PASSIVE_CONSUMPTION:
            parts.append(f"这已经是「被困住」的量级了。{action.text}？")
        else:
            parts.append(f"{action.text}？")
        return "".join(parts)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_wording.py -v`
Expected: PASS，7 passed

- [ ] **Step 5: 提交**

```bash
git add src/statesense/intervention/wording.py tests/test_wording.py
git commit -m "feat(gate): 模板文案渲染，预留 Wording 接口给后续 LLM"
```

---

<details>
<summary>⚠️ <b>Task 8 旧版（Windows Toast）—— 已作废，仅供追溯</b></summary>

> 施行到 Task 8 时，真机实测推翻了 Toast 方案的**每一条**前提：
> 本机全局通知开关 `ToastEnabled=0`（Toast 被系统整体丢弃，而 `show()` 仍返回成功）、
> Windows 在全屏应用下抑制通知、Win10 需要已注册的 AUMID 而 winotify 并不自动注册、
> winotify 用 `Popen(..., stdout=DEVNULL, stderr=DEVNULL)` 把失败信息彻底丢弃。
>
> 已改为**原生前台弹窗**。见下方「Task 8 修订版」与 spec §8。

### （旧）Notifier（Windows Toast）

**Files:**
- Create: `src/statesense/notify/__init__.py`, `src/statesense/notify/base.py`, `src/statesense/notify/windows_toast.py`
- Test: `tests/test_notifier.py`

**Interfaces:**
- Consumes: `NotifyConfig`、`Clock`
- Produces: `DeliveryResult(status, channel, error, delivered_at)` 及类方法 `delivered` / `failed`、属性 `stored_status`；`Notifier` 协议；`RecordingNotifier`；`WindowsToastNotifier(config, clock)`

- [ ] **Step 1: 写失败的测试**

`tests/test_notifier.py`：

```python
from datetime import datetime, timezone

from statesense.clock import FrozenClock
from statesense.config import NotifyConfig
from statesense.notify.base import DeliveryResult, RecordingNotifier
from statesense.notify.windows_toast import WindowsToastNotifier

T0 = datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)


def test_delivery_result_helpers():
    ok = DeliveryResult.delivered("windows_toast", T0)
    assert ok.status == "delivered"
    assert ok.error is None
    assert ok.stored_status == "delivered"
    bad = DeliveryResult.failed("windows_toast", "toast unavailable")
    assert bad.status == "failed"
    assert bad.error == "toast unavailable"
    assert bad.delivered_at is None
    assert bad.stored_status == "failed:toast unavailable"


def test_recording_notifier_captures_messages():
    n = RecordingNotifier()
    result = n.notify("标题", "正文")
    assert result.status == "delivered"
    assert n.sent == [("标题", "正文")]


def test_windows_notifier_reports_failure_instead_of_raising(monkeypatch):
    """投递失败必须变成结构化结果，绝不能让调度器崩掉。"""

    class Boom:
        def __init__(self, **kwargs):
            raise RuntimeError("no AppUserModelID")

    monkeypatch.setattr("statesense.notify.windows_toast._notification_class", lambda: Boom)
    result = WindowsToastNotifier(NotifyConfig(), FrozenClock(T0)).notify("标题", "正文")
    assert result.status == "failed"
    assert "no AppUserModelID" in (result.error or "")
    assert result.delivered_at is None


def test_windows_notifier_reports_success(monkeypatch):
    sent: list = []

    class Fake:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def show(self):
            sent.append(self.kwargs)

    monkeypatch.setattr("statesense.notify.windows_toast._notification_class", lambda: Fake)
    result = WindowsToastNotifier(
        NotifyConfig(app_id="StateSense.Agent"), FrozenClock(T0)
    ).notify("标题", "正文")
    assert result.status == "delivered"
    assert result.delivered_at == T0
    assert sent[0]["title"] == "标题"
    assert sent[0]["msg"] == "正文"
    assert sent[0]["app_id"] == "StateSense.Agent"
    assert sent[0]["duration"] == "short"


def test_windows_notifier_passes_long_duration(monkeypatch):
    sent: list = []

    class Fake:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def show(self):
            sent.append(self.kwargs)

    monkeypatch.setattr("statesense.notify.windows_toast._notification_class", lambda: Fake)
    WindowsToastNotifier(NotifyConfig(toast_duration="long"), FrozenClock(T0)).notify("t", "b")
    assert sent[0]["duration"] == "long"


def test_show_failure_is_reported(monkeypatch):
    class FailsOnShow:
        def __init__(self, **kwargs):
            pass

        def show(self):
            raise OSError("shell not available")

    monkeypatch.setattr("statesense.notify.windows_toast._notification_class", lambda: FailsOnShow)
    result = WindowsToastNotifier(NotifyConfig(), FrozenClock(T0)).notify("t", "b")
    assert result.status == "failed"
    assert "shell not available" in (result.error or "")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_notifier.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'statesense.notify'`

- [ ] **Step 3: 写最小实现**

`src/statesense/notify/__init__.py`：空文件。

`src/statesense/notify/base.py`：

```python
"""投递契约。失败必须返回结构化错误，绝不静默吞掉。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol


@dataclass(frozen=True)
class DeliveryResult:
    status: str
    channel: str
    error: str | None
    delivered_at: datetime | None

    @classmethod
    def delivered(cls, channel: str, at: datetime) -> "DeliveryResult":
        return cls(status="delivered", channel=channel, error=None, delivered_at=at)

    @classmethod
    def failed(cls, channel: str, error: str) -> "DeliveryResult":
        return cls(status="failed", channel=channel, error=error, delivered_at=None)

    @property
    def stored_status(self) -> str:
        """入库用字符串。失败原因必须保留，否则回执与重试无从判断。"""
        return "delivered" if self.status == "delivered" else f"failed:{self.error}"


class Notifier(Protocol):
    channel: str

    def notify(self, title: str, body: str) -> DeliveryResult: ...


class RecordingNotifier:
    """测试与 --dry-run 用：不发真通知，只记下来。"""

    channel = "recording"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def notify(self, title: str, body: str) -> DeliveryResult:
        self.sent.append((title, body))
        return DeliveryResult.delivered(self.channel, datetime.now(timezone.utc))
```

`src/statesense/notify/windows_toast.py`：

```python
"""Windows Toast 投递。

Windows 10（Build 19045）的 Toast 必须绑定已注册的 AUMID，否则静默失败；
winotify 会自动在开始菜单创建带 AppUserModelID 的快捷方式。

已知局限：Windows 在「全屏应用」下默认抑制通知，而本项目的核心场景恰恰是
全屏刷视频。该场景需在真机实测；备用方案见 spec §8.3。
"""

from __future__ import annotations

from typing import Any

from statesense.clock import Clock
from statesense.config import NotifyConfig

from .base import DeliveryResult


def _notification_class() -> Any:
    """延迟导入：便于测试替换，也让非 Windows 平台能导入本模块。"""
    from winotify import Notification

    return Notification


class WindowsToastNotifier:
    channel = "windows_toast"

    def __init__(self, config: NotifyConfig, clock: Clock) -> None:
        self._config = config
        self._clock = clock

    def notify(self, title: str, body: str) -> DeliveryResult:
        try:
            factory = _notification_class()
            toast = factory(
                app_id=self._config.app_id,
                title=title,
                msg=body,
                duration=self._config.toast_duration,
            )
            toast.show()
        except Exception as exc:  # noqa: BLE001 - 投递失败不能拖垮调度器
            return DeliveryResult.failed(self.channel, f"{exc.__class__.__name__}: {exc}")
        return DeliveryResult.delivered(self.channel, self._clock.now())
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_notifier.py -v`
Expected: PASS，6 passed

- [ ] **Step 5: 在真机上实测全屏场景（本计划最高风险项）**

Run:

```bash
uv run python -c "
from statesense.clock import SystemClock
from statesense.config import NotifyConfig
from statesense.notify.windows_toast import WindowsToastNotifier
print(WindowsToastNotifier(NotifyConfig(), SystemClock()).notify('StateSense 投递测试', '看到这条说明 Toast 通道可用。'))
"
```

Expected: 右下角出现通知，且输出 `DeliveryResult(status='delivered', ...)`。

然后**打开一个全屏视频**，重复执行同一条命令，记录通知是否可见。

把两次结果写进 `docs/specs/2026-09-16-v0-state-intervention-design.md` 的 §8.3 末尾。若全屏下不可见，把 `config.example.toml` 的 `fallback_topmost_window` 改为 `true`，并在 spec §16 风险表第 1 条标注「已复现」。

- [ ] **Step 6: 提交**

```bash
git add src/statesense/notify tests/test_notifier.py docs/specs/2026-09-16-v0-state-intervention-design.md config/config.example.toml
git commit -m "feat(notify): Windows Toast 投递（结构化失败）并回填全屏实测结果"
```

---

</details>

---

## Task 8 修订版: Notifier（原生前台弹窗）—— 已实施

> 本节记录**实际落地**的版本。实施结果见 commit `220bcea`。

**Files:**
- Create: `src/statesense/notify/__init__.py`
- Create: `src/statesense/notify/base.py`
- Create: `src/statesense/notify/win32_popup.py`
- Create: `src/statesense/notify/foreground_popup.py`
- Delete: `src/statesense/notify/windows_toast.py`
- Test: `tests/test_notifier.py`

**Interfaces:**
- Consumes: `NotifyConfig`（`channel` / `answer_timeout_seconds` / `foreground_timeout_seconds`）、`Clock`
- Produces:
  - `DeliveryResult(status, channel, error, delivered_at, user_response=None)`，类方法 `delivered(channel, at, user_response=None)` / `failed(channel, error)`，属性 `stored_status`
  - 常量 `RESPONSE_ACCEPTED = "accepted"`、`RESPONSE_DECLINED = "declined"`
  - `Notifier` 协议；`RecordingNotifier(response=None)`
  - `Win32Popup()`：`show(title, body) -> int`、`find(title) -> int`、`force_front(title, timeout) -> bool`、`close(title) -> bool`、`beep()`
  - `ForegroundPopupNotifier(config, clock, popup=None)`，方法 `notify(title, body) -> DeliveryResult`

**关键实现点：**

1. **弹窗必须在独立线程创建** —— `MessageBoxW` 阻塞。
2. **必须显式抢前台** —— 只加 `MB_TOPMOST | MB_SETFOREGROUND` 时，后台进程会被「前台锁定」挡住，窗口被创建却压在全屏应用后面（只闻其声、不见其形）。实测矩阵：

   | 场景 | 结果 |
   |---|---|
   | 窗口模式 | 可见 |
   | 全屏模式，仅 `MB_TOPMOST` | **不可见** |
   | 全屏模式，加显式抢前台 | **可见**（`attached=True setforeground=True`） |

   抢前台序列：`FindWindowW` → `ShowWindow(SW_SHOW)` → `SetWindowPos(HWND_TOPMOST)`
   → `AttachThreadInput` → `BringWindowToTop` → `SetForegroundWindow` → `FlashWindow`。

3. **ctypes 必须声明 `restype`** —— 不声明的话 64 位下 `HWND` 会被截断成 `int`。

4. **一次只留一个窗口** —— 显示前先 `close(title)` 清掉同标题残留。

5. **超时关闭后回执必须是 `None`** —— 程序关闭对话框后返回的按钮 id 不是用户的意思。
   这是实施中真实踩到的缺陷（第一版把「程序关闭」记成了用户点了「否」）。

6. **关闭顺序**（每试一种都确认窗口是否真的消失）：
   `WM_COMMAND(IDNO)` → `WM_COMMAND(IDCANCEL)` → `WM_CLOSE` → `WM_SYSCOMMAND(SC_CLOSE)`。
   只用 `WM_CLOSE` 不够：`MB_YESNO` 没有取消按钮时关闭按钮会被禁用。

**真机验证结果**（2026-09-16）：

```
弹窗 1（点【是】）  status=delivered user_response=accepted
弹窗 2（不点）      status=delivered user_response=None 耗时=10.4s 窗口句柄=0
```

`窗口句柄=0` 是关键证据：程序化关闭确实生效，而不是靠进程退出把窗口带走。

**连带改动：**
- `pyproject.toml`：运行期依赖降为 `[]`（去掉 winotify）
- `config.py`：`NotifyConfig` 改为 `channel="foreground_popup"` / `answer_timeout_seconds=180` / `foreground_timeout_seconds=15`
- `store/schema.sql`：`interventions` 增加 `user_response TEXT`（schema v2）
- `store/db.py`：`SCHEMA_VERSION = 2`，`_apply_incremental_migrations()` 给 v1 老库补列
- `store/db.py`：`insert_intervention(..., user_response=None)`，新增 `fetch_intervention()`

**测试覆盖**（`tests/test_notifier.py`，13 项）：按钮映射（`IDYES`/`IDNO`/未知值不猜）、
残留清理、beep 与抢前台被调用、超时关闭且回执为空、`show()` 抛异常变成结构化失败。

---

## Task 9: OutcomeTracker（行为回执）

**Files:**
- Create: `src/statesense/outcome/tracker.py`
- Test: `tests/test_outcome.py`

**Interfaces:**
- Consumes: `ActivitySnapshot`、`OutcomeConfig`
- Produces: `label(before, after, config) -> str`；`evaluate(before, after, ent_minutes, config) -> OutcomeVerdict`

- [ ] **Step 1: 写失败的测试**

`tests/test_outcome.py`：

```python
from datetime import datetime, timedelta, timezone

from statesense.activity.models import ActivitySnapshot, Entry
from statesense.config import OutcomeConfig
from statesense.outcome.tracker import evaluate, label

T0 = datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)
CFG = OutcomeConfig(delay_minutes=10, disengaged_ratio=0.5, continued_ratio=0.8)


def _snap(ent_minutes: float, data_status: str = "ok") -> ActivitySnapshot:
    return ActivitySnapshot(
        window_start=T0,
        window_end=T0 + timedelta(minutes=10),
        window_minutes=10,
        total_active_minutes=10.0,
        entries=(Entry("chrome.exe", "哔哩哔哩_bilibili", "", ent_minutes),),
        data_status=data_status,
        captured_at=T0 + timedelta(minutes=10),
    )


def _ent(snapshot: ActivitySnapshot) -> float:
    return sum(e.minutes for e in snapshot.entries)


# ── 标签边界（45 → 22.5 / 36.0）─────────────────────────────

def test_disengaged_when_drop_is_steep():
    assert label(45.0, 10.0, CFG) == "disengaged"


def test_boundary_at_exactly_half_is_partial():
    """0.5 * 45 = 22.5，按「严格小于」处理，恰好相等算 partial。"""
    assert label(45.0, 22.5, CFG) == "partial"


def test_partial_in_the_middle():
    assert label(45.0, 30.0, CFG) == "partial"


def test_boundary_at_continued_ratio_is_continued():
    """0.8 * 45 = 36.0，恰好相等算 continued。"""
    assert label(45.0, 36.0, CFG) == "continued"


def test_continued_when_activity_persists():
    assert label(45.0, 44.0, CFG) == "continued"


def test_more_activity_after_is_still_continued():
    assert label(45.0, 60.0, CFG) == "continued"


# ── 完整评估 ────────────────────────────────────────────────

def test_evaluate_returns_raw_values_and_label():
    verdict = evaluate(_snap(45.0), _snap(12.0), _ent, CFG)
    assert verdict.outcome == "disengaged"
    assert verdict.ent_before == 45.0
    assert verdict.ent_after == 12.0
    assert verdict.after_window_minutes == 10


def test_evaluate_returns_no_data_when_after_snapshot_is_untrustworthy():
    verdict = evaluate(_snap(45.0), _snap(0.0, data_status="no_capture_in_range"), _ent, CFG)
    assert verdict.outcome == "no_data"
    assert verdict.ent_before == 45.0


def test_evaluate_returns_no_data_when_before_snapshot_is_untrustworthy():
    verdict = evaluate(_snap(45.0, data_status="unreachable"), _snap(12.0), _ent, CFG)
    assert verdict.outcome == "no_data"


def test_zero_before_is_no_data_not_disengaged():
    """理论上前提是 ent>=40，但真出现 0 时不能当成「干预成功」。"""
    assert evaluate(_snap(0.0), _snap(0.0), _ent, CFG).outcome == "no_data"


def test_raw_values_are_recorded_even_when_no_data():
    verdict = evaluate(_snap(45.0), _snap(12.0, data_status="unreachable"), _ent, CFG)
    assert verdict.ent_before == 45.0
    assert verdict.ent_after == 12.0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_outcome.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'statesense.outcome.tracker'`

- [ ] **Step 3: 写最小实现**

`src/statesense/outcome/tracker.py`：

```python
"""行为回执：干预后复查同一窗口，看被动消费是否下降。

原始值 ent_before / ent_after 必须入库，标签只是派生 —— 「多低才算有效」
这个判断以后可能会改，原始数据不能丢。
"""

from __future__ import annotations

from collections.abc import Callable

from statesense.activity.models import ActivitySnapshot
from statesense.config import OutcomeConfig

from .models import OutcomeVerdict

NO_DATA = "no_data"


def label(before: float, after: float, config: OutcomeConfig) -> str:
    if after < before * config.disengaged_ratio:
        return "disengaged"
    if after < before * config.continued_ratio:
        return "partial"
    return "continued"


def evaluate(
    before: ActivitySnapshot,
    after: ActivitySnapshot,
    ent_minutes: Callable[[ActivitySnapshot], float],
    config: OutcomeConfig,
) -> OutcomeVerdict:
    ent_before = ent_minutes(before)
    ent_after = ent_minutes(after)
    window_minutes = float(after.window_minutes)

    # 任一侧采集中断，或干预前根本没有被动消费，都不能当结论。
    if not before.is_trustworthy or not after.is_trustworthy or ent_before <= 0:
        return OutcomeVerdict(
            outcome=NO_DATA,
            ent_before=ent_before,
            ent_after=ent_after,
            after_window_minutes=window_minutes,
        )

    return OutcomeVerdict(
        outcome=label(ent_before, ent_after, config),
        ent_before=ent_before,
        ent_after=ent_after,
        after_window_minutes=window_minutes,
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_outcome.py -v`
Expected: PASS，11 passed

- [ ] **Step 5: 提交**

```bash
git add src/statesense/outcome/tracker.py tests/test_outcome.py
git commit -m "feat(outcome): 行为回执判定，原始值入库、标签派生"
```

---

## Task 10: Scheduler 与命令行入口

**Files:**
- Create: `src/statesense/scheduler.py`, `src/statesense/__main__.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: 前九个任务的全部组件
- Produces:
  - `TickReport(evaluation_id, state, intervened, outcomes_closed, note)`
  - `Scheduler(config, clock, reader, store, notifier, wording=None)`，方法 `run_once() -> TickReport`、`run_forever() -> None`、`ent_minutes(snapshot) -> float`
  - `main(argv=None) -> int`

- [ ] **Step 1: 写失败的测试**

`tests/test_scheduler.py`：

```python
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from statesense.activity.reader import ActivityReader
from statesense.clock import FrozenClock
from statesense.config import load_config
from statesense.notify.base import RecordingNotifier
from statesense.scheduler import Scheduler
from statesense.store.db import Store

T0 = datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)
REPO = Path(__file__).resolve().parents[1]


def _body(ent_minutes: float, total: float = 60.0, status: str = "ok") -> bytes:
    payload = {
        "total_active_minutes": total,
        "data_status": status,
        "windows": [
            {
                "app_name": "chrome.exe",
                "window_name": "【某视频】_哔哩哔哩_bilibili",
                "browser_url": "https://www.bilibili.com/video/BV1",
                "minutes": ent_minutes,
            },
            {
                "app_name": "Code.exe",
                "window_name": "engine.py - Visual Studio Code",
                "browser_url": "",
                "minutes": max(total - ent_minutes, 0.0),
            },
        ],
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


@pytest.fixture()
def config(tmp_path):
    src = (REPO / "config" / "config.example.toml").read_text(encoding="utf-8")
    src = src.replace('path = "statesense.db"', f'path = "{tmp_path / "s.db"}"')
    target = tmp_path / "config.toml"
    target.write_text(src, encoding="utf-8")
    return load_config(target)


@pytest.fixture()
def store(config):
    s = Store(config.store_path)
    s.migrate()
    yield s
    s.close()


def _scheduler(config, store, bodies: list[bytes], clock, notifier=None) -> Scheduler:
    queue = list(bodies)

    def fake_get(url, headers, timeout):
        body = queue.pop(0) if queue else _body(0.0, 0.0)
        return 200, body

    return Scheduler(
        config=config,
        clock=clock,
        reader=ActivityReader("http://localhost:3030", "k", 10.0, fake_get),
        store=store,
        notifier=notifier or RecordingNotifier(),
    )


def test_run_once_records_normal_evaluation_without_intervening(config, store):
    report = _scheduler(config, store, [_body(5.0)], FrozenClock(T0)).run_once()
    assert report.state == "NORMAL"
    assert report.intervened is False
    assert store.fetch_evaluation(report.evaluation_id)["decision"] == "skip"


def test_run_once_intervenes_at_threshold_and_records_delivery(config, store):
    notifier = RecordingNotifier()
    report = _scheduler(config, store, [_body(45.0)], FrozenClock(T0), notifier).run_once()
    assert report.state == "PASSIVE_CONSUMPTION"
    assert report.intervened is True
    assert len(notifier.sent) == 1
    assert "45" in notifier.sent[0][1]


def test_ratio_gate_blocks_mixed_activity(config, store):
    """42 / 60 = 0.70 < 0.75，状态成立但闸门挡下。"""
    report = _scheduler(config, store, [_body(42.0)], FrozenClock(T0)).run_once()
    assert report.state == "PASSIVE_CONSUMPTION"
    assert report.intervened is False
    assert "ratio_min" in report.note


def test_data_status_not_ok_skips_without_concluding(config, store):
    report = _scheduler(
        config, store, [_body(0.0, 0.0, status="no_capture_in_range")], FrozenClock(T0)
    ).run_once()
    assert report.intervened is False
    assert "skip" in report.note


def test_cooldown_blocks_second_intervention(config, store):
    clock = FrozenClock(T0)
    sch = _scheduler(config, store, [_body(45.0), _body(45.0)], clock)
    assert sch.run_once().intervened is True
    clock.advance(minutes=5)
    assert sch.run_once().intervened is False


def test_cooldown_releases_after_window(config, store):
    clock = FrozenClock(T0)
    sch = _scheduler(config, store, [_body(45.0), _body(45.0)], clock)
    assert sch.run_once().intervened is True
    clock.advance(minutes=31)
    assert sch.run_once().intervened is True


def test_outcome_is_closed_after_delay(config, store):
    clock = FrozenClock(T0)
    # body 顺序：① 首轮评估（45）② 回执的 before 窗口（45）③ 回执的 after 窗口（3）
    sch = _scheduler(config, store, [_body(45.0), _body(45.0), _body(3.0)], clock)
    sch.run_once()
    iid = store._conn.execute("SELECT id FROM interventions").fetchone()["id"]
    clock.advance(minutes=10)
    report = sch.run_once()
    assert report.outcomes_closed == 1
    row = store.fetch_outcome(iid)
    assert row is not None
    assert row["outcome"] == "disengaged"
    assert row["ent_before"] == 45.0
    assert row["ent_after"] == 3.0


def test_outcome_not_closed_before_delay(config, store):
    clock = FrozenClock(T0)
    sch = _scheduler(config, store, [_body(45.0), _body(45.0)], clock)
    sch.run_once()
    clock.advance(minutes=9)
    assert sch.run_once().outcomes_closed == 0


def test_action_rotation_advances_between_interventions(config, store):
    clock = FrozenClock(T0)
    sch = _scheduler(config, store, [_body(45.0), _body(45.0)], clock)
    sch.run_once()
    clock.advance(minutes=31)
    sch.run_once()
    rows = store._conn.execute("SELECT action_id FROM interventions ORDER BY id").fetchall()
    assert len(rows) == 2
    assert rows[0]["action_id"] != rows[1]["action_id"]


def test_due_outcomes_are_closed_even_when_current_tick_skips(config, store):
    clock = FrozenClock(T0)
    sch = _scheduler(
        config, store, [_body(45.0), _body(0.0, 0.0, "unreachable"), _body(2.0)], clock
    )
    sch.run_once()
    clock.advance(minutes=10)
    report = sch.run_once()
    assert report.outcomes_closed == 1
    assert report.intervened is False


def test_failed_delivery_does_not_start_cooldown(config, store):
    """投递失败不该消耗冷却 —— 用户根本没被打扰到。"""

    class Failing:
        channel = "failing"

        def notify(self, title, body):
            from statesense.notify.base import DeliveryResult

            return DeliveryResult.failed(self.channel, "boom")

    clock = FrozenClock(T0)
    sch = _scheduler(config, store, [_body(45.0), _body(45.0)], clock, Failing())
    assert sch.run_once().intervened is True
    clock.advance(minutes=1)
    assert sch.run_once().intervened is True
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_scheduler.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'statesense.scheduler'`

- [ ] **Step 3: 写最小实现**

`src/statesense/scheduler.py`：

```python
"""tick 主循环。一次 tick 的顺序：先补回执，再评估，最后（可能）投递。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from statesense.activity.models import ActivitySnapshot
from statesense.activity.reader import ActivityReader
from statesense.clock import Clock
from statesense.config import Config
from statesense.intervention.actions import candidates
from statesense.intervention.decider import decide
from statesense.intervention.gates import GateContext
from statesense.intervention.wording import TemplateWording, Wording
from statesense.notify.base import Notifier
from statesense.outcome.tracker import evaluate as evaluate_outcome
from statesense.state.engine import classify
from statesense.state.taxonomy import Category, bucket_minutes
from statesense.store.db import Store

log = logging.getLogger(__name__)

ACTION_CURSOR_KEY = "action_cursor"


@dataclass(frozen=True)
class TickReport:
    evaluation_id: int | None
    state: str | None
    intervened: bool
    outcomes_closed: int
    note: str


class Scheduler:
    def __init__(
        self,
        config: Config,
        clock: Clock,
        reader: ActivityReader,
        store: Store,
        notifier: Notifier,
        wording: Wording | None = None,
    ) -> None:
        self._config = config
        self._clock = clock
        self._reader = reader
        self._store = store
        self._notifier = notifier
        self._wording = wording or TemplateWording()

    # ── 对外 ────────────────────────────────────────────────

    def ent_minutes(self, snapshot: ActivitySnapshot) -> float:
        """被动消费分钟数。回执比较与状态判定用的是同一个口径。"""
        buckets = bucket_minutes(snapshot.entries, self._config.taxonomy)
        return round(buckets[Category.ENTERTAINMENT], 2)

    def run_once(self) -> TickReport:
        now = self._clock.now()
        closed = self._close_due_outcomes(now)
        return self._evaluate_tick(now, closed)

    def run_forever(self) -> None:  # pragma: no cover - 常驻路径靠手动验证
        interval = timedelta(minutes=self._config.schedule.evaluate_every_minutes)
        last: datetime | None = None
        while True:
            now = self._clock.now()
            if last is None or now - last >= interval:
                self.run_once()
                last = now
            time.sleep(self._config.schedule.tick_seconds)

    # ── 内部 ────────────────────────────────────────────────

    def _close_due_outcomes(self, now: datetime) -> int:
        delay = self._config.outcome.delay_minutes
        closed = 0
        for due in self._store.due_interventions(now):
            before = self._reader.read(
                due.at - timedelta(minutes=delay), due.at, int(delay), now
            )
            after = self._reader.read(
                due.at, due.at + timedelta(minutes=delay), int(delay), now
            )
            verdict = evaluate_outcome(before, after, self.ent_minutes, self._config.outcome)
            self._store.insert_outcome(due.id, now, verdict)
            closed += 1
        return closed

    def _evaluate_tick(self, now: datetime, closed: int) -> TickReport:
        window = self._config.schedule.window_minutes
        snapshot = self._reader.read(now - timedelta(minutes=window), now, window, now)
        verdict = classify(snapshot, self._config.taxonomy, self._config.thresholds)

        day_start = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        ctx = GateContext(
            verdict=verdict,
            config=self._config.gate,
            now=now,
            last_intervention_at=self._store.last_intervention_at(),
            interventions_today=self._store.intervention_count_since(day_start),
        )
        last_action = self._store.get_kv(ACTION_CURSOR_KEY)
        pool = candidates(self._config.actions, verdict.state)
        decision = decide(ctx, pool, last_action)

        evaluation_id = self._store.insert_evaluation(now, verdict, decision)

        if not decision.intervene or decision.action_id is None:
            return TickReport(evaluation_id, str(verdict.state), False, closed, decision.reason)

        action = next(a for a in pool if a.id == decision.action_id)
        top_label = (
            max(snapshot.entries, key=lambda e: e.minutes).title if snapshot.entries else None
        )
        body = self._wording.render(verdict, action, top_label)
        result = self._notifier.notify("StateSense 轻推", body)

        self._store.insert_intervention(
            evaluation_id=evaluation_id,
            at=now,
            state=str(verdict.state),
            late_night=verdict.late_night,
            action_id=action.id,
            action_text=body,
            delivery_status=result.stored_status,
            outcome_due_at=now + timedelta(minutes=self._config.outcome.delay_minutes),
        )
        self._store.set_kv(ACTION_CURSOR_KEY, action.id)
        return TickReport(evaluation_id, str(verdict.state), True, closed, result.stored_status)
```

`src/statesense/__main__.py`：

```python
"""命令行入口：--check 自检 / --once 单轮 / --daemon 常驻。"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from statesense.activity.reader import ActivityReader
from statesense.clock import SystemClock
from statesense.config import Config, ConfigError, load_config
from statesense.notify.base import Notifier, RecordingNotifier
from statesense.notify.windows_toast import WindowsToastNotifier
from statesense.scheduler import Scheduler
from statesense.store.db import Store


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="statesense", description="StateSense-Agent V0")
    parser.add_argument("--config", type=Path, default=Path("config/config.toml"))
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="校验配置与 Screenpipe 连通性后退出")
    group.add_argument("--once", action="store_true", help="跑一轮评估")
    group.add_argument("--daemon", action="store_true", help="常驻运行")
    parser.add_argument("--dry-run", action="store_true", help="不真发通知，只记录")
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


def make_notifier(config: Config, dry_run: bool) -> Notifier:
    if dry_run:
        return RecordingNotifier()
    if config.notify.channel != "windows_toast":
        raise ConfigError(f"不支持的 notify.channel: {config.notify.channel}")
    return WindowsToastNotifier(config.notify, SystemClock())


def build_scheduler(config: Config, notifier: Notifier) -> Scheduler:
    store = Store(config.store_path)
    store.migrate()
    return Scheduler(
        config=config,
        clock=SystemClock(),
        reader=ActivityReader(
            base_url=config.screenpipe.base_url,
            api_key=resolve_api_key(config),
            timeout=config.screenpipe.request_timeout_sec,
        ),
        store=store,
        notifier=notifier,
    )


def check(config: Config) -> int:
    scheduler = build_scheduler(config, RecordingNotifier())
    snapshot = scheduler._reader.read(  # noqa: SLF001 - 自检就是要看这一次真实取数
        datetime_now := SystemClock().now(),
        datetime_now,
        config.schedule.window_minutes,
        datetime_now,
    )
    print(f"配置      OK（store={config.store_path}）")
    print(f"ratio_min {config.gate.ratio_min}")
    print(f"data_status {snapshot.data_status}")
    print(f"取到 {len(snapshot.entries)} 条窗口记录，总活跃 {snapshot.total_active_minutes} 分钟")
    if snapshot.data_status != "ok":
        print("警告：data_status 不是 ok，此时任何状态结论都不成立", file=sys.stderr)
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
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    try:
        if args.check:
            return check(config)
        scheduler = build_scheduler(config, make_notifier(config, args.dry_run))
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest -v`
Expected: PASS，全绿（累计 100+ 项）

- [ ] **Step 5: 提交**

```bash
git add src/statesense/scheduler.py src/statesense/__main__.py tests/test_scheduler.py
git commit -m "feat(scheduler): tick 主循环与命令行入口（--check/--once/--daemon）"
```

---

## Task 11: 文档同步与端到端人工验证

**Files:**
- Modify: `docs/specs/2026-09-16-v0-state-intervention-design.md`
- Modify: `README.md`
- Create: `docs/plans/2026-09-16-v0-verification-log.md`

**Interfaces:**
- Consumes: 全部
- Produces: 记录在案的验证结论

- [ ] **Step 1: 跑真实端到端（不发通知）**

```bash
export SCREENPIPE_LOCAL_API_KEY="$(screenpipe auth token)"
uv run python -m statesense --config config/config.toml --check
uv run python -m statesense --config config/config.toml --once --dry-run
uv run python -m statesense --config config/config.toml --once --dry-run -v
```

Expected：`--check` 输出 `data_status ok`；`--once` 输出 `state=... intervened=...`。

- [ ] **Step 2: 用真实数据核对判定是否符合直觉**

```bash
uv run python - <<'PY'
import sqlite3, json, pathlib
db = pathlib.Path("config/statesense.db")
con = sqlite3.connect(db); con.row_factory = sqlite3.Row
for r in con.execute("SELECT * FROM evaluations ORDER BY id DESC LIMIT 5"):
    print(r["at"], r["state"], f'ent={r["ent_minutes"]}', f'ratio={r["ent_ratio"]}',
          r["decision"], r["data_status"])
    for g in json.loads(r["gate_trace"]):
        print("   ", g["name"], "pass" if g["passed"] else "BLOCK", g["value"], "vs", g["threshold"])
PY
```

Expected：每条评估的四条闸门与判定结果都打印出来，且 `ratio_min` 的值与 0.75 对照合理。

- [ ] **Step 3: 把结论写进验证日志**

创建 `docs/plans/2026-09-16-v0-verification-log.md`，内容至少包含：

- 运行环境（Windows 版本、Screenpipe 版本、Python 版本）
- `--check` 的实际输出
- 至少 5 条真实评估记录（时间、状态、ent、ratio、decision、被哪条闸门挡下）
- 全屏场景下 Toast 可达性的实测结论
- 与预期的偏差，以及由此需要修改的配置项

- [ ] **Step 4: 回填 spec 的三处待定项**

编辑 `docs/specs/2026-09-16-v0-state-intervention-design.md`：

1. §6.2 与 §12：把 `gate.ratio_min` 从「待填写」改为确定值 **0.75**，并保留「缺失时启动失败」的约束。
2. §10：补记 `kv` 表（存放动作池轮转游标），说明这是对原三表设计的一处补充。
3. §16 风险表：按实测结果更新第 1、2 条的状态。
4. §2 成功判据：追加指向 `docs/plans/2026-09-16-v0-verification-log.md` 的链接。

同时在 `README.md` 的文档索引里加入实现计划与验证日志两行。

- [ ] **Step 5: 提交**

```bash
git add docs/specs/2026-09-16-v0-state-intervention-design.md docs/plans/2026-09-16-v0-verification-log.md README.md
git commit -m "docs: 回填 ratio_min=0.75、kv 表与全屏实测结论，新增验证日志"
```

---

## Self-Review

**1. Spec 覆盖检查**

| Spec 章节 | 对应任务 |
|---|---|
| §4.1–4.4 架构与边界 | Task 1（骨架）、Task 10（装配）；`ActivityReader` 单点依赖在 Task 2 |
| §5.1 输入契约 | Task 2 |
| §5.2 分类三档 | Task 3 |
| §5.3 状态定义 20/40/65 | Task 4 |
| §5.4 late_night 正交 | Task 4（`test_late_night_is_independent_of_state`） |
| §5.5 WORKING 不判定 | Task 3（`WORK` 只进 buckets，不进状态机） |
| §6.1 可插拔闸门 + gateTrace | Task 6 |
| §6.2 只启用窗口占比、值 0.75 | Task 1（配置）、Task 6（闸门）、Task 11（回填文档） |
| §7.1 动作池 | Task 1（配置）、Task 6（`candidates`） |
| §7.2 轮转选择 | Task 6（`pick`）、Task 10（游标持久化） |
| §7.3 Wording 接口 | Task 7 |
| §8.1 投递契约 | Task 8 |
| §8.2 Win10 AUMID | Task 8（`winotify` + 真机实测步骤） |
| §8.3 降级链 | Task 8 Step 5（决定 `fallback_topmost_window`）—— **未实现置顶窗口本身，见下方已知差距** |
| §9 行为回执 | Task 9、Task 10 |
| §10 数据模型 | Task 5（含 `kv` 补充，Task 11 回填） |
| §11 错误与边界 | Task 2（HTTP/JSON/超时）、Task 4（data_status）、Task 8（投递失败）、Task 9（no_data） |
| §12 配置 | Task 1 |
| §13 测试策略 | 每个任务的测试步骤 |
| §14 部署常驻 | **未包含任务，见下方已知差距** |
| §15 运行时前置条件 | Task 1 Step 4 之前需先按 spec §15 准备环境；Task 11 Step 1 验证 |
| §19 目录结构 | File Structure 表 |
| §20 实施顺序 | 任务顺序一致 |

**已知差距（有意留到 V0 之后，不是本计划的缺陷）：**

- §8.3 的「置顶无边框窗口」兜底通道：本计划只实现开关与实测决策，不实现该窗口。若 Task 8 Step 5 实测发现全屏下 Toast 不可达，需要另开一个任务补齐，届时再决定是否纳入 V0。
- §14 的任务计划程序注册：属于部署动作而非代码，本计划在 Task 11 之后由人工执行，命令为
  `schtasks /Create /TN StateSenseAgent /TR "uv run python -m statesense --daemon" /SC ONLOGON /RL LIMITED /F`。
  未纳入任务清单是因为它依赖仓库绝对路径，属于机器相关配置，不应写死在计划里。

**2. 占位符扫描**

已检查：全文无 `TBD`、`TODO`、`implement later`、`fill in details`。所有代码步骤均给出可直接粘贴的完整代码。Task 8 Step 5 与 Task 11 的三处「按实测结果填写」属于**刻意留给运行结果的记录项**，不是缺失的实现内容——相应的代码路径（`fallback_topmost_window` 开关）已在 Task 1 与 Task 8 中完整实现。

**3. 类型一致性检查**

| 名称 | 定义处 | 使用处 | 一致 |
|---|---|---|---|
| `StateVerdict.window_minutes` | Task 4 | Task 5 `insert_evaluation`、Task 7 `render` | ✓ |
| `DeliveryResult.stored_status` | Task 8 | Task 10 `insert_intervention` | ✓ |
| `GateContext` 五个字段 | Task 6 | Task 10 构造处 | ✓ |
| `candidates(actions, state)` | Task 6 | Task 10 调用 | ✓ |
| `decide(ctx, actions, last_action_id)` | Task 6 | Task 10 调用 | ✓ |
| `Scheduler.ent_minutes(snapshot)` | Task 10 | Task 10 `_close_due_outcomes` 传给 `evaluate_outcome` | ✓ |
| `Store.due_interventions(now) -> list[DueIntervention]` | Task 5 | Task 10 迭代 `.id` / `.at` | ✓ |
| `OutcomeVerdict(outcome, ent_before, ent_after, after_window_minutes)` | Task 5 | Task 9 构造、Task 5 `insert_outcome` 消费 | ✓ |
| `ActivityReader.read(start, end, window_minutes, captured_at)` | Task 2 | Task 10 三处调用 | ✓ |
| `load_config(path) -> Config` | Task 1 | Task 10 `main` | ✓ |

**4. 执行顺序依赖**

Task 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11，严格线性。Task 5 需要 Task 4 的 `StateVerdict`；Task 6 需要 Task 5 的 `GateResult`；Task 10 需要全部。每个任务的测试在自身提交后即可独立跑绿。
