"""CLI tests for the platform-tournament modes: ``python -m agent --join
<competition_id>`` and ``--tournament-auto [--tournament-id T]``. The client
and the altruagent.autojoin entry points are replaced by fakes; no network,
no processes. (altruagent.autojoin itself is covered by tests/test_autojoin.py.)
"""

from __future__ import annotations

import sys
import textwrap

import pytest

import agent.__main__ as agent_main
from altruagent.autojoin import GameNeverStarted, JoinRefused
from altruagent.errors import AuthenticationError, ConfigurationError, PlatformError
from altruagent.models import Agent, TournamentDetail
from altruagent.runner import DecisionError

AGENT = Agent(id="agent-a", name="Alpha", status="claimed")


class FakeClient:
    def __init__(self, *, me=AGENT, me_error=None, tournament=None):
        self._me, self._me_error, self._tournament = me, me_error, tournament
        self.closed = False
        self.tournament_ids: list[str] = []

    def me(self):
        if self._me_error:
            raise self._me_error
        return self._me

    def tournament(self, tournament_id):
        self.tournament_ids.append(tournament_id)
        if isinstance(self._tournament, Exception):
            raise self._tournament
        return self._tournament

    def close(self):
        self.closed = True


def _detail(status, **extra):
    return TournamentDetail.from_dict({"tournament_id": "t-1", "name": "Autumn Cup", "status": status,
                                       "game_label": "Red Alert", **extra})


@pytest.fixture
def env(monkeypatch):
    monkeypatch.delenv("ALTRUAGENT_CLAIM_TOKEN", raising=False)
    monkeypatch.setenv("ALTRUAGENT_API_KEY", "sk_agent_test")
    monkeypatch.setenv("ALTRUAGENT_CONTROL_URL", "https://control.example.test")


def _install(monkeypatch, client=None, *, join=None, auto=None):
    client = client or FakeClient()
    calls: dict[str, list] = {"join": [], "auto": []}
    monkeypatch.setattr(agent_main, "AltruAgentClient", lambda *a, **k: client)

    def fake_join(client_arg, session_id, contestant, **kwargs):
        calls["join"].append((client_arg, session_id, contestant, kwargs))
        if join:
            return join()

    def fake_auto(client_arg, **kwargs):
        calls["auto"].append((client_arg, kwargs))
        return auto() if auto else None

    monkeypatch.setattr(agent_main, "join_and_play", fake_join)
    monkeypatch.setattr(agent_main, "run_autojoin_forever", fake_auto)
    monkeypatch.setattr(agent_main, "run_forever_concurrent", lambda *a, **k: pytest.fail("discovery mode started"))
    monkeypatch.setattr(agent_main, "_run_claim", lambda *a, **k: pytest.fail("claim mode started"))
    return client, calls


def _raise(exc):
    def raiser():
        raise exc

    return raiser


def _write_agent_module(tmp_path, monkeypatch, name: str, body: str) -> None:
    (tmp_path / f"{name}.py").write_text(textwrap.dedent(body), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, name, raising=False)


# -- --join -------------------------------------------------------------------------------


def test_join_builds_the_agent_then_joins_and_plays(env, monkeypatch, capsys):
    client, calls = _install(monkeypatch)

    assert agent_main.main(["--join", "game-7"]) == 0

    (client_arg, session_id, contestant, kwargs), = calls["join"]
    assert client_arg is client and session_id == "game-7" and callable(contestant)
    assert kwargs["agent_id"] == "agent-a"
    assert kwargs["game_factory"] is agent_main._ProgressGameSession
    assert client.closed
    out = capsys.readouterr().out
    assert "Authenticated as agent 'Alpha' (agent-a)." in out
    assert "Joining competition game-7 with agent agent.agent:create_agent..." in out


def test_join_uses_the_agent_override(env, monkeypatch, capsys, tmp_path):
    _write_agent_module(tmp_path, monkeypatch, "join_mode_agent", """
        class Agent:
            def choose_action(self, state, context):
                return state.legal_actions[0]

        def build():
            return Agent()
    """)
    _, calls = _install(monkeypatch)

    assert agent_main.main(["--join", "game-7", "--agent", "join_mode_agent:build"]) == 0

    contestant = calls["join"][0][2]
    assert type(contestant).__name__ == "Agent" and type(contestant).__module__ == "join_mode_agent"


