"""Фоновый расчёт вопроса: job переживает перерисовку страницы.

Регрессия к случаю «нажали «Выгрузить» во время долгого ответа локальной
модели»: Streamlit обрывает текущий прогон скрипта на любом взаимодействии,
поэтому вызов LLM живёт в отдельном потоке, а UI-поток только ждёт готовности.
Эти тесты закрепляют, что поток не привязан к прогону и что следующий прогон
подхватывает тот же job, а не теряет расчёт.
"""

from __future__ import annotations

import threading
import time

import pytest
from streamlit.testing.v1 import AppTest

import app
from tests.paths import APP_FILE

TIMEOUT = 120


def test_job_runs_work_in_thread() -> None:
    """Работа выполняется и результат доступен после wait()."""
    job = app.QuestionJob(meta={"question": "q"}, work=lambda: "ok")

    job.start()

    assert job.wait(5) is True
    assert job.done is True
    assert job.result == "ok"
    assert job.error is None


def test_job_keeps_running_after_ui_stops_waiting() -> None:
    """Поток живёт дальше, даже если UI-поток перестал ждать (rerun).

    Именно это и терялось раньше: расчёт останавливался вместе с прогоном.
    """
    gate = threading.Event()
    job = app.QuestionJob(meta={}, work=lambda: (gate.wait(5), "готово")[1])

    job.start()
    assert job.wait(0.05) is False, "расчёт не должен завершиться мгновенно"

    # UI-поток «бросил» ожидание (новый прогон), работа продолжается.
    gate.set()

    assert job.wait(5) is True
    assert job.result == "готово"


def test_job_transfers_error_to_ui_thread() -> None:
    """Исключение из потока не теряется: оно поднимается в UI-потоке."""

    def boom() -> None:
        raise ValueError("сеть недоступна")

    job = app.QuestionJob(meta={}, work=boom)
    job.start()

    assert job.wait(5) is True
    assert job.error is not None
    assert isinstance(job.error, ValueError)
    assert "сеть" in str(job.error)


def _job(question: str = "лучшие 3 товара 2025", delay: float = 0.0) -> app.QuestionJob:
    """Джоб с метаданными в том виде, в каком их кладёт ``main``."""

    def work() -> None:
        if delay:
            time.sleep(delay)
        return None

    return app.QuestionJob(
        meta={
            "question": question,
            "caption": f"Расчёт по вопросу: «{question}»",
            "started": time.monotonic(),
            "request_id": "test-request",
            "turn": 1,
            "profile": None,
            "settings": None,
            "source_label": "Демо-данные",
        },
        work=work,
    )


def test_pending_job_is_picked_up_on_next_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Следующий прогон находит job в session_state и ждёт его до конца."""
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    assert len(at.exception) == 0, [str(e.value)[:300] for e in at.exception]

    job = _job(delay=0.3)
    at.session_state["question_job"] = job
    job.start()

    started = time.monotonic()
    at.run()

    assert len(at.exception) == 0, [str(e.value)[:300] for e in at.exception]
    assert job.done is True, "прогон должен был дождаться расчёта"
    assert time.monotonic() - started >= 0.25
    assert "question_job" not in at.session_state, "job должен сниматься после ответа"
