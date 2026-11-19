# EVAL Live

- Провайдер/модель: **deepseek / deepseek-chat**
- Дата прогона: 2026-11-19
- Кейсов: 48 (пропущено по xfail: 2)

## Метрики

| Метрика | Значение |
|---|---|
| Status accuracy | 93.8% |
| DSL accuracy | 89.6% |
| Fact-check | 100.0% (29/29) |

## По категориям

| Категория | N | Status | DSL |
|---|---|---|---|
| simple | 25 | 100.0% | 92.0% |
| context | 6 | 50.0% | 50.0% |
| edge_case | 11 | 100.0% | 100.0% |
| non_analytical | 6 | 100.0% | 100.0% |

## Расхождения с GoldSet (5)

| Кейс | Категория | Ожидалось | Получено |
|---|---|---|---|
| test_009_memory_ellipsis | context | clarify | group_dynamics |
| test_014_filter_product | simple | ok/timeseries | timeseries |
| test_016_context_change_intent | context | clarify | refuse |
| test_022_top_limit_10 | simple | ok/top_bottom_products | top_bottom_products |
| test_029_context_pronoun_products | context | refuse | clarify |
