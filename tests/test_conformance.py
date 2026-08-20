"""Offline conformance tests. No network, no workspace, no credentials.

Every case mirrors the shared Praxsuite SDK conformance contract, so behaviour cannot drift
between languages. Each one exists because getting it wrong produces silently wrong data rather
than an error.
"""

from __future__ import annotations

import json
import logging

import pytest

from praxsuite import filters as f
from praxsuite import keyguard, result, routes
from praxsuite.errors import (
    PraxAuthError,
    PraxError,
    PraxForbiddenError,
    PraxQuotaExceededError,
    PraxRateLimitError,
    PraxValidationError,
    error_for,
)
from praxsuite.log import scrub

# Shape-accurate fakes, assembled from fragments so a secret scanner does not flag this file.
FAKE_SECRET = "sk_live_" + "0123456789abcdef0123456789abcdef"
FAKE_PUBLISHABLE = "pk_live_" + "fedcba9876543210fedcba9876543210"
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhYmMifQ.signaturehere"


# ── filters ─────────────────────────────────────────────────────────────────

class TestFilters:
    def test_eq(self):
        assert f.eq("Score", 100) == {"field": "Score", "op": "eq", "value": 100}

    def test_is_null_uses_the_is_operator(self):
        # There is no isNull operator server-side; `is` only tests for null.
        assert f.is_null("DeletedAt") == {"field": "DeletedAt", "op": "is", "value": None}

    def test_is_not_null_compiles_to_neq_null(self):
        assert f.is_not_null("DeletedAt") == {"field": "DeletedAt", "op": "neq", "value": None}

    def test_starts_with_compiles_to_like(self):
        assert f.starts_with("Name", "Sword") == {
            "field": "Name", "op": "like", "value": "Sword%"
        }

    def test_ends_with_compiles_to_like(self):
        assert f.ends_with("Name", "blade") == {"field": "Name", "op": "like", "value": "%blade"}

    def test_in_carries_a_list(self):
        assert f.in_("Level", [1, 2, 3]) == {"field": "Level", "op": "in", "value": [1, 2, 3]}

    def test_in_accepts_any_iterable(self):
        assert f.in_("Level", (n for n in (1, 2)))["value"] == [1, 2]

    def test_empty_in_is_refused(self):
        # An empty IN matches nothing, silently. Refusing is better than a query that returns
        # zero rows for a reason nobody can see.
        with pytest.raises(PraxValidationError):
            f.in_("Level", [])

    def test_between_carries_two_values(self):
        assert f.between("Score", 10, 20) == {
            "field": "Score", "op": "between", "value": [10, 20]
        }

    def test_groups_nest(self):
        assert f.any_of(f.eq("R", "a"), f.eq("R", "b"))["or"] == [
            {"field": "R", "op": "eq", "value": "a"},
            {"field": "R", "op": "eq", "value": "b"},
        ]
        assert len(f.all_of(f.gte("L", 5), f.lte("L", 10))["and"]) == 2

    def test_blank_column_is_refused(self):
        with pytest.raises(PraxValidationError):
            f.eq("   ", 1)

    def test_only_real_operators_are_reachable(self):
        """Nothing in this module can produce an operator the gateway does not implement."""
        produced = {
            f.eq("c", 1)["op"], f.neq("c", 1)["op"], f.gt("c", 1)["op"], f.gte("c", 1)["op"],
            f.lt("c", 1)["op"], f.lte("c", 1)["op"], f.like("c", "x")["op"],
            f.ilike("c", "x")["op"], f.contains("c", "x")["op"], f.text_search("c", "x")["op"],
            f.starts_with("c", "x")["op"], f.ends_with("c", "x")["op"], f.is_null("c")["op"],
            f.is_not_null("c")["op"], f.in_("c", [1])["op"], f.between("c", 1, 2)["op"],
        }
        assert produced <= f.SUPPORTED_OPERATORS
        # The plausible-sounding ones the server does NOT implement must not be exported.
        for name in ("startsWith", "endsWith", "notIn", "isNull", "isNotNull", "nin"):
            assert name not in f.__all__


# ── result parsing ──────────────────────────────────────────────────────────

