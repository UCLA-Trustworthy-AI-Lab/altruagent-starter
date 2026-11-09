"""MCP session reuse (altruagent.mcp_transport.MCPConnection / call_tool).

Like tests/test_mcp_transport.py, these drive the REAL official SDK client
(streamablehttp_client + ClientSession: JSON-RPC framing, initialize,
tools/list schema validation, anyio error wrapping) against an in-process
fake GameAPI MCP endpoint behind httpx.MockTransport. No sockets, no network.

The fake endpoint is stateless per request, like GameAPI's
``stateless_http=True`` server, and counts every JSON-RPC method so the
tests can assert exactly how many initialize / tools/list / tools/call
requests a match costs.
"""

from __future__ import annotations

import asyncio
import collections
import json
import threading
import time

import httpx
import pytest

from altruagent import mcp_transport
from altruagent.errors import AuthenticationError, is_transient_error
from altruagent.mcp_game import MCPGameSession
from altruagent.mcp_transport import MCPConnection, MCPToolError, call_tool
from altruagent.models import DecisionContext
from altruagent.runner import run_game

MCP_URL = "http://game.example.test/mcp"
GAME_SERVER_URL = "http://game.example.test"

TOOLS = [
    "get_game_state", "wait_for_update", "get_legal_actions", "play_action", "send_message",
    "get_messages", "resign", "get_result", "get_game_config", "list_games", "list_sessions",
]


class FakeClient:
    """Stand-in for AltruAgentClient: just the token source call_tool uses."""

    def __init__(self, tokens=("jwt-1",), *, refuse_relogin=False):
        self._tokens = list(tokens)
        self._index = 0
        self.relogins = 0
        self.refuse_relogin = refuse_relogin

    def _current_access_token(self, *, force_relogin=False):
        if force_relogin:
            if self.refuse_relogin:
                raise AuthenticationError("seat renewal refused")
            self.relogins += 1
            self._index = min(self._index + 1, len(self._tokens) - 1)
        return self._tokens[self._index]


