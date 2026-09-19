"""工具层单元测试：Schema 校验 / 路径越界 / SQL 只读 / 自愈接口。"""
from __future__ import annotations

import sqlite3

import pytest

from app.tools.db_query import make_db_query_handler, validate_readonly_sql
from app.tools.registry import ToolExecutionError, ToolRegistry, ToolSpec


async def test_schema_validation_rejects_bad_args():
    reg = ToolRegistry()
    spec = ToolSpec(
        name="t", description="d",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        handler=None,
    )
    reg.register(spec)
    with pytest.raises(ToolExecutionError, match="未知工具"):
        await reg.execute("nope", {})
    # 校验失败在执行前抛出 ToolValidationError
    from app.tools.registry import ToolValidationError

    with pytest.raises(ToolValidationError):
        spec.validate({"q": 123})


async def test_file_ops_rejects_path_escape(registry):
    with pytest.raises(ToolExecutionError, match="越界"):
        await registry.execute("file_ops", {"action": "read", "path": "../../secrets.txt"})
    # 正常写读
    await registry.execute("file_ops", {"action": "write", "path": "a/b.txt", "content": "hello"})
    out = await registry.execute("file_ops", {"action": "read", "path": "a/b.txt"})
    assert out["result"] == "hello"


async def test_db_query_readonly(tmp_path, registry):
    db = tmp_path / "t.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE students (name TEXT, score INT)")
    conn.execute("INSERT INTO students VALUES ('张三', 90)")
    conn.commit()
    conn.close()

    import app.tools.db_query as dq

    assert validate_readonly_sql("SELECT * FROM students") == "SELECT * FROM students"
    with pytest.raises(ValueError):
        validate_readonly_sql("SELECT 1; DROP TABLE students")
    with pytest.raises(ValueError):
        validate_readonly_sql("DELETE FROM students")
    with pytest.raises(ValueError):
        validate_readonly_sql("INSERT INTO students VALUES (1)")

    handler = make_db_query_handler(str(db))
    res = await handler({"sql": "SELECT name, score FROM students"})
    assert res["rows"] == [{"name": "张三", "score": 90}]


async def test_registry_normalizes_output(registry):
    res = await registry.execute("web_search", {"query": "北京 天气"})
    assert res["tool"] == "web_search" and "elapsed_ms" in res
    key = registry.extract_key_output("web_search", res)
    assert key and "北京" in key
    assert registry.extract_key_output("code_run", {"result": "x"}) is None
