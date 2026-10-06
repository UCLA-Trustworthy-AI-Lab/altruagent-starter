"""CLI tests for ``python -m agent --tournament`` / ``--check-tournament``.
The official client and the supervisor loop are replaced by fakes; no
network, no processes. The retired modes (no mode, ``--claim``) are covered
by tests/test_main.py.
"""

from __future__ import annotations

import pytest

import agent.__main__ as agent_main
from altruagent.errors import AuthenticationError, ConfigurationError, PlatformError
from altruagent.official import OfficialAgentError

KEY = "eak_live_" + "cd" * 32


class FakeOfficial:
    def __init__(self, *, auth_error=None, assignments_result=()):
        self.control_url = "https://control.example.test"
        self.auth_error = auth_error
        self.assignments_result = assignments_result
        self.authenticated = False
        self.closed = False

    def authenticate(self):
        if self.auth_error:
            raise self.auth_error
        self.authenticated = True

    def assignments(self):
        if isinstance(self.assignments_result, Exception):
            raise self.assignments_result
        return list(self.assignments_result)

    def close(self):
        self.closed = True


@pytest.fixture
def env(monkeypatch):
    for name in ("ALTRUAGENT_CLAIM_TOKEN", "ALTRUAGENT_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALTRUAGENT_OFFICIAL_AGENT_KEY", KEY)
    monkeypatch.setenv("ALTRUAGENT_CONTROL_URL", "https://control.example.test")


def _install(monkeypatch, official=None, *, supervisor=None):
    official = official or FakeOfficial()
    calls = []
    monkeypatch.setattr(agent_main, "OfficialAgentClient", lambda *a, **k: official)

    def fake_supervisor(client, *, agent_spec):
        calls.append((client, agent_spec))
        if supervisor:
            supervisor()

    monkeypatch.setattr(agent_main, "run_tournament_forever", fake_supervisor)
    return official, calls


# -- --tournament -----------------------------------------------------------------------


def test_tournament_mode_authenticates_and_runs_the_tournament_supervisor(env, monkeypatch, capsys):
    official, calls = _install(monkeypatch)

    assert agent_main.main(["--tournament"]) == 0

    assert official.authenticated and official.closed
    assert calls == [(official, "agent.agent:create_agent")]
    out = capsys.readouterr().out.splitlines()
    assert out[:2] == ["Connected with your Official Agent Key.",
                       "Waiting for your next game... (Press Ctrl+C to stop.)"]


def test_tournament_mode_passes_agent_override(env, monkeypatch):
    _, calls = _install(monkeypatch)

    assert agent_main.main(["--tournament", "--agent", "examples.llm_agent"]) == 0
    assert calls[0][1] == "examples.llm_agent"


def test_tournament_mode_rejects_bad_agent_spec_before_connecting(env, monkeypatch, capsys):
    official, calls = _install(monkeypatch)

    assert agent_main.main(["--tournament", "--agent", "no_such_module_xyz"]) == 1
    assert "Could not find agent module" in capsys.readouterr().out
    assert not official.authenticated and calls == []


def test_tournament_mode_configuration_error(env, monkeypatch, capsys):
    def raise_config(*a, **k):
        raise ConfigurationError("ALTRUAGENT_OFFICIAL_AGENT_KEY is not set.")

    monkeypatch.setattr(agent_main, "OfficialAgentClient", raise_config)

    assert agent_main.main(["--tournament"]) == 1
    assert "ALTRUAGENT_OFFICIAL_AGENT_KEY is not set" in capsys.readouterr().out


def test_tournament_mode_authentication_failure(env, monkeypatch, capsys):
    official, calls = _install(monkeypatch, FakeOfficial(auth_error=OfficialAgentError("The Official Agent Key was not accepted.")))

    assert agent_main.main(["--tournament"]) == 1
    assert "Could not connect with your Official Agent Key" in capsys.readouterr().out
    assert calls == [] and official.closed


def test_tournament_mode_ctrl_c(env, monkeypatch, capsys):
    def interrupt():
        raise KeyboardInterrupt

    official, _ = _install(monkeypatch, supervisor=interrupt)

    assert agent_main.main(["--tournament"]) == 0
    assert "Stopped." in capsys.readouterr().out and official.closed


def test_tournament_mode_fatal_auth_error_during_run(env, monkeypatch, capsys):
    def revoked():
        raise AuthenticationError("The Official Agent Key was not accepted.")

    _install(monkeypatch, supervisor=revoked)

    assert agent_main.main(["--tournament"]) == 1
    assert "unrecoverable error" in capsys.readouterr().out


def test_tournament_mode_ignores_a_stray_claim_token_env(env, monkeypatch):
    monkeypatch.setenv("ALTRUAGENT_CLAIM_TOKEN", "seatclaim_should_be_ignored")
    _, calls = _install(monkeypatch)

    assert agent_main.main(["--tournament"]) == 0 and len(calls) == 1


