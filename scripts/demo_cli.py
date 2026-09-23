"""CLI 演示：M1 执行内核的最小可演示入口。

离线（默认，无需 API Key，且模型与工具都不出网）:
  python scripts/demo_cli.py --offline
在线（任意 OpenAI 兼容模型，需 .env 配置）:
  python scripts/demo_cli.py --goal "查北京和上海今天的天气并计算温差"

⚠️ --offline 会同时冻结两层：
  - 模型层：FakeScriptedLLM（离线脚本化响应）
  - 工具层：search_provider 强制为 mock
  只冻结模型层是不够的——web_search 会按 .env 的 SEARCH_PROVIDER 真的出网，
  此时这里跑的既不是"离线"也不是"可复现"。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.core.llm import FakeScriptedLLM, OpenAIChatLLM  # noqa: E402
from app.graph.engine import AgentEngine  # noqa: E402
from app.tools.factory import build_default_registry  # noqa: E402
from app.worker.local_queue import new_task_id  # noqa: E402

OFFLINE_SCRIPT = [
    {"thought": "两地天气相互独立，一次并行查询。", "tools": [
        {"name": "web_search", "arguments": {"query": "北京 今天 天气"}},
        {"name": "web_search", "arguments": {"query": "上海 今天 天气"}},
    ]},
    {"thought": "拿到两地最高气温（北京 31℃、上海 28℃），用沙箱精确计算温差。",
     "tool": {"name": "code_run", "arguments": {"code": "bj, sh = 31, 28\nprint(f'温差: {bj - sh}℃')"}}},
    {"final": "今日天气对比：北京晴，31℃；上海多云转小雨，28℃。两地最高气温相差 3℃，北京更高，外出注意防暑/带伞。"},
]

TYPE_COLOR = {
    "plan_created": "\033[95m", "replan": "\033[95m",
    "llm_step": "\033[94m", "tool_result": "\033[92m",
    "tool_error": "\033[91m", "tool_validation_failed": "\033[91m",
    "selfheal_success": "\033[92m", "critic": "\033[93m",
    "tool_replay": "\033[95m",  # 命中执行流水、跳过重复执行
    "tool_retry_scheduled": "\033[93m", "tool_retry_success": "\033[92m",
    "tool_retry_exhausted": "\033[91m",
    "budget_downgrade": "\033[93m", "budget_exceeded": "\033[93m",
    "context_compressed": "\033[96m", "task_done": "\033[92m",
    "task_failed": "\033[91m", "task_canceled": "\033[93m",
}


async def print_event(event: dict) -> None:
    color = TYPE_COLOR.get(event["type"], "\033[0m")
    payload = json.dumps(event["payload"], ensure_ascii=False, default=str)
    print(f"  {color}[#{event['seq']:>3}] {event['type']:<22}\033[0m {payload[:220]}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="LLM Agent Runtime CLI 演示")
    parser.add_argument("--goal", type=str, default="查北京和上海今天的天气，并计算两地温差")
    parser.add_argument("--mode", choices=["react", "plan_execute"], default="react")
    parser.add_argument("--offline", action="store_true",
                        help="脚本化假模型 + 强制 mock 搜索（模型与工具均不出网）")
    parser.add_argument("--max-steps", type=int, default=24)
    args = parser.parse_args()

    settings = get_settings()
    if args.offline:
        # 冻结工具层：否则 web_search 会按 .env 的 SEARCH_PROVIDER（常为 bing）真的出网
        settings = settings.model_copy(update={"search_provider": "mock"})
    registry = build_default_registry(settings)
    task_id = new_task_id()

    if args.offline:
        llm = FakeScriptedLLM(OFFLINE_SCRIPT)
    else:
        llm_api_key = settings.llm_api_key.get_secret_value()
        if not llm_api_key:
            sys.exit("未配置 LLM_API_KEY：请在 .env 中配置，或使用 --offline 离线演示")
        llm = OpenAIChatLLM(settings.llm_base_url, llm_api_key, settings.llm_model)

    engine = AgentEngine(settings=settings, llm=llm, registry=registry, event_sink=print_event)
    mode_desc = ("离线（假模型 + mock 搜索，不出网）" if args.offline
                 else f"在线（模型 {settings.llm_model} + 搜索 {settings.search_provider}）")
    print(f"\n▶ 任务 {task_id} [{args.mode}] {args.goal}")
    print(f"  执行环境: {mode_desc}\n")
    final = await engine.run_task(task_id, args.goal, args.mode,
                                  settings.default_max_tokens, args.max_steps)
    print(f"\n{'=' * 60}")
    print(f"状态: {final['status']} | 步数: {final['steps_used']} | "
          f"tokens≈{final['tokens_used']} | 自愈: {final.get('selfheal_total', 0)} 次")
    print(f"---- 最终交付 ----\n{final['final_answer']}")


if __name__ == "__main__":
    asyncio.run(main())
