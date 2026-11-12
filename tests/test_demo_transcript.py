"""Тесты генератора демонстрационного диалога.

Главное свойство: транскрипт собран из вызовов инструментов, а не из
рукописных цифр, и каждый ответ проходит контракт «период + дата данных».
"""

from __future__ import annotations

from pathlib import Path

import pytest

import demo_transcript
from demo_transcript import STEPS, build_transcript, main
from onec_analyst import mcp_server


def test_every_step_has_period_and_data() -> None:
    transcript = build_transcript()

    assert transcript.startswith("# Демонстрационный диалог")
    assert transcript.count("**Инструмент:**") == len(STEPS)
    assert transcript.count("Период:") >= len(STEPS)
    assert transcript.count("Данные:") >= len(STEPS)
    assert "Traceback" not in transcript


def test_transcript_contains_engine_numbers() -> None:
    transcript = build_transcript()

    assert "52 253 500,00" in transcript
    assert "Ноутбук Beta" in transcript
    assert "Аксессуары" in transcript
    assert "ООО Лютик" in transcript


def test_unsupported_dataset_is_refused() -> None:
    transcript = build_transcript()

    assert "вне каталога проекта" in transcript


def test_transcript_calls_tools_not_literals(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_total(**kwargs: object) -> str:
        calls.append(kwargs)
        return "Период: 01.01.2024–31.12.2024\nДанные: выгрузка 01.01.2025 00:00\nМАРКЕР"

    monkeypatch.setattr(mcp_server, "total", fake_total)

    transcript = build_transcript()

    assert "МАРКЕР" in transcript
    assert calls


def test_broken_contract_fails_the_generator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "total", lambda **_: "ответ без контракта")

    with pytest.raises(RuntimeError, match="нарушает контракт"):
        build_transcript()


def test_check_mode_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--check"]) == 0

    captured = capsys.readouterr()
    assert "контракт соблюдён" in captured.out


def test_out_writes_file(tmp_path: Path) -> None:
    target = tmp_path / "demo.md"

    assert main(["--out", str(target)]) == 0

    text = target.read_text(encoding="utf-8")
    assert text.startswith("# Демонстрационный диалог")
    assert demo_transcript.STEPS[0].question in text


def test_json_mode_is_serializable(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--json"]) == 0

    captured = capsys.readouterr()
    assert '"question"' in captured.out
    assert '"answer"' in captured.out