class FakeGameAPI:
    """A tiny phased game served over MCP JSON-RPC.

    One player; phases ``draft`` (2 picks) -> ``teambuild`` (1 submit) ->
    ``moving`` (3 moves) -> terminal. ``state_version`` bumps on every move,
    legal actions are derived from the *current* phase/version on every
    read, and a ``play_action`` with a stale ``state_version`` or an action
    that isn't legal right now is refused (``STALE_STATE``/``INVALID_ACTION``)
    — so any cached state or legal-action list on the client side would show
    up as a refusal.
    """

    PLAN = [("draft", 2), ("teambuild", 1), ("moving", 3)]

    def __init__(self, *, accepted_tokens=None, latency=0.0):
        self.lock = threading.Lock()
        self.counts = collections.Counter()
        self.auth_seen = []
        self.method_log = []
        self.accepted_tokens = accepted_tokens  # None: accept any bearer
        self.latency = latency
        self.games = {}
        self.refusals = []
        # One-shot fault injection: method or tool name -> callable(request) raising/returning.
        self.faults = {}

    # -- game model -------------------------------------------------------------------

    def _game(self, session_id):
        return self.games.setdefault(session_id, {"step": 0, "version": 1, "history": []})

    def _phase(self, game):
        step = game["step"]
        for phase, count in self.PLAN:
            if step < count:
                return phase, step
            step -= count
        return None, None

    def _legal(self, session_id, game):
        phase, index = self._phase(game)
        if phase is None:
            return []
        return [f"{phase}:{game['version']}:{option}" for option in ("a", "b")]

    def state(self, session_id):
        game = self._game(session_id)
        phase, _ = self._phase(game)
        terminal = phase is None
        payload = {
            "session_id": session_id,
            "game_type": "fake_phased",
            "status": "completed" if terminal else "in_progress",
            "state_version": game["version"],
            "phase": phase or "complete",
            "is_current_actor": not terminal,
            "is_terminal": terminal,
            "current_player": 0,
            "observation": {"history": list(game["history"])},
        }
        if not terminal:
            payload["legal_actions"] = {
                "session_id": session_id,
                "state_version": game["version"],
                "actions": [{"action_id": a, "label": a} for a in self._legal(session_id, game)],
            }
        return payload

    def call(self, tool, args):
        session_id = args.get("session_id", "s-1")
        with self.lock:
            game = self._game(session_id)
            if tool in ("get_game_state", "wait_for_update"):
                return self.state(session_id)
            if tool == "get_legal_actions":
                return self.state(session_id).get("legal_actions") or {
                    "session_id": session_id, "state_version": game["version"], "actions": []}
            if tool == "play_action":
                if args.get("state_version") != game["version"]:
                    self.refusals.append(("STALE_STATE", args))
                    return {"error": "STALE_STATE", "detail": "stale"}
                if args.get("action_id") not in self._legal(session_id, game):
                    self.refusals.append(("INVALID_ACTION", args))
                    return {"error": "INVALID_ACTION", "detail": "not legal now"}
                game["history"].append(args["action_id"])
                game["step"] += 1
                game["version"] += 1
                result = {"accepted": True, "session_id": session_id, "state_version": game["version"]}
                state = self.state(session_id)
                result["status"] = state["status"]
                if not state["is_terminal"]:
                    result["state"] = state
                return result
            if tool == "get_result":
                return {"session_id": session_id, "is_terminal": self._phase(game)[0] is None,
                        "returns": [1.0], "termination_reason": "completed"}
            if tool == "echo":
                return {"echo": args}
            return {"session_id": session_id}

    # -- JSON-RPC over HTTP -------------------------------------------------------------

    async def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method, req_id = body.get("method"), body.get("id")
        tool = body.get("params", {}).get("name") if method == "tools/call" else None
        with self.lock:
            self.counts[method] += 1
            self.counts["http"] += 1
            if tool:
                self.counts[f"tool:{tool}"] += 1
            self.auth_seen.append(request.headers.get("authorization"))
            self.method_log.append(method)
            fault = self.faults.pop(tool, None) or self.faults.pop(method, None)
        if fault is not None:
            answer = fault(request)
            if answer is not None:
                return answer
        token = (request.headers.get("authorization") or "").removeprefix("Bearer ")
        if self.accepted_tokens is not None and token not in self.accepted_tokens:
            return httpx.Response(401, json={"error": "invalid token"})
        if method == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": req_id, "result": {
                "protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-gameapi", "version": "0"}}})
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            tools = [{"name": name, "inputSchema": {"type": "object"}} for name in TOOLS + ["echo"]]
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}})
        if method == "tools/call":
            if self.latency:
                await asyncio.sleep(self.latency)
            payload = self.call(tool, body["params"].get("arguments") or {})
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": req_id, "result": {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "structuredContent": payload, "isError": False}})
        return httpx.Response(404)

    def factory(self, headers=None, timeout=None, auth=None):
        # Looked up per request, so a test can swap the handler mid-session.
        transport = httpx.MockTransport(lambda request: self.handler(request))
        return httpx.AsyncClient(transport=transport, headers=headers, timeout=timeout)


def _session_threads():
    return [t for t in threading.enumerate() if t.name.startswith("mcp-session") and t.is_alive()]


@pytest.fixture
def server():
    return FakeGameAPI()


@pytest.fixture(autouse=True)
def _reuse_on(monkeypatch):
    monkeypatch.delenv(mcp_transport.SESSION_REUSE_ENV, raising=False)


@pytest.fixture
def connection(server):
    conn = MCPConnection(FakeClient(), MCP_URL, httpx_client_factory=server.factory)
    yield conn
    conn.close()


def _game(client, server, session_id="s-1"):
    """A real MCPGameSession whose calls go through the real call_tool."""
    game = MCPGameSession(client, session_id=session_id, game_server_url=GAME_SERVER_URL)
    game._call = lambda tool, arguments: call_tool(
        game._client, game._mcp_url(), tool, arguments, httpx_client_factory=server.factory)
    return game


def _play_first_legal(state, context):
    return state.legal_actions[0]


CONTEXT = DecisionContext(session_id="s-1", tournament_id=None, game_type="fake_phased", agent_id="agent-1")


