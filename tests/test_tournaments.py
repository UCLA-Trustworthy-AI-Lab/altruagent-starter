"""Unit tests for the platform-tournament client surface:
``AltruAgentClient.tournament(id)``, ``competition(id)``, ``mcp_url``,
``join_competition(id)`` and ``sessions().tournament_matches``. The control
plane is an ``httpx.MockTransport``; MCP is either a recording fake of
``call_tool`` or the real MCP SDK client against a mocked MCP server. No
network.
"""

from __future__ import annotations

import json

import httpx
import pytest

import altruagent.client as client_module
from altruagent.client import KNOWN_MCP_URLS, AltruAgentClient
from altruagent.errors import AuthenticationError, PlatformError
from altruagent.mcp_transport import MCPToolError
from altruagent.mcp_transport import call_tool as real_call_tool

from test_mcp_transport import _initialize_response, _tool_call_response, make_factory

CONTROL_URL = "https://example.test"
MCP_URL = "https://gameapi.example.test/mcp"


@pytest.fixture(autouse=True)
def no_mcp_env(monkeypatch):
    monkeypatch.delenv("ALTRUAGENT_MCP_URL", raising=False)


def make_client(handler, *, control_url: str = CONTROL_URL, mcp_url: str | None = None) -> AltruAgentClient:
    return AltruAgentClient(
        control_url=control_url,
        api_key="sk_agent_test",
        transport=httpx.MockTransport(handler),
        load_env_file=False,
        mcp_url=mcp_url,
    )


def login_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"access_token": "jwt-1"})


def detail_payload(**overrides) -> dict:
    payload = {
        "tournament_id": "t-1",
        "name": "Autumn Cup",
        "game_type": "red_alert",
        "game_family": "red_alert",
        "game_label": "Red Alert",
        "status": "in_progress",
        "phase": "bracket",
        "current_round": {"index": 5, "phase": "bracket", "number": 1, "label": "Semifinals"},
        "participant_count": 4,
        "standings": [{"rank": 1, "agent_id": "agent-a", "agent_name": "Alpha", "points": 2}],
        "final_ranking": None,
        "viewer": None,
    }
    payload.update(overrides)
    return payload


