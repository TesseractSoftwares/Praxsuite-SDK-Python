"""Reads and writes table rows. Reached through the client: ``prax.data``.

Every call is authorised twice on the server: the credential (or the signed-in user's role) must
be scoped to the table, and any row filter on that scope is applied on top of your conditions. A
client cannot widen either, which is why this SDK exposes no way to try.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Iterable, Iterator, Mapping, Sequence

from . import filters as f
from . import routes
from .errors import PraxError, PraxValidationError
from .result import MutationResult, Page, parse_mutation, parse_page

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .client import Praxsuite

__all__ = ["PraxData", "Query", "NATIVE_COLUMNS"]

#: The root table's alias inside a request. The gateway addresses tables through ``refs``, so the
#: alias is an implementation detail callers never see.
ROOT = "t"

#: Columns the backend fills in and rejects if a client supplies them.
NATIVE_COLUMNS = frozenset({
    "ID", "CREATEDDATE", "CREATEDBY", "UPDATEDDATE", "UPDATEDBY", "POSITION",
})

AGGREGATES = frozenset({"count", "sum", "avg", "min", "max"})

#: Enforced by the gateway to stop injection through the alias.
_ALIAS_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")


def _find_native(keys: Iterable[Any]) -> list[str]:
    return [str(k) for k in keys if str(k).upper() in NATIVE_COLUMNS]


class Query:
    """A chained query. Build it, then call a terminal method.

    ::

        page = (prax.data.table("Scores")
                .select("Player", "Score")
                .where(f.gte("Score", 100))
                .order_by("Score", ascending=False)
                .limit(20)
                .fetch())

    Nothing is sent until a terminal method (``fetch``, ``first``, ``count``, ``exists``,
    ``all``) is called, so building a query costs nothing. Iterating the query is the same as
    iterating ``fetch()``.
    """

    def __init__(self, data: "PraxData", table: str) -> None:
        if not isinstance(table, str) or not table.strip():
            raise PraxValidationError("INVALID_REQUEST", "A table name or id is required.")
        self._data = data
        self._table = table.strip()
        self._select: list[Any] = []
        self._where: list[Mapping[str, Any]] = []
        self._order: list[dict[str, str]] = []
        self._group: list[str] = []
        self._having: list[Mapping[str, Any]] = []
        self._extra_refs: dict[str, str] = {}
        self._limit: int | None = None
        self._offset: int | None = None
        self._include_total = False

    # ── building ────────────────────────────────────────────────────────────

    def select(self, *columns: str) -> "Query":
        """Restricts the columns returned.

        Worth doing on wide tables: the gateway meters egress against the workspace's plan, so
        fetching columns you discard costs real allowance.
        """
        for c in columns:
            if isinstance(c, str) and c.strip():
                self._select.append(c.strip())
        return self

    def include(self, related_table: str, columns: Sequence[str] = (),
                limit: int | None = None) -> "Query":
        """Includes rows from a related table as a nested list on each row."""
        if not related_table or not related_table.strip():
            raise PraxValidationError("INVALID_REQUEST", "A related table name or id is required.")
        alias = f"r{len(self._extra_refs) + 1}"
        self._extra_refs[alias] = related_table.strip()

        relation: dict[str, Any] = {"table": alias}
        picked = [c.strip() for c in columns if isinstance(c, str) and c.strip()]
        if picked:
            relation["select"] = picked
        if limit is not None:
            relation["limit"] = limit
        self._select.append(relation)
        return self

    def where(self, *conditions: Mapping[str, Any], **equals: Any) -> "Query":
        """Adds conditions. Repeated calls and multiple arguments are ANDed.

        Keyword arguments are a shorthand for equality, which covers most real queries::

            .where(Season=3, Mode="ranked")
            .where(f.gte("Score", 100))
        """
        for condition in conditions:
            if condition:
                self._where.append(condition)
        for column, value in equals.items():
            self._where.append(f.eq(column, value))
        return self

    def order_by(self, column: str, ascending: bool = True) -> "Query":
        if not column or not column.strip():
            raise PraxValidationError("INVALID_REQUEST", "A column name is required.")
        self._order.append({"field": column.strip(), "dir": "asc" if ascending else "desc"})
        return self

    def limit(self, n: int) -> "Query":
        # The gateway clamps limit up to a minimum of 1, so 0 never means "no rows".
        self._limit = max(1, int(n))
        return self

    def offset(self, n: int) -> "Query":
        self._offset = max(0, int(n))
        return self

    def with_total_count(self) -> "Query":
        """Asks for the total match count alongside the page.

        Off by default: it costs the server a second counting pass.
        """
        self._include_total = True
        return self

    def group_by(self, *columns: str) -> "Query":
        for c in columns:
            if isinstance(c, str) and c.strip():
                self._group.append(c.strip())
        return self

    def having(self, *conditions: Mapping[str, Any]) -> "Query":
        """Conditions applied after grouping. Built the same way as ``where``."""
        for condition in conditions:
            if condition:
                self._having.append(condition)
        return self

    def aggregate(self, fn: str, column: str, alias: str) -> "Query":
        """Adds an aggregate column, e.g. ``aggregate("sum", "Score", "total_score")``.

        Aggregations are disabled on a table scope by default, so a 403 here is a workspace
        setting to change, not a mistake in the query.
        """
        normalized = (fn or "").strip().lower()
        if normalized not in AGGREGATES:
            raise PraxValidationError(
                "INVALID_REQUEST",
                f"Unsupported aggregate {fn!r}. The gateway accepts "
                f"{', '.join(sorted(AGGREGATES))}.",
            )
        if not _ALIAS_RE.match(alias or ""):
            raise PraxValidationError(
                "INVALID_REQUEST",
                f"Invalid aggregate alias {alias!r}. Use letters, digits and underscore, "
                f"starting with a letter.",
            )
        self._select.append({
            "field": (column or "").strip() or "*", "fn": normalized, "alias": alias.strip(),
        })
        return self

    # ── terminal ────────────────────────────────────────────────────────────

    def fetch(self) -> Page:
        """Runs the query and returns one page."""
        return parse_page(self._data.execute(self.build()))

    def first(self) -> dict[str, Any] | None:
        """The first matching row, or None when nothing matched.

        An empty result is not an error - most callers want to branch on it.
        """
        saved, self._limit = self._limit, 1
        try:
            return self.fetch().first
        finally:
            self._limit = saved

    def exists(self) -> bool:
        return self.first() is not None

    def count(self) -> int:
        """The number of matching rows, ignoring limit and offset.

        Implemented as ``includeTotalCount`` plus a one-row fetch: the gateway clamps limit up to
        a minimum of 1, so a zero-row request is not possible and asking for one silently returns
        a row.
        """
        saved = (self._limit, self._offset, self._include_total)
        self._limit, self._offset, self._include_total = 1, None, True
        try:
            page = self.fetch()
        finally:
            self._limit, self._offset, self._include_total = saved

        if page.total is None:
            raise PraxError(
                "TOTAL_COUNT_UNAVAILABLE",
                "The gateway returned no total count. Aggregations are probably disabled on this "
                "table's scope - enable them in the workspace's API Gateway settings, or use "
                "aggregate('count', '*', 'n').",
            )
        return page.total

    def all(self, page_size: int = 200, max_rows: int | None = None) -> list[dict[str, Any]]:
        """Pages through every matching row.

        Each page is a separate request, and the gateway may clamp ``page_size`` below what you
        asked for - ``meta.limit`` is read back rather than assumed, or a clamp would turn this
        into an infinite loop re-reading the same rows.
        """
        rows: list[dict[str, Any]] = []
        saved = (self._limit, self._offset)
        try:
            offset = self._offset or 0
            while True:
                self._limit, self._offset = max(1, page_size), offset
                page = self.fetch()
                rows.extend(page.rows)

                if max_rows is not None and len(rows) >= max_rows:
                    return rows[:max_rows]
                # An empty page, or one short of the limit the server actually applied, is the end.
                step = page.limit or len(page.rows)
                if not page.rows or len(page.rows) < step:
                    return rows
                offset += len(page.rows)
        finally:
            self._limit, self._offset = saved

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.fetch().rows)

    def build(self) -> dict[str, Any]:
        """The request body this query will send.

        Public because seeing it is the fastest way to understand a 400, and because the tests
        assert on it.
        """
        refs = {ROOT: self._data.resolve_table(self._table)}
        for alias, name in self._extra_refs.items():
            refs[alias] = self._data.resolve_table(name)

        query: dict[str, Any] = {"from": ROOT}
        if self._select:
            query["select"] = self._select
        if self._where:
            query["where"] = self._where
        if self._order:
            query["orderBy"] = self._order
        if self._group:
            query["groupBy"] = self._group
        if self._having:
            query["having"] = self._having
        if self._limit is not None:
            query["limit"] = self._limit
        if self._offset is not None:
            query["offset"] = self._offset

        request: dict[str, Any] = {"refs": refs, "query": query}
        # includeTotalCount sits BESIDE query, not inside it. Nesting it is silently ignored and
        # the total then comes back absent forever.
        if self._include_total:
            request["includeTotalCount"] = True
        return request


class PraxData:
    """Table reads and writes."""

    def __init__(self, client: "Praxsuite") -> None:
        self._client = client

    def table(self, name_or_id: str) -> Query:
        """Starts a query against a table, by name or id."""
        return Query(self, name_or_id)

    # ── writes ──────────────────────────────────────────────────────────────

    def insert(self, table: str, values: Mapping[str, Any]) -> MutationResult:
        """Inserts one row.

        Do not set an ownership column yourself. A column carrying a DefaultValueTemplate is
        stamped from the caller's verified token and the gateway rejects a request that supplies
        it - that rejection is the anti-tamper guarantee, so working around it defeats the
        isolation it provides.
        """
        if not values:
            raise PraxValidationError("INVALID_REQUEST",
                                      "insert() needs at least one column to set.")
        self._reject_native(values.keys(), "insert")
        return self._mutate(table, {
            "type": "insert", "table": ROOT, "values": [dict(values)], "returning": True,
        })

    def insert_many(self, table: str, rows: Sequence[Mapping[str, Any]]) -> MutationResult:
        """Inserts several rows in one request."""
        values = [dict(r) for r in rows if r]
        if not values:
            raise PraxValidationError("INVALID_REQUEST",
                                      "insert_many() needs at least one non-empty row.")
        for row in values:
            self._reject_native(row.keys(), "insert")
        return self._mutate(table, {
            "type": "insert", "table": ROOT, "values": values, "returning": True,
        })

    def update(
        self, table: str, values: Mapping[str, Any], *conditions: Mapping[str, Any]
    ) -> MutationResult:
        """Updates every row matching ``conditions``.

        The conditions are mandatory. The gateway rejects an unscoped update, and refusing here
        means the mistake surfaces while you are writing the code rather than as a 400 in
        production. Conditions are positional so an update cannot be written without them by
        accident.
        """
        if not values:
            raise PraxValidationError("INVALID_REQUEST",
                                      "update() needs at least one column to set.")
        if not conditions:
            raise PraxValidationError(
                "UNSCOPED_MUTATION",
                "update() requires conditions. An update with no WHERE would target every row "
                "you can reach; pass filters, or use update_by_id().",
            )
        self._reject_native(values.keys(), "update")
        return self._mutate(table, {
            "type": "update", "table": ROOT, "set": dict(values), "where": list(conditions),
        })

    def update_by_id(self, table: str, row_id: str, values: Mapping[str, Any]) -> MutationResult:
        if not row_id or not str(row_id).strip():
            raise PraxValidationError("INVALID_REQUEST", "update_by_id() needs a row id.")
        return self.update(table, values, f.eq("ID", str(row_id).strip()))

    def delete(self, table: str, *conditions: Mapping[str, Any]) -> MutationResult:
        """Deletes every row matching ``conditions``. Mandatory, for the same reason as update."""
        if not conditions:
            raise PraxValidationError(
                "UNSCOPED_MUTATION",
                "delete() requires conditions. A delete with no WHERE would remove every row you "
                "can reach; pass filters, or use delete_by_id().",
            )
        return self._mutate(table, {"type": "delete", "table": ROOT, "where": list(conditions)})

    def delete_by_id(self, table: str, row_id: str) -> MutationResult:
        if not row_id or not str(row_id).strip():
            raise PraxValidationError("INVALID_REQUEST", "delete_by_id() needs a row id.")
        return self.delete(table, f.eq("ID", str(row_id).strip()))

    def upsert(
        self, table: str, values: Mapping[str, Any], row_id: str | None = None
    ) -> MutationResult:
        """Updates when ``row_id`` is given, inserts otherwise."""
        if row_id and str(row_id).strip():
            return self.update_by_id(table, row_id, values)
        return self.insert(table, values)

    # ── plumbing ────────────────────────────────────────────────────────────

    def execute(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Sends a hand-built PraxQL request. The escape hatch for shapes the builder misses."""
        if not request:
            raise PraxValidationError("INVALID_REQUEST", "A request body is required.")
        url = routes.query(self._client.base_url, self._client.workspace_id)
        # Reads are safe to retry; a mutation is not.
        retry_safe = "mutation" not in request
        return self._client.send("POST", url, request, retry_safe=retry_safe)

    def resolve_table(self, name_or_id: str) -> str:
        """Resolves a table name to whatever the gateway addresses it by.

        A seam, so a future name-to-id lookup does not change every call site.
        """
        return name_or_id.strip()

    def _mutate(self, table: str, mutation: Mapping[str, Any]) -> MutationResult:
        body = self.execute({
            "refs": {ROOT: self.resolve_table(table)}, "mutation": dict(mutation),
        })
        return parse_mutation(body)

    @staticmethod
    def _reject_native(keys: Iterable[Any], verb: str) -> None:
        offending = _find_native(keys)
        if offending:
            raise PraxValidationError(
                "INVALID_REQUEST",
                f"The backend maintains {', '.join(offending)} - remove "
                f"{'it' if len(offending) == 1 else 'them'} from the {verb}.",
            )
