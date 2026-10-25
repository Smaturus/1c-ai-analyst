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
    assert toggle.label == "Использовать LLM (DeepSeek/OpenAI)"
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
