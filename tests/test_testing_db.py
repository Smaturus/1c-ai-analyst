"""Тестовые артефакты в analytics.db: программа, результаты, golden-кассеты."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from openpyxl import Workbook

from onec_analyst.analytics_db import refresh
from onec_analyst.testing_db import (
    import_testing,
    load_cassettes,
    load_program,
    load_results,
    stats,
)


def _make_program(path: Path) -> Path:
    book = Workbook()
    sheet = book.active
    sheet.title = "Набор тестов с очередностью"
    sheet.append(["Очередность тестов", "ID теста", "Текст теста", "Тест-предшественник", "Интент", "Примечание"])
    sheet.append([1, 1, "лучшие 2 товара 2023", 0, "top_bottom_products", "limit=2"])
    sheet.append([2, 2, "Сколько мы продали за 2024", 1, "total", "metric=revenue"])
    book.save(path)
    return path


def _make_results(path: Path) -> Path:
    book = Workbook()
    summary = book.active
    summary.title = "Сводка"
    summary.append(["Модель", "Кейсов", "Пройдено", "%", "Статус", "DSL", "Факты", "Ошибок", "p50, с"])
    summary.append(["deepseek-v4-flash", 57, 55, 96.5, 56, 54, 45, 2, 1.6])
    model = book.create_sheet("deepseek-v4-flash")
    model.append(
        [
            "order",
            "id",
            "question",
            "status",
            "expected_status",
            "intent",
            "expected_intent",
            "dsl_match",
            "fact_match",
            "verdict",
            "seconds",
            "today",
        ]
    )
    model.append([1, 1, "лучшие 2 товара 2023", "ok", "ok", "top_bottom_products", "top_bottom_products", True, None, True, 1.4, "2024-12-31"])
    model.append([2, 2, "Сколько мы продали", "ok", "ok", "total", "total", False, None, False, 2.1, "2024-12-31"])
    book.save(path)
    return path


def _make_cassette(path: Path, case_id: str) -> Path:
    payload = {
        "case_id": case_id,
        "source": "recorded",
        "recorded_at": "2026-11-18",
        "provider": "deepseek",
        "model": "deepseek-chat",
        "responses": [{"tool_call": {"name": "total", "arguments": {"start_date": "2024-01-01"}}}],
        "recorded_outcome": {"status": "ok", "intent": "total"},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture()
def artifacts(tmp_path: Path) -> dict[str, Path]:
    cassettes = tmp_path / "cassettes"
    cassettes.mkdir()
    return {
        "program": _make_program(tmp_path / "program.xlsx"),
        "results": _make_results(tmp_path / "results.xlsx"),
        "cassettes": cassettes,
        "cassette_1": _make_cassette(cassettes / "test_001.json", "test_001"),
        "cassette_2": _make_cassette(cassettes / "test_002.json", "test_002"),
        "db": tmp_path / "analytics.db",
    }


def _count(db: Path, table: str) -> int:
    connection = sqlite3.connect(db)
    try:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        connection.close()


def test_load_program(artifacts: dict[str, Path]) -> None:
    """Программа тестирования ложится в ``test_program`` с номером и интентом."""
    from onec_analyst.analytics_db import connect

    connection = connect(artifacts["db"])
    try:
        rows = load_program(connection, artifacts["program"])
        connection.commit()
        stored = connection.execute(
            "SELECT test_order, test_id, question, predecessor, intent, note FROM test_program ORDER BY test_order"
        ).fetchall()
    finally:
        connection.close()

    assert rows == 2
    assert stored[0] == (1, "1", "лучшие 2 товара 2023", "0", "top_bottom_products", "limit=2")
    assert stored[1][4] == "total"


def test_load_results_maps_models_and_booleans(artifacts: dict[str, Path]) -> None:
    """Сводка и результаты: модель из имени листа, True/False → 1/0."""
    from onec_analyst.analytics_db import connect

    connection = connect(artifacts["db"])
    try:
        summary_rows, result_rows = load_results(connection, artifacts["results"])
        connection.commit()
        summary = connection.execute(
            "SELECT model, cases, passed, percent, errors, p50_sec FROM test_summary"
        ).fetchone()
        results = connection.execute(
            "SELECT model, test_id, dsl_match, verdict, seconds FROM test_results ORDER BY test_id"
        ).fetchall()
    finally:
        connection.close()

    assert (summary_rows, result_rows) == (1, 2)
    assert summary == ("deepseek-v4-flash", 57, 55, 96.5, 2, 1.6)
    assert results[0] == ("deepseek-v4-flash", "1", 1, 1, 1.4)
    assert results[1][2] == 0 and results[1][3] == 0


def test_load_cassettes_keeps_raw_payload(artifacts: dict[str, Path]) -> None:
    """Кассета: разобранные поля + сырой JSON + sha256."""
    from onec_analyst.analytics_db import connect

    connection = connect(artifacts["db"])
    try:
        rows = load_cassettes(
            connection, [artifacts["cassette_1"], artifacts["cassette_2"]]
        )
        connection.commit()
        stored = connection.execute(
            "SELECT case_id, provider, model, payload, sha256 FROM golden_cassettes ORDER BY case_id"
        ).fetchall()
    finally:
        connection.close()

    assert rows == 2
    assert stored[0][0] == "test_001"
    assert stored[0][1] == "deepseek"
    assert json.loads(stored[0][3])["recorded_outcome"]["intent"] == "total"
    assert len(stored[0][4]) == 64


def test_import_is_idempotent(artifacts: dict[str, Path]) -> None:
    """Повторный импорт перезаписывает, а не дублирует."""
    import_testing(
        program_xlsx=artifacts["program"],
        results_xlsx=artifacts["results"],
        cassettes_dir=artifacts["cassettes"],
        db_path=artifacts["db"],
    )
    import_testing(
        program_xlsx=artifacts["program"],
        results_xlsx=artifacts["results"],
        cassettes_dir=artifacts["cassettes"],
        db_path=artifacts["db"],
    )
    assert _count(artifacts["db"], "test_program") == 2
    assert _count(artifacts["db"], "test_results") == 2
    assert _count(artifacts["db"], "golden_cassettes") == 2


def test_refresh_of_onec_data_keeps_test_artifacts(artifacts: dict[str, Path], tmp_path: Path) -> None:
    """Перезагрузка выгрузки 1С не стирает тестовые таблицы."""
    import_testing(program_xlsx=artifacts["program"], db_path=artifacts["db"])

    nomenclature = tmp_path / "n.csv"
    nomenclature.write_text(
        "name,parent,is_group,product_group,product_group_path\nP1,,0,G1,Group\n",
        encoding="utf-8",
    )
    sales = tmp_path / "s.csv"
    sales.write_text(
        "date,product,product_group,product_group_path,counterparty,quantity,"
        "revenue_net,revenue_gross,operation,registrar,organization\n"
        "2024-01-01,P1,G1,Group,A,1,100.0,120.0,Реализация,R,ООО\n",
        encoding="utf-8",
    )
    refresh(nomenclature_csv=nomenclature, sales_csv=sales, db_path=artifacts["db"])

    assert _count(artifacts["db"], "sales") == 1
    assert _count(artifacts["db"], "test_program") == 2


def test_missing_files_raise(artifacts: dict[str, Path]) -> None:
    """Отсутствующие файлы — понятная ошибка."""
    with pytest.raises(FileNotFoundError):
        import_testing(program_xlsx=artifacts["cassettes"] / "nope.xlsx", db_path=artifacts["db"])
    with pytest.raises(FileNotFoundError):
        import_testing(results_xlsx=artifacts["cassettes"] / "nope.xlsx", db_path=artifacts["db"])


def test_invalid_cassette_raises(artifacts: dict[str, Path]) -> None:
    """Битый JSON не должен молча попасть в архив."""
    bad = artifacts["cassettes"] / "test_bad.json"
    bad.write_text("{не json", encoding="utf-8")
    with pytest.raises(ValueError):
        import_testing(cassettes=[bad], db_path=artifacts["db"])


def test_stats_lists_models(artifacts: dict[str, Path]) -> None:
    """``--stats`` показывает счётчики и список моделей."""
    import_testing(
        program_xlsx=artifacts["program"],
        results_xlsx=artifacts["results"],
        cassettes_dir=artifacts["cassettes"],
        db_path=artifacts["db"],
    )
    report = stats(artifacts["db"])
    assert report["counts"] == {
        "test_program": 2,
        "test_results": 2,
        "test_summary": 1,
        "golden_cassettes": 2,
    }
    assert report["models"] == ["deepseek-v4-flash"]
