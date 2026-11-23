"""Тесты метаданных выгрузки: дата выгрузки, а не дата последней продажи."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from onec_analyst.onec_source import (
    EXPORT_META_SUFFIX,
    load_sample_vitrina,
    load_vitrina,
    meta_path_for,
    parse_exported_at,
    read_export_meta,
    write_export_meta,
)

SAMPLE_HEADER = "date,product,counterparty,quantity,revenue,product_group\n"
SAMPLE_ROW = "2024-01-15,Товар A,ООО Ромашка,2,1000.0,Группа\n"

EXPORT_HEADER = (
    "date,product,product_group,product_group_path,counterparty,quantity,"
    "revenue_net,revenue_gross,operation,registrar,organization\n"
)
EXPORT_ROW = (
    "2024-01-15,Товар A,Группа,Группа,ООО Ромашка,2,1000.0,1200.0,"
    "Продажа,Документ,Организация\n"
)


def _sample_csv(tmp_path: Path) -> Path:
    path = tmp_path / "sample_data.csv"
    path.write_text(SAMPLE_HEADER + SAMPLE_ROW, encoding="utf-8")
    return path


def _export_csv(tmp_path: Path) -> Path:
    path = tmp_path / "sales_1c.csv"
    path.write_text(EXPORT_HEADER + EXPORT_ROW, encoding="utf-8")
    return path


def test_meta_path_for_appends_suffix(tmp_path: Path) -> None:
    expected = f"sales_1c.csv{EXPORT_META_SUFFIX}"

    assert meta_path_for(tmp_path / "sales_1c.csv").name == expected


def test_write_and_read_roundtrip(tmp_path: Path) -> None:
    csv_path = _export_csv(tmp_path)
    moment = datetime(2026, 11, 12, 7, 5, 30)

    meta_file = write_export_meta(
        csv_path,
        rows=1,
        exported_at=moment,
        extra={"config": {"synonym": "УТ"}},
    )

    assert meta_file.is_file()
    payload = read_export_meta(csv_path)
    assert payload is not None
    assert payload["exported_at"] == moment.isoformat(timespec="seconds")
    assert payload["rows"] == 1
    assert payload["csv"] == "sales_1c.csv"
    assert payload["config"] == {"synonym": "УТ"}


def test_load_sample_vitrina_uses_export_meta(tmp_path: Path) -> None:
    csv_path = _sample_csv(tmp_path)
    moment = datetime(2026, 11, 12, 7, 5, 30)
    write_export_meta(csv_path, rows=1, source="synthetic", exported_at=moment)

    df = load_sample_vitrina(str(csv_path))

    assert df.attrs["exported_at_source"] == "meta"
    assert df.attrs["exported_at"] == moment.isoformat(timespec="seconds")


def test_load_sample_vitrina_falls_back_to_file_mtime(tmp_path: Path) -> None:
    df = load_sample_vitrina(str(_sample_csv(tmp_path)))

    assert df.attrs["exported_at_source"] == "file_mtime"
    assert isinstance(df.attrs["exported_at"], str)
    assert isinstance(parse_exported_at(df.attrs["exported_at"]), datetime)


def test_load_vitrina_uses_export_meta(tmp_path: Path) -> None:
    csv_path = _export_csv(tmp_path)
    moment = datetime(2026, 11, 12, 8, 15)
    write_export_meta(csv_path, rows=1, revenue_mode="net", exported_at=moment)

    df = load_vitrina(str(csv_path))

    assert df.attrs["revenue_mode"] == "net"
    assert df.attrs["exported_at_source"] == "meta"
    assert df.attrs["exported_at"] == moment.isoformat(timespec="seconds")


def test_attrs_are_json_serializable(tmp_path: Path) -> None:
    # Streamlit сериализует DataFrame.attrs; datetime там недопустим (attrs теряются).
    df = load_sample_vitrina(str(_sample_csv(tmp_path)))

    assert json.loads(json.dumps(df.attrs))["exported_at"] == df.attrs["exported_at"]


def test_parse_exported_at_handles_empty_and_broken_values() -> None:
    assert parse_exported_at(None) is None
    assert parse_exported_at("") is None
    assert parse_exported_at("не дата") is None
    assert parse_exported_at("2026-11-12T07:05:30") == datetime(2026, 11, 12, 7, 5, 30)


def test_corrupt_meta_falls_back_to_file_mtime(tmp_path: Path) -> None:
    csv_path = _sample_csv(tmp_path)
    meta_path_for(csv_path).write_text("{не json", encoding="utf-8")

    df = load_sample_vitrina(str(csv_path))

    assert read_export_meta(csv_path) is None
    assert df.attrs["exported_at_source"] == "file_mtime"


def test_meta_without_exported_at_falls_back(tmp_path: Path) -> None:
    csv_path = _sample_csv(tmp_path)
    meta_path_for(csv_path).write_text(json.dumps({"rows": 1}), encoding="utf-8")

    df = load_sample_vitrina(str(csv_path))

    assert df.attrs["exported_at_source"] == "file_mtime"
