"""工具错误码契约：把"错误性质"从自然语言文本里解耦出来。

存在的理由：此前的错误分类靠**中文字符串嗅探**（`"超时" in errors`），脆弱且随文案漂移。
结构化错误码让"这个错该不该重试、该重试还是该重规划还是该终止"变成**可枚举、可表驱动测试**
的确定性问题，而不是猜文本。

三方共用本模块，保证口径唯一：
  - `app/tools/registry.py`：把异常折叠成带 code 的 `ToolExecutionError`
  - `app/core/retry.py`：按 code 决定"是否值得原样重试"，并读 `retry_after_s` 做退避提示
  - `app/graph/nodes.py` 的 critic：按 code 决定 retryable / plan_defect / fatal

兼容策略（**不是破坏性迁移**）：`ToolExecutionError.code` 默认 `None`。
`retry.py` 的解析顺序是「显式 retryable → 结构化 code → 文本启发式兜底」，
所以未标注 code 的既有错误仍走原逻辑，行为不变；标注了 code 的则精确判定。
"""
from __future__ import annotations

from enum import StrEnum


class ToolErrorCode(StrEnum):
    """工具失败的性质。字符串值进事件流与日志，保持人类可读。

    刻意用 `StrEnum` 而非 `(str, Enum)`：后者在 Python 3.11 下
    `str(member)` 会得到 `"ToolErrorCode.TIMEOUT"`，且枚举的 `__hash__` 基于成员名，
    与等值字符串的哈希不一致——跨 checkpoint 反序列化退回普通字符串后，
    集合交集会静默失配，分类逻辑会悄悄退化成文本兜底。
    """

    TIMEOUT = "timeout"              # 本地超时（含 wait_for 与工具自身超时）
    NETWORK = "network"              # 连接类故障（DNS/连接重置/不可达）
    RATE_LIMITED = "rate_limited"    # 上游限流（HTTP 429），通常带 Retry-After
    UPSTREAM_5XX = "upstream_5xx"    # 上游服务端错误，通常可重试
    UPSTREAM_4XX = "upstream_4xx"    # 上游客户端错误，重试无意义
    AUTH = "auth"                    # 凭证/鉴权失败，重试无用且需告警
    PERMISSION = "permission"        # 本地权限/越界拦截，安全类终止
    NOT_FOUND = "not_found"          # 工具不存在，或远端资源 404
    INVALID_ARGS = "invalid_args"    # 入参不合法（含 schema 校验失败耗尽自愈）
    UNKNOWN = "unknown"              # 未识别，交由文本启发式兜底


# 各错误码的默认可重试性（"原样重试"是否可能自愈）。
# UNKNOWN 刻意标 False 且不参与自动重试的判定——未识别就不自作主张，
# 让 retry.py 退回文本启发式（兼容旧行为）。
RETRYABLE_BY_CODE: dict[ToolErrorCode, bool] = {
    ToolErrorCode.TIMEOUT: True,
    ToolErrorCode.NETWORK: True,
    ToolErrorCode.RATE_LIMITED: True,
    ToolErrorCode.UPSTREAM_5XX: True,
    ToolErrorCode.UPSTREAM_4XX: False,
    ToolErrorCode.AUTH: False,
    ToolErrorCode.PERMISSION: False,
    ToolErrorCode.NOT_FOUND: False,
    ToolErrorCode.INVALID_ARGS: False,
    ToolErrorCode.UNKNOWN: False,
}


def code_from_http_status(status: int) -> ToolErrorCode:
    """按 HTTP 状态码分流。补齐了文本嗅探时代漏掉的一整类错误（5xx / 429 / 404）。"""
    if status == 429:
        return ToolErrorCode.RATE_LIMITED
    if status in (401, 403):
        return ToolErrorCode.AUTH
    if status == 404:
        return ToolErrorCode.NOT_FOUND
    if 500 <= status < 600:
        return ToolErrorCode.UPSTREAM_5XX
    if 400 <= status < 500:
        return ToolErrorCode.UPSTREAM_4XX
    return ToolErrorCode.UNKNOWN


class UpstreamHTTPError(Exception):
    """工具侧的上游 HTTP 失败。

    带上状态码与 Retry-After，让 registry 能折算出 code 与退避提示，
    而不是把状态码塞进一句自由文本里再去嗅探。
    """

    def __init__(self, status_code: int, message: str,
                 retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_s = retry_after_s

    @property
    def code(self) -> ToolErrorCode:
        return code_from_http_status(self.status_code)


def parse_retry_after(value: str | None) -> float | None:
    """解析 `Retry-After` 头。只支持秒数形式（HTTP-date 形式返回 None 交回退避计算）。"""
    if not value:
        return None
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None
