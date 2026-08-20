"""An async face for the sync client, for FastAPI and anything else on an event loop.

Be clear about what this is, because the distinction matters and a wrapper that hides it would be
misleading: every call runs the blocking ``urllib`` request on a worker thread via
``asyncio.to_thread``. It is **not** native async I/O.

What that buys you is the thing that actually matters in an async web service - your event loop
is never blocked, so one slow gateway call does not stall every other request being served. What
it does not buy you is the memory profile of a native async client at very high concurrency,
since each in-flight call occupies a thread from the default executor.

Chosen over depending on httpx or aiohttp so that ``pip install praxsuite`` still pulls in
nothing. If your workload is thousands of concurrent gateway calls rather than dozens, use the
sync client from your own thread pool sized to suit, or reach for the TypeScript SDK in a Node
sidecar.

::

    from praxsuite.aio import AsyncPraxsuite

    prax = AsyncPraxsuite("workspace-id", "sk_live_...")
    page = await prax.query("Orders").where(Status="paid").limit(50).fetch()
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any, Callable, Mapping, Sequence, TypeVar

from .auth import RegistrationResult, Session
from .client import Praxsuite
from .data import Query
from .result import MutationResult, Page

__all__ = ["AsyncPraxsuite", "AsyncQuery"]


T = TypeVar("T")


async def _off_thread(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    return await asyncio.to_thread(functools.partial(fn, *args, **kwargs))


class AsyncQuery:
    """Wraps a :class:`~praxsuite.data.Query`; the builder methods are sync, the terminals await.

    Building a query touches no I/O, so only the terminal methods need awaiting. The builder
    methods return ``self`` so chaining reads the same as the sync version.
    """

    def __init__(self, query: Query) -> None:
        self._query = query

    def select(self, *columns: str) -> "AsyncQuery":
        self._query.select(*columns)
        return self

    def include(self, related_table: str, columns: Sequence[str] = (),
                limit: int | None = None) -> "AsyncQuery":
        self._query.include(related_table, columns, limit)
        return self

    def where(self, *conditions: Mapping[str, Any], **equals: Any) -> "AsyncQuery":
        self._query.where(*conditions, **equals)
        return self

    def order_by(self, column: str, ascending: bool = True) -> "AsyncQuery":
        self._query.order_by(column, ascending)
        return self

    def limit(self, n: int) -> "AsyncQuery":
        self._query.limit(n)
        return self

    def offset(self, n: int) -> "AsyncQuery":
        self._query.offset(n)
        return self

    def with_total_count(self) -> "AsyncQuery":
        self._query.with_total_count()
        return self

    def group_by(self, *columns: str) -> "AsyncQuery":
        self._query.group_by(*columns)
        return self

    def having(self, *conditions: Mapping[str, Any]) -> "AsyncQuery":
        self._query.having(*conditions)
        return self

    def aggregate(self, fn: str, column: str, alias: str) -> "AsyncQuery":
        self._query.aggregate(fn, column, alias)
        return self

    def build(self) -> dict[str, Any]:
        """The request body. Sync, because building sends nothing."""
        return self._query.build()

    async def fetch(self) -> Page:
        return await _off_thread(self._query.fetch)

    async def first(self) -> dict[str, Any] | None:
        return await _off_thread(self._query.first)

    async def exists(self) -> bool:
        return await _off_thread(self._query.exists)

    async def count(self) -> int:
        return await _off_thread(self._query.count)

    async def all(self, page_size: int = 200,
                  max_rows: int | None = None) -> list[dict[str, Any]]:
        # One thread for the whole pagination loop rather than one per page: the loop is
        # sequential anyway, so hopping threads per page would only add overhead.
        return await _off_thread(self._query.all, page_size, max_rows)


class AsyncAuth:
    """Async face for :class:`~praxsuite.auth.PraxAuth`."""

    def __init__(self, client: Praxsuite) -> None:
        self._client = client

    @property
    def session(self) -> Session | None:
        return self._client.auth.session

    @property
    def is_signed_in(self) -> bool:
        return self._client.auth.is_signed_in

    def on_session_change(self, callback: Callable[[Session | None], None]) -> None:
        self._client.auth.on_session_change(callback)

    async def register(self, email: str, password: str, **extra: Any) -> RegistrationResult:
        return await _off_thread(self._client.auth.register, email, password, **extra)

    async def login(self, email: str, password: str) -> Session:
        return await _off_thread(self._client.auth.login, email, password)

    async def logout(self) -> None:
        await _off_thread(self._client.auth.logout)

    async def refresh(self) -> Session:
        return await _off_thread(self._client.auth.refresh)

    async def forgot_password(self, email: str) -> None:
        await _off_thread(self._client.auth.forgot_password, email)

    async def verify_reset_code(self, email: str, code: str) -> None:
        await _off_thread(self._client.auth.verify_reset_code, email, code)

    async def reset_password(self, email: str, code: str, new_password: str) -> None:
        await _off_thread(self._client.auth.reset_password, email, code, new_password)

    async def resend_confirmation(self, email: str) -> None:
        await _off_thread(self._client.auth.resend_confirmation, email)

    async def get_config(self) -> dict[str, Any]:
        return await _off_thread(self._client.auth.get_config)


class AsyncData:
    """Async face for :class:`~praxsuite.data.PraxData`.

    Argument validation still happens synchronously inside the wrapped call, so an unscoped
    ``update`` raises when awaited. Note that in Python a coroutine that is never awaited raises
    a RuntimeWarning rather than failing silently, so a dropped guardrail is still visible.
    """

    def __init__(self, client: Praxsuite) -> None:
        self._data = client.data

    def table(self, name_or_id: str) -> AsyncQuery:
        return AsyncQuery(self._data.table(name_or_id))

    async def insert(self, table: str, values: Mapping[str, Any]) -> MutationResult:
        return await _off_thread(self._data.insert, table, values)

    async def insert_many(
        self, table: str, rows: Sequence[Mapping[str, Any]]
    ) -> MutationResult:
        return await _off_thread(self._data.insert_many, table, rows)

    async def update(
        self, table: str, values: Mapping[str, Any], *conditions: Mapping[str, Any]
    ) -> MutationResult:
        return await _off_thread(self._data.update, table, values, *conditions)

    async def update_by_id(
        self, table: str, row_id: str, values: Mapping[str, Any]
    ) -> MutationResult:
        return await _off_thread(self._data.update_by_id, table, row_id, values)

    async def delete(self, table: str, *conditions: Mapping[str, Any]) -> MutationResult:
        return await _off_thread(self._data.delete, table, *conditions)

    async def delete_by_id(self, table: str, row_id: str) -> MutationResult:
        return await _off_thread(self._data.delete_by_id, table, row_id)

    async def upsert(
        self, table: str, values: Mapping[str, Any], row_id: str | None = None
    ) -> MutationResult:
        return await _off_thread(self._data.upsert, table, values, row_id)

    async def execute(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return await _off_thread(self._data.execute, request)


class AsyncEndpoints:
    def __init__(self, client: Praxsuite) -> None:
        self._endpoints = client.endpoints

    async def call(self, slug: str, body: Any = None) -> dict[str, Any]:
        return await _off_thread(self._endpoints.call, slug, body)

    async def get(self, slug: str) -> dict[str, Any]:
        return await _off_thread(self._endpoints.get, slug)


class AsyncSchema:
    def __init__(self, client: Praxsuite) -> None:
        self._schema = client.schema

    async def tables(self, force_reload: bool = False) -> dict[str, dict[str, Any]]:
        return await _off_thread(self._schema.tables, force_reload)

    async def table(self, name: str) -> dict[str, Any] | None:
        return await _off_thread(self._schema.table, name)

    async def columns(self, table: str) -> list[str]:
        return await _off_thread(self._schema.columns, table)

    async def has_table(self, name: str) -> bool:
        return await _off_thread(self._schema.has_table, name)


class AsyncPraxsuite:
    """Async client. Takes the same arguments as :class:`~praxsuite.client.Praxsuite`."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._sync = Praxsuite(*args, **kwargs)
        self.auth = AsyncAuth(self._sync)
        self.data = AsyncData(self._sync)
        self.endpoints = AsyncEndpoints(self._sync)
        self.schema = AsyncSchema(self._sync)

    @property
    def workspace_id(self) -> str:
        return self._sync.workspace_id

    @property
    def base_url(self) -> str:
        return self._sync.base_url

    @property
    def sync(self) -> Praxsuite:
        """The underlying sync client, for anything not wrapped here."""
        return self._sync

    def query(self, name_or_id: str) -> AsyncQuery:
        """Shorthand for ``prax.data.table``."""
        return self.data.table(name_or_id)

    async def execute(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return await self.data.execute(request)

    def __repr__(self) -> str:
        return f"Async{self._sync!r}"