# 1. Multiple tool calls reuse one initialization ---------------------------------------


def test_many_calls_cost_one_initialize_and_one_tools_list(connection, server):
    for i in range(20):
        assert connection.call_tool("echo", {"i": i}) == {"echo": {"i": i}}

    assert server.counts["initialize"] == 1
    assert server.counts["notifications/initialized"] == 1
    assert server.counts["tools/list"] == 1
    assert server.counts["tools/call"] == 20
    assert server.counts["http"] == 23  # 20 calls + 3 one-time session requests
    assert connection.initializations == 1


def test_call_tool_shares_one_session_per_client_and_endpoint(server):
    client = FakeClient()
    for i in range(5):
        call_tool(client, MCP_URL, "echo", {"i": i}, httpx_client_factory=server.factory)
    assert (server.counts["initialize"], server.counts["tools/list"], server.counts["tools/call"]) == (1, 1, 5)
    mcp_transport.close_connections(client)


def test_opt_out_env_restores_one_session_per_call(server, monkeypatch):
    monkeypatch.setenv(mcp_transport.SESSION_REUSE_ENV, "0")
    before = set(_session_threads())  # other test modules' clients may not be collected yet
    client = FakeClient()
    for i in range(3):
        call_tool(client, MCP_URL, "echo", {"i": i}, httpx_client_factory=server.factory)
    assert (server.counts["initialize"], server.counts["tools/list"], server.counts["tools/call"]) == (3, 3, 3)
    assert set(_session_threads()) <= before  # every per-call session closed its thread


# 2. Current legal actions change across phases (nothing cached) -----------------------


def test_legal_actions_are_read_fresh_in_every_phase(connection, server):
    seen = []
    while True:
        state = connection.call_tool("get_game_state", {"session_id": "s-1"})
        if state["is_terminal"]:
            break
        actions = [a["action_id"] for a in state["legal_actions"]["actions"]]
        seen.append((state["phase"], state["state_version"], actions))
        connection.call_tool("play_action", {"session_id": "s-1", "action_id": actions[0],
                                             "state_version": state["state_version"]})

    assert [phase for phase, _, _ in seen] == ["draft", "draft", "teambuild", "moving", "moving", "moving"]
    # Every read carried that moment's version and actions, never a previous one.
    assert [version for _, version, _ in seen] == [1, 2, 3, 4, 5, 6]
    assert all(actions == [f"{p}:{v}:a", f"{p}:{v}:b"] for p, v, actions in seen)
    assert server.refusals == []
    assert server.counts["initialize"] == 1 and server.counts["tools/list"] == 1


def test_identical_reads_still_go_to_the_server(connection, server):
    connection.call_tool("get_legal_actions", {"session_id": "s-1"})
    server.games["s-1"]["step"] = 2  # the game moves on outside this client
    server.games["s-1"]["version"] = 9
    second = connection.call_tool("get_legal_actions", {"session_id": "s-1"})
    assert second["state_version"] == 9
    assert [a["action_id"] for a in second["actions"]] == ["teambuild:9:a", "teambuild:9:b"]
    assert server.counts["tool:get_legal_actions"] == 2


# 3. Full gameplay through the runner, no stale state or actions -----------------------


def test_full_match_through_run_game_on_one_session(server):
    client = FakeClient()
    final = run_game(_game(client, server), CONTEXT, _play_first_legal, sleep=lambda s: None)

    assert final.is_terminal
    assert server.games["s-1"]["history"] == [
        "draft:1:a", "draft:2:a", "teambuild:3:a", "moving:4:a", "moving:5:a", "moving:6:a"]
    assert server.refusals == []
    assert server.counts["initialize"] == 1 and server.counts["tools/list"] == 1
    mcp_transport.close_connections(client)


# 4. Reconnection after a dropped session -----------------------------------------------


def _drop(request):
    raise httpx.RemoteProtocolError("Server disconnected without sending a response.", request=request)


