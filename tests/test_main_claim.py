"""Tests for claim mode in agent/__main__.py (``python -m agent --claim ...``)
plus a guard that the no-argument workflow still takes the discovery/
supervisor path. No real network: the control plane is an
``httpx.MockTransport``, ``run_game`` is replaced by a recorder, and any
call to ``me()``/``sessions()``/the supervisor fails the test.
"""

from __future__ import annotations

import json
import sys
import textwrap
import types

import httpx
import pytest

import agent.__main__ as agent_main
import agent.agent as default_agent_module
from altruagent.auth import SEAT_CLAIM_PATH, SeatGrantAuth
from altruagent.client import AltruAgentClient
from altruagent.mcp_game import MCPGameSession
from altruagent.runner import DecisionError

CLAIM_TOKEN = "seatclaim_cli_secret_token"
CONTROL_URL = "https://control.example.test"


def _grant(access_token="seat-jwt-1", seat_position=1, match_id="match-1"):
    return {
        "access_token": access_token,
        "expires_at": "2026-09-26T12:00:00.000Z",
        "agent_id": f"synthetic-agent-{seat_position}",
        "gameapi_server_url": "https://gameapi.example.test",
        "game_session_id": f"game-session-{match_id}",
        "match_id": match_id,
        "seat_id": f"seat-{seat_position}",
        "seat_position": seat_position,
        "seat_count": 2,
        "game_type": "pokemon_vgc_doubles_draft",
        "match_status": "starting",
    }


def _fail(message):
    def raiser(*args, **kwargs):
        raise AssertionError(message)

    return raiser


class ClaimHarness:
    """Wires agent_main to a mock control plane and a fake run_game, and
    records everything claim mode does. Any request other than the seat
    claim (e.g. /auth/agent/login, /auth/agent/me, /agents/me/sessions) is
    recorded in ``forbidden``.
    """

    def __init__(self, monkeypatch, *, response=None):
        self.response = response or (lambda body: httpx.Response(200, json=_grant()))
        self.claims: list[dict] = []
        self.clients: list[AltruAgentClient] = []
        self.runs: list[tuple] = []
        self.forbidden: list[str] = []

        monkeypatch.delenv("ALTRUAGENT_API_KEY", raising=False)
        monkeypatch.delenv("ALTRUAGENT_CLAIM_TOKEN", raising=False)
        monkeypatch.setenv("ALTRUAGENT_CONTROL_URL", CONTROL_URL)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path == SEAT_CLAIM_PATH:
                body = json.loads(request.content)
                self.claims.append(body)
                return self.response(body)
            self.forbidden.append(request.url.path)
            return httpx.Response(500)

        def client_factory(*args, **kwargs):
            assert "api_key" not in kwargs
            client = AltruAgentClient(
                *args, **kwargs, transport=httpx.MockTransport(handler), load_env_file=False
            )
            self.clients.append(client)
            return client

        def fake_run_game(game, context, contestant):
            self.runs.append((game, context, contestant))
            return types.SimpleNamespace(termination_reason="normal", returns={"0": 1.0, "1": -1.0})

        monkeypatch.setattr(agent_main, "AltruAgentClient", client_factory)
        monkeypatch.setattr(agent_main, "run_game", fake_run_game)
        monkeypatch.setattr(agent_main, "run_forever_concurrent", _fail("claim mode started the supervisor"))
        monkeypatch.setattr(AltruAgentClient, "me", _fail("claim mode called me()"))
        monkeypatch.setattr(AltruAgentClient, "sessions", _fail("claim mode called sessions()"))


def _write_agent_module(tmp_path, monkeypatch, name: str, body: str) -> None:
    (tmp_path / f"{name}.py").write_text(textwrap.dedent(body), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, name, raising=False)


# -- claim token sources ---------------------------------------------------------


