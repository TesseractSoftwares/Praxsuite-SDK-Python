# Praxsuite SDK for Python

Auth, queries and server-authoritative endpoints for your Praxsuite workspace.

**Zero dependencies.** Everything here is standard library, so `pip install praxsuite` adds
nothing to your lock file — which is the difference between "usable" and "argued about" in a
locked service image, a Lambda bundle, or a notebook someone else has to reproduce.

```python
from praxsuite import Praxsuite, filters as f

prax = Praxsuite("your-workspace-id", "sk_live_...")

page = (prax.data.table("Orders")
        .select("ID", "Total", "Status")
        .where(f.gte("Total", 100), Status="paid")
        .order_by("Total", ascending=False)
        .limit(50)
        .fetch())

for row in page:
    print(row["ID"], row["Total"])
```

---

## Install

```bash
pip install praxsuite
```

Python 3.9+. Fully typed, ships `py.typed`.

## Configure

```python
prax = Praxsuite("your-workspace-id", "sk_live_...")
```

Or from the environment, which is what you want in a deployed service:

```bash
export PRAXSUITE_WORKSPACE_ID=...
export PRAXSUITE_API_KEY=sk_live_...
export PRAXSUITE_BASE_URL=https://gateway.praxsuite.com   # optional
```

```python
prax = Praxsuite()
```

Both values come from your workspace under **API Gateway**. The client is cheap to construct and
safe to share between threads; one instance per workspace per process is the usual arrangement.

### Which key to use

Unlike the game-engine SDKs, **a secret key is usually correct here** — a Python process is
normally a server you control, and that is what `sk_live_` is for.

The exception is code a user can read: a published notebook, a Pyodide or PyScript page, a
desktop app, a shared Jupyter kernel. Pass `client_side=True` and the SDK refuses a secret key
outright rather than trusting you to remember:

```python
prax = Praxsuite(workspace_id, "pk_live_...", client_side=True)
```

Two things to know regardless:

**Every credential carries both halves.** There is no publishable-only credential, and
`/{workspace}/auth/config` is unauthenticated — so the workspace id alone yields the publishable
key, and whatever tables you scope to that credential are reachable by anyone who has it. Scope
narrowly and keep the rest on a credential your client never sees.

**Anything a caller must not influence belongs in an endpoint.** The client asks for an outcome;
the server decides it.

```python
result = prax.endpoints.call("submit-score", {"score": score})
```

---

## Querying

```python
page = (prax.data.table("Scores")
        .select("Player", "Score")
        .where(f.gte("Score", 100))
        .where(Season=3)                    # kwargs are shorthand for equality
        .order_by("Score", ascending=False)
        .limit(20)
        .fetch())

print(len(page), "of", page.total)          # total is None unless you asked for it
```

Nothing is sent until a terminal method — `fetch()`, `first()`, `count()`, `exists()`, `all()`.
A `Page` is iterable and indexable, so `for row in page` and `page[0]` both work.

To page through everything:

```python
for row in prax.data.table("Scores").where(Season=3).all(page_size=200):
    ...
```

`all()` reads back the limit the server actually applied rather than assuming yours was honoured
— a table scope can clamp it, and assuming otherwise turns pagination into an infinite loop.

`filters` exposes **only** operators the gateway implements: `eq neq gt gte lt lte like ilike in
is between contains textsearch`. The friendly-sounding ones compile down — `starts_with` becomes
`like "value%"`, `is_null` becomes `is null`. Offering `startsWith` as an operator would only
produce a 400 at runtime.

`page.total` is `None` when a count was not requested, not `0`, so "no rows matched" stays
distinguishable from "nobody asked". Call `.with_total_count()` or `.count()` for a real number.

### Writes

```python
prax.data.insert("Saves", {"Slot": 1, "Level": 12})
prax.data.update_by_id("Saves", row_id, {"Level": 13})
prax.data.update("Saves", {"Level": 13}, f.eq("Slot", 1))
prax.data.delete("Saves", f.eq("Slot", 1))
```

`update()` and `delete()` take their conditions **positionally and require at least one**, so an
unscoped write cannot be written by accident. Do not set an ownership column yourself — see below.

## Errors

Exceptions, since that is what Python code expects:

```python
from praxsuite import PraxRateLimitError, PraxQuotaExceededError, PraxError

try:
    page = prax.data.table("Scores").fetch()
except PraxRateLimitError:
    ...          # transient — back off and retry
except PraxQuotaExceededError:
    ...          # NOT transient — the workspace owner has to upgrade
except PraxError as exc:
    print(exc.code, exc.status, exc.details)
```

Both of those arrive as HTTP 429 and mean opposite things, which is exactly why they are separate
types. `PraxValidationError` is also a `ValueError`, so code already catching `ValueError` for bad
arguments keeps working.

