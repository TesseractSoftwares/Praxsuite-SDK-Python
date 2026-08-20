"""Reads the tables and columns this credential is allowed to see.

What comes back is filtered by scope. A table you have not granted access to simply is not
listed, and a column hidden from the credential is absent rather than empty - so this is also the
quickest way to tell a typo apart from a missing scope, which look identical in a 403.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import routes
from .result import unwrap_envelope

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .client import Praxsuite

__all__ = ["PraxSchema"]


class PraxSchema:
    """Schema reads, cached for the client's lifetime."""

    def __init__(self, client: "Praxsuite") -> None:
        self._client = client
        self._cache: dict[str, dict[str, Any]] | None = None

    def tables(self, force_reload: bool = False) -> dict[str, dict[str, Any]]:
        """Every visible table, keyed by name. Cached after the first call."""
        if self._cache is not None and not force_reload:
            return self._cache

        url = routes.schema(self._client.base_url, self._client.workspace_id)
        body = unwrap_envelope(self._client.send("GET", url, None, retry_safe=True))

        listed = body.get("tables")
        cache: dict[str, dict[str, Any]] = {}
        if isinstance(listed, list):
            for entry in listed:
                if isinstance(entry, dict):
                    cache[str(entry.get("name") or "")] = entry
        self._cache = cache
        return cache

    def table(self, name: str) -> dict[str, Any] | None:
        """One table's definition, or None when it is not visible to this credential."""
        return self.tables().get(name)

    def columns(self, table: str) -> list[str]:
        """The column names visible on a table."""
        definition = self.table(table) or {}
        listed = definition.get("columns")
        if not isinstance(listed, list):
            return []
        return [str(c.get("name") or "") for c in listed if isinstance(c, dict)]

    def has_table(self, name: str) -> bool:
        return self.table(name) is not None
