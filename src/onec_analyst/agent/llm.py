"""Адаптер GigaChat для LangGraph-агента: официальная ``langchain-gigachat``.

Зачем адаптер
-------------
Узлы графа работают с маленьким интерфейсом
(:attr:`~onec_analyst.llm_agent.LLMClient.supports_tools`,
:meth:`complete`, :meth:`chat`). Официальная библиотека даёт
``ChatGigaChat`` с ``bind_tools``/``invoke`` и ``AIMessage``, поэтому между
ними нужен тонкий слой перевода:

* ``list[dict]`` → ``SystemMessage``/``HumanMessage``/``AIMessage``;
* JSON-схемы инструментов → совместимый с GigaChat вид (``anyOf`` + ``null``
  провайдер не принимает: 422 ``Type properties.*.type is wrong``);
* ``AIMessage.tool_calls`` → :class:`~onec_analyst.llm_agent.ToolCall`;
* ``invalid_tool_calls``/не-объект аргументов → ``LLMError``.

Граф, промпты, статусы и Pydantic-контракт не меняются (ADR 0014). Старый
:class:`~onec_analyst.llm_agent.GigaChatClient` остаётся legacy для
health-check и списка моделей: адаптер его не заменяет в
``llm_agent.check_provider``/``chat_models``.

Модуль не импортирует Streamlit и не создаёт сетевых соединений при импорте:
``langchain_gigachat`` подгружается лениво, только при первом вызове.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from onec_analyst import llm_agent
from onec_analyst.llm_agent import CompletionResult, LLMError, LLMSettings, ToolCall

#: Роли сообщений, которые понимает граф (история UI — только user/assistant).
_ROLE_MAP = {
    "system": "system",
    "user": "human",
    "human": "human",
    "assistant": "ai",
    "ai": "ai",
}


def _gigachat_class() -> Any:
    """Ленивый импорт ``GigaChat``: без вызова библиотека не загружается."""
    from langchain_gigachat import GigaChat

    return GigaChat


def _message_classes() -> tuple[Any, Any, Any]:
    """Ленивый импорт классов сообщений ``langchain_core``."""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    return SystemMessage, HumanMessage, AIMessage


def normalize_tool_schemas(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Привести JSON-схемы к виду, который принимает GigaChat.

    Провайдер требует явный ``type`` у каждого свойства и не поддерживает
    ``anyOf`` (Pydantic 2 генерирует ``anyOf: [{type: X}, {type: null}]`` для
    ``Optional``). Функция сводит ``anyOf`` с единственным не-``null``
    вариантом к этому варианту; ``anyOf`` из двух содержательных типов
    по-прежнему невозможен и даёт ``LLMError``. Это транспортная
    совместимость, а не изменение контракта: схемы по-прежнему строятся из
    Pydantic-моделей в :func:`onec_analyst.agent.tools.build_agent_tools`.
    """
    return [_normalize_node(copy.deepcopy(tool)) for tool in tools]


def _normalize_node(node: Any) -> Any:
    """Рекурсивно свести ``anyOf``/``null`` и проставить ``type`` объектам."""
    if isinstance(node, list):
        return [_normalize_node(item) for item in node]
    if not isinstance(node, dict):
        return node
    normalized = {key: _normalize_node(value) for key, value in node.items()}
    variants = normalized.pop("anyOf", None)
    if variants is not None:
        if not isinstance(variants, list):
            raise LLMError("Некорректная JSON-схема инструмента: anyOf не список")
        real = [item for item in variants if item.get("type") != "null"]
        if len(real) != 1:
            raise LLMError(
                "GigaChat не поддерживает Union в аргументах инструмента: "
                f"anyOf={variants!r}"
            )
        normalized = {**real[0], **normalized}
        # ``default: null`` на не-nullable типе невалиден для JSON Schema:
        # GigaChat «чинит» его, подставляя минимум (0) или своё значение, и
        # досыпает опциональные параметры (min_abs_delta=0, top_n=1…).
        # Опциональность выражается отсутствием в ``required``, а не null-дефолтом.
        if normalized.get("default") is None:
            normalized.pop("default", None)
    if "type" not in normalized and "properties" in normalized:
        normalized["type"] = "object"
    return normalized


