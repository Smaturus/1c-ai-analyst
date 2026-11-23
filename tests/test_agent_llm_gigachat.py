"""Адаптер GigaChat на ``langchain-gigachat`` — без сети (fake GigaChat).

Проверяется транспортный слой, а не граф: маппинг ``LLMSettings`` в
конструктор, вызов ``bind_tools(tools, tool_choice="auto")``, форма
``AIMessage`` → :class:`CompletionResult` и отказ на некорректных tool call.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from onec_analyst import llm_agent
from onec_analyst.agent import llm as agent_llm
from onec_analyst.agent.llm import (
    LangChainGigaChatClient,
    make_agent_client,
    normalize_tool_schemas,
)
from onec_analyst.agent.tools import build_agent_tools
from onec_analyst.llm_agent import (
    CompletionResult,
    LLMClient,
    LLMError,
    LLMSettings,
    ToolCall,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _settings(**overrides: Any) -> LLMSettings:
    """Настройки GigaChat с фиктивным ключом (в сеть не ходим)."""
    values = dict(
        provider="gigachat",
        model="GigaChat-2-Pro",
        api_key="fake-credentials",
        base_url="https://api.giga.chat/v1",
        timeout=15.0,
        temperature=0.2,
        scope="GIGACHAT_API_CORP",
        ca_bundle=r"C:\certs\root.crt",
        verify_tls=True,
    )
    values.update(overrides)
    return LLMSettings(**values)


def _message(
    content: Any = "",
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    invalid: list[dict[str, Any]] | None = None,
) -> SimpleNamespace:
    """Ответ, который по атрибутам совпадает с ``AIMessage``."""
    return SimpleNamespace(
        content=content,
        tool_calls=list(tool_calls or []),
        invalid_tool_calls=list(invalid or []),
    )


class FakeGigaChat:
    """Подмена ``langchain_gigachat.GigaChat``: запоминает вызовы, отдаёт ответы."""

    instances: list["FakeGigaChat"] = []
    responses: list[SimpleNamespace] = []
    init_error: BaseException | None = None
    bind_error: BaseException | None = None
    invoke_error: BaseException | None = None

    def __init__(self, **kwargs: Any) -> None:
        if FakeGigaChat.init_error is not None:
            raise FakeGigaChat.init_error
        self.kwargs = kwargs
        self.bound: list[dict[str, Any]] = []
        self.messages: list[list[Any]] = []
        self.invocations = 0
        FakeGigaChat.instances.append(self)

    def bind_tools(self, tools, *, tool_choice=None, **kwargs: Any):
        if FakeGigaChat.bind_error is not None:
            raise FakeGigaChat.bind_error
        self.bound.append({"tools": list(tools), "tool_choice": tool_choice})
        return self

    def invoke(self, messages):
        self.messages.append(list(messages))
        self.invocations += 1
        if FakeGigaChat.invoke_error is not None:
            raise FakeGigaChat.invoke_error
        if not FakeGigaChat.responses:
            raise AssertionError("fake GigaChat: не задан ответ")
        response = FakeGigaChat.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


@pytest.fixture(autouse=True)
def _reset_fake(monkeypatch: pytest.MonkeyPatch):
    """Чистая очередь ответов и подмена ленивого импорта на каждый тест."""
    FakeGigaChat.instances = []
    FakeGigaChat.responses = []
    FakeGigaChat.init_error = None
    FakeGigaChat.bind_error = None
    FakeGigaChat.invoke_error = None
    monkeypatch.setattr(agent_llm, "_gigachat_class", lambda: FakeGigaChat)


# --------------------------------------------------------------------------- #
# Ленивый импорт и маппинг настроек
# --------------------------------------------------------------------------- #
def test_langchain_gigachat_is_not_imported_with_module() -> None:
    """Импорт ``agent.llm`` не тянет SDK: офлайн-тесты и другие провайдеры чисты."""
    code = (
        "import sys; "
        f"sys.path.insert(0, {str(REPO_ROOT / 'src')!r}); "
        "import onec_analyst.agent.llm; "
        "print('langchain_gigachat' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def test_constructor_maps_llm_settings() -> None:
    """credentials/scope/model/base_url/TLS/ca_bundle/temperature — как в ТЗ."""
    client = LangChainGigaChatClient(_settings())
    FakeGigaChat.responses = [_message("ok")]
    assert client.complete([{"role": "user", "content": "привет"}]).content == "ok"

    assert len(FakeGigaChat.instances) == 1
    assert FakeGigaChat.instances[0].kwargs == {
        "credentials": "fake-credentials",
        "scope": "GIGACHAT_API_CORP",
        "model": "GigaChat-2-Pro",
        "base_url": "https://api.giga.chat/v1",
        "verify_ssl_certs": True,
        "ca_bundle_file": r"C:\certs\root.crt",
        "temperature": 0.2,
        "timeout": 15.0,
    }


@pytest.mark.parametrize(
    "model", ["GigaChat-2-Pro", "GigaChat-Max", "Custom-Model"]
)
def test_settings_model_is_the_only_model_source(model: str) -> None:
    """Три разные ``settings.model`` доходят до ``GigaChat(model=...)`` как есть."""
    client = LangChainGigaChatClient(_settings(model=model))
    FakeGigaChat.responses = [_message("ok")]
    client.complete([{"role": "user", "content": "привет"}])

    assert [item.kwargs["model"] for item in FakeGigaChat.instances] == [model]


def test_each_client_constructs_its_own_model() -> None:
    """Новый ``make_agent_client(settings)`` не переиспользует чужую модель."""
    FakeGigaChat.responses = [_message("ok"), _message("ok"), _message("ok")]
    first = make_agent_client(_settings(model="GigaChat-2-Pro"))
    second = make_agent_client(_settings(model="Custom-Model"))
    first.complete([{"role": "user", "content": "u"}])
    second.complete([{"role": "user", "content": "u"}])

    assert [item.kwargs["model"] for item in FakeGigaChat.instances] == [
        "GigaChat-2-Pro",
        "Custom-Model",
    ]
    # Повторный вызов того же клиента не создаёт второй GigaChat.
    first.complete([{"role": "user", "content": "u"}])
    assert len(FakeGigaChat.instances) == 2


def test_constructor_error_becomes_llm_error_with_model() -> None:
    """Ошибка ``GigaChat(...)`` → LLMError с именем модели, а не сырое исключение."""
    FakeGigaChat.init_error = RuntimeError("model not found")
    client = LangChainGigaChatClient(_settings(model="Custom-Model"))

    with pytest.raises(LLMError) as info:
        client.complete([{"role": "user", "content": "u"}])

    text = str(info.value)
    assert "Custom-Model" in text
    assert "RuntimeError" in text
    assert "model not found" in text


def test_constructor_error_does_not_leak_credentials() -> None:
    """Credentials не попадают в текст LLMError, даже если SDK их упомянул."""
    FakeGigaChat.init_error = RuntimeError("bad credentials fake-credentials")
    client = LangChainGigaChatClient(_settings())

    with pytest.raises(LLMError) as info:
        client.complete([{"role": "user", "content": "u"}])

    assert "fake-credentials" not in str(info.value)
    assert "***" in str(info.value)


def test_invoke_error_becomes_llm_error_with_model() -> None:
    """Ошибка вызова (404 model not found) → LLMError с именем модели."""
    FakeGigaChat.invoke_error = Exception("404 model not found")
    client = LangChainGigaChatClient(_settings(model="GigaChat-Max"))

    with pytest.raises(LLMError) as info:
        client.complete([{"role": "user", "content": "u"}])

    text = str(info.value)
    assert "GigaChat-Max" in text
    assert "404" in text


def test_bind_tools_error_becomes_llm_error_with_model() -> None:
    """Ошибка ``bind_tools`` (например, схема) тоже заворачивается в LLMError."""
    FakeGigaChat.bind_error = ValueError("schema rejected")
    client = LangChainGigaChatClient(_settings(model="GigaChat-Max"))

    with pytest.raises(LLMError) as info:
        client.complete([{"role": "user", "content": "u"}], tools=build_agent_tools())

    text = str(info.value)
    assert "GigaChat-Max" in text
    assert "ValueError" in text


def test_adapter_does_not_touch_models_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Адаптер не ходит в ``GET /models`` и не подменяет модель по списку."""
    calls: list[str] = []

    class GuardedFake(FakeGigaChat):
        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            calls.append("bind_tools")
            return super().bind_tools(tools, tool_choice=tool_choice, **kwargs)

        def invoke(self, messages):
            calls.append("invoke")
            return super().invoke(messages)

    monkeypatch.setattr(agent_llm, "_gigachat_class", lambda: GuardedFake)
    FakeGigaChat.responses = [_message("ok")]

    client = LangChainGigaChatClient(_settings(model="Custom-Model"))
    client.complete([{"role": "user", "content": "u"}])

    assert calls == ["invoke"]
    assert FakeGigaChat.instances[0].kwargs["model"] == "Custom-Model"