def test_a_dropped_reused_session_is_reconnected_once_for_a_read(connection, server):
    connection.call_tool("get_game_state", {"session_id": "s-1"})
    server.faults["get_game_state"] = _drop

    state = connection.call_tool("get_game_state", {"session_id": "s-1"})

    assert state["state_version"] == 1
    assert server.counts["initialize"] == 2  # one new session, then back to normal
    assert connection.initializations == 2
    connection.call_tool("get_game_state", {"session_id": "s-1"})
    assert server.counts["initialize"] == 2


def test_a_move_on_a_dropped_session_is_never_sent_twice(connection, server):
    state = connection.call_tool("get_game_state", {"session_id": "s-1"})
    server.faults["play_action"] = _drop
    move = {"session_id": "s-1", "action_id": "draft:1:a", "state_version": state["state_version"]}

    with pytest.raises(MCPToolError) as exc_info:
        connection.call_tool("play_action", move)

    assert is_transient_error(exc_info.value)  # the runner re-reads state and carries on
    assert server.counts["tool:play_action"] == 1
    # The next call starts a fresh session by itself.
    assert connection.call_tool("get_game_state", {"session_id": "s-1"})["state_version"] == 1
    assert server.counts["initialize"] == 2


@pytest.mark.parametrize("tool, arguments", [
    ("send_message", {"session_id": "s-1", "message_type": "chat", "content": "hi"}),
    ("resign", {"session_id": "s-1"}),
])
def test_a_message_or_resignation_on_a_dropped_session_is_never_sent_twice(connection, server, tool, arguments):
    connection.call_tool("get_game_state", {"session_id": "s-1"})
    server.faults[tool] = _drop

    with pytest.raises(MCPToolError) as exc_info:
        connection.call_tool(tool, arguments)

    assert is_transient_error(exc_info.value)
    assert server.counts[f"tool:{tool}"] == 1


def test_a_timed_out_move_is_never_sent_twice(connection, server, monkeypatch):
    monkeypatch.setattr(mcp_transport, "MCP_SESSION_TIMEOUT_SECONDS", 0.3)
    connection.call_tool("get_game_state", {"session_id": "s-1"})
    original = server.handler
    moves_sent = []

    async def handler(request):
        if json.loads(request.content).get("params", {}).get("name") == "play_action":
            moves_sent.append(request)
            await asyncio.sleep(30)  # the answer never comes
        return await original(request)

    server.handler = handler
    with pytest.raises(MCPToolError) as exc_info:
        connection.call_tool("play_action", {"session_id": "s-1", "action_id": "draft:1:a", "state_version": 1})

    assert is_transient_error(exc_info.value)
    assert len(moves_sent) == 1


def test_a_dropped_connection_on_a_fresh_session_is_left_to_the_runner(connection, server):
    server.faults["get_game_state"] = _drop
    with pytest.raises(MCPToolError):
        connection.call_tool("get_game_state", {"session_id": "s-1"})
    assert server.counts["tool:get_game_state"] == 1


def test_a_server_error_ends_the_session_and_the_next_call_reinitializes(connection, server):
    connection.call_tool("echo", {})
    server.faults["echo"] = lambda request: httpx.Response(502, text="bad gateway")

    with pytest.raises(MCPToolError) as exc_info:
        connection.call_tool("echo", {})
    assert exc_info.value.status_code == 502 and is_transient_error(exc_info.value)

    assert connection.call_tool("echo", {"again": 1}) == {"echo": {"again": 1}}
    assert server.counts["initialize"] == 2


def test_the_runner_survives_a_dropped_session_mid_match(server):
    client = FakeClient()
    game = _game(client, server)
    server.faults["play_action"] = _drop  # the first move's connection drops before the server sees it

    final = run_game(game, CONTEXT, _play_first_legal, sleep=lambda s: None, now=time.monotonic)

    assert final.is_terminal
    assert server.refusals == []
    assert len(server.games["s-1"]["history"]) == 6
    assert server.counts["initialize"] == 2
    mcp_transport.close_connections(client)


