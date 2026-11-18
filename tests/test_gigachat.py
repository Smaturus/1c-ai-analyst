"""Unit-тесты GigaChat-провайдера и tool calling (без сети и секретов).

Проверяют:

* настройки ``gigachat`` (scope/auth_url/CA-bundle/verify_tls) из окружения;
* двухшаговую OAuth-авторизацию: ``Authorization: Basic <key>`` + ``RqUID`` +
  ``scope``, кэш токена и повторный запрос при ``401``;
* tool calling: модель возвращает вызов функции-интента, аргументы валидируются
  тем же Pydantic-контрактом; свободный текст трактуется как уточнение;
* graceful degradation: при ошибке сети/HTTP планировщик возвращает ``ok=False``,
  а не падает.

Все сетевые вызовы подменяются через ``monkeypatch`` на ``urllib.request.urlopen``,
поэтому тесты не требуют ни ключей, ни сертификатов НУЦ, ни доступа к Сберу.
"""

from __future__ import annotations

import io
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

import pytest

from onec_analyst.llm_agent import (
    INTENT_NAMES,
    CompletionResult,
    GigaChatClient,
    LLMClient,
    LLMError,
    LLMPlanner,
    LLMSettings,
    ProviderCheck,
    ToolCall,
    available_providers,
    build_tools,
    check_provider,
    check_providers,
    make_client,
    make_planner,
    provider_catalog,
    settings_from_env,
    with_dotenv,
)


class _Response:
    """Поддельный ответ urlopen: читается и работает как контекстный менеджер."""

    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: Any) -> bool:
        return False


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    chat_fn: Callable[[int, urllib.request.Request], Any],
    oauth_fn: Callable[[int, urllib.request.Request], str] | None = None,
) -> dict[str, list[Any]]:
    """Подменить ``urlopen`` и записать OAuth- и chat-запросы.

    ``chat_fn(index, request)`` возвращает тело JSON-ответа либо исключение
    (его ``_patch`` пробрасывает). ``oauth_fn(index, request)`` возвращает тело
    OAuth-ответа; по умолчанию — токен ``tok`` с ``expires_at`` в будущем.
    """
    calls: dict[str, list[Any]] = {"oauth": [], "chat": []}

    def urlopen(request: urllib.request.Request, timeout: Any = None, context: Any = None) -> Any:
        if "oauth" in request.full_url:
            calls["oauth"].append(request)
            if oauth_fn is None:
                body = json.dumps(
                    {"access_token": "tok", "expires_at": int(time.time()) + 1800}
                )
            else:
                body = oauth_fn(len(calls["oauth"]) - 1, request)
            return _Response(body)
        calls["chat"].append(request)
        result = chat_fn(len(calls["chat"]) - 1, request)
        if isinstance(result, Exception):
            raise result
        return _Response(result)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return calls


def _gigachat_settings(**overrides: Any) -> LLMSettings:
    """Настройки GigaChat для тестов (URL фиктивные, ключ не настоящий)."""
    values = dict(
        provider="gigachat",
        api_key="a2V5OnNlY3JldA==",  # base64("key:secret")
        base_url="https://api.test/api/v1",
        auth_url="https://auth.test/api/v2/oauth",
        verify_tls=False,
    )
    values.update(overrides)
    return LLMSettings(**values)


# --------------------------------------------------------------------------- #
# Настройки из окружения
# --------------------------------------------------------------------------- #
def test_settings_from_env_gigachat_defaults() -> None:
    """Провайдер gigachat: дефолтная модель, URL, scope и включённый TLS."""
    settings = settings_from_env({"LLM_PROVIDER": "gigachat", "LLM_API_KEY": "abc"})

    assert settings.provider == "gigachat"
    assert settings.model == "GigaChat-2-Pro"
    assert settings.base_url == "https://api.giga.chat/v1"
    assert settings.auth_url == "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    assert settings.scope == "GIGACHAT_API_PERS"
    assert settings.verify_tls is True
    assert settings.ca_bundle == ""
    assert settings.is_configured is True


