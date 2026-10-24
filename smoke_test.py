"""Смоук-тест MVP на реальной выгрузке ``тест.xlsx``.

Запуск::

    python smoke_test.py

Скрипт проверяет контракт загрузчика (схема, контрольные суммы, обработку
``Итого``), прогоняет все 5 интентов и печатает результаты так, как их увидит
пользователь в Streamlit.
"""

from __future__ import annotations

import json
import sys
import warnings
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
from data_loader import OUTPUT_COLUMNS, load_and_clean_1c_report

REPORT_PATH = "тест.xlsx"

# Контрольные суммы из строки «Итого» исходного файла: строки данных должны
# давать ровно те же значения, иначе «Итого» не удалено или удалено дважды.
EXPECTED_ROWS = 821
EXPECTED_REVENUE_NET = 19_313_936.63
EXPECTED_QUANTITY = 10_878.0
EXPECTED_PRODUCTS = 96
EXPECTED_COUNTERPARTIES = 40
EXPECTED_UNDATED_ROWS = 250


def _check(condition: bool, message: str) -> None:
    """Мини-ассерт, чтобы не тянуть pytest в MVP."""
    if not condition:
        raise AssertionError(message)
    print(f"  OK  {message}")


def main() -> int:
    pd.set_option("display.width", 160)
    pd.set_option("display.max_columns", 20)

    print("=" * 100)
    print("1. ЗАГРУЗКА И ОЧИСТКА")
    print("=" * 100)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        df = load_and_clean_1c_report(REPORT_PATH)
    for item in caught:
        print(f"  [warning] {item.message}")
    print()
    print(df.head(5).to_string())
    print()
    print(df.dtypes.to_string())
    print()

    _check(list(df.columns) == list(OUTPUT_COLUMNS), "схема колонок соответствует контракту")
    _check(len(df) == EXPECTED_ROWS, f"строк данных: {len(df)} (ожидалось {EXPECTED_ROWS})")
    _check(
        abs(float(df["revenue"].sum()) - EXPECTED_REVENUE_NET) < 0.01,
        f"выручка без НДС совпала с «Итого»: {df['revenue'].sum():,.2f}",
    )
    _check(
        abs(float(df["quantity"].sum()) - EXPECTED_QUANTITY) < 0.01,
        f"количество совпало с «Итого»: {df['quantity'].sum():,.2f}",
    )
    _check(df["product"].nunique() == EXPECTED_PRODUCTS, f"товаров: {df['product'].nunique()}")
    _check(
        df["counterparty"].nunique() == EXPECTED_COUNTERPARTIES,
        f"контрагентов: {df['counterparty'].nunique()}",
    )
    _check(
        int(df["date"].isna().sum()) == EXPECTED_UNDATED_ROWS,
        f"строк без даты: {int(df['date'].isna().sum())}",
    )
    _check(
        str(df["date"].dtype).startswith("datetime64"),
        f"тип date: {df['date'].dtype}",
    )
    _check(
        (df["revenue"] < 0).any(),
        f"отрицательные суммы сохранены (строк: {int((df['revenue'] < 0).sum())})",
    )
    _check(
        not df["product"].astype(str).str.contains(";").any(),
        "в product не осталось склеенных значений",
    )
    _check(
        not df["counterparty"].astype(str).str.contains(";").any(),
        "в counterparty не осталось склеенных значений",
    )
    _check("Итого" not in set(df["product"]), "строка «Итого» удалена")

    print()
    print("=" * 100)
    print("2. ИНТЕНТ 1: ТОП/АНТИТОП ТОВАРОВ (за весь период)")
    print("=" * 100)
    top = get_top_bottom_products(df, metric="revenue", limit=5, ascending=False)
    print("Лучшие 5 по выручке:")
    print(top.to_string(index=False))
    print()
    bottom = get_top_bottom_products(df, metric="revenue", limit=5, ascending=True)
    print("Худшие 5 по выручке:")
    print(bottom.to_string(index=False))
    _check(len(top) == 5 and len(bottom) == 5, "limit соблюдён")
    _check(top["revenue"].is_monotonic_decreasing, "топ отсортирован по убыванию")
    _check(bottom["revenue"].is_monotonic_increasing, "антитоп отсортирован по возрастанию")
    _check(
        abs(float(get_top_bottom_products(df, limit=10_000)["revenue"].sum()) - float(df["revenue"].sum())) < 0.01,
        "сумма всех товарных строк равна общей выручке",
    )

    print()
    print("=" * 100)
    print("3. ИНТЕНТ 3: ВРЕМЕННОЙ РЯД ПО МЕСЯЦАМ")
    print("=" * 100)
    series = get_timeseries(df, freq="M")
    print(series.head(8).to_string(index=False))
    print("...")
    print(series.tail(3).to_string(index=False))
    dated_revenue = float(df.loc[df["date"].notna(), "revenue"].sum())
    _check(len(series) > 24, f"месяцев в ряду: {len(series)}")
    _check(
        abs(float(series["revenue"].sum()) - dated_revenue) < 0.01,
        f"сумма ряда равна выручке строк с датой: {series['revenue'].sum():,.2f}",
    )
    _check(series["period"].is_monotonic_increasing, "ряд отсортирован по времени")

    quarterly = get_timeseries(df, freq="Q", metric="quantity")
    _check(quarterly["period"].is_monotonic_increasing, "квартальный ряд в порядке")

    print()
    print("=" * 100)
    print("4. ИНТЕНТ 2: ДИНАМИКА КОНТРАГЕНТОВ (2023 -> 2024)")
    print("=" * 100)
    dynamics = get_counterparty_dynamics(
        df,
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
    check_delta = (dynamics["period2_value"] - dynamics["period1_value"] - dynamics["delta"]).abs().max()
    _check(check_delta < 1e-6, "delta согласована с разницей периодов")

    print()
    print("=" * 100)
    print("5. ИНТЕНТ 4: СРЕЗ ПО ТОВАРНЫМ ГРУППАМ (янв 2024 -> фев 2024)")
    print("=" * 100)
    print("ВНИМАНИЕ: в выгрузке нет колонки «товарная группа», группа выведена")
    print("эвристикой (первое слово товара).")
    groups = get_group_dynamics(
        df,
        ("2024-01-01", "2024-01-31"),
        ("2024-02-01", "2024-02-29"),
        only_drops=True,
        top_n=8,
    )
    print(groups.to_string(index=False))
    _check(
        groups.empty or (groups["delta"] < 0).all(),
        "only_drops оставил только падающие группы",
    )

    print()
    print("=" * 100)
    print("6. ИНТЕНТ 5: ОБЩАЯ СУММА ЗА ПЕРИОД")
    print("=" * 100)
    total_all = get_total(df)
    print("Весь файл:")
    print(total_all.to_string(index=False))
    total_2024 = get_total(df, start_date="2024-01-01", end_date="2024-12-31")
    print()
    print("Только 2024 год:")
    print(total_2024.to_string(index=False))
    _check(
        abs(float(total_all.iloc[0]["revenue"]) - float(df["revenue"].sum())) < 0.01,
        "итог по всему файлу совпадает с суммой строк",
    )
    _check(
        float(total_2024.iloc[0]["revenue"]) < float(total_all.iloc[0]["revenue"]),
        "итог за 2024 меньше итога за всё время",
    )

    print()
    print("=" * 100)
    print("7. ДИАГНОСТИКА КАЧЕСТВА ДАННЫХ")
    print("=" * 100)
    print(get_data_quality(df).to_string(index=False))

    print()
    print("=" * 100)
    print("8. ДИСПЕТЧЕР JSON-DSL (как это будет вызывать LangChain-инструмент)")
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
    ]
    for payload in dsl_payloads:
        result = dispatch_intent(payload["intent"], df, payload["params"])
        print(f"  {payload['intent']:22s} -> {result.shape[0]} строк, колонки {list(result.columns)}")
    _check(set(INTENT_REGISTRY) == {p["intent"] for p in dsl_payloads}, "все 5 интентов доступны через диспетчер")

    try:
        dispatch_intent("unknown_intent", df)
    except ValueError as exc:
        print(f"  OK  неизвестный интент: {exc}")
    else:
        raise AssertionError("не поднялась ошибка для неизвестного интента")

    try:
        dispatch_intent("total", df, {"limit": 5})
    except ValueError as exc:
        print(f"  OK  лишний параметр: {exc}")
    else:
        raise AssertionError("не поднялась ошибка для лишнего параметра")

    print()
    print("=" * 100)
    print("9. НЕГАТИВНЫЕ ПРОВЕРКИ (ошибки должны быть понятными)")
    print("=" * 100)
    for label, call in (
        ("неизвестная метрика", lambda: get_top_bottom_products(df, metric="profit")),
        ("limit <= 0", lambda: get_top_bottom_products(df, limit=0)),
        ("битая дата", lambda: get_timeseries(df, start_date="вчера")),
        ("начало позже конца", lambda: get_counterparty_dynamics(df, ("2024-12-31", "2024-01-01"), ("2024-01-01", "2024-01-31"))),
        ("неизвестная частота", lambda: get_timeseries(df, freq="century")),
        ("нет колонки-группы", lambda: get_group_dynamics(df, ("2024-01-01", "2024-01-31"), ("2024-02-01", "2024-02-29"), group_by="nope")),
    ):
        try:
            call()
        except (ValueError, TypeError) as exc:
            print(f"  OK  {label}: {type(exc).__name__}: {exc}")
        else:
            raise AssertionError(f"не поднялась ошибка: {label}")

    empty = get_top_bottom_products(df, start_date="2030-01-01", end_date="2030-12-31")
    _check(empty.empty, "пустой период даёт пустой DataFrame, а не исключение")

    print()
    print("=" * 100)
    print("10. РЕГРЕССИИ, НАЙДЕННЫЕ НА РЕВЬЮ")
    print("=" * 100)

    from data_loader import load_and_clean_1c_report as _load

    gross = _load(REPORT_PATH, revenue_mode="gross")
    _check(
        abs(float(gross["revenue"].sum()) - 22_957_182.97) < 0.01,
        f"revenue_mode='gross' даёт выручку с НДС: {gross['revenue'].sum():,.2f}",
    )

    all_groups = get_group_dynamics(
        df, ("2023-01-01", "2023-06-30"), ("2023-07-01", "2023-12-31")
    )
    total_drops = int((all_groups["delta"] < 0).sum())
    limited = get_group_dynamics(
        df,
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

    counts = get_timeseries(df, freq="M", agg="count")
    _check(
        int(counts["revenue"].sum()) == int(df["date"].notna().sum()),
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
    _check(jan_value == 10.0, f"Series групп выравнивается по индексу (январь у 'jan' = {jan_value})")

    for label, call in (
        ("порт top: start>end", lambda: get_top_bottom_products(df, start_date="2024-12-31", end_date="2024-01-01")),
        ("порт ts: start>end", lambda: get_timeseries(df, start_date="2024-12-31", end_date="2024-01-01")),
        ("порт total: start>end", lambda: get_total(df, start_date="2024-12-31", end_date="2024-01-01")),
    ):
        try:
            call()
        except ValueError:
            print(f"  OK  {label} -> ValueError")
        else:
            raise AssertionError(label)

    print()
    print("=" * 100)
    print("11. ИСТОЧНИК 1С (onec_source): дата у 100% строк и реальные товарные группы")
    print("=" * 100)
    onec_sales = Path("1c_export") / "sales_1c.csv"
    if not onec_sales.is_file():
        print(f"  SKIP  нет {onec_sales} -- выгрузка из 1С не найдена")
    else:
        from analytics import attach_product_group
        from onec_source import VITRINA_COLUMNS, load_vitrina

        vitrina = load_vitrina(str(onec_sales))
        print(vitrina.head(3).to_string(index=False))
        print()
        _check(list(vitrina.columns) == list(VITRINA_COLUMNS), "схема витрины 1С с product_group")
        _check(len(vitrina) > len(df), f"записей регистра больше, чем строк Excel-отчёта: {len(vitrina)} > {len(df)}")
        _check(int(vitrina["date"].isna().sum()) == 0, "в витрине 1С нет строк без даты (было 250 в Excel)")
        _check(
            abs(float(vitrina["revenue"].sum()) - float(df["revenue"].sum())) < 0.01,
            f"выручка 1С совпала с Excel: {vitrina['revenue'].sum():,.2f}",
        )
        _check(
            abs(float(vitrina["quantity"].sum()) - float(df["quantity"].sum())) < 0.01,
            f"количество 1С совпало с Excel: {vitrina['quantity'].sum():,.2f}",
        )
        _check(vitrina["product_group"].nunique() >= 10, f"реальных товарных групп: {vitrina['product_group'].nunique()}")

        grouped = attach_product_group(vitrina)
        _check(
            grouped["product_group"].astype(str).eq(vitrina["product_group"].astype(str)).all(),
            "attach_product_group не заменяет реальные группы 1С эвристикой",
        )

        full = get_timeseries(vitrina, freq="M")
        _check(
            abs(float(full["revenue"].sum()) - float(vitrina["revenue"].sum())) < 0.01,
            f"временной ряд покрывает 100% выручки: {full['revenue'].sum():,.2f}",
        )

        real_groups = get_group_dynamics(
            vitrina,
            ("2023-01-01", "2023-06-30"),
            ("2023-07-01", "2023-12-31"),
            top_n=5,
        )
        _check(
            set(real_groups.iloc[:, 0]).issubset(set(vitrina["product_group"]))
            and "Вентиляторы, пылесосы, кондиционеры" in set(vitrina["product_group"]),
            f"групповая динамика идёт по группам 1С: {list(real_groups.iloc[:3, 0])}",
        )

    print()
    print("=" * 100)
    print("12. СХЕМА LLM-СЛОЯ (docs/intent_schema.json)")
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
