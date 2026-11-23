"""Резолюция сущностей по каталогу (ADR 0007: не выдумывать сущности).

Сырая строка из вопроса/DSL сопоставляется с фактическими значениями витрины:
точное совпадение → нормализованное (регистр, ё/е) → fuzzy по токенам.
Fuzzy применяется только к токенам не короче ``MIN_FUZZY_TOKEN`` (короткие —
шум), порог — ``FUZZY_THRESHOLD``. При неоднозначности (≥2 кандидатов) или
отсутствии совпадения возвращается сырая строка: витрина даст честный пустой
результат, а не выдуманную сущность.
"""

from __future__ import annotations

import difflib
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

#: Порог fuzzy-сходства по токенам.
FUZZY_THRESHOLD = 0.8
#: Fuzzy применяется только к токенам не короче этого (короткие — шум).
MIN_FUZZY_TOKEN = 4

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def normalize(text: str) -> str:
    """Нормализация для сравнения: lower, ё→е, схлопнуть пробелы."""
    return " ".join(text.lower().replace("ё", "е").split())


def _tokens(text: str) -> list[str]:
    return [token for token in _TOKEN_RE.findall(text) if len(token) >= MIN_FUZZY_TOKEN]


def _token_similarity(left: str, right: str) -> float:
    """Доля токенов ``left``, fuzzy-совпавших с токенами ``right``."""
    left_tokens, right_tokens = _tokens(left), _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    hits = sum(
        1
        for token in left_tokens
        if any(
            difflib.SequenceMatcher(None, token, other).ratio() >= FUZZY_THRESHOLD
            for other in right_tokens
        )
    )
    return hits / len(left_tokens)


@dataclass(frozen=True)
class Resolution:
    """Результат резолюции: что подставить и как это получено."""

    value: str
    matched: str | None = None
    how: str | None = None
    score: float = 0.0

    @property
    def resolved(self) -> bool:
        return self.matched is not None


def _say(raw: str, name: str, how: str, score: float) -> Resolution:
    _LOGGER.info("resolved %r -> %r (%s %.2f)", raw, name, how, score)
    return Resolution(name, name, how, score)


def resolve_entity(raw: str, catalog: Sequence[str]) -> Resolution:
    """Сопоставить сырую строку с каталогом: exact → normalized → fuzzy.

    При неоднозначности (≥2 кандидатов) или отсутствии совпадения возвращает
    сырую строку (``resolved=False``).
    """
    text = normalize(raw)
    names = [name for name in catalog if isinstance(name, str) and name.strip()]
    if not text or not names:
        return Resolution(raw)

    if any(name == raw.strip() for name in names):
        return _say(raw, raw.strip(), "exact", 1.0)

    for name in names:
        if normalize(name) == text:
            return _say(raw, name, "normalized", 1.0)

    scored = [(name, _token_similarity(text, normalize(name))) for name in names]
    best = [(name, score) for name, score in scored if score >= FUZZY_THRESHOLD]
    if len(best) == 1:
        name, score = best[0]
        return _say(raw, name, "fuzzy", score)
    if len(best) > 1:
        _LOGGER.warning("ambiguous entity %r: %s", raw, [name for name, _ in best])
    return Resolution(raw)


__all__ = [
    "FUZZY_THRESHOLD",
    "MIN_FUZZY_TOKEN",
    "Resolution",
    "normalize",
    "resolve_entity",
]