def test_claim_flag_claims_seat_and_runs_game_with_default_agent(monkeypatch, capsys):
    harness = ClaimHarness(monkeypatch)

    assert agent_main.main(["--claim", CLAIM_TOKEN]) == 0

    assert len(harness.claims) == 1
    assert harness.claims[0]["claim_token"] == CLAIM_TOKEN
    assert harness.forbidden == []

    game, context, contestant = harness.runs[0]
    assert isinstance(game, MCPGameSession)
    assert game.session_id == "game-session-match-1"
    assert game.game_server_url == "https://gameapi.example.test"
    assert game._client is harness.clients[0]
    assert isinstance(harness.clients[0].auth, SeatGrantAuth)
    assert context.session_id == "game-session-match-1"
    assert context.agent_id == "synthetic-agent-1"
    assert context.game_type == "pokemon_vgc_doubles_draft"
    assert context.seat_position == 1
    assert context.tournament_id is None
    assert contestant is default_agent_module.choose_action  # the normal create_agent()

    out = capsys.readouterr().out
    assert "Claimed seat 2/2" in out
    assert "pokemon_vgc_doubles_draft" in out
    assert "match-1" in out
    assert "termination_reason=normal" in out


def test_claim_mode_never_prints_secrets(monkeypatch, capsys):
    harness = ClaimHarness(
        monkeypatch, response=lambda body: httpx.Response(200, json=_grant(access_token="seat-jwt-SECRET"))
    )

    agent_main.main(["--claim", CLAIM_TOKEN])

    out = capsys.readouterr().out
    assert CLAIM_TOKEN not in out
    assert harness.claims[0]["claim_key"] not in out
    assert "seat-jwt-SECRET" not in out


def test_claim_dash_prompts_without_echo(monkeypatch):
    harness = ClaimHarness(monkeypatch)
    prompts = []

    def fake_getpass(prompt):
        prompts.append(prompt)
        return CLAIM_TOKEN

    monkeypatch.setattr(agent_main.getpass, "getpass", fake_getpass)

    assert agent_main.main(["--claim", "-"]) == 0
    assert len(prompts) == 1
    assert harness.claims[0]["claim_token"] == CLAIM_TOKEN


def test_claim_dash_with_empty_input_fails_without_request(monkeypatch):
    harness = ClaimHarness(monkeypatch)
    monkeypatch.setattr(agent_main.getpass, "getpass", lambda prompt: "")

    assert agent_main.main(["--claim", "-"]) == 1
    assert harness.claims == []


def test_claim_token_from_environment(monkeypatch):
    harness = ClaimHarness(monkeypatch)
    monkeypatch.setenv("ALTRUAGENT_CLAIM_TOKEN", CLAIM_TOKEN)

    assert agent_main.main([]) == 0
    assert harness.claims[0]["claim_token"] == CLAIM_TOKEN
    assert len(harness.runs) == 1


def test_claim_flag_takes_precedence_over_environment(monkeypatch):
    harness = ClaimHarness(monkeypatch)
    monkeypatch.setenv("ALTRUAGENT_CLAIM_TOKEN", "seatclaim_from_env")

    agent_main.main(["--claim", CLAIM_TOKEN])

    assert harness.claims[0]["claim_token"] == CLAIM_TOKEN


def test_malformed_claim_token_fails_before_any_request(monkeypatch, capsys):
    harness = ClaimHarness(monkeypatch)

    assert agent_main.main(["--claim", "sk_agent_oops"]) == 1
    assert harness.claims == []
    assert "sk_agent_oops" not in capsys.readouterr().out


# -- claim errors / configuration --------------------------------------------------


@pytest.mark.parametrize(
    "status, code, phrase",
    [
        (404, "invalid_claim_token", "isn't valid"),
        (409, "seat_already_claimed", "already claimed"),
        (410, "claim_expired", "expired"),
        (409, "match_not_claimable", "no longer active"),
    ],
)
def test_claim_errors_are_reported_clearly(monkeypatch, capsys, status, code, phrase):
    harness = ClaimHarness(
        monkeypatch,
        response=lambda body: httpx.Response(status, json={"error": code, "detail": "raw backend detail"}),
    )

    assert agent_main.main(["--claim", CLAIM_TOKEN]) == 1

    out = capsys.readouterr().out
    assert "Could not claim seat" in out
    assert phrase in out
    assert "raw backend detail" not in out
    assert CLAIM_TOKEN not in out
    assert harness.runs == []


