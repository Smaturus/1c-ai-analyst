"""Unit-тесты :class:`llm_agent.RuleBasedPlanner` и контракта JSON-DSL.

Проверяют, что офлайн-правила превращают типовые вопросы в валидный DSL:
правильный интент, параметры, периоды и фильтр ``only_drops``. Отдельный блок
тестов — регрессия бага «год попал в ``limit``» («топ 5 за 2024» → ``limit=5``,
а не ``202``).
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from onec_analyst.llm_agent import (
    INTENT_PARAMETER_NAMES,
    AgentDecision,
    LLMPlanner,
    LLMSettings,
    RuleBasedPlanner,
    build_messages,
    build_system_prompt,
    guard_decision,
    make_planner,
    parse_plan_json,
    to_dispatch_params,
)

PLANNER = RuleBasedPlanner()

PERIOD_2023 = (date(2023, 1, 1), date(2023, 12, 31))
PERIOD_2024 = (date(2024, 1, 1), date(2024, 12, 31))


def _plan(question: str, profile: dict[str, Any] | None = None):
    """Разобрать вопрос офлайн-планировщиком."""
    return PLANNER.plan(question, data_profile=profile)


# --------------------------------------------------------------------------- #
# Регрессия limit: годы и даты не должны попадать в количество строк
# --------------------------------------------------------------------------- #
def test_limit_does_not_capture_year_with_explicit_top() -> None:
    """«за 2024 год, топ 5» → limit=5 (а не 202 из «2024»)."""
    decision = _plan("Какие товары продаются хуже остальных за 2024 год, топ 5?")

    assert decision.ok and decision.intent == "top_bottom_products", decision.error
    assert decision.params["limit"] == 5, decision.params
    assert decision.params["ascending"] is True
    assert decision.params["start_date"] == date(2024, 1, 1)
    assert decision.params["end_date"] == date(2024, 12, 31)


def test_year_alone_does_not_set_limit() -> None:
    """Год без явного количества не превращается в лимит: default 10."""
    decision = _plan("Какие товары продаются хуже остальных за 2024 год?")

    assert decision.intent == "top_bottom_products", decision.error
    assert decision.params["limit"] == 10, decision.params


def test_date_with_dots_does_not_set_limit() -> None:
    """Точки в дате (21.09.2023) не дают limit=21 или 202."""
    decision = _plan("Топ товаров по выручке за 21.09.2023")

    assert decision.intent == "top_bottom_products", decision.error
    assert decision.params["limit"] == 10, decision.params


def test_explicit_top_limit_parsed() -> None:
    """«Топ-10» → limit=10, сортировка по убыванию."""
    decision = _plan("Топ-10 товаров по выручке за 2023")

    assert decision.intent == "top_bottom_products", decision.error
    assert decision.params["limit"] == 10, decision.params
    assert decision.params["ascending"] is False


def test_limit_above_maximum_falls_back_to_default() -> None:
    """Число больше 1000 откатывается к default 10."""
    decision = _plan("Топ 5000 товаров по выручке за 2024")

    assert decision.params["limit"] == 10, decision.params


# --------------------------------------------------------------------------- #
# Типовые вопросы → интент и параметры (таблица)
# --------------------------------------------------------------------------- #
CASES: list[tuple[str, str, dict[str, Any]]] = [
    (
        "Какие 5 товаров продаются хуже остальных за 2024 год?",
        "top_bottom_products",
        {"limit": 5, "ascending": True, "start_date": date(2024, 1, 1)},
    ),
    (
        "Покажи первые 3 товара по выручке за 2024",
        "top_bottom_products",
        {"limit": 3, "ascending": False},
    ),
    (
        "Покажи 7 худших товаров за 2024",
        "top_bottom_products",
        {"limit": 7, "ascending": True},
    ),
    (
        "Топ 5 товаров по количеству за 2024",
        "top_bottom_products",
        {"limit": 5, "metric": "quantity"},
    ),
    (
        "Как менялись продажи по месяцам в 2023 году?",
        "timeseries",
        {"freq": "M", "start_date": date(2023, 1, 1), "end_date": date(2023, 12, 31)},
    ),
    (
        "Как менялись продажи по кварталам в 2024?",
        "timeseries",
        {"freq": "Q"},
    ),
    (
        "Сколько мы продали за 2024 год?",
        "total",
        {"start_date": date(2024, 1, 1), "end_date": date(2024, 12, 31)},
    ),
    (
        "Сколько продали штук за 2024?",
        "total",
        {"start_date": date(2024, 1, 1)},
    ),
    (
        "У каких контрагентов упали продажи в 2024 по сравнению с 2023?",
        "counterparty_dynamics",
        {
            "period1": PERIOD_2023,
            "period2": PERIOD_2024,
            "metric": "revenue",
            "only_drops": True,
        },
    ),
    (
        "Сравни контрагентов в 2023 и 2024 годах",
        "counterparty_dynamics",
        {"period1": PERIOD_2023, "period2": PERIOD_2024, "only_drops": False},
    ),
    (
        "Какие товарные группы просели в 2024 по сравнению с 2023?",
        "group_dynamics",
        {"period1": PERIOD_2023, "period2": PERIOD_2024, "only_drops": True},
    ),
]


@pytest.mark.parametrize("question,intent,expected", CASES)
def test_rule_planner_parses_typical_questions(
    question: str, intent: str, expected: dict[str, Any]
) -> None:
    """Каждая типовая формулировка даёт ожидаемый интент и параметры."""
    decision = _plan(question)

    assert decision.ok, decision.error
    assert decision.intent == intent
    for key, value in expected.items():
        assert decision.params.get(key) == value, f"{key}: {decision.params.get(key)!r}"


@pytest.mark.parametrize("question,intent,_expected", CASES)
def test_rule_planner_dsl_passes_pydantic_validation(
    question: str, intent: str, _expected: dict[str, Any]
) -> None:
    """DSL из офлайн-правил валиден по Pydantic-моделям и совместим с dispatch."""
    decision = _plan(question)

    assert decision.ok, decision.error
    plan = parse_plan_json(json.dumps(decision.dsl, ensure_ascii=False))
    parsed_intent, params = to_dispatch_params(plan)

    assert parsed_intent == intent
    assert set(params) <= set(INTENT_PARAMETER_NAMES[intent])


# --------------------------------------------------------------------------- #
# only_drops: новый параметр для контрагентов
# --------------------------------------------------------------------------- #
def test_counterparty_only_drops_true_for_falls() -> None:
    """«У каких контрагентов упали продажи?» включает only_drops=True."""
    decision = _plan("У каких контрагентов упали продажи в 2024 по сравнению с 2023?")

    assert decision.intent == "counterparty_dynamics", decision.error
    assert decision.params["only_drops"] is True


def test_counterparty_only_drops_false_for_growth() -> None:
    """Вопрос про рост не включает фильтр падений."""
    decision = _plan("У каких контрагентов выросли продажи в 2024 по сравнению с 2023?")

    assert decision.intent == "counterparty_dynamics", decision.error
    assert decision.params["only_drops"] is False


def test_counterparty_only_drops_in_prompt_and_contract() -> None:
    """Параметр задокументирован в промпте и в контракте интента."""
    prompt = build_system_prompt()

    assert "only_drops" in INTENT_PARAMETER_NAMES["counterparty_dynamics"]
    assert "only_drops" in prompt
    assert "counterparty_dynamics" in prompt


# --------------------------------------------------------------------------- #
# Периоды, уточнения и ошибки
# --------------------------------------------------------------------------- #
def test_last_month_uses_data_profile() -> None:
    """«последний месяц» берёт дату окончания из профиля данных."""
    decision = _plan(
        "Сколько продали за последний месяц?",
        profile={"date_max": "2024-12-26", "rows": 10},
    )

    assert decision.intent == "total", decision.error
    assert decision.params["end_date"] == date(2024, 12, 26)
    assert decision.params["start_date"] == date(2024, 11, 27)


def test_clarification_for_ambiguous_question() -> None:
    """Неоднозначный вопрос → clarification с вариантами примеров."""
    decision = _plan("Покажи что-нибудь интересное")

    assert decision.ok and decision.intent == "clarification", decision.error
    assert decision.clarification is not None
    assert decision.clarification.suggestions


def test_clarification_when_periods_missing() -> None:
    """Динамика без периодов → clarification с недостающими period1/period2."""
    decision = _plan("У каких контрагентов выросли продажи?")

    assert decision.ok and decision.intent == "clarification", decision.error
    assert decision.clarification is not None
    assert "period1" in decision.clarification.missing


def test_empty_question_returns_error() -> None:
    """Пустой вопрос — ``ok=False`` без исключения."""
    decision = _plan("   ")

    assert decision.ok is False
    assert decision.error


# --------------------------------------------------------------------------- #
# Контракт DSL и выбор планировщика
# --------------------------------------------------------------------------- #
def test_parse_plan_json_extracts_fenced_json() -> None:
    """JSON в ```-блоке и в тексте ответа распознаётся."""
    fenced = '```json\n{"intent":"total","params":{"start_date":"2024-01-01"}}\n```'
    prose = 'Вот ответ: {"intent":"timeseries","params":{"freq":"Q"}} — готово.'

    assert parse_plan_json(fenced).intent == "total"
    assert parse_plan_json(prose).intent == "timeseries"


def test_parse_plan_json_rejects_unknown_intent() -> None:
    """Неизвестный интент отклоняется Pydantic-валидацией."""
    with pytest.raises((ValidationError, ValueError)):
        parse_plan_json('{"intent":"profit_forecast","params":{}}')


def test_parse_plan_json_rejects_unknown_parameter() -> None:
    """Лишний параметр отклоняется (extra="forbid")."""
    with pytest.raises((ValidationError, ValueError)):
        parse_plan_json('{"intent":"total","params":{"limit":5}}')


def test_make_planner_falls_back_to_rules_without_key() -> None:
    """Без настроенной LLM выбираются детерминированные правила."""
    assert isinstance(make_planner(LLMSettings()), RuleBasedPlanner)


def test_make_planner_uses_llm_when_configured() -> None:
    """С ключом выбирается LLM-планировщик (сеть в тесте не вызывается)."""
    settings = LLMSettings(provider="deepseek", model="deepseek-chat", api_key="test")

    assert isinstance(make_planner(settings), LLMPlanner)


# --------------------------------------------------------------------------- #
# Лояльные формулировки: слово «продажи/выручка» не обязательно
# --------------------------------------------------------------------------- #
LOOSE_CASES: list[tuple[str, str, dict[str, Any]]] = [
    (
        "Динамика по месяцам за 2023",
        "timeseries",
        {"freq": "M", "start_date": date(2023, 1, 1), "end_date": date(2023, 12, 31)},
    ),
    (
        "Динамика по кварталам за 2024",
        "timeseries",
        {"freq": "Q", "start_date": date(2024, 1, 1), "end_date": date(2024, 12, 31)},
    ),
    (
        "Покажи динамику за 2024",
        "timeseries",
        {"freq": "M", "start_date": date(2024, 1, 1), "end_date": date(2024, 12, 31)},
    ),
    (
        "Тренд за 2023",
        "timeseries",
        {"freq": "M", "start_date": date(2023, 1, 1), "end_date": date(2023, 12, 31)},
    ),
    (
        "Лидеры по выручке за 2024",
        "top_bottom_products",
        {"ascending": False, "start_date": date(2024, 1, 1), "end_date": date(2024, 12, 31)},
    ),
    (
        "Аутсайдеры по выручке за 2024",
        "top_bottom_products",
        {"ascending": True, "start_date": date(2024, 1, 1)},
    ),
    (
        "Лучший товар за 2024",
        "top_bottom_products",
        {"ascending": False, "start_date": date(2024, 1, 1)},
    ),
    (
        "Худший товар за 2024",
        "top_bottom_products",
        {"ascending": True, "start_date": date(2024, 1, 1)},
    ),
    (
        "Итог за 2024",
        "total",
        {"start_date": date(2024, 1, 1), "end_date": date(2024, 12, 31)},
    ),
    (
        "Динамика контрагентов за 2023 и 2024",
        "counterparty_dynamics",
        {"period1": PERIOD_2023, "period2": PERIOD_2024},
    ),
]


@pytest.mark.parametrize("question,intent,expected", LOOSE_CASES)
def test_loose_phrases_do_not_require_sales_word(
    question: str, intent: str, expected: dict[str, Any]
) -> None:
    """Без слов «продажи/выручка» вопрос всё равно разбирается по смыслу."""
    decision = _plan(question)

    assert decision.ok, decision.error
    assert decision.intent == intent
    for key, value in expected.items():
        assert decision.params.get(key) == value, f"{key}: {decision.params.get(key)!r}"


@pytest.mark.parametrize("question,_intent,_expected", LOOSE_CASES)
def test_loose_phrases_pass_pydantic_validation(
    question: str, _intent: str, _expected: dict[str, Any]
) -> None:
    """DSL из лояльных формулировок валиден по Pydantic-моделям."""
    decision = _plan(question)

    parsed = parse_plan_json(json.dumps(decision.dsl, ensure_ascii=False))

    assert parsed.intent == decision.intent


def test_counterparty_keyword_implies_intent_and_asks_periods() -> None:
    """«Динамика контрагентов» — уже интент; без периодов просим уточнение."""
    decision = _plan("Динамика контрагентов")

    assert decision.ok and decision.intent == "clarification", decision.error
    assert decision.clarification is not None
    assert "period1" in decision.clarification.missing


def test_compare_counterparties_without_periods_asks_periods() -> None:
    """«Сравни контрагентов» без периодов уходит в уточнение, а не в ошибку."""
    decision = _plan("Сравни контрагентов")

    assert decision.ok and decision.intent == "clarification", decision.error
    assert decision.clarification is not None
    assert "period1" in decision.clarification.missing


def test_leaders_are_not_mistaken_for_total() -> None:
    """«Лидеры по выручке за 2024» — топ, а не итог (регрессия после смягчения правил)."""
    decision = _plan("Лидеры по выручке за 2024")

    assert decision.intent == "top_bottom_products", decision.error
    assert decision.params["ascending"] is False


# --------------------------------------------------------------------------- #
# Текущая дата в системном промпте (относительные периоды)
# --------------------------------------------------------------------------- #
def test_system_prompt_starts_with_current_date() -> None:
    """Промпт начинается с даты и объясняет, как считать относительные периоды."""
    prompt = build_system_prompt(today=date(2025, 1, 15))

    assert prompt.startswith("Сегодняшняя дата: 2025-01-15.")
    assert "относительных дат" in prompt
    assert "позапрошлый год" in prompt
    assert "в этом месяце" in prompt
    assert "ОТНОСИТЕЛЬНЫЕ ПЕРИОДЫ" in prompt
    assert prompt.index("Сегодняшняя дата") < prompt.index("ИНТЕНТЫ И ПАРАМЕТРЫ")


def test_system_prompt_defaults_to_today() -> None:
    """Без параметра используется текущая дата компьютера."""
    prompt = build_system_prompt()

    assert f"Сегодняшняя дата: {date.today().isoformat()}." in prompt


def test_system_prompt_keeps_data_profile_after_rules() -> None:
    """Профиль данных добавляется в конец и не сдвигает дату из начала промпта."""
    prompt = build_system_prompt(
        {"rows": 100, "date_min": "2022-01-01", "date_max": "2024-12-31", "source": "1C"},
        today=date(2025, 1, 15),
    )

    assert prompt.startswith("Сегодняшняя дата: 2025-01-15.")
    assert "строк в витрине: 100" in prompt
    assert prompt.index("Сегодняшняя дата") < prompt.index("ДАННЫЕ")


def test_system_prompt_has_dynamic_few_shot_examples() -> None:
    """Few-Shot примеры содержат даты, рассчитанные от переданного today."""
    prompt = build_system_prompt(today=date(2026, 10, 23))

    assert "ПРИМЕРЫ С ОТНОСИТЕЛЬНЫМИ ДАТАМИ" in prompt
    assert '"start_date":"2025-01-01"' in prompt  # прошлый год
    assert '"start_date":"2024-03-01"' in prompt  # март позапрошлого года
    assert '"period1":["2024-01-01","2024-12-31"]' in prompt


def test_system_prompt_few_shot_answers_are_valid_dsl() -> None:
    """JSON из Few-Shot примеров проходит Pydantic-валидацию и dispatch-контракт."""
    prompt = build_system_prompt(today=date(2026, 10, 23))
    block = prompt.split("ПРИМЕРЫ С ОТНОСИТЕЛЬНЫМИ ДАТАМИ", 1)[1]
    answers = [line[3:] for line in block.splitlines() if line.startswith("A: ")]

    assert len(answers) == 3
    for answer in answers:
        plan = parse_plan_json(answer)
        intent, params = to_dispatch_params(plan)
        assert set(params) <= set(INTENT_PARAMETER_NAMES[intent])


def test_few_shot_examples_are_before_data_profile() -> None:
    """Примеры идут после правил, но до блока ДАННЫЕ — порядок промпта стабилен."""
    prompt = build_system_prompt({"rows": 10}, today=date(2026, 10, 23))

    assert (
        prompt.index("Сегодняшняя дата")
        < prompt.index("ПРИМЕРЫ С ОТНОСИТЕЛЬНЫМИ ДАТАМИ")
        < prompt.index("ДАННЫЕ")
    )


def test_build_messages_passes_today_to_system_prompt() -> None:
    """Текущая дата попадает в system-сообщение для LLM."""
    messages = build_messages(
        "Сколько продали в позапрошлом году?",
        data_profile={"date_max": "2024-12-31"},
        today=date(2026, 10, 23),
    )

    assert messages[0]["role"] == "system"
    assert "Сегодняшняя дата: 2026-10-23." in messages[0]["content"]
    assert messages[-1] == {"role": "user", "content": "Сколько продали в позапрошлом году?"}


class _CapturingClient:
    """Поддельный LLM-клиент: запоминает сообщения и возвращает готовый JSON."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.messages: list[dict[str, str]] = []

    def chat(self, messages: list[dict[str, str]]) -> str:
        """Сохранить сообщения и вернуть заготовленный ответ."""
        self.messages = messages
        return self.reply


def test_llm_planner_sends_current_date_to_model() -> None:
    """LLMPlanner передаёт текущую дату в system-сообщении (без вызова сети)."""
    client = _CapturingClient('{"intent":"total","params":{"start_date":"2024-01-01"}}')
    planner = LLMPlanner(
        LLMSettings(provider="deepseek", model="deepseek-chat", api_key="test"),
        client=client,
    )

    decision = planner.plan("Сколько продали в прошлом году?")

    assert decision.ok, decision.error
    assert client.messages[0]["role"] == "system"
    assert f"Сегодняшняя дата: {date.today().isoformat()}." in client.messages[0]["content"]


# --------------------------------------------------------------------------- #
# Относительные даты в офлайн-правилах (паритет с LLM)
# --------------------------------------------------------------------------- #
TODAY = date(2026, 10, 23)


def _plan_on(question: str, today: date = TODAY):
    """Разобрать вопрос с фиксированной «сегодняшней» датой."""
    return PLANNER.plan(question, today=today)


def test_relative_month_of_previous_year_for_top_products() -> None:
    """«март позапрошлого года» + «топ-5» → март 2024 года, лучшие 5 товаров."""
    decision = _plan_on(
        "Какие топ-5 товаров продавались лучше всего в марте позапрошлого года?"
    )

    assert decision.ok and decision.intent == "top_bottom_products", decision.error
    assert decision.params["limit"] == 5
    assert decision.params["ascending"] is False
    assert decision.params["start_date"] == date(2024, 3, 1)
    assert decision.params["end_date"] == date(2024, 3, 31)


def test_relative_previous_year_total() -> None:
    """«в прошлом году» → календарный прошлый год относительно today."""
    decision = _plan_on("Сколько мы продали в прошлом году?")

    assert decision.intent == "total", decision.error
    assert decision.params["start_date"] == date(2025, 1, 1)
    assert decision.params["end_date"] == date(2025, 12, 31)


def test_relative_current_year_total() -> None:
    """«в этом году» → календарный текущий год."""
    decision = _plan_on("Сколько продали в этом году?")

    assert decision.intent == "total", decision.error
    assert decision.params["start_date"] == date(2026, 1, 1)
    assert decision.params["end_date"] == date(2026, 12, 31)


def test_relative_previous_month_total() -> None:
    """«в прошлом месяце» → предыдущий календарный месяц."""
    decision = _plan_on("Сколько продали в прошлом месяце?")

    assert decision.intent == "total", decision.error
    assert decision.params["start_date"] == date(2026, 9, 1)
    assert decision.params["end_date"] == date(2026, 9, 30)


def test_relative_current_month_total() -> None:
    """«в этом месяце» → текущий календарный месяц."""
    decision = _plan_on("Сколько продали в этом месяце?")

    assert decision.intent == "total", decision.error
    assert decision.params["start_date"] == date(2026, 10, 1)
    assert decision.params["end_date"] == date(2026, 10, 31)


def test_relative_year_for_top_products() -> None:
    """«Топ-5 в позапрошлом году» → 2024 год целиком."""
    decision = _plan_on("Топ-5 товаров в позапрошлом году")

    assert decision.intent == "top_bottom_products", decision.error
    assert decision.params["start_date"] == date(2024, 1, 1)
    assert decision.params["end_date"] == date(2024, 12, 31)


def test_relative_dates_default_to_today() -> None:
    """Без параметра relative-периоды считаются от текущей даты компьютера."""
    decision = PLANNER.plan("Сколько продали в прошлом году?")

    assert decision.intent == "total", decision.error
    assert decision.params["start_date"] == date(date.today().year - 1, 1, 1)


def test_relative_pair_comparison_counterparties() -> None:
    """«прошлый год по сравнению с позапрошлым» → база 2024, оцениваемый 2025."""
    decision = _plan_on(
        "У каких контрагентов упали продажи в прошлом году по сравнению с позапрошлым?"
    )

    assert decision.ok and decision.intent == "counterparty_dynamics", decision.error
    assert decision.params["period1"] == (date(2024, 1, 1), date(2024, 12, 31))
    assert decision.params["period2"] == (date(2025, 1, 1), date(2025, 12, 31))
    assert decision.params["only_drops"] is True


def test_relative_pair_comparison_groups() -> None:
    """Та же пара относительных лет работает для товарных групп."""
    decision = _plan_on(
        "Какие товарные группы просели в прошлом году по сравнению с позапрошлым?"
    )

    assert decision.ok and decision.intent == "group_dynamics", decision.error
    assert decision.params["period1"] == (date(2024, 1, 1), date(2024, 12, 31))
    assert decision.params["period2"] == (date(2025, 1, 1), date(2025, 12, 31))
    assert decision.params["only_drops"] is True


def test_relative_pair_comparison_this_year_vs_last_year() -> None:
    """«этот год по сравнению с прошлым» → база 2025, оцениваемый 2026."""
    decision = _plan_on(
        "У каких контрагентов упали продажи в этом году по сравнению с прошлым?"
    )

    assert decision.intent == "counterparty_dynamics", decision.error
    assert decision.params["period1"] == (date(2025, 1, 1), date(2025, 12, 31))
    assert decision.params["period2"] == (date(2026, 1, 1), date(2026, 12, 31))


def test_relative_pair_without_comparison_keeps_mention_order() -> None:
    """Без «по сравнению с» порядок периодов — как в вопросе (первый = база)."""
    decision = _plan_on("Динамика контрагентов в прошлом году и позапрошлом году")

    assert decision.intent == "counterparty_dynamics", decision.error
    assert decision.params["period1"] == (date(2025, 1, 1), date(2025, 12, 31))
    assert decision.params["period2"] == (date(2024, 1, 1), date(2024, 12, 31))


# --------------------------------------------------------------------------- #
# Интент 6: сравнение топов двух периодов (L4L)
# --------------------------------------------------------------------------- #
def test_compare_tops_by_rules() -> None:
    """«Сравни топ-5 за 2023 и 2024» → compare_top_products с двумя периодами."""
    decision = _plan("Сравни топ-5 товаров за 2023 и 2024 годы")

    assert decision.intent == "compare_top_products", decision.error
    assert decision.params["period1"] == PERIOD_2023
    assert decision.params["period2"] == PERIOD_2024
    assert decision.params["top_n"] == 5
    assert decision.params["metric"] == "revenue"


def test_compare_tops_for_two_separate_ratings() -> None:
    """«топ-5 ... и топ-5 ...» (два рейтинга) тоже даёт compare_top_products."""
    decision = _plan("топ-5 товаров за 2023 и топ-5 товаров за 2024")

    assert decision.intent == "compare_top_products", decision.error
    assert decision.params["period1"] == PERIOD_2023
    assert decision.params["period2"] == PERIOD_2024


def test_single_period_top_stays_top_bottom() -> None:
    """Один период — по-прежнему обычный топ, а не L4L-сравнение."""
    decision = _plan("Топ-5 товаров по выручке за 2023 год")

    assert decision.intent == "top_bottom_products", decision.error
    assert decision.params["limit"] == 5


def test_compare_tops_requires_two_periods_in_dsl() -> None:
    """В DSL у compare_top_products обязательны оба периода."""
    with pytest.raises(ValidationError):
        parse_plan_json(
            '{"intent":"compare_top_products","params":{"period1":["2023-01-01","2023-12-31"]}}'
        )


# --------------------------------------------------------------------------- #
# Интент 7: отрицательная выручка (возвраты, сторно)
# --------------------------------------------------------------------------- #
def test_negative_revenue_by_rules() -> None:
    """«Товары с отрицательной выручкой за 2024» → negative_revenue, а не антитоп."""
    decision = _plan("покажи товары с отрицательной выручкой за 2024")

    assert decision.intent == "negative_revenue", decision.error
    assert decision.params["start_date"] == date(2024, 1, 1)
    assert decision.params["end_date"] == date(2024, 12, 31)
    assert decision.params["group_by"] == "product"


def test_returns_by_counterparty_rules() -> None:
    """«Покажи возвраты по контрагентам» → negative_revenue с group_by=counterparty."""
    decision = _plan("покажи возвраты по контрагентам за 2024")

    assert decision.intent == "negative_revenue", decision.error
    assert decision.params["group_by"] == "counterparty"


def test_negative_revenue_month_range() -> None:
    """«январь–июнь 2024» — диапазон месяцев, а не весь год."""
    decision = _plan(
        "покажи все отрицательные выручки январь–июнь 2024, товары с отрицательной выручкой"
    )

    assert decision.intent == "negative_revenue", decision.error
    assert decision.params["start_date"] == date(2024, 1, 1)
    assert decision.params["end_date"] == date(2024, 6, 30)


def test_guard_blocks_antipot_substitution() -> None:
    """LLM вернула top_bottom на вопрос о возвратах — guard переключает на negative_revenue."""
    substituted = AgentDecision(
        ok=True, intent="top_bottom_products", params={"limit": 5, "ascending": True}
    )
    guarded = guard_decision(
        substituted, "покажи товары с отрицательной выручкой за 2024"
    )

    assert guarded.intent == "negative_revenue", guarded.intent
    assert guarded.params["start_date"] == date(2024, 1, 1)


def test_guard_keeps_regular_top_questions() -> None:
    """Обычный вопрос про худшие товары guard не трогает."""
    regular = AgentDecision(
        ok=True, intent="top_bottom_products", params={"limit": 5, "ascending": True}
    )

    assert guard_decision(regular, "Топ-5 худших товаров за 2024") is regular


# --------------------------------------------------------------------------- #
# Средний чек: честный отказ вместо «похожего» интента
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "question",
    [
        "Сравни средний чек за 2024",
        "Какая средняя выручка в 2024?",
        "Покажи средний оборот по месяцам за 2023",
    ],
)
def test_average_check_returns_clarification(question: str) -> None:
    """Средний чек не поддерживается — clarification с объяснением."""
    decision = _plan(question)

    assert decision.ok and decision.intent == "clarification", decision.error
    assert decision.clarification is not None
    assert "Расчёт среднего чека не поддерживается" in decision.clarification.question


# --------------------------------------------------------------------------- #
# Новые фильтры: товар, группа, минимальная база
# --------------------------------------------------------------------------- #
def test_product_filter_from_quotes() -> None:
    """Название товара в кавычках уходит в product_filter."""
    decision = _plan("Сколько продали «Крупа Геркулес» за 2024")

    assert decision.intent == "total", decision.error
    assert decision.params["product_filter"] == "Крупа Геркулес"


def test_group_filter_from_quotes() -> None:
    """Название группы в кавычках уходит в group_filter."""
    decision = _plan("Топ-5 товаров в группе «Бытовая техника» за 2024")

    assert decision.intent == "top_bottom_products", decision.error
    assert decision.params["group_filter"] == "Бытовая техника"
    assert "product_filter" not in decision.params


def test_group_filter_for_group_dynamics() -> None:
    """Для динамики групп кавычки дают group_filter."""
    decision = _plan("Какие группы просели в 2024 по сравнению с 2023, группа «Аксессуары»")

    assert decision.intent == "group_dynamics", decision.error
    assert decision.params["group_filter"] == "Аксессуары"


def test_min_base_revenue_from_phrase() -> None:
    """«выручка больше 100к» → min_base_revenue=100000 для контрагентов."""
    decision = _plan("У каких контрагентов выручка в 2023 была больше 100к и упала в 2024")

    assert decision.intent == "counterparty_dynamics", decision.error
    assert decision.params["min_base_revenue"] == pytest.approx(100000.0)


def test_group_filter_from_group_pattern() -> None:
    """Паттерн «группа "Название"» (прямые кавычки) даёт group_filter."""
    decision = _plan('Топ-5 товаров группы "Бытовая техника"')

    assert decision.intent == "top_bottom_products", decision.error
    assert decision.params["group_filter"] == "Бытовая техника"


def test_default_period_when_no_dates_total() -> None:
    """Без дат total получает демо-период 2023-01-01…2024-12-31."""
    decision = _plan("Сколько продали")

    assert decision.intent == "total", decision.error
    assert decision.params["start_date"] == date(2023, 1, 1)
    assert decision.params["end_date"] == date(2024, 12, 31)


def test_default_period_uses_data_profile() -> None:
    """Если есть профиль данных, дефолтные даты берутся из него."""
    decision = _plan(
        "Лидеры по выручке",
        profile={"date_min": "2022-03-01", "date_max": "2024-12-26"},
    )

    assert decision.intent == "top_bottom_products", decision.error
    assert decision.params["start_date"] == date(2022, 3, 1)
    assert decision.params["end_date"] == date(2024, 12, 26)


def test_default_period_does_not_skip_missing_periods_clarification() -> None:
    """Динамика контрагентов без периодов всё равно просит уточнение."""
    decision = _plan("У каких контрагентов выросли продажи?")

    assert decision.ok and decision.intent == "clarification", decision.error


# --------------------------------------------------------------------------- #
# Регрессия: .env ищется в корне репозитория, а не внутри src/onec_analyst/
# --------------------------------------------------------------------------- #
def test_dotenv_file_is_in_repo_root() -> None:
    """Ключи LLM читаются из ``.env`` рядом с ``pyproject.toml``."""
    from onec_analyst import llm_agent
    from tests.paths import REPO_ROOT

    assert llm_agent._DOTENV_FILE == REPO_ROOT / ".env"
    assert (llm_agent._DOTENV_FILE.parent / "pyproject.toml").is_file()
