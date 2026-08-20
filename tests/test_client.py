"""Client, query-builder and guardrail tests. Offline: the transport is stubbed.

Nothing here opens a socket. The stub records what the SDK would have sent, which is the part
worth asserting on - a wire shape that drifts is how an SDK starts returning silently wrong data.
"""

from __future__ import annotations

import asyncio

import pytest

from praxsuite import Praxsuite
from praxsuite import filters as f
from praxsuite.aio import AsyncPraxsuite
from praxsuite.errors import PraxAuthError, PraxError, PraxValidationError

FAKE_SECRET = "sk_live_" + "0123456789abcdef0123456789abcdef"
FAKE_PUBLISHABLE = "pk_live_" + "fedcba9876543210fedcba9876543210"
WS = "1eb92f32-d628-4656-8c64-cd0d43c9869d"


class StubTransport:
    """Records requests and replays queued responses."""

    def __init__(self, responses=None):
        self.calls: list[dict] = []
        self.responses = list(responses or [])

    def request_json(self, method, url, headers, body=None, retry_safe=False):
        self.calls.append({
            "method": method, "url": url, "headers": dict(headers),
            "body": body, "retry_safe": retry_safe,
        })
        if not self.responses:
            return {"data": [], "meta": {"count": 0}}
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    @property
    def last(self) -> dict:
        return self.calls[-1]


@pytest.fixture
def prax():
    client = Praxsuite(WS, FAKE_SECRET, "https://gateway.example.com")
    client.transport = StubTransport()
    return client


# ── construction ────────────────────────────────────────────────────────────

class TestConstruction:
    def test_missing_workspace_is_refused(self, monkeypatch):
        monkeypatch.delenv("PRAXSUITE_WORKSPACE_ID", raising=False)
        with pytest.raises(PraxValidationError):
            Praxsuite("", FAKE_SECRET)

    def test_missing_credential_is_refused(self, monkeypatch):
        monkeypatch.delenv("PRAXSUITE_API_KEY", raising=False)
        with pytest.raises(PraxValidationError):
            Praxsuite(WS, "")

    def test_reads_environment(self, monkeypatch):
        monkeypatch.setenv("PRAXSUITE_WORKSPACE_ID", WS)
        monkeypatch.setenv("PRAXSUITE_API_KEY", FAKE_SECRET)
        monkeypatch.setenv("PRAXSUITE_BASE_URL", "https://tier.example.com")
        client = Praxsuite()
        assert client.workspace_id == WS
        assert client.base_url == "https://tier.example.com"

    def test_secret_key_allowed_server_side(self):
        # A Python process is usually a server, so a secret key is the correct thing to use.
        Praxsuite(WS, FAKE_SECRET)

    def test_secret_key_refused_client_side(self):
        with pytest.raises(PraxValidationError) as caught:
            Praxsuite(WS, FAKE_SECRET, client_side=True)
        assert caught.value.code == "SECRET_KEY_REFUSED"

    def test_publishable_key_allowed_client_side(self):
        Praxsuite(WS, FAKE_PUBLISHABLE, client_side=True)

    def test_repr_does_not_leak_the_credential(self):
        assert "0123456789abcdef" not in repr(Praxsuite(WS, FAKE_SECRET))

    def test_plaintext_remote_warns(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="praxsuite"):
            Praxsuite(WS, FAKE_SECRET, "http://gateway.example.com")
        assert "plaintext" in caplog.text.lower()

    def test_loopback_does_not_warn(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="praxsuite"):
            Praxsuite(WS, FAKE_SECRET, "http://localhost:5049")
        assert "plaintext" not in caplog.text.lower()


# ── query building ──────────────────────────────────────────────────────────

