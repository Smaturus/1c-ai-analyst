"""Тесты выбора планировщика и переключателя «Использовать LLM» в сайдбаре.

Проверяют:
* :func:`app.select_planner` — LLM используется только при явном включении и
  настроенном провайдере, иначе берутся офлайн-правила;
* виджет ``st.toggle`` в сайдбаре: доступен при настроенной LLM, заблокирован
  без ключа, а его выключение реально переводит приложение в офлайн-режим.
"""

from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

from app import select_planner
from onec_analyst.llm_agent import LLMPlanner, LLMSettings, RuleBasedPlanner
from tests.paths import APP_FILE

TIMEOUT = 120


def _configured_settings() -> LLMSettings:
    """Настройки с валидным (фиктивным) ключом: сеть не нужна."""
    return LLMSettings(provider="deepseek", model="deepseek-chat", api_key="test-key")


# --------------------------------------------------------------------------- #
# select_planner: чистая функция выбора
# --------------------------------------------------------------------------- #
def test_select_planner_uses_llm_when_enabled_and_configured() -> None:
    """Флаг включён и есть ключ → LLM-планировщик."""
    assert isinstance(select_planner(True, _configured_settings()), LLMPlanner)


def test_select_planner_falls_back_to_rules_when_disabled() -> None:
    """Флаг выключен → офлайн-правила, даже если ключ есть."""
    assert isinstance(select_planner(False, _configured_settings()), RuleBasedPlanner)


def test_select_planner_falls_back_to_rules_without_key() -> None:
    """LLM не настроена → флаг включить нечего, работают правила."""
    settings = LLMSettings(provider="none")

    assert isinstance(select_planner(True, settings), RuleBasedPlanner)


# --------------------------------------------------------------------------- #
# Переключатель в сайдбаре
# --------------------------------------------------------------------------- #
@pytest.fixture()
def llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Окружение с настроенной LLM (ключ фиктивный, сеть не вызывается)."""
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
    monkeypatch.setenv("LLM_API_KEY", "test-key")


def test_sidebar_toggle_is_available_when_llm_configured(llm_env: None) -> None:
    """При настроенной LLM переключатель активен и включён по умолчанию."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()

    assert len(at.exception) == 0
    toggle = at.sidebar.toggle[0]
    assert toggle.label == "Использовать LLM"
    assert toggle.value is True
    assert toggle.disabled is False


def test_sidebar_toggle_is_disabled_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без ключа переключатель заблокирован, в сайдбаре — пояснение про офлайн."""
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()

    assert len(at.exception) == 0
    toggle = at.sidebar.toggle[0]
    assert toggle.disabled is True
    assert any("Офлайн-режим" in info.value for info in at.sidebar.info)


def test_toggle_off_switches_question_to_offline_rules(llm_env: None) -> None:
    """Выключение переключателя переводит разбор вопроса на офлайн-правила."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    at.sidebar.toggle[0].set_value(False).run()
    at.text_input[0].set_value("Сколько мы продали за 2024 год?")
    next(button for button in at.button if button.label == "Спросить").click()
    at.run()

    assert len(at.exception) == 0
    modes = [markdown.value for markdown in at.markdown if "Режим" in markdown.value]
    assert modes, "в «Технических деталях» должен быть указан режим расчёта"
    assert "офлайн-правила" in modes[0]


def test_toggle_on_keeps_llm_mode_label(llm_env: None) -> None:
    """При включённом переключателе выбран LLM-планировщик (проверяем подпись в сайдбаре)."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()

    captions = [caption.value for caption in at.sidebar.caption]
    assert any("deepseek" in caption for caption in captions)


# --------------------------------------------------------------------------- #
# Переключатель провайдера (DeepSeek + GigaChat в одном приложении)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def two_providers_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Два провайдера одновременно: deepseek (общий ключ) + gigachat (свой ключ).

    Модель GigaChat задаётся явно: реальный ``.env`` может содержать
    ``LLM_MODEL_GIGACHAT`` (например, ``GigaChat Pro``), и тест не должен
    зависеть от локального файла.
    """
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_API_KEY_GIGACHAT", "test-gc-key")
    monkeypatch.setenv("LLM_MODEL_GIGACHAT", "GigaChat-2-Pro")


def test_sidebar_shows_provider_selector_when_two_providers(
    two_providers_env: None,
) -> None:
    """Два настроенных провайдера → в сайдбаре появляется выбор провайдера."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()

    assert len(at.exception) == 0
    assert len(at.sidebar.selectbox) == 1
    captions = [caption.value for caption in at.sidebar.caption]
    assert any("deepseek" in caption for caption in captions), "по умолчанию — deepseek"


GIGACHAT_LABEL = "GigaChat"


def test_sidebar_switches_to_gigachat(two_providers_env: None) -> None:
    """Выбор GigaChat в переключателе меняет активного провайдера."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    at.sidebar.selectbox[0].select(GIGACHAT_LABEL).run()

    assert len(at.exception) == 0
    captions = [caption.value for caption in at.sidebar.caption]
    assert any("gigachat" in caption for caption in captions), captions


def test_sidebar_gigachat_keeps_own_default_model(two_providers_env: None) -> None:
    """После переключения GigaChat не наследует ``LLM_MODEL=deepseek-chat``.

    Регрессия системного дефекта: общая ``LLM_MODEL`` была моделью любого
    выбранного провайдера, хотя принадлежит только провайдеру по умолчанию.
    """
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    options = list(at.sidebar.selectbox[0].options)
    assert GIGACHAT_LABEL in options, options

    at.sidebar.selectbox[0].select(GIGACHAT_LABEL).run()

    assert len(at.exception) == 0
    captions = [caption.value for caption in at.sidebar.caption]
    assert any("gigachat" in caption and "GigaChat-2-Pro" in caption for caption in captions), (
        captions
    )
    assert not any("deepseek-chat" in caption for caption in captions if "gigachat" in caption)


def test_sidebar_shows_unconfigured_gigachat(llm_env: None) -> None:
    """Даже без ключа GigaChat виден в списке с пометкой «не настроен»."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()

    assert len(at.exception) == 0
    labels = list(at.sidebar.selectbox[0].options)
    assert any("GigaChat" in label and "не настроен" in label for label in labels), labels


def test_sidebar_selecting_unconfigured_provider_warns(llm_env: None) -> None:
    """Выбор ненастроенного GigaChat → предупреждение и офлайн-правила."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    label = next(
        option
        for option in at.sidebar.selectbox[0].options
        if option.startswith("GigaChat")
    )
    at.sidebar.selectbox[0].select(label).run()

    assert len(at.exception) == 0
    assert any("не настроен" in item.value for item in at.sidebar.warning)


def test_unconfigured_openai_hint_is_provider_specific(llm_env: None) -> None:
    """Подсказка для openai не должна упоминать GigaChat."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    label = next(
        option for option in at.sidebar.selectbox[0].options if option.startswith("OpenAI")
    )
    at.sidebar.selectbox[0].select(label).run()

    warnings = " ".join(item.value for item in at.sidebar.warning)
    assert "openai" in warnings
    assert "GIGACHAT" not in warnings.upper()


def test_unconfigured_gigachat_hint_mentions_gigachat_vars(llm_env: None) -> None:
    """Подсказка для GigaChat упоминает scope и сертификат НУЦ."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    label = next(
        option
        for option in at.sidebar.selectbox[0].options
        if option.startswith("GigaChat")
    )
    at.sidebar.selectbox[0].select(label).run()

    warnings = " ".join(item.value for item in at.sidebar.warning)
    assert "GIGACHAT_SCOPE" in warnings
    assert "LLM_TLS_CA_BUNDLE" in warnings


# --------------------------------------------------------------------------- #
# Модель GigaChat: выбор в сайдбаре из фактического списка API
# --------------------------------------------------------------------------- #
_API_MODELS = ("GigaChat Ultra", "GigaChat Max", "GigaChat Pro", "GigaChat Lite")


class _FakeResponse:
    """Минимальный ответ ``urlopen`` для OAuth и ``/models``."""

    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


@pytest.fixture()
def gigachat_models_api(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Подменить сеть: OAuth отдаёт токен, ``/models`` — четыре уровня.

    Возвращаемый словарь считает запросы к ``/models``. Кеш живёт в
    ``st.session_state`` (на сессию ``AppTest``), поэтому между тестами не
    протекает и явного сброса не требует.
    """
    import json
    import time
    import urllib.request

    calls = {"models": 0}

    def urlopen(request: object, timeout: object = None, context: object = None) -> _FakeResponse:
        url = getattr(request, "full_url", "")
        if "oauth" in url:
            return _FakeResponse(
                json.dumps({"access_token": "tok", "expires_at": int(time.time()) + 1800})
            )
        if url.endswith("/models"):
            calls["models"] += 1
            body = {"data": [{"id": name} for name in _API_MODELS]}
            return _FakeResponse(json.dumps(body))
        raise AssertionError(f"неожиданный запрос: {url}")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return calls


def _select_gigachat(at: AppTest) -> None:
    label = next(
        option for option in at.sidebar.selectbox[0].options if option.startswith("GigaChat")
    )
    at.sidebar.selectbox[0].select(label).run()


def test_gigachat_model_selector_lists_api_models(
    two_providers_env: None, gigachat_models_api: dict
) -> None:
    """Под GigaChat модели из API — выпадающий список, а не подпись."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    _select_gigachat(at)

    assert len(at.exception) == 0
    boxes = [box for box in at.sidebar.selectbox if box.label == "Модель GigaChat"]
    assert boxes, [box.label for box in at.sidebar.selectbox]
    options = list(boxes[0].options)
    for name in _API_MODELS:
        assert name in options, options
    # Модель из .env не подменяется на первую из списка, даже если её там нет.
    assert boxes[0].value == "GigaChat-2-Pro"


def test_gigachat_model_selection_switches_model(
    two_providers_env: None, gigachat_models_api: dict
) -> None:
    """Выбор в списке меняет модель, которой будет вызван провайдер."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    _select_gigachat(at)

    box = next(box for box in at.sidebar.selectbox if box.label == "Модель GigaChat")
    box.set_value("GigaChat Max").run()

    assert len(at.exception) == 0
    captions = " ".join(caption.value for caption in at.sidebar.caption)
    assert "GigaChat Max" in captions, captions


def test_gigachat_model_selector_hidden_for_other_providers(
    two_providers_env: None, gigachat_models_api: dict
) -> None:
    """Для DeepSeek селектора моделей GigaChat нет и запрос не делается."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()

    assert len(at.exception) == 0
    labels = [box.label for box in at.sidebar.selectbox]
    assert labels == ["Провайдер LLM"], labels
    assert gigachat_models_api["models"] == 0


def test_gigachat_model_selector_empty_on_api_failure(
    two_providers_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ошибка сети/ключа — селектор не рисуется, приложение работает дальше."""
    import urllib.error
    import urllib.request

    def urlopen(request: object, timeout: object = None, context: object = None) -> object:
        raise urllib.error.URLError("network down")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    _select_gigachat(at)

    assert len(at.exception) == 0
    labels = [box.label for box in at.sidebar.selectbox]
    assert "Модель GigaChat" not in labels, labels
    captions = " ".join(caption.value for caption in at.sidebar.caption)
    assert "gigachat" in captions, "остальная панель должна работать"
