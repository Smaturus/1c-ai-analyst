"""Тесты журнала MCP: аудит вызовов, отказы и устойчивость к сбою записи."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from onec_analyst.audit import (
    AUDIT_FILE_ENV,
    AuditLog,
    read_audit,
    summarize_audit,
    to_jsonable,
)
from onec_analyst.mcp_server import load_dataset, total


def test_record_writes_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)

    log.record("slice", intent="total", params={"start_date": date(2024, 1, 1)}, rows=1)

    records = read_audit(path)
    assert len(records) == 1
    record = records[0]
    assert record["event"] == "slice"
    assert record["intent"] == "total"
    assert record["params"]["start_date"] == "2024-01-01"
    assert datetime.fromisoformat(record["ts"])
    json.dumps(record)  # запись обязана быть JSON-безопасной


def test_to_jsonable_converts_dates_and_tuples() -> None:
    value = to_jsonable(
        {
            "period1": (date(2023, 1, 1), date(2023, 12, 31)),
            "flag": True,
            "none": None,
        }
    )

    assert value == {
        "period1": ["2023-01-01", "2023-12-31"],
        "flag": True,
        "none": None,
    }


def test_summarize_counts_refusals_and_drops_old_records(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    now = datetime.now().astimezone()
    records = [
        {"ts": now.isoformat(timespec="seconds"), "event": "slice"},
        {"ts": now.isoformat(timespec="seconds"), "event": "refusal", "reason": "маржа"},
        {"ts": now.isoformat(timespec="seconds"), "event": "refusal", "reason": "маржа"},
        {
            "ts": (now - timedelta(days=60)).isoformat(timespec="seconds"),
            "event": "refusal",
            "reason": "старое",
        },
    ]
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )

    summary = summarize_audit(path, days=30)

    assert summary["slices"] == 1
    assert summary["refusals"] == 2
    assert summary["refusal_rate"] == pytest.approx(2 / 3, abs=0.001)
    assert summary["top_refusal_reasons"][0] == ("маржа", 2)


def test_tool_call_is_audited(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv(AUDIT_FILE_ENV, str(path))

    text = total(start_date=date(2024, 1, 1), end_date=date(2024, 12, 31))

    assert "52 253 500,00" in text
    records = [record for record in read_audit(path) if record["event"] == "slice"]
    assert records
    record = records[-1]
    assert record["intent"] == "total"
    assert record["params"] == {"start_date": "2024-01-01", "end_date": "2024-12-31"}
    assert record["rows"] == 1
    assert record["period"] == ["2024-01-01", "2024-12-31"]
    assert record["period_specified"] is True


def test_refusal_is_audited(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv(AUDIT_FILE_ENV, str(path))

    load_dataset(path="C:/Windows/win.ini")

    refusals = [record for record in read_audit(path) if record["event"] == "refusal"]
    assert refusals
    assert "вне каталога" in refusals[-1]["reason"]


def test_audit_failure_does_not_break_tool(monkeypatch, tmp_path: Path) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv(AUDIT_FILE_ENV, str(blocker / "audit.jsonl"))

    text = total()

    assert "Период:" in text
