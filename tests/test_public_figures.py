"""Страж: в публичных артефактах нет бизнес-цифр непубличной базы.

Делает проверяемым принцип из ``docs/PUBLIC-DATA-POLICY.md``: публичные
документы не содержат сумм, объёмов, диапазонов дат и номенклатуры
непубличной базы 1С — только величины, воспроизводимые из синтетической
витрины ``sample_data.csv``, или явно обоснованные параметры алгоритмов.

Фингерпринты непубличной базы (имена, суммы, коды) в самом тесте **не
хранятся** — иначе они снова попали бы в публикацию. Они читаются из
локального ``.private_markers.json`` (в ``.gitignore``). В CI файла нет, и
работают общие проверки: структура путей/учётных данных и воспроизводимость
каждой денежной величины из синтетики.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE = REPO_ROOT / "sample_data.csv"
ALLOWLIST = REPO_ROOT / "tests" / "fixtures" / "public_figures_allowlist.json"
PRIVATE_MARKERS = REPO_ROOT / ".private_markers.json"
SKIP_FILES = {"tests/test_public_figures.py", "tools/sanitize_public_artifacts.py"}
PUBLIC_PREFIXES = ("docs/", "src/")
PUBLIC_ROOT = (
    "README.md",
    "CHANGELOG.md",
    "app.py",
    "smoke_test.py",
    "smoke_llm.py",
    "demo_transcript.py",
    "generate_synthetic_data.py",
    ".env.example",
)
TEXT = {".md", ".py", ".json", ".txt", ".yml", ".yaml", ".toml", ".example", ".csv"}

#: Сгенерированные отчёты eval: суммы в них — ответы моделей на синтетических
#: кейсах прежних версий генератора; воспроизводимость проверяют кассеты.
SKIP_MONEY_PREFIXES = ("docs/EVAL_",)

#: Общие проверки, не требующие знания фингерпринтов.
BASE_FORBIDDEN: dict[str, re.Pattern[str]] = {
    "локальный каталог базы": re.compile(r"[A-Za-z]:\\+1C\b"),
    "учётка с учётными данными": re.compile(r"парол\w*\s+пуст|пуст\w*\s+парол", re.I),
}

MONEY_RUB = re.compile(r"([0-9][0-9\s\u00a0]*(?:[.,][0-9]{2})?)\s*₽")
MONEY_LABEL = re.compile(
    r"(?i)(?:выручка[^:\n]*|количество|итог|сумма[^:\n]*):\s*([0-9][0-9.,\s\u00a0]*)"
)


def forbidden() -> dict[str, re.Pattern[str]]:
    """Общие проверки плюс локальные фингерпринты, если файл есть."""
    rules = dict(BASE_FORBIDDEN)
    if PRIVATE_MARKERS.is_file():
        data = json.loads(PRIVATE_MARKERS.read_text(encoding="utf-8"))
        for item in data.get("forbidden", []):
            rules[str(item["label"])] = re.compile(str(item["regex"]), re.IGNORECASE)
    return rules


def _tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


def _public() -> list[str]:
    return [
        name
        for name in _tracked()
        if name not in SKIP_FILES
        and not name.startswith(SKIP_MONEY_PREFIXES)
        and Path(name).suffix in TEXT
        and (name.startswith(PUBLIC_PREFIXES) or name in PUBLIC_ROOT)
    ]


def _norm(raw: str) -> float | None:
    value = raw.replace("\u00a0", "").replace(" ", "").strip()
    if not value:
        return None
    if "," in value and "." in value:
        value = value.replace(",", "")
    elif "," in value:
        value = value.replace(",", ".")
    try:
        return round(float(value), 2)
    except ValueError:
        return None


def _synthetic() -> set[float]:
    """Все величины, воспроизводимые из синтетической витрины."""
    frame = pd.read_csv(SAMPLE)
    frame["year"] = frame["date"].str.slice(0, 4)
    values: set[float] = set()

    def add(series: pd.Series) -> None:
        values.update(round(abs(float(item)), 2) for item in series.to_numpy())

    add(pd.Series([frame["revenue"].sum(), frame["quantity"].sum()]))
    for keys in (
        ["year"],
        ["product_group"],
        ["product"],
        ["counterparty"],
        ["product", "year"],
        ["counterparty", "year"],
        ["product_group", "year"],
    ):
        add(frame.groupby(keys)["revenue"].sum())
        add(frame.groupby(keys)["quantity"].sum())
    negative = frame[frame["revenue"] < 0]
    add(pd.Series([negative["revenue"].sum()]))
    add(negative.groupby("year")["revenue"].sum())
    for dim in ("product", "counterparty", "product_group"):
        pivot = frame.pivot_table(
            index=dim, columns="year", values="revenue", aggfunc="sum"
        ).fillna(0.0)
        if pivot.shape[1] >= 2:
            add(pivot.iloc[:, -1] - pivot.iloc[:, -2])
    return values


def test_no_private_base_markers() -> None:
    """Ни один отслеживаемый файл не содержит маркеров непубличной базы."""
    rules = forbidden()
    problems: list[str] = []
    for name in _tracked():
        if name in SKIP_FILES or Path(name).suffix not in TEXT:
            continue
        path = REPO_ROOT / name
        if not path.is_file():
            continue
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
        ):
            for label, pattern in rules.items():
                if pattern.search(line):
                    problems.append(f"{name}:{number}: {label}: {line.strip()[:100]}")
    assert problems == [], (
        "Маркеры непубличной базы (docs/PUBLIC-DATA-POLICY.md):\n" + "\n".join(problems)
    )


def test_public_money_is_reproducible() -> None:
    """Каждая денежная величина в публичных доках воспроизводима или обоснована."""
    extra: set[float] = set()
    if ALLOWLIST.is_file():
        data = json.loads(ALLOWLIST.read_text(encoding="utf-8"))
        extra = {round(abs(float(item["value"])), 2) for item in data["allowed"]}
    allowed = _synthetic() | extra
    problems: list[str] = []
    for name in _public():
        path = REPO_ROOT / name
        if not path.is_file():
            continue
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
        ):
            for pattern in (MONEY_RUB, MONEY_LABEL):
                for match in pattern.finditer(line):
                    value = _norm(match.group(1))
                    if value and value not in allowed:
                        problems.append(
                            f"{name}:{number}: {match.group(1).strip()} — "
                            "нет в синтетике/allowlist"
                        )
    assert problems == [], (
        "Бизнес-цифры без синтетического происхождения "
        "(docs/PUBLIC-DATA-POLICY.md):\n" + "\n".join(problems)
    )