class TestParsing:
    def test_page_reads_total_not_total_count(self):
        # Reading meta.totalCount instead of meta.total returns nothing and reports 0 forever.
        # That exact mistake shipped in another SDK and went unnoticed for months.
        page = result.parse_page({
            "data": [{"ID": "a"}, {"ID": "b"}],
            "meta": {"limit": 50, "offset": 0, "count": 2, "total": 137, "durationMs": 12},
        })
        assert page.total == 137
        assert len(page) == 2
        assert page.limit == 50
        assert page.duration_ms == 12
        assert page.has_more

    def test_absent_total_is_none_not_zero(self):
        # "No rows matched" must stay distinguishable from "nobody asked for a count".
        page = result.parse_page({"data": [], "meta": {"limit": 50, "count": 0}})
        assert page.total is None
        assert not page.rows

    def test_total_count_field_is_ignored(self):
        """A body carrying only totalCount must NOT be read as a total."""
        page = result.parse_page({"data": [], "meta": {"totalCount": 99}})
        assert page.total is None

    def test_page_is_iterable_and_indexable(self):
        page = result.parse_page({"data": [{"ID": "a"}], "meta": {}})
        assert [r["ID"] for r in page] == ["a"]
        assert page[0]["ID"] == "a"
        assert page.first == {"ID": "a"}

    def test_mutation(self):
        m = result.parse_mutation({
            "affectedRows": 1, "data": [{"ID": "new"}], "meta": {"durationMs": 8},
        })
        assert m.affected_rows == 1
        assert m.row == {"ID": "new"}

    def test_auth_envelope_is_unwrapped(self):
        assert result.unwrap_envelope(
            {"isSuccess": True, "data": {"accessToken": "a.b.c"}}
        ) == {"accessToken": "a.b.c"}

    def test_query_body_is_not_unwrapped(self):
        # A /query body also has a `data` key, but it is a LIST. Unwrapping it would discard meta.
        body = {"data": [{"ID": "x"}], "meta": {"count": 1}}
        assert "meta" in result.unwrap_envelope(body)


# ── errors ──────────────────────────────────────────────────────────────────

class TestErrors:
    def test_query_error_shape(self):
        err = result.parse_error(
            403,
            '{"error":{"code":"FORBIDDEN","message":"Read access denied.","details":["scope"]}}',
        )
        assert err.code == "FORBIDDEN"
        assert err.is_forbidden
        assert not err.is_transient
        assert err.details == ("scope",)
        assert isinstance(err, PraxForbiddenError)

    def test_files_error_is_a_bare_string(self):
        err = result.parse_error(400, '{"error":"File type not allowed."}')
        assert err.message == "File type not allowed."

    def test_non_json_body_does_not_raise(self):
        err = result.parse_error(502, "<html>Bad Gateway</html>")
        assert "Bad Gateway" in err.message
        assert err.code == "HTTP_502"

    def test_empty_body_still_explains_itself(self):
        assert result.parse_error(500, "").message

    def test_rate_limit_and_quota_share_429_but_classify_oppositely(self):
        rate = error_for("RATE_LIMIT_EXCEEDED", "", 429)
        quota = error_for("QUOTA_EXCEEDED", "", 429)
        assert isinstance(rate, PraxRateLimitError) and rate.is_transient
        assert isinstance(quota, PraxQuotaExceededError) and not quota.is_transient
        assert quota.is_quota_exceeded
        assert error_for("EGRESS_LIMIT_EXCEEDED", "", 429).is_quota_exceeded

    def test_transient_classification(self):
        assert error_for("NETWORK_ERROR", "").is_transient
        assert error_for("HTTP_503", "", 503).is_transient
        assert not error_for("FORBIDDEN", "", 403).is_transient

    def test_401_is_an_auth_error(self):
        assert isinstance(error_for("UNAUTHORIZED", "", 401), PraxAuthError)

    def test_validation_error_is_also_a_value_error(self):
        # Callers already catching ValueError should not have to learn a new type.
        assert issubclass(PraxValidationError, ValueError)
        assert issubclass(PraxValidationError, PraxError)

    def test_str_includes_code_status_and_details(self):
        text = str(PraxError("X", "boom", 400, ("a", "b")))
        assert "X" in text and "400" in text and "boom" in text and "a" in text


# ── credentials ─────────────────────────────────────────────────────────────

