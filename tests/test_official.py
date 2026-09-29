"""Tests for altruagent.official: Official Agent Key auth, session caching and
one-retry-after-401, assignment discovery, seat grants, and the GameAPI-side
OfficialSeatAuth. Mirrors Agent_ACP backend/src/routes/tournament.ts
(``/tournament/agent/authenticate``, ``/tournament/agent/assignments``,
``/tournament/agent/assignments/:seatId/grant``). No real network.
"""

from __future__ import annotations

import json

import httpx
import pytest

from altruagent.client import AltruAgentClient
from altruagent.errors import AuthenticationError, ConfigurationError, PlatformError
from altruagent.official import (
    ASSIGNMENTS_PATH,
    AUTHENTICATE_PATH,
    OFFICIAL_AGENT_KEY_ENV,
    OfficialAgentAuth,
    OfficialAgentClient,
    OfficialAgentError,
    OfficialSeatAuth,
    load_official_agent_key,
)

CONTROL = "https://control.example.test"
GAMEAPI = "https://gameapi.example.test"
KEY = "eak_live_" + "ab" * 32
EXEC = "exec-" + "z" * 40


def assignment(seat_id="seat-1", match_id="match-1", game_type="pokemon_vgc_doubles_draft", **extra):
    return {"match_id": match_id, "seat_id": seat_id, "game_type": game_type, "seat_position": 0,
            "seat_count": 2, "match_status": "starting", "seat_status": "pending", **extra}


def grant(access_token="seat-jwt-1", seat_id="seat-1"):
    return {"access_token": access_token, "expires_at": "2026-09-28T12:00:00Z", "agent_id": "synthetic-1",
            "gameapi_server_url": GAMEAPI, "game_session_id": "game-1", "match_id": "match-1", "seat_id": seat_id,
            "seat_position": 0, "seat_count": 2, "game_type": "pokemon_vgc_doubles_draft", "match_status": "in_progress"}


class Backend:
    """Mock control plane. Sessions are "sess-N"; ``valid`` holds the ones
    currently accepted (tests expire them by removing them).
    """

    def __init__(self, *, assignments=None, key_valid=True):
        self.assignments = assignments if assignments is not None else []
        self.key_valid = key_valid
        self.auth_bodies: list[dict] = []
        self.valid: set[str] = set()
        self.requests: list[tuple[str, str, str | None]] = []
        self.grant_responses: dict[str, list[httpx.Response]] = {}
        self.bodies: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization")
        self.requests.append((request.method, request.url.path, auth))
        if request.url.path == AUTHENTICATE_PATH:
            self.auth_bodies.append(json.loads(request.content))
            if not self.key_valid:
                return httpx.Response(401, json={"error": "invalid_official_agent_key", "detail": "Invalid official agent key"})
            token = f"sess-{len(self.auth_bodies)}"
            self.valid.add(token)
            return httpx.Response(200, json={"access_token": token, "expires_at": "2026-09-28T12:00:00Z"})
        if auth is None or auth.removeprefix("Bearer ") not in self.valid:
            return httpx.Response(401, json={"error": "invalid_agent_session", "detail": "Invalid or expired agent session token"})
        if request.method == "GET" and request.url.path == ASSIGNMENTS_PATH:
            return httpx.Response(200, json={"assignments": self.assignments})
        if request.method == "POST" and request.url.path.startswith(ASSIGNMENTS_PATH + "/"):
            self.bodies.append((request.url.path, json.loads(request.content or b"{}")))
            if request.url.path.endswith("/lease/renew"):
                return httpx.Response(200, json={"seat_id": request.url.path.split("/")[-3], "execution_lease_expires_at": "x"})
            seat_id = request.url.path.split("/")[-2]
            queue = self.grant_responses.get(seat_id)
            if queue:
                return queue.pop(0) if len(queue) > 1 else queue[0]
            return httpx.Response(404, json={"error": "assignment_not_found", "detail": "Assignment not found"})
        return httpx.Response(404)


def official(backend, **kwargs) -> OfficialAgentClient:
    return OfficialAgentClient(CONTROL, KEY, load_env_file=False, transport=httpx.MockTransport(backend), **kwargs)


# -- configuration ---------------------------------------------------------------------


def test_key_from_environment(monkeypatch):
    monkeypatch.setenv(OFFICIAL_AGENT_KEY_ENV, f"  {KEY}\n")

    assert load_official_agent_key() == KEY


