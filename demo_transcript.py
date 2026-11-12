"""Генератор демонстрационного диалога по инструментам MCP-сервера.

Запуск::

    python -X utf8 demo_transcript.py            # транскрипт в stdout
    python -X utf8 demo_transcript.py --check    # только проверка контракта
    python -X utf8 demo_transcript.py --out demo.md
    python -X utf8 demo_transcript.py --json

Ни одно число в транскрипте не вводится вручную: каждый ответ — результат
вызова инструмента. Скрипт падает, если ответ не содержит обязательных строк
контракта (период и дата данных). Это защита от подмен, из-за которых редакция
1.0 сценария получила NO-GO: демонстрация физически не может разойтись с
поведением сервера.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from onec_analyst import mcp_server  # noqa: E402
from onec_analyst.audit import to_jsonable  # noqa: E402

__all__ = ["STEPS", "Step", "build_transcript", "main"]


@dataclass(frozen=True)
class Step:
    """Один вопрос демонстрации и инструмент, которым на него отвечают."""

    question: str
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)


STEPS: tuple[Step, ...] = (
    Step("Что за данные загружены?", "describe_dataset"),
    Step("Что вообще можно спросить?", "describe_capabilities"),
    Step(
        "Сколько продали за 2024 год?",
        "total",
        {"start_date": date(2024, 1, 1), "end_date": date(2024, 12, 31)},
    ),
    Step(
        "Топ-5 товаров по выручке за 2024",
        "top_bottom_products",
        {"start_date": date(2024, 1, 1), "end_date": date(2024, 12, 31), "limit": 5},
    ),
    Step(
        "Какие товарные группы просели в 2024 к 2023?",
        "group_dynamics",
        {
            "period1_start": date(2023, 1, 1),
            "period1_end": date(2023, 12, 31),
            "period2_start": date(2024, 1, 1),
            "period2_end": date(2024, 12, 31),
            "only_drops": True,
        },
    ),
    Step(
        "У каких контрагентов упало сильнее всего?",
        "counterparty_dynamics",
        {
            "period1_start": date(2023, 1, 1),
            "period1_end": date(2023, 12, 31),
            "period2_start": date(2024, 1, 1),
            "period2_end": date(2024, 12, 31),
            "only_drops": True,
            "top_n": 5,
        },
    ),
    Step(
        "По «Лютику» возвраты есть?",
        "negative_revenue",
        {
            "start_date": date(2024, 1, 1),
            "end_date": date(2024, 12, 31),
            "group_by": "counterparty",
        },
    ),
    Step(
        "Попытка загрузить файл вне проекта",
        "load_dataset",
        {"path": "../secrets.csv"},
    ),
)


def _assert_contract(answer: str, step: Step) -> None:
    """Каждый ответ обязан содержать период и дату данных."""
    missing = [marker for marker in ("Период:", "Данные:") if marker not in answer]
    if missing:
        raise RuntimeError(
            f"шаг «{step.question}» ({step.tool}) нарушает контракт: нет {missing}"
        )


def build_transcript() -> str:
    """Собрать транскрипт: только реальные вызовы инструментов."""
    lines = [
        "# Демонстрационный диалог MCP-сервера аналитики 1С",
        "",
        "> Сгенерировано автоматически: `python -X utf8 demo_transcript.py`. "
        "Каждый ответ — результат вызова инструмента сервера, цифры не вводятся "
        "вручную. Данные — синтетический `sample_data.csv`.",
        "",
    ]
    for index, step in enumerate(STEPS, start=1):
        tool = getattr(mcp_server, step.tool)
        answer = tool(**step.arguments)
        _assert_contract(answer, step)
        lines.extend(
            [
                f"## Шаг {index}. {step.question}",
                "",
                f"**Инструмент:** `{step.tool}`",
                "",
                "```text",
                answer.rstrip(),
                "```",
                "",
            ]
        )
    return "\n".join(lines)


def as_json() -> list[dict[str, Any]]:
    """Транскрипт как данные — для тестов и сравнения версий."""
    result: list[dict[str, Any]] = []
    for step in STEPS:
        tool = getattr(mcp_server, step.tool)
        answer = tool(**step.arguments)
        _assert_contract(answer, step)
        result.append(
            {
                "question": step.question,
                "tool": step.tool,
                "arguments": to_jsonable(step.arguments),
                "answer": answer,
            }
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", help="записать транскрипт в файл (UTF-8)")
    parser.add_argument("--json", action="store_true", help="вывести JSON вместо Markdown")
    parser.add_argument(
        "--check",
        action="store_true",
        help="только проверить контракт и вернуть статус (для CI)",
    )
    args = parser.parse_args(argv)

    if args.check:
        transcript = build_transcript()
        print(f"OK: {len(STEPS)} шагов, контракт соблюдён ({len(transcript)} символов)")
        return 0

    if args.json:
        payload = json.dumps(as_json(), ensure_ascii=False, indent=2)
    else:
        payload = build_transcript()

    if args.out:
        target = Path(args.out)
        target.write_text(payload + "\n", encoding="utf-8")
        print(f"Транскрипт записан: {target}")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
