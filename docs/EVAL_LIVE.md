# EVAL Live

- Провайдер/модель: **deepseek / deepseek-chat**
- Дата прогона: 2026-11-18
- Кейсов: 49 (пропущено по xfail: 1)

## Метрики

| Метрика | Значение |
|---|---|
| Status accuracy | 89.8% |
| DSL accuracy | 81.6% |
| Fact-check | 100.0% (25/25) |

## По категориям

| Категория | N | Status | DSL |
|---|---|---|---|
| simple | 25 | 96.0% | 80.0% |
| context | 7 | 71.4% | 71.4% |
| edge_case | 11 | 81.8% | 81.8% |
| non_analytical | 6 | 100.0% | 100.0% |

## Расхождения с GoldSet (9)

| Кейс | Категория | Ожидалось | Получено |
|---|---|---|---|
| test_002_antitop | simple | ok/top_bottom_products | top_bottom_products |
| test_007_counterparty | simple | ok/counterparty_dynamics | counterparty_dynamics |
| test_009_memory_ellipsis | context | clarify | group_dynamics |
| test_014_filter_product | simple | ok/timeseries | timeseries |
| test_016_context_change_intent | context | clarify | refuse |
| test_017_context_relative_dates | simple | ok/total | total |
| test_023_l4l_specific | simple | refuse | compare_top_products |
| test_044_injection_in_product_name | edge_case | ok/top_bottom_products | refuse |
| test_045_injection_tool_call | edge_case | ok/top_bottom_products | refuse |