def test_claim_mode_missing_control_url_is_a_configuration_error(monkeypatch, capsys):
    harness = ClaimHarness(monkeypatch)
    monkeypatch.delenv("ALTRUAGENT_CONTROL_URL")

    assert agent_main.main(["--claim", CLAIM_TOKEN]) == 1
    assert "ALTRUAGENT_CONTROL_URL" in capsys.readouterr().out
    assert harness.claims == []


def test_match_failure_exits_nonzero(monkeypatch, capsys):
    ClaimHarness(monkeypatch)
    monkeypatch.setattr(agent_main, "run_game", _raise(DecisionError("choose_action raised boom")))

    assert agent_main.main(["--claim", CLAIM_TOKEN]) == 1
    assert "Match failed" in capsys.readouterr().out


def test_ctrl_c_stops_cleanly_without_resigning(monkeypatch, capsys):
    ClaimHarness(monkeypatch)
    monkeypatch.setattr(agent_main, "run_game", _raise(KeyboardInterrupt()))

    assert agent_main.main(["--claim", CLAIM_TOKEN]) == 0
    assert "Stopped" in capsys.readouterr().out


def _raise(exc):
    def raiser(*args, **kwargs):
        raise exc

    return raiser


# -- --agent -------------------------------------------------------------------------


def test_agent_override_uses_the_named_factory(monkeypatch, tmp_path):
    harness = ClaimHarness(monkeypatch)
    _write_agent_module(
        tmp_path,
        monkeypatch,
        "experimental_agent_a",
        """
        class Experimental:
            def choose_action(self, state, context):
                return state.legal_actions[0]

        def build():
            return Experimental()
        """,
    )

    assert agent_main.main(["--claim", CLAIM_TOKEN, "--agent", "experimental_agent_a:build"]) == 0

    contestant = harness.runs[0][2]
    assert type(contestant).__name__ == "Experimental"
    assert type(contestant).__module__ == "experimental_agent_a"


def test_agent_override_defaults_factory_to_create_agent(monkeypatch, tmp_path):
    harness = ClaimHarness(monkeypatch)
    _write_agent_module(
        tmp_path,
        monkeypatch,
        "experimental_agent_b",
        """
        def choose_action(state, context):
            return state.legal_actions[0]

        def create_agent():
            return choose_action
        """,
    )

    assert agent_main.main(["--claim", CLAIM_TOKEN, "--agent", "experimental_agent_b"]) == 0
    assert harness.runs[0][2].__module__ == "experimental_agent_b"


def test_agent_override_accepts_bundled_example(monkeypatch):
    harness = ClaimHarness(monkeypatch)

    assert agent_main.main(["--claim", CLAIM_TOKEN, "--agent", "examples.messaging_agent"]) == 0
    assert len(harness.runs) == 1


@pytest.mark.parametrize(
    "spec, phrase",
    [
        ("no_such_agent_module_xyz", "Could not find agent module"),
        ("examples/basic_agent.py", "MODULE[:FACTORY]"),
        ("bad name:x", "MODULE[:FACTORY]"),
        ("examples.basic_agent:not_there", "missing or not callable"),
    ],
)
def test_agent_override_errors_are_clear_and_nothing_is_claimed(monkeypatch, capsys, spec, phrase):
    harness = ClaimHarness(monkeypatch)

    assert agent_main.main(["--claim", CLAIM_TOKEN, "--agent", spec]) == 1

    assert phrase in capsys.readouterr().out
    assert harness.claims == []


def test_agent_override_import_failure_inside_module_is_reported(monkeypatch, tmp_path, capsys):
    harness = ClaimHarness(monkeypatch)
    _write_agent_module(tmp_path, monkeypatch, "broken_agent_mod", "import definitely_missing_dependency_xyz\n")

    assert agent_main.main(["--claim", CLAIM_TOKEN, "--agent", "broken_agent_mod"]) == 1

    assert "Importing agent module 'broken_agent_mod' failed" in capsys.readouterr().out
    assert harness.claims == []


