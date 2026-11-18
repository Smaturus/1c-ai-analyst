"""Граф с GigaChat из ``langchain-gigachat`` (fake SDK, без сети).

Проверяется Definition of Done этапа 2.5: при ``provider="gigachat"``
``run_agent_graph`` создаёт :class:`LangChainGigaChatClient`; один tool call
остаётся обязательным (0/2 → ``handle_error``), невалидный ответ чинится
одним ``fix_dsl``, ``clarify``/``refuse`` не доходят до валидации DSL.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from onec_analyst.agent import llm as agent_llm
from onec_analyst.agent.graph import run_agent_graph
from onec_analyst.agent.llm import LangChainGigaChatClient
from onec_analyst.agent.state import initial_state
from onec_analyst.llm_agent import LLMSettings

TODAY = date(2026, 11, 18)


class FakeGigaChat:
    """Минимальный ``GigaChat``: ``bind_tools`` + ``invoke`` с очередью ответов."""

    instances: list["FakeGigaChat"] = []
    responses: list[SimpleNamespace] = []
    init_error: BaseException | None = None
    invoke_error: BaseException | None = None

    def __init__(self, **kwargs: Any) -> None:
        if FakeGigaChat.init_error is not None:
            raise FakeGigaChat.init_error
        self.kwargs = kwargs
        self.bound: list[dict[str, Any]] = []
        self.invocations = 0
        FakeGigaChat.instances.append(self)

    def bind_tools(self, tools, *, tool_choice=None, **kwargs: Any):
        self.bound.append({"tools": list(tools), "tool_choice": tool_choice})
        return self

    def invoke(self, messages):
        self.invocations += 1
        if FakeGigaChat.invoke_error is not None:
            raise FakeGigaChat.invoke_error
        if not FakeGigaChat.responses:
            raise AssertionError("fake GigaChat: не задан ответ")
        return FakeGigaChat.responses.pop(0)


def _message(
    content: Any = "",
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    invalid: list[dict[str, Any]] | None = None,
) -> SimpleNamespace:
    """Ответ с атрибутами ``AIMessage``."""
    return SimpleNamespace(
        content=content,
        tool_calls=list(tool_calls or []),
        invalid_tool_calls=list(invalid or []),
    )


def _call(name: str, args: dict[str, Any], call_id: str = "call-1") -> dict[str, Any]:
    """Один tool call в формате ``AIMessage``."""
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _settings(model: str = "GigaChat-2-Pro") -> LLMSettings:
    """Настройки GigaChat с фиктивным ключом: сеть не нужна."""
    return LLMSettings(
        provider="gigachat",
        model=model,
        api_key="fake-credentials",
        base_url="https://api.giga.chat/v1",
    )


@pytest.fixture(autouse=True)
def _fake_sdk(monkeypatch: pytest.MonkeyPatch):
    """Подменить ленивый импорт SDK и очистить очередь ответов."""
    FakeGigaChat.instances = []
    FakeGigaChat.responses = []
    FakeGigaChat.init_error = None
    FakeGigaChat.invoke_error = None
    monkeypatch.setattr(agent_llm, "_gigachat_class", lambda: FakeGigaChat)


def _run(
    question: str,
    responses: list[SimpleNamespace],
    *,
    model: str = "GigaChat-2-Pro",
):
    """Запустить граф без явного client: клиент строит ``run_agent_graph``."""
    FakeGigaChat.responses = list(responses)
    state = initial_state(question, today=TODAY)
    return run_agent_graph(state, settings=_settings(model))


# --------------------------------------------------------------------------- #
# Успешный и аварийные ответы
# --------------------------------------------------------------------------- #
def test_one_tool_call_goes_to_ok() -> None:
    """Один tool call → validate_dsl → ok; граф использует LangChainGigaChatClient."""
    final = _run(
        "Сколько выручки за 2024 год?",
        [
            _message(
                "",
                tool_calls=[
                    _call("total", {"start_date": "2024-01-01", "end_date": "2024-12-31"})
                ],
            )
        ],
    )

    assert isinstance(final["client"], LangChainGigaChatClient)
    assert final["decision"].status == "ok"
    assert final["decision"].intent == "total"
    assert final["decision"].dsl["params"]["start_date"] == "2024-01-01"
    assert final["trace"] == ["agent_decision", "validate_dsl"]
    # GigaChat получил схемы инструментов и auto-выбор.
    assert FakeGigaChat.instances[0].bound[0]["tool_choice"] == "auto"


def test_zero_tool_calls_go_to_handle_error() -> None:
    """Свободный текст/пустой ответ не угадывается: handle_error."""
    final = _run("Сколько выручки за 2024 год?", [_message("")])

    assert "handle_error" in final["trace"]
    assert final["decision"].status in {"error", "fallback"}
    assert final["decision"].source == "rules"


def test_two_tool_calls_go_to_handle_error() -> None:
    """Два вызова в одном ответе нарушают контракт: handle_error."""
    final = _run(
        "Сколько выручки за 2024 год?",
        [
            _message(
                "",
                tool_calls=[
                    _call("total", {"start_date": "2024-01-01", "end_date": "2024-12-31"}, "1"),
                    _call("timeseries", {"freq": "M"}, "2"),
                ],
            )
        ],
    )

    assert "handle_error" in final["trace"]
    assert final["decision"].status in {"error", "fallback"}


def test_invalid_tool_call_is_fixed_once_to_ok() -> None:
    """Невалидные аргументы → fix_dsl (текстовый JSON) → ok, ровно два вызова."""
    final = _run(
        "Сколько выручки за 2024 год?",
        [
            _message(
                "",
                tool_calls=[_call("total", {"start_date": "вчера", "end_date": "2024-12-31"})],
            ),
            _message('{"start_date": "2024-01-01", "end_date": "2024-12-31"}'),
        ],
    )

    assert final["decision"].status == "ok"
    assert final["trace"] == ["agent_decision", "validate_dsl", "fix_dsl", "validate_dsl"]
    assert final["retry_count"] == 1
    assert FakeGigaChat.instances[0].invocations == 2
    # fix_dsl идёт текстом, без повторного bind_tools.
    assert len(FakeGigaChat.instances[0].bound) == 1


def test_invalid_tool_call_after_fix_goes_to_handle_error() -> None:
    """Вторая ошибка после починки → handle_error (лишних повторов нет)."""
    final = _run(
        "Сколько выручки за 2024 год?",
        [
            _message(
                "",
                tool_calls=[_call("total", {"start_date": "вчера", "end_date": "2024-12-31"})],
            ),
            _message('{"start_date": "позавчера"}'),
        ],
    )

    assert "handle_error" in final["trace"]
    assert final["decision"].status in {"error", "fallback"}


def test_invalid_tool_calls_from_sdk_go_to_handle_error() -> None:
    """``invalid_tool_calls`` адаптер превращает в LLMError → handle_error."""
    final = _run(
        "Сколько выручки за 2024 год?",
        [_message("", invalid=[{"name": "total", "args": "{bad", "id": "1", "error": "bad"}])],
    )

    assert "handle_error" in final["trace"]
    assert "некорректный tool call" in str(final.get("error") or final.get("validation_error"))


# --------------------------------------------------------------------------- #
# Мета-инструменты
# --------------------------------------------------------------------------- #
def test_clarify_stops_before_validation() -> None:
    """clarify → статус clarify, без validate_dsl."""
    final = _run(
        "Сравни топы",
        [
            _message(
                "",
                tool_calls=[
                    _call("clarify", {"missing": ["period2"], "question": "За какой период?"})
                ],
            )
        ],
    )

    assert final["decision"].status == "clarify"
    assert final["decision"].clarification is not None
    assert final["trace"] == ["agent_decision", "clarify"]


def test_refuse_stops_before_validation() -> None:
    """refuse → статус refuse, без validate_dsl."""
    final = _run(
        "Посчитай средний чек",
        [_message("", tool_calls=[_call("refuse", {"reason": "Средний чек не поддерживается."})])],
    )

    assert final["decision"].status == "refuse"
    assert final["trace"] == ["agent_decision", "refuse"]


# --------------------------------------------------------------------------- #
# Ошибка модели: handle_error → fallback/error без подмены модели
# --------------------------------------------------------------------------- #
def test_constructor_error_goes_to_fallback_with_same_model() -> None:
    """Ошибка ``GigaChat(model=...)`` → handle_error; модель в клиенте не меняется."""
    FakeGigaChat.init_error = RuntimeError("model not found")
    final = _run("Сколько выручки за 2024 год?", [], model="Custom-Model")

    assert "handle_error" in final["trace"]
    assert final["decision"].status in {"fallback", "error"}
    assert isinstance(final["client"], LangChainGigaChatClient)
    assert final["client"]._settings.model == "Custom-Model"
    assert FakeGigaChat.instances == []  # конструктор не состоялся


def test_model_error_message_has_model_and_no_credentials() -> None:
    """В тексте ошибки — имя модели, типа исключения; credentials отсутствуют."""
    FakeGigaChat.init_error = RuntimeError("bad credentials fake-credentials")
    final = _run("Сколько выручки за 2024 год?", [], model="Custom-Model")

    text = str(final.get("error") or "") + str(final.get("validation_error") or "")
    assert "Custom-Model" in text
    assert "RuntimeError" in text
    assert "fake-credentials" not in text
    assert final["decision"].status in {"fallback", "error"}


def test_invoke_model_error_goes_to_fallback_with_same_model() -> None:
    """Ошибка вызова (404 model not found) → handle_error, модель не подменяется."""
    FakeGigaChat.invoke_error = Exception("404 model not found")
    final = _run("Сколько выручки за 2024 год?", [], model="GigaChat-Max")

    assert "handle_error" in final["trace"]
    assert final["decision"].status in {"fallback", "error"}
    assert FakeGigaChat.instances[0].kwargs["model"] == "GigaChat-Max"
    assert "GigaChat-Max" in str(final.get("error") or final.get("validation_error") or "")


def test_other_providers_keep_old_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """provider=deepseek по-прежнему идёт через ``llm_agent.make_client``."""
    sentinel = SimpleNamespace(supports_tools=False)

    def fake_make_client(settings):
        return sentinel

    monkeypatch.setattr(agent_llm.llm_agent, "make_client", fake_make_client)
    final = _run("Сколько выручки за 2024 год?", [_message("текст")])
    # _run передаёт gigachat-настройки, поэтому подмена не должна сработать.
    assert isinstance(final["client"], LangChainGigaChatClient)

    from onec_analyst.agent.llm import make_agent_client

    assert make_agent_client(LLMSettings(provider="deepseek", api_key="k")) is sentinel
