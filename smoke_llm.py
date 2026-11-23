"""Самопроверка LLM-слоя (Pydantic) без Streamlit и без pandas.

Запускается интерпретатором, где установлен **pydantic 2** (в этом проекте —
системный Python 3.14 с пакетами из соседнего окружения)::

    set PYTHONPATH=<путь к site-packages>;<путь к проекту>
    python smoke_llm.py

Проверяет:

* Pydantic-модели принимают корректные DSL всех 7 интентов и уточнение;
* отклоняют неизвестный интент, лишние параметры, плохие даты/метрики/лимиты;
* ``to_dispatch_params`` даёт аргументы, совместимые с ``dispatch_intent``;
* схема моделей совпадает с ``docs/intent_schema.json`` (снята из analytics);
* детерминированный планировщик разбирает типовые вопросы и просит уточнение;
* LLM-планировщик обрабатывает валидный ответ, «починку» и двойную ошибку;
* ``app.py`` синтаксически корректен и использует нужные вызовы Streamlit.

Если pydantic недоступен — печатает инструкцию и выходит с кодом 2.
"""

from __future__ import annotations

import ast
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

try:
    from pydantic import ValidationError
except ImportError:  # pragma: no cover - зависит от окружения
    print(
        "SKIP: pydantic не установлен. Установите pydantic>=2 или используйте "
        "интерпретатор с pydantic (см. docstring)."
    )
    raise SystemExit(2) from None

import onec_analyst.llm_agent as la

ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "docs" / "intent_schema.json"

_checks = 0


def check(condition: bool, message: str) -> None:
    """Проверить условие и посчитать проверку."""
    global _checks
    if not condition:
        raise AssertionError(message)
    _checks += 1


def expect_validation_error(payload: dict[str, Any], label: str) -> None:
    """Проверить, что DSL отклоняется Pydantic-моделью."""
    try:
        la.parse_plan_json(json.dumps(payload, ensure_ascii=False))
    except (ValidationError, ValueError):
        check(True, f"отклонено: {label}")
    else:
        raise AssertionError(f"должно было быть отклонено: {label}")


def test_models() -> None:
    """Валидные и невалидные DSL."""
    valid = [
        {
            "intent": "top_bottom_products",
            "params": {"metric": "revenue", "limit": 5, "ascending": True,
                       "start_date": "2024-01-01", "end_date": "2024-12-31"},
        },
        {"intent": "timeseries", "params": {"freq": "M", "metric": "revenue",
                                            "start_date": "2023-01-01", "end_date": "2023-12-31"}},
        {
            "intent": "counterparty_dynamics",
            "params": {"period1": ["2023-01-01", "2023-12-31"],
                       "period2": ["2024-01-01", "2024-12-31"], "top_n": 10},
        },
        {
            "intent": "group_dynamics",
            "params": {"period1": ["01.01.2023", "31.12.2023"],
                       "period2": ["01.01.2024", "31.12.2024"], "only_drops": True},
        },
        {"intent": "total", "params": {"start_date": "2024-01-01"}},
        {"intent": "total"},
        {"intent": "clarification", "question": "За какой период?", "missing": ["period1"]},
    ]
    for payload in valid:
        la.parse_plan_json(json.dumps(payload, ensure_ascii=False))
        check(True, f"валидный DSL: {payload['intent']}")

    invalid = [
        ({"intent": "unknown", "params": {}}, "неизвестный интент"),
        ({"intent": "top_bottom_products", "params": {"limit": 0}}, "limit=0"),
        ({"intent": "top_bottom_products", "params": {"limit": 5, "unknown": 1}}, "лишний параметр"),
        ({"intent": "top_bottom_products", "params": {"metric": "profit"}}, "неизвестная метрика"),
        ({"intent": "top_bottom_products", "params": {"start_date": "2024-02-31"}}, "несуществующая дата"),
        (
            {"intent": "counterparty_dynamics",
             "params": {"period1": ["2024-01-01", "2024-06-30"]}},
            "нет period2",
        ),
        (
            {"intent": "counterparty_dynamics",
             "params": {"period1": ["2024-06-30", "2024-01-01"],
                        "period2": ["2023-01-01", "2023-12-31"]}},
            "начало периода позже конца",
        ),
        ({"intent": "timeseries", "params": {"freq": "century"}}, "неизвестная частота"),
        ({"intent": "group_dynamics", "params": {"period1": [], "period2": []}}, "пустые периоды"),
    ]
    for payload, label in invalid:
        expect_validation_error(payload, label)


