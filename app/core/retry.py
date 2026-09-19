"""瞬时错误的判别与退避策略。

判定顺序（**结构化优先，文本兜底**）：
  1. 异常显式标注了 `retryable` → 直接采信
  2. 异常带结构化 `code`（且非 UNKNOWN）→ 查 `RETRYABLE_BY_CODE`
  3. 都没有 → 退回 `looks_transient` 的文本标记匹配

第 3 步是**过渡实现**：给尚未标注错误码的抛错点保底，使加固不改变既有行为。
新代码应当抛带 `code` 的 `ToolExecutionError`（见 `app/core/errors.py`），
这样"该不该重试"就是可枚举、可表驱动测试的确定性问题，而不是猜文本。

两个消费方共用本模块，避免判定口径分叉：
  - `tool_executor_node`：命中瞬时错误时**原样重试**同一调用（指数退避 + 上限）
  - `critic_node`：按错误码把失败分流到 retryable / plan_defect / fatal
"""
from __future__ import annotations

from app.core.errors import RETRYABLE_BY_CODE, ToolErrorCode

# 文本兜底标记（仅在异常未标注错误码时使用；内容与加固前一致）
TRANSIENT_MARKERS = ("timeout", "超时", "connection", "网络", "temporarily")


def looks_transient(error_text: str) -> bool:
    """错误文本是否像瞬时故障。保守判定：只认明确标记。"""
    lowered = (error_text or "").lower()
    return any(marker in lowered for marker in TRANSIENT_MARKERS)


def is_transient_error(error: object) -> bool:
    """错误是否值得**原样重试**。结构化优先，文本兜底（见模块 docstring）。"""
    explicit = getattr(error, "retryable", None)
    if explicit is not None:
        return bool(explicit)
    code = getattr(error, "code", None)
    if code is not None and code is not ToolErrorCode.UNKNOWN:
        return RETRYABLE_BY_CODE.get(code, False)
    return looks_transient(str(error))


def retry_delay_hint(error: object) -> float | None:
    """上游给出的 Retry-After（秒）。有值时**优先于**指数退避计算。"""
    value = getattr(error, "retry_after_s", None)
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def backoff_delay(attempt: int, base_delay_s: float, max_delay_s: float) -> float:
    """第 `attempt` 次重试前应等待的秒数（attempt 从 1 开始，指数增长并封顶）。

    不做抖动（jitter）：本运行时的并发重试量级是「单任务内的并行工具数」（个位数），
    不存在惊群场景；去掉抖动让延迟可精确断言，收益大于成本。
    """
    if attempt < 1:
        raise ValueError("attempt 从 1 开始")
    return min(base_delay_s * (2 ** (attempt - 1)), max_delay_s)
