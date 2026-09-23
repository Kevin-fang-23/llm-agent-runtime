"""状态机节点实现：planner / react_step / tool_executor / critic / compressor / finisher。

每个节点都是纯「读 state → 做事 → 返回部分更新」的函数（绑定在 AgentEngine 上），
节点内部通过 event sink 对外广播轨迹事件，自身不感知存储介质。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from langgraph.types import interrupt

from app.core.budget import check_budget
from app.core.compressor import compress_messages, inject_key_outputs, needs_compression
from app.core.errors import ToolErrorCode
from app.core.llm import estimate_messages_tokens, estimate_tokens
from app.core.retry import (
    backoff_delay,
    is_transient_error,
    looks_transient,
    retry_delay_hint,
)
from app.graph import prompts
from app.graph.state import (
    ERR_FATAL,
    ERR_NONE,
    ERR_PLAN_DEFECT,
    ERR_RETRYABLE,
    STATUS_BUDGET_EXCEEDED,
    STATUS_CANCELED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
    AgentState,
)
from app.observability import metrics as obs_metrics
from app.observability import spans as obs_spans
from app.tools.registry import ToolExecutionError, ToolValidationError

log = logging.getLogger("agent.nodes")


# ---------- M3：幂等流水的调用键 ----------
# 占位在他人手里时的等待窗口：真并发（同调用双 worker）应等到对方回填；
# 对方执行中死亡则超时后由本方接管重跑 —— 不比旧行为（无占位直接重跑）更差。
CLAIM_WAIT_S = 5.0
CLAIM_POLL_INTERVAL_S = 0.25


# ---------- A2：key_outputs 注入段上限 ----------
# key_outputs 每步拼进 system 提示且永不被压缩：不设上限时，长任务的上下文
# 会被这段"赢不回来"的数据被动撑爆 —— 压缩机制省下的预算又被注入段吃掉。
# dict 保持插入序，最近确认的数据价值更高，超限从最旧删除。
KEY_OUTPUTS_MAX_ITEMS = 30
KEY_OUTPUTS_TOKEN_BUDGET = 1500

# A2：扣除固定开销后的历史段预算下限 —— 极端情况下（开销≥配置阈值）也要给
# 最近窗口留出生存空间，否则压缩退化为"每轮都压但永远压不下去"。
MIN_HISTORY_BUDGET_TOKENS = 512


def _prune_key_outputs(key_outputs: dict[str, str]) -> dict[str, str]:
    items = list(key_outputs.items())
    if len(items) > KEY_OUTPUTS_MAX_ITEMS:
        items = items[-KEY_OUTPUTS_MAX_ITEMS:]
    total = sum(estimate_tokens(f"{k}: {v}") for k, v in items)
    while items and total > KEY_OUTPUTS_TOKEN_BUDGET:
        k, v = items.pop(0)
        total -= estimate_tokens(f"{k}: {v}")
    return dict(items)


def _journal_call_key(state: AgentState, call: dict) -> str:
    """流水幂等键 = call_id + (轮次, 工具, 原始参数) 指纹。

    为什么纯 call_id 不够（M3）：部分 OpenAI 兼容端点**每一轮都复用 "call_1"**，
    旧键会把后一轮的真实新调用误判为前一轮的回放（新结果被旧结果替换）。
    指纹取 state.iterations + 工具名 + 模型给出的**原始**参数：checkpoint 重放
    同一 superstep 时三者与状态完全一致，键稳定可命中；换了轮次或参数即新调用。
    """
    raw = json.dumps({"n": call["name"], "a": call.get("arguments", {}),
                      "p": call.get("args_parse_error", "")},
                     sort_keys=True, ensure_ascii=False, default=str)
    fp = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    key = f"{call['id']}#{state.get('iterations', 0)}:{fp}"
    if len(key) > 80:  # call_id 列宽 String(80)：超长 id 整体折叠为摘要
        key = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
    return key


def parse_json_loose(text: str) -> dict[str, Any] | list[Any] | None:
    """容错解析模型输出的 JSON（容忍代码块围栏与前后杂文本）。

    顶层对象与顶层数组都接受：小模型经常省掉外层的 `{"steps": ...}` 壳、
    直接输出步骤数组 —— 旧实现只找 `{`，会把数组里的第一个内层对象当成
    整个结果，其余步骤**静默丢失**（计划退化成单步还自以为规划成功）。
    """
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    for start in sorted(i for i in (text.find("{"), text.find("[")) if i >= 0):
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        return obj if isinstance(obj, (dict, list)) else None
    return None


def _plan_text(plan: list[dict]) -> str:
    return "\n".join(f"{i + 1}. [{p['status']}] {p['description']}" for i, p in enumerate(plan))


# ---------- 计划 DAG 化（P2-3）----------
# 依赖关系的表示：PlanStep 可选 `deps` 键声明前置步骤 id（DAG 形态）；
# **键缺省 = 线性链**（依赖列表中紧邻的上一步）—— 旧 prompt 输出与旧 checkpoint
# 里没有这个键，语义自动等价于原有线性执行，零迁移成本。

def _parse_plan_steps(raw: Any) -> list[dict]:
    """把 planner 输出的 steps 归一为带 id / description 的步骤列表。

    兼容两种模型输出形态：
    - dict 形态（新版）：{"id": "s1", "description": "...", "deps": [...]}。
      id 缺省按序补齐 s1..sn；deps **显式给出才写键**（含空数组 = 无依赖可并行），
      省略时不写键、退回线性链 —— 新提示词要求模型显式给出 deps，
      模型漏给时宁串行勿乱序并行；
    - 字符串形态（旧版）：["步骤1", "步骤2", ...]。**不写 deps 键**，交由
      _plan_layers 的「键缺省 = 线性链」语义处理，行为与旧版完全一致。
    """
    steps: list[dict] = []
    for item in raw or []:
        if isinstance(item, dict):
            desc = str(item.get("description", "")).strip()
            if not desc:
                continue
            step: dict = {"id": str(item.get("id") or f"s{len(steps) + 1}"),
                          "description": desc, "status": "pending", "result": ""}
            deps = item.get("deps")
            if isinstance(deps, list):
                step["deps"] = [str(d) for d in deps]
            # deps 键缺省/非法时**不写键**：交由 _plan_layers 的「键缺省 = 线性链」
            # 保守兜底 —— 模型省略 deps 时宁可串行，不做乱序的全并行
        else:
            desc = str(item).strip()
            if not desc:
                continue
            step = {"id": f"s{len(steps) + 1}", "description": desc,
                    "status": "pending", "result": ""}
        steps.append(step)
    return steps


def _plan_layers(plan: list[dict]) -> list[list[dict]] | None:
    """按依赖关系做 Kahn 分层，返回批次列表（层内保持声明顺序）。

    每步的依赖：显式 `deps` 键优先；键缺省 = 线性链（依赖列表上一步，
    首步无依赖）。返回 None 表示计划非法：id 重复 / deps 引用不存在的 id /
    自依赖 / 环 —— 调用方按 _safe_layers 的兜底阶梯退化处理。
    """
    n = len(plan)
    ids: dict[str, int] = {}
    for i, p in enumerate(plan):
        pid = p.get("id", f"s{i + 1}")
        if pid in ids:
            return None  # id 重复（旧 checkpoint 的 replan 重用 id 会落到这）
        ids[pid] = i
    dep_idx: list[set[int]] = []
    for i, p in enumerate(plan):
        if "deps" in p:
            refs = p["deps"]
            if not isinstance(refs, list) or any(not isinstance(r, str) for r in refs):
                return None
            deps = set()
            for r in refs:
                if r not in ids:
                    return None  # 引用不存在的 id
                deps.add(ids[r])
            if i in deps:
                return None  # 自依赖
            dep_idx.append(deps)
        else:
            dep_idx.append({i - 1} if i > 0 else set())
    # Kahn 分层：每轮取全部依赖已就绪的步骤为一批
    remaining = [set(d) for d in dep_idx]
    done: set[int] = set()
    layers: list[list[dict]] = []
    while len(done) < n:
        ready = [i for i in range(n) if i not in done and not (remaining[i] - done)]
        if not ready:
            return None  # 有环
        layers.append([plan[i] for i in ready])
        done.update(ready)
    return layers


def _linearize_plan(plan: list[dict]) -> list[dict]:
    """去掉全部 deps 键，把计划退化为线性链（deps 非法时的兜底）。"""
    return [{k: v for k, v in p.items() if k != "deps"} for p in plan]


def _safe_layers(plan: list[dict]) -> list[list[dict]]:
    """三级兜底取执行分层，保证任何形态的计划都能得到可跑的批次序列。

    1. DAG 分层（依赖关系成立时，独立步骤同批并行）；
    2. deps 非法 → 删 deps 退化为线性链重分层（串行执行，旧语义）；
    3. 仍失败（如旧 checkpoint 的重复 id）→ 逐步分层，宁可串行不崩。
    """
    layers = _plan_layers(plan)
    if layers is not None:
        return layers
    lin = _linearize_plan(plan)
    layers = _plan_layers(lin)
    if layers is not None:
        # _linearize_plan 产出的是**拷贝**：按位置翻译回原 plan 步骤再返回。
        # 否则 critic 在层内标 done 只改到拷贝，state 里的计划纹丝不动。
        at = {id(c): i for i, c in enumerate(lin)}
        return [[plan[at[id(p)]] for p in group] for group in layers]
    return [[p] for p in plan]


def _now_context() -> str:
    """当前时间（本地时区）。

    **每轮现构造，不写入 state.messages**：时间会流逝，写进历史下一刻就是错的；
    而且历史里的旧时间会让模型产生矛盾（"到底哪个才是现在"）。
    必须注入的原因是：模型无法感知真实时间，只能拿训练数据里的日期猜
    —— 实测问"今天是几号"答成 2024-06-19，比真实日期早两年多。
    """
    now = datetime.now().astimezone()
    weekday = "一二三四五六日"[now.weekday()]
    raw = now.strftime("%z")                       # 形如 +0800
    offset = f"{raw[:3]}:{raw[3:]}" if len(raw) == 5 else raw
    return prompts.NOW_CONTEXT.format(
        now=now.strftime("%Y-%m-%d %H:%M:%S"), weekday=f"星期{weekday}", offset=offset)


# critic 的错误码 → 判定分流表。用**字符串**而非枚举成员：
# 观测值会随 checkpoint 持久化，反序列化后错误码退回普通字符串，
# 此时枚举集合交集会失配；统一按字符串比较即与持久化形态一致。
_PLAN_DEFECT_CODES = {ToolErrorCode.INVALID_ARGS.value, ToolErrorCode.NOT_FOUND.value}
_FATAL_CODES = {ToolErrorCode.PERMISSION.value, ToolErrorCode.AUTH.value}


def _code_value(code: ToolErrorCode | str | None) -> str | None:
    """错误码统一以字符串形态落进观测值与事件流（枚举也行，字面量也行）。"""
    if code is None:
        return None
    return getattr(code, "value", code)


async def _cancellable_sleep(delay_s: float, canceled: Callable[[], bool]) -> bool:
    """可打断的退避睡眠：以 0.5s 切片轮询取消位；睡眠中收到取消返回 True。

    旧实现只在 sleep **之前**检查一次取消，而 sleep 时长可能来自上游 Retry-After
    —— "协作式取消要立刻生效"的承诺（见 _retry_transient docstring）需要一个
    真正能在睡眠中途响应的实现。
    """
    remaining = max(0.0, delay_s)
    while remaining > 0:
        if canceled():
            return True
        step = min(0.5, remaining)
        await asyncio.sleep(step)
        remaining -= step
    return False


def init_state(task_id: str, goal: str, mode: str, max_tokens: int, max_steps: int,
               require_approval: bool = False) -> AgentState:
    return AgentState(
        task_id=task_id,
        goal=goal,
        mode=mode,
        messages=[],
        plan=[],
        current_step=0,
        key_outputs={},
        iterations=0,
        selfheal_total=0,
        steps_used=0,
        tokens_used=0,
        max_steps=max_steps,
        max_tokens=max_tokens,
        downgraded=False,
        plan_defect_streak=0,
        mode_switches=0,
        status="running",
        last_error="",
        error_kind=ERR_NONE,
        pending_tool_calls=[],
        last_observations=[],
        needs_final=False,
        final_answer="",
        require_approval=require_approval,
        approval_pending=False,
    )


class GraphNodes:
    """节点集合，由 AgentEngine 实例化并注册进 StateGraph。"""

    def __init__(self, engine):
        self.engine = engine
        self.settings = engine.settings

    # ---------- 入口路由 ----------
    async def route_entry(self, state: AgentState) -> str:
        return "planner" if state["mode"] == "plan_execute" else "react_step"

    # ---------- 规划 ----------
    async def planner_node(self, state: AgentState) -> dict:
        is_replan = bool(state.get("last_error"))
        plan_desc = "\n".join(f"- {p['description']}" for p in state.get("plan", []))
        user = f"用户目标：{state['goal']}"
        if is_replan:
            user += prompts.REPLAN_NOTE.format(
                failed_step=state.get("last_error", "")[:200],
                error="见上",
            )
        else:
            user += f"\n现有背景信息：{plan_desc}" if plan_desc else ""

        messages = [
            {"role": "system", "content": prompts.PLAN_SYSTEM.format(max_steps=state["max_steps"])
                                          + _now_context()},
            {"role": "user", "content": user},
        ]
        resp = await self.engine.llm.chat(messages, model=self._model(state))
        tokens = resp.tokens_used
        parsed = parse_json_loose(resp.text)
        # 顶层数组 = 模型直接给出步骤列表（省掉 {"steps": ...} 壳）
        raw_steps = parsed.get("steps") if isinstance(parsed, dict) else parsed
        steps = _parse_plan_steps(raw_steps)
        if not steps:  # 规划失败兜底：目标本身就是一步
            steps = [{"id": "s1", "description": state["goal"],
                      "status": "pending", "result": ""}]

        # 自适应升级（P2-DAG）：react 模式重规划成功 → 切回 plan_execute。
        # 旧实现的缺口：react 重规划产出的计划没有任何执行轨道（plan_context
        # 仅 plan_execute 注入），重规划结果只是躺进 state.plan 的死数据。
        # H6：互切要计入总开关数——过去"升级即清零 streak"让降级护栏永远攒不满，
        # react↔plan 无限乒乓、每环至少多烧一次 planner 调用直到 max_steps 兜底。
        switches = state.get("mode_switches", 0)
        upgraded = (is_replan and state["mode"] == "react"
                    and switches < self.settings.max_mode_switches)
        if is_replan:
            done = [p for p in state.get("plan", []) if p["status"] == "done"]
            # 新步骤 id 顺延编号（接续旧计划 s<数字> 的最大值）：DAG 时代 id 是
            # 依赖引用键，旧实现重用 s1..sn 会与保留的 done 步骤撞 id（非法计划）。
            # rename map 同步改写新步骤之间的 deps 引用。
            base = 0
            for p in state.get("plan", []):
                m = re.match(r"^s(\d+)$", str(p.get("id", "")))
                if m:
                    base = max(base, int(m.group(1)))
            rename = {s["id"]: f"s{base + 1 + i}" for i, s in enumerate(steps)}
            last_done_id = done[-1]["id"] if done else ""
            for s in steps:
                s["id"] = rename[s["id"]]
                if "deps" in s:
                    s["deps"] = [rename.get(d, d) for d in s["deps"]]
                    if not s["deps"] and last_done_id:
                        # 显式「无依赖」的新步骤挂到最后一个已完成步骤之后：
                        # done 占据计划头部，不挂依赖会与它们混入同批 ——
                        # 批内 pending 只剩它自己，能跑但批次语义混乱
                        s["deps"] = [last_done_id]
            new_plan = [*done, *steps]
            event_type = "replan"
        else:
            new_plan = steps
            event_type = "plan_created"

        layers = _safe_layers(new_plan)
        current = next((i for i, layer in enumerate(layers)
                        if any(p["status"] == "pending" for p in layer)), len(layers))
        await self.engine.emit(state, event_type, {
            "steps": [s["description"] for s in steps], "plan_size": len(new_plan),
            "layers": [len(layer) for layer in layers],
            "tokens": tokens, "raw": resp.text[:500],
        })
        if upgraded:
            await self.engine.emit(state, "mode_upgraded", {
                "from": "react", "to": "plan_execute", "plan_size": len(new_plan),
            })
        updates: dict = {"plan": new_plan, "current_step": current,
                         "tokens_used": state["tokens_used"] + tokens,
                         "last_error": "", "error_kind": ERR_NONE}
        if upgraded:
            updates["mode"] = "plan_execute"
            updates["mode_switches"] = switches + 1
            # H6：刻意**不清** plan_defect_streak。它是"计划路线被证明失败的次数"，
            # 升级清零正是旧实现乒乓循环的来源；成功批次的清零在 critic 里仍然有效。
        return updates

    # ---------- 决策步上下文组装 ----------
    def _assemble_base(self, state: AgentState) -> list[dict]:
        """组装每一步的模型上下文：system（含关键数据注入）+ 任务提示 + 执行历史。

        state.messages 只存执行历史（assistant/tool），不含 system 与任务提示，
        因此任务提示可携带最新计划进度，且压缩只作用于历史段。

        ⚠️ 不要把组装好的 base 写回 state.messages：
           base 每轮都会新增一份 system + user，若写回历史，下一轮又把它当历史拼进去，
           上下文随步数近似 O(n²) 膨胀。实测 4 轮工具调用时第 5 次 LLM 调用收到
           5 份 system + 5 份 user，token 从 202 涨到 5184（26 倍），
           并连带打穿 compressor（其切片假设 system 只出现在头部）。

        compressor_node 也调用它来估算"固定开销段"的实际 token 量（A2）——
        传 messages=[] 的 state 即可拿到不含历史的那部分。
        """
        system = (inject_key_outputs(prompts.REACT_SYSTEM, state.get("key_outputs", {}))
                  + _now_context())
        plan_context = ""
        if state.get("plan") and state["mode"] == "plan_execute":
            layers = _safe_layers(state["plan"])
            idx = state.get("current_step", 0)
            if idx < len(layers):
                # 注入当前**批**的全部待执行步骤：DAG 下同批步骤相互独立，
                # 模型应在一次回复里并行调用（REACT_SYSTEM 准则 2 与此呼应）
                pending = [p for p in layers[idx] if p["status"] == "pending"]
                step_desc = "\n".join(f"- {p['description']}" for p in pending) \
                    or "（本批步骤已全部完成）"
                plan_context = prompts.PLAN_CONTEXT_LINE.format(
                    plan_text=_plan_text(state["plan"]), current_step=idx + 1,
                    total_batches=len(layers), step_desc=step_desc,
                )
        user = prompts.REACT_TASK.format(
            goal=state["goal"], used_steps=state.get("steps_used", 0),
            max_steps=state.get("max_steps", 0), plan_context=plan_context,
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
            *state.get("messages", []),
        ]

    # ---------- ReAct 决策步 ----------
    async def react_step_node(self, state: AgentState) -> dict:
        # step span：包住"组装上下文 + 调模型"，用来回答"决策步里模型占多少、编排占多少"。
        # 它挂在 react_step_node 而不是 chat() 上：一次决策步可能触发 0 次或多次
        # 模型调用（自愈/降级），step span 是这层的耗时，llm span 是每个出网点的耗时，
        # 两者的差就是纯编排开销。
        with self.engine.open_span(state["task_id"], obs_spans.KIND_STEP, "react_step",
                                   iteration=state.get("iterations", 0)) as step_span:
            return await self._react_step_inner(state, step_span)

    async def _react_step_inner(self, state: AgentState, step_span) -> dict:
        if self.engine.is_canceled(state["task_id"]):
            return {"status": STATUS_CANCELED, "needs_final": True, "final_answer": "任务已被用户取消。"}

        decision = check_budget(
            state["tokens_used"], state["steps_used"], state["max_tokens"],
            state["max_steps"], state.get("downgraded", False),
            bool(self.settings.llm_model_cheap),
        )
        if decision.action == "exceeded":
            await self.engine.emit(state, "budget_exceeded", {"reason": decision.reason})
            return {"status": STATUS_BUDGET_EXCEEDED, "needs_final": True,
                    "last_error": decision.reason, "error_kind": ERR_NONE}
        # plan 模式：全部批次已执行完 → 直接进入汇总
        if (state["mode"] == "plan_execute" and state.get("plan")
                and state.get("current_step", 0) >= len(_safe_layers(state["plan"]))):
            return {"needs_final": True}
        model = self._model(state)
        downgrade_once = {}
        if decision.action == "downgrade":
            model = self.settings.llm_model_cheap
            downgrade_once = {"downgraded": True}
            await self.engine.emit(state, "budget_downgrade", {"reason": decision.reason, "model": model})

        # 组装上下文：见 _assemble_base
        base = self._assemble_base(state)

        resp = await self.engine.llm.chat(base, tools=self.engine.registry.to_openai_tools(), model=model)
        step_span.set_attribute("model", resp.model)
        step_span.set_attribute("tool_calls", len(resp.tool_calls))
        step_span.set_attribute("tokens", resp.tokens_used)
        # 只把本轮 assistant 决策追加进历史；system/user 每轮现构造，不入库
        messages = [*state.get("messages", []), self._assistant_message(resp)]
        tokens = state["tokens_used"] + resp.tokens_used
        steps = state["steps_used"] + 1

        if resp.tool_calls:
            pending = [{"id": c.id, "name": c.name, "arguments": c.arguments,
                        "args_parse_error": c.args_parse_error} for c in resp.tool_calls]
            await self.engine.emit(state, "llm_step", {
                "iteration": state["iterations"] + 1, "thought": resp.text[:400],
                "tool_calls": [{"name": c.name, "arguments": c.arguments} for c in resp.tool_calls],
                "model": resp.model,
            })
            return {"messages": messages, "pending_tool_calls": pending, "tokens_used": tokens,
                    "steps_used": steps, "iterations": state["iterations"] + 1,
                    "error_kind": ERR_NONE, **downgrade_once}

        await self.engine.emit(state, "llm_final_draft", {"text": resp.text[:400], "model": resp.model})
        return {"messages": messages, "pending_tool_calls": [], "needs_final": True,
                "final_answer": resp.text, "tokens_used": tokens, "steps_used": steps,
                "iterations": state["iterations"] + 1, "error_kind": ERR_NONE, **downgrade_once}

    # ---------- 工具执行（含自愈循环 + 瞬时错误退避重试） ----------
    async def tool_executor_node(self, state: AgentState) -> dict:
        calls = state.get("pending_tool_calls", [])
        if not calls:
            return {"pending_tool_calls": [], "last_observations": []}
        if self.engine.is_canceled(state["task_id"]):
            # A1：取消路径清空待执行与观测，不给下游留陈旧数据（critic 的终态守卫是第一道）
            return {"status": STATUS_CANCELED, "needs_final": True, "final_answer": "任务已被用户取消。",
                    "pending_tool_calls": [], "last_observations": []}

        sem = asyncio.Semaphore(self.settings.max_concurrent_tools)
        messages = list(state.get("messages", []))
        key_outputs = dict(state.get("key_outputs", {}))
        # 自愈配额按「单次工具调用」计（见 _execute_call），此处只累计本轮用量。
        # 单条累加语句内无 await，asyncio 单线程下不会与其他 run_one 竞争。
        selfheal_used = 0
        tokens = state["tokens_used"]
        observations: list[dict] = []

        async def run_one(call: dict) -> dict:
            nonlocal selfheal_used, tokens, messages
            name, args = call["name"], call.get("arguments", {})
            journal = self.engine.journal
            task_id = state["task_id"]
            call_key = _journal_call_key(state, call)
            async with sem:
                replay: dict | None = None
                if journal is not None:
                    # M3：原子认领替代"查后执行"。旧流程两个 worker 都能查到
                    # "无记录"，副作用工具各跑一次；现在抢插占位行裁决归属。
                    claimed, existing = await journal.try_claim_tool_execution(
                        task_id, call_key, name, args)
                    if not claimed:
                        replay = existing
                        if replay is None:  # 占位在他人手里：等它回填或判定滞留
                            replay = await self._await_claim_result(journal, task_id, call_key)
                            if replay is None:
                                await self.engine.emit(state, "tool_claim_takeover", {
                                    "tool": name, "call_id": call["id"],
                                    "reason": f"占位超过 {CLAIM_WAIT_S}s 未完成，"
                                              "判定对方已中断，接管重跑",
                                })
                if replay is not None:
                    # 该调用已执行过（进程在节点执行中被杀 → 恢复后重跑本节点）：
                    # 直接回放已提交的结果，不再触碰工具本身。
                    await self.engine.emit(state, "tool_replay", {
                        "tool": replay["tool"], "call_id": call["id"],
                        "arguments": replay["arguments"], "ok": replay["ok"],
                        "reason": "checkpoint 重跑：命中工具执行流水，跳过重复执行",
                    })
                    obs = replay
                else:
                    obs, attempts, repair_tokens = await self._execute_call(
                        state, call, name, args)
                    tokens += repair_tokens
                    selfheal_used += attempts
                    if journal is not None:
                        # 回填认领到的占位：这样「工具已完成、checkpoint 未提交」的崩溃窗口
                        # 也能在恢复时被拦住。
                        await journal.complete_tool_execution(task_id, call_key, obs)

                # 统一后处理（执行与回放两条路径共用，保证 key_outputs / 预算上卷一致）
                if obs["ok"]:
                    result = obs["result"]
                    key = self.engine.registry.extract_key_output(obs["tool"], result)
                    if key:
                        # 以调用 id 保证并行调用不互相覆盖
                        key_outputs[f"{obs['tool']}({call['id']})"] = key
                    # 子 Agent（agent-as-tool）预算上卷：子任务消耗计入父任务预算，
                    # 父预算因此约束整棵执行树；标记键不进入模型上下文
                    extra = result.pop("_budget_tokens", 0) if isinstance(result, dict) else 0
                    if extra:
                        tokens += int(extra)
                return obs

        # M3：return_exceptions 保证"一错不拖全军"。旧实现首个异常直接抛出，
        # 其余协程被 gather 弃管成孤儿（副作用照发生、结果无人收），且本批
        # tool_calls 缺 tool 消息配对 —— 下一次模型调用会被端点判 400。
        raw_results = await asyncio.gather(*(run_one(c) for c in calls),
                                           return_exceptions=True)
        # 非 Exception 的 BaseException（SimulatedCrash 型"进程死亡"、取消）：
        # 保持节点整体失败的旧语义，原样上抛
        for res in raw_results:
            if isinstance(res, BaseException) and not isinstance(res, Exception):
                raise res
        results: list[dict] = []
        for call, res in zip(calls, raw_results):
            if isinstance(res, Exception):
                log.warning("工具执行协程未捕获异常 tool=%s call_id=%s",
                            call["name"], call["id"], exc_info=res)
                results.append({"ok": False, "tool": call["name"],
                                "arguments": call.get("arguments", {}),
                                "error": f"执行协程内部异常: {res}",
                                "error_type": "runtime", "error_code": None})
            else:
                results.append(res)
        for call, obs in zip(calls, results):
            observations.append(obs)
            # 失败时把结构化错误码一并交给模型：模型因此能区分"重试可能有用"与"改策略"
            if obs["ok"]:
                payload: dict = obs.get("result")
            else:
                payload = {"error": obs["error"]}
                if obs.get("error_code"):
                    payload["error_code"] = obs["error_code"]
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "name": call["name"],
                "content": json.dumps(payload, ensure_ascii=False, default=str)[:6000],
            })
            if obs["ok"]:
                await self.engine.emit(state, "tool_result", {
                    "tool": obs["tool"], "arguments": obs["arguments"],
                    "summary": str(obs["result"].get("summary", ""))[:300],
                    "elapsed_ms": obs["result"].get("elapsed_ms"),
                })
            else:
                await self.engine.emit(state, "tool_error", {
                    "tool": obs["tool"], "arguments": obs["arguments"],
                    "error": obs["error"], "error_type": obs["error_type"],
                    "error_code": obs.get("error_code"),
                })
            # 工具指标：标签是工具名（注册表里的固定集合）+ 成败，均为低基数枚举。
            # 注意**不用** error_code 做标签 —— 它虽也是枚举，但会让序列数乘上
            # 错误种类；错误分布由事件流（events 表）承担，那里无基数上限。
            obs_metrics.TOOL_CALLS.inc(
                {"tool": obs["tool"], "outcome": "ok" if obs["ok"] else "error"})

        pruned = _prune_key_outputs(key_outputs)
        if len(pruned) < len(key_outputs):
            # 丢弃是静默的上下文保护，但必须留痕：排障时"模型为什么看不到某条关键数据"
            # 只有从这条日志才能回答
            log.info("key_outputs 超限裁剪：%d → %d（丢弃最旧：%s）", len(key_outputs),
                     len(pruned),
                     ", ".join(k for k in key_outputs if k not in pruned)[:200])
        return {"messages": messages, "pending_tool_calls": [], "last_observations": observations,
                "key_outputs": pruned,
                "selfheal_total": state.get("selfheal_total", 0) + selfheal_used,
                "tokens_used": tokens}

    async def _await_claim_result(self, journal, task_id: str, call_key: str) -> dict | None:
        """撞上他人 in-flight 占位时的等待：给占位者留出回填窗口（M3）。

        两种现实形态：对方**真在执行**（并发重复提交）→ 等到回填直接回放，
        避免双跑副作用；对方**执行中死亡**（占位滞留）→ 超时返回 None，
        由调用方接管重跑。接管不劣于旧行为：旧实现根本没有占位，一律直接重跑。
        """
        deadline = time.monotonic() + CLAIM_WAIT_S
        while time.monotonic() < deadline:
            await asyncio.sleep(CLAIM_POLL_INTERVAL_S)
            done = await journal.get_tool_execution(task_id, call_key)
            if done is not None:
                return done
        return None

    async def _execute_call(self, state: AgentState, call: dict, name: str,
                            args: dict) -> tuple[dict, int, int]:
        """执行单次工具调用（含参数自愈循环）。

        返回 (观测值, 本次调用消耗的自愈次数, 自愈消耗的 token)。

        自愈配额是**每次调用独立**的，不是整个任务的共享池：
          - 旧实现用任务级计数器，一个工具耗尽 3 次配额后，后续工具的校验失败
            直接判 plan_defect、拿不到任何修复机会；
          - 该计数器还在 asyncio.gather 的多个协程间共享读写，跨 await 的
            「读-判-加」使并行调用互相挤占配额（3 次配额最多只被用 3 次，
            而不是每个调用各 3 次）。
        改为协程内局部计数后，既保证每个调用有完整配额，也天然无竞争。
        任务的累计用量由调用方汇总写入 state.selfheal_total（用于指标与展示）。
        """
        attempts = 0
        repair_tokens_used = 0
        started = time.perf_counter()
        # tool span：覆盖**首次执行 + 自愈 + 退避重试**的全部耗时 —— 与 TOOL_DURATION
        # 直方图同一口径。只计首次执行会系统性低估（自愈最多 3 轮、退避最多 2 次），
        # 而"这个工具到底阻塞了任务多久"才是 span 要回答的问题。
        with self.engine.open_span(state["task_id"], obs_spans.KIND_TOOL, name,
                                   call_id=call["id"]) as tool_span:
            try:
                if call.get("args_parse_error"):
                    # 模型给出的 arguments 不是合法 JSON（截断/围栏，小模型最常见失败形态）。
                    # 折叠成校验错进入自愈循环，而不是在解析层抛异常打死整个任务——
                    # 整套"参数自愈 + 结构化错误码"契约正是为这种输入设计的。
                    raise ToolValidationError(call["args_parse_error"])
                result = await self.engine.registry.execute(name, args)
            except ToolValidationError as e:
                healed = False
                last_err = str(e)
                # 修复后真执行时抛出的运行错（ToolExecutionError）：参数问题已结束，
                # 后续归类必须交给运行层，不能再冒充 validation/INVALID_ARGS
                runtime_exc: ToolExecutionError | None = None
                while attempts < self.settings.max_selfheal_retries:
                    attempts += 1
                    obs_metrics.SELFHEAL_TOTAL.inc({"tool": name})
                    await self.engine.emit(state, "tool_validation_failed", {
                        "tool": name, "call_id": call["id"], "arguments": args,
                        "error": last_err, "attempt": attempts,
                    })
                    fixed, repair_tokens = await self._repair_args(state, name, args, last_err)
                    repair_tokens_used += repair_tokens
                    if fixed is None:
                        continue  # 修复器没给出合法 JSON，继续尝试
                    args = fixed
                    try:
                        result = await self.engine.registry.execute(name, args)
                        healed = True
                        await self.engine.emit(state, "selfheal_success", {
                            "tool": name, "call_id": call["id"],
                            "healed_arguments": args, "attempt": attempts,
                        })
                        break
                    except ToolValidationError as e2:
                        last_err = str(e2)
                        continue
                    except ToolExecutionError as e2:
                        runtime_exc = e2
                        last_err = f"运行错误: {e2}"
                        break
                if runtime_exc is not None and not healed:
                    # 与首错同一条瞬时重试契约：retryable 码（timeout/429/5xx）获得
                    # 原样退避重试机会；fatal 码（permission/auth）绝不进重试也绝不
                    # 被降级成 plan_defect——否则 critic 会拿它们去重规划烧 token
                    result, last_err, err_code = await self._retry_transient(
                        state, call, name, args, runtime_exc)
                    if result is None:
                        tool_span.set_status("error")
                        tool_span.set_attribute("error_type", "runtime")
                        tool_span.set_attribute("error_code", str(err_code or ""))
                        tool_span.set_attribute("selfheal_attempts", attempts)
                        return ({"ok": False, "tool": name, "arguments": args,
                                 "error": last_err, "error_type": "runtime",
                                 "error_code": _code_value(err_code)},
                                attempts, repair_tokens_used)
                    healed = True  # 重试成功：result 已就绪，落到下方成功出口
                if not healed:
                    tool_span.set_status("error")
                    tool_span.set_attribute("error_type", "validation")
                    tool_span.set_attribute("selfheal_attempts", attempts)
                    return ({"ok": False, "tool": name, "arguments": args,
                             "error": last_err, "error_type": "validation",
                             "error_code": _code_value(ToolErrorCode.INVALID_ARGS)},
                            attempts, repair_tokens_used)
            except ToolExecutionError as e:
                result, last_err, err_code = await self._retry_transient(state, call, name, args, e)
                if result is None:
                    tool_span.set_status("error")
                    tool_span.set_attribute("error_type", "runtime")
                    tool_span.set_attribute("error_code", str(err_code or ""))
                    return ({"ok": False, "tool": name, "arguments": args,
                             "error": last_err, "error_type": "runtime",
                             "error_code": _code_value(err_code)},
                            attempts, repair_tokens_used)
            finally:
                # 含自愈与退避重试的全部时间：这才是"这个工具调用阻塞了任务多久"，
                # 只计首次执行会系统性低估（自愈最多 3 轮、退避最多 2 次）
                obs_metrics.TOOL_DURATION.observe(time.perf_counter() - started, {"tool": name})

            tool_span.set_attribute("selfheal_attempts", attempts)
            tool_span.set_attribute("ok", True)
            return ({"ok": True, "tool": name, "arguments": args, "result": result},
                    attempts, repair_tokens_used)

    async def _retry_transient(self, state: AgentState, call: dict, name: str, args: dict,
                               first_error: ToolExecutionError) -> tuple[dict | None, str, object]:
        """对瞬时运行错**原样重试**同一调用（指数退避，上限 retry_max_attempts）。

        返回 (成功的结果, 最后一次错误文本, 最后一次错误码)；始终失败时结果为 None。

        与自愈循环的分工：
          - 自愈修「参数错」（ToolValidationError，改参数后重试）
          - 本方法重试「运行错」（ToolExecutionError，参数一字不改）
        升级阶梯：本方法用尽 → 观测值带 runtime 错误与错误码 → critic 按码分流 →
        交回 react_step 由模型决定换工具还是换策略。所以这里只做有限次盲重试，
        不试图在这里"解决"问题。

        三重闸门（缺一不可）：
          1. 错误被判定为瞬时（结构化 code 优先，文本兜底）—— 永久性错误重试只是浪费；
          2. 工具声明 retry_transient —— 超时不等于失败，有副作用的工具盲目重试会做两遍；
          3. 未取消 —— 协作式取消要能立刻生效，不能卡在退避的 sleep 里。
        """
        # 先判瞬时性：非瞬时（含"未知工具"）直接退出，也避免为它去查不存在的 spec
        if not is_transient_error(first_error):
            return None, str(first_error), getattr(first_error, "code", None)
        try:
            retry_ok = self.engine.registry.get(name).retry_transient
        except ToolExecutionError:
            return None, str(first_error), getattr(first_error, "code", None)
        if not retry_ok:
            return None, str(first_error), getattr(first_error, "code", None)

        last_exc: ToolExecutionError = first_error
        attempts_made = 0
        for attempt in range(1, self.settings.retry_max_attempts + 1):
            if not is_transient_error(last_exc):
                break  # 非瞬时错误：重试无意义
            if self.engine.is_canceled(state["task_id"]):
                return (None, f"{last_exc}（任务已取消，不再重试）",
                        getattr(last_exc, "code", None))
            # 上游给了 Retry-After 就听它的，否则退回指数退避。
            # H4：hint 同样受 retry_max_delay_s 封顶——上游回 Retry-After: 86400
            # 不能让本协程睡一天（占着工具信号量，任务还迟迟落不了 checkpoint）
            hint = retry_delay_hint(last_exc)
            delay = (min(hint, self.settings.retry_max_delay_s) if hint is not None
                     else backoff_delay(attempt, self.settings.retry_base_delay_s,
                                        self.settings.retry_max_delay_s))
            attempts_made = attempt
            await self.engine.emit(state, "tool_retry_scheduled", {
                "tool": name, "call_id": call["id"], "attempt": attempt,
                "delay_s": round(delay, 3),
                "delay_source": "retry_after" if hint is not None else "backoff",
                "error_code": _code_value(getattr(last_exc, "code", None)),
                "error": str(last_exc)[:300],
            })
            if await _cancellable_sleep(delay,
                                        lambda: self.engine.is_canceled(state["task_id"])):
                return (None, f"{last_exc}（任务已取消，不再重试）",
                        getattr(last_exc, "code", None))
            try:
                result = await self.engine.registry.execute(name, args)
            except ToolExecutionError as e:
                last_exc = e
                continue
            except ToolValidationError as e:  # 防御分支：参数未改却报校验错，不可重试
                return (None, f"重试期间参数校验失败: {e}", ToolErrorCode.INVALID_ARGS)
            await self.engine.emit(state, "tool_retry_success", {
                "tool": name, "call_id": call["id"], "attempt": attempt,
                "delay_s": round(delay, 3),
            })
            return result, str(last_exc), getattr(last_exc, "code", None)

        if attempts_made >= self.settings.retry_max_attempts > 0:
            await self.engine.emit(state, "tool_retry_exhausted", {
                "tool": name, "call_id": call["id"], "attempts": attempts_made,
                "error_code": _code_value(getattr(last_exc, "code", None)),
                "error": str(last_exc)[:300],
            })
        return None, str(last_exc), getattr(last_exc, "code", None)

    async def _repair_args(self, state, tool_name, bad_args, error) -> tuple[dict | None, int]:
        spec = self.engine.registry.get(tool_name)
        messages = [
            {"role": "system", "content": prompts.REPAIR_SYSTEM},
            {"role": "user", "content": json.dumps({
                "tool": tool_name,
                "description": spec.description,
                "input_schema": spec.input_schema,
                "validation_error": error,
                "original_arguments": bad_args,
            }, ensure_ascii=False, indent=2)},
        ]
        resp = await self.engine.llm.chat(messages, model=self._model(state))
        fixed = parse_json_loose(resp.text)
        # 修复结果必须是参数对象本身；顶层数组等形态直接判修复失败
        if not isinstance(fixed, dict):
            return None, resp.tokens_used
        try:
            spec.validate(fixed)
        except ToolValidationError:
            return None, resp.tokens_used
        return fixed, resp.tokens_used

    # ---------- 批判节点：错误分类与计划推进 ----------
    async def critic_node(self, state: AgentState) -> dict:
        # 终态不参与评审：取消/失败路径仍流经本节点时，若按上一轮的陈旧观测
        # 判"成功"，会把**没执行过的步骤整批标 done**、计划进度失真（A1）。
        if state.get("status") not in (STATUS_RUNNING, ""):
            return {}
        observations = state.get("last_observations", [])
        failed = [o for o in observations if not o["ok"]]

        if not failed:
            plan = [dict(p) for p in state.get("plan", [])]
            idx = state.get("current_step", 0)
            # observations 为空 = 本轮根本没有工具被执行（空批次直通），
            # 批次未推进，更不能拿"零失败"当"整批成功"（A1）
            if plan and state["mode"] == "plan_execute" and observations:
                # 整批推进（P2-DAG）：当前批里的全部 pending 步骤一并标 done。
                # 一批可能含多个并行步骤（diamond 形态），线性计划时只有一步。
                layers = _safe_layers(plan)
                if idx < len(layers):
                    summaries = "; ".join(
                        o["result"].get("summary", "")[:200] for o in observations if o["ok"]
                    )[:400]
                    # 位置一次建表：plan.index(p) 既 O(n²)，又按**相等**匹配 ——
                    # 两个字段全同的步骤（replan 撞描述很常见）会被定位到前者，
                    # step_done 报出的步号是错的。层元素必为 plan 本体引用
                    # （_safe_layers 保证），按 id 引用映射才唯一。
                    positions = {id(q): i for i, q in enumerate(plan)}
                    pending_left = sum(1 for q in plan if q["status"] == "pending")
                    for p in layers[idx]:
                        if p["status"] != "pending":
                            continue  # replan 后同批可能混有已完成步骤
                        p["status"] = "done"
                        p["result"] = summaries
                        pending_left -= 1
                        await self.engine.emit(state, "step_done", {
                            "step": positions[id(p)] + 1, "description": p["description"],
                            "remaining": pending_left,
                        })
            return {"error_kind": ERR_NONE, "last_error": "", "plan": plan,
                    "current_step": idx + 1, "plan_defect_streak": 0}

        codes = {o.get("error_code") for o in failed if o.get("error_code")}
        error_types = {o.get("error_type") for o in failed}
        errors = "; ".join(o["error"][:200] for o in failed)
        kind = self._classify_failure(codes, error_types, errors)

        await self.engine.emit(state, "critic", {
            "verdict": kind, "errors": errors[:300],
            "error_codes": sorted(str(c) for c in codes),
        })
        updates: dict = {"error_kind": kind, "last_error": errors}
        if kind == ERR_FATAL:
            updates["status"] = STATUS_FAILED
            updates["needs_final"] = True
        if kind == ERR_RETRYABLE:
            # 重试交给 compressor → react_step；连续重试计数防打转
            if state.get("iterations", 0) >= state["max_steps"]:
                updates["status"] = STATUS_FAILED
                updates["needs_final"] = True
        if kind == ERR_PLAN_DEFECT:
            # 自适应降级（P2-DAG）：plan_execute 连续 plan_defect 达阈值 → 退回
            # react 裸跑。计划被反复证明不可行时重规划只是空转（每次都烧 token）；
            # 降级保留已完成步骤的结果，把决策权交还模型自由推理。
            streak = state.get("plan_defect_streak", 0) + 1
            threshold = self.settings.adaptive_downgrade_after_replans
            if state["mode"] == "plan_execute" and 0 < threshold <= streak:
                switches = state.get("mode_switches", 0) + 1
                if switches > self.settings.max_mode_switches:
                    # H6 乒乓止损：切换次数已用尽，两条执行轨道都被反复证伪，
                    # 继续降级/升级只是换方向烧 token——直接判 failed 收尾
                    await self.engine.emit(state, "mode_thrashing_stopped", {
                        "mode_switches": switches - 1,
                        "limit": self.settings.max_mode_switches,
                        "consecutive_defects": streak, "error": errors[:200],
                    })
                    updates.update({"status": STATUS_FAILED, "needs_final": True})
                    return updates
                await self.engine.emit(state, "mode_downgraded", {
                    "from": "plan_execute", "to": "react",
                    "consecutive_defects": streak, "error": errors[:200],
                    "mode_switches": switches,
                })
                # error_kind 改记 retryable：route_after_critic 因此走
                # compressor → react_step 继续执行，不再回 planner 重规划
                updates.update({
                    "mode": "react",
                    "plan": [p for p in state.get("plan", []) if p["status"] == "done"],
                    "current_step": 0,
                    "plan_defect_streak": 0,
                    "mode_switches": switches,
                    "error_kind": ERR_RETRYABLE,
                })
            else:
                updates["plan_defect_streak"] = streak
        return updates

    @staticmethod
    def _classify_failure(codes: set, error_types: set, errors: str) -> str:
        """把工具失败分流为 plan_defect / fatal / retryable。

        优先按**结构化错误码**判定（可枚举、可表驱动测试）；仅当观测值完全没有错误码时
        才退回文本启发式——那条路径服务于从旧 checkpoint 恢复出来的历史观测值，
        新产生的观测值一律带码。
        """
        if codes & _PLAN_DEFECT_CODES:
            return ERR_PLAN_DEFECT    # 参数怎么修都非法 / 工具不存在 → 计划或能力缺陷
        if codes & _FATAL_CODES:
            return ERR_FATAL          # 安全与鉴权类：重试与重规划都无意义，直接终止
        if codes:
            return ERR_RETRYABLE      # 超时 / 网络 / 限流 / 上游 5xx 等 → 交回模型层决策

        # ---- 以下为文本兜底（内容与结构化改造前逐条一致）----
        if "未知工具" in errors:
            return ERR_PLAN_DEFECT
        if error_types & {"validation"}:
            return ERR_PLAN_DEFECT    # 自愈次数耗尽仍无法给出合法参数 → 计划/能力缺陷
        if looks_transient(errors):
            return ERR_RETRYABLE
        if any(k in errors for k in ("越界", "Permission", "禁止", "路径")):
            return ERR_FATAL
        return ERR_RETRYABLE

    # ---------- 上下文压缩 ----------
    async def compressor_node(self, state: AgentState) -> dict:
        messages = state.get("messages", [])
        # A2：阈值作用于「模型实际会收到的上下文」，而不是只看历史段。每一步还会注入
        # system（含 key_outputs）+ 任务提示，这部分可达上千 token —— 只按配置阈值
        # 压历史段，触发会系统性偏晚，真实 prompt 在压缩生效前就可能超出模型窗口。
        # 做法：先扣掉固定开销段，剩下的才是历史段允许占用的预算。
        overhead = estimate_messages_tokens(
            self._assemble_base({**state, "messages": []}))
        threshold = max(self.settings.compress_threshold_tokens - overhead,
                        MIN_HISTORY_BUDGET_TOKENS)
        if not needs_compression(messages, threshold):
            return {}
        compressed, summary, summary_tokens = await compress_messages(
            self.engine.llm, messages, threshold, model=self._model(state))
        if compressed is messages:
            # 历史短于保留窗口（中段为空，无从摘要）：原样返回时不发假事件，
            # 否则轨迹里会出现"压缩发生了但什么也没变"的误导记录
            return {}
        await self.engine.emit(state, "context_compressed", {
            "tokens_before": estimate_messages_tokens(messages),
            "tokens_after": estimate_messages_tokens(compressed),
            "fixed_overhead_tokens": overhead,
            "effective_history_threshold": threshold,
            "summary": summary[:300],
            "llm_tokens": summary_tokens,
            "key_outputs_retained": sorted(state.get("key_outputs", {}).keys()),
        })
        # H5：摘要是真实出网的 LLM 调用，token 必须上卷进 tokens_used——
        # 漏记会让预算与 L4 日配额在长轨迹上系统性低报（每压一次漏一次）
        return {"messages": compressed,
                "tokens_used": state["tokens_used"] + summary_tokens}

    # ---------- 收尾 ----------
    async def finisher_node(self, state: AgentState) -> dict:
        status = state.get("status", STATUS_DONE)
        key_outputs = state.get("key_outputs", {})

        if status == STATUS_CANCELED:
            await self.engine.emit(state, "task_canceled", {})
            # A3：取消/审批拒绝统一在此收尾。final_answer 已由上游节点给出时原样保留
            # （用户取消、审批拒绝各有专属文案），兜底防「canceled 但答案为空」。
            return {"status": STATUS_CANCELED,
                    "final_answer": state.get("final_answer") or "任务已被取消。"}

        if status == STATUS_FAILED:
            await self.engine.emit(state, "task_failed", {"error": state.get("last_error", "")[:300]})
            return {"final_answer": f"任务失败：{state.get('last_error', '')}"}

        if status == STATUS_BUDGET_EXCEEDED:
            plan = state.get("plan", [])
            body = prompts.BUDGET_EXCEEDED_TEMPLATE.format(
                reason=state.get("last_error", "预算耗尽"),
                done_count=sum(1 for p in plan if p["status"] == "done"),
                total_count=len(plan) or state.get("steps_used", 0),
                key_data="\n".join(f"- {k}: {v}" for k, v in key_outputs.items()) or "（无）",
            )
            await self.engine.emit(state, "task_done", {"degraded": True})
            return {"final_answer": body}

        # 正常收尾：react 模式模型已给出最终稿则直接采用；否则/或 plan 模式做汇总
        tokens_total = state["tokens_used"]
        if state.get("needs_final") and state.get("final_answer") and state["mode"] != "plan_execute":
            answer = state["final_answer"]
        else:
            plan = state.get("plan", [])
            step_results = "\n".join(
                f"- [{p['status']}] {p['description']}：{p.get('result', '')[:200]}" for p in plan
            )
            messages = [
                {"role": "system", "content": prompts.FINISH_SYSTEM + _now_context()},
                {"role": "user", "content": json.dumps({
                    "goal": state["goal"],
                    "key_outputs": key_outputs,
                    "step_results": step_results,
                    "recent": [m.get("content", "")[:200] for m in state.get("messages", [])[-4:]],
                }, ensure_ascii=False)},
            ]
            resp = await self.engine.llm.chat(messages, model=self._model(state))
            answer = resp.text
            # 汇总调用是任务的**最后一次 LLM 消耗**，必须上卷进 tokens_used：
            # 它是 L4 租户配额与成本统计（repository.tenant_token_usage 聚合
            # tasks.tokens_used）的事实来源，漏记会让成本系统性低报。
            # 事件流的 "tokens" 只是展示用增量，不参与配额判定。
            tokens_total += resp.tokens_used
            await self.engine.emit(state, "tokens", {"delta": resp.tokens_used})
        await self.engine.emit(state, "task_done", {"degraded": False, "answer_preview": answer[:200]})
        return {"final_answer": answer, "status": STATUS_DONE, "tokens_used": tokens_total}

    # ---------- 步后路由 ----------
    async def route_after_step(self, state: AgentState) -> str:
        if state.get("status") not in (STATUS_RUNNING, ""):
            return "finisher"
        if state.get("needs_final"):
            return "finisher"
        return "tool_executor"

    # ---------- HITL 审批门（P2-2） ----------
    async def approval_gate_node(self, state: AgentState) -> dict:
        """工具执行前的人工审批门：require_approval 任务每一轮工具执行前在此挂起。

        用 langgraph 原生 interrupt() 实现：
        - 首次到达：interrupt() 抛 GraphInterrupt，checkpoint 记录挂起与待审批清单，
          ainvoke 返回，任务行被置为 waiting_approval —— 图停在门上，**不执行任何工具**；
        - 人工批准后：以 Command(resume=True) 恢复，节点从头重跑，interrupt() 返回决策，
          放行进入 tool_executor；
        - 人工拒绝：Command(resume=False) 恢复，节点置 canceled，经条件边直接到 END。

        多轮工具的任务会在每轮工具前再次挂起 —— "每一步都经过人工确认"。
        require_approval=False 的任务在此节点是纯透传（返回空更新）。
        """
        if not state.get("require_approval"):
            return {}
        pending = state.get("pending_tool_calls") or []
        decision = interrupt({
            "goal": state.get("goal", ""),
            "round": state.get("iterations", 0),
            "tools": [{"name": c.get("name"), "arguments": c.get("arguments")}
                      for c in pending],
        })
        if decision:
            await self.engine.emit(state, "approval_granted", {"decision": True})
            return {"approval_pending": False}
        await self.engine.emit(state, "approval_rejected", {"decision": False})
        # A3：拒绝也走 finisher 统一收尾（旧实现直连 END：任务行没有 final_answer、
        # 不发 task_canceled 事件、pending 工具清单留在 checkpoint 里）。
        # final_answer 兜底文案在此给出，finisher 的 canceled 分支负责发事件与落状态。
        return {"approval_pending": False, "status": STATUS_CANCELED,
                "last_error": "人工审批拒绝", "pending_tool_calls": [],
                "final_answer": "人工审批拒绝，任务已被人工终止。"}

    def route_after_gate(self, state: AgentState) -> str:
        if state.get("status") not in (STATUS_RUNNING, ""):
            # A3：拒绝/取消 → finisher 收尾，而非直连 END
            return "finisher"
        return "tool_executor"

    async def route_after_critic(self, state: AgentState) -> str:
        # A1：任何终态都直接收尾。旧实现只拦 STATUS_FAILED —— tool_executor 在
        # 取消路径置 STATUS_CANCELED 后，边仍是无条件进 critic，靠 critic 头部
        # 守卫侥幸不评审；评审通过后会继续走 compressor→react，让已终止的任务
        # 多跑一步。这里把口径统一到「status 不再 running」，与 route_after_step 对称。
        if state.get("status") not in (STATUS_RUNNING, ""):
            return "finisher"
        kind = state.get("error_kind", ERR_NONE)
        if kind == ERR_PLAN_DEFECT:
            return "planner"
        return "compressor"  # ok / retryable 都先过压缩再进下一个决策步

    def _model(self, state: AgentState) -> str:
        if state.get("downgraded") and self.settings.llm_model_cheap:
            return self.settings.llm_model_cheap
        return self.settings.llm_model

    @staticmethod
    def _assistant_message(resp) -> dict:
        msg: dict[str, Any] = {"role": "assistant", "content": resp.text or ""}
        if resp.tool_calls:
            msg["tool_calls"] = [
                {"id": c.id, "type": "function",
                 "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)}}
                for c in resp.tool_calls
            ]
        return msg