def test_join_reports_a_broken_agent_before_joining_anything(env, monkeypatch, capsys, tmp_path):
    _write_agent_module(tmp_path, monkeypatch, "join_broken_agent", """
        def create_agent():
            raise RuntimeError("OPENAI_API_KEY is not set")
    """)
    _, calls = _install(monkeypatch)
    monkeypatch.setattr(agent_main, "AltruAgentClient", lambda *a, **k: pytest.fail("connected before building the agent"))

    assert agent_main.main(["--join", "game-7", "--agent", "join_broken_agent"]) == 1

    assert calls["join"] == []
    assert "could not be created, so nothing was joined: OPENAI_API_KEY is not set" in capsys.readouterr().out


def test_join_unknown_agent_module(env, monkeypatch, capsys):
    _, calls = _install(monkeypatch)

    assert agent_main.main(["--join", "game-7", "--agent", "no_such_module_xyz"]) == 1
    assert calls["join"] == [] and "Could not find agent module" in capsys.readouterr().out


def test_join_with_an_empty_id(env, monkeypatch, capsys):
    _, calls = _install(monkeypatch)

    assert agent_main.main(["--join", "  "]) == 1
    assert calls["join"] == [] and "no competition id was given" in capsys.readouterr().out


def test_join_configuration_error(env, monkeypatch, capsys):
    def raise_config(*a, **k):
        raise ConfigurationError("ALTRUAGENT_API_KEY is not set.")

    _install(monkeypatch)
    monkeypatch.setattr(agent_main, "AltruAgentClient", raise_config)

    assert agent_main.main(["--join", "game-7"]) == 1
    assert "ALTRUAGENT_API_KEY is not set." in capsys.readouterr().out


def test_join_refuses_an_unclaimed_agent(env, monkeypatch, capsys):
    client, calls = _install(monkeypatch, FakeClient(me=Agent(id="x", name="X", status="unclaimed")))

    assert agent_main.main(["--join", "game-7"]) == 1
    assert calls["join"] == [] and client.closed
    assert "is not claimed yet" in capsys.readouterr().out


def test_join_authentication_failure(env, monkeypatch, capsys):
    client, calls = _install(monkeypatch, FakeClient(me_error=AuthenticationError("Invalid API key", status_code=401)))

    assert agent_main.main(["--join", "game-7"]) == 1
    assert calls["join"] == [] and "Could not authenticate: Invalid API key" in capsys.readouterr().out


@pytest.mark.parametrize(
    "error, code, message",
    [
        (JoinRefused("the join window for this game has closed", error_code="join_deadline_passed"), 1,
         "Could not join competition game-7: the join window for this game has closed"),
        (JoinRefused("this tournament game is reserved", error_code="not_in_this_match"), 1,
         "Could not join competition game-7: this tournament game is reserved"),
        (GameNeverStarted("still waiting"), 1, "Stopped: still waiting"),
        (DecisionError("bad move"), 1, "Match failed: bad move"),
        (PlatformError("down", status_code=500), 1, "unrecoverable error: down"),
        (KeyboardInterrupt(), 0, "run the same command again to carry on playing it"),
    ],
)
def test_join_failures(env, monkeypatch, capsys, error, code, message):
    client, _ = _install(monkeypatch, join=_raise(error))

    assert agent_main.main(["--join", "game-7"]) == code
    assert message in capsys.readouterr().out and client.closed


def test_join_ignores_a_stray_claim_token_env(env, monkeypatch):
    monkeypatch.setenv("ALTRUAGENT_CLAIM_TOKEN", "seatclaim_should_be_ignored")
    _, calls = _install(monkeypatch)

    assert agent_main.main(["--join", "game-7"]) == 0 and len(calls["join"]) == 1


# -- --tournament-auto ------------------------------------------------------------------------


def test_auto_without_a_tournament_id_runs_the_loop_for_every_tournament(env, monkeypatch, capsys):
    client, calls = _install(monkeypatch)

    assert agent_main.main(["--tournament-auto", "--agent", "examples.smoke_agent"]) == 0

    (client_arg, kwargs), = calls["auto"]
    assert client_arg is client
    assert kwargs == {"agent_id": "agent-a", "agent_spec": "examples.smoke_agent", "tournament_id": None}
    assert client.tournament_ids == []
    assert "in every tournament, with agent examples.smoke_agent" in capsys.readouterr().out