def test_settings_from_env_gigachat_overrides() -> None:
    """scope/auth_url/CA-bundle/verify_tls/model читаются из окружения."""
    settings = settings_from_env(
        {
            "LLM_PROVIDER": "gigachat",
            "LLM_API_KEY": "abc",
            "GIGACHAT_SCOPE": "GIGACHAT_API_CORP",
            "GIGACHAT_AUTH_URL": "https://auth.example/api/v2/oauth",
            "LLM_TLS_CA_BUNDLE": "/etc/nuc.crt",
            "LLM_TLS_VERIFY": "0",
            "LLM_MODEL": "GigaChat-2-Max",
        }
    )

    assert settings.scope == "GIGACHAT_API_CORP"
    assert settings.auth_url == "https://auth.example/api/v2/oauth"
    assert settings.ca_bundle == "/etc/nuc.crt"
    assert settings.verify_tls is False
    assert settings.model == "GigaChat-2-Max"


def test_settings_from_env_still_rejects_unknown_provider() -> None:
    """Неизвестный провайдер по-прежнему отвергается."""
    with pytest.raises(ValueError):
        settings_from_env({"LLM_PROVIDER": "unknown"})


# --------------------------------------------------------------------------- #
# Фабрики клиентов и инструменты
# --------------------------------------------------------------------------- #
def test_make_client_returns_gigachat_for_gigachat() -> None:
    assert isinstance(make_client(LLMSettings(provider="gigachat", api_key="k")), GigaChatClient)


def test_make_client_returns_openai_compat_otherwise() -> None:
    client = make_client(LLMSettings(provider="deepseek", api_key="k"))
    assert isinstance(client, LLMClient)
    assert not isinstance(client, GigaChatClient)


def test_make_planner_uses_tool_calling_for_gigachat() -> None:
    planner = make_planner(LLMSettings(provider="gigachat", api_key="k"))

    assert isinstance(planner, LLMPlanner)
    assert planner.supports_tools is True


def test_gigachat_client_rejects_wrong_provider() -> None:
    with pytest.raises(ValueError):
        GigaChatClient(LLMSettings(provider="deepseek", api_key="k"))


def test_build_tools_covers_all_intents_with_schema() -> None:
    tools = build_tools()

    assert [tool["function"]["name"] for tool in tools] == list(INTENT_NAMES)
    for tool in tools:
        assert tool["type"] == "function"
        assert tool["function"]["parameters"]["type"] == "object"


# --------------------------------------------------------------------------- #
# OAuth: заголовки, scope, кэш токена, повтор при 401
# --------------------------------------------------------------------------- #
def test_oauth_sends_basic_rquid_scope_and_caches_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client = GigaChatClient(_gigachat_settings())

    def chat_fn(_index: int, _request: Any) -> str:
        return '{"choices":[{"message":{"content":"ok"}}]}'

    calls = _patch(monkeypatch, chat_fn)
    client.chat([{"role": "user", "content": "hi"}])
    client.chat([{"role": "user", "content": "hi"}])

    assert len(calls["oauth"]) == 1, "токен кэшируется — OAuth вызывается один раз"
    oauth = calls["oauth"][0]
    assert oauth.headers["Authorization"] == "Basic a2V5OnNlY3JldA=="
    # HTTP-заголовки регистронезависимы: urllib нормализует RqUID -> Rquid.
    header_names = {name.lower() for name in oauth.headers}
    assert "rquid" in header_names
    assert oauth.data == b"scope=GIGACHAT_API_PERS"
    # chat-запросы идут с Bearer-токеном
    assert all(c.headers["Authorization"] == "Bearer tok" for c in calls["chat"])


def test_retries_chat_with_new_token_on_401(monkeypatch: pytest.MonkeyPatch) -> None:
    client = GigaChatClient(_gigachat_settings())

    def oauth_fn(index: int, _request: Any) -> str:
        return json.dumps({"access_token": f"tok{index}", "expires_at": int(time.time()) + 1800})

    def chat_fn(_index: int, request: Any) -> Any:
        if request.headers["Authorization"] == "Bearer tok0":
            return urllib.error.HTTPError("https://api.test", 401, "unauthorized", {}, io.BytesIO(b"{}"))
        return '{"choices":[{"message":{"content":"ok"}}]}'

    calls = _patch(monkeypatch, chat_fn, oauth_fn)
    result = client.chat([{"role": "user", "content": "hi"}])

    assert result == "ok"
    assert len(calls["oauth"]) == 2, "после 401 токен запрашивается заново"


