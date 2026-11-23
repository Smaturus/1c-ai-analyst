# EVAL Live

- Провайдер/модель: **deepseek / deepseek-chat**
- Дата прогона: 2026-11-18
- Кейсов: 49 (пропущено по xfail: 1)

## Метрики

| Метрика | Значение |
|---|---|
| Status accuracy | 93.9% |
| DSL accuracy | 85.7% |
| Fact-check | 100.0% (26/26) |

## По категориям

| Категория | N | Status | DSL |
|---|---|---|---|
| simple | 25 | 96.0% | 80.0% |
| context | 7 | 71.4% | 71.4% |
| edge_case | 11 | 100.0% | 100.0% |
| non_analytical | 6 | 100.0% | 100.0% |

## Расхождения с GoldSet (7)

| Кейс | Категория | Ожидалось | Получено |
|---|---|---|---|
| test_002_antitop | simple | ok/top_bottom_products | top_bottom_products |
| test_007_counterparty | simple | ok/counterparty_dynamics | counterparty_dynamics |
| test_014_filter_product | simple | ok/timeseries | timeseries |
| test_016_context_change_intent | context | clarify | refuse |
| test_017_context_relative_dates | simple | ok/total | refuse |
| test_022_top_limit_10 | simple | ok/top_bottom_products | top_bottom_products |
| test_029_context_pronoun_products | context | refuse | clarify |
