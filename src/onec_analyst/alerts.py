"""Правило ежедневного алерта по возвратам (BS-01, задача T8).

Алерт строится только на движке: сумма отрицательной выручки — это сумма строк
среза ``negative_revenue`` за окно, выручка дня — итог среза ``total``.
Порог и минимальное число строк настраиваются; калибровка на истории показывает,
сколько раз правило сработало бы за период.

Окно оповещения:

* вторник–пятница — предыдущий день;
* понедельник — пятница, суббота и воскресенье (иначе пятничные возвраты
  не попадали бы ни в одно окно);
* суббота и воскресенье — алерты не отправляются.

Запуск калибровки::

    python -X utf8 -m onec_analyst.alerts --days 365
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from onec_analyst.analytics import dispatch_intent
from onec_analyst.paths import find_repo_root

__all__ = [
    "ALERTS_ENABLED_ENV",
    "AlertPolicy",
    "CalibrationReport",
    "ReturnsStats",
    "ReturnsWindow",
    "STATE_DIR_ENV",
    "alert_flag_path",
    "alert_reason",
    "alert_status",
    "alert_window",
    "alerts_enabled",
    "calibrate",
    "collect_returns",
    "main",
    "set_alerts_enabled",
]

MIN_ABS_ENV: str = "ONEC_ALERT_MIN_ABS"
MIN_ROWS_ENV: str = "ONEC_ALERT_MIN_ROWS"
SHARE_ENV: str = "ONEC_ALERT_SHARE_PCT"
STATE_DIR_ENV: str = "ONEC_MCP_STATE_DIR"
ALERTS_ENABLED_ENV: str = "ONEC_ALERTS_ENABLED"
DISABLED_FLAG_NAME: str = "alerts.disabled"

DEFAULT_MIN_ABS: float = 50_000.0
"""Стартовый порог, ₽. Калибруется на истории реальной базы, а не берётся на веру."""
DEFAULT_MIN_ROWS: int = 1

Dispatch = Callable[[str, pd.DataFrame, dict[str, Any] | None], pd.DataFrame]


@dataclass(frozen=True)
class AlertPolicy:
    """Порог срабатывания ежедневного алерта."""

    min_abs: float = DEFAULT_MIN_ABS
    min_rows: int = DEFAULT_MIN_ROWS
    share_pct: float | None = None

    @classmethod
    def from_env(
        cls,
        *,
        min_abs: float | None = None,
        min_rows: int | None = None,
        share_pct: float | None = None,
    ) -> "AlertPolicy":
        """Политика из окружения с явными переопределениями."""
        base = cls(
            min_abs=_env_float(MIN_ABS_ENV, DEFAULT_MIN_ABS),
            min_rows=_env_int(MIN_ROWS_ENV, DEFAULT_MIN_ROWS),
            share_pct=_env_float(SHARE_ENV, None),
        )
        return replace(
            base,
            min_abs=min_abs if min_abs is not None else base.min_abs,
            min_rows=min_rows if min_rows is not None else base.min_rows,
            share_pct=share_pct if share_pct is not None else base.share_pct,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "min_abs": self.min_abs,
            "min_rows": self.min_rows,
            "share_pct": self.share_pct,
        }


@dataclass(frozen=True)
class ReturnsWindow:
    """Окно, за которое считается сводка возвратов."""

    start: date
    end: date
    label: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "label": self.label,
        }


@dataclass(frozen=True)
class ReturnsStats:
    """Факты по окну: сумма возвратов, строки и доля в выручке."""

    window: ReturnsWindow
    rows: int
    negative_total: float
    revenue: float
    share_pct: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "window": self.window.as_dict(),
            "rows": self.rows,
            "negative_total": round(self.negative_total, 2),
            "revenue": round(self.revenue, 2),
            "share_pct": round(self.share_pct, 2),
        }


@dataclass(frozen=True)
class CalibrationReport:
    """Сколько раз правило сработало бы на истории."""

    days: int
    checked_days: int
    alert_days: int
    alerts_per_month: float
    max_abs: float
    p95_abs: float
    recommended_min_abs: float
    recommended_alert_days: int
    target_per_month: float
    policy: AlertPolicy

    def as_dict(self) -> dict[str, Any]:
        return {
            "days": self.days,
            "checked_days": self.checked_days,
            "alert_days": self.alert_days,
            "alerts_per_month": round(self.alerts_per_month, 2),
            "max_abs": round(self.max_abs, 2),
            "p95_abs": round(self.p95_abs, 2),
            "recommended_min_abs": round(self.recommended_min_abs, 2),
            "recommended_alert_days": self.recommended_alert_days,
            "target_per_month": self.target_per_month,
            "policy": self.policy.as_dict(),
        }


def alert_window(today: date) -> ReturnsWindow | None:
    """Окно для ежедневного алерта; ``None`` — сегодня алерт не отправляется."""
    weekday = today.weekday()
    if weekday in (5, 6):  # суббота, воскресенье
        return None
    if weekday == 0:  # понедельник — сводка за пятницу и выходные
        return ReturnsWindow(
            start=today - timedelta(days=3),
            end=today - timedelta(days=1),
            label="за пятницу–воскресенье",
        )
    day = today - timedelta(days=1)
    return ReturnsWindow(start=day, end=day)


def state_dir() -> Path:
    """Каталог служебного состояния MCP (по умолчанию ``.mcp_state``)."""
    raw = os.environ.get(STATE_DIR_ENV, "").strip()
    return Path(raw) if raw else find_repo_root() / ".mcp_state"


def alert_flag_path() -> Path:
    """Файл-выключатель: его наличие означает «алерты отключены»."""
    return state_dir() / DISABLED_FLAG_NAME


def alerts_enabled() -> bool:
    """Включены ли алерты.

    Выключить можно двумя способами: переменной ``ONEC_ALERTS_ENABLED=0``
    (или ``false/no/off``) либо файлом-флагом. Любой из них означает «выключено»;
    включение — явное ``set_alerts_enabled(True)`` или ``ONEC_ALERTS_ENABLED=1``
    без файла-флага.
    """
    raw = os.environ.get(ALERTS_ENABLED_ENV)
    if raw is not None and raw.strip().lower() in {"0", "false", "no", "off"}:
        return False
    return not alert_flag_path().exists()


def set_alerts_enabled(enabled: bool) -> Path | None:
    """Включить или выключить алерты; возвращает путь к флагу при выключении."""
    path = alert_flag_path()
    if enabled:
        path.unlink(missing_ok=True)
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "Алерты возвратов отключены администратором.\n"
        "Включить: python -X utf8 -m onec_analyst.alerts --enable\n",
        encoding="utf-8",
    )
    return path


def alert_status() -> dict[str, Any]:
    """Текущее состояние выключателя — для runbook и CLI."""
    flag = alert_flag_path()
    return {
        "enabled": alerts_enabled(),
        "env": os.environ.get(ALERTS_ENABLED_ENV),
        "flag_path": str(flag),
        "flag_present": flag.exists(),
    }


def collect_returns(
    frame: pd.DataFrame,
    window: ReturnsWindow,
    *,
    dispatch: Dispatch = dispatch_intent,
) -> ReturnsStats:
    """Собрать факты по окну из движка, без собственной арифметики."""
    negative = dispatch(
        "negative_revenue",
        frame,
        {
            "start_date": window.start,
            "end_date": window.end,
            "group_by": "product",
        },
    )
    total = dispatch(
        "total",
        frame,
        {"start_date": window.start, "end_date": window.end},
    )
    negative_total = float(-negative["revenue"].sum()) if not negative.empty else 0.0
    revenue = float(total["revenue"].iloc[0]) if not total.empty else 0.0
    share = (negative_total / revenue * 100.0) if revenue else 0.0
    return ReturnsStats(
        window=window,
        rows=int(len(negative)),
        negative_total=negative_total,
        revenue=revenue,
        share_pct=share,
    )


def alert_reason(stats: ReturnsStats, policy: AlertPolicy) -> str | None:
    """Причина срабатывания или ``None``; тексты — для человека."""
    if stats.rows < policy.min_rows:
        return None
    if stats.negative_total < policy.min_abs:
        return None
    if policy.share_pct is not None and stats.share_pct < policy.share_pct:
        return None
    return (
        f"сумма возвратов {stats.negative_total:,.0f} ₽ не ниже порога "
        f"{policy.min_abs:,.0f} ₽ (строк: {stats.rows}, доля {stats.share_pct:.2f} %)"
    ).replace(",", " ")


def calibrate(
    frame: pd.DataFrame,
    *,
    days: int = 365,
    policy: AlertPolicy | None = None,
    today: date | None = None,
    target_per_month: float = 2.0,
    dispatch: Dispatch = dispatch_intent,
) -> CalibrationReport:
    """Прогнать правило по истории: сколько алертов было бы за ``days`` дней.

    ``target_per_month`` — желаемая частота алертов: по ней подбирается
    рекомендуемый порог (значение на нужном месте в распределении), а не
    «красивый» процент. Так правило не молчит и не шумит.
    """
    policy = policy or AlertPolicy.from_env()
    anchor = today or date.today()
    checked = 0
    alerts = 0
    values: list[float] = []
    for offset in range(days):
        day = anchor - timedelta(days=offset)
        window = alert_window(day)
        if window is None:
            continue
        stats = collect_returns(frame, window, dispatch=dispatch)
        checked += 1
        if stats.rows:
            values.append(stats.negative_total)
        if alert_reason(stats, policy) is not None:
            alerts += 1
    p95 = _quantile(values, 0.95)
    ordered = sorted(values, reverse=True)
    target_days = max(1, round(target_per_month * days / 30.0))
    if ordered:
        recommended = ordered[min(len(ordered) - 1, target_days - 1)]
        recommended_alerts = sum(1 for value in values if value >= recommended)
    else:
        recommended = policy.min_abs
        recommended_alerts = 0
    return CalibrationReport(
        days=days,
        checked_days=checked,
        alert_days=alerts,
        alerts_per_month=(alerts / days * 30.0) if days else 0.0,
        max_abs=max(values) if values else 0.0,
        p95_abs=p95,
        recommended_min_abs=recommended,
        recommended_alert_days=recommended_alerts,
        target_per_month=target_per_month,
        policy=policy,
    )


def _quantile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(quantile * (len(ordered) - 1))))
    return ordered[index]


def _env_float(name: str, default: float | None) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    value = _env_float(name, float(default))
    return int(value) if value is not None else default


def _load_dataset(path: str | None) -> pd.DataFrame:
    from onec_analyst.onec_source import load_sample_vitrina, load_vitrina
    from onec_analyst.paths import find_repo_root

    target = Path(path) if path else find_repo_root() / "sample_data.csv"
    with target.open("r", encoding="utf-8-sig", newline="") as handle:
        header = handle.readline()
    if "revenue_net" in header:
        return load_vitrina(str(target))
    return load_sample_vitrina(str(target))


def main(argv: list[str] | None = None) -> int:
    """Калибровка правила на витрине: сколько алертов было бы за период."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=365, help="окно истории, дней")
    parser.add_argument("--today", help="дата отсчёта, ГГГГ-ММ-ДД (по умолчанию сегодня)")
    parser.add_argument("--dataset", help="путь к CSV витрины (по умолчанию sample_data.csv)")
    parser.add_argument(
        "--target-per-month",
        type=float,
        default=2.0,
        help="желаемая частота алертов в месяц для подбора порога",
    )
    parser.add_argument("--json", action="store_true", help="вывести отчёт в JSON")
    parser.add_argument("--disable", action="store_true", help="отключить алерты (файл-флаг)")
    parser.add_argument("--enable", action="store_true", help="включить алерты")
    parser.add_argument("--status", action="store_true", help="показать состояние выключателя")
    args = parser.parse_args(argv)

    if args.status:
        print(json.dumps(alert_status(), ensure_ascii=False, indent=2))
        return 0
    if args.disable:
        flag = set_alerts_enabled(False)
        print(f"Алерты отключены: {flag}")
        return 0
    if args.enable:
        set_alerts_enabled(True)
        print("Алерты включены")
        return 0

    frame = _load_dataset(args.dataset)
    today = date.fromisoformat(args.today) if args.today else None
    report = calibrate(
        frame,
        days=args.days,
        today=today,
        target_per_month=args.target_per_month,
    )
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        data = report.as_dict()
        print(f"Окно: {data['days']} дней, проверено {data['checked_days']} дней")
        print(
            f"При текущем пороге: алертов {data['alert_days']} "
            f"(~{data['alerts_per_month']}/мес)"
        )
        print(f"Максимум возвратов за день: {data['max_abs']:.0f} ₽")
        print(f"p95: {data['p95_abs']:.0f} ₽")
        print(
            f"Порог под {data['target_per_month']}/мес: "
            f"{data['recommended_min_abs']:.0f} ₽ → алертов {data['recommended_alert_days']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
