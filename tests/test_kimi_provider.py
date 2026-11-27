"""Провайдер Kimi (Moonshot AI): каталог, env, клиент, Notion-схема.

Живых запросов к API здесь нет: ``--check`` требует действующего ключа и
выполняется отдельно. Проверяется регистрация провайдера и разбор настроек —
именно эти места ломаются молча, если добавить провайдера только в один из
четырёх файлов.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import _PROVIDER_TITLES, KIMI_MODELS, choose_kimi_model
from onec_analyst import notion_export
from onec_analyst.llm_agent import (
    CHOOSABLE_PROVIDERS,
    GigaChatClient,
    available_providers,
    make_client,
    provider_catalog,
    settings_from_env,
)
from tests.paths import REPO_ROOT

KIMI_MODEL = "kimi-k3"
KIMI_BASE_URL = "https://api.moonshot.ai/v1"


def _by_provider(env: dict[str, str]) -> dict[str, object]:
    """Каталог провайдеров в виде словаря ``provider -> ProviderOption``."""
    return {option.settings.provider: option for option in provider_catalog(env)}


# --------------------------------------------------------------------------- #
# Каталог провайдеров: Kimi виден в переключателе, даже без ключа
# --------------------------------------------------------------------------- #
def test_kimi_is_choosable_provider() -> None:
    """``kimi`` входит в список выбираемых и попадает в каталог UI."""
    assert "kimi" in CHOOSABLE_PROVIDERS

    by_provider = _by_provider({"LLM_PROVIDER": "deepseek", "LLM_API_KEY": "ds-key"})

    assert "kimi" in by_provider
    option = by_provider["kimi"]
    assert option.settings.model == KIMI_MODEL
    assert option.settings.base_url == KIMI_BASE_URL
    # Ключа нет — провайдер показан, но помечен «не настроен».
    assert option.configured is False


def test_kimi_defaults_are_live_model_and_global_endpoint() -> None:
    """Дефолты — ``kimi-k3`` и глобальный эндпоинт Moonshot.

    Снятые имена (``kimi-k2-0905-preview``, ``kimi-k2-turbo-preview``,
    ``kimi-latest``) отдают 404, поэтому дефолтом они быть не могут.
    """
    settings = settings_from_env({"LLM_PROVIDER": "kimi", "LLM_API_KEY": "k"}, provider="kimi")

    assert settings.provider == "kimi"
    assert settings.model == KIMI_MODEL
    assert settings.base_url == KIMI_BASE_URL


def test_kimi_selector_knows_default_model() -> None:
    """Модель из ``.env`` по умолчанию есть в списке селектора «Модель LLM»."""
    assert KIMI_MODELS[0] == KIMI_MODEL
    assert KIMI_MODEL in KIMI_MODELS


# --------------------------------------------------------------------------- #
# Настройки: свои переменные, изоляция от провайдера по умолчанию
# --------------------------------------------------------------------------- #
def test_kimi_reads_its_own_env_vars() -> None:
    """``LLM_*_KIMI`` перекрывают дефолты, включая таймаут и региональный URL."""
    env = {
        "LLM_PROVIDER": "deepseek",
        "LLM_API_KEY": "ds-key",
        "LLM_API_KEY_KIMI": "kimi-key",
        "LLM_MODEL_KIMI": "kimi-k3.1-pro",
        "LLM_BASE_URL_KIMI": "https://api.moonshot.cn/v1",
        "LLM_TIMEOUT_KIMI": "120",
        "LLM_TIMEOUT": "60",
    }

    settings = settings_from_env(env, provider="kimi")

    assert settings.api_key == "kimi-key"
    assert settings.model == "kimi-k3.1-pro"
    assert settings.base_url == "https://api.moonshot.cn/v1"
    assert settings.timeout == 120.0


def test_kimi_is_configured_only_with_own_key() -> None:
    """Общий ``LLM_API_KEY`` принадлежит провайдеру по умолчанию, не Kimi."""
    without_key = _by_provider({"LLM_PROVIDER": "deepseek", "LLM_API_KEY": "ds-key"})
    assert without_key["kimi"].configured is False
    assert [settings.provider for settings in available_providers(
        {"LLM_PROVIDER": "deepseek", "LLM_API_KEY": "ds-key"}
    )] == ["deepseek"]

    with_key = _by_provider(
        {
            "LLM_PROVIDER": "deepseek",
            "LLM_API_KEY": "ds-key",
            "LLM_API_KEY_KIMI": "kimi-key",
        }
    )
    assert with_key["kimi"].configured is True
    assert [settings.provider for settings in available_providers(
        {
            "LLM_PROVIDER": "deepseek",
            "LLM_API_KEY": "ds-key",
            "LLM_API_KEY_KIMI": "kimi-key",
        }
    )] == ["deepseek", "kimi"]


def test_generic_model_does_not_leak_to_kimi() -> None:
    """``LLM_MODEL``/``LLM_BASE_URL`` провайдера по умолчанию не уходят в Kimi."""
    env = {
        "LLM_PROVIDER": "deepseek",
        "LLM_API_KEY": "ds-key",
        "LLM_MODEL": "deepseek-chat",
        "LLM_BASE_URL": "https://api.deepseek.com/v1",
        "LLM_API_KEY_KIMI": "kimi-key",
    }

    settings = settings_from_env(env, provider="kimi")

    assert settings.model == KIMI_MODEL
    assert settings.base_url == KIMI_BASE_URL


# --------------------------------------------------------------------------- #
# Температура: K3 принимает только 1 — общий LLM_TEMPERATURE ей не подходит
# --------------------------------------------------------------------------- #
def test_kimi_temperature_is_one_by_default() -> None:
    """``LLM_TEMPERATURE=0`` не уходит в Kimi: на любое значение ≠1 приходит 400."""
    settings = settings_from_env(
        {
            "LLM_PROVIDER": "deepseek",
            "LLM_API_KEY": "ds-key",
            "LLM_TEMPERATURE": "0",
            "LLM_API_KEY_KIMI": "kimi-key",
        },
        provider="kimi",
    )

    assert settings.temperature == 1.0


def test_kimi_payload_carries_locked_temperature() -> None:
    """В теле запроса к Kimi — ``temperature: 1.0``, а не общий ноль."""
    settings = settings_from_env(
        {"LLM_PROVIDER": "kimi", "LLM_API_KEY": "kimi-key", "LLM_TEMPERATURE": "0"},
        provider="kimi",
    )

    payload = make_client(settings)._payload([{"role": "user", "content": "?"}])

    assert payload["temperature"] == 1.0
    assert payload["model"] == KIMI_MODEL


def test_kimi_temperature_can_be_overridden_for_k2_models() -> None:
    """Своя переменная перекрывает фиксацию: у семейства K2 диапазон не зашит."""
    settings = settings_from_env(
        {
            "LLM_PROVIDER": "kimi",
            "LLM_API_KEY": "kimi-key",
            "LLM_TEMPERATURE": "0",
            "LLM_TEMPERATURE_KIMI": "0.3",
        },
        provider="kimi",
    )

    assert settings.temperature == 0.3


def test_generic_temperature_still_applies_to_other_providers() -> None:
    """Общая температура остаётся рабочей для всех остальных провайдеров."""
    settings = settings_from_env(
        {"LLM_PROVIDER": "deepseek", "LLM_API_KEY": "ds-key", "LLM_TEMPERATURE": "0.2"},
        provider="deepseek",
    )

    assert settings.temperature == 0.2


# --------------------------------------------------------------------------- #
# Валидация: неизвестный провайдер по-прежнему падает
# --------------------------------------------------------------------------- #
def test_unknown_provider_is_still_rejected() -> None:
    """Опечатка в имени получает ``ValueError`` со списком допустимых значений."""
    with pytest.raises(ValueError) as excinfo:
        settings_from_env({"LLM_PROVIDER": "kimix", "LLM_API_KEY": "k"}, provider="kimix")

    message = str(excinfo.value)
    assert "kimix" in message
    assert "kimi" in message


# --------------------------------------------------------------------------- #
# Клиент и Notion: отдельный клиент не нужен, имя в отчёте есть
# --------------------------------------------------------------------------- #
def test_kimi_uses_openai_compatible_client() -> None:
    """Kimi OpenAI-совместим, поэтому GigaChat-клиент ему не подставляется."""
    settings = settings_from_env(
        {"LLM_PROVIDER": "kimi", "LLM_API_KEY": "kimi-key"}, provider="kimi"
    )

    assert not isinstance(make_client(settings), GigaChatClient)


def test_notion_schema_lists_kimi_provider() -> None:
    """В схеме базы Notion у «Провайдера» есть значение ``kimi``."""
    schema = notion_export.database_schema()
    options = [option["name"] for option in schema["Провайдер"]["select"]["options"]]

    assert "kimi" in options
    assert "rules" in options


# --------------------------------------------------------------------------- #
# Зеркальные файлы: app.py и app_local.py правятся вручную, сверяем маркеры
# --------------------------------------------------------------------------- #
KIMI_APP_MARKERS = (
    '"kimi": "Kimi",',
    "KIMI_MODELS: Final[tuple[str, ...]] = (",
    "def choose_kimi_model(",
    'elif chosen_settings.provider == "kimi":',
    "chosen_settings = choose_kimi_model(chosen_settings)",
)


@pytest.mark.parametrize("filename", ["app.py", "app_local.py"])
def test_app_files_expose_kimi_selector(filename: str) -> None:
    """Оба приложения содержат подпись, селектор и ветку вызова Kimi."""
    text = Path(REPO_ROOT, filename).read_text(encoding="utf-8")

    for marker in KIMI_APP_MARKERS:
        assert marker in text, f"{filename}: нет маркера {marker!r}"


def test_kimi_title_for_sidebar() -> None:
    """Подпись в сайдбаре — «Kimi», а не машинное имя провайдера."""
    assert _PROVIDER_TITLES["kimi"] == "Kimi"
    assert callable(choose_kimi_model)


#: Подписи, которые раньше зависели от провайдера, а теперь запрещены.
VENDOR_MODEL_LABELS = ('"Модель Kimi"', '"Модель Mistral"', '"Модель GigaChat"')


@pytest.mark.parametrize("filename", ["app.py", "app_local.py"])
def test_model_selector_label_is_uniform(filename: str) -> None:
    """Селектор модели называется одинаково у всех провайдеров — «Модель LLM».

    Заголовок — часть контракта UI: по нему ищут виджет человек и тесты
    ``AppTest``. Пять вхождений — это GigaChat, Mistral, Groq, Kimi и Ollama.
    """
    text = Path(REPO_ROOT, filename).read_text(encoding="utf-8")

    for old_label in VENDOR_MODEL_LABELS:
        assert old_label not in text, f"{filename}: вернулась подпись {old_label}"
    assert text.count('"Модель LLM"') >= 5, f"{filename}: меньше пяти селекторов модели"