def test_constructor_empty_base_url_and_ca_bundle_become_none() -> None:
    """Пустые строки не уходят в SDK как «пустой URL/файл»."""
    client = LangChainGigaChatClient(_settings(base_url="", ca_bundle=""))
    FakeGigaChat.responses = [_message("ok")]
    client.complete([{"role": "user", "content": "привет"}])

    kwargs = FakeGigaChat.instances[0].kwargs
    assert kwargs["base_url"] is None
    assert kwargs["ca_bundle_file"] is None


def test_constructor_rejects_other_provider() -> None:
    """Адаптер не подменяет другие провайдеры."""
    with pytest.raises(ValueError, match="gigachat"):
        LangChainGigaChatClient(_settings(provider="deepseek"))


def test_make_agent_client_routes_gigachat_and_others() -> None:
    """provider=gigachat → адаптер; остальные — прежний ``make_client``."""
    gigachat = make_agent_client(_settings())
    assert isinstance(gigachat, LangChainGigaChatClient)
    assert gigachat.supports_tools is True

    other = make_agent_client(
        LLMSettings(provider="deepseek", model="deepseek-chat", api_key="k")
    )
    assert isinstance(other, LLMClient)
    assert not isinstance(other, LangChainGigaChatClient)


# --------------------------------------------------------------------------- #
# Инструменты и ответ модели
# --------------------------------------------------------------------------- #
def test_bind_tools_gets_agent_tools_and_auto_choice() -> None:
    """bind_tools вызван со схемами build_agent_tools() и tool_choice='auto'."""
    client = LangChainGigaChatClient(_settings())
    FakeGigaChat.responses = [
        _message(
            "",
            tool_calls=[
                {"name": "total", "args": {"start_date": "2024-01-01"}, "id": "1", "type": "tool_call"}
            ],
        )
    ]
    client.complete([{"role": "user", "content": "выручка за 2024"}], tools=build_agent_tools())

    model = FakeGigaChat.instances[0]
    assert len(model.bound) == 1
    assert model.bound[0]["tool_choice"] == "auto"
    sent = model.bound[0]["tools"]
    assert [tool["function"]["name"] for tool in sent] == [
        tool["function"]["name"] for tool in build_agent_tools()
    ]
    # Совместимость с GigaChat: явный type у свойства, без anyOf (иначе 422).
    for tool in sent:
        properties = tool["function"]["parameters"].get("properties") or {}
        for name, spec in properties.items():
            assert "anyOf" not in spec, name
            assert "type" in spec, name


