# EVAL Live

- Провайдер/модель: **deepseek / deepseek-chat**
- Дата прогона: 2026-11-19
- Кейсов: 48 (пропущено по xfail: 2)

## Метрики

| Метрика | Значение |
|---|---|
| Status accuracy | 95.8% |
| DSL accuracy | 93.8% |
| Fact-check | 100.0% (30/30) |

## По категориям

| Категория | N | Status | DSL |
|---|---|---|---|
| simple | 25 | 100.0% | 96.0% |
| context | 6 | 66.7% | 66.7% |
| edge_case | 11 | 100.0% | 100.0% |
| non_analytical | 6 | 100.0% | 100.0% |

## Расхождения с GoldSet (3)

| Кейс | Категория | Ожидалось | Получено |
|---|---|---|---|
| test_014_filter_product | simple | ok/timeseries | timeseries |
| test_016_context_change_intent | context | clarify | refuse |
| test_029_context_pronoun_products | context | refuse | clarify |
