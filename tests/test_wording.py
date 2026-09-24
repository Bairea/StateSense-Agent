"""模板文案的基线测试。输出与 v1 时代**逐字一致**——3.3 的完成条件是
「变化仅限文案」，模板就是那条不变的基线，它自己先不能漂。"""

from statesense.config import Action
from statesense.intervention.wording import TemplateWording, WordingContext
from statesense.state.models import State, StateVerdict

WALK = Action("walk5", "离开电脑走 5 分钟", ("PASSIVE_CONSUMPTION",))


def _verdict(state=State.PASSIVE_CONSUMPTION, *, ent=45.0, total=60.0, ratio=0.75,
             late_night=False, window=60) -> StateVerdict:
    return StateVerdict(
        state=state,
        late_night=late_night,
        total_active_minutes=total,
        ent_minutes=ent,
        gray_minutes=5.0,
        work_minutes=10.0,
        ent_ratio=ratio,
        entries_minutes=total,
        fullscreen_state=None,
        window_minutes=window,
        data_status="ok",
        skipped=False,
        skip_reason=None,
    )


def _context(**kwargs) -> WordingContext:
    """v2 起 Scheduler 构造上下文再调 render；测试用同一条构造路径。"""
    return WordingContext.from_verdict(
        _verdict(**kwargs), WALK, reminders_today=0, last_receipt=None
    )


def test_render_includes_numbers_and_action():
    text = TemplateWording().render(_context(), "【某视频】_哔哩哔哩_bilibili")
    assert "45" in text
    assert "75%" in text
    assert "离开电脑走 5 分钟" in text


def test_render_uses_configured_window_minutes():
    text = TemplateWording().render(_context(window=30), "B站")
    assert "30 分钟" in text


def test_render_truncates_long_window_titles():
    text = TemplateWording().render(_context(), "标题" * 100)
    assert len(text) < 200
    assert "…" in text


def test_render_prepends_late_night_warning():
    text = TemplateWording().render(_context(late_night=True), "B站")
    assert "凌晨" in text
    assert text.index("凌晨") < text.index("离开电脑")


def test_render_works_without_top_label():
    text = TemplateWording().render(_context(), None)
    assert "离开电脑走 5 分钟" in text
    assert "None" not in text


def test_render_high_risk_uses_stronger_wording():
    high = TemplateWording().render(
        _context(state=State.HIGH_RISK_PASSIVE_CONSUMPTION, ent=70.0, ratio=0.95), "B站"
    )
    normal = TemplateWording().render(_context(), "B站")
    assert "70" in high
    assert high != normal


def test_render_never_mentions_screen_content():
    """文案只由数字与动作组成，不接受任何屏幕文本作为输入。"""
    text = TemplateWording().render(_context(), "B站")
    assert "B站" in text  # top_label 是窗口标题，属行为元数据


def test_template_ignores_receipt_context():
    """模板**刻意不消费**回执上下文：它是「候选与基线只差文案」里的常量，
    行为跟着回执变是候选的事。 reminders_today / last_receipt 怎么变，
    输出都逐字相同——这条锁住「模板是基线」的语义。"""
    plain = WordingContext.from_verdict(
        _verdict(), WALK, reminders_today=0, last_receipt=None
    )
    seasoned = WordingContext.from_verdict(
        _verdict(), WALK, reminders_today=3, last_receipt="disengaged"
    )
    assert TemplateWording().render(plain, "B站") == TemplateWording().render(seasoned, "B站")