Reads are retried automatically with backoff on transient failures. Writes and endpoint calls are
not: retrying a failed insert is how you get two rows.

## Async

For FastAPI and anything else on an event loop:

```python
from praxsuite.aio import AsyncPraxsuite

prax = AsyncPraxsuite("workspace-id", "sk_live_...")
page = await prax.query("Orders").where(Status="paid").limit(50).fetch()
```

Being precise about what this is, because a wrapper that hid it would be misleading: every call
runs the blocking request on a worker thread via `asyncio.to_thread`. It is **not** native async
I/O. What that buys you is the thing that matters in a web service — your event loop is never
blocked, so one slow gateway call does not stall every other request. What it does not buy you is
a native async client's memory profile at very high concurrency, since each in-flight call holds
a thread.

That trade exists to keep the dependency count at zero. If your workload is thousands of
concurrent gateway calls rather than dozens, use the sync client from a thread pool you size
yourself.

Builder methods stay synchronous (building sends nothing); only terminals are awaited.

## Logging

Standard `logging`, under the `praxsuite` logger, with credentials scrubbed from every record —
including ones passed as lazy `%s` arguments.

```python
import logging
logging.getLogger("praxsuite").setLevel(logging.DEBUG)   # logs request/response bodies
```

Leave that at default in production.

---

## Per-player isolation needs TWO settings

This is the most damaging misconfiguration in the platform, so it is worth stating plainly.

| Setting | Where | Value | Covers |
|---|---|---|---|
| Row filter | the role's **table** scope | `__SELF__` | select, update, delete |
| Default value template | the ownership **column**'s scope | `{{claim:sub}}` | insert |

The row filter cannot cover inserts, because an insert has no WHERE clause to constrain. Configure
only the row filter and **inserts succeed with a null owner, which the filter then hides** — the
user saves a record and cannot read it back, with no error raised anywhere.

The default value template also blocks the client from setting the column at all, which is what
makes ownership untamperable. That rejection is the guarantee; do not work around it.

---

## Conformance is the law

Praxsuite has SDKs in several languages. Where they touch the gateway they do **not** get to
disagree. A single normative contract — the internal `Praxsuite-SDK-Conformance` repository —
defines the shared behaviour, and every SDK implements it identically:

1. **The contract is normative.** Where this SDK and the contract differ, this SDK is wrong.
2. **Every rule cites the backend source it derives from.** No rule rests on memory.
3. **Every rule exists because getting it wrong fails silently.** Wrong data, not an error.
4. **A behaviour change is a contract change first.** Not an implementation detail.

The contract is internal and deliberately has no public repository. Its value is that it is
authoritative for us, not that it is browsable — and it cites backend internals that are not ours
to publish. Everything a consumer of this SDK needs is in this README.

What it pins down, and why each one earned its place:

- **Operators.** Only the thirteen the parser accepts. A friendlier name is a runtime 400.
- **`meta.total`, never `meta.totalCount`.** Reading the wrong name returns nothing and reports
  zero, silently, forever. One SDK shipped that for months.
- **Three response envelopes.** `/query` is bare, `/auth/*` nests under `.data`, `/files` errors
  are a bare string. Assuming one shape mis-parses the other two.
- **`limit` is clamped up to a minimum of 1.** A zero-row count request quietly returns a row.
- **Unscoped updates and deletes refused before sending**, synchronously.
- **No client-supplied identity parameter.** The server ignores it, so it would read as a
  security boundary while being decorative.

The suite is offline — no workspace, no network, no credentials:

```bash
pip install -e ".[dev]"
pytest
```

## API surface

| | |
|---|---|
| `Praxsuite(...)` | `workspace_id`, `credential`, `base_url`, `client_side`, `timeout`, `max_attempts` |
| `prax.auth` | `register` `login` `logout` `refresh` `ensure_fresh_session` `forgot_password` `verify_reset_code` `reset_password` `resend_confirmation` `get_config` `on_session_change` |
| `prax.data` | `table(name)` → query builder; `insert` `insert_many` `update` `update_by_id` `delete` `delete_by_id` `upsert` `execute` |
| `prax.endpoints` | `call` `get` |
| `prax.schema` | `tables` `table` `columns` `has_table` |
| `filters` | `eq neq gt gte lt lte like ilike contains text_search starts_with ends_with is_null is_not_null in_ between any_of all_of` |

## Licence

[Praxsuite Open SDK Licence](LICENSE) — source-available, not OSI open source.

Free to use in anything you build, including products you sell. Free to fork, modify and publish
your changes. Not free to resell as an SDK, or to point at a competing backend.

Derived from the Praxsuite SDK — <https://praxsuite.com>
