"""规则版本标识的性质（阶段 1.1 的代码任务）。

版本号只有一个用途：跨配置版本比较样本时，把「不同的规则」分开。
所以它的性质也只需要三条 —— 稳定、只随语义变、不变的东西不参与。
"""

from __future__ import annotations

import re
from dataclasses import replace

from statesense.config import Config, TaxonomyConfig, ThresholdConfig
from statesense.perception import GAMING_STATES
from statesense.rulebook import PROMOTION_RULE, rule_version, version_of


def test_version_is_stable_and_short(config: Config):
    first = version_of(config)
    assert first == version_of(config)
    assert len(first) == 12
    assert all(c in "0123456789abcdef" for c in first)


def test_version_ignores_pattern_order(config: Config):
    """分类是「任一命中」，清单顺序不改变语义，因此也不该改变版本号。

    不排序的话，调一下书写顺序就会换版本 —— 那样版本号就不再表示「规则变了」，
    而跨版本比较也会凭空多出一层，把同一套规则拆成两批样本。
    """
    shuffled = TaxonomyConfig(
        entertainment=tuple(reversed(config.taxonomy.entertainment)),
        gray=tuple(reversed(config.taxonomy.gray)),
        work=tuple(reversed(config.taxonomy.work)),
    )
    assert rule_version(shuffled, config.thresholds) == version_of(config)


def test_version_distinguishes_the_three_layers(config: Config):
    """至少要能区分分类清单、阈值、全屏规则的改变（计划 1.1 原文）。

    只区分其中两层不够用：阶段 1.4 要求「一次只上线一组规则变更」，
    两类变更共用一个版本号的话，「这次变化是因为改了哪一类」永远答不上来。
    """
    base = version_of(config)

    assert rule_version(
        TaxonomyConfig(
            entertainment=tuple(config.taxonomy.entertainment) + (re.compile("brotato"),),
            gray=config.taxonomy.gray,
            work=config.taxonomy.work,
        ),
        config.thresholds,
        gaming_states=GAMING_STATES,
    ) != base, "分类清单变了，版本号必须变"

    assert rule_version(
        config.taxonomy, replace(config.thresholds, passive_minutes=45), gaming_states=GAMING_STATES
    ) != base, "阈值变了，版本号必须变"

    assert rule_version(
        config.taxonomy, config.thresholds, gaming_states=(3,)
    ) != base, "全屏取值集合变了，版本号必须变"


def test_version_ignores_what_is_already_recoverable_from_the_rows(config: Config):
    """闸门参数不进版本号，理由是它们本来就逐行留在 `gate_trace` 里。

    版本号只该承载「库里读不出来的东西」。判据是「别的字段能不能复原它」——
    按这个判据，`ratio_min` / `cooldown` 之类不该进来；照抄一份配置全文
    则会让版本号随无关改动漂移，反而失去「规则变了没有」这个唯一含义。
    """
    different_gate = replace(config, gate=replace(config.gate, ratio_min=0.9))
    assert version_of(different_gate) == version_of(config)
    # 但提权语义是代码里的规则，库里读不出来，只能靠人工标记带进版本号。
    assert rule_version(
        config.taxonomy, config.thresholds, promotion="ANY->ENTERTAINMENT"
    ) != version_of(config)


def test_promotion_marker_is_part_of_the_version():
    """提权语义的改动必须能反映到版本号上 —— 这正是它存在的理由。

    这条断言看起来是同义反复，实则锁住一件事：`PROMOTION_RULE` 必须真的被算进去。
    哪天有人把它从 payload 里删掉（它看着像个常量、不像数据），
    提权规则改动就会静默地不改变版本号 —— 而那是最难发现的一类漂移。
    """
    tax = TaxonomyConfig(entertainment=(re.compile("bilibili"),), gray=(), work=())
    thresholds = ThresholdConfig()
    assert rule_version(tax, thresholds, promotion=PROMOTION_RULE) == rule_version(
        tax, thresholds
    )
    assert rule_version(tax, thresholds, promotion="别的语义") != rule_version(tax, thresholds)
    assert PROMOTION_RULE != ""