def test_complete_parses_tool_calls_and_sends_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    client = GigaChatClient(_gigachat_settings())
    body = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "total",
                                    "arguments": '{"start_date": "2024-01-01"}',
                                }
                            }
                        ]
                    }
                }
            ]
        }
    )
    calls = _patch(monkeypatch, lambda _i, _r: body)

    result = client.complete([{"role": "user", "content": "?"}], tools=build_tools())

    assert result.tool_calls == (ToolCall(name="total", arguments={"start_date": "2024-01-01"}),)
    sent = json.loads(calls["chat"][0].data)
    assert sent["tools"] == build_tools()
    assert sent["tool_choice"] == "auto"


def test_llm_client_complete_parses_tool_calls_and_sends_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenAI-совместимый клиент отправляет tools и разбирает tool_calls."""
    client = LLMClient(LLMSettings(provider="deepseek", api_key="k", base_url="http://x/v1"))
    sent: list[dict[str, Any]] = []
    body = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "total",
                                "arguments": '{"start_date": "2024-01-01"}',
                            }
                        }
                    ],
                }
            }
        ]
    }

    def fake_post(payload: dict[str, Any]) -> dict[str, Any]:
        sent.append(payload)
        return body

    monkeypatch.setattr(client, "_post_json", fake_post)
    result = client.complete([{"role": "user", "content": "?"}], tools=build_tools())

    assert sent[0]["tools"] == build_tools()
    assert sent[0]["tool_choice"] == "auto"
    assert "response_format" not in sent[0]
    assert result.tool_calls == (ToolCall(name="total", arguments={"start_date": "2024-01-01"}),)


def test_llm_client_falls_back_to_json_when_tools_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Провайдер без tools откатывается к текстовому JSON-адаптеру."""
    client = LLMClient(LLMSettings(provider="deepseek", api_key="k", base_url="http://x/v1"))

    def fake_post(payload: dict[str, Any]) -> dict[str, Any]:
        if "tools" in payload:
            raise LLMError("HTTP 400: tools are not supported")
        return {"choices": [{"message": {"content": '{"intent":"total","params":{}}'}}]}

    monkeypatch.setattr(client, "_post_json", fake_post)
    result = client.complete([{"role": "user", "content": "?"}], tools=build_tools())

    assert result.content == '{"intent":"total","params":{}}'
    assert result.tool_calls == ()


# --------------------------------------------------------------------------- #
# Планировщик: tool call → DSL, свободный текст → уточнение, починка, деградация
# --------------------------------------------------------------------------- #
def _tool_call_body(name: str, arguments: dict[str, Any]) -> str:
    return json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {"function": {"name": name, "arguments": json.dumps(arguments)}}
                        ]
                    }
                }
            ]
        }
    )


def test_planner_tool_call_becomes_dispatchable_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _tool_call_body(
        "total", {"start_date": "2024-01-01", "end_date": "2024-12-31"}
    )
    _patch(monkeypatch, lambda _i, _r: body)

    decision = LLMPlanner(_gigachat_settings()).plan("Сколько продали за 2024?")

    assert decision.ok and decision.intent == "total", decision.error
    assert decision.params["start_date"].isoformat() == "2024-01-01"
    assert decision.params["end_date"].isoformat() == "2024-12-31"
    assert decision.source == "llm"


