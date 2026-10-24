"""Explain, in plain words, why stored roles did not match (D-090).

The stored decision already carries the reasons (`match_reason`, a JSON array of tokens such
as `exclude:family:none` or `review:seniority_unclear`). A new operator's first experience is
often "nothing matched", which looks the same as "the product is broken"; this module turns
those tokens into a short, honest summary so the difference is visible.

It only reads and counts. It never re-decides anything, and it never changes a role's state.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass

# Ordered: the first matching prefix wins, so the summary reads in the order an operator
# would naturally check their own scope.
_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("exclude:family:none", "the title named no role family we recognise"),
    ("exclude:family:", "the role family is outside the ones you chose"),
    ("exclude:employment_type:", "the employment type is outside the ones you chose"),
    ("exclude:seniority:", "the title reads as senior or experienced hire"),
    ("exclude:title:", "a title exclusion you set"),
    ("exclude:location:", "the location is outside your scope"),
    ("exclude:", "another exclusion rule"),
)


@dataclass(frozen=True)
class ExclusionSummary:
    label: str
    count: int


def _category_of(reason: str) -> str:
    for prefix, label in _CATEGORIES:
        if reason == prefix or reason.startswith(prefix):
            return label
    return "another exclusion rule"


def summarise_exclusions(reasons: tuple[str, ...] | list[str]) -> tuple[ExclusionSummary, ...]:
    """Aggregate stored exclusion reasons into ordered, countable explanations.

    Each role contributes its leading exclusion (the first `exclude:` token), so the counts
    add up to the number of excluded roles rather than to the number of rules that fired.
    """
    counts: Counter[str] = Counter()
    for raw in reasons:
        try:
            tokens = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(tokens, list):
            continue
        leading = next(
            (str(token) for token in tokens if str(token).startswith("exclude:")),
            None,
        )
        if leading is None:
            continue
        counts[_category_of(leading)] += 1
    order = {label: index for index, (_prefix, label) in enumerate(_CATEGORIES)}
    return tuple(
        ExclusionSummary(label=label, count=count)
        for label, count in sorted(
            counts.items(), key=lambda item: (-item[1], order.get(item[0], len(order)))
        )
    )


__all__ = ["ExclusionSummary", "summarise_exclusions"]