class TestQueryBuilding:
    def test_minimal_query(self, prax):
        built = prax.data.table("Scores").build()
        assert built["refs"] == {"t": "Scores"}
        assert built["query"]["from"] == "t"
        assert "limit" not in built["query"]
        assert "includeTotalCount" not in built

    def test_full_query(self, prax):
        built = (prax.data.table("Scores")
                 .select("Player", "Score")
                 .where(f.gte("Score", 100))
                 .where(Season=3)
                 .order_by("Score", ascending=False)
                 .limit(20)
                 .offset(40)
                 .with_total_count()
                 .build())

        assert built["query"]["select"] == ["Player", "Score"]
        # Repeated where calls and kwargs all land in one ANDed list.
        assert len(built["query"]["where"]) == 2
        assert built["query"]["orderBy"] == [{"field": "Score", "dir": "desc"}]
        assert built["query"]["limit"] == 20
        assert built["query"]["offset"] == 40
        # includeTotalCount sits BESIDE query. Nesting it is silently ignored and the total then
        # comes back absent forever.
        assert built["includeTotalCount"] is True
        assert "includeTotalCount" not in built["query"]

    def test_limit_is_clamped_up_to_one(self, prax):
        # The gateway clamps limit up to a minimum of 1, so 0 never means "no rows".
        assert prax.data.table("Scores").limit(0).build()["query"]["limit"] == 1

    def test_include_uses_an_alias(self, prax):
        built = prax.data.table("Players").include("Inventory", ["Item"], 10).build()
        assert len(built["refs"]) == 2
        assert built["query"]["select"][0]["table"] == "r1"
        assert built["query"]["select"][0]["select"] == ["Item"]

    def test_aggregate_shape(self, prax):
        built = prax.data.table("Scores").aggregate("sum", "Score", "total").build()
        assert built["query"]["select"][0] == {"field": "Score", "fn": "sum", "alias": "total"}

    def test_unsupported_aggregate_is_refused(self, prax):
        with pytest.raises(PraxValidationError):
            prax.data.table("Scores").aggregate("median", "Score", "m")

    def test_bad_alias_is_refused(self, prax):
        # The alias constraint exists to prevent injection through the alias.
        with pytest.raises(PraxValidationError):
            prax.data.table("Scores").aggregate("sum", "Score", "total; DROP TABLE")

    def test_blank_table_is_refused(self, prax):
        with pytest.raises(PraxValidationError):
            prax.data.table("  ")


# ── terminal methods ────────────────────────────────────────────────────────

class TestTerminals:
    def test_fetch_parses_a_page(self, prax):
        prax.transport.responses = [
            {"data": [{"ID": "a"}], "meta": {"count": 1, "limit": 50, "total": 7}}
        ]
        page = prax.data.table("Scores").fetch()
        assert page.total == 7 and len(page) == 1
        assert prax.transport.last["method"] == "POST"
        # A select is idempotent, so it is safe to retry.
        assert prax.transport.last["retry_safe"] is True

    def test_first_requests_one_row_and_restores_the_limit(self, prax):
        prax.transport.responses = [{"data": [{"ID": "a"}], "meta": {"count": 1}}]
        query = prax.data.table("Scores").limit(25)
        assert query.first() == {"ID": "a"}
        assert prax.transport.last["body"]["query"]["limit"] == 1
        # The builder must be reusable afterwards.
        assert query.build()["query"]["limit"] == 25

    def test_first_returns_none_when_empty(self, prax):
        prax.transport.responses = [{"data": [], "meta": {"count": 0}}]
        assert prax.data.table("Scores").first() is None

    def test_count_asks_for_one_row_plus_a_total(self, prax):
        prax.transport.responses = [{"data": [{"ID": "a"}], "meta": {"count": 1, "total": 42}}]
        assert prax.data.table("Scores").count() == 42
        body = prax.transport.last["body"]
        assert body["includeTotalCount"] is True
        assert body["query"]["limit"] == 1

    def test_count_without_a_total_explains_why(self, prax):
        prax.transport.responses = [{"data": [], "meta": {"count": 0}}]
        with pytest.raises(PraxError) as caught:
            prax.data.table("Scores").count()
        assert caught.value.code == "TOTAL_COUNT_UNAVAILABLE"

    def test_all_pages_until_short(self, prax):
        prax.transport.responses = [
            {"data": [{"ID": "1"}, {"ID": "2"}], "meta": {"count": 2, "limit": 2}},
            {"data": [{"ID": "3"}], "meta": {"count": 1, "limit": 2}},
        ]
        rows = prax.data.table("Scores").all(page_size=2)
        assert [r["ID"] for r in rows] == ["1", "2", "3"]
        assert len(prax.transport.calls) == 2

    def test_all_respects_a_server_clamped_limit(self, prax):
        """A clamp must not cause an endless loop re-reading the same rows.

        page_size 500 is asked for, the server applies 2. `all` has to notice from meta.limit,
        not from what it requested.
        """
        prax.transport.responses = [
            {"data": [{"ID": "1"}, {"ID": "2"}], "meta": {"count": 2, "limit": 2}},
            {"data": [], "meta": {"count": 0, "limit": 2}},
        ]
        rows = prax.data.table("Scores").all(page_size=500)
        assert len(rows) == 2
        assert len(prax.transport.calls) == 2

    def test_all_honours_max_rows(self, prax):
        prax.transport.responses = [
            {"data": [{"ID": str(i)} for i in range(5)], "meta": {"count": 5, "limit": 5}},
        ]
        assert len(prax.data.table("Scores").all(page_size=5, max_rows=3)) == 3

    def test_iterating_a_query_fetches(self, prax):
        prax.transport.responses = [{"data": [{"ID": "a"}], "meta": {"count": 1}}]
        assert [r["ID"] for r in prax.data.table("Scores")] == ["a"]


