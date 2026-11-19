# EVAL Live

- Провайдер/модель: **deepseek / deepseek-chat**
- Дата прогона: 2026-11-19
- Кейсов: 48 (пропущено по xfail: 2)

## Метрики

| Метрика | Значение |
|---|---|
| Status accuracy | 97.9% |
| DSL accuracy | 95.8% |
| Fact-check | 100.0% (30/30) |

## По категориям

| Категория | N | Status | DSL |
|---|---|---|---|
| simple | 25 | 100.0% | 96.0% |
| context | 6 | 83.3% | 83.3% |
| edge_case | 11 | 100.0% | 100.0% |
| non_analytical | 6 | 100.0% | 100.0% |

## Расхождения с GoldSet (2)

| Кейс | Категория | Ожидалось | Получено |
|---|---|---|---|
| test_009_memory_ellipsis | context | clarify | group_dynamics |
| test_014_filter_product | simple | ok/timeseries | timeseries |