class RecordingCallTool:
    """Stands in for ``call_tool``: records each call, then answers with the
    next scripted payload (or raises it, if it's an exception)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, client, mcp_url, name, arguments):
        self.calls.append((mcp_url, name, arguments))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


# -- client.tournament(id) / client.competition(id) ----------------------------------------


def test_tournament_reads_the_detail_with_the_agent_token():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/agent/login":
            return login_ok(request)
        seen.append((request.method, request.url.path, request.headers["Authorization"]))
        return httpx.Response(200, json=detail_payload())

    detail = make_client(handler).tournament("t-1")

    assert seen == [("GET", "/tournaments/t-1", "Bearer jwt-1")]
    assert detail.name == "Autumn Cup"
    assert detail.current_round == "Semifinals"
    assert detail.standings[0].agent_name == "Alpha"
    assert detail.is_finished is False


def test_tournament_unknown_id_is_a_404_platform_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/agent/login":
            return login_ok(request)
        return httpx.Response(404, json={"error": "tournament_not_found"})

    with pytest.raises(PlatformError) as exc_info:
        make_client(handler).tournament("nope")

    assert exc_info.value.status_code == 404


def test_competition_returns_the_row_as_a_dict():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/agent/login":
            return login_ok(request)
        assert request.url.path == "/competitions/game-7"
        return httpx.Response(200, json={"session_id": "game-7", "status": "in_progress", "game_server_url": "host"})

    assert make_client(handler).competition("game-7")["game_server_url"] == "host"


def test_sessions_parses_tournament_matches():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/agent/login":
            return login_ok(request)
        return httpx.Response(200, json={
            "joined_sessions": [], "active_sessions": [], "completed_sessions": [],
            "tournament_matches": [{"tournament_id": "t-1", "session_id": "game-7", "status": "join_now",
                                    "seconds_left": 200, "opponents": [{"agent_id": "b", "agent_name": "B"}]}],
        })

    rows = make_client(handler).sessions().tournament_matches

    assert [(r.session_id, r.needs_join, r.seconds_left) for r in rows] == [("game-7", True, 200)]


def test_old_round_robin_client_methods_are_gone():
    client = make_client(login_ok)
    for name in ("tournaments", "join_tournament", "leave_tournament"):
        assert not hasattr(client, name)


# -- mcp_url --------------------------------------------------------------------------------


def test_mcp_url_defaults_to_the_deployed_platform_for_the_deployed_control_plane():
    client = make_client(login_ok, control_url="https://api.altruagent-game.com/")

    assert client.mcp_url == KNOWN_MCP_URLS["https://api.altruagent-game.com"]
    assert client.mcp_url == "https://gameapi.altruagent-game.com/mcp"


def test_mcp_url_is_none_for_an_unknown_control_plane():
    assert make_client(login_ok, control_url="http://localhost:3000").mcp_url is None


@pytest.mark.parametrize(
    "configured, expected",
    [
        ("gameapi.example.test", "https://gameapi.example.test/mcp"),
        ("https://gameapi.example.test/", "https://gameapi.example.test/mcp"),
        ("https://gameapi.example.test/mcp", "https://gameapi.example.test/mcp"),
        ("localhost:8000", "http://localhost:8000/mcp"),
    ],
)
def test_mcp_url_from_env_is_normalized(monkeypatch, configured, expected):
    monkeypatch.setenv("ALTRUAGENT_MCP_URL", configured)

    assert make_client(login_ok, control_url="https://api.altruagent-game.com").mcp_url == expected


def test_mcp_url_argument_wins_over_env(monkeypatch):
    monkeypatch.setenv("ALTRUAGENT_MCP_URL", "https://from-env.example.test")

    assert make_client(login_ok, mcp_url=MCP_URL).mcp_url == MCP_URL


# -- join_competition: MCP first ---------------------------------------------------------------


def _no_rest(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/auth/agent/login":
        return login_ok(request)
    raise AssertionError(f"unexpected control-plane request {request.method} {request.url.path}")


def test_join_competition_calls_mcp_join_session(monkeypatch):
    fake = RecordingCallTool({"status": "waiting", "session_id": "game-7", "position": 1,
                              "current_participants": 1, "max_participants": 2})
    monkeypatch.setattr(client_module, "call_tool", fake)

    result = make_client(_no_rest, mcp_url=MCP_URL).join_competition("game-7")

    assert fake.calls == [(MCP_URL, "join_session", {"session_id": "game-7"})]
    assert (result.status, result.already_joined, result.transport) == ("waiting", False, "mcp")


def test_join_competition_already_joined_is_success(monkeypatch):
    monkeypatch.setattr(client_module, "call_tool", RecordingCallTool(
        {"status": "in_progress", "session_id": "game-7", "already_joined": True}))

    result = make_client(_no_rest, mcp_url=MCP_URL).join_competition("game-7")

    assert result.already_joined is True and result.status == "in_progress"


@pytest.mark.parametrize("code, message", [
    ("not_in_this_match", "refused"),
    ("join_deadline_passed", "refused"),
    # GameAPI's catch-all, but with one of the backend's final refusal messages.
    ("SESSION_JOIN_FAILED", "Competition is full"),
    ("SESSION_JOIN_FAILED", "Competition not found"),
    ("SESSION_JOIN_FAILED", "Competition is not accepting participants"),
])
def test_join_competition_refusals_are_raised_without_a_rest_retry(monkeypatch, code, message):
    monkeypatch.setattr(client_module, "call_tool", RecordingCallTool(
        MCPToolError(message, status_code=None, error_code=code)))

    with pytest.raises(MCPToolError) as exc_info:
        make_client(_no_rest, mcp_url=MCP_URL).join_competition("game-7")

    assert exc_info.value.error_code == code


def _rest_join_handler(rest, logins=None, *, answer=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/agent/login":
            if logins is not None:
                logins.append(1)
                return httpx.Response(200, json={"access_token": f"jwt-{len(logins)}"})
            return login_ok(request)
        rest.append((request.method, request.url.path, request.headers["Authorization"]))
        if answer is not None:
            return answer
        return httpx.Response(200, json={"success": True, "session_id": "game-7", "status": "waiting"})

    return handler


@pytest.mark.parametrize("detail", ["Backend request failed.", "TypeError: fetch failed", "Internal Server Error"])
def test_join_competition_unexplained_session_join_failed_tries_rest_once(monkeypatch, detail):
    """SESSION_JOIN_FAILED is also what GameAPI answers when the control plane
    failed temporarily (a Supabase blip, a Lambda 500): the join is made once
    more through the control plane directly."""
    monkeypatch.setattr(client_module, "call_tool", RecordingCallTool(
        MCPToolError(detail, status_code=None, error_code="SESSION_JOIN_FAILED")))
    rest = []

    result = make_client(_rest_join_handler(rest), mcp_url=MCP_URL).join_competition("game-7")

    assert [(m, p) for m, p, _ in rest] == [("POST", "/competitions/game-7/join")]
    assert result.transport == "rest" and result.status == "waiting"


def test_join_competition_unexplained_failure_on_both_routes_keeps_the_rest_code(monkeypatch):
    monkeypatch.setattr(client_module, "call_tool", RecordingCallTool(
        MCPToolError("Backend request failed.", status_code=None, error_code="SESSION_JOIN_FAILED")))
    rest = []
    answer = httpx.Response(400, json={"error": "join_failed", "detail": "TypeError: fetch failed"})

    with pytest.raises(PlatformError) as exc_info:
        make_client(_rest_join_handler(rest, answer=answer), mcp_url=MCP_URL).join_competition("game-7")

    assert len(rest) == 1
    assert (exc_info.value.status_code, exc_info.value.error_code) == (400, "join_failed")


@pytest.mark.parametrize("code", ["Invalid or expired token", "Agent authentication required", "UNAUTHENTICATED"])
def test_join_competition_backend_token_rejection_over_mcp_relogs_in_and_joins_over_rest(monkeypatch, code):
    """GameAPI accepted the token but the control plane then didn't (it
    expired in between, or the auth service blipped): that arrives inside the
    tool answer, not as an HTTP 401, so the client logs in again itself."""
    monkeypatch.setattr(client_module, "call_tool", RecordingCallTool(
        MCPToolError(code, status_code=None, error_code=code)))
    rest, logins = [], []
    client = make_client(_rest_join_handler(rest, logins), mcp_url=MCP_URL)
    client.login()

    result = client.join_competition("game-7")

    assert len(logins) == 2  # the first token, then one fresh login
    assert rest == [("POST", "/competitions/game-7/join", "Bearer jwt-2")]
    assert result.transport == "rest" and result.status == "waiting"


def test_join_competition_backend_token_rejection_through_the_real_mcp_sdk(monkeypatch):
    """The real MCP client: the first join_session answers with the
    backend's 401 string as its error code; the join still succeeds after one
    re-login."""
    tool_calls = []

    async def mcp_server(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method, req_id = body.get("method"), body.get("id")
        if method == "initialize":
            return httpx.Response(200, json=_initialize_response(req_id))
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": req_id, "result": {"tools": [
                {"name": "join_session", "inputSchema": {"type": "object"}, "outputSchema": None}]}})
        if method == "tools/call":
            tool_calls.append(body["params"])
            return httpx.Response(200, json=_tool_call_response(req_id, {
                "error": "Invalid or expired token", "detail": "Invalid or expired token"}))
        return httpx.Response(404)

    monkeypatch.setattr(
        client_module, "call_tool",
        lambda client, url, name, args: real_call_tool(client, url, name, args, httpx_client_factory=make_factory(mcp_server)),
    )
    rest, logins = [], []

    result = make_client(_rest_join_handler(rest, logins), mcp_url=MCP_URL).join_competition("game-7")

    assert len(tool_calls) == 1 and len(logins) == 2
    assert [(m, p) for m, p, _ in rest] == [("POST", "/competitions/game-7/join")]
    assert result.status == "waiting"


def test_join_competition_relogin_failure_after_a_token_rejection_propagates(monkeypatch):
    monkeypatch.setattr(client_module, "call_tool", RecordingCallTool(
        MCPToolError("Invalid or expired token", status_code=None, error_code="Invalid or expired token")))
    logins = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/agent/login":
            logins.append(1)
            if len(logins) == 1:
                return httpx.Response(200, json={"access_token": "jwt-1"})
            return httpx.Response(401, json={"error": "Invalid API key"})
        raise AssertionError("no join is made without a token")

    client = make_client(handler, mcp_url=MCP_URL)
    client.login()

    with pytest.raises(AuthenticationError) as exc_info:
        client.join_competition("game-7")

    assert exc_info.value.transient is False


@pytest.mark.parametrize(
    "failure",
    [
        MCPToolError("unreachable", status_code=None, error_code=None),
        MCPToolError("HTTP 502", status_code=502, error_code=None),
        MCPToolError("backend down", status_code=None, error_code="BACKEND_UNAVAILABLE"),
    ],
)
def test_join_competition_falls_back_to_rest_when_mcp_gives_no_answer(monkeypatch, failure):
    monkeypatch.setattr(client_module, "call_tool", RecordingCallTool(failure))
    rest = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/agent/login":
            return login_ok(request)
        rest.append((request.method, request.url.path, request.content))
        return httpx.Response(200, json={"success": True, "session_id": "game-7", "status": "waiting",
                                         "position": 2, "participants": 2, "max_participants": 7})

    result = make_client(handler, mcp_url=MCP_URL).join_competition("game-7")

    assert rest == [("POST", "/competitions/game-7/join", b"")]
    assert result.transport == "rest" and result.current_participants == 2


def test_join_competition_uses_rest_when_no_mcp_url_is_known(monkeypatch):
    monkeypatch.setattr(client_module, "call_tool", RecordingCallTool())  # any call fails the test
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/agent/login":
            return login_ok(request)
        paths.append(request.url.path)
        return httpx.Response(200, json={"success": True, "session_id": "game-7", "status": "waiting"})

    result = make_client(handler, control_url="http://localhost:3000").join_competition("game-7")

    assert paths == ["/competitions/game-7/join"] and result.transport == "rest"


@pytest.mark.parametrize("code", ["not_in_this_match", "join_deadline_passed"])
def test_join_competition_rest_refusal_keeps_the_platform_code(code):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/agent/login":
            return login_ok(request)
        return httpx.Response(409, json={"error": code, "detail": "Refused."})

    with pytest.raises(PlatformError) as exc_info:
        make_client(handler, control_url="http://localhost:3000").join_competition("game-7")

    assert (exc_info.value.status_code, exc_info.value.error_code) == (409, code)


def test_join_competition_rest_401_relogs_in_once_and_retries():
    logins = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/agent/login":
            logins.append(1)
            return httpx.Response(200, json={"access_token": f"jwt-{len(logins)}"})
        if request.headers["Authorization"] == "Bearer jwt-1":
            return httpx.Response(401, json={"error": "Invalid or expired token"})
        return httpx.Response(200, json={"success": True, "session_id": "game-7", "status": "waiting"})

    result = make_client(handler, control_url="http://localhost:3000").join_competition("game-7")

    assert len(logins) == 2 and result.status == "waiting"


def test_join_competition_rest_second_401_raises_authentication_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/agent/login":
            return login_ok(request)
        return httpx.Response(401, json={"error": "Invalid or expired token"})

    with pytest.raises(AuthenticationError):
        make_client(handler, control_url="http://localhost:3000").join_competition("game-7")


def test_join_competition_through_the_real_mcp_sdk_surfaces_the_refusal_code(monkeypatch):
    """The real MCP client against a mocked MCP server: a join_session tool
    error payload arrives as MCPToolError with the platform's own code."""
    calls = []

    async def mcp_server(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method, req_id = body.get("method"), body.get("id")
        if method == "initialize":
            return httpx.Response(200, json=_initialize_response(req_id))
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": req_id, "result": {"tools": [
                {"name": "join_session", "inputSchema": {"type": "object"}, "outputSchema": None}]}})
        if method == "tools/call":
            calls.append(body["params"])
            return httpx.Response(200, json=_tool_call_response(req_id, {
                "error": "not_in_this_match",
                "detail": "This tournament game is reserved for its paired agents.",
                "next_actions": [{"tool": "get_agent_status", "hint": "Your own tournament games are listed under tournament_matches."}],
            }))
        return httpx.Response(404)

    monkeypatch.setattr(
        client_module, "call_tool",
        lambda client, url, name, args: real_call_tool(client, url, name, args, httpx_client_factory=make_factory(mcp_server)),
    )

    with pytest.raises(MCPToolError) as exc_info:
        make_client(_no_rest, mcp_url=MCP_URL).join_competition("game-7")

    assert calls == [{"name": "join_session", "arguments": {"session_id": "game-7"}}]
    assert exc_info.value.error_code == "not_in_this_match"
    assert "reserved for its paired agents" in str(exc_info.value)