# ── write guardrails ────────────────────────────────────────────────────────

class TestWriteGuardrails:
    def test_unscoped_update_is_refused_before_sending(self, prax):
        with pytest.raises(PraxValidationError) as caught:
            prax.data.update("Scores", {"Score": 0})
        assert caught.value.code == "UNSCOPED_MUTATION"
        assert not prax.transport.calls, "nothing may reach the network"

    def test_unscoped_delete_is_refused_before_sending(self, prax):
        with pytest.raises(PraxValidationError) as caught:
            prax.data.delete("Scores")
        assert caught.value.code == "UNSCOPED_MUTATION"
        assert not prax.transport.calls

    def test_empty_values_are_refused(self, prax):
        with pytest.raises(PraxValidationError):
            prax.data.insert("Scores", {})
        with pytest.raises(PraxValidationError):
            prax.data.update("Scores", {}, f.eq("ID", "x"))

    def test_native_columns_are_refused(self, prax):
        with pytest.raises(PraxValidationError) as caught:
            prax.data.insert("Scores", {"ID": "forced", "Score": 1})
        assert "ID" in caught.value.message

    def test_native_columns_are_case_insensitive(self, prax):
        with pytest.raises(PraxValidationError):
            prax.data.update("Scores", {"CreatedDate": "now"}, f.eq("ID", "x"))

    def test_blank_row_id_is_refused(self, prax):
        with pytest.raises(PraxValidationError):
            prax.data.update_by_id("Scores", "  ", {"Score": 1})
        with pytest.raises(PraxValidationError):
            prax.data.delete_by_id("Scores", "")

    def test_insert_wire_shape(self, prax):
        prax.transport.responses = [{"affectedRows": 1, "data": [{"ID": "new"}], "meta": {}}]
        outcome = prax.data.insert("Scores", {"Score": 10})
        mutation = prax.transport.last["body"]["mutation"]
        assert mutation["type"] == "insert"
        assert mutation["table"] == "t"
        assert mutation["values"] == [{"Score": 10}]
        assert mutation["returning"] is True
        assert outcome.row == {"ID": "new"}
        # A mutation must never be retried automatically.
        assert prax.transport.last["retry_safe"] is False

    def test_update_wire_shape(self, prax):
        prax.transport.responses = [{"affectedRows": 2, "data": [], "meta": {}}]
        prax.data.update("Scores", {"Score": 1}, f.eq("Season", 3))
        mutation = prax.transport.last["body"]["mutation"]
        assert mutation["type"] == "update"
        assert mutation["set"] == {"Score": 1}
        assert mutation["where"] == [{"field": "Season", "op": "eq", "value": 3}]

    def test_upsert_routes_by_presence_of_an_id(self, prax):
        prax.transport.responses = [
            {"affectedRows": 1, "data": [], "meta": {}},
            {"affectedRows": 1, "data": [], "meta": {}},
        ]
        prax.data.upsert("Saves", {"Level": 1})
        assert prax.transport.last["body"]["mutation"]["type"] == "insert"
        prax.data.upsert("Saves", {"Level": 2}, row_id="abc")
        assert prax.transport.last["body"]["mutation"]["type"] == "update"


# ── headers and sessions ────────────────────────────────────────────────────