@pytest.mark.parametrize("value", ["", "sk_agent_abc", "seatclaim_abc", "eak_live_short", "eak_live_" + "G" * 64])
def test_missing_or_malformed_key_is_a_configuration_error_that_hides_the_value(monkeypatch, value):
    monkeypatch.setenv(OFFICIAL_AGENT_KEY_ENV, value)

    with pytest.raises(ConfigurationError) as exc_info:
        load_official_agent_key()

    assert OFFICIAL_AGENT_KEY_ENV in str(exc_info.value)
    if value:
        assert value not in str(exc_info.value)


def test_missing_control_url_is_a_configuration_error(monkeypatch):
    monkeypatch.delenv("ALTRUAGENT_CONTROL_URL", raising=False)

    with pytest.raises(ConfigurationError, match="ALTRUAGENT_CONTROL_URL"):
        OfficialAgentClient(official_agent_key=KEY, load_env_file=False)


def test_official_auth_repr_hides_key():
    assert KEY not in repr(OfficialAgentAuth(KEY))


# -- authentication / session ---------------------------------------------------------------


def test_authentication_success_sends_only_the_key():
    backend = Backend()

    official(backend).authenticate()

    assert backend.auth_bodies == [{"official_agent_key": KEY}]
    assert backend.requests[0][2] is None  # no bearer on the authenticate call itself


def test_session_token_is_cached_across_requests():
    backend = Backend(assignments=[assignment()])
    client = official(backend)

    client.assignments()
    client.assignments()
    client.assignments()

    assert len(backend.auth_bodies) == 1
    assert [r[2] for r in backend.requests if r[1] == ASSIGNMENTS_PATH] == ["Bearer sess-1"] * 3


def test_expired_session_reauthenticates_once_and_retries():
    backend = Backend(assignments=[assignment()])
    client = official(backend)
    client.assignments()
    backend.valid.clear()  # the short-lived session expires

    result = client.assignments()

    assert [a.seat_id for a in result] == ["seat-1"]
    assert len(backend.auth_bodies) == 2
    assert [r[2] for r in backend.requests if r[1] == ASSIGNMENTS_PATH] == ["Bearer sess-1", "Bearer sess-1", "Bearer sess-2"]


def test_second_401_after_reauth_stops_without_looping():
    backend = Backend()
    client = official(backend)
    client.authenticate()
    backend.valid = set()
    original = backend.__call__

    def always_reject_sessions(request):
        response = original(request)
        backend.valid = set()  # every newly minted session is immediately rejected
        return response

    client._client._http._transport = httpx.MockTransport(always_reject_sessions)

    with pytest.raises(AuthenticationError):
        client.assignments()
    assert len(backend.auth_bodies) == 2  # initial + exactly one re-authentication


def test_invalid_key_raises_clear_error_without_the_key():
    backend = Backend(key_valid=False)

    with pytest.raises(OfficialAgentError) as exc_info:
        official(backend).authenticate()

    assert exc_info.value.error_code == "invalid_official_agent_key"
    assert "dashboard" in str(exc_info.value) and KEY not in str(exc_info.value)


def test_key_revoked_mid_run_fails_after_one_reauth_attempt():
    backend = Backend()
    client = official(backend)
    client.authenticate()
    backend.valid.clear()
    backend.key_valid = False

    with pytest.raises(OfficialAgentError):
        client.assignments()
    assert len(backend.auth_bodies) == 2


