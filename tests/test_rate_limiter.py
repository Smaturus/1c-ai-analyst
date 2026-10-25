"""Тесты учёта лимитов LLM (`rate_limiter`) и интеграции с UI.

Проверяются: счётчик вызовов, лимит на сессию, глобальный лимит, сброс при
смене даты, демо-сброс сессии, устойчивость к недоступной SQLite-базе, а также
баннеры и счётчик в Streamlit-сайдбаре (через `AppTest`, без сетевых вызовов).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from rate_limiter import (
    MAX_GLOBAL,
    MAX_PER_SESSION,
    LimitStatus,
    RateLimiter,
    RateLimiterError,
)

APP_FILE = Path(__file__).resolve().parents[1] / "app.py"
TIMEOUT = 120


# --------------------------------------------------------------------------- #
# Базовый учёт
# --------------------------------------------------------------------------- #
def test_new_session_is_allowed_and_counts_zero(rate_limit_db_path: Path) -> None:
    """Новая сессия может вызывать LLM, счётчики нулевые, лимиты по умолчанию."""
    status = RateLimiter(rate_limit_db_path).check_limit("session-a")

    assert isinstance(status, LimitStatus)
    assert status.allowed is True
    assert status.reason is None
    assert status.used_session == 0
    assert status.used_global == 0
    assert status.limit_session == MAX_PER_SESSION
    assert status.limit_global == MAX_GLOBAL


def test_increment_counts_per_session_and_globally(rate_limit_db_path: Path) -> None:
    """Успешные вызовы учитываются отдельно по сессиям и суммарно."""
    limiter = RateLimiter(rate_limit_db_path)

    assert limiter.increment("session-a") == 1
    assert limiter.increment("session-a") == 2
    assert limiter.increment("session-b") == 1

    status_a = limiter.check_limit("session-a")
    status_b = limiter.check_limit("session-b")
    assert status_a.used_session == 2
    assert status_b.used_session == 1
    assert status_a.used_global == 3
    assert status_b.used_global == 3


def test_counts_persist_between_instances(rate_limit_db_path: Path) -> None:
    """Счётчик хранится в SQLite и переживает пересоздание объекта."""
    RateLimiter(rate_limit_db_path).increment("session-a")

    status = RateLimiter(rate_limit_db_path).check_limit("session-a")
    assert status.used_session == 1


# --------------------------------------------------------------------------- #
# Лимиты
# --------------------------------------------------------------------------- #
def test_session_limit_blocks_after_max(rate_limit_db_path: Path) -> None:
    """После N вызовов сессия блокируется с причиной ``session``."""
    limiter = RateLimiter(rate_limit_db_path)
    for _ in range(2):
        limiter.increment("session-a")

    status = limiter.check_limit("session-a", max_per_session=2)
    assert status.allowed is False
    assert status.reason == "session"
    assert status.used_session == 2
    # Другая сессия при этом не заблокирована.
    assert limiter.check_limit("session-b", max_per_session=2).allowed is True


def test_global_limit_blocks_all_sessions(rate_limit_db_path: Path) -> None:
    """Глобальный лимит считается по сумме всех сессий за день."""
    limiter = RateLimiter(rate_limit_db_path)
    limiter.increment("session-a")
    limiter.increment("session-a")
    limiter.increment("session-b")

    blocked = limiter.check_limit("session-c", max_per_session=15, max_global=3)
    assert blocked.allowed is False
    assert blocked.reason == "global"
    assert blocked.used_global == 3


def test_limits_reset_on_new_date(rate_limit_db_path: Path) -> None:
    """Смена календарной даты обнуляет счётчики (новая строка в таблице)."""
    day_one = RateLimiter(rate_limit_db_path, today=date(2026, 10, 25))
    day_one.increment("session-a")
    day_one.increment("session-a")
    assert day_one.check_limit("session-a").used_session == 2

    day_two = RateLimiter(rate_limit_db_path, today=date(2026, 10, 26))
    status = day_two.check_limit("session-a")
    assert status.used_session == 0
    assert status.used_global == 0
    assert status.allowed is True


def test_reset_session_clears_today_counter(rate_limit_db_path: Path) -> None:
    """Демо-сброс очищает счётчик текущей сессии, не трогая другие."""
    limiter = RateLimiter(rate_limit_db_path)
    limiter.increment("session-a")
    limiter.increment("session-b")

    limiter.reset_session("session-a")

    assert limiter.check_limit("session-a").used_session == 0
    assert limiter.check_limit("session-b").used_session == 1
    assert limiter.check_limit("session-a").used_global == 1


# --------------------------------------------------------------------------- #
# Ошибки SQLite и настройка пути
# --------------------------------------------------------------------------- #
def test_unavailable_db_raises_rate_limiter_error(rate_limit_db_path: Path) -> None:
    """Если вместо файла базы — каталог, ошибка оборачивается в RateLimiterError."""
    directory = rate_limit_db_path.parent / "db_as_directory"
    directory.mkdir(parents=True, exist_ok=True)

    limiter = RateLimiter(directory)
    with pytest.raises(RateLimiterError):
        limiter.check_limit("session-a")
    with pytest.raises(RateLimiterError):
        limiter.increment("session-a")


def test_db_path_can_be_overridden_by_env(
    monkeypatch: pytest.MonkeyPatch, rate_limit_db_path: Path
) -> None:
    """Путь к базе берётся из ``RATE_LIMIT_DB`` (нужно для демо-деплоя)."""
    monkeypatch.setenv("RATE_LIMIT_DB", str(rate_limit_db_path))
    assert RateLimiter().db_path == rate_limit_db_path


# --------------------------------------------------------------------------- #
# Интеграция с UI (AppTest, без реальных вызовов LLM)
# --------------------------------------------------------------------------- #
def _run_app(monkeypatch: pytest.MonkeyPatch) -> AppTest:
    """Запустить приложение в офлайн-режиме и вернуть AppTest."""
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    return AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()


def test_sidebar_shows_usage_counter(
    monkeypatch: pytest.MonkeyPatch, rate_limit_db_path: Path
) -> None:
    """В сайдбаре виден счётчик «Использовано запросов к ИИ: 0/15»."""
    at = _run_app(monkeypatch)

    captions = [caption.value for caption in at.sidebar.caption]
    assert any("Использовано запросов к ИИ: 0/15" in text for text in captions)
    assert at.session_state["llm_session_id"]


def test_session_limit_shows_warning_and_reset_button(
    monkeypatch: pytest.MonkeyPatch, rate_limit_db_path: Path
) -> None:
    """Исчерпанный лимит сессии: предупреждение, офлайн-режим и кнопка сброса."""
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    session_id = str(at.session_state["llm_session_id"])

    limiter = RateLimiter(rate_limit_db_path)
    for _ in range(MAX_PER_SESSION):
        limiter.increment(session_id)
    at.run()

    assert any("Вы достигли лимита запросов к ИИ" in warning.value for warning in at.warning)
    reset = [button for button in at.button if button.label == "Сбросить лимит (Демо-режим)"]
    assert reset, "кнопка демо-сброса лимита должна быть видна"

    reset[0].click()
    at.run()
    captions = [caption.value for caption in at.sidebar.caption]
    assert any("Использовано запросов к ИИ: 0/15" in text for text in captions)
    assert not any("Вы достигли лимита запросов к ИИ" in warning.value for warning in at.warning)


def test_global_limit_shows_banner(
    monkeypatch: pytest.MonkeyPatch, rate_limit_db_path: Path
) -> None:
    """Глобальный дневной лимит: баннер вверху страницы и офлайн-режим."""
    limiter = RateLimiter(rate_limit_db_path)
    for _ in range(MAX_GLOBAL):
        limiter.increment("other-session")
    at = _run_app(monkeypatch)

    assert any("Дневной лимит ИИ-запросов исчерпан" in error.value for error in at.error)