def test_planner_free_text_becomes_clarification(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps({"choices": [{"message": {"content": "За какой период?"}}]})
    _patch(monkeypatch, lambda _i, _r: body)

    decision = LLMPlanner(_gigachat_settings()).plan("Покажи динамику")

    assert decision.ok and decision.intent == "clarification", decision.error
    assert decision.clarification is not None
    assert decision.clarification.question == "За какой период?"


def test_planner_repairs_invalid_tool_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    bad = _tool_call_body("total", {"start_date": "не-дата"})
    good = _tool_call_body(
        "total", {"start_date": "2024-01-01", "end_date": "2024-12-31"}
    )
    _patch(monkeypatch, lambda i, _r: [bad, good][i])

    decision = LLMPlanner(_gigachat_settings()).plan("Сколько продали за 2024?")

    assert decision.ok and decision.intent == "total", decision.error


def test_planner_unknown_tool_name_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Несуществующий интент-инструмент не доходит до dispatch."""
    body = _tool_call_body("profit_forecast", {})
    _patch(monkeypatch, lambda _i, _r: body)

    decision = LLMPlanner(_gigachat_settings()).plan("Спрогнозируй прибыль")

    assert decision.ok is False
    assert decision.source == "llm"


def test_planner_network_error_degrades_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    def chat_fn(_index: int, _request: Any) -> Exception:
        return urllib.error.URLError("no network")

    _patch(monkeypatch, chat_fn)

    decision = LLMPlanner(_gigachat_settings()).plan("Сколько продали?")

    assert decision.ok is False
    assert decision.source == "llm"
    assert "недоступна" in (decision.error or "")


# --------------------------------------------------------------------------- #
# Выбор провайдера в UI: несколько провайдеров одновременно
# --------------------------------------------------------------------------- #
def test_available_providers_default_and_dedicated() -> None:
    """Провайдер по умолчанию + провайдер со своим ключом попадают в список."""
    env = {
        "LLM_PROVIDER": "deepseek",
        "LLM_API_KEY": "ds-key",
        "LLM_API_KEY_GIGACHAT": "gc-key",
    }

    providers = [settings.provider for settings in available_providers(env)]

    assert providers == ["deepseek", "gigachat"]


def test_available_providers_does_not_leak_generic_key() -> None:
    """Общий LLM_API_KEY не делает «настроенным» другого провайдера."""
    env = {"LLM_PROVIDER": "deepseek", "LLM_API_KEY": "ds-key"}

    providers = [settings.provider for settings in available_providers(env)]

    assert providers == ["deepseek"]


def test_settings_from_env_provider_override_uses_dedicated_key() -> None:
    """Явный провайдер берёт свой ключ и модель по умолчанию."""
    env = {
        "LLM_PROVIDER": "deepseek",
        "LLM_API_KEY": "ds-key",
        "LLM_API_KEY_GIGACHAT": "gc-key",
    }

    settings = settings_from_env(env, provider="gigachat")

    assert settings.provider == "gigachat"
    assert settings.api_key == "gc-key"
    assert settings.model == "GigaChat-2-Pro"
    assert settings.base_url == "https://api.giga.chat/v1"


def test_provider_catalog_marks_unconfigured_gigachat() -> None:
    """Каталог показывает GigaChat даже без ключа, помечая его «не настроен»."""
    env = {"LLM_PROVIDER": "deepseek", "LLM_API_KEY": "ds-key"}

    catalog = provider_catalog(env)
    by_provider = {option.settings.provider: option for option in catalog}

    assert by_provider["deepseek"].configured is True
    assert by_provider["gigachat"].configured is False
    assert by_provider["gigachat"].settings.model == "GigaChat-2-Pro"


def test_provider_catalog_marks_configured_gigachat() -> None:
    """С собственным ключом GigaChat в каталоге считается настроенным."""
    env = {
        "LLM_PROVIDER": "deepseek",
        "LLM_API_KEY": "ds-key",
        "LLM_API_KEY_GIGACHAT": "gc-key",
    }

    catalog = provider_catalog(env)
    by_provider = {option.settings.provider: option for option in catalog}

    assert by_provider["gigachat"].configured is True


# --------------------------------------------------------------------------- #
# Изоляция настроек по провайдеру: общие имена не перетекают на чужих
# --------------------------------------------------------------------------- #
def test_generic_model_does_not_leak_to_other_provider() -> None:
    """``LLM_MODEL`` default-провайдера не становится моделью другого провайдера.

    Регрессия системного дефекта: ``LLM_MODEL=deepseek-chat`` уходил в GigaChat
    без всяких подсказок. Ожидание: у чужого провайдера модели нет вовсе —
    ``settings_from_env`` подставит его собственный дефолт.
    """
    env = {
        "LLM_PROVIDER": "deepseek",
        "LLM_API_KEY": "ds-key",
        "LLM_MODEL": "deepseek-chat",
        "LLM_API_KEY_GIGACHAT": "gc-key",
    }

    settings = settings_from_env(env, provider="gigachat")

    assert settings.model == "GigaChat-2-Pro", "иначе deepseek-chat уйдёт в GigaChat"


def test_generic_base_url_does_not_leak_to_other_provider() -> None:
    """``LLM_BASE_URL`` default-провайдера не становится эндпоинтом другого."""
    env = {
        "LLM_PROVIDER": "deepseek",
        "LLM_API_KEY": "ds-key",
        "LLM_BASE_URL": "https://api.deepseek.com/v1",
        "LLM_API_KEY_GIGACHAT": "gc-key",
    }

    settings = settings_from_env(env, provider="gigachat")

    assert settings.base_url == "https://api.giga.chat/v1"


def test_empty_dedicated_model_overrides_generic() -> None:
    """Пустой ``LLM_MODEL_GIGACHAT=`` в ``.env`` — явное «у меня своя»: дефолт GigaChat.

    Именно так устроен поставляемый ``.env.example``: закомментированные
    примеры оставляют значение пустым, и пустота должна перекрывать общий
    ``LLM_MODEL``, а не наследовать чужую модель.
    """
    env = {
        "LLM_PROVIDER": "deepseek",
        "LLM_API_KEY": "ds-key",
        "LLM_MODEL": "deepseek-chat",
        "LLM_API_KEY_GIGACHAT": "gc-key",
        "LLM_MODEL_GIGACHAT": "",
    }

    settings = settings_from_env(env, provider="gigachat")

    assert settings.model == "GigaChat-2-Pro"


def test_dedicated_model_applies_to_its_provider() -> None:
    """Своя модель провайдера (``LLM_MODEL_<PROVIDER>``) имеет приоритет."""
    env = {
        "LLM_PROVIDER": "deepseek",
        "LLM_API_KEY": "ds-key",
        "LLM_MODEL": "deepseek-chat",
        "LLM_API_KEY_GIGACHAT": "gc-key",
        "LLM_MODEL_GIGACHAT": "GigaChat-2-Max",
    }

    settings = settings_from_env(env, provider="gigachat")

    assert settings.model == "GigaChat-2-Max"


def test_generic_model_still_applies_to_default_provider() -> None:
    """Для провайдера по умолчанию общее имя остаётся рабочим сокращением."""
    env = {"LLM_PROVIDER": "deepseek", "LLM_API_KEY": "ds-key", "LLM_MODEL": "deepseek-reasoner"}

    assert settings_from_env(env).model == "deepseek-reasoner"


def test_shared_settings_apply_to_all_providers() -> None:
    """Таймаут/температура — не провайдерные: действуют на обоих провайдеров."""
    env = {
        "LLM_PROVIDER": "deepseek",
        "LLM_API_KEY": "ds-key",
        "LLM_TIMEOUT": "15",
        "LLM_TEMPERATURE": "0.5",
    }

    for provider in ("deepseek", "gigachat"):
        settings = settings_from_env({**env, "LLM_API_KEY_GIGACHAT": "gc-key"}, provider=provider)
        assert settings.timeout == 15.0
        assert settings.temperature == 0.5


def test_with_dotenv_overrides_win_over_environment() -> None:
    """Явные переопределения важнее окружения процесса и файла ``.env``."""
    env = with_dotenv({"LLM_PROVIDER": "none", "LLM_API_KEY": ""})

    assert env["LLM_PROVIDER"] == "none"
    assert "LLM_API_KEY" not in env, "пустая строка удаляет значение"


# --------------------------------------------------------------------------- #
# Готовность GigaChat учитывает TLS-сертификат
# --------------------------------------------------------------------------- #
def test_gigachat_unconfigured_when_ca_bundle_missing(tmp_path: Path) -> None:
    """Указан несуществующий путь к сертификату → провайдер «не настроен».

    Сертификат — общая переменная ``LLM_TLS_CA_BUNDLE``, поэтому он влияет и на
    TLS-провайдеров по умолчанию: неверный путь делает «не настроенным» того,
    кто его использует, и не оставляет приложения с заведомо падающим вызовом.
    """
    env = {
        "LLM_PROVIDER": "deepseek",
        "LLM_API_KEY": "ds-key",
        "LLM_API_KEY_GIGACHAT": "gc-key",
        "LLM_TLS_CA_BUNDLE": str(tmp_path / "nuc-missing.crt"),
    }

    catalog = provider_catalog(env)
    by_provider = {option.settings.provider: option for option in catalog}

    assert by_provider["gigachat"].configured is False
    assert by_provider["deepseek"].configured is False
    assert available_providers(env) == []


def test_valid_ca_bundle_keeps_default_provider_configured(tmp_path: Path) -> None:
    """Существующий сертификат не мешает провайдеру по умолчанию."""
    certificate = tmp_path / "russian_trusted_root_ca_pem.crt"
    certificate.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    env = {
        "LLM_PROVIDER": "deepseek",
        "LLM_API_KEY": "ds-key",
        "LLM_API_KEY_GIGACHAT": "gc-key",
        "LLM_TLS_CA_BUNDLE": str(certificate),
    }

    providers = [settings.provider for settings in available_providers(env)]

    assert providers == ["deepseek", "gigachat"]


def test_gigachat_configured_when_tls_verification_disabled() -> None:
    """Dev-флаг ``LLM_TLS_VERIFY=0`` снимает требование к сертификату."""
    env = {
        "LLM_PROVIDER": "deepseek",
        "LLM_API_KEY": "ds-key",
        "LLM_API_KEY_GIGACHAT": "gc-key",
        "LLM_TLS_CA_BUNDLE": "C:/definitely/missing.crt",
        "LLM_TLS_VERIFY": "0",
    }

    catalog = provider_catalog(env)
    by_provider = {option.settings.provider: option for option in catalog}

    assert by_provider["gigachat"].configured is True


# --------------------------------------------------------------------------- #
# Проверка связки: OAuth + GET /models (без генерации токенов)
# --------------------------------------------------------------------------- #
def _patch_models(
    monkeypatch: pytest.MonkeyPatch,
    body: Any,
) -> list[urllib.request.Request]:
    """Подменить ``urlopen``: OAuth отдаёт токен, ``/models`` — заданное тело."""
    requests: list[urllib.request.Request] = []

    def urlopen(request: urllib.request.Request, timeout: Any = None, context: Any = None) -> Any:
        requests.append(request)
        if "oauth" in request.full_url:
            return _Response(json.dumps({"access_token": "tok", "expires_at": int(time.time()) + 1800}))
        if isinstance(body, Exception):
            raise body
        return _Response(body)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return requests


def test_models_returns_ids_from_list_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """``GET /models`` парсит список ID и шлёт Bearer-токен."""
    models_url = "https://api.test/api/v1/models"
    body = json.dumps(
        {"object": "list", "data": [{"id": "GigaChat-2"}, {"id": "GigaChat-2-Pro"}]}
    )

    requests: list[urllib.request.Request] = []

    def urlopen(request: urllib.request.Request, timeout: Any = None, context: Any = None) -> Any:
        requests.append(request)
        if "oauth" in request.full_url:
            return _Response(json.dumps({"access_token": "tok", "expires_at": int(time.time()) + 1800}))
        return _Response(body)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    models = GigaChatClient(_gigachat_settings()).models()

    assert models == ["GigaChat-2", "GigaChat-2-Pro"]
    request = requests[-1]
    assert request.full_url == models_url
    assert request.get_method() == "GET"
    assert request.headers["Authorization"] == "Bearer tok"


def test_chat_models_excludes_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    """В список моделей чата не попадают эмбеддинги (``type != "chat"``).

    GigaChat отдаёт эмбеддинги в том же ``/models``; в выпадающем списке моделей
    чата они недопустимы. Полный :meth:`models` при этом не сужается — он нужен
    диагностике (``--check``).
    """
    body = json.dumps(
        {
            "object": "list",
            "data": [
                {"id": "GigaChat-2", "type": "chat"},
                {"id": "Embeddings", "type": "embedder"},
                {"id": "Embeddings-2", "type": "embedder"},
                {"id": "GigaChat-3-Ultra", "type": "chat"},
                {"id": "БезТипа"},
            ],
        }
    )
    _patch_models(monkeypatch, body)

    client = GigaChatClient(_gigachat_settings())

    assert client.chat_models() == ["GigaChat-2", "GigaChat-3-Ultra", "БезТипа"]
    assert "Embeddings" in client.models()


def test_check_provider_ok_when_model_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """Модель из настроек есть в списке — проверка успешна."""
    _patch_models(monkeypatch, json.dumps({"data": [{"id": "GigaChat-2-Pro"}]}))

    result = check_provider(_gigachat_settings(model="GigaChat-2-Pro"))

    assert result.ok is True
    assert result.model_available is True
    assert result.models == ("GigaChat-2-Pro",)
    assert result.describe().startswith("OK")


def test_check_provider_warns_when_model_not_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ключ рабочий, но модели нет в доступе (например, Ultra на Freemium)."""
    _patch_models(monkeypatch, json.dumps({"data": [{"id": "GigaChat-2"}, {"id": "GigaChat-2-Pro"}]}))

    result = check_provider(_gigachat_settings(model="GigaChat-3-Ultra"))

    assert result.ok is True
    assert result.model_available is False
    assert "в доступе нет" in result.describe()


def test_check_provider_reports_oauth_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ошибка авторизации не маскируется: в результате текст провайдера."""
    error = urllib.error.HTTPError(
        "https://auth.test/oauth", 401, "unauthorized", {}, io.BytesIO(b'{"error":"invalid key"}')
    )

    def urlopen(request: urllib.request.Request, timeout: Any = None, context: Any = None) -> Any:
        raise error

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    result = check_provider(_gigachat_settings())

    assert result.ok is False
    assert "401" in result.error
    assert result.describe().startswith("ОШИБКА")


def test_check_provider_rejects_unconfigured() -> None:
    """Провайдер без ключа не делает сетевых запросов."""
    result = check_provider(LLMSettings(provider="gigachat"))

    assert result.ok is False
    assert "не настроен" in result.error


def test_check_provider_checks_openai_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    """Для DeepSeek проверяется ``/models`` с Bearer-ключом.

    Список моделей OpenAI-совместимых провайдеров не авторитетен (DeepSeek
    обслуживает ``deepseek-chat`` как алиас, не показывая его в ``/models``),
    поэтому ``model_available`` здесь не вычисляется — только живость ключа.
    """
    body = json.dumps({"data": [{"id": "deepseek-flash"}, {"id": "deepseek-v4-pro"}]})
    requests: list[urllib.request.Request] = []

    def urlopen(request: urllib.request.Request, timeout: Any = None, context: Any = None) -> Any:
        requests.append(request)
        return _Response(body)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    settings = LLMSettings(
        provider="deepseek",
        api_key="ds-key",
        model="deepseek-chat",
        base_url="https://api.deepseek.test/v1",
    )

    result = check_provider(settings)

    assert result.ok is True
    assert result.model_available is None, "алиасы не должны давать ложное «модели нет»"
    assert "deepseek-flash" in result.describe()
    assert requests[-1].full_url.endswith("/models")
    assert requests[-1].headers["Authorization"] == "Bearer ds-key"


def test_check_provider_rejects_non_ascii_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Кириллица в ключе (незаменённый плейсхолдер) — внятная ошибка, а не трейсбек."""
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *args, **kwargs: pytest.fail("сеть не должна вызываться"),
    )
    settings = LLMSettings(
        provider="gigachat",
        api_key="ВСТАВЬТЕ_КЛЮЧ",
        base_url="https://api.test/api/v1",
        auth_url="https://auth.test/api/v2/oauth",
    )

    result = check_provider(settings)

    assert result.ok is False
    assert "плейсхолдер" in result.error