def test_parsing_and_conversion() -> None:
    """Извлечение JSON из текста и конвертация в параметры dispatch."""
    fenced = '```json\n{"intent":"total","params":{"start_date":"2024-01-01"}}\n```'
    plan = la.parse_plan_json(fenced)
    check(plan.intent == "total", "JSON в ```-блоке разобран")
    prose = 'Вот ответ: {"intent":"timeseries","params":{"freq":"Q"}} — готово.'
    check(la.parse_plan_json(prose).intent == "timeseries", "JSON из текста разобран")

    plan = la.parse_plan_json(
        '{"intent":"counterparty_dynamics","params":{'
        '"period1":["2023-01-01","2023-12-31"],'
        '"period2":["2024-01-01","2024-12-31"]}}'
    )
    intent, params = la.to_dispatch_params(plan)
    check(intent == "counterparty_dynamics", "интент для dispatch")
    check(set(params) <= set(la.INTENT_PARAMETER_NAMES[intent]), "параметры допустимы")
    check(isinstance(params["period1"][0], date), "даты периода — date, а не строка")

    plan = la.parse_plan_json('{"intent":"total","params":{"start_date":"01.03.2024"}}')
    _, params = la.to_dispatch_params(plan)
    check(params["start_date"] == date(2024, 3, 1), "русский формат даты поддержан")


def test_rule_planner() -> None:
    """Детерминированный fallback: типовые вопросы и уточнения."""
    planner = la.RuleBasedPlanner()
    cases: list[tuple[str, str, dict[str, Any]]] = [
        (
            "Какие 5 товаров продаются хуже остальных за 2024 год?",
            "top_bottom_products",
            {"limit": 5, "ascending": True, "start_date": date(2024, 1, 1), "end_date": date(2024, 12, 31)},
        ),
        (
            "Топ-10 товаров по выручке за 2023",
            "top_bottom_products",
            {"limit": 10, "ascending": False, "start_date": date(2023, 1, 1)},
        ),
        (
            "Как менялись продажи по месяцам в 2023 году?",
            "timeseries",
            {"freq": "M", "start_date": date(2023, 1, 1), "end_date": date(2023, 12, 31)},
        ),
        (
            "Как менялись продажи по кварталам в 2024?",
            "timeseries",
            {"freq": "Q", "start_date": date(2024, 1, 1)},
        ),
        (
            "У каких контрагентов упали продажи в 2024 по сравнению с 2023?",
            "counterparty_dynamics",
            {
                "period1": (date(2023, 1, 1), date(2023, 12, 31)),
                "period2": (date(2024, 1, 1), date(2024, 12, 31)),
            },
        ),
        (
            "Какие товарные группы просели в 2024 по сравнению с 2023?",
            "group_dynamics",
            {"only_drops": True},
        ),
        ("Сколько мы продали за 2024 год?", "total", {"start_date": date(2024, 1, 1)}),
        ("Сколько продали штук за 2024?", "total", {"start_date": date(2024, 1, 1)}),
    ]
    for question, expected_intent, expected_params in cases:
        decision = planner.plan(question)
        check(decision.ok and decision.intent == expected_intent, f"интент: {question!r}")
        for key, value in expected_params.items():
            check(
                decision.params.get(key) == value,
                f"параметр {key} для {question!r}: {decision.params.get(key)!r} != {value!r}",
            )

    clarified = planner.plan("Покажи что-нибудь интересное")
    check(
        clarified.ok and clarified.intent == "clarification",
        "неоднозначный вопрос → clarification",
    )

    clarified = planner.plan("У каких контрагентов выросли продажи?")
    check(
        clarified.ok and clarified.intent == "clarification",
        "динамика без периодов → clarification",
    )
    check(
        clarified.clarification is not None and "period1" in clarified.clarification.missing,
        "в уточнении перечислены недостающие периоды",
    )


