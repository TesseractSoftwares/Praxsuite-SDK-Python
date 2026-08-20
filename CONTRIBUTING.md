# Contributing

Contributions are welcome — bug reports, fixes, docs, samples.

Before anything else: **security issues do not go in the issue tracker.** See
[SECURITY.md](SECURITY.md).

## Licence, up front

This SDK is under the [Praxsuite Open SDK Licence](LICENSE) — source-available, not OSI open
source. You can use it free in anything you build, including products you sell, and you can fork
and modify it. You cannot resell the SDK itself or use it to build a competing backend platform.

By contributing, you license your contribution under the same terms and confirm you have the right
to submit it. You keep the copyright in what you wrote.

## Running the checks

```bash
pip install -e ".[dev]"
pytest
mypy
```

All three must be clean. The test suite is entirely offline — no workspace, no credentials, no
network — and covers the filter compiler, the query builder, the three response envelopes, error
classification, the credential guard and the log scrubber: the parts where a mistake produces
silently wrong data rather than a crash.

`pytest` runs with `filterwarnings = ["error"]`. That is deliberate: a coroutine that is never
awaited raises `RuntimeWarning`, which is exactly the class of mistake these tests exist to catch.

## Things worth knowing before you change code

- **Do not add a dependency.** The empty `dependencies` list is a feature, not an oversight — it
  is why this installs cleanly into a locked service image, a Lambda bundle, or someone else's
  notebook. If you think you need one, open an issue first. This is also why the async client uses
  `asyncio.to_thread` rather than httpx.
- **Guardrails validate before sending, and outside `async`.** In Python a coroutine's body does
  not run until awaited, so validation inside one is validation a caller can defer. Keep argument
  checks in the sync path.
- **Nothing may log a credential.** Everything goes through the `praxsuite` logger, whose filter
  scrubs keys, JWTs and password fields — including values passed as lazy `%s` arguments, which is
  the case that is easy to miss. Never use `print`.
- **The client is untrusted, even here.** Do not add an API that takes a caller-supplied identity.
  The server ignores it, so it would read as a security boundary while being decorative.
- **`filters` must never expose an operator the gateway lacks.** A friendlier-sounding name
  produces a runtime 400 on someone else's machine. `starts_with` compiles to `like`; that is the
  pattern to follow.
- **Read `meta.total`, never `meta.totalCount`.** The latter does not exist. Reading it returns
  nothing and reports zero, silently, forever.
- **Keep `mypy --strict` clean.** The package ships `py.typed`, so its annotations are a promise to
  consumers rather than decoration.

## Conformance

Behaviour shared across the Praxsuite SDKs is not a matter of local judgement — see
[the conformance section of the README](README.md#conformance-is-the-law). If you change how a
filter compiles, how a response is parsed, or how a credential is classified, that is a contract
change, not an implementation change.

## Style

Match the surrounding code. A few conventions the codebase holds to:

- Comments explain *why*, not *what*. If a line needs a comment to say what it does, rename
  something instead.
- Public API gets a docstring written for someone who has never seen Praxsuite.
- Error messages say what to do next, not just what went wrong.
- 100 columns.

## Pull requests

1. Fork, branch from `master`
2. Make the change, keeping `pytest` and `mypy` green
3. Add a test if you fixed a bug — it should fail before your fix
4. Update `CHANGELOG.md` under Unreleased
5. Open the PR describing what changed and why

Small, focused PRs get reviewed faster than large ones. If you are planning something substantial,
open an issue first so we can agree on the shape before you spend the time.

## Reporting a bug

Use the issue template. The two things that make a report actionable are the **exact error**
(every `PraxError` carries a stable `code` — include it) and a **minimal repro**. Python version,
SDK version and platform help too.