def test_auto_with_a_tournament_id_checks_it_then_runs_until_it_is_over(env, monkeypatch, capsys):
    finished = _detail("completed", champion={"agent_id": "agent-b", "agent_name": "Bravo"},
                       final_ranking=[{"rank": 1, "agent_id": "agent-b", "points": 3},
                                      {"rank": 2, "agent_id": "agent-a", "points": 2}])
    client, calls = _install(monkeypatch, FakeClient(tournament=_detail("in_progress", current_round={"label": "Semifinals"})),
                             auto=lambda: finished)

    assert agent_main.main(["--tournament-auto", "--tournament-id", "t-1"]) == 0

    assert client.tournament_ids == ["t-1"]
    assert calls["auto"][0][1]["tournament_id"] == "t-1"
    out = capsys.readouterr().out.splitlines()
    assert 'Tournament "Autumn Cup" — Red Alert: in_progress (Semifinals).' in out
    assert out[-3:] == ['Tournament "Autumn Cup" is complete.', "Champion: Bravo.",
                        "Your agent finished #2 of 2 (2 Swiss point(s))."]


def test_auto_waits_for_a_tournament_still_in_registration(env, monkeypatch, capsys):
    _, calls = _install(monkeypatch, FakeClient(tournament=_detail("registration")))

    assert agent_main.main(["--tournament-auto", "--tournament-id", "t-1"]) == 0
    assert len(calls["auto"]) == 1
    assert "It hasn't started yet" in capsys.readouterr().out


def test_auto_with_an_unknown_tournament_id(env, monkeypatch, capsys):
    _, calls = _install(monkeypatch, FakeClient(tournament=PlatformError("Not found", status_code=404)))

    assert agent_main.main(["--tournament-auto", "--tournament-id", "nope"]) == 1
    assert calls["auto"] == [] and "No tournament with id nope." in capsys.readouterr().out


def test_auto_with_a_finished_tournament_exits_straight_away(env, monkeypatch, capsys):
    _, calls = _install(monkeypatch, FakeClient(tournament=_detail("cancelled")))

    assert agent_main.main(["--tournament-auto", "--tournament-id", "t-1"]) == 0
    assert calls["auto"] == []
    assert 'Tournament "Autumn Cup" was cancelled.' in capsys.readouterr().out


def test_auto_carries_on_when_the_first_tournament_read_fails_transiently(env, monkeypatch, capsys):
    _, calls = _install(monkeypatch, FakeClient(tournament=PlatformError("bad gateway", status_code=502)))

    assert agent_main.main(["--tournament-auto", "--tournament-id", "t-1"]) == 0
    assert calls["auto"][0][1]["tournament_id"] == "t-1"
    assert "Could not read tournament t-1 yet (bad gateway); carrying on." in capsys.readouterr().out


def test_auto_ctrl_c(env, monkeypatch, capsys):
    client, _ = _install(monkeypatch, auto=_raise(KeyboardInterrupt()))

    assert agent_main.main(["--tournament-auto"]) == 0
    assert "Stopped." in capsys.readouterr().out and client.closed


def test_auto_fatal_error(env, monkeypatch, capsys):
    _install(monkeypatch, auto=_raise(AuthenticationError("Invalid API key", status_code=401)))

    assert agent_main.main(["--tournament-auto"]) == 1
    assert "unrecoverable error: Invalid API key" in capsys.readouterr().out


def test_auto_reports_a_broken_agent_before_connecting(env, monkeypatch, capsys):
    _, calls = _install(monkeypatch)
    monkeypatch.setattr(agent_main, "AltruAgentClient", lambda *a, **k: pytest.fail("connected"))

    assert agent_main.main(["--tournament-auto", "--agent", "examples.basic_agent:missing"]) == 1
    assert calls["auto"] == []


# -- argument rules ---------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [
    ["--join", "g", "--tournament-auto"],
    ["--join", "g", "--claim", "seatclaim_x"],
    ["--join", "g", "--tournament"],
    ["--tournament-auto", "--tournament"],
    ["--tournament-auto", "--check-tournament"],
])
def test_new_modes_are_mutually_exclusive_with_the_others(env, argv):
    with pytest.raises(SystemExit) as exc_info:
        agent_main.main(argv)
    assert exc_info.value.code == 2


@pytest.mark.parametrize("argv", [["--tournament-id", "t-1"], ["--join", "g", "--tournament-id", "t-1"],
                                  ["--tournament", "--tournament-id", "t-1"]])
def test_tournament_id_needs_tournament_auto(env, argv, capsys):
    with pytest.raises(SystemExit) as exc_info:
        agent_main.main(argv)
    assert exc_info.value.code == 2
    assert "--tournament-id is only supported together with --tournament-auto" in capsys.readouterr().err


def test_help_explains_both_tournament_systems(capsys):
    with pytest.raises(SystemExit):
        agent_main.main(["--help"])
    out = capsys.readouterr().out

    assert "--join ID" in out and "--tournament-auto" in out
    assert "4 minutes" in out
    assert "The UCLA event uses a different system, with no joining at all" in out
