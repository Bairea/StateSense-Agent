"""判定规则的版本标识。**「这一轮是用哪套规则判的」必须能被回答。**

为什么需要一个版本号：`evaluations` 里存的是判定**结果**，而判定所依据的
分类清单、状态阈值与全屏提权语义都不在行内。跨配置版本做阈值比较时（阶段 1.3），
两组行若来自不同规则，「阈值调优的效果」与「规则本身换了」会混成一件事，
而且从数据上完全看不出来。

版本涵盖什么、不涵盖什么，判据只有一条：**别的字段是否已经能复原它**。

  · 涵盖：分类清单（三个模式列表）、状态阶梯阈值、late_night 窗口、全屏提权规则。
    这些在库里没有任何痕迹 —— 改了它们，历史行不会变，也读不出当时的值。
  · 不涵盖：闸门参数（`ratio_min` / `cooldown` / `daily_cap` / `enabled`）。
    每条闸门在写入时就带着自己的值与阈值落进了 `gate_trace`，
    闸门口径的改变本来就可从行内复原，塞进版本号只是重复。

**这不是加密，也不是完整性校验。** 它只回答「是不是同一套规则」，
因此取短前缀即可：短到能一眼比对，长到碰撞不成问题。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from statesense.config import Config, TaxonomyConfig, ThresholdConfig
from statesense.perception import GAMING_STATES

#: 提权语义的手写标记。
#:
#: `effective_entertainment_minutes` 的语义写在代码里，配置里看不见 ——
#: 「只提权 OTHER」「数据不可信时不提权」这些规则若被改动，任何从配置算出的
#: 指纹都不会变，跨版本比较会静默把两套口径算成一件事。所以留一个人工开关：
#: 谁改了提权规则，谁就得改这一行。它是版本号的一部分，不是注释。
PROMOTION_RULE = "OTHER->ENTERTAINMENT@trustworthy_only"


def rule_version(
    taxonomy: TaxonomyConfig,
    thresholds: ThresholdConfig,
    *,
    gaming_states: Iterable[int] = GAMING_STATES,
    promotion: str = PROMOTION_RULE,
) -> str:
    """把一套判定规则压成 12 位十六进制标识。

    三条性质各自有理由：

      · **清单排序后参与。** 分类是「任一命中」，清单顺序不改变语义；
        不排序的话，调一下书写顺序就会换版本号，而那说明不了任何事。
      · **只取模式文本，不取编译标志。** 标志目前恒为 IGNORECASE，
        带进来只会让版本号跟着实现细节漂移。
      · **阈值按数值参与，不按文本。** `20` 与 `20.0` 是同一个阈值的两种写法。

    `gaming_states` 的默认值是真实的 `GAMING_STATES`，不是空集。空集看着「更中立」，
    实际后果是：默认调用算出的版本号**不含全屏规则**，而写上全屏规则的调用算出的
    又是另一个 —— 同一套规则两个版本号，且从结果上完全看不出差别。默认值必须
    等于真实语义，要让测试换取值就得显式传。
    """
    payload = {
        "entertainment": sorted(p.pattern for p in taxonomy.entertainment),
        "gray": sorted(p.pattern for p in taxonomy.gray),
        "work": sorted(p.pattern for p in taxonomy.work),
        "ladder": [
            float(thresholds.watch_minutes),
            float(thresholds.passive_minutes),
            float(thresholds.high_risk_minutes),
        ],
        "late_night": [
            int(thresholds.late_night_start_hour),
            int(thresholds.late_night_end_hour),
            float(thresholds.late_night_min_active_minutes),
        ],
        "gaming_states": sorted(int(state) for state in gaming_states),
        "promotion": promotion,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def version_of(config: Config) -> str:
    """从整份配置取版本。默认参数已等于真实语义，这里只是把调用点收成一处。"""
    return rule_version(config.taxonomy, config.thresholds)