def test_unreachable_control_plane_is_platform_error():
    def down(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(PlatformError) as exc_info:
        official(down).authenticate()
    assert exc_info.value.status_code is None


# -- discovery / grants --------------------------------------------------------------------------


def test_no_assignments():
    assert official(Backend()).assignments() == []


def test_one_and_many_assignments_parse():
    backend = Backend(assignments=[assignment("seat-1"), assignment("seat-2", match_id="match-2", game_type="werewolf",
                                                                    seat_position=5, seat_count=7)])

    result = official(backend).assignments()

    assert [(a.seat_id, a.match_id, a.game_type) for a in result] == [
        ("seat-1", "match-1", "pokemon_vgc_doubles_draft"), ("seat-2", "match-2", "werewolf")]
    assert (result[1].seat_position, result[1].seat_count, result[1].seat_status) == (5, 7, "pending")


def test_grant_returns_seat_grant():
    backend = Backend()
    backend.grant_responses["seat-1"] = [httpx.Response(200, json=grant())]

    seat_grant = official(backend).grant("seat-1", EXEC)

    assert (seat_grant.game_session_id, seat_grant.agent_id, seat_grant.seat_id) == ("game-1", "synthetic-1", "seat-1")
    assert ("POST", f"{ASSIGNMENTS_PATH}/seat-1/grant", "Bearer sess-1") in backend.requests


@pytest.mark.parametrize(
    "response, code",
    [
        (httpx.Response(404, json={"error": "assignment_not_found", "detail": "x"}), "assignment_not_found"),
        (httpx.Response(409, json={"error": "assignment_not_grantable", "detail": "x"}), "assignment_not_grantable"),
    ],
)
def test_grant_errors_are_official_agent_errors(response, code):
    backend = Backend()
    backend.grant_responses["seat-1"] = [response]

    with pytest.raises(OfficialAgentError) as exc_info:
        official(backend).grant("seat-1", EXEC)
    assert exc_info.value.error_code == code


def test_grant_missing_fields_is_rejected():
    backend = Backend()
    backend.grant_responses["seat-1"] = [httpx.Response(200, json={"access_token": "t"})]

    with pytest.raises(OfficialAgentError):
        official(backend).grant("seat-1", EXEC)


# -- OfficialSeatAuth (GameAPI side) -----------------------------------------------------------------


def test_seat_auth_uses_grant_token_for_gameapi_and_regrants_after_401():
    backend = Backend()
    backend.grant_responses["seat-1"] = [httpx.Response(200, json=grant("seat-jwt-1")),
                                         httpx.Response(200, json=grant("seat-jwt-2"))]
    control = official(backend)
    game_requests = []

    def gameapi(request):
        game_requests.append(request.headers.get("authorization"))
        if request.headers.get("authorization") == "Bearer seat-jwt-1":
            return httpx.Response(401, json={"detail": "Invalid or expired token"})
        return httpx.Response(200, json={"ok": True})

    seat_auth = OfficialSeatAuth(control, "seat-1", EXEC)
    game_client = AltruAgentClient(CONTROL, auth=seat_auth, load_env_file=False, transport=httpx.MockTransport(gameapi))

    assert game_client.request("GET", f"{GAMEAPI}/games/game-1") == {"ok": True}
    assert game_requests == ["Bearer seat-jwt-1", "Bearer seat-jwt-2"]  # renewal = re-grant (reconnect)
    assert seat_auth.grant.access_token == "seat-jwt-2"
    # Neither the persistent key nor the agent session ever reaches GameAPI.
    assert all(KEY not in (h or "") and "sess-" not in (h or "") for h in game_requests)
    assert KEY not in repr(seat_auth) and "seat-jwt" not in repr(seat_auth) and EXEC not in repr(seat_auth)
    # Both the first grant and the GameAPI-401 re-grant carry the same execution_id.
    assert backend.bodies == [(f"{ASSIGNMENTS_PATH}/seat-1/grant", {"execution_id": EXEC})] * 2


def test_grant_sends_execution_id():
    backend = Backend()
    backend.grant_responses["seat-1"] = [httpx.Response(200, json=grant())]

    official(backend).grant("seat-1", EXEC)

    assert backend.bodies == [(f"{ASSIGNMENTS_PATH}/seat-1/grant", {"execution_id": EXEC})]


def test_renew_lease_uses_the_renew_endpoint_not_grant():
    backend = Backend()

    result = official(backend).renew_lease("seat-1", EXEC)

    assert result == {"seat_id": "seat-1", "execution_lease_expires_at": "x"}
    assert backend.bodies == [(f"{ASSIGNMENTS_PATH}/seat-1/lease/renew", {"execution_id": EXEC})]
    assert not any(path.endswith("/grant") for _, path, _ in backend.requests)


def test_session_reauthentication_preserves_execution_id():
    backend = Backend()
    client = official(backend)
    client.renew_lease("seat-1", EXEC)
    backend.valid.clear()  # the agent session expires mid-match

    client.renew_lease("seat-1", EXEC)

    assert len(backend.auth_bodies) == 2  # re-authenticated once...
    assert [body for _, body in backend.bodies] == [{"execution_id": EXEC}] * 2  # ...same lease owner


def test_lease_not_held_is_an_official_agent_error():
    def handler(request):
        if request.url.path == AUTHENTICATE_PATH:
            return httpx.Response(200, json={"access_token": "sess-1", "expires_at": "x"})
        return httpx.Response(409, json={"error": "lease_not_held", "detail": "request a new grant"})

    with pytest.raises(OfficialAgentError) as exc_info:
        official(handler).renew_lease("seat-1", EXEC)
    assert exc_info.value.error_code == "lease_not_held"


def test_seat_auth_rejects_a_renewal_for_a_different_seat():
    backend = Backend()
    backend.grant_responses["seat-1"] = [httpx.Response(200, json=grant("a")),
                                         httpx.Response(200, json={**grant("b"), "game_session_id": "other"})]
    seat_auth = OfficialSeatAuth(official(backend), "seat-1", EXEC)
    seat_auth.login(None, CONTROL)

    with pytest.raises(OfficialAgentError):
        seat_auth.login(None, CONTROL)
