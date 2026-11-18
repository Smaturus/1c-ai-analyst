# EVAL Live

- Провайдер/модель: **gigachat / GigaChat-2-Pro**
- Дата прогона: 2026-11-18
- Кейсов: 49 (пропущено по xfail: 1)

## Метрики

| Метрика | Значение |
|---|---|
| Status accuracy | 83.7% |
| DSL accuracy | 69.4% |
| Fact-check | 100.0% (24/24) |

## По категориям

| Категория | N | Status | DSL |
|---|---|---|---|
| simple | 25 | 96.0% | 68.0% |
| context | 7 | 71.4% | 71.4% |
| edge_case | 11 | 54.5% | 54.5% |
| non_analytical | 6 | 100.0% | 100.0% |

## Расхождения с GoldSet (15)

| Кейс | Категория | Ожидалось | Получено |
|---|---|---|---|
| test_002_antitop | simple | ok/top_bottom_products | top_bottom_products |
| test_007_counterparty | simple | ok/counterparty_dynamics | counterparty_dynamics |
| test_009_memory_ellipsis | context | clarify | timeseries |
| test_010_clarify_missing_dates | edge_case | clarify | total |
| test_011_clarify_missing_product | edge_case | clarify | timeseries |
| test_014_filter_product | simple | ok/timeseries | timeseries |
| test_016_context_change_intent | context | clarify | negative_revenue |
| test_017_context_relative_dates | simple | ok/total | total |
| test_020_clarify_vague | edge_case | clarify | total |
| test_022_top_limit_10 | simple | ok/top_bottom_products | top_bottom_products |
| test_023_l4l_specific | simple | refuse | compare_top_products |
| test_028_group_specific | simple | ok/group_dynamics | group_dynamics |
| test_032_quantity | simple | ok/total | total |
| test_042_dangling_pronoun_no_history | edge_case | clarify | timeseries |
| test_043_dangling_pronoun_no_entities | edge_case | clarify | timeseries |
