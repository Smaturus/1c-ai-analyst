# EVAL Judge

- Судья: **deepseek / deepseek-chat**
- Дата: 2026-11-19
- Доля «да»: **80.0%** (20/25)

## Сводка по группам

| Группа | «да» |
|---|---|
| refuse/clarify — объяснение/уточнение | 5/5 (100.0%) |
| ok — числа (payload) | 9/10 (90.0%) |
| ok — выводы | 6/10 (60.0%) |

## Результаты по кейсам

| Кейс | Статус | Вердикты |
|---|---|---|
| test_001_top_simple | ok | numbers_match_payload=нет, conclusions_follow=нет |
| test_003_total | ok | numbers_match_payload=да, conclusions_follow=да |
| test_004_timeseries | ok | numbers_match_payload=да, conclusions_follow=да |
| test_005_negative | ok | numbers_match_payload=да, conclusions_follow=нет |
| test_007_counterparty | ok | numbers_match_payload=да, conclusions_follow=нет |
| test_010_clarify_missing_dates | clarify | clarification_matches_missing=да |
| test_012_unknown_greeting | refuse | refusal_explained=да |
| test_014_filter_product | ok | numbers_match_payload=да, conclusions_follow=да |
| test_015_aggregated_metric | refuse | refusal_explained=да |
| test_025_total_year | ok | numbers_match_payload=да, conclusions_follow=да |
| test_026_edge_future_date | refuse | refusal_explained=да |
| test_028_group_specific | ok | numbers_match_payload=да, conclusions_follow=нет |
| test_032_quantity | ok | numbers_match_payload=да, conclusions_follow=да |
| test_042_dangling_pronoun_no_history | clarify | clarification_matches_missing=да |
| test_049_total_quarter | ok | numbers_match_payload=да, conclusions_follow=да |