class TestHeadersAndSessions:
    def test_anonymous_headers_carry_the_key(self, prax):
        headers = prax.anonymous_headers()
        assert headers["x-api-key"] == FAKE_SECRET
        assert "Authorization" not in headers
        assert headers["x-praxsuite-sdk"].startswith("python/")

    def test_session_headers_prefer_the_session(self, prax):
        prax.transport.responses = [{
            "isSuccess": True,
            "data": {
                "accessToken": "a.b.c", "refreshToken": "r", "expiresIn": 3600,
                "user": {"id": "u1", "email": "x@y.z", "displayName": "X"},
            },
        }]
        prax.auth.login("x@y.z", "pw")
        headers = prax.session_headers()
        # Either header, never both: Authorization carries a session, x-api-key carries a key.
        assert headers["Authorization"] == "Bearer a.b.c"
        assert "x-api-key" not in headers
        assert prax.auth.session.display_name == "X"

    def test_login_uses_the_credential_not_the_session(self, prax):
        """Signing in while already signed in must not depend on the old token."""
        prax.transport.responses = [
            {"data": {"accessToken": "first", "refreshToken": "r1", "expiresIn": 3600}},
            {"data": {"accessToken": "second", "refreshToken": "r2", "expiresIn": 3600}},
        ]
        prax.auth.login("a@b.c", "pw")
        prax.auth.login("a@b.c", "pw")
        assert prax.transport.last["headers"]["x-api-key"] == FAKE_SECRET
        assert "Authorization" not in prax.transport.last["headers"]

    def test_refresh_carries_the_profile_forward(self, prax):
        """A refresh response often omits the user block; the identity must not vanish."""
        prax.transport.responses = [
            {"data": {
                "accessToken": "a1", "refreshToken": "r1", "expiresIn": 3600,
                "user": {"id": "u1", "email": "x@y.z", "displayName": "Original"},
            }},
            {"data": {"accessToken": "a2", "refreshToken": "r2", "expiresIn": 3600}},
        ]
        prax.auth.login("x@y.z", "pw")
        prax.auth.refresh()
        assert prax.auth.session.access_token == "a2"
        assert prax.auth.session.display_name == "Original"
        assert prax.auth.session.user_id == "u1"

    def test_registration_requiring_confirmation_is_not_a_failure(self, prax):
        # Reporting this as a bad password would leave users retrying a correct one forever.
        prax.transport.responses = [{
            "data": {"requiresEmailConfirmation": True, "message": "Check your inbox."}
        }]
        outcome = prax.auth.register("x@y.z", "pw")
        assert outcome.requires_email_confirmation
        assert outcome.session is None
        assert not prax.auth.is_signed_in

    def test_logout_clears_locally_even_if_the_server_fails(self, prax):
        prax.transport.responses = [
            {"data": {"accessToken": "a", "refreshToken": "r", "expiresIn": 3600}},
            PraxError("BOOM", "server said no", 500),
        ]
        prax.auth.login("x@y.z", "pw")
        prax.auth.logout()
        assert not prax.auth.is_signed_in, "pressing sign out must sign you out"

    def test_rejected_refresh_signs_the_user_out(self, prax):
        prax.transport.responses = [
            {"data": {"accessToken": "a", "refreshToken": "r", "expiresIn": 3600}},
            PraxAuthError("INVALID_REFRESH_TOKEN", "no", 401),
        ]
        prax.auth.login("x@y.z", "pw")
        with pytest.raises(PraxAuthError):
            prax.auth.refresh()
        assert not prax.auth.is_signed_in

    def test_network_failure_during_refresh_keeps_the_session(self, prax):
        """A wifi blip must not sign someone out - the existing token may still work."""
        from praxsuite.errors import PraxNetworkError
        prax.transport.responses = [
            {"data": {"accessToken": "a", "refreshToken": "r", "expiresIn": 3600}},
            PraxNetworkError("NETWORK_ERROR", "offline"),
        ]
        prax.auth.login("x@y.z", "pw")
        with pytest.raises(PraxNetworkError):
            prax.auth.refresh()
        assert prax.auth.is_signed_in

    def test_session_change_callback_fires(self, prax):
        seen = []
        prax.auth.on_session_change(seen.append)
        prax.transport.responses = [
            {"data": {"accessToken": "a", "refreshToken": "r", "expiresIn": 3600}},
            {},
        ]
        prax.auth.login("x@y.z", "pw")
        prax.auth.logout()
        assert len(seen) == 2 and seen[0] is not None and seen[1] is None

    def test_a_raising_callback_does_not_break_sign_in(self, prax):
        def bad(_):
            raise RuntimeError("callback bug")

        prax.auth.on_session_change(bad)
        prax.transport.responses = [
            {"data": {"accessToken": "a", "refreshToken": "r", "expiresIn": 3600}}
        ]
        assert prax.auth.login("x@y.z", "pw").access_token == "a"

    def test_401_triggers_one_refresh_and_one_retry(self, prax):
        prax.transport.responses = [
            {"data": {"accessToken": "old", "refreshToken": "r", "expiresIn": 3600}},
            PraxAuthError("UNAUTHORIZED", "expired", 401),          # the query
            {"data": {"accessToken": "new", "refreshToken": "r2", "expiresIn": 3600}},  # refresh
            {"data": [{"ID": "a"}], "meta": {"count": 1}},          # the retry
        ]
        prax.auth.login("x@y.z", "pw")
        page = prax.data.table("Scores").fetch()
        assert len(page) == 1
        assert prax.transport.last["headers"]["Authorization"] == "Bearer new"

    def test_a_second_401_is_not_retried_forever(self, prax):
        prax.transport.responses = [
            {"data": {"accessToken": "old", "refreshToken": "r", "expiresIn": 3600}},
            PraxAuthError("UNAUTHORIZED", "expired", 401),
            {"data": {"accessToken": "new", "refreshToken": "r2", "expiresIn": 3600}},
            PraxAuthError("UNAUTHORIZED", "still expired", 401),
        ]
        prax.auth.login("x@y.z", "pw")
        with pytest.raises(PraxAuthError):
            prax.data.table("Scores").fetch()