class _FakeClient:
    """Поддельный LLM-клиент: возвращает заранее заданные ответы."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls = 0

    def chat(self, messages: list[dict[str, str]]) -> str:
        """Вернуть следующий заготовленный ответ."""
        self.calls += 1
        return self._replies.pop(0)


def test_llm_planner() -> None:
    """LLM-планировщик: валидный ответ, починка и двойная ошибка."""
    settings = la.LLMSettings(
        provider="deepseek",
        model="deepseek-chat",
        api_key="test",
        base_url="http://example.invalid/v1",
    )
    valid = json.dumps(
        {"intent": "total", "params": {"start_date": "2024-01-01", "end_date": "2024-12-31"}}
    )
    planner = la.LLMPlanner(settings, _FakeClient([valid]))
    decision = planner.plan("Сколько продали за 2024?")
    check(decision.ok and decision.intent == "total" and decision.source == "llm", "LLM: валидный ответ")

    planner = la.LLMPlanner(settings, _FakeClient(["не JSON", valid]))
    decision = planner.plan("Сколько продали за 2024?")
    check(decision.ok and decision.intent == "total", "LLM: ответ починен со второй попытки")

    planner = la.LLMPlanner(settings, _FakeClient(["не JSON", "снова не JSON"]))
    decision = planner.plan("Сколько продали за 2024?")
    check(not decision.ok and decision.error is not None, "LLM: дружелюбная ошибка после двух попыток")


def test_prompt_and_settings() -> None:
    """Промпт и настройки из окружения."""
    prompt = la.build_system_prompt({"rows": 100, "date_min": "2024-01-01", "date_max": "2024-12-31"})
    for name in la.INTENT_NAMES:
        check(name in prompt, f"промпт содержит интент {name}")
        check(la.INTENT_EXAMPLES[name] in prompt, f"промпт содержит пример {name}")
    check("clarification" in prompt and "JSON" in prompt, "промпт: формат и уточнение")
    check("строк в витрине: 100" in prompt, "промпт: профиль данных")

    messages = la.build_messages("Сколько продали?", [{"role": "user", "content": "привет"}])
    check(messages[0]["role"] == "system" and messages[-1]["role"] == "user", "формат сообщений")

    settings = la.settings_from_env({})
    check(settings.provider == "none" and not settings.is_configured, "по умолчанию LLM выключена")
    settings = la.settings_from_env({"LLM_PROVIDER": "deepseek", "LLM_API_KEY": "k"})
    check(settings.base_url.endswith("deepseek.com/v1"), "deepseek: базовый URL")
    settings = la.settings_from_env({"LLM_PROVIDER": "ollama"})
    check(settings.is_configured, "ollama настроена без ключа")
    settings = la.settings_from_env({"LLM_PROVIDER": "gigachat", "LLM_API_KEY": "k"})
    check(settings.model == "GigaChat-2-Pro", "gigachat: модель по умолчанию")
    check(settings.auth_url.endswith("api/v2/oauth"), "gigachat: эндпоинт OAuth")
    check(isinstance(la.make_client(settings), la.GigaChatClient), "gigachat: клиент с tool calling")
    try:
        la.settings_from_env({"LLM_PROVIDER": "unknown"})
    except ValueError:
        check(True, "неизвестный провайдер отклонён")
    else:
        raise AssertionError("должен был быть ValueError")


def test_schema_file() -> None:
    """Сверка моделей с JSON-схемой, снятой из analytics.INTENT_PARAMETERS."""
    if not SCHEMA_PATH.is_file():
        print(f"  SKIP  нет {SCHEMA_PATH} (сгенерируйте из analytics при необходимости)")
        return
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    check(set(schema["intents"]) == set(la.INTENT_NAMES), "набор интентов совпадает с analytics")
    for name, info in schema["intents"].items():
        check(
            set(info["params"]) == set(la.INTENT_PARAMETER_NAMES[name]),
            f"параметры {name} совпадают с analytics",
        )


def test_app_static() -> None:
    """Статическая проверка app.py: синтаксис и обязательные вызовы."""
    app_path = ROOT / "app.py"
    tree = ast.parse(app_path.read_text(encoding="utf-8"))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    check("process_request" in names, "app.py вызывает process_request (диспетчер → dispatch_intent)")
    check("get_data_quality" in names, "app.py показывает качество данных")
    for attribute in ("altair_chart", "expander", "dataframe", "columns", "button", "metric"):
        check(attribute in attributes, f"app.py использует st.{attribute}")


def test_export_schema() -> None:
    """Экспорт схемы во временный файл (внутри рабочей папки — без прав вне её)."""
    import shutil

    directory = ROOT / ".tmp_schema_test"
    directory.mkdir(exist_ok=True)
    try:
        path = directory / "schema.json"
        schema = la.export_schema(str(path))
        check(set(schema["intents"]) == set(la.INTENT_NAMES), "экспорт схемы")
        check(path.is_file() and json.loads(path.read_text(encoding="utf-8")) == schema, "схема записана")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def main() -> int:
    """Запустить все проверки и напечатать итог."""
    for test in (
        test_models,
        test_parsing_and_conversion,
        test_rule_planner,
        test_llm_planner,
        test_prompt_and_settings,
        test_schema_file,
        test_app_static,
        test_export_schema,
    ):
        test()
        print(f"OK  {test.__name__}")
    print(f"\nSMOKE LLM PASSED: {_checks} проверок")
    return 0


if __name__ == "__main__":
    sys.exit(main())
