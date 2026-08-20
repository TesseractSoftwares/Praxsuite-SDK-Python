"""Parses the gateway's three response shapes.

The gateway does not use one envelope, and an SDK that assumes it does mis-parses two of the
three:

* ``POST /{ws}/query`` - the body IS the result: ``{"data": [...], "meta": {...}}``
* ``POST /{ws}/auth/*`` - platform envelope: the payload is under ``.data``
* ``/{ws}/files/*``     - errors are ``{"error": "a bare string"}``, not an object
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping, Sequence

from .errors import PraxError, error_for

__all__ = ["Page", "MutationResult", "parse_page", "parse_mutation", "unwrap_envelope",
           "parse_error", "parse_json_or_none"]


@dataclass
class Page:
    """One page of rows, plus the metadata the gateway returned with it."""

    rows: list[dict[str, Any]] = field(default_factory=list)

    #: Total matching rows ignoring limit/offset, or ``None`` when it was not requested.
    #:
    #: ``None`` rather than 0 deliberately: "no rows matched" must stay distinguishable from
    #: "nobody asked for a count".
    total: int | None = None

    #: The limit the gateway ACTUALLY applied, after clamping to the table scope's maximum. Read
    #: this rather than assuming your requested limit was honoured.
    limit: int = 0
    offset: int = 0
    count: int = 0
    duration_ms: int = 0

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.rows)

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, index: int) -> dict[str, Any]:
        return self.rows[index]

    @property
    def has_more(self) -> bool:
        """True when another page exists.

        Falls back to a full page being a hint when no total was requested, since that is all the
        information available in that case.
        """
        if self.total is not None:
            return self.offset + len(self.rows) < self.total
        return self.limit > 0 and len(self.rows) >= self.limit

    @property
    def first(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None


@dataclass
class MutationResult:
    """The outcome of an insert, update or delete."""

    affected_rows: int = 0
    rows: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: int = 0

    @property
    def row(self) -> dict[str, Any] | None:
        """The single affected row, for the common one-row case."""
        return self.rows[0] if self.rows else None


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def parse_page(body: Mapping[str, Any]) -> Page:
    """Reads a ``/query`` select response."""
    meta = body.get("meta") or {}
    rows = body.get("data") or []
    if not isinstance(rows, list):
        rows = []

    # meta.total, NEVER meta.totalCount. Reading the wrong name returns nothing and reports zero,
    # silently, forever - one SDK shipped that for months before anyone noticed.
    raw_total = meta.get("total")

    return Page(
        rows=[r for r in rows if isinstance(r, dict)],
        total=None if raw_total is None else _as_int(raw_total),
        limit=_as_int(meta.get("limit")),
        offset=_as_int(meta.get("offset")),
        count=_as_int(meta.get("count"), len(rows)),
        duration_ms=_as_int(meta.get("durationMs")),
    )


def parse_mutation(body: Mapping[str, Any]) -> MutationResult:
    """Reads a ``/query`` mutation response."""
    meta = body.get("meta") or {}
    rows = body.get("data") or []
    if not isinstance(rows, list):
        rows = []
    return MutationResult(
        affected_rows=_as_int(body.get("affectedRows")),
        rows=[r for r in rows if isinstance(r, dict)],
        duration_ms=_as_int(meta.get("durationMs")),
    )


def unwrap_envelope(body: Mapping[str, Any]) -> dict[str, Any]:
    """Unwraps the platform envelope used by ``/auth/*``.

    A ``/query`` body also has a ``data`` key, but it is a LIST, so checking the type is what
    keeps this safe to call on either shape.
    """
    inner = body.get("data")
    if isinstance(inner, dict):
        return inner
    return dict(body)


def parse_json_or_none(text: str) -> Any:
    """Parses JSON, returning None instead of raising. Error bodies are not always JSON."""
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def parse_error(status: int, raw_body: str) -> PraxError:
    """Builds a typed error from a non-2xx body, handling all three shapes above."""
    code = ""
    message = ""
    details: Sequence[str] = ()

    if raw_body:
        parsed = parse_json_or_none(raw_body)
        if isinstance(parsed, dict):
            err = parsed.get("error")
            if isinstance(err, dict):
                code = str(err.get("code") or "")
                message = str(err.get("message") or "")
                if isinstance(err.get("details"), list):
                    details = [str(d) for d in err["details"]]
            elif isinstance(err, str):
                # The /files routes report a bare string here, not an object.
                message = err
            else:
                message = str(parsed.get("message") or "")
                if isinstance(parsed.get("errors"), list):
                    details = [str(d) for d in parsed["errors"]]
        else:
            # Not JSON at all - an HTML error page from an edge proxy, most likely.
            message = raw_body[:400]

    if not code:
        code = f"HTTP_{status}"
    if not message:
        message = _describe_status(status)

    return error_for(code, message, status, details, raw_body)


def _describe_status(status: int) -> str:
    return {
        400: "The gateway rejected the request as malformed.",
        401: "Not authenticated. The API key or session token is missing, expired, or does not "
             "belong to this workspace.",
        403: "Not authorised. This credential or role is not scoped for that table or operation.",
        404: "Not found. Check the workspace id, and that it exists on this gateway host - "
             "Praxsuite runs several independent tiers.",
        409: "Conflict. The row was changed by someone else, or a unique value is already taken.",
        413: "The request body is too large.",
        429: "Rate limited, or a plan allowance is exhausted. Check the error code to tell "
             "which - only one of them is worth retrying.",
        500: "The gateway failed to handle the request.",
        502: "The gateway is unreachable from the edge.",
        503: "The gateway is temporarily unavailable.",
        504: "The gateway timed out.",
    }.get(status, f"The gateway returned HTTP {status}.")
