"""瞬时错误的判别与退避策略。

判别与退避集中在这一处，供两个消费方共用，避免关键词表在两边各写一份后漂移：
  - `tool_executor_node`：命中瞬时错误时**原样重试**同一调用（指数退避 + 上限）
  - `critic_node`：把瞬时错误标为 retryable，交给模型层决策

⚠️ `looks_transient` 是**字符串嗅探**，脆弱且会随错误文案漂移。它只是一个显式收口的
   过渡实现；结构化错误码（工具返回 error_code / retryable）是后续工作。
   把标记表收在单一模块里，就是为了让那次替换只需改这一处。
   也正因为不可靠，自动重试**只认明确标记**（保守），不做「默认可疑即重试」——
   未命中的运行错仍按原路径交给 critic。
"""
from __future__ import annotations

# 瞬时错误标记（原先内联在 critic_node 里，为供自动重试复用而集中到这里，内容未改）
TRANSIENT_MARKERS = ("timeout", "超时", "connection", "网络", "temporarily")


def looks_transient(error_text: str) -> bool:
    """错误文本是否像瞬时故障而值得原样重试。保守判定：只认明确标记。"""
    lowered = (error_text or "").lower()
    return any(marker in lowered for marker in TRANSIENT_MARKERS)


def backoff_delay(attempt: int, base_delay_s: float, max_delay_s: float) -> float:
    """第 `attempt` 次重试前应等待的秒数（attempt 从 1 开始，指数增长并封顶）。

    不做抖动（jitter）：本运行时的并发重试量级是「单任务内的并行工具数」（个位数），
    不存在惊群场景；去掉抖动让延迟可精确断言，收益大于成本。
    """
    if attempt < 1:
        raise ValueError("attempt 从 1 开始")
    return min(base_delay_s * (2 ** (attempt - 1)), max_delay_s)