def test_tournament_mode_ignores_a_stray_platform_api_key(env, monkeypatch):
    monkeypatch.setenv("ALTRUAGENT_API_KEY", "sk_agent_old_platform_key")
    _, calls = _install(monkeypatch)

    assert agent_main.main(["--tournament"]) == 0 and len(calls) == 1


@pytest.mark.parametrize("argv", [["--tournament", "--claim", "seatclaim_x"],
                                  ["--tournament", "--check-tournament"],
                                  ["--check-tournament", "--claim", "seatclaim_x"]])
def test_modes_are_mutually_exclusive(env, argv):
    with pytest.raises(SystemExit) as exc_info:
        agent_main.main(argv)
    assert exc_info.value.code == 2


# -- --check-tournament ---------------------------------------------------------------------------


def _check(monkeypatch, capsys, official, argv=("--check-tournament",)):
    _install(monkeypatch, official)
    monkeypatch.setattr(agent_main, "_mark", lambda symbol, fallback: symbol)
    code = agent_main.main(list(argv))
    return code, capsys.readouterr().out


def test_check_tournament_success(env, monkeypatch, capsys):
    code, out = _check(monkeypatch, capsys, FakeOfficial(assignments_result=[]))

    assert code == 0
    assert out.splitlines() == [
        "✓ Control plane reachable",
        "✓ Official Agent Key accepted",
        "✓ Tournament agent authenticated",
        "✓ Assignment discovery available (0 active assignment(s))",
        "✓ Agent ready (agent.agent:create_agent)",
        "✓ Ready to play Testing and tournament games",
    ]
    assert KEY not in out


def test_check_tournament_never_acquires_or_renews_a_seat_lease(env, monkeypatch, capsys):
    import httpx

    from altruagent.official import OfficialAgentClient

    paths = []

    def backend(request):
        paths.append(request.url.path)
        if request.url.path == "/tournament/agent/authenticate":
            return httpx.Response(200, json={"access_token": "sess-1", "expires_at": "x"})
        return httpx.Response(200, json={"assignments": [
            {"match_id": "m", "seat_id": "s", "game_type": "werewolf", "seat_position": 3, "seat_count": 7,
             "match_status": "in_progress", "seat_status": "running"}]})

    monkeypatch.setattr(agent_main, "OfficialAgentClient",
                        lambda: OfficialAgentClient(load_env_file=False, transport=httpx.MockTransport(backend)))
    monkeypatch.setattr(agent_main, "_mark", lambda symbol, fallback: symbol)

    assert agent_main.main(["--check-tournament"]) == 0
    assert paths == ["/tournament/agent/authenticate", "/tournament/agent/assignments"]
    assert "(1 active assignment(s))" in capsys.readouterr().out


def test_check_tournament_uses_agent_override(env, monkeypatch, capsys):
    code, out = _check(monkeypatch, capsys, FakeOfficial(), argv=("--check-tournament", "--agent", "examples.smoke_agent"))

    assert code == 0 and "✓ Agent ready (examples.smoke_agent)" in out


@pytest.mark.parametrize(
    "official, last_line",
    [
        (FakeOfficial(auth_error=PlatformError("Could not reach", status_code=None)), "✗ Control plane not reachable"),
        (FakeOfficial(auth_error=OfficialAgentError("not accepted", status_code=401)), "✗ Official Agent Key rejected"),
        (FakeOfficial(auth_error=OfficialAgentError("boom", status_code=500)), "✗ Official agent authentication failed"),
        (FakeOfficial(assignments_result=AuthenticationError("session rejected")), "✗ Tournament agent session was not accepted"),
        (FakeOfficial(assignments_result=PlatformError("down", status_code=500)), "✗ Assignment discovery failed"),
    ],
)
def test_check_tournament_failures_are_nonzero_and_specific(env, monkeypatch, capsys, official, last_line):
    code, out = _check(monkeypatch, capsys, official)

    assert code == 1
    assert out.splitlines()[-1].startswith(last_line)
    assert "Ready to play" not in out


def test_check_tournament_agent_factory_failure(env, monkeypatch, capsys):
    code, out = _check(monkeypatch, capsys, FakeOfficial(),
                       argv=("--check-tournament", "--agent", "examples.basic_agent:missing"))

    assert code == 1 and out.splitlines()[-1].startswith("✗ Agent examples.basic_agent:missing could not be created")


def test_check_tournament_configuration_failure(env, monkeypatch, capsys):
    monkeypatch.setattr(agent_main, "_mark", lambda symbol, fallback: symbol)
    monkeypatch.setenv("ALTRUAGENT_OFFICIAL_AGENT_KEY", "sk_agent_wrong_kind")

    assert agent_main.main(["--check-tournament"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("✗ ALTRUAGENT_OFFICIAL_AGENT_KEY doesn't look like an Official Agent Key")
    assert "sk_agent_wrong_kind" not in out


def test_mark_falls_back_when_console_cannot_encode(monkeypatch):
    import io
    import sys

    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))

    assert agent_main._mark("✓", "[ok]") == "[ok]"