def test_a_timed_out_call_retires_its_session(connection, server, monkeypatch):
    monkeypatch.setattr(mcp_transport, "MCP_SESSION_TIMEOUT_SECONDS", 0.3)
    connection.call_tool("echo", {})

    original = server.handler

    async def handler(request):
        body = json.loads(request.content)
        if body.get("params", {}).get("name") == "wait_for_update" and not handler.hung:
            handler.hung = True
            await asyncio.sleep(30)
        return await original(request)

    handler.hung = False
    server.handler = handler

    started = time.monotonic()
    with pytest.raises(MCPToolError):
        connection.call_tool("wait_for_update", {"session_id": "s-1", "since_version": 1})
    assert time.monotonic() - started < 10

    monkeypatch.setattr(mcp_transport, "MCP_SESSION_TIMEOUT_SECONDS", 45.0)
    assert connection.call_tool("echo", {"after": 1}) == {"echo": {"after": 1}}
    assert connection.initializations == 2  # the stuck session was not reused


# 5. Authentication failures and token refresh ------------------------------------------


def test_an_expired_token_mid_session_relogs_in_once_and_resumes(server):
    client = FakeClient(tokens=["jwt-1", "jwt-2"])
    conn = MCPConnection(client, MCP_URL, httpx_client_factory=server.factory)
    try:
        conn.call_tool("echo", {"n": 1})
        server.accepted_tokens = {"jwt-2"}  # jwt-1 expires

        assert conn.call_tool("echo", {"n": 2}) == {"echo": {"n": 2}}
        assert client.relogins == 1
        for n in range(3, 6):
            conn.call_tool("echo", {"n": n})
        assert client.relogins == 1  # the refreshed session is reused, no more logins
        assert server.counts["initialize"] == 2
        assert server.auth_seen[-1] == "Bearer jwt-2"
    finally:
        conn.close()


def test_a_token_renewed_elsewhere_is_used_from_the_next_call(server):
    """A re-login through another path on the same client (e.g. a REST
    call's 401 retry) moves MCP to the new token at once, as the per-call
    transport did, instead of waiting for the old one to be refused."""
    client = FakeClient(tokens=["jwt-1", "jwt-2"])
    with MCPConnection(client, MCP_URL, httpx_client_factory=server.factory) as conn:
        conn.call_tool("echo", {"n": 1})
        client._index = 1  # the client now holds jwt-2
        server.accepted_tokens = {"jwt-2"}

        assert conn.call_tool("echo", {"n": 2}) == {"echo": {"n": 2}}
        conn.call_tool("echo", {"n": 3})

    assert client.relogins == 0  # no 401, no extra login
    assert server.counts["initialize"] == 2
    assert server.auth_seen[-1] == "Bearer jwt-2"


def test_a_refused_relogin_reaches_the_caller_unchanged(server):
    client = FakeClient(refuse_relogin=True)
    server.accepted_tokens = set()
    with MCPConnection(client, MCP_URL, httpx_client_factory=server.factory) as conn:
        with pytest.raises(AuthenticationError):
            conn.call_tool("echo", {})


def test_a_second_401_after_relogin_stops(server):
    client = FakeClient(tokens=["jwt-1", "jwt-2"])
    server.accepted_tokens = set()
    with MCPConnection(client, MCP_URL, httpx_client_factory=server.factory) as conn:
        with pytest.raises(MCPToolError) as exc_info:
            conn.call_tool("echo", {})
    assert exc_info.value.status_code == 401
    assert client.relogins == 1
    assert server.counts["initialize"] == 2


# 6. Simultaneous independent matches ---------------------------------------------------


def test_simultaneous_matches_each_get_their_own_session_and_loop(server):
    server.latency = 0.005
    results, errors = {}, []
    clients = {f"s-{i}": FakeClient() for i in range(4)}

    def play(session_id):
        try:
            context = DecisionContext(session_id=session_id, tournament_id=None,
                                      game_type="fake_phased", agent_id=f"agent-{session_id}")
            results[session_id] = run_game(_game(clients[session_id], server, session_id), context,
                                           _play_first_legal, sleep=lambda s: None)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=play, args=(sid,)) for sid in clients]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert errors == []
    assert all(results[sid].is_terminal for sid in clients)
    for sid in clients:
        assert server.games[sid]["history"][0] == "draft:1:a" and len(server.games[sid]["history"]) == 6
    assert server.refusals == []
    assert server.counts["initialize"] == 4  # one per match, not one per call
    assert len({t.name for t in _session_threads()}) >= 1
    for client in clients.values():
        mcp_transport.close_connections(client)


