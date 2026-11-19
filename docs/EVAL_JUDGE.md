# EVAL Judge

- Судья: **deepseek / deepseek-chat**
- Дата: 2026-11-19
- Доля «да»: **92.0%** (23/25)

## Сводка по группам

| Группа | «да» |
|---|---|
| refuse/clarify — объяснение/уточнение | 5/5 (100.0%) |
| ok — числа (противоречия, «нет» = корректно) | 10/10 (100.0%) |
| ok — выводы (faithfulness, «нет» = корректно) | 8/10 (80.0%) |

## Результаты по кейсам

| Кейс | Статус | Вердикты |
|---|---|---|
| test_001_top_simple | ok | numbers_contradict_data=нет, contradicts_data=нет |
| test_003_total | ok | numbers_contradict_data=нет, contradicts_data=нет |
| test_004_timeseries | ok | numbers_contradict_data=нет, contradicts_data=нет |
| test_005_negative | ok | numbers_contradict_data=нет, contradicts_data=нет |
| test_007_counterparty | ok | numbers_contradict_data=нет, contradicts_data=да |
| test_010_clarify_missing_dates | clarify | clarification_matches_missing=да |
| test_012_unknown_greeting | refuse | refusal_explained=да |
| test_014_filter_product | ok | numbers_contradict_data=нет, contradicts_data=да |
| test_015_aggregated_metric | refuse | refusal_explained=да |
| test_025_total_year | ok | numbers_contradict_data=нет, contradicts_data=нет |
| test_026_edge_future_date | refuse | refusal_explained=да |
| test_028_group_specific | ok | numbers_contradict_data=нет, contradicts_data=нет |
| test_032_quantity | ok | numbers_contradict_data=нет, contradicts_data=нет |
| test_042_dangling_pronoun_no_history | clarify | clarification_matches_missing=да |
| test_049_total_quarter | ok | numbers_contradict_data=нет, contradicts_data=нет |
