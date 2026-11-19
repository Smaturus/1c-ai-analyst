# EVAL Live

- Провайдер/модель: **deepseek / deepseek-chat**
- Дата прогона: 2026-11-19
- Кейсов: 49 (пропущено по xfail: 1)

## Метрики

| Метрика | Значение |
|---|---|
| Status accuracy | 95.9% |
| DSL accuracy | 89.8% |
| Fact-check | 100.0% (28/28) |

## По категориям

| Категория | N | Status | DSL |
|---|---|---|---|
| simple | 25 | 100.0% | 88.0% |
| context | 7 | 71.4% | 71.4% |
| edge_case | 11 | 100.0% | 100.0% |
| non_analytical | 6 | 100.0% | 100.0% |

## Расхождения с GoldSet (5)

| Кейс | Категория | Ожидалось | Получено |
|---|---|---|---|
| test_002_antitop | simple | ok/top_bottom_products | top_bottom_products |
| test_007_counterparty | simple | ok/counterparty_dynamics | counterparty_dynamics |
| test_009_memory_ellipsis | context | clarify | group_dynamics |
| test_014_filter_product | simple | ok/timeseries | timeseries |
| test_016_context_change_intent | context | clarify | refuse |