def test_agent_without_claim_is_rejected(monkeypatch):
    monkeypatch.delenv("ALTRUAGENT_CLAIM_TOKEN", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        agent_main.main(["--agent", "examples.basic_agent"])
    assert exc_info.value.code == 2


def test_failing_factory_is_reported_before_the_seat_is_claimed(monkeypatch, tmp_path, capsys):
    harness = ClaimHarness(monkeypatch)
    _write_agent_module(
        tmp_path,
        monkeypatch,
        "raising_factory_agent",
        """
        def create_agent():
            raise RuntimeError("missing LLM key")
        """,
    )

    assert agent_main.main(["--claim", CLAIM_TOKEN, "--agent", "raising_factory_agent"]) == 1

    assert "missing LLM key" in capsys.readouterr().out
    assert harness.claims == []


def test_factory_returning_no_decision_logic_is_reported_before_claim(monkeypatch, tmp_path):
    harness = ClaimHarness(monkeypatch)
    _write_agent_module(tmp_path, monkeypatch, "none_factory_agent", "def create_agent():\n    return None\n")

    assert agent_main.main(["--claim", CLAIM_TOKEN, "--agent", "none_factory_agent"]) == 1
    assert harness.claims == []


# -- independence ----------------------------------------------------------------------


def test_two_claim_invocations_are_fully_independent(monkeypatch, tmp_path):
    grants = iter([_grant("jwt-A", seat_position=0), _grant("jwt-B", seat_position=1)])
    harness = ClaimHarness(monkeypatch, response=lambda body: httpx.Response(200, json=next(grants)))
    _write_agent_module(
        tmp_path,
        monkeypatch,
        "counting_agent",
        """
        created = []

        class Counting:
            def choose_action(self, state, context):
                return state.legal_actions[0]

        def create_agent():
            instance = Counting()
            created.append(instance)
            return instance
        """,
    )

    assert agent_main.main(["--claim", "seatclaim_token_A", "--agent", "counting_agent"]) == 0
    assert agent_main.main(["--claim", "seatclaim_token_B", "--agent", "counting_agent"]) == 0

    claim_a, claim_b = harness.claims
    assert claim_a["claim_token"] == "seatclaim_token_A"
    assert claim_b["claim_token"] == "seatclaim_token_B"
    assert claim_a["claim_key"] != claim_b["claim_key"]

    client_a, client_b = harness.clients
    assert client_a is not client_b
    assert client_a.auth is not client_b.auth
    assert client_a._access_token == "jwt-A"
    assert client_b._access_token == "jwt-B"

    (game_a, context_a, contestant_a), (game_b, context_b, contestant_b) = harness.runs
    assert game_a is not game_b
    assert contestant_a is not contestant_b
    assert (context_a.seat_position, context_b.seat_position) == (0, 1)
    assert len(sys.modules["counting_agent"].created) == 2  # create_agent() once per invocation


# -- lifecycle output ----------------------------------------------------------------------


def _draft_state(version, *, my_turn, phase="draft", action_id="draft_pick:a"):
    state = {
        "session_id": "game-session-match-1",
        "status": "in_progress",
        "phase": phase,
        "is_current_actor": my_turn,
        "state_version": version,
        "observation": "FULL OBSERVATION TEXT",
    }
    if my_turn:
        state["legal_actions"] = {
            "session_id": "game-session-match-1",
            "state_version": version,
            "actions": [{"action_id": action_id, "label": action_id, "input": {}}],
        }
    return state


def test_claim_mode_prints_lifecycle_through_real_runner(monkeypatch, capsys):
    """Real run_game against a scripted MCP server: draft (with a wait for the
    opponent in between) -> battle -> terminal result.
    """
    import altruagent.mcp_game as mcp_game_module
    from altruagent.runner import run_game as real_run_game

    ClaimHarness(monkeypatch)
    monkeypatch.setattr(agent_main, "run_game", real_run_game)

    script = [
        ("get_game_state", _draft_state(1, my_turn=True)),
        ("play_action", {"accepted": True, "state_version": 2, "state": _draft_state(2, my_turn=False)}),
        ("wait_for_update", {**_draft_state(3, my_turn=True, action_id="draft_pick:b"), "updated": True}),
        ("play_action", {"accepted": True, "state_version": 4,
                         "state": _draft_state(4, my_turn=True, phase="moving", action_id="move:0")}),
        ("play_action", {"accepted": True, "state_version": 5, "status": "completed"}),
        ("get_game_state", {**_draft_state(5, my_turn=False, phase="moving"), "is_terminal": True}),
        ("get_result", {"is_terminal": True, "status": "completed", "termination_reason": "normal",
                        "returns": {"synthetic-agent-1": 1.0, "synthetic-agent-0": 0.0}}),
    ]
    calls = []

    def fake_call_tool(client, url, tool, arguments):
        expected_tool, response = script[len(calls)]
        calls.append(tool)
        assert tool == expected_tool
        return response

    monkeypatch.setattr(mcp_game_module, "call_tool", fake_call_tool)

    assert agent_main.main(["--claim", CLAIM_TOKEN]) == 0

    assert calls == [tool for tool, _ in script]  # no extra/changed gameplay calls
    assert capsys.readouterr().out.splitlines() == [
        "Claimed seat 2/2",
        "Game: pokemon_vgc_doubles_draft",
        "Match: match-1 (waiting for every seat's agent to connect)",
        "Connecting to GameAPI...",
        "Connected. Playing — press Ctrl+C to stop.",
        "Phase: draft",
        "Phase: moving",
        "Match finished (termination_reason=normal) after 3 decision(s).",
        "Your score: 1.0",
    ]


def test_claim_mode_omits_score_when_result_has_no_entry_for_this_seat(monkeypatch, capsys):
    ClaimHarness(monkeypatch)  # fake run_game returns returns keyed "0"/"1"

    agent_main.main(["--claim", CLAIM_TOKEN])

    out = capsys.readouterr().out
    assert "Match finished (termination_reason=normal) after 0 decision(s)." in out
    assert "Your score" not in out


# -- the normal no-argument workflow is unchanged ---------------------------------------


def test_no_arguments_still_runs_discovery_supervisor(monkeypatch):
    monkeypatch.delenv("ALTRUAGENT_CLAIM_TOKEN", raising=False)
    calls = []
    constructed = []

    class FakeClient:
        def me(self):
            return types.SimpleNamespace(is_claimed=True, name="bot", id="agent-123", status="claimed")

        def close(self):
            calls.append("close")

    def fake_client_factory(*args, **kwargs):
        constructed.append((args, kwargs))
        return FakeClient()

    def fake_supervisor(client, *, agent_id):
        calls.append(("supervisor", agent_id))

    monkeypatch.setattr(agent_main, "AltruAgentClient", fake_client_factory)
    monkeypatch.setattr(agent_main, "run_forever_concurrent", fake_supervisor)
    monkeypatch.setattr(agent_main, "run_game", _fail("run_game must not be called"))

    assert agent_main.main() == 0
    assert constructed == [((), {})]  # default API-key client, no auth= override
    assert calls == [("supervisor", "agent-123"), "close"]


# -- waiting for the Testing lobby to fill (409 match_not_ready) ---------------------


def _not_ready(retry_after=20):
    body = {"error": "match_not_ready", "detail": "waiting for open seats"}
    if retry_after is not None:
        body["retry_after_seconds"] = retry_after
    return httpx.Response(409, json=body, headers={"Retry-After": str(retry_after or 20)})


def _sequence(*responses):
    queue = list(responses)
    return lambda body: queue.pop(0) if len(queue) > 1 else queue[0]


@pytest.fixture
def sleeps(monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr(agent_main.time, "sleep", lambda s: calls.append(s))
    monkeypatch.setattr(agent_main.random, "uniform", lambda a, b: 0.0)
    return calls


def test_waits_while_the_lobby_fills_then_claims_with_the_same_key(monkeypatch, capsys, sleeps):
    harness = ClaimHarness(
        monkeypatch,
        response=_sequence(_not_ready(), _not_ready(), httpx.Response(200, json=_grant())),
    )

    assert agent_main.main(["--claim", CLAIM_TOKEN]) == 0

    assert len(harness.claims) == 3
    assert len({c["claim_key"] for c in harness.claims}) == 1  # one SeatGrantAuth, one key
    assert all(c["claim_token"] == CLAIM_TOKEN for c in harness.claims)
    assert sleeps == [20.0, 20.0]
    assert len(harness.runs) == 1
    out = capsys.readouterr().out
    assert out.count("Waiting for the match's open seats to be filled") == 1


def test_honours_the_server_retry_hint(monkeypatch, sleeps):
    ClaimHarness(monkeypatch, response=_sequence(_not_ready(retry_after=7), httpx.Response(200, json=_grant())))
    assert agent_main.main(["--claim", CLAIM_TOKEN]) == 0
    assert sleeps == [7.0]


def test_falls_back_to_twenty_seconds_without_a_hint(monkeypatch, sleeps):
    ClaimHarness(monkeypatch, response=_sequence(_not_ready(retry_after=None), httpx.Response(200, json=_grant())))
    assert agent_main.main(["--claim", CLAIM_TOKEN]) == 0
    assert sleeps == [agent_main.CLAIM_WAIT_DEFAULT_S]


def test_rate_limited_while_waiting_retries(monkeypatch, sleeps):
    harness = ClaimHarness(
        monkeypatch,
        response=_sequence(
            _not_ready(),
            httpx.Response(429, json={"error": "rate_limited", "detail": "slow down"}),
            httpx.Response(200, json=_grant()),
        ),
    )
    assert agent_main.main(["--claim", CLAIM_TOKEN]) == 0
    assert len(harness.claims) == 3
    assert sleeps == [20.0, agent_main.CLAIM_RATE_LIMITED_WAIT_S]


@pytest.mark.parametrize(
    ("status", "code"),
    [(404, "invalid_claim_token"), (410, "claim_expired"), (409, "seat_already_claimed"), (409, "match_not_claimable")],
)
def test_other_claim_errors_still_exit(monkeypatch, capsys, sleeps, status, code):
    harness = ClaimHarness(monkeypatch, response=lambda body: httpx.Response(status, json={"error": code}))
    assert agent_main.main(["--claim", CLAIM_TOKEN]) == 1
    assert len(harness.claims) == 1
    assert sleeps == []
    assert "Could not claim seat" in capsys.readouterr().out


def test_ctrl_c_while_waiting_stops_cleanly_without_claiming(monkeypatch, capsys):
    ClaimHarness(monkeypatch, response=lambda body: _not_ready())

    def interrupted(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(agent_main.time, "sleep", interrupted)
    assert agent_main.main(["--claim", CLAIM_TOKEN]) == 0
    out = capsys.readouterr().out
    assert "Stopped before the seat was claimed." in out
    assert "stays claimed" not in out


def test_waiting_never_prints_secrets(monkeypatch, capsys, sleeps):
    harness = ClaimHarness(
        monkeypatch,
        response=_sequence(_not_ready(), httpx.Response(200, json=_grant(access_token="seat-jwt-SECRET"))),
    )
    agent_main.main(["--claim", CLAIM_TOKEN])
    out = capsys.readouterr().out
    assert CLAIM_TOKEN not in out
    assert "seat-jwt-SECRET" not in out
    assert harness.claims[0]["claim_key"] not in out


def test_login_itself_is_still_a_single_attempt():
    auth = SeatGrantAuth(CLAIM_TOKEN)
    calls = []

    def handler(request):
        calls.append(request)
        return _not_ready()

    http = httpx.Client(base_url=CONTROL_URL, transport=httpx.MockTransport(handler))
    from altruagent.auth import SeatClaimError

    with pytest.raises(SeatClaimError) as info:
        auth.login(http, CONTROL_URL)
    assert len(calls) == 1
    assert info.value.error_code == "match_not_ready"
    assert info.value.retry_after_seconds == 20.0