# ── endpoints and schema ────────────────────────────────────────────────────

class TestEndpointsAndSchema:
    def test_endpoint_call_is_not_retried(self, prax):
        prax.transport.responses = [{"rank": 3}]
        assert prax.endpoints.call("submit-score", {"score": 1})["rank"] == 3
        # An endpoint runs an automation; running one twice is rarely harmless.
        assert prax.transport.last["retry_safe"] is False

    def test_endpoint_get_is_retried(self, prax):
        prax.transport.responses = [{"ok": True}]
        prax.endpoints.get("leaderboard")
        assert prax.transport.last["retry_safe"] is True

    def test_blank_slug_is_refused(self, prax):
        with pytest.raises(PraxValidationError):
            prax.endpoints.call("  ")

    def test_schema_is_cached(self, prax):
        prax.transport.responses = [
            {"data": {"tables": [{"name": "Scores", "columns": [{"name": "Score"}]}]}}
        ]
        assert prax.schema.columns("Scores") == ["Score"]
        assert prax.schema.has_table("Scores")
        assert prax.schema.table("Nope") is None
        assert len(prax.transport.calls) == 1, "the schema must be fetched once"


# ── async face ──────────────────────────────────────────────────────────────

class TestAsyncClient:
    def _client(self, responses=None):
        client = AsyncPraxsuite(WS, FAKE_SECRET, "https://gateway.example.com")
        client.sync.transport = StubTransport(responses)
        return client

    def test_builder_is_sync_and_terminals_await(self):
        client = self._client([{"data": [{"ID": "a"}], "meta": {"count": 1, "total": 1}}])

        async def run():
            query = client.query("Scores").select("ID").where(Season=3).limit(5)
            # Building sends nothing, so it needs no await.
            assert query.build()["query"]["limit"] == 5
            return await query.fetch()

        page = asyncio.run(run())
        assert page.total == 1

    def test_guardrails_still_apply(self):
        client = self._client()

        async def run():
            with pytest.raises(PraxValidationError):
                await client.data.update("Scores", {"Score": 0})

        asyncio.run(run())
        assert not client.sync.transport.calls

    def test_login_and_write(self):
        client = self._client([
            {"data": {"accessToken": "a", "refreshToken": "r", "expiresIn": 3600}},
            {"affectedRows": 1, "data": [{"ID": "x"}], "meta": {}},
        ])

        async def run():
            await client.auth.login("x@y.z", "pw")
            assert client.auth.is_signed_in
            return await client.data.insert("Saves", {"Level": 1})

        assert asyncio.run(run()).row == {"ID": "x"}

    def test_does_not_block_the_event_loop(self):
        """The point of the async face: a slow call must not stall other coroutines."""
        import time

        class SlowTransport(StubTransport):
            def request_json(self, *args, **kwargs):
                time.sleep(0.25)
                return super().request_json(*args, **kwargs)

        client = AsyncPraxsuite(WS, FAKE_SECRET, "https://gateway.example.com")
        client.sync.transport = SlowTransport([{"data": [], "meta": {}}])

        async def run():
            ticks = 0

            async def ticker():
                nonlocal ticks
                for _ in range(10):
                    await asyncio.sleep(0.01)
                    ticks += 1

            await asyncio.gather(client.query("Scores").fetch(), ticker())
            return ticks

        # A blocking call on the loop thread would starve the ticker entirely.
        assert asyncio.run(run()) == 10
