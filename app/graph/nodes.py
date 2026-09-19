"""状态机节点实现：planner / react_step / tool_executor / critic / compressor / finisher。

每个节点都是纯「读 state → 做事 → 返回部分更新」的函数（绑定在 AgentEngine 上），
节点内部通过 event sink 对外广播轨迹事件，自身不感知存储介质。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from app.core.budget import check_budget
from app.core.compressor import compress_messages, inject_key_outputs, needs_compression
from app.core.llm import estimate_messages_tokens
from app.core.retry import backoff_delay, looks_transient
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


def init_state(task_id: str, goal: str, mode: str, max_tokens: int, max_steps: int) -> AgentState:
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
        status="running",
        last_error="",
        error_kind=ERR_NONE,
        pending_tool_calls=[],
        last_observations=[],
        needs_final=False,
        final_answer="",
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
            {"role": "system", "content": prompts.PLAN_SYSTEM.format(max_steps=state["max_steps"])},
            {"role": "user", "content": user},
        ]
        resp = await self.engine.llm.chat(messages, model=self._model(state))
        tokens = resp.tokens_used
        parsed = parse_json_loose(resp.text)
        steps = [str(s).strip() for s in (parsed or {}).get("steps", []) if str(s).strip()]
        if not steps:  # 规划失败兜底：目标本身就是一步
            steps = [state["goal"]]

        if is_replan:
            plan = [p for p in state.get("plan", []) if p["status"] == "done"]
            new_plan = [
                *plan,
                *[{"id": f"s{i + 1}", "description": d, "status": "pending", "result": ""}
                  for i, d in enumerate(steps)],
            ]
            event_type = "replan"
        else:
            new_plan = [{"id": f"s{i + 1}", "description": d, "status": "pending", "result": ""}
                        for i, d in enumerate(steps)]
            event_type = "plan_created"

        current = next((i for i, p in enumerate(new_plan) if p["status"] == "pending"), len(new_plan))
        await self.engine.emit(state, event_type, {
            "steps": steps, "plan_size": len(new_plan),
            "tokens": tokens, "raw": resp.text[:500],
        })
        return {"plan": new_plan, "current_step": current, "tokens_used": state["tokens_used"] + tokens,
                "last_error": "", "error_kind": ERR_NONE}

    # ---------- ReAct 决策步 ----------
    async def react_step_node(self, state: AgentState) -> dict:
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
        # plan 模式：全部步骤已执行完 → 直接进入汇总
        if (state["mode"] == "plan_execute" and state.get("plan")
                and state.get("current_step", 0) >= len(state["plan"])):
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
        system = inject_key_outputs(prompts.REACT_SYSTEM, state.get("key_outputs", {}))
        plan_context = ""
        if state.get("plan") and state["mode"] == "plan_execute":
            idx = state.get("current_step", 0)
            plan = state["plan"]
            if idx < len(plan):
                plan_context = prompts.PLAN_CONTEXT_LINE.format(
                    plan_text=_plan_text(plan), current_step=idx + 1,
                    step_desc=plan[idx]["description"],
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
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "name": call["name"],
                "content": json.dumps(
                    obs.get("result") if obs["ok"] else {"error": obs["error"]},
                    ensure_ascii=False, default=str,
                )[:6000],
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
                })

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
        try:
            result = await self.engine.registry.execute(name, args)
        except ToolValidationError as e:
            healed = False
            last_err = str(e)
            while attempts < self.settings.max_selfheal_retries:
                attempts += 1
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
                return ({"ok": False, "tool": name, "arguments": args,
                         "error": last_err, "error_type": "validation"},
                        attempts, repair_tokens_used)
        except ToolExecutionError as e:
            result, last_err = await self._retry_transient(state, call, name, args, str(e))
            if result is None:
                return ({"ok": False, "tool": name, "arguments": args,
                         "error": last_err, "error_type": "runtime"},
                        attempts, repair_tokens_used)

        return ({"ok": True, "tool": name, "arguments": args, "result": result},
                attempts, repair_tokens_used)

    async def _retry_transient(self, state: AgentState, call: dict, name: str, args: dict,
                               first_error: str) -> tuple[dict | None, str]:
        """对瞬时运行错**原样重试**同一调用（指数退避，上限 retry_max_attempts）。

        返回 (成功的结果, 最后一次错误文本)；始终失败时结果为 None。

        与自愈循环的分工：
          - 自愈修「参数错」（ToolValidationError，改参数后重试）
          - 本方法重试「运行错」（ToolExecutionError，参数一字不改）
        升级阶梯：本方法用尽 → 观测值带 runtime 错误 → critic 判 retryable →
        交回 react_step 由模型决定换工具还是换策略。所以这里只做有限次盲重试，
        不试图在这里"解决"问题。

        三重闸门（缺一不可）：
          1. 错误文本命中 TRANSIENT_MARKERS —— 永久性错误重试只是浪费；
          2. 工具声明 retry_transient —— 超时不等于失败，有副作用的工具盲目重试会做两遍；
          3. 未取消 —— 协作式取消要能立刻生效，不能卡在退避的 sleep 里。
        """
        # 先判瞬时性：非瞬时（含"未知工具"）直接退出，也避免为它去查不存在的 spec
        if not looks_transient(first_error):
            return None, first_error
        try:
            retry_ok = self.engine.registry.get(name).retry_transient
        except ToolExecutionError:
            return None, first_error  # 工具不在注册表里（错误正来自 get）：不可重试
        if not retry_ok:
            return None, first_error

        last_err = first_error
        attempts_made = 0
        for attempt in range(1, self.settings.retry_max_attempts + 1):
            if not looks_transient(last_err):
                break  # 非瞬时错误：重试无意义
            if self.engine.is_canceled(state["task_id"]):
                last_err = f"{last_err}（任务已取消，不再重试）"
                break
            delay = backoff_delay(attempt, self.settings.retry_base_delay_s,
                                  self.settings.retry_max_delay_s)
            attempts_made = attempt
            await self.engine.emit(state, "tool_retry_scheduled", {
                "tool": name, "call_id": call["id"], "attempt": attempt,
                "delay_s": round(delay, 3), "error": last_err[:300],
            })
            await asyncio.sleep(delay)
            try:
                result = await self.engine.registry.execute(name, args)
            except ToolExecutionError as e:
                last_err = str(e)
                continue
            except ToolValidationError as e:  # 防御分支：参数未改却报校验错，不可重试
                last_err = f"重试期间参数校验失败: {e}"
                break
            await self.engine.emit(state, "tool_retry_success", {
                "tool": name, "call_id": call["id"], "attempt": attempt,
                "delay_s": round(delay, 3),
            })
            return result, last_err

        if attempts_made >= self.settings.retry_max_attempts > 0:
            await self.engine.emit(state, "tool_retry_exhausted", {
                "tool": name, "call_id": call["id"], "attempts": attempts_made,
                "error": last_err[:300],
            })
        return None, last_err

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
            if plan and state["mode"] == "plan_execute" and idx < len(plan):
                plan[idx]["status"] = "done"
                plan[idx]["result"] = "; ".join(
                    o["result"].get("summary", "")[:200] for o in observations if o["ok"]
                )[:400]
                await self.engine.emit(state, "step_done", {
                    "step": idx + 1, "description": plan[idx]["description"],
                    "remaining": len(plan) - idx - 1,
                })
            return {"error_kind": ERR_NONE, "last_error": "", "plan": plan,
                    "current_step": idx + 1}

        kinds = {o["error_type"] for o in failed}
        errors = "; ".join(o["error"][:200] for o in failed)
        if any("未知工具" in o["error"] for o in failed):
            kind = ERR_PLAN_DEFECT
        elif kinds & {"validation"}:
            kind = ERR_PLAN_DEFECT  # 自愈次数耗尽仍无法给出合法参数 → 计划/能力缺陷
        elif looks_transient(errors):
            kind = ERR_RETRYABLE
        elif any(k in errors for k in ("越界", "Permission", "禁止", "路径")):
            kind = ERR_FATAL
        else:
            kind = ERR_RETRYABLE

        await self.engine.emit(state, "critic", {"verdict": kind, "errors": errors[:300]})
        updates: dict = {"error_kind": kind, "last_error": errors}
        if kind == ERR_FATAL:
            updates["status"] = STATUS_FAILED
            updates["needs_final"] = True
        if kind == ERR_RETRYABLE:
            # 重试交给 compressor → react_step；连续重试计数防打转
            if state.get("iterations", 0) >= state["max_steps"]:
                updates["status"] = STATUS_FAILED
                updates["needs_final"] = True
        return updates

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
                {"role": "system", "content": prompts.FINISH_SYSTEM},
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
