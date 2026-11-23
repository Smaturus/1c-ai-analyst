"""CI-гейт: `cases.json` — производная от эталона, набор не дрейфует молча (US-5).

Сравнивается **генерация адаптера** из ``tests/golden/GoldenSet.py`` (старый формат
ТЗ 6.3) с ``tests/golden/cases.json`` (канон), а не сырые файлы: иначе форматный
маппинг был бы вечно красным.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from onec_analyst.paths import find_repo_root
from tests.golden.adapter import NonMappable, adapt_expected_dsl, adapt_facts
from tests.golden.schema import canonical_params, load_cases

_ROOT = find_repo_root(Path(__file__).resolve())
_GOLDEN_FILE = _ROOT / "tests" / "golden" / "GoldenSet.py"


def _load_golden() -> dict[str, dict[str, Any]]:
    text = _GOLDEN_FILE.read_text(encoding="utf-8")
    match = re.search(r'GOLDEN_SET_JSON\s*=\s*"""(.*?)"""', text, re.S)
    assert match is not None, "в эталоне не найден GOLDEN_SET_JSON"
    return {case["id"]: case for case in json.loads(match.group(1))}


def _canon(dsl: Any) -> Any:
    if dsl is None:
        return None
    intent, params = canonical_params(dsl)
    return (intent, json.dumps(params, ensure_ascii=False, sort_keys=True, default=str))


def _norm_facts(facts: Any) -> list[dict[str, Any]]:
    """Факты без числового ``value``: число — независимый pandas-расчёт проекта, не эталон."""
    return [
        {key: value for key, value in fact.items() if key != "value"}
        for fact in adapt_facts(facts)
    ]


GOLDEN = _load_golden()
REPO = {case["id"]: case for case in load_cases()}


@pytest.mark.parametrize("case_id", sorted(GOLDEN))
def test_cases_json_is_derived_from_golden(case_id: str) -> None:
    """`cases.json` равен генерации адаптера из эталона (не сырому файлу)."""
    golden = GOLDEN[case_id]
    repo = REPO[case_id]

    assert repo["question"] == golden["question"]
    assert repo["category"] == golden["category"]
    if repo.get("expected_status") == "ok":
        assert repo["expected_intent"] == golden["expected_intent"]
        assert _norm_facts(repo.get("expected_facts")) == _norm_facts(
            golden.get("expected_facts")
        ), "expected_facts дрейфуют от эталона (без value)"
    else:
        # clarify/refuse/xfail: интент UNKNOWN и факты отказа/уточнения — решение
        # проекта, из эталона не выводятся (эталон хранит «потенциальный» DSL).
        assert repo["expected_intent"] == "UNKNOWN"

    try:
        adapted = _canon(adapt_expected_dsl(golden))
    except NonMappable:
        adapted = None  # products/revenue_vat вне MVP → clarify/refuse/xfail
    except Exception:  # noqa: BLE001 — невалидный канон (period2 обязателен и т.п.)
        adapted = None
    assert _canon(repo["expected_dsl"]) == adapted, "expected_dsl дрейфует от эталона"