def test_check_providers_skips_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Проверяются только настроенные провайдеры: openai/ollama без ключа пропущены."""
    _patch_models(monkeypatch, json.dumps({"data": [{"id": "GigaChat-2-Pro"}]}))
    env = {"LLM_PROVIDER": "deepseek", "LLM_API_KEY": "ds-key", "LLM_API_KEY_GIGACHAT": "gc-key"}

    checks = check_providers(env)

    assert [check.provider for check in checks] == ["deepseek", "gigachat"]


def test_provider_check_describe_never_leaks_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Текст результата не содержит ключа — его можно печатать в чат."""
    _patch_models(monkeypatch, json.dumps({"data": [{"id": "GigaChat-2-Pro"}]}))

    text = check_provider(_gigachat_settings()).describe()

    assert "a2V5OnNlY3JldA" not in text
    assert ProviderCheck is not None


def test_check_suggests_model_of_same_level(monkeypatch: pytest.MonkeyPatch) -> None:
    """Уровень модели сопоставляется между версиями, но не подменяется.

    ``GigaChat-3-Pro`` — уровень Pro, поэтому находится как ``GigaChat-2-Pro``.
    ``GigaChat-2-Max`` при доступной только Ultra не находит пары: это другой
    уровень, другая цена и скорость.
    """
    _patch_models(
        monkeypatch,
        json.dumps({"data": [{"id": "GigaChat-3-Ultra"}, {"id": "GigaChat-2-Pro"}]}),
    )

    same_level = check_provider(_gigachat_settings(model="GigaChat-3-Pro"))
    other_level = check_provider(_gigachat_settings(model="GigaChat-2-Max"))

    assert same_level.model_available is True
    assert same_level.suggestion == "GigaChat-2-Pro"
    assert other_level.model_available is False, "уровень Max не подменяется на Ultra"
    assert "в доступе нет" in other_level.describe()


def test_closest_model_ignores_unrelated_family() -> None:
    """Чужие названия не сопоставляются: подсказка только внутри линейки."""
    from onec_analyst.llm_agent import _closest_model

    assert _closest_model("Qwen-Coder", ["GigaChat-2-Pro"]) == ""
    assert _closest_model("GigaChat-Pro", ["GigaChat-2-Pro", "GigaChat-2"]) == "GigaChat-2-Pro"


#: Как кабинет GigaChat показывает модели: пробел вместо дефиса и без номера версии.
_CABINET_MODELS = ("GigaChat Ultra", "GigaChat Max", "GigaChat Pro", "GigaChat Lite")


@pytest.mark.parametrize(
    ("configured", "counterpart"),
    [
        ("GigaChat-2-Pro", "GigaChat Pro"),
        ("GigaChat-3-Ultra", "GigaChat Ultra"),
        ("GigaChat-2-Max", "GigaChat Max"),
        ("GigaChat-2-Lite", "GigaChat Lite"),
    ],
)
def test_check_accepts_cabinet_spelling(monkeypatch: pytest.MonkeyPatch, configured: str, counterpart: str) -> None:
    """Название из кода («GigaChat-2-Pro») соответствует кабинету («GigaChat Pro»).

    Кабинет печатает уровень без номера версии и с пробелом; сравнивать строки
    буквально нельзя — иначе рабочая модель объявляется отсутствующей.
    """
    _patch_models(monkeypatch, json.dumps({"data": [{"id": name} for name in _CABINET_MODELS]}))

    result = check_provider(_gigachat_settings(model=configured))

    assert result.model_available is True
    assert result.suggestion == counterpart
    assert "в доступе нет" not in result.describe()


def test_check_unknown_model_warns_without_false_suggestion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Неизвестный уровень — только предупреждение, без ложной подсказки.

    Подсказывать здесь опасно: предложенная модель другого уровня изменит
    поведение и стоимость. Точное имя покажет сам ``--check`` в списке.
    """
    _patch_models(
        monkeypatch,
        json.dumps({"data": [{"id": "GigaChat Ultra"}, {"id": "GigaChat Pro"}]}),
    )

    result = check_provider(_gigachat_settings(model="GigaChat-9-Mini"))

    assert result.model_available is False
    assert result.suggestion == ""
    assert "в доступе нет" in result.describe()
