"""Where conditions.

Only the operators the gateway's PraxQL parser accepts are exposed::

    eq neq gt gte lt lte like ilike in is between contains textsearch

``starts_with`` and ``ends_with`` exist as conveniences but compile down to ``like`` with the
wildcard already applied, and ``is_null``/``is_not_null`` compile to ``is``/``neq`` against null.
Nothing here sends an operator the server would reject - offering one would only produce a 400 at
runtime, which is strictly worse than not offering it.

Every function returns a plain dict, so a caller can build a condition by hand if they need to.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Sequence

from .errors import PraxValidationError

__all__ = [
    "eq", "neq", "gt", "gte", "lt", "lte", "like", "ilike", "contains", "text_search",
    "starts_with", "ends_with", "is_null", "is_not_null", "in_", "between", "any_of", "all_of",
    "SUPPORTED_OPERATORS",
]

#: The complete set the gateway implements. Anything else is rejected at parse time.
SUPPORTED_OPERATORS = frozenset({
    "eq", "neq", "gt", "gte", "lt", "lte", "like", "ilike", "in", "is", "between",
    "contains", "textsearch",
})

#: A single where condition, as the gateway expects it on the wire.
Condition = Dict[str, Any]


def _field(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise PraxValidationError("INVALID_REQUEST", "A column name is required.")
    return name.strip()


def _simple(field: str, op: str, value: Any) -> Condition:
    return {"field": _field(field), "op": op, "value": value}


def eq(field: str, value: Any) -> Condition:
    return _simple(field, "eq", value)


def neq(field: str, value: Any) -> Condition:
    return _simple(field, "neq", value)


def gt(field: str, value: Any) -> Condition:
    return _simple(field, "gt", value)


def gte(field: str, value: Any) -> Condition:
    return _simple(field, "gte", value)


def lt(field: str, value: Any) -> Condition:
    return _simple(field, "lt", value)


def lte(field: str, value: Any) -> Condition:
    return _simple(field, "lte", value)


def like(field: str, pattern: str) -> Condition:
    """SQL LIKE, case-sensitive. You supply the wildcards."""
    return _simple(field, "like", pattern)


def ilike(field: str, pattern: str) -> Condition:
    """Case-insensitive LIKE."""
    return _simple(field, "ilike", pattern)


def contains(field: str, text: str) -> Condition:
    """Substring match, no wildcards needed."""
    return _simple(field, "contains", text)


def text_search(field: str, q: str) -> Condition:
    """Full-text search over the column."""
    return _simple(field, "textsearch", q)


def starts_with(field: str, value: str) -> Condition:
    """Prefix match. Compiles to ``like 'value%'`` - there is no startsWith operator."""
    return _simple(field, "like", f"{value}%")


def ends_with(field: str, value: str) -> Condition:
    """Suffix match. Compiles to ``like '%value'``."""
    return _simple(field, "like", f"%{value}")


def is_null(field: str) -> Condition:
    """``field IS NULL``. The gateway's ``is`` operator only tests for null."""
    return _simple(field, "is", None)


def is_not_null(field: str) -> Condition:
    """``field IS NOT NULL``. Compiles to ``neq null``."""
    return _simple(field, "neq", None)


def in_(field: str, values: Iterable[Any]) -> Condition:
    """``field IN (...)``.

    Trailing underscore because ``in`` is a keyword. At least one value is required: an empty IN
    matches nothing, which is almost never what a caller means and is silent when it happens.
    """
    items = list(values)
    if not items:
        raise PraxValidationError(
            "INVALID_REQUEST",
            f"in_({field!r}, []) needs at least one value. An empty IN matches nothing - "
            f"omit the filter instead.",
        )
    return _simple(field, "in", items)


def between(field: str, low: Any, high: Any) -> Condition:
    """``field BETWEEN low AND high``, inclusive."""
    return _simple(field, "between", [low, high])


def _group(key: str, filters: Sequence[Mapping[str, Any]]) -> Condition:
    items = [f for f in filters if f]
    if not items:
        raise PraxValidationError("INVALID_REQUEST", f"{key}_of() needs at least one filter.")
    return {key: list(items)}


def any_of(*filters: Mapping[str, Any]) -> Condition:
    """Matches when any child matches."""
    return _group("or", filters)


def all_of(*filters: Mapping[str, Any]) -> Condition:
    """Matches when every child matches.

    Top-level conditions are already ANDed, so this is only needed to nest an AND group inside an
    ``any_of``.
    """
    return _group("and", filters)
