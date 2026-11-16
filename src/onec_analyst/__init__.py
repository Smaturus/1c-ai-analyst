"""Пакет доменной логики NL2DSL-аналитика 1С.

UI (`app.py`) импортирует публичные точки входа отсюда; расчёты идут только
через `analytics.dispatch_intent` после валидации JSON-DSL.
"""

from __future__ import annotations

__version__ = "0.7.0"