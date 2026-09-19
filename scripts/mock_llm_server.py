"""OpenAI 兼容 Mock LLM 服务器：本地端到端联调用，不花钱、不外联。

启动：python scripts/mock_llm_server.py   （监听 127.0.0.1:9100）
配合：LLM_BASE_URL=http://127.0.0.1:9100/v1 LLM_API_KEY=mock LLM_MODEL=mock-model
行为：对「搜索→计算→总结」类任务返回脚本化 Function Calling 响应。
"""
from __future__ import annotations

import json
import time
import uuid

from fastapi import FastAPI, Request

app = FastAPI(title="Mock OpenAI-compatible LLM")


def _resp(content=None, tool_calls=None):
    msg: dict = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = [
            {"id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
             "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}}
            for n, a in tool_calls
        ]
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "mock-model",
        "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    system = str((messages[0] or {}).get("content", "") if messages else "")
    tool_results = [m for m in messages if m.get("role") == "tool"]
    searched = any("web_search" in (m.get("name") or "") for m in tool_results)
    computed = any("code_run" in (m.get("name") or "") for m in tool_results)

    # 按系统提示区分引擎内的不同角色调用
    if "任务规划器" in system:
        return _resp(json.dumps({"steps": [
            "查询北京今天的天气",
            "查询上海今天的天气",
            "用沙箱计算两地最高气温温差并给出结论",
        ]}, ensure_ascii=False))
    if "参数修复器" in system:
        req = json.loads(messages[-1]["content"])
        return _resp(json.dumps({"query": "北京 今天 天气"}, ensure_ascii=False))
    if "任务交付器" in system or "摘要器" in system:
        return _resp("综合执行结果：北京晴 31℃、上海多云 28℃，温差 3℃，北京更热；上海有雨建议带伞。")

    if not searched:
        return _resp("两地天气相互独立，并行查询。", tool_calls=[
            ("web_search", {"query": "北京 今天 天气"}),
            ("web_search", {"query": "上海 今天 天气"}),
        ])
    if not computed:
        return _resp("提取两地气温并计算温差。", tool_calls=[
            ("code_run", {"code": "bj, sh = 31, 28\nprint(f'温差: {bj - sh}℃')"}),
        ])
    return _resp("今日北京晴 31℃，上海多云 28℃，最高气温相差 3℃。北京更热，出行注意防暑；上海有雨，记得带伞。")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=9100, log_level="warning")
