# Security Policy

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

Email **security@tesseractsoftwares.com** with:

- what the issue is, and what an attacker gains
- steps to reproduce, or a proof of concept
- the SDK version and Python version
- whether it affects the SDK, the Praxsuite gateway, or both

We aim to acknowledge within **2 business days** and to give you an assessment and a fix timeline
within **10 business days**.

If you would like credit in the release notes, say so and tell us how to name you. If you would
rather stay anonymous, that is fine too.

We will not take legal action against good-faith research that follows this policy: report
privately, do not access or modify data belonging to anyone else, and give us a reasonable window
to fix the issue before disclosing it.

## Supported versions

| Version | Supported |
|---|---|
| 1.x | ✅ |
| < 1.0 | ❌ |

## What counts as a vulnerability here

Unlike the game-engine SDKs, this one usually runs on a server you control, so the threat model is
different: a secret key is the *correct* credential here, and the interesting failures are about
that key escaping.

**In scope:**

- The SDK sending a credential somewhere it should not, or logging one unredacted — including via
  a lazy `%s` logging argument, an exception message, or a `repr()`
- `client_side=True` accepting an `sk_live_` key
- A way to make the SDK talk to a host other than the configured `base_url`
- An endpoint slug or table name escaping its path segment
- Anything letting one signed-in user's session reach another user's data
- A dependency appearing in the install footprint — the package declares none, and a transitive
  dependency would be a supply-chain surface this SDK exists to avoid

**Not vulnerabilities — these are documented properties, not defects:**

- *A secret key is readable by anyone who can read your process environment or source.* That is
  true of every server credential. Scope the credential narrowly and rotate it.
- *A publishable key can be extracted from anything shipped to a user.* It is designed to be
  public, like a Stripe publishable key.
- *The async client uses threads rather than native async I/O.* Documented, deliberate, and the
  reason the dependency count is zero. Not a security property.
- *A modified client can send arbitrary requests.* Assumed. The gateway is the boundary, which is
  why anything valuable belongs behind an endpoint.

If you are unsure which side of that line something falls on, report it. We would rather triage a
non-issue than miss a real one.

## For developers using this SDK

Most incidents involving a backend SDK are configuration, not code. Before shipping:

- The credential comes from the environment or a secret manager, never from source. Never commit
  an `sk_live_` key — assume any key that reaches a repository is compromised and rotate it.
- Pass `client_side=True` anywhere a user could read the code: a published notebook, a Pyodide or
  PyScript page, a desktop app, a shared Jupyter kernel.
- The credential is scoped to the minimum tables the code needs. Auth routes skip table-scope
  checks, so sign-in works on a credential that can reach nothing.
- Per-user tables carry an ownership column with a `__SELF__` row filter on the **table** scope
  **and** a `{{claim:sub}}` default value template on the **column** scope — both, not one.
- Anything a caller must not influence goes through a gateway endpoint, not a client table write.
- The `praxsuite` logger is left at its default level in production. `DEBUG` logs request and
  response bodies.

The reasoning behind the two isolation settings, including the silent failure when only the row
filter is configured, is in the
[README](README.md#per-player-isolation-needs-two-settings).