def test_normalize_tool_schemas_collapses_nullable_any_of() -> None:
    """anyOf [string, null] → string; исходные схемы не мутируются."""
    source = build_agent_tools()
    original = json.dumps(source, ensure_ascii=False, sort_keys=True)

    normalized = normalize_tool_schemas(source)

    total = next(t for t in normalized if t["function"]["name"] == "total")
    start = total["function"]["parameters"]["properties"]["start_date"]
    assert start["type"] == "string"
    assert start["format"] == "date"
    assert json.dumps(source, ensure_ascii=False, sort_keys=True) == original


def test_normalize_tool_schemas_removes_null_default_on_non_nullable() -> None:
    """``default: null`` на не-nullable типе не остаётся.

    GigaChat трактует такой невалидный дефолт как повод подставить значение
    (minimum 0, top_n=1…) и досыпает опциональные параметры. Поэтому после
    нормализации у опциональных чисел не должно быть ни ``default: null``,
    ни попадания в ``required``.
    """
    normalized = normalize_tool_schemas(build_agent_tools())

    for tool in normalized:
        for name, spec in tool["function"]["parameters"].get("properties", {}).items():
            if spec.get("type") != "null":
                assert "default" not in spec or spec["default"] is not None, name

    params = next(
        t for t in normalized if t["function"]["name"] == "counterparty_dynamics"
    )["function"]["parameters"]
    for key in ("top_n", "min_abs_delta", "min_base_revenue"):
        assert "default" not in params["properties"][key]
        assert key not in params["required"]


