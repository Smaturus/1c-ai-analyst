"""Тесты правила ежедневного алерта по возвратам (T8)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from onec_analyst.alerts import (
    MIN_ABS_ENV,
    MIN_ROWS_ENV,
    AlertPolicy,
    ReturnsStats,
    ReturnsWindow,
    alert_reason,
    alert_window,
    calibrate,
    collect_returns,
)
from onec_analyst.mcp_server import returns_alert

WEEKDAY = date(2024, 1, 10)
MONDAY = date(2024, 1, 8)
SATURDAY = date(2024, 1, 13)
SUNDAY = date(2024, 1, 14)


def _frame(rows: list[tuple[str, str, float]]) -> pd.DataFrame:
    """Витрина из строк ``(дата, товар, выручка)``; остальные колонки — заглушки."""
    frame = pd.DataFrame(
        [
            {
                "date": pd.Timestamp(day),
                "product": product,
                "counterparty": "C",
                "quantity": 1,
                "revenue": revenue,
                "product_group": "G",
            }
            for day, product, revenue in rows
        ]
    )
    return frame


def test_window_for_weekday_is_previous_day() -> None:
    window = alert_window(WEEKDAY)

    assert window == ReturnsWindow(start=date(2024, 1, 9), end=date(2024, 1, 9))


def test_window_for_monday_covers_weekend() -> None:
    window = alert_window(MONDAY)

    assert window is not None
    assert window.start == date(2024, 1, 6)
    assert window.end == date(2024, 1, 7)
    assert window.label == "за выходные"


def test_no_alert_on_weekend() -> None:
    assert alert_window(SATURDAY) is None
    assert alert_window(SUNDAY) is None


def test_policy_from_env_and_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MIN_ABS_ENV, "12345")
    monkeypatch.setenv(MIN_ROWS_ENV, "3")

    policy = AlertPolicy.from_env()

    assert policy.min_abs == 12345.0
    assert policy.min_rows == 3
    assert AlertPolicy.from_env(min_abs=999).min_abs == 999


def test_collect_returns_uses_engine() -> None:
    frame = _frame(
        [
            ("2024-01-09", "A", 1000.0),
            ("2024-01-09", "B", -300.0),
            ("2024-01-10", "A", 2000.0),
        ]
    )
    window = ReturnsWindow(start=date(2024, 1, 9), end=date(2024, 1, 9))

    stats = collect_returns(frame, window)

    assert stats.rows == 1
    assert stats.negative_total == pytest.approx(300.0)
    assert stats.revenue == pytest.approx(700.0)
    assert stats.share_pct == pytest.approx(300 / 700 * 100)


def test_alert_reason_respects_thresholds() -> None:
    window = ReturnsWindow(start=date(2024, 1, 9), end=date(2024, 1, 9))
    stats = ReturnsStats(
        window=window,
        rows=2,
        negative_total=60_000.0,
        revenue=100_000.0,
        share_pct=60.0,
    )

    assert alert_reason(stats, AlertPolicy(min_abs=50_000.0)) is not None
    assert alert_reason(stats, AlertPolicy(min_abs=70_000.0)) is None
    assert alert_reason(stats, AlertPolicy(min_abs=50_000.0, min_rows=3)) is None
    assert alert_reason(stats, AlertPolicy(min_abs=50_000.0, share_pct=70.0)) is None
    assert alert_reason(stats, AlertPolicy(min_abs=50_000.0, share_pct=50.0)) is not None


def test_calibrate_counts_alerts_on_history() -> None:
    frame = _frame(
        [
            ("2023-12-31", "A", 1000.0),
            ("2023-12-31", "B", -10.0),
            ("2024-01-01", "A", 1000.0),
            ("2024-01-01", "B", -50.0),
            ("2024-01-02", "A", 1000.0),
            ("2024-01-02", "B", -200.0),
            ("2024-01-03", "A", 1000.0),
            ("2024-01-04", "A", 1000.0),
            ("2024-01-04", "B", -100.0),
            ("2024-01-05", "A", 1000.0),
            ("2024-01-05", "B", -300.0),
        ]
    )

    report = calibrate(
        frame,
        days=6,
        policy=AlertPolicy(min_abs=100.0),
        today=date(2024, 1, 6),
    )

    assert report.checked_days == 5  # пн–пт; суббота и воскресенье пропущены
    assert report.alert_days == 2  # окно 4 января (100) и 2 января (200)
    assert report.max_abs == pytest.approx(200.0)
    assert report.recommended_min_abs == pytest.approx(200.0)
    assert report.recommended_alert_days == 1


def test_returns_alert_tool_reports_breach() -> None:
    text = returns_alert(today=date(2023, 2, 16))

    assert "Период: 15.02.2023–15.02.2023" in text
    assert "Данные:" in text
    assert "⚠️ Алерт" in text
    assert "350 000,00" in text


def test_returns_alert_tool_respects_threshold() -> None:
    text = returns_alert(today=date(2023, 2, 16), min_abs=400_000.0)

    assert "Превышения нет" in text


def test_returns_alert_monday_uses_weekend_window() -> None:
    text = returns_alert(today=date(2023, 2, 20))

    assert "Период: 18.02.2023–19.02.2023" in text
    assert "за выходные" in text


def test_returns_alert_is_silent_on_weekend() -> None:
    text = returns_alert(today=date(2024, 12, 29))

    assert "выходной" in text
    assert "Период:" in text
    assert "Данные:" in text
