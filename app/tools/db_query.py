"""db_query 工具：对 SQLite 业务库执行只读查询。

安全约束：
  - 仅接受单条 SELECT/WITH 语句（拒绝多语句、拒绝任何写关键字）；
  - 以只读模式（mode=ro）打开连接，即使校验被绕过也无法写库；
  - 行数与单元格长度限制，防止把上下文撑爆。
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

_FORBIDDEN = (
    "insert", "update", "delete", "drop", "alter", "create", "attach",
    "detach", "pragma", "vacuum", "reindex", "replace", "grant", "revoke",
)


def validate_readonly_sql(sql: str) -> str:
    cleaned = sql.strip().rstrip(";").strip()
    if not cleaned:
        raise ValueError("SQL 不能为空")
    if ";" in cleaned:
        raise ValueError("仅允许单条语句（检测到多余的分号）")
    lowered = cleaned.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise ValueError("仅允许 SELECT/WITH 查询")
    for kw in _FORBIDDEN:
        # 粗粒度词边界检查，挡掉注释/CTE 名里夹带写操作的情况
        if f" {kw} " in f" {lowered} " or lowered.startswith(kw):
            raise ValueError(f"禁止的 SQL 关键字: {kw.upper()}")
    return cleaned


def _run_sync(db_path: str, sql: str, max_rows: int) -> dict[str, Any]:
    uri = Path(db_path).resolve().as_uri().replace("file:///", "file:///") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(sql)
        rows = cur.fetchmany(max_rows + 1)
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        cols = [d[0] for d in cur.description] if cur.description else []
        data = [
            {
                c: (v if v is None or isinstance(v, (int, float, str, bool)) else str(v)[:200])
                for c, v in zip(cols, row)
            }
            for row in rows
        ]
        return {"columns": cols, "rows": data, "truncated": truncated, "row_count": len(data)}
    finally:
        conn.close()


def make_db_query_handler(db_path: str):
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        sql = validate_readonly_sql(args["sql"])
        max_rows = int(args.get("max_rows", 20))
        result = await asyncio.to_thread(_run_sync, db_path, sql, max_rows)
        result["summary"] = (
            f"查询返回 {result['row_count']} 行"
            + ("（已截断）" if result["truncated"] else "")
            + f"，列: {result['columns']}"
        )
        return result

    return handler


DB_QUERY_SPEC_KWARGS = dict(
    name="db_query",
    description="对业务 SQLite 库执行只读 SQL 查询（仅 SELECT），返回行列数据。适合结构化数据统计。",
    input_schema={
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "单条 SELECT 语句"},
            "max_rows": {"type": "integer", "minimum": 1, "maximum": 100, "description": "最多返回行数，默认 20"},
        },
        "required": ["sql"],
    },
    key_result=True,
    key_output_limit=1500,
    retry_transient=True,  # 只读查询（连接以 mode=ro 打开），瞬时故障可原样重试
)
