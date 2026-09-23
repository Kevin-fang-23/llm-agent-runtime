"""P1 批次一（7-9）回归用例。

7. registry.execute 的死代码删除后：校验错误仍在执行前抛出，其余折叠行为不变；
8. span() 签名不再携带从未使用的 sink 参数（说谎 API）；
9. 凭据字段 SecretStr（repr 天然打码）+ checkpoint_durability Literal 构造期即校验。
"""
from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings
from app.tools.registry import ToolExecutionError, ToolRegistry, ToolSpec, ToolValidationError


# ---------------- 7. registry 死代码删除 ----------------

def _spec(handler, **kw) -> ToolSpec:
    return ToolSpec(name="t", description="d",
                    input_schema={"type": "object",
                                  "properties": {"q": {"type": "string"}},
                                  "required": ["q"]},
                    handler=handler, **kw)


async def test_validation_error_still_raises_before_execution():
    """删除 except ToolValidationError: raise 后语义不变：校验在执行之外，原样抛。"""
    called = []

    async def handler(args):
        called.append(args)
        return {"result": "ok"}

    reg = ToolRegistry()
    reg.register(_spec(handler))
    with pytest.raises(ToolValidationError):
        await reg.execute("t", {"q": 123})
    assert called == [], "校验失败不得触达 handler"


async def test_handler_errors_still_fold_with_codes():
    async def bad(args):
        raise ValueError("入参不合法")

    reg = ToolRegistry()
    reg.register(_spec(bad))
    with pytest.raises(ToolExecutionError) as ei:
        await reg.execute("t", {"q": "x"})
    from app.core.errors import ToolErrorCode
    assert ei.value.code is ToolErrorCode.INVALID_ARGS


# ---------------- 8. span() 不再携带未用 sink 参数 ----------------

def test_span_signature_has_no_sink_param():
    import inspect

    from app.observability.spans import span

    assert "sink" not in inspect.signature(span).parameters


def test_span_still_records_to_buffer_without_sink():
    from app.observability import spans as obs

    obs.reset_buffer()
    with obs.span(obs.KIND_TOOL, "web_search") as sp:
        sp.set_attribute("outcome", "ok")
    closed = obs.BUFFER.peek()
    assert len(closed) == 1 and closed[0]["status"] == "ok"
    obs.reset_buffer()


# ---------------- 9. SecretStr + Literal 校验 ----------------

_SECRETS = {"llm_api_key": "sk-secret-llm",
            "admin_api_key": "adm-secret-admin",
            "bocha_api_key": "bc-secret-bocha"}


def test_settings_repr_never_leaks_plaintext_secrets():
    """整体 repr/str 只出现掩码 —— 这是 SecretStr 要买的唯一东西。"""
    s = Settings(**_SECRETS)
    for text in (repr(s), str(s)):
        for plain in _SECRETS.values():
            assert plain not in text
    assert s.llm_api_key.get_secret_value() == "sk-secret-llm"


def test_empty_secret_is_falsy_for_not_configured_checks(monkeypatch):
    """旧代码靠 falsy str 判"未配置"；SecretStr("") 本身是 truthy，
    读取点必须 get_secret_value() 后再判 —— 这里钉住解包后的语义。

    空串 setenv 覆盖 .env（与 test_bocha_source 的约定同源）：默认构造会把
    开发者 .env 里的真实 key 读进来，断言就绑到了本机配置。
    """
    monkeypatch.setenv("LLM_API_KEY", "")
    s = Settings()
    assert s.llm_api_key.get_secret_value() == ""
    assert not s.llm_api_key.get_secret_value()
    assert isinstance(s.admin_api_key, SecretStr)


def test_checkpoint_durability_rejects_typo_at_construction():
    """配错在构造期即炸，不再拖到首个 checkpoint 才被队列折叠成 failed。"""
    with pytest.raises(ValidationError):
        Settings(checkpoint_durability="Sync")
    with pytest.raises(ValidationError):
        Settings(checkpoint_durability="sync ")
    assert Settings(checkpoint_durability="exit").checkpoint_durability == "exit"


def test_llm_api_key_env_override_still_reaches_call_site(monkeypatch):
    """env 覆盖 → SecretStr → get_secret_value 全链路通（含空串覆盖 .env 的测试约定）。"""
    from app.config import get_settings

    monkeypatch.setenv("LLM_API_KEY", "sk-from-env")
    get_settings.cache_clear()
    assert get_settings().llm_api_key.get_secret_value() == "sk-from-env"
    monkeypatch.setenv("LLM_API_KEY", "")
    get_settings.cache_clear()
    assert get_settings().llm_api_key.get_secret_value() == ""
    get_settings.cache_clear()
