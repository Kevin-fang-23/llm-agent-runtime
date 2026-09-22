"""P2-留：本机免密放行 + 搜索相关性收紧。

两块都是"界面/检索行为"的回归护栏：

1. **本机免密**：一键启动脚本打开的是 127.0.0.1，页面必须开箱即用；但一旦经
   ngrok/反代暴露到公网，来源地址不再是回环地址，必须回到"要求密钥"。
   这里逐条钉住"谁能免密、谁不能"，尤其是 **X-Forwarded-For 伪造必须无效**
   （否则任何外部请求只要加一个头就能冒充本机，鉴权形同虚设）。

2. **相关性收紧**：旧实现中文整段比对（"北京天气"匹配不到"北京今日天气"，
   假阴性），英文裸子串（`climate change report` 命中 "Change your report
   settings"，假阳性），且 mock 源把"没查到"包装成"查到一条通用结果"。三处都要有
   断言，否则很容易改回去。

   注意 mock 那条**刻意要求不抛错**：mock 语料只有几个关键词，命中不了是夹具没覆盖
   而非上游失败；抛错会让所有"用 web_search 推进流程"的既有用例提前终止。
   正确做法是空结果 + 显式标注无证据。
"""
from __future__ import annotations

import pytest
from starlette.datastructures import Address

from app.tools.web_search import (
    _mock_search,
    _query_tokens,
    _relevance,
    is_relevant_result,
)

# ---------------- 本机免密 ----------------


def _as_remote(client, host: str):
    """把测试客户端的对端地址改成指定值，模拟"请求来自哪里"。"""
    client.raw._transport.client = Address(host, 45678)
    return client


def test_loopback_without_key_is_allowed(client):
    """本机（IPv4 回环）无任何凭据即可读写 —— 一键启动后的默认路径。"""
    c = _as_remote(client, "127.0.0.1")
    assert c.raw.get("/api/tasks").status_code == 200
    r = c.raw.post("/api/tasks", json={"goal": "本机免密任务", "mode": "react"})
    assert r.status_code == 202


def test_loopback_ipv6_without_key_is_allowed(client):
    c = _as_remote(client, "::1")
    assert c.raw.get("/api/tasks").status_code == 200


def test_ipv4_mapped_ipv6_is_allowed(client):
    c = _as_remote(client, "::ffff:127.0.0.1")
    assert c.raw.get("/api/tasks").status_code == 200


def test_remote_ip_without_key_is_denied(client):
    """外部来源（局域网/公网）必须回到密钥认证。"""
    c = _as_remote(client, "192.168.1.50")
    assert c.raw.get("/api/tasks").status_code == 401


