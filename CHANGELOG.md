# Changelog

All notable changes to the Praxsuite SDK for Python.

## [1.0.0] - 2026-08-20

First release. Zero dependencies, Python 3.9+, fully typed.

3.9 is supported and tested, not merely declared: CI provisions a real 3.9 interpreter with uv and
runs the whole suite on it, alongside 3.13. `mypy` cannot target 3.9 any more, so it checks against
3.10 - the 3.9 guarantee comes from executing the code there, which is the stronger signal anyway.
No 3.10+ syntax is used, and every module defers its annotations.

### Added

- **`Praxsuite`** - sync client, cheap to construct and safe to share between threads. Reads
  `PRAXSUITE_WORKSPACE_ID`, `PRAXSUITE_API_KEY` and `PRAXSUITE_BASE_URL` when arguments are
  omitted, which is what a deployed service wants.
- **`praxsuite.aio.AsyncPraxsuite`** - async face for FastAPI and friends. Runs each call on a
  worker thread via `asyncio.to_thread`; documented plainly as thread-offloaded rather than native
  async I/O, because a wrapper that hid that would be misleading.
- **Auth** - register, login, logout, refresh, password reset, resend confirmation, and the
  unauthenticated `auth/config` read. A refresh is serialised with a lock, because the gateway
  retires the old refresh token as it issues the new one and two racing refreshes would leave the
  loser holding a retired token. Profile fields carry forward across a refresh.
- **Query builder** - select, where (conditions or `Column=value` kwargs), order, limit, offset,
  group/having, aggregates, related-table includes, plus `fetch` / `first` / `count` / `exists` /
  `all`. `all()` reads back the limit the server actually applied, so a scope clamp cannot turn
  pagination into an infinite loop.
- **Writes** - insert, insert_many, update, update_by_id, delete, delete_by_id, upsert. `update`
  and `delete` take conditions positionally and require at least one, so an unscoped write cannot
  be written by accident. Native columns the backend maintains are refused.
- **Endpoints** and **schema** reads.
- **Errors as exceptions**, with subclasses for the failures worth branching on.
  `PraxRateLimitError` and `PraxQuotaExceededError` are distinct types because both arrive as HTTP
  429 and only one is worth retrying. `PraxValidationError` is also a `ValueError`.
- **Credential guard** - `client_side=True` refuses an `sk_live_` key outright, for a published
  notebook, a Pyodide page or a desktop app. Server-side, a secret key is correct and allowed.
- **Log scrubbing** on the `praxsuite` logger, including credentials passed as lazy `%s`
  arguments.
- 98 offline tests and `mypy --strict` clean.

### Notes for anyone coming from another Praxsuite SDK

- Errors are **raised**, not returned - unlike the Godot SDK, where GDScript has no exceptions.
- `Page.total` is `None` when a count was not requested, so "no rows matched" stays
  distinguishable from "nobody asked".
- Python's `json` preserves `int`, so unlike Godot there is no float-cast trap on Int columns.
- A coroutine that is never awaited raises `RuntimeWarning` rather than failing silently, so a
  dropped guardrail on the async client is still visible. The test suite runs with
  `filterwarnings = ["error"]` to keep it that way.

### Not included

Publishing to PyPI. The GitHub release carries the wheel and sdist; a PyPI upload needs an API
token in the CI vault that does not exist yet.
