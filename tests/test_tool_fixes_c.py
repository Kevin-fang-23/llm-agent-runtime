"""C组（工具层）修复回归测试：

- C1 db_query：骨架 SQL 校验（注释/字面量里的禁用词不再误杀）+ 单元格长度上限；
- C2 file_ops：超大文件读取按字节截断并带标记，不再整读进内存；
- C3 subagent：父引擎经 ContextVar 解析，共享注册表被第二个引擎复用时事件不串扰；
- C4 weather：200 + 畸形 JSON 归为上游错误（可重试），不再被折叠成 INVALID_ARGS；
- C5 logging：BOCHA_API_KEY=xxx 这类下划线断词的 env 式密钥能被脱敏；
- C6 mcp_server：有副作用工具在描述与启动警示中显式标注。
"""
from __future__ import annotations

import sqlite3

import httpx
import pytest

from app.core.errors import ToolErrorCode
from app.observability.logging import redact_text
from app.tools.db_query import CELL_MAX_CHARS, make_db_query_handler, validate_readonly_sql
from app.tools.file_ops import MAX_FILE_BYTES, make_file_ops_handler
from app.tools.registry import ToolExecutionError


# ---------- C1 db_query ----------

def test_c1_comment_with_forbidden_word_passes():
    sql = "SELECT id FROM users -- remember to delete old rows\n/* update later */"
    assert validate_readonly_sql(sql).startswith("SELECT")


def test_c1_string_literal_with_forbidden_word_passes():
    sql = "SELECT 'insert into the void' AS label FROM t"
    assert validate_readonly_sql(sql).startswith("SELECT")


def test_c1_real_write_still_rejected():
    for sql in ("SELECT 1; DROP TABLE t", "DELETE FROM t",
                "UPDATE t SET a = 1", "SELECT 1 UNION SELECT 2 /*x*/ ; ATTACH 'f' AS db"):
        with pytest.raises(ValueError):
            validate_readonly_sql(sql)


def test_c1_word_boundary_catches_glued_keyword():
    # 旧版靠 " kw " 空格包裹，`1)delete from t` 这种无空格粘连能绕过；\b 挡得住
    with pytest.raises(ValueError):
        validate_readonly_sql("SELECT 1 UNION SELECT 2)delete from t")
    # 反向：标识符里含关键字子串（pragma_table_info）不应被误杀
    assert validate_readonly_sql("SELECT * FROM pragma_table_info('t')")


async def test_c1_long_cell_truncated(tmp_path):
    db = tmp_path / "biz.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE big (text TEXT)")
    conn.execute("INSERT INTO big VALUES (?)", ("字" * 5000,))
    conn.commit()
    conn.close()
    handler = make_db_query_handler(str(db))
    out = await handler({"sql": "SELECT text FROM big"})
    assert len(out["rows"][0]["text"]) <= CELL_MAX_CHARS


# ---------- C2 file_ops ----------

async def test_c2_oversized_read_truncated_with_marker(tmp_path):
    ws = tmp_path / "ws"
    handler = make_file_ops_handler(str(ws))
    (ws / "huge.txt").write_bytes(b"a" * (MAX_FILE_BYTES + 1024))
    out = await handler({"action": "read", "path": "huge.txt"})
    assert out["result"].endswith("…[文件超过 2MB，已截断]")
    assert len(out["result"]) <= MAX_FILE_BYTES + 40  # 截断标记的余量


async def test_c2_normal_read_unchanged(tmp_path):
    ws = tmp_path / "ws"
    handler = make_file_ops_handler(str(ws))
    (ws / "small.txt").write_text("hello 天气", encoding="utf-8")
    out = await handler({"action": "read", "path": "small.txt"})
    assert out["result"] == "hello 天气"


# ---------- C3 subagent ----------

async def test_c3_shared_registry_second_engine_does_not_hijack(settings):
    """同一注册表先后被两个引擎使用：子事件必须仍路由到**实际执行任务**的引擎。"""
    from app.core.llm import FakeScriptedLLM
    from app.graph.engine import AgentEngine
    from tests.conftest import collect_events, make_engine
    from tests.test_subagent import build_subagent_registry

    events, sink = collect_events()
    registry = build_subagent_registry(
        settings, llm_factory=lambda: FakeScriptedLLM([{"final": "子任务交付"}]))
    engine_a, _ = make_engine(settings, [
        {"tool": {"name": "subagent", "arguments": {"task": "查一下"}}},
        {"final": "父任务完成"},
    ], registry, event_sink=sink)
    # 旧缺陷触发点：再建一个引擎复用同一注册表，会把 handler.parent_engine 顶掉
    AgentEngine(settings=settings, llm=FakeScriptedLLM([]), registry=registry)

    final = await engine_a.run_task("t-c3", "C3 事件路由", "react", 100000, 24)
    assert final["status"] == "done"
    assert any(e["type"] == "subagent_event" for e in events), \
        "子 Agent 事件必须进父任务（engine_a）的轨迹，而不是被后来构建的引擎劫走"


# ---------- C4 weather ----------

async def test_c4_malformed_json_body_classified_as_upstream(monkeypatch):
    resp = httpx.Response(200, text="<html>wttr.in is busy</html>")

    async def fake_get(self, *a, **k):
        return resp

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    with pytest.raises(ToolExecutionError) as ei:
        from app.tools import weather
        await weather.handler({"city": "Shenzhen"})
    # 关键断言：JSONDecodeError 是 ValueError 子类，registry 默认会折叠成
    # INVALID_ARGS（critic 判 plan_defect 去改参数重规划）——必须显式盖掉
    assert ei.value.code == ToolErrorCode.UPSTREAM_5XX
    assert "畸形" in str(ei.value)


async def test_c4_missing_structure_classified_as_upstream(monkeypatch):
    resp = httpx.Response(200, json={"current_condition": []})  # 结构缺字段 → IndexError

    async def fake_get(self, *a, **k):
        return resp

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    with pytest.raises(ToolExecutionError) as ei:
        from app.tools import weather
        await weather.handler({"city": "Shenzhen"})
    assert ei.value.code == ToolErrorCode.UPSTREAM_5XX


# ---------- C5 logging 脱敏 ----------

def test_c5_env_style_keys_redacted():
    out = redact_text("启动参数 BOCHA_API_KEY=abcdef123456 已加载")
    assert "abcdef123456" not in out
    assert "[REDACTED]" in out
    assert "BOCHA" in out  # 键名保留，方便运维定位是哪把钥匙
    out2 = redact_text("export LLM_TOKEN=xyz789abc000")
    assert "xyz789abc000" not in out2


def test_c5_redaction_no_regression():
    # 字母粘连的伪键名不误伤；连接串规则仍保留 scheme/user 结构
    assert redact_text("monkeyspace=aaaaaaaa") == "monkeyspace=aaaaaaaa"
    out = redact_text("postgres://admin:hunter2pw@db:5432/app")
    assert out.startswith("postgres://admin:")
    assert "hunter2pw" not in out


# ---------- C6 mcp_server ----------

async def test_c6_side_effect_tools_annotated(settings):
    from app.mcp_server import build_mcp_server

    server = build_mcp_server(settings)
    assert set(server.exposed_side_effect_tools) == {"code_run", "file_ops"}
    tools = {t.name: t.description for t in await server.list_tools()}
    assert "副作用" in tools["code_run"]
    assert "副作用" in tools["file_ops"]
    assert "副作用" not in tools["web_search"]
    assert "副作用" not in tools["get_weather"]
    assert "副作用" not in tools["db_query"]
