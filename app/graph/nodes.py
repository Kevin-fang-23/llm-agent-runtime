"""状态机节点实现：planner / react_step / tool_executor / critic / compressor / finisher。

每个节点都是纯「读 state → 做事 → 返回部分更新」的函数（绑定在 AgentEngine 上），
节点内部通过 event sink 对外广播轨迹事件，自身不感知存储介质。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from typing import Any

from langgraph.types import interrupt

from app.core.budget import check_budget
from app.core.compressor import compress_messages, inject_key_outputs, needs_compression
from app.core.errors import ToolErrorCode
from app.core.llm import estimate_messages_tokens
from app.core.retry import backoff_delay, is_transient_error, looks_transient, retry_delay_hint
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
    AgentState,
)
from app.observability import metrics as obs_metrics
from app.observability import spans as obs_spans
from app.tools.registry import ToolExecutionError, ToolValidationError


def parse_json_loose(text: str) -> dict[str, Any] | None:
    """容错解析模型输出的 JSON（容忍代码块围栏与前后杂文本）。"""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    if start < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
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
    layers = _plan_layers(_linearize_plan(plan))
    if layers is not None:
        return layers
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
        steps = _parse_plan_steps((parsed or {}).get("steps"))
        if not steps:  # 规划失败兜底：目标本身就是一步
            steps = [{"id": "s1", "description": state["goal"],
                      "status": "pending", "result": ""}]

        # 自适应升级（P2-DAG）：react 模式重规划成功 → 切回 plan_execute。
        # 旧实现的缺口：react 重规划产出的计划没有任何执行轨道（plan_context
        # 仅 plan_execute 注入），重规划结果只是躺进 state.plan 的死数据。
        upgraded = is_replan and state["mode"] == "react"
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
            updates["plan_defect_streak"] = 0
        return updates

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

        # 组装上下文：system（含关键数据注入）+ 任务提示（每步重建，含当前计划步骤）
        # state.messages 只存执行历史（assistant/tool），不含 system 与任务提示，
        # 因此任务提示可携带最新计划进度，且压缩只作用于历史段。
        #
        # ⚠️ 不要把组装好的 base 写回 state.messages：
        #    base 每轮都会新增一份 system + user，若写回历史，下一轮又把它当历史拼进去，
        #    上下文随步数近似 O(n²) 膨胀。实测 4 轮工具调用时第 5 次 LLM 调用收到
        #    5 份 system + 5 份 user，token 从 202 涨到 5184（26 倍），
        #    并连带打穿 compressor（其切片假设 system 只出现在头部）。
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
            goal=state["goal"], used_steps=state["steps_used"],
            max_steps=state["max_steps"], plan_context=plan_context,
        )
        history = state.get("messages", [])
        base = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
            *history,
        ]

        resp = await self.engine.llm.chat(base, tools=self.engine.registry.to_openai_tools(), model=model)
        step_span.set_attribute("model", resp.model)
        step_span.set_attribute("tool_calls", len(resp.tool_calls))
        step_span.set_attribute("tokens", resp.tokens_used)
        # 只把本轮 assistant 决策追加进历史；system/user 每轮现构造，不入库
        messages = [*history, self._assistant_message(resp)]
        tokens = state["tokens_used"] + resp.tokens_used
        steps = state["steps_used"] + 1

        if resp.tool_calls:
            pending = [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in resp.tool_calls]
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
            return {"status": STATUS_CANCELED, "needs_final": True, "final_answer": "任务已被用户取消。"}

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
            async with sem:
                done = await journal.get_tool_execution(task_id, call["id"]) if journal else None
                if done is not None:
                    # 该 call_id 已执行过（进程在节点执行中被杀 → 恢复后重跑本节点）：
                    # 直接回放已提交的结果，不再触碰工具本身。
                    await self.engine.emit(state, "tool_replay", {
                        "tool": done["tool"], "call_id": call["id"],
                        "arguments": done["arguments"], "ok": done["ok"],
                        "reason": "checkpoint 重跑：命中工具执行流水，跳过重复执行",
                    })
                    obs = done
                else:
                    obs, attempts, repair_tokens = await self._execute_call(
                        state, call, name, args)
                    tokens += repair_tokens
                    selfheal_used += attempts
                    if journal is not None:
                        # 先落流水再返回：这样「工具已完成、checkpoint 未提交」的崩溃窗口
                        # 也能在恢复时被拦住。
                        await journal.record_tool_execution(task_id, call["id"], obs)

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

        results = await asyncio.gather(*(run_one(c) for c in calls))
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

        return {"messages": messages, "pending_tool_calls": [], "last_observations": observations,
                "key_outputs": key_outputs,
                "selfheal_total": state.get("selfheal_total", 0) + selfheal_used,
                "tokens_used": tokens}

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
                result = await self.engine.registry.execute(name, args)
            except ToolValidationError as e:
                healed = False
                last_err = str(e)
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
                        last_err = f"运行错误: {e2}"
                        break
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
            # 上游给了 Retry-After 就听它的，否则退回指数退避
            hint = retry_delay_hint(last_exc)
            delay = hint if hint is not None else backoff_delay(
                attempt, self.settings.retry_base_delay_s, self.settings.retry_max_delay_s)
            attempts_made = attempt
            await self.engine.emit(state, "tool_retry_scheduled", {
                "tool": name, "call_id": call["id"], "attempt": attempt,
                "delay_s": round(delay, 3),
                "delay_source": "retry_after" if hint is not None else "backoff",
                "error_code": _code_value(getattr(last_exc, "code", None)),
                "error": str(last_exc)[:300],
            })
            await asyncio.sleep(delay)
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
        if fixed is None:
            return None, resp.tokens_used
        try:
            spec.validate(fixed)
        except ToolValidationError:
            return None, resp.tokens_used
        return fixed, resp.tokens_used

    # ---------- 批判节点：错误分类与计划推进 ----------
    async def critic_node(self, state: AgentState) -> dict:
        observations = state.get("last_observations", [])
        failed = [o for o in observations if not o["ok"]]

        if not failed:
            plan = [dict(p) for p in state.get("plan", [])]
            idx = state.get("current_step", 0)
            if plan and state["mode"] == "plan_execute":
                # 整批推进（P2-DAG）：当前批里的全部 pending 步骤一并标 done。
                # 一批可能含多个并行步骤（diamond 形态），线性计划时只有一步。
                layers = _safe_layers(plan)
                if idx < len(layers):
                    summaries = "; ".join(
                        o["result"].get("summary", "")[:200] for o in observations if o["ok"]
                    )[:400]
                    for p in layers[idx]:
                        if p["status"] != "pending":
                            continue  # replan 后同批可能混有已完成步骤
                        p["status"] = "done"
                        p["result"] = summaries
                        await self.engine.emit(state, "step_done", {
                            "step": plan.index(p) + 1, "description": p["description"],
                            "remaining": sum(1 for q in plan if q["status"] == "pending"),
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
                await self.engine.emit(state, "mode_downgraded", {
                    "from": "plan_execute", "to": "react",
                    "consecutive_defects": streak, "error": errors[:200],
                })
                # error_kind 改记 retryable：route_after_critic 因此走
                # compressor → react_step 继续执行，不再回 planner 重规划
                updates.update({
                    "mode": "react",
                    "plan": [p for p in state.get("plan", []) if p["status"] == "done"],
                    "current_step": 0,
                    "plan_defect_streak": 0,
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
        threshold = self.settings.compress_threshold_tokens
        if not needs_compression(messages, threshold):
            return {}
        compressed, summary = await compress_messages(self.engine.llm, messages, threshold)
        await self.engine.emit(state, "context_compressed", {
            "tokens_before": estimate_messages_tokens(messages),
            "tokens_after": estimate_messages_tokens(compressed),
            "summary": summary[:300],
            "key_outputs_retained": sorted(state.get("key_outputs", {}).keys()),
        })
        return {"messages": compressed}

    # ---------- 收尾 ----------
    async def finisher_node(self, state: AgentState) -> dict:
        status = state.get("status", STATUS_DONE)
        key_outputs = state.get("key_outputs", {})

        if status == STATUS_CANCELED:
            await self.engine.emit(state, "task_canceled", {})
            return {"status": STATUS_CANCELED}

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
            await self.engine.emit(state, "tokens", {"delta": resp.tokens_used})
        await self.engine.emit(state, "task_done", {"degraded": False, "answer_preview": answer[:200]})
        return {"final_answer": answer, "status": STATUS_DONE}

    # ---------- 步后路由 ----------
    async def route_after_step(self, state: AgentState) -> str:
        if state.get("status") not in ("running", ""):
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
        return {"approval_pending": False, "status": STATUS_CANCELED,
                "last_error": "人工审批拒绝"}

    def route_after_gate(self, state: AgentState) -> str:
        if state.get("status") == STATUS_CANCELED:
            return "end"
        return "tool_executor"

    async def route_after_critic(self, state: AgentState) -> str:
        kind = state.get("error_kind", ERR_NONE)
        if state.get("status") == STATUS_FAILED:
            return "finisher"
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