def test_spoofed_x_forwarded_for_does_not_grant_access(client):
    """伪造 X-Forwarded-For: 127.0.0.1 不得被当成本机 —— 否则鉴权可被一行头绕过。"""
    c = _as_remote(client, "203.0.113.7")
    assert c.raw.get("/api/tasks",
                     headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 401
    assert c.raw.get("/api/tasks",
                     headers={"X-Real-IP": "127.0.0.1"}).status_code == 401


def test_remote_ip_with_valid_key_is_allowed(client):
    """公网演示的正解：带上租户密钥仍可用（免密只是本机便利，不是唯一通路）。"""
    c = _as_remote(client, "203.0.113.7")
    assert c.raw.get("/api/tasks",
                     headers={"X-API-Key": client.tenant_key}).status_code == 200


def test_admin_endpoints_are_not_covered_by_localhost_bypass(client):
    """管理员端点与租户 key 两套凭据，**不受本机免密影响**。"""
    c = _as_remote(client, "127.0.0.1")
    assert c.raw.get("/api/admin/tenants").status_code == 401
    assert c.raw.get("/api/admin/tenants",
                     headers={"X-Admin-Key": "test-admin-key"}).status_code == 200


def test_session_reports_mode_honestly(client):
    """会话端点必须如实回报"这次是怎么进来的"，页面据此决定要不要索要密钥。"""
    c = _as_remote(client, "127.0.0.1")
    body = c.raw.get("/api/session").json()
    assert body["mode"] == "passwordless" and body["passwordless"] is True
    assert body["tenant"] == "default"

    # 外部来源带 default 租户密钥时，租户名同样是 default，但**不是**免密 ——
    # 靠"租户名 == default"反推会把"仍需密钥"误报成"免密"，页面就漏掉填写入口。
    c2 = _as_remote(client, "203.0.113.7")
    body2 = c2.raw.get("/api/session", headers={"X-API-Key": client.tenant_key}).json()
    assert body2["mode"] == "api_key" and body2["passwordless"] is False


def test_localhost_bypass_can_be_disabled(client, monkeypatch):
    """严格模式：关掉开关后本机也要密钥（AUTH_LOCALHOST_BYPASS=false 的场景）。"""
    from app.config import get_settings

    monkeypatch.setenv("AUTH_LOCALHOST_BYPASS", "false")
    get_settings.cache_clear()
    try:
        c = _as_remote(client, "127.0.0.1")
        assert c.raw.get("/api/tasks").status_code == 401
        assert c.raw.get("/api/tasks",
                         headers={"X-API-Key": client.tenant_key}).status_code == 200
    finally:
        get_settings.cache_clear()


def test_key_still_wins_over_bypass_on_localhost(client):
    """本机带别的租户 key 时，身份应保持该租户（不被免密改写成 default），
    否则"本机调试另一个租户"会看到错的数据。"""
    from tests.conftest import ADMIN_HEADERS

    r = client.raw.post("/api/admin/tenants", json={"name": "local-other"},
                        headers=ADMIN_HEADERS)
    other_key = r.json()["api_key"]
    c = _as_remote(client, "127.0.0.1")
    body = c.raw.get("/api/session", headers={"X-API-Key": other_key}).json()
    assert body["tenant"] == "local-other" and body["mode"] == "api_key"


# ---------------- 失效密钥的容错（浏览器 localStorage 残留旧 key）----------------
# 真实事故：浏览器里残留着上一轮填的旧 API Key，前端把它塞进每一个请求头，
# 后端"密钥优先"直接 401 → 页面什么都点不动。而启动自检用 curl 不带密钥，
# 永远显示"本机免密已生效"—— 只测无密钥的自检恰好绕开了这条路径，盲区藏了三轮。
# 下面四条把这个盲区钉住：回环要容错、远程不许容错、语义不能被改写。


def test_loopback_with_stale_key_falls_back_to_passwordless(client):
    """回环 + 失效密钥 → 回落本机免密（而不是 401）。"""
    c = _as_remote(client, "127.0.0.1")
    body = c.raw.get("/api/session",
                     headers={"X-API-Key": "stale-key-from-old-session"}).json()
    assert body["mode"] == "passwordless"
    assert body["via"] == "localhost"      # 如实记录实际走的分支，别用租户名反推
    assert body["tenant"] == "default"


def test_loopback_with_stale_key_can_still_submit(client):
    """写操作同样要容错 —— 否则用户看到的还是"提交失败：未授权"。"""
    c = _as_remote(client, "127.0.0.1")
    r = c.raw.post("/api/tasks", json={"goal": "带失效密钥的本机提交", "mode": "react"},
                   headers={"X-API-Key": "stale-key-from-old-session"})
    assert r.status_code == 202


def test_remote_with_stale_key_is_still_denied(client):
    """安全边界不放松：外部来源带失效密钥仍 401（容错只对回环生效）。"""
    c = _as_remote(client, "203.0.113.7")
    assert c.raw.get("/api/tasks",
                     headers={"X-API-Key": "stale-key-from-old-session"}).status_code == 401


def test_loopback_with_disabled_tenant_key_stays_403(client):
    """被禁用租户的密钥是**真的、只是停用了** → 保持 403，
    不能被"无效密钥回落"当成无效混过去（否则停用租户反而能借免密进来）。"""
    from tests.conftest import ADMIN_HEADERS

    r = client.raw.post("/api/admin/tenants", json={"name": "to-be-disabled"},
                        headers=ADMIN_HEADERS)
    tid, key = r.json()["id"], r.json()["api_key"]
    assert client.raw.patch(f"/api/admin/tenants/{tid}", json={"enabled": False},
                            headers=ADMIN_HEADERS).status_code == 200
    c = _as_remote(client, "127.0.0.1")
    assert c.raw.get("/api/tasks", headers={"X-API-Key": key}).status_code == 403
    # 会话端点同样 403 —— 不能被"无效密钥回落"当成免密放行
    assert c.raw.get("/api/session", headers={"X-API-Key": key}).status_code == 403


# ---------------- 搜索相关性 ----------------


@pytest.mark.parametrize("query,hit,expected", [
    # 中文：部分重叠必须算命中（旧实现整段比对 → 假阴性）
    ("北京天气", {"title": "北京今日天气", "snippet": "晴，31℃。"}, True),
    # 中文：换城市必须不命中
    ("北京天气", {"title": "上海今日天气", "snippet": "雨。"}, False),
    # 英文：同词根（复/单数）算命中
    ("agent framework", {"title": "Agents in AI", "snippet": "many agents"}, True),
    # 英文：无关主题不命中
    ("agent framework", {"title": "上海天气预报", "snippet": "多云"}, False),
    # 泛化词：climate/change/report 里的泛化词不计分 → 不命中
    ("climate change report", {"title": "Change your report settings",
                               "snippet": "Click to change"}, False),
    # 年份词条不得因年份数字沾边而放行
    ("2025年NBA总冠军", {"title": "2025年_百度百科", "snippet": "农历乙巳蛇年"}, False),
    ("2025年NBA总冠军", {"title": "2025年NBA总冠军诞生", "snippet": "凯尔特人"}, True),
])
def test_relevance_matching(query, hit, expected):
    assert is_relevant_result([hit], query) is expected


def test_digits_are_not_relevance_tokens():
    """纯数字（年份/编号）刻意不入 token：否则"2024年_百度百科"会因年份沾边而放行。"""
    assert _query_tokens("2025年NBA总冠军") == ["nba", "总冠军"]


def test_relevance_is_normalized():
    """相关度必须在 0~1 之间（分母是"有判别力的关键词数"，不是全部 token 数）。"""
    assert 0.0 <= _relevance({"title": "北京今日天气", "snippet": ""},
                             _query_tokens("北京天气")) <= 1.0


def test_mock_source_marks_uncovered_query_as_no_evidence():
    """mock 源不得把"没查到"包装成"查到了一条内容"。

    这里刻意**断言不抛错**：mock 语料只有几个关键词，命中不了是"夹具没覆盖"而非
    上游失败；若让它抛错，所有"用 web_search 推进流程"的既有用例（预算耗尽、
    重规划编号）都会因工具报错提前终止。正确做法是返回空结果 + 明确标注无证据。
    """
    import asyncio

    r = asyncio.run(_mock_search("完全不存在的偏僻查询词xyz", 3))
    assert r["result"] == [], "没命中语料就不该返回任何结果条目"
    assert "未命中" in r["summary"] and "不得" in r["summary"], \
        "summary 必须明确告知模型：没有证据、不得编造"


def test_mock_source_still_answers_covered_query():
    import asyncio

    r = asyncio.run(_mock_search("北京天气", 3))
    assert r["result"], "被语料覆盖的查询必须正常返回"
    assert any("北京" in h["title"] for h in r["result"])
