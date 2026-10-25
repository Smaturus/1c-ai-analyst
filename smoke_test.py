"""Смоук-тест MVP на синтетическом демо-датасете.

Запуск::

    python smoke_test.py

Скрипт не использует реальные выгрузки 1С/Excel: ``sample_data.csv``
генерируется ``generate_synthetic_data.py`` и содержит только вымышленные
данные. Проверяются схема витрины, контрольные суммы генератора, все 5
интентов, DSL-диспетчер и негативные сценарии.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

from analytics import (
    INTENT_REGISTRY,
    dispatch_intent,
    get_counterparty_dynamics,
    get_data_quality,
    get_group_dynamics,
    get_timeseries,
    get_top_bottom_products,
    get_total,
)
from generate_synthetic_data import (
    NEGATIVE_SHARE,
    ROWS,
    SAMPLE_CSV,
    generate_sample_data,
)
from onec_source import SAMPLE_COLUMNS, load_sample_vitrina

EXPECTED = generate_sample_data()
"""Эталон, сгенерированный в памяти с тем же семенем (600 строк)."""


def _check(condition: bool, message: str) -> None:
    """Мини-ассерт, чтобы не тянуть pytest в MVP."""
    if not condition:
        raise AssertionError(message)
    print(f"  OK  {message}")


def _ensure_sample_file() -> None:
    """Сгенерировать ``sample_data.csv``, если его нет в рабочей копии."""
    if not SAMPLE_CSV.is_file():
        print("  [info] sample_data.csv отсутствует — генерирую заново")
        generate_sample_data().to_csv(SAMPLE_CSV, index=False, encoding="utf-8")


def main() -> int:
    pd.set_option("display.width", 160)
    pd.set_option("display.max_columns", 20)
    _ensure_sample_file()

    print("=" * 100)
    print("1. СИНТЕТИЧЕСКИЙ ДАТАСЕТ sample_data.csv")
    print("=" * 100)
    vitrina = load_sample_vitrina(str(SAMPLE_CSV))
    print(vitrina.head(5).to_string(index=False))
    print()
    print(vitrina.dtypes.to_string())
    print()

    _check(list(vitrina.columns) == list(SAMPLE_COLUMNS), "схема колонок соответствует контракту")
    _check(len(vitrina) == ROWS, f"строк данных: {len(vitrina)} (ожидалось {ROWS})")
    _check(str(vitrina["date"].dtype).startswith("datetime64"), f"тип date: {vitrina['date'].dtype}")
    _check(int(vitrina["date"].isna().sum()) == 0, "в демо-данных нет строк без даты")
    _check(
        vitrina["date"].min().date().isoformat() == "2023-01-01"
        and vitrina["date"].max().date().year == 2024,
        f"период данных: {vitrina['date'].min():%d.%m.%Y} – {vitrina['date'].max():%d.%m.%Y}",
    )
    _check(
        abs(float(vitrina["revenue"].sum()) - float(EXPECTED["revenue"].sum())) < 0.01,
        f"выручка совпала с генератором: {vitrina['revenue'].sum():,.2f}",
    )
    _check(
        abs(float(vitrina["quantity"].sum()) - float(EXPECTED["quantity"].sum())) < 0.01,
        f"количество совпало с генератором: {vitrina['quantity'].sum():,.2f}",
    )
    _check(
        vitrina["product"].nunique() == EXPECTED["product"].nunique(),
        f"товаров: {vitrina['product'].nunique()}",
    )
    _check(
        vitrina["counterparty"].nunique() == EXPECTED["counterparty"].nunique(),
        f"контрагентов: {vitrina['counterparty'].nunique()}",
    )
    _check(
        vitrina["product_group"].nunique() == EXPECTED["product_group"].nunique(),
        f"товарных групп: {vitrina['product_group'].nunique()}",
    )
    negative_count = int((vitrina["revenue"] < 0).sum())
    expected_negative = round(ROWS * NEGATIVE_SHARE)
    _check(
        negative_count == expected_negative,
        f"возвратов: {negative_count} (ожидалось {expected_negative}, {NEGATIVE_SHARE:.0%})",
    )
    _check(
        (vitrina.loc[vitrina["revenue"] < 0, "quantity"] < 0).all(),
        "у возвратов отрицательны и количество, и выручка",
    )
    _check(
        (vitrina.loc[vitrina["revenue"] >= 0, "quantity"] > 0).all(),
        "у обычных строк количество положительно",
    )

    print()
    print("=" * 100)
    print("2. ИНТЕНТ 1: ТОП/АНТИТОП ТОВАРОВ")
    print("=" * 100)
    top = get_top_bottom_products(vitrina, metric="revenue", limit=5, ascending=False)
    bottom = get_top_bottom_products(vitrina, metric="revenue", limit=5, ascending=True)
    print(top.to_string(index=False))
    _check(len(top) == 5 and len(bottom) == 5, "limit соблюдён")
    _check(top["revenue"].is_monotonic_decreasing, "топ отсортирован по убыванию")
    _check(bottom["revenue"].is_monotonic_increasing, "антитоп отсортирован по возрастанию")
    _check(
        abs(
            float(get_top_bottom_products(vitrina, limit=10_000)["revenue"].sum())
            - float(vitrina["revenue"].sum())
        )
        < 0.01,
        "сумма всех товарных строк равна общей выручке",
    )

    print()
    print("=" * 100)
    print("3. ИНТЕНТ 3: ВРЕМЕННОЙ РЯД ПО МЕСЯЦАМ")
    print("=" * 100)
    series = get_timeseries(vitrina, freq="M")
    print(series.head(5).to_string(index=False))
    print("...")
    print(series.tail(3).to_string(index=False))
    _check(len(series) == 24, f"месяцев в ряду: {len(series)}")
    _check(
        abs(float(series["revenue"].sum()) - float(vitrina["revenue"].sum())) < 0.01,
        f"сумма ряда равна выручке витрины: {series['revenue'].sum():,.2f}",
    )
    _check(series["period"].is_monotonic_increasing, "ряд отсортирован по времени")
    quarterly = get_timeseries(vitrina, freq="Q", metric="quantity")
    _check(quarterly["period"].is_monotonic_increasing, "квартальный ряд в порядке")

    print()
    print("=" * 100)
    print("4. ИНТЕНТ 2: ДИНАМИКА КОНТРАГЕНТОВ (2023 -> 2024)")
    print("=" * 100)
    dynamics = get_counterparty_dynamics(
        vitrina,
        ("2023-01-01", "2023-12-31"),
        ("2024-01-01", "2024-12-31"),
        top_n=8,
    )
    print(dynamics.to_string(index=False))
    _check(not dynamics.empty, f"контрагентов в сравнении: {len(dynamics)}")
    _check(
        set(dynamics["direction"].astype(str)) <= {"up", "down", "flat", "new", "lost"},
        "direction размечен допустимыми значениями",
    )
    check_delta = (
        dynamics["period2_value"] - dynamics["period1_value"] - dynamics["delta"]
    ).abs().max()
    _check(check_delta < 1e-6, "delta согласована с разницей периодов")

    print()
    print("=" * 100)
    print("5. ИНТЕНТ 4: СРЕЗ ПО ТОВАРНЫМ ГРУППАМ (2023 H1 -> H2)")
    print("=" * 100)
    groups = get_group_dynamics(
        vitrina,
        ("2023-01-01", "2023-06-30"),
        ("2023-07-01", "2023-12-31"),
        only_drops=True,
        top_n=8,
    )
    print(groups.to_string(index=False))
    _check(
        groups.empty or (groups["delta"] < 0).all(),
        "only_drops оставил только падающие группы",
    )
    _check(
        groups.empty or set(groups.iloc[:, 0]).issubset(set(vitrina["product_group"])),
        "групповая динамика идёт по товарным группам витрины",
    )

    print()
    print("=" * 100)
    print("6. ИНТЕНТ 5: ОБЩАЯ СУММА ЗА ПЕРИОД")
    print("=" * 100)
    total_all = get_total(vitrina)
    total_2024 = get_total(vitrina, start_date="2024-01-01", end_date="2024-12-31")
    print(total_all.to_string(index=False))
    print()
    print(total_2024.to_string(index=False))
    _check(
        abs(float(total_all.iloc[0]["revenue"]) - float(vitrina["revenue"].sum())) < 0.01,
        "итог по всей витрине совпадает с суммой строк",
    )
    _check(
        float(total_2024.iloc[0]["revenue"]) < float(total_all.iloc[0]["revenue"]),
        "итог за 2024 меньше итога за всё время",
    )

    print()
    print("=" * 100)
    print("7. ДИАГНОСТИКА КАЧЕСТВА ДАННЫХ")
    print("=" * 100)
    print(get_data_quality(vitrina).to_string(index=False))

    print()
    print("=" * 100)
    print("8. ДИСПЕТЧЕР JSON-DSL")
    print("=" * 100)
    dsl_payloads = [
        {"intent": "total", "params": {"start_date": "2024-01-01", "end_date": "2024-12-31"}},
        {"intent": "top_bottom_products", "params": {"limit": 3, "metric": "revenue"}},
        {"intent": "timeseries", "params": {"freq": "Q"}},
        {
            "intent": "counterparty_dynamics",
            "params": {"period1": ["2023-01-01", "2023-12-31"], "period2": ["2024-01-01", "2024-12-31"]},
        },
        {
            "intent": "group_dynamics",
            "params": {"period1": ["2023-01-01", "2023-06-30"], "period2": ["2023-07-01", "2023-12-31"]},
        },
        {
            "intent": "compare_top_products",
            "params": {"period1": ["2023-01-01", "2023-12-31"], "period2": ["2024-01-01", "2024-12-31"], "top_n": 5},
        },
    ]
    for payload in dsl_payloads:
        result = dispatch_intent(payload["intent"], vitrina, payload["params"])
        print(f"  {payload['intent']:22s} -> {result.shape[0]} строк, колонки {list(result.columns)}")
    _check(
        set(INTENT_REGISTRY) == {p["intent"] for p in dsl_payloads},
        "все 6 интентов доступны через диспетчер",
    )
    try:
        dispatch_intent("unknown_intent", vitrina)
    except ValueError as exc:
        print(f"  OK  неизвестный интент: {exc}")
    else:
        raise AssertionError("не поднялась ошибка для неизвестного интента")
    try:
        dispatch_intent("total", vitrina, {"limit": 5})
    except ValueError as exc:
        print(f"  OK  лишний параметр: {exc}")
    else:
        raise AssertionError("не поднялась ошибка для лишнего параметра")

    print()
    print("=" * 100)
    print("9. НЕГАТИВНЫЕ ПРОВЕРКИ И РЕГРЕССИИ")
    print("=" * 100)
    for label, call in (
        ("неизвестная метрика", lambda: get_top_bottom_products(vitrina, metric="profit")),
        ("limit <= 0", lambda: get_top_bottom_products(vitrina, limit=0)),
        ("битая дата", lambda: get_timeseries(vitrina, start_date="вчера")),
        (
            "начало позже конца",
            lambda: get_counterparty_dynamics(
                vitrina, ("2024-12-31", "2024-01-01"), ("2024-01-01", "2024-01-31")
            ),
        ),
        ("неизвестная частота", lambda: get_timeseries(vitrina, freq="century")),
        (
            "нет колонки-группы",
            lambda: get_group_dynamics(
                vitrina,
                ("2024-01-01", "2024-01-31"),
                ("2024-02-01", "2024-02-29"),
                group_by="nope",
            ),
        ),
    ):
        try:
            call()
        except (ValueError, TypeError) as exc:
            print(f"  OK  {label}: {type(exc).__name__}: {exc}")
        else:
            raise AssertionError(f"не поднялась ошибка: {label}")

    empty = get_top_bottom_products(vitrina, start_date="2030-01-01", end_date="2030-12-31")
    _check(empty.empty, "пустой период даёт пустой DataFrame, а не исключение")

    all_groups = get_group_dynamics(
        vitrina, ("2023-01-01", "2023-06-30"), ("2023-07-01", "2023-12-31")
    )
    total_drops = int((all_groups["delta"] < 0).sum())
    limited = get_group_dynamics(
        vitrina,
        ("2023-01-01", "2023-06-30"),
        ("2023-07-01", "2023-12-31"),
        only_drops=True,
        top_n=1,
    )
    _check(
        len(limited) == min(1, total_drops) and (limited.empty or (limited["delta"] < 0).all()),
        f"only_drops применяется до top_n: {len(limited)} из {total_drops} падений",
    )

    negative = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-05", "2024-02-05"]),
            "product": pd.array(["p", "p"], dtype="string"),
            "counterparty": pd.array(["X", "X"], dtype="string"),
            "quantity": [-10.0, -5.0],
            "revenue": [-100.0, -50.0],
        }
    )
    negative_row = get_counterparty_dynamics(
        negative, ("2024-01-01", "2024-01-31"), ("2024-02-01", "2024-02-29")
    ).iloc[0]
    _check(
        negative_row["delta"] == 50.0
        and negative_row["direction"] == "up"
        and pd.isna(negative_row["delta_pct"]),
        "отрицательная база: delta=+50, direction=up, delta_pct=NaN",
    )

    counts = get_timeseries(vitrina, freq="M", agg="count")
    _check(
        int(counts["revenue"].sum()) == int(vitrina["date"].notna().sum()),
        f"agg='count' считает записи: {int(counts['revenue'].sum())}",
    )

    small = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-05", "2024-02-05"]),
            "product": pd.array(["A", "A"], dtype="string"),
            "counterparty": pd.array(["X", "X"], dtype="string"),
            "quantity": [1.0, 1.0],
            "revenue": [10.0, 20.0],
        }
    )
    permuted = pd.Series(["feb", "jan"], index=[1, 0])
    grouped = get_group_dynamics(
        small,
        ("2024-01-01", "2024-01-31"),
        ("2024-02-01", "2024-02-29"),
        group_by=permuted,
    )
    jan_value = float(grouped.set_index("product_group").loc["jan", "period1_value"])
    _check(
        jan_value == 10.0,
        f"Series групп выравнивается по индексу (январь у 'jan' = {jan_value})",
    )

    for label, call in (
        (
            "порт top: start>end",
            lambda: get_top_bottom_products(
                vitrina, start_date="2024-12-31", end_date="2024-01-01"
            ),
        ),
        (
            "порт ts: start>end",
            lambda: get_timeseries(vitrina, start_date="2024-12-31", end_date="2024-01-01"),
        ),
        (
            "порт total: start>end",
            lambda: get_total(vitrina, start_date="2024-12-31", end_date="2024-01-01"),
        ),
    ):
        try:
            call()
        except ValueError:
            print(f"  OK  {label} -> ValueError")
        else:
            raise AssertionError(label)

    print()
    print("=" * 100)
    print("10. СХЕМА LLM-СЛОЯ (docs/intent_schema.json)")
    print("=" * 100)
    schema_path = Path("docs") / "intent_schema.json"
    if not schema_path.is_file():
        print(f"  SKIP  нет {schema_path}")
    else:
        from analytics import INTENT_PARAMETERS

        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        _check(
            set(schema["intents"]) == set(INTENT_PARAMETERS),
            "набор интентов схемы LLM совпадает с analytics",
        )
        for name, info in schema["intents"].items():
            _check(
                set(info["params"]) == set(INTENT_PARAMETERS[name]),
                f"параметры интента {name} совпадают с analytics",
            )

    print()
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
