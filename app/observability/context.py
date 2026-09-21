"""Trace 上下文：W3C Trace Context 解析 + `contextvars` 贯穿 + span 身份传递。

为什么用 `contextvars` 而不是把 trace_id 塞进 `AgentState`：

- `AgentState` 是 LangGraph 的 checkpoint 单一事实来源，改它的形态会让**存量
  checkpoint 反序列化后缺字段**（虽然 `total=False` 能容忍，但每个读取点都得
  写成 `.get("trace_id", "")`，噪声大且容易漏）。
- trace 是**请求级上下文**，不是任务状态：`resume` 一次恢复应当延续原 trace，
  而 `cancel` 决策来自另一个请求（另一条 trace）—— 用 ContextVar 天然表达
  "谁在什么时候触发了什么"，比把最后一个写入者存进状态更准确。
- 与既有 `_current_task_id`（`app/graph/engine.py:38`）同一套机制，风格一致。

trace_id 由 `run_task` / `resume_task` 的入口显式绑定：这样**后台队列**（任务由
`LocalTaskQueue` 协程拉起，不在 HTTP 请求上下文里）与 **HTTP 请求路径**走的是同
一条 API，不依赖 FastAPI 中间件的隐式传播 —— 而中间件方案在 `asyncio.create_task`
之后就会丢失上下文，Celery 路径更是完全不在同一个进程里。

**span 身份的传递（P2-6）**：`traceparent` 的四段里，第三段 `parent-id` 是
span 树的关键 —— 它声明"我是谁的孩子"。本模块因此提供两个解析入口：

- `parse_traceparent()` 只取 trace_id（既有签名不变，16 条既有测试零改动）；
- `parse_traceparent_full()` 返回完整的 `TraceParent`，含 parent_id。

`span_id_var` 与 `trace_id_var` 并列，表示"当前正在执行的 span"。新的子 span
生成时以它作为 parent_id —— 这就是把"共用一个 trace_id 的扁平事件流"
变成"有父子关系的 span 树"的那一步。
"""
from __future__ import annotations

import os
import secrets
from contextvars import ContextVar
from dataclasses import dataclass

# 当前 trace id（asyncio 上下文隔离）。空串 = 未绑定，此时日志/指标退化为"无 trace"。
trace_id_var: ContextVar[str] = ContextVar("agent_trace_id", default="")

# 当前 span id：嵌套 span 的 parent 来源。空串 = 尚无 span（root span 以入站 parent_id
# 或全零作为父）。
span_id_var: ContextVar[str] = ContextVar("agent_span_id", default="")

# W3C traceparent: 00-<32 hex trace-id>-<16 hex parent-id>-<2 hex flags>
TRACEPARENT_HEADER = "traceparent"
_TRACEPARENT_PARTS = 4
_HEX = set("0123456789abcdef")

# 全零 trace-id 与全零 parent-id 在 W3C 规范里是**非法**的（保留值），必须当作缺失。
_INVALID_TRACE_ID = "0" * 32
_INVALID_PARENT_ID = "0" * 16

# 无父 span 时的父 id（W3C 规定 root span 的 parent-id 为全零）
ROOT_PARENT_ID = _INVALID_PARENT_ID


@dataclass(frozen=True)
class TraceParent:
    """解析后的 `traceparent`：四段全部保留。

    `version` / `flags` 目前不参与业务判断（本项目不做采样位决策 —— 采样在
    日志过滤层独立实现，见 `logging.py`），但保留它们让"解析结果"与协议字段
    一一对应，将来要支持 `sampled` 位时不必再改签名。
    """

    version: str
    trace_id: str
    parent_id: str
    flags: str

    @property
    def sampled(self) -> bool:
        """W3C flags 最低位 = 上游是否采样。仅作信息透传，不改变本进程行为。"""
        try:
            return bool(int(self.flags, 16) & 0x01)
        except ValueError:  # pragma: no cover - parse 已保证是十六进制
            return False


