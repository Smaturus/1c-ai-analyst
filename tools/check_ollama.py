"""Smoke локального провайдера Ollama — запускает человек (нужен запущенный сервер).

Что проверяется (не качество DSL, а работоспособность транспорта):

(a) ``GET {base_url}/models`` — какие модели реально видит сервер;
(b) по одному ``chat.completions`` с коротким промптом к каждой модели;
(c) время ответа каждой модели (CPU-only: ожидаемо десятки секунд).

Скрипт использует тот же код интеграции, что и приложение:
:class:`~onec_analyst.llm_agent.LLMSettings` + :func:`~onec_analyst.llm_agent.make_client`,
поэтому проверяет и ``base_url``/заглушку ключа из ``settings_from_env``.

Малые модели почти наверняка хуже держат JSON-схему DSL — это ожидаемо,
скрипт ничего не «чинит» и live-eval не запускает.

Запуск::

    .\\.venv\\Scripts\\python.exe tools/check_ollama.py

Коды возврата: 0 — все установленные модели ответили; 1 — сервер недоступен,
моделей нет или модель не ответила.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _entry in (str(_ROOT), str(_ROOT / "src")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from onec_analyst import llm_agent  # noqa: E402

#: Модели из явного списка S2; порядок — как в отчёте.
EXPECTED_MODELS = ("qwen3:4b", "mistral:7b")

#: Короткий промпт smoke: проверяем транспорт, а не способность к DSL.
PROMPT = "Ответь одним словом: 2+2?"

#: Таймаут одного ответа: 7B на CPU может думать минуты.
TIMEOUT_SECONDS = 600.0


def _settings_for(model: str) -> llm_agent.LLMSettings:
    """Настройки ollama для конкретной модели (без чтения ``.env``)."""
    return llm_agent.LLMSettings(
        provider="ollama",
        model=model,
        api_key="ollama",
        base_url="http://localhost:11434/v1",
        timeout=TIMEOUT_SECONDS,
        temperature=0.0,
        json_mode=False,
        num_ctx=8192,
        think=False,
    )


def _client_for(model: str) -> llm_agent.LLMClient:
    """Клиент ollama для конкретной модели (OpenAI-совместимый ``LLMClient``)."""
    client = llm_agent.make_client(_settings_for(model))
    if not isinstance(client, llm_agent.LLMClient):
        raise llm_agent.LLMError(
            f"для ollama ожидался LLMClient, получен {type(client).__name__}"
        )
    return client


def main() -> int:
    try:
        available = _client_for(EXPECTED_MODELS[0]).list_models()
    except llm_agent.LLMError as exc:
        print(f"СЕРВЕР: недоступен — {exc}")
        print("Проверьте: ollama serve / Invoke-RestMethod http://localhost:11434/api/version")
        return 1

    print("МОДЕЛИ на сервере:", ", ".join(available) if available else "(пусто)")
    installed = [name for name in EXPECTED_MODELS if name in available]
    missing = [name for name in EXPECTED_MODELS if name not in available]
    for name in missing:
        print(f"МОДЕЛЬ {name}: НЕТ на сервере (не скачана — ollama pull {name})")
    if not installed:
        print("НЕТ НИ ОДНОЙ модели из списка — smoke невозможен")
        return 1

    failures = 0
    for name in installed:
        model_client = _client_for(name)
        # Первый запрос — прогрев (загрузка модели/контекста): время не метрика.
        try:
            model_client.chat([{"role": "user", "content": PROMPT}])
        except llm_agent.LLMError as exc:
            print(f"WARMUP {name}: ОШИБКА — {exc}")
            failures += 1
            continue
        started = time.perf_counter()
        try:
            answer = model_client.chat([{"role": "user", "content": PROMPT}])
        except llm_agent.LLMError as exc:
            elapsed = time.perf_counter() - started
            print(f"SMOKE {name}: ОШИБКА за {elapsed:.1f} c — {exc}")
            failures += 1
            continue
        elapsed = time.perf_counter() - started
        preview = " ".join(answer.split())[:200]
        usage = getattr(model_client, "last_usage", {}) or {}
        print(
            f"SMOKE {name}: OK за {elapsed:.1f} c — {preview!r} "
            f"(eval_count={usage.get('eval_count')}, "
            f"prompt_eval_count={usage.get('prompt_eval_count')})"
        )

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