def _content_to_text(content: Any) -> str:
    """Нормализовать ``AIMessage.content`` в строку (может быть списком блоков)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return str(content)


def _to_langchain_messages(messages: list[dict[str, str]]) -> list[Any]:
    """Конвертировать сообщения графа в сообщения ``langchain_core``."""
    SystemMessage, HumanMessage, AIMessage = _message_classes()
    classes = {"system": SystemMessage, "human": HumanMessage, "ai": AIMessage}
    converted: list[Any] = []
    for message in messages or []:
        role = str(message.get("role") or "").strip().lower()
        if role not in _ROLE_MAP:
            raise LLMError(f"Неизвестная роль сообщения для GigaChat: {role!r}")
        content = message.get("content")
        text = content if isinstance(content, str) else _content_to_text(content)
        converted.append(classes[_ROLE_MAP[role]](content=text))
    return converted


class LangChainGigaChatClient:
    """GigaChat через ``langchain-gigachat`` с интерфейсом клиента графа.

    Parameters
    ----------
    settings:
        Настройки провайдера ``gigachat`` (:class:`LLMSettings`).
    model:
        Готовый экземпляр ``GigaChat`` (подмена в тестах); по умолчанию
        создаётся из ``settings`` при первом вызове.
    """

    supports_tools: bool = True

    def __init__(self, settings: LLMSettings, *, model: Any = None) -> None:
        if settings.provider != "gigachat":
            raise ValueError(
                "LangChainGigaChatClient ожидает provider='gigachat', "
                f"получено {settings.provider!r}"
            )
        self._settings = settings
        self._model = model

    def _constructor_kwargs(self) -> dict[str, Any]:
        """Параметры ``GigaChat(...)`` из :class:`LLMSettings` (без секретов в логах)."""
        settings = self._settings
        return {
            "credentials": settings.api_key,
            "scope": settings.scope,
            "model": settings.model,
            "base_url": settings.base_url or None,
            "verify_ssl_certs": settings.verify_tls,
            "ca_bundle_file": settings.ca_bundle or None,
            "temperature": settings.temperature,
            "timeout": settings.timeout,
        }

    def _get_model(self) -> Any:
        """Создать (один раз) ``GigaChat`` из настроек."""
        if self._model is None:
            self._model = _gigachat_class()(**self._constructor_kwargs())
        return self._model

    def complete(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
    ) -> CompletionResult:
        """Вызвать GigaChat и нормализовать ответ в :class:`CompletionResult`.

        В ``try`` входит **весь** путь: создание клиента, ``bind_tools`` и
        ``invoke``. Ошибка конструктора (неверная модель/credentials) иначе
        уходила бы из ``complete`` сырым исключением SDK мимо ``LLMError`` и
        мимо ``handle_error`` графа. Имя модели в тексте — из
        ``settings.model`` и только из него.
        """
        model = self._settings.model
        try:
            runnable: Any = self._get_model()
            if tools:
                runnable = runnable.bind_tools(
                    normalize_tool_schemas(tools), tool_choice="auto"
                )
            response = runnable.invoke(_to_langchain_messages(messages))
        except LLMError:
            raise
        except Exception as exc:  # конструктор/сеть/TLS/HTTP — в единый LLMError
            raise LLMError(
                f"GigaChat ({model}) недоступен: "
                f"{type(exc).__name__}: {self._short_error(exc)}"
            ) from exc
        return _completion_from_message(response)

    def _short_error(self, exc: BaseException) -> str:
        """Краткий текст ошибки без секретов: credentials вырезаются."""
        text = str(exc)[:500]
        secret = self._settings.api_key
        if secret:
            text = text.replace(secret, "***")
        return text

    def chat(self, messages: list[dict[str, str]]) -> str:
        """Текстовый вызов без инструментов (совместимость с классификатором)."""
        return self.complete(messages, tools=None).content


def _completion_from_message(response: Any) -> CompletionResult:
    """``AIMessage`` → :class:`CompletionResult` с проверкой контракта вызова."""
    content = _content_to_text(getattr(response, "content", ""))
    invalid = list(getattr(response, "invalid_tool_calls", None) or ())
    if invalid:
        details = "; ".join(
            f"{item.get('name') or '?'}: {item.get('error') or item.get('args')!r}"
            for item in invalid
            if isinstance(item, dict)
        )
        raise LLMError(f"GigaChat вернул некорректный tool call: {details or invalid!r}")
    calls: list[ToolCall] = []
    for item in getattr(response, "tool_calls", None) or ():
        name = str(item.get("name") or "")
        arguments = item.get("args")
        if not name:
            raise LLMError("GigaChat вернул tool call без имени инструмента")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise LLMError(
                    f"Аргументы инструмента {name!r} не JSON: {arguments[:200]!r}"
                ) from exc
        if not isinstance(arguments, dict):
            raise LLMError(
                f"Аргументы инструмента {name!r} должны быть объектом, "
                f"получено {type(arguments)!r}"
            )
        calls.append(ToolCall(name=name, arguments=arguments))
    return CompletionResult(content=content, tool_calls=tuple(calls))


def make_agent_client(settings: LLMSettings) -> Any:
    """Клиент графа: GigaChat — через ``langchain-gigachat``, остальные — как раньше."""
    if settings.provider == "gigachat":
        return LangChainGigaChatClient(settings)
    return llm_agent.make_client(settings)


__all__ = [
    "LangChainGigaChatClient",
    "make_agent_client",
    "normalize_tool_schemas",
]