def test_normalize_tool_schemas_rejects_real_union() -> None:
    """Union из двух содержательных типов GigaChat не поддерживает — LLMError."""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "x",
                "parameters": {
                    "type": "object",
                    "properties": {"v": {"anyOf": [{"type": "string"}, {"type": "integer"}]}},
                },
            },
        }
    ]
    with pytest.raises(LLMError, match="Union"):
        normalize_tool_schemas(tools)


def test_tool_calls_become_completion_result() -> None:
    """AIMessage.tool_calls → ToolCall(name, arguments)."""
    client = LangChainGigaChatClient(_settings())
    FakeGigaChat.responses = [
        _message(
            "",
            tool_calls=[
                {
                    "name": "total",
                    "args": {"start_date": "2024-01-01", "end_date": "2024-12-31"},
                    "id": "call-1",
                    "type": "tool_call",
                }
            ],
        )
    ]
    result = client.complete(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        tools=build_agent_tools(),
    )

    assert result == CompletionResult(
        content="",
        tool_calls=(
            ToolCall("total", {"start_date": "2024-01-01", "end_date": "2024-12-31"}),
        ),
    )


def test_content_without_tools_becomes_completion_result() -> None:
    """Обычный текстовый ответ (уточнение/классификатор) → content."""
    client = LangChainGigaChatClient(_settings())
    FakeGigaChat.responses = [_message("За какой период сравнить?")]
    result = client.complete([{"role": "user", "content": "сравни"}])

    assert result == CompletionResult(content="За какой период сравнить?")
    assert FakeGigaChat.instances[0].bound == []


def test_chat_returns_content() -> None:
    """chat() — текстовый вызов без инструментов."""
    client = LangChainGigaChatClient(_settings())
    FakeGigaChat.responses = [_message("ok")]
    assert client.chat([{"role": "user", "content": "привет"}]) == "ok"
    assert FakeGigaChat.instances[0].bound == []


def test_list_content_is_normalized_to_text() -> None:
    """content не-str (список блоков) нормализуется в строку."""
    client = LangChainGigaChatClient(_settings())
    FakeGigaChat.responses = [
        _message([{"type": "text", "text": "часть 1"}, {"type": "text", "text": " и 2"}])
    ]
    assert client.complete([{"role": "user", "content": "u"}]).content == "часть 1 и 2"


def test_invalid_tool_calls_raise_llm_error() -> None:
    """invalid_tool_calls → LLMError с понятным текстом (без догадок)."""
    client = LangChainGigaChatClient(_settings())
    FakeGigaChat.responses = [
        _message(
            "",
            invalid=[
                {"name": "total", "args": "{не json", "id": "1", "error": "bad args"}
            ],
        )
    ]
    with pytest.raises(LLMError, match="некорректный tool call"):
        client.complete([{"role": "user", "content": "u"}], tools=build_agent_tools())


def test_non_object_arguments_raise_llm_error() -> None:
    """Аргументы-не-объект → LLMError."""
    client = LangChainGigaChatClient(_settings())
    FakeGigaChat.responses = [
        _message("", tool_calls=[{"name": "total", "args": ["2024"], "id": "1"}])
    ]
    with pytest.raises(LLMError, match="объектом"):
        client.complete([{"role": "user", "content": "u"}], tools=build_agent_tools())


def test_empty_answer_is_empty_completion() -> None:
    """Пустой ответ не превращается в догадку: пустой CompletionResult."""
    client = LangChainGigaChatClient(_settings())
    FakeGigaChat.responses = [_message("")]
    assert client.complete([{"role": "user", "content": "u"}]) == CompletionResult()


def test_unknown_role_raises_llm_error() -> None:
    """Роль вне контракта графа — ошибка, а не молчаливая подмена."""
    client = LangChainGigaChatClient(_settings())
    with pytest.raises(LLMError, match="роль"):
        client.complete([{"role": "tool", "content": "x"}])


def test_provider_exception_becomes_llm_error() -> None:
    """Сетевые/TLS-ошибки SDK заворачиваются в LLMError (узлы их ловят)."""
    client = LangChainGigaChatClient(_settings())
    FakeGigaChat.responses = [RuntimeError("CERTIFICATE_VERIFY_FAILED")]
    with pytest.raises(LLMError, match="недоступен"):
        client.complete([{"role": "user", "content": "u"}])


def test_make_client_still_returns_legacy_for_health_check() -> None:
    """Старый самописный клиент не тронут: health-check/список моделей на нём."""
    legacy = llm_agent.make_client(_settings())
    assert isinstance(legacy, llm_agent.GigaChatClient)
    assert legacy.supports_tools is True
