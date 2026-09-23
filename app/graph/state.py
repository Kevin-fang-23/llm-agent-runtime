"""AgentState：LangGraph 状态机的单一事实来源。

所有通道都是「整值覆写」语义（无 reducer），节点返回部分更新即可；
messages 由节点显式管理整列表，为上下文压缩（整体替换）留出自由度。
"""
from __future__ import annotations

from typing import TypedDict

# 状态机全局 status 取值
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELED = "canceled"
STATUS_BUDGET_EXCEEDED = "budget_exceeded"
STATUS_WAITING_APPROVAL = "waiting_approval"  # HITL：在审批门挂起，等待人工决策

# critic 对错误的分类
ERR_NONE = "none"
ERR_RETRYABLE = "retryable"          # 瞬时错误：可原样重试
ERR_PLAN_DEFECT = "plan_defect"      # 计划缺陷：回 planner 重规划
ERR_FATAL = "fatal"                  # 安全/权限类错误：直接终止


class PlanStep(TypedDict, total=False):
    """计划步骤。

    P2-DAG 起支持可选 `deps`：声明依赖的步骤 id 列表（DAG 形态，空列表 = 无依赖
    可并行）；**键缺省 = 线性链**（依赖列表中的上一步）—— 旧 prompt 输出与旧
    checkpoint 里没有这个键，语义自动等价于原有线性执行，零迁移成本。
    """

    id: str
    description: str
    status: str  # pending | done | failed | skipped
    result: str
    deps: list[str]


class AgentState(TypedDict, total=False):
    task_id: str
    goal: str
    mode: str                    # react | plan_execute

    messages: list[dict]         # OpenAI 风格消息（整列表覆写）
    plan: list[PlanStep]         # plan_execute 模式的计划（DAG：步骤可声明 deps）
    current_step: int            # 当前执行的**批次号**（Kahn 分层下标；线性计划 = 旧步骤下标）
    plan_defect_streak: int      # 连续 plan_defect 次数（自适应降级的判定依据）
    mode_switches: int           # react↔plan_execute 累计互切次数（H6 乒乓止损依据）
    key_outputs: dict[str, str]  # 不可压缩的关键工具输出

    iterations: int              # 已执行的 LLM 决策步数
    steps_used: int
    tokens_used: int
    max_steps: int
    max_tokens: int
    downgraded: bool             # 是否已降级到便宜模型

    status: str
    last_error: str
    error_kind: str
    pending_tool_calls: list[dict]
    last_observations: list[dict]
    needs_final: bool
    final_answer: str
    selfheal_total: int          # 本任务累计自愈修复次数

    # HITL（P2-2）：require_approval 由任务行传入；approval_pending 由审批门节点维护
    require_approval: bool
    approval_pending: bool