# 7. Concurrent calls on one session -----------------------------------------------------


def test_concurrent_calls_on_one_session_stay_matched_to_their_callers(connection, server):
    server.latency = 0.002
    errors, mismatches = [], []

    def worker(w):
        for i in range(25):
            try:
                answer = connection.call_tool("echo", {"w": w, "i": i})
                if answer != {"echo": {"w": w, "i": i}}:
                    mismatches.append((w, i, answer))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)

    assert errors == [] and mismatches == []
    assert server.counts["tools/call"] == 200
    assert server.counts["initialize"] == 1


def _fail_under(connection, server, write_tool, write_args, read_fault):
    """Start ``write_tool`` (held 0.5 s by the server), then fail a concurrent
    read on the same session with ``read_fault``. Returns (write outcome,
    read outcome)."""
    server.latency = 0.5
    connection.call_tool("echo", {})
    outcome = {}

    def write():
        try:
            outcome["write"] = connection.call_tool(write_tool, write_args)
        except Exception as exc:  # noqa: BLE001
            outcome["write"] = exc

    thread = threading.Thread(target=write)
    thread.start()
    time.sleep(0.15)  # the write is in flight
    server.faults["get_game_state"] = read_fault
    try:
        outcome["read"] = connection.call_tool("get_game_state", {"session_id": "s-1"})
    except Exception as exc:  # noqa: BLE001
        outcome["read"] = exc
    thread.join(10)
    return outcome["write"], outcome["read"]


@pytest.mark.parametrize("tool, arguments", [
    ("play_action", {"session_id": "s-1", "action_id": "draft:1:a", "state_version": 1}),
    ("send_message", {"session_id": "s-1", "message_type": "chat", "content": "hi"}),
])
def test_a_write_is_not_resent_when_a_concurrent_call_ends_the_session(server, tool, arguments):
    """Another call's 401 ends the shared session under an in-flight move or
    message. That write may have reached the server, so it is not resent
    after the re-login; it fails as a temporary error (the runner re-reads)."""
    client = FakeClient(tokens=["jwt-1", "jwt-2"])
    with MCPConnection(client, MCP_URL, httpx_client_factory=server.factory) as conn:
        write, read = _fail_under(conn, server, tool, arguments,
                                  lambda request: httpx.Response(401, json={"error": "expired"}))

    assert isinstance(write, MCPToolError) and is_transient_error(write)
    assert "not resent" in str(write)
    assert server.counts[f"tool:{tool}"] == 1
    assert isinstance(read, dict)  # the read itself re-logged in and was retried
    assert client.relogins == 1


def test_a_lone_write_is_still_retried_after_a_401(server):
    """Without a concurrent call the 401 is the write's own auth check, so the
    tool never ran: one re-login and one retry, as before."""
    client = FakeClient(tokens=["jwt-1", "jwt-2"])
    with MCPConnection(client, MCP_URL, httpx_client_factory=server.factory) as conn:
        conn.call_tool("echo", {})
        server.accepted_tokens = {"jwt-2"}
        answer = conn.call_tool("play_action", {"session_id": "s-1", "action_id": "draft:1:a", "state_version": 1})
    assert answer["accepted"] is True
    assert server.games["s-1"]["history"] == ["draft:1:a"]


def test_clients_in_one_process_never_share_a_session_or_token(server):
    seat_a, seat_b = FakeClient(tokens=["jwt-a"]), FakeClient(tokens=["jwt-b"])
    for i in range(3):
        call_tool(seat_a, MCP_URL, "echo", {"i": i}, httpx_client_factory=server.factory)
        call_tool(seat_b, MCP_URL, "echo", {"i": i}, httpx_client_factory=server.factory)

    assert server.counts["initialize"] == 2  # one session per seat
    tool_calls = [a for a, method in zip(server.auth_seen, server.method_log) if method == "tools/call"]
    assert tool_calls == ["Bearer jwt-a", "Bearer jwt-b"] * 3
    for client in (seat_a, seat_b):
        mcp_transport.close_connections(client)


