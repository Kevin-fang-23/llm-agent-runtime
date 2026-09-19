"""token / 步数双维度预算控制。

策略：
  1. 任一维度超限 → 若是 token 超限且配置了便宜模型且尚未降级 → 降级续跑（budget_downgrade）；
  2. 否则 → 预算终止（budget_exceeded），带着已完成的 key_outputs 产出降级总结。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BudgetDecision:
    action: str  # ok | downgrade | exceeded
    reason: str = ""


def check_budget(
    tokens_used: int,
    steps_used: int,
    max_tokens: int,
    max_steps: int,
    downgraded: bool,
    has_cheap_model: bool,
) -> BudgetDecision:
    if steps_used >= max_steps:
        return BudgetDecision("exceeded", f"步数预算耗尽（{steps_used}/{max_steps}）")
    if tokens_used >= max_tokens:
        if has_cheap_model and not downgraded:
            return BudgetDecision("downgrade", f"token 预算超限（{tokens_used}/{max_tokens}），降级为便宜模型续跑")
        return BudgetDecision("exceeded", f"token 预算耗尽（{tokens_used}/{max_tokens}）")
    return BudgetDecision("ok")
