"""get_weather 工具：wttr.in 后端的真实天气查询（无需 API key）。

返回当前实况（气温/湿度/体感/风况）与今日最高最低温，
summary 进入不可压缩关键数据。
"""
from __future__ import annotations

from typing import Any

import httpx

_UA = {"User-Agent": "curl/8.0"}  # wttr.in 对 curl UA 返回干净 JSON


async def handler(args: dict[str, Any]) -> dict[str, Any]:
    city = args["city"].strip()
    if not city:
        raise ValueError("city 不能为空")
    async with httpx.AsyncClient(timeout=12.0) as client:
        r = await client.get(f"https://wttr.in/{city}", params={"format": "j1"}, headers=_UA)
    if r.status_code != 200:
        raise RuntimeError(f"天气查询失败（HTTP {r.status_code}），请检查城市名（建议用拼音/英文名）")
    d = r.json()
    cur = d["current_condition"][0]
    today = d.get("weather", [{}])[0]
    desc = (cur.get("weatherDesc") or [{}])[0].get("value", "")
    result = {
        "city": city,
        "temp_c": cur.get("temp_C"),
        "feels_like_c": cur.get("FeelsLikeC"),
        "humidity_percent": cur.get("humidity"),
        "wind_kmph": cur.get("windspeedKmph"),
        "desc": desc,
        "today_max_c": today.get("maxtempC"),
        "today_min_c": today.get("mintempC"),
    }
    return {
        "result": result,
        "summary": (f"{city} 实时天气：{result['desc']}，气温 {result['temp_c']}℃"
                    f"（体感 {result['feels_like_c']}℃），湿度 {result['humidity_percent']}%，"
                    f"风速 {result['wind_kmph']}km/h；今日 {result['today_min_c']}~{result['today_max_c']}℃"),
    }


WEATHER_SPEC_KWARGS = dict(
    name="get_weather",
    description="查询全球城市实时天气（气温/湿度/体感/风况/今日最高最低温），数据源为公开气象服务。city 传拼音或英文名，如 Shenzhen、Beijing、Shanghai。",
    input_schema={
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "城市名（拼音或英文），如 Shenzhen"},
        },
        "required": ["city"],
    },
    key_result=True,
    key_output_limit=500,
    retry_transient=True,  # 只读查询，瞬时故障原样重试是安全的
)