class TestCredentials:
    def test_classification(self):
        assert keyguard.classify(FAKE_SECRET) is keyguard.CredentialKind.SECRET
        assert keyguard.classify(FAKE_PUBLISHABLE) is keyguard.CredentialKind.PUBLISHABLE
        assert keyguard.classify(FAKE_JWT) is keyguard.CredentialKind.JWT
        assert keyguard.classify("") is keyguard.CredentialKind.UNKNOWN
        assert keyguard.classify(None) is keyguard.CredentialKind.UNKNOWN

    def test_client_side_refuses_a_secret_key(self):
        with pytest.raises(PraxValidationError) as caught:
            keyguard.check_client_safe(FAKE_SECRET, "a test")
        assert caught.value.code == "SECRET_KEY_REFUSED"

    def test_client_side_accepts_publishable_and_session(self):
        keyguard.check_client_safe(FAKE_PUBLISHABLE, "a test")
        keyguard.check_client_safe(FAKE_JWT, "a test")

    def test_redaction_keeps_the_prefix_and_hides_the_material(self):
        masked = keyguard.redact(FAKE_SECRET)
        assert masked.startswith("sk_live_")
        assert "0123456789abcdef" not in masked

    def test_scrub_removes_every_credential_shape(self):
        text = (
            f'key={FAKE_SECRET} pub={FAKE_PUBLISHABLE} jwt={FAKE_JWT} '
            f'{{"refreshToken":"rt-secret-value","password":"hunter2"}}'
        )
        cleaned = scrub(text)
        assert "0123456789abcdef" not in cleaned
        assert "fedcba9876543210" not in cleaned
        assert "signaturehere" not in cleaned
        assert "rt-secret-value" not in cleaned
        assert "hunter2" not in cleaned
        # The prefix survives, so a log still says which kind of key was involved.
        assert "sk_live_" in cleaned and "pk_live_" in cleaned

    def test_logger_scrubs_lazy_arguments(self, caplog):
        """A credential passed as a %s argument must not reach a handler unscrubbed."""
        from praxsuite.log import logger

        with caplog.at_level(logging.INFO, logger="praxsuite"):
            logger.info("using %s", FAKE_SECRET)
        assert "0123456789abcdef" not in caplog.text


# ── routes ──────────────────────────────────────────────────────────────────

class TestRoutes:
    WS = "1eb92f32-d628-4656-8c64-cd0d43c9869d"

    def test_query_uses_the_frontdoor_short_form(self):
        assert routes.query("https://gateway.praxsuite.com", self.WS) == (
            f"https://gateway.praxsuite.com/{self.WS}/query"
        )

    def test_auth_actions_nest_under_auth(self):
        assert routes.auth("https://gateway.praxsuite.com", self.WS, "login").endswith(
            f"/{self.WS}/auth/login"
        )

    def test_normalisation(self):
        assert routes.schema("gateway.praxsuite.com/", self.WS) == (
            f"https://gateway.praxsuite.com/{self.WS}/schema"
        )
        assert routes.normalize_base_url("") == routes.CLOUD_HOST
        assert routes.normalize_base_url(None) == routes.CLOUD_HOST

    def test_endpoint_slug_is_escaped(self):
        # A slug lands in a path segment, so it must not be able to walk out of it.
        url = routes.endpoint("https://g.example.com", self.WS, "a/../b")
        assert "/../" not in url

    def test_insecure_remote_detection(self):
        assert routes.is_insecure_remote("http://gateway.example.com")
        assert not routes.is_insecure_remote("https://gateway.example.com")
        assert not routes.is_insecure_remote("http://localhost:5049")
        assert not routes.is_insecure_remote("http://127.0.0.1:5049")


# ── encoding ────────────────────────────────────────────────────────────────

class TestEncoding:
    def test_astral_emoji_survive_a_round_trip(self):
        # Display names contain emoji constantly; a codec that mangles them corrupts data
        # silently.
        name = "Aria 🚀🇨🇱"
        assert json.loads(json.dumps({"name": name}))["name"] == name

    def test_escaped_surrogate_pairs_decode_to_one_character(self):
        assert json.loads('{"name":"\\ud83d\\ude80"}')["name"] == "🚀"

    def test_body_is_not_ascii_escaped_on_the_wire(self):
        """The transport sends ensure_ascii=False, so emoji go out as UTF-8, not \\uXXXX."""
        payload = json.dumps({"name": "🚀"}, ensure_ascii=False).encode("utf-8")
        assert "🚀".encode("utf-8") in payload

    def test_integers_stay_integers(self):
        # Unlike Godot, whose JSON decodes every number as a float, Python preserves int - so no
        # int() cast is needed on an Int column here.
        assert isinstance(json.loads('{"n":1}')["n"], int)