def test_a_forked_child_starts_its_own_sessions(server):
    """What os.register_at_fork runs in a forked child (the runtime itself
    uses spawn): the parent's loop thread doesn't exist there, so every
    connection starts afresh on its next call instead of hanging."""
    conn = MCPConnection(FakeClient(), MCP_URL, httpx_client_factory=server.factory)
    conn.call_tool("echo", {})
    parent_loop, parent_thread = conn._loop, conn._thread
    try:
        mcp_transport._reset_connections_after_fork()
        assert conn._loop is None and conn._current is None
        assert conn.call_tool("echo", {"child": 1}) == {"echo": {"child": 1}}
        assert conn._thread is not parent_thread
        assert conn.initializations == 2
    finally:
        conn.close()
        parent_loop.call_soon_threadsafe(parent_loop.stop)  # the "parent's" loop, in this one process
        parent_thread.join(5)


def test_a_call_from_inside_a_running_event_loop_works(connection):
    async def agent_code():
        return connection.call_tool("echo", {"from": "async"})

    assert asyncio.run(agent_code()) == {"echo": {"from": "async"}}


# 8. Cleanup on shutdown and errors --------------------------------------------------------


def test_close_stops_the_loop_thread_and_a_later_call_reopens(server):
    before = len(_session_threads())
    conn = MCPConnection(FakeClient(), MCP_URL, httpx_client_factory=server.factory)
    conn.call_tool("echo", {})
    assert len(_session_threads()) == before + 1

    conn.close()
    conn.close()  # idempotent
    assert len(_session_threads()) == before

    assert conn.call_tool("echo", {"reopened": True}) == {"echo": {"reopened": True}}
    conn.close()
    assert len(_session_threads()) == before
    assert server.counts["initialize"] == 2


def test_close_during_a_call_fails_that_call_promptly(server):
    server.latency = 5.0
    conn = MCPConnection(FakeClient(), MCP_URL, httpx_client_factory=server.factory)
    errors = []

    def slow_call():
        try:
            conn.call_tool("echo", {})
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=slow_call)
    thread.start()
    time.sleep(0.3)
    started = time.monotonic()
    conn.close()
    thread.join(5)

    assert not thread.is_alive()
    assert time.monotonic() - started < 5
    assert len(errors) == 1 and isinstance(errors[0], MCPToolError)


def test_failed_sessions_leave_no_threads_or_hosts_behind(server):
    before = len(_session_threads())
    server.accepted_tokens = set()
    conn = MCPConnection(FakeClient(), MCP_URL, httpx_client_factory=server.factory)
    for _ in range(3):
        with pytest.raises(MCPToolError):
            conn.call_tool("echo", {})
    assert conn._hosts == set()  # every failed session already finished
    conn.close()
    assert len(_session_threads()) == before


def test_altruagent_client_close_ends_its_mcp_sessions(server):
    from altruagent.client import AltruAgentClient

    def control_plane(request):
        return httpx.Response(200, json={"access_token": "jwt-real"})

    client = AltruAgentClient(control_url="https://control.example.test", api_key="sk_agent_test",
                              transport=httpx.MockTransport(control_plane), load_env_file=False)
    before = len(_session_threads())
    call_tool(client, MCP_URL, "echo", {}, httpx_client_factory=server.factory)
    call_tool(client, MCP_URL, "echo", {}, httpx_client_factory=server.factory)
    assert len(_session_threads()) == before + 1
    assert server.auth_seen[0] == "Bearer jwt-real"

    client.close()

    assert len(_session_threads()) == before
    assert server.counts["initialize"] == 1


def test_a_garbage_collected_client_releases_its_session(server):
    import gc

    before = len(_session_threads())
    client = FakeClient()
    call_tool(client, MCP_URL, "echo", {}, httpx_client_factory=server.factory)
    assert len(_session_threads()) == before + 1
    del client
    gc.collect()
    deadline = time.monotonic() + 5
    while len(_session_threads()) > before and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(_session_threads()) == before
