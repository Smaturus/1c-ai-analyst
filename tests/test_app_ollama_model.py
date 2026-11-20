"""Селектор модели Ollama в сайдбаре (аналог «Модель GigaChat»).

Список моделей — из ``GET /models``, возможности каждой — из ``/api/show``:
gemma3 не умеет thinking/tools, qwen3 умеет; переключение не должно требовать
правки ``.env`` и не должно ронять запрос 400-й ошибкой.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest
from streamlit.testing.v1 import AppTest

import app
from tests.paths import APP_FILE

TIMEOUT = 120


class _FakeResponse:
    """Минимальный ответ ``urlopen``."""

    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def _capabilities(model: str) -> list[str]:
    """Возможности как у реальных моделей: qwen3 — tools+thinking, gemma3 — нет."""
    if model.startswith("qwen3"):
        return ["completion", "tools", "thinking"]
    return ["completion", "vision"]


_MODELS = ("qwen3:4b", "gemma3:4b", "qwen3-4b-instruct-2507:latest")


@pytest.fixture()
def ollama_api(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Подменить сеть Ollama: список моделей и ``/api/show`` по каждой."""
    calls: dict[str, list[str]] = {"show": []}

    def urlopen(request: object, timeout: object = None) -> _FakeResponse:
        url = getattr(request, "full_url", "")
        if url.endswith("/models"):
            body = {"data": [{"id": name} for name in _MODELS]}
            return _FakeResponse(body)
        if url.endswith("/api/show"):
            payload = json.loads(getattr(request, "data", b"{}"))
            model = str(payload.get("model", ""))
            calls["show"].append(model)
            return _FakeResponse({"model": model, "capabilities": _capabilities(model)})
        raise AssertionError(f"неожиданный запрос: {url}")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return calls


@pytest.fixture()
def ollama_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Окружение: локальная модель включена и выбрана по умолчанию."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_ENABLED", "true")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:4b")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")


def _model_box(at: AppTest) -> object:
    return next(box for box in at.sidebar.selectbox if box.label == "Модель Ollama")


def test_ollama_model_selector_lists_models(
    ollama_env: None, ollama_api: dict[str, list[str]]
) -> None:
    """Под Ollama модели из API — выпадающий список, а не подпись."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()

    assert len(at.exception) == 0
    box = _model_box(at)
    assert list(box.options) == list(_MODELS)
    assert box.value == "qwen3:4b"


def test_env_model_without_tag_is_not_duplicated(
    ollama_env: None,
    ollama_api: dict[str, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``OLLAMA_MODEL`` без тега не даёт второй строки того же веса.

    Ollama знает модель как ``<имя>:latest``; имя из .env канонизируется, а не
    вставляется дублем рядом с тегированным.
    """
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3-4b-instruct-2507")

    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()

    assert len(at.exception) == 0
    box = _model_box(at)
    assert list(box.options) == list(_MODELS), box.options
    assert box.value == "qwen3-4b-instruct-2507:latest", box.value


def test_ollama_model_selection_switches_model(
    ollama_env: None, ollama_api: dict[str, list[str]]
) -> None:
    """Выбор в списке меняет модель, которой будет вызван провайдер."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()

    _model_box(at).set_value("gemma3:4b").run()

    assert len(at.exception) == 0
    captions = " ".join(caption.value for caption in at.sidebar.caption)
    assert "gemma3:4b" in captions, captions
    assert "gemma3:4b" in ollama_api["show"], ollama_api["show"]


def test_gemma_switches_off_think_and_tools() -> None:
    """Возможности gemma3 выключают think/tools; qwen3 их сохраняет."""
    from onec_analyst.llm_agent import LLMSettings

    gemma = LLMSettings(
        provider="ollama",
        model="gemma3:4b",
        api_key="ollama",
        base_url="http://localhost:11434/v1",
    )
    qwen = LLMSettings(
        provider="ollama",
        model="qwen3:4b",
        api_key="ollama",
        base_url="http://localhost:11434/v1",
    )

    gemma_resolved = app.with_ollama_capabilities(gemma, _capabilities("gemma3:4b"))
    qwen_resolved = app.with_ollama_capabilities(qwen, _capabilities("qwen3:4b"))

    assert gemma_resolved.think is False
    assert gemma_resolved.tools is False
    assert qwen_resolved.think is True
    assert qwen_resolved.tools is True


def test_no_selector_when_ollama_unreachable(
    ollama_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сервер не отвечает — селектор не рисуется, панель работает дальше."""

    def urlopen(request: object, timeout: object = None) -> object:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()

    assert len(at.exception) == 0
    labels = [box.label for box in at.sidebar.selectbox]
    assert "Модель Ollama" not in labels, labels
    captions = " ".join(caption.value for caption in at.sidebar.caption)
    assert "ollama" in captions, captions
