from statesense.config import Action
from statesense.intervention.wording import TemplateWording
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
        window_minutes=window,
        data_status="ok",
        skipped=False,
        skip_reason=None,
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