def new_trace_id() -> str:
    """生成 32 位小写十六进制 trace id（符合 W3C 规范，可被 collector 接受）。

    用 `secrets.token_hex` 而非 `uuid4().hex`：语义更准（trace id 是随机标识、
    不需要 UUID 的版本位），且避免有人误以为可以从 trace_id 反解出 UUID 版本。
    """
    return secrets.token_hex(16)


def new_span_id() -> str:
    """生成 16 位小写十六进制 span id（W3C span-id 段长度）。"""
    return secrets.token_hex(8)


def parse_traceparent_full(value: str | None) -> TraceParent | None:
    """从 `traceparent` 头解析**完整四段**；非法/缺失/全零一律返回 None。

    严格按 W3C 规范校验（`version-traceid-parentid-flags`）：
      - 恰好 4 段；版本段 2 位十六进制；
      - trace-id 32 位十六进制且非全零；parent-id 16 位十六进制且非全零。
    """
    if not value:
        return None
    parts = value.split("-")
    if len(parts) != _TRACEPARENT_PARTS:
        return None
    version, trace_id, parent_id, flags = parts
    if len(version) != 2 or any(c not in _HEX for c in version):
        return None
    if len(trace_id) != 32 or any(c not in _HEX for c in trace_id):
        return None
    if len(parent_id) != 16 or any(c not in _HEX for c in parent_id):
        return None
    if len(flags) != 2 or any(c not in _HEX for c in flags):
        return None
    if trace_id == _INVALID_TRACE_ID or parent_id == _INVALID_PARENT_ID:
        return None
    return TraceParent(version=version, trace_id=trace_id, parent_id=parent_id, flags=flags)


def parse_traceparent(value: str | None) -> str:
    """从 `traceparent` 头解析 trace id；非法/缺失/全零一律返回空串。

    宽松解析看似更"健壮"，实则会复用脏 id 并把它传播到下游 —— 一旦上游发了
    错误格式，整条链路都会带着垃圾标识，反而更难排查。宁可退回自生成。

    保持 `-> str` 签名不变（既有调用点与 16 条测试零改动）；需要 parent_id 时
    用 `parse_traceparent_full()`。
    """
    parsed = parse_traceparent_full(value)
    return parsed.trace_id if parsed else ""


def bind_trace(header_value: str | None = None) -> tuple[str, object]:
    """绑定 trace：优先复用入站 `traceparent`，否则自生成。

    返回 `(trace_id, token)`；调用方必须在 finally 里 `trace_id_var.reset(token)`，
    否则并发任务的 trace 会互相污染（ContextVar 是上下文隔离的，但**同一个**
    上下文里不 reset 就会一直向后传递）。

    **parent_span_id 的落点**：入站 parent-id 存在时写入 `span_id_var`，让本进程
    产生的第一个 span 成为它的孩子，从而与调用方（网关 / 上游服务）的 span 树
    连起来。这是"跨进程 span 树"在零依赖下的可行形态。
    """
    parsed = parse_traceparent_full(header_value)
    trace_id = parsed.trace_id if parsed else new_trace_id()
    token = trace_id_var.set(trace_id)
    if parsed is not None:
        # 不 reset：它代表"上游 span 是本进程根 span 的父亲"，供首个 span 读取
        span_id_var.set(parsed.parent_id)
    return trace_id, token


def current_trace_id() -> str:
    """读当前 trace id；未绑定时返回空串（调用方自行决定回退策略）。

    环境变量 `TRACE_ID` 作为兜底来源：CLI 脚本（`scripts/demo_cli.py` 等）没有
    HTTP 头可解析，但运维仍可能想给某次手工执行打标。只在 ContextVar 为空时读，
    避免覆盖调用链上真实的 trace。
    """
    return trace_id_var.get() or os.environ.get("TRACE_ID", "")


def current_span_id() -> str:
    """读当前 span id（嵌套 span 的 parent 来源）；未绑定时返回空串。"""
    return span_id_var.get()


def current_parent_span_id() -> str:
    """新 span 应当挂载的父 span id：当前 span，缺失时退化为全零（root）。"""
    return span_id_var.get() or ROOT_PARENT_ID
