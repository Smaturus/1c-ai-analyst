"""Выбор текста запроса после уточнения: полный вопрос или склейка.

Сценарий: пользователь задал вопрос, планировщик вернул ``clarification``,
пользователь выбрал вариант ответа. Дальше нужно решить, что показывать в поле
ввода и отправлять в расчёт:

* вторая часть сама по себе распознаётся планировщиком как полноценный вопрос
  (например, «Сколько мы продали за 2024 год?») — показываем только её;
* вторая часть — фрагмент/параметр (например, «2024 год») — склеиваем её с
  исходным вопросом через пробел.

Спорный случай (похоже на вопрос, но правила его не распознали) решает
LLM-классификатор — только если LLM включена. Если LLM недоступна или вернула
мусор, используется безопасная склейка.

Модуль отвечает только за текст запроса: числа по-прежнему считает pandas,
LLM может лишь выбрать представление. Это не нарушает принцип «LLM не считает
числа» (см. [ADR 0004](docs/adr/0004-llm-json-dsl.md)).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

from onec_analyst.llm_agent import LLMError, LLMSettings, RuleBasedPlanner, make_client

#: Вторая часть самодостаточна — в запрос уходит только она.
SOURCE_ANSWER = "answer"
#: Вторая часть — фрагмент, тексты склеены по правилам.
SOURCE_MERGED = "merged"
#: Решение принял LLM-классификатор (в спорном случае).
SOURCE_LLM = "llm"

#: Минимум слов, при котором нераспознанная вторая часть считается «похожей на
#: вопрос» и уходит на LLM-классификацию (иначе — явный фрагмент).
MIN_QUESTION_WORDS = 4

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_WORD_RE = re.compile(r"[\w\-]+", re.UNICODE)
_FLAG_RE = re.compile(r'"self_sufficient"\s*:\s*(true|false)', re.IGNORECASE)

_CLASSIFIER_SYSTEM = (
    "Ты классификатор запросов к аналитике продаж. Даны исходный вопрос пользователя "
    "и уточнение (второй шаг диалога). Определи, является ли уточнение самостоятельным "
    "вопросом, который можно выполнить без исходного (например, «Сколько мы продали "
    "за 2024 год?»), или это только фрагмент/параметр (например, «2024 год», "
    "«2023 и 2024»). Ответь ТОЛЬКО JSON: "
    '{"self_sufficient": true} или {"self_sufficient": false}.'
)


class ChatClient(Protocol):
    """Минимальный интерфейс LLM-клиента: один метод ``chat``."""

    def chat(self, messages: list[dict[str, str]]) -> str:  # pragma: no cover - протокол
        ...


@dataclass(frozen=True)
class ClarifiedText:
    """Результат выбора текста после уточнения.

    Attributes
    ----------
    text:
        Текст, который уходит в поле ввода, в расчёт и в историю (один и тот же).
    source:
        Кто выбрал: :data:`SOURCE_ANSWER`, :data:`SOURCE_MERGED` или :data:`SOURCE_LLM`.
    reason:
        Короткое объяснение решения (для тестов и отладки).
    """

    text: str
    source: str
    reason: str


def merge_clarification(base: str, answer: str) -> str:
    """Склеить исходный вопрос с уточнением через пробел без дублирования.

    Если уточнение уже содержит исходный вопрос (модель вернула полную
    формулировку), возвращается уточнение как есть.
    """
    base = base.strip()
    answer = answer.strip()
    if not base:
        return answer
    if not answer:
        return base
    if answer.startswith(base) or base in answer:
        return answer
    if base.startswith(answer):
        return base
    return f"{base} {answer}"


def answer_is_self_sufficient(
    answer: str,
    *,
    planner: RuleBasedPlanner | None = None,
    data_profile: dict[str, Any] | None = None,
) -> bool:
    """Распознаётся ли вторая часть как полноценный вопрос одного из 7 интентов.

    Проверка детерминированная: используется тот же :class:`RuleBasedPlanner`,
    что и в офлайн-режиме, поэтому «Сколько мы продали за 2024 год?» — полный
    вопрос, а «2024 год» или «2023 и 2024» — только фрагменты.
    """
    answer = (answer or "").strip()
    if not answer:
        return False
    planner = planner or RuleBasedPlanner()
    decision = planner.plan(answer, data_profile=data_profile)
    return bool(decision.ok and decision.intent and decision.intent != "clarification")


def _word_count(text: str) -> int:
    """Число слов/чисел в тексте (для оценки «похоже на вопрос»)."""
    return len(_WORD_RE.findall(text))


def _looks_like_question(text: str) -> bool:
    """Похож ли фрагмент на вопрос: есть «?» или он достаточно длинный."""
    return text.rstrip().endswith("?") or _word_count(text) >= MIN_QUESTION_WORDS


def _parse_self_sufficient(raw: str) -> bool | None:
    """Достать флаг ``self_sufficient`` из ответа модели; ``None`` — не удалось."""
    candidates = [raw.strip()]
    match = _JSON_OBJECT_RE.search(raw)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("self_sufficient"), bool):
            return data["self_sufficient"]
    flag = _FLAG_RE.search(raw)
    if flag:
        return flag.group(1).lower() == "true"
    return None


def classify_with_llm(
    base: str,
    answer: str,
    *,
    client: ChatClient | None = None,
    settings: LLMSettings | None = None,
) -> bool | None:
    """Спросить LLM, самодостаточна ли вторая часть.

    Returns
    -------
    bool | None
        ``True`` — уточнение можно выполнить отдельно, ``False`` — это фрагмент,
        ``None`` — LLM недоступна/ответ не разобран (тогда решают правила).
    """
    if client is None:
        if settings is None or not settings.is_configured:
            return None
        client = make_client(settings)
    payload = json.dumps({"base": base, "answer": answer}, ensure_ascii=False)
    try:
        raw = client.chat(
            [
                {"role": "system", "content": _CLASSIFIER_SYSTEM},
                {"role": "user", "content": payload},
            ]
        )
    except (LLMError, OSError, ValueError):
        return None
    return _parse_self_sufficient(raw)


def clarification_limit_banner(question: str) -> str:
    """Жёлтая плашка ADR 0007 при принудительной интерпретации правилами."""
    text = (question or "").strip()
    return f"Уточнения исчерпаны — показываю как понял: «{text}»"


CLARIFICATION_LIMIT_FAILURE_HINT = (
    "Не удалось однозначно разобрать вопрос даже после уточнений. "
    "Задайте, пожалуйста, один показатель и один период."
)
CLARIFICATION_LIMIT_ERROR = (
    "Не удалось однозначно разобрать вопрос даже после уточнений"
)


def force_interpretation(
    question: str,
    *,
    planner: RuleBasedPlanner | None = None,
    data_profile: dict[str, Any] | None = None,
) -> Any | None:
    """Детерминированная «ближайшая» интерпретация вопроса без LLM.

    Используется, когда лимит уточнений исчерпан: вместо нового круга вопрос
    разбирается теми же правилами, что и в офлайн-режиме. Составной запрос
    сводится к одному показателю (например, диапазону периодов).

    Returns
    -------
    AgentDecision | None
        Решение с конкретным интентом или ``None``, если правила тоже не смогли
        выбрать интент.
    """
    planner = planner or RuleBasedPlanner()
    decision = planner.plan(question, data_profile=data_profile)
    if not decision.ok or not decision.intent or decision.intent == "clarification":
        return None
    return decision


def choose_clarified_text(
    base: str,
    answer: str,
    *,
    use_llm: bool = False,
    settings: LLMSettings | None = None,
    client: ChatClient | None = None,
    planner: RuleBasedPlanner | None = None,
    data_profile: dict[str, Any] | None = None,
) -> ClarifiedText:
    """Выбрать итоговый текст запроса после уточнения.

    Порядок решений:

    1. Пустое уточнение — остаётся исходный вопрос.
    2. Нет исходного вопроса — остаётся уточнение.
    3. Уточнение уже содержит исходный вопрос — остаётся уточнение.
    4. Вторая часть — полный вопрос по правилам планировщика — только она.
    5. Вторая часть — явный фрагмент — склейка через пробел.
    6. Спорный случай — LLM-классификатор (если включена), иначе склейка.
    """
    base = (base or "").strip()
    answer = (answer or "").strip()
    if not answer:
        return ClarifiedText(base, SOURCE_MERGED, "пустое уточнение — оставлен исходный вопрос")
    if not base:
        return ClarifiedText(answer, SOURCE_ANSWER, "нет исходного вопроса")
    if answer.startswith(base) or base in answer:
        return ClarifiedText(answer, SOURCE_ANSWER, "уточнение уже содержит исходный вопрос")
    if answer_is_self_sufficient(answer, planner=planner, data_profile=data_profile):
        return ClarifiedText(answer, SOURCE_ANSWER, "вторая часть — полный вопрос")
    if not _looks_like_question(answer):
        return ClarifiedText(merge_clarification(base, answer), SOURCE_MERGED, "вторая часть — фрагмент")
    if use_llm and (client is not None or (settings is not None and settings.is_configured)):
        verdict = classify_with_llm(base, answer, client=client, settings=settings)
        if verdict is True:
            return ClarifiedText(answer, SOURCE_LLM, "LLM: уточнение самодостаточно")
        if verdict is False:
            return ClarifiedText(merge_clarification(base, answer), SOURCE_LLM, "LLM: уточнение — фрагмент")
    return ClarifiedText(
        merge_clarification(base, answer),
        SOURCE_MERGED,
        "спорный случай — безопасная склейка",
    )


__all__ = [
    "CLARIFICATION_LIMIT_ERROR",
    "CLARIFICATION_LIMIT_FAILURE_HINT",
    "SOURCE_ANSWER",
    "SOURCE_LLM",
    "SOURCE_MERGED",
    "ClarifiedText",
    "answer_is_self_sufficient",
    "choose_clarified_text",
    "clarification_limit_banner",
    "classify_with_llm",
    "force_interpretation",
    "merge_clarification",
]
