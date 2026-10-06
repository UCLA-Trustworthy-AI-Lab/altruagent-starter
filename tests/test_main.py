"""CLI tests for ``python -m agent``'s single flow and its retired modes.

``--tournament``/``--check-tournament`` are covered in
tests/test_main_tournament.py. Here: the retired modes (no mode, the old
platform API-key mode; ``--claim``, Testing claim codes) only print a notice
pointing to the Official Agent Key and never touch the network, and
``--help`` describes the one flow.
"""

from __future__ import annotations

import pytest

import agent.__main__ as agent_main
from altruagent.notices import AGENT_GUIDE_URL, CLAIM_CODES_RETIRED, PLATFORM_KEY_RETIRED

CLAIM_TOKEN = "seatclaim_cli_secret_token"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """A retired mode must not start the runtime or build any client."""
    for name in ("ALTRUAGENT_CLAIM_TOKEN", "ALTRUAGENT_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    def fail(*args, **kwargs):
        pytest.fail("a retired mode started the runtime")

    monkeypatch.setattr(agent_main, "OfficialAgentClient", fail)
    monkeypatch.setattr(agent_main, "run_tournament_forever", fail)


def _assert_points_to_the_official_key(out: str) -> None:
    assert "python -m agent --tournament" in out
    assert "ALTRUAGENT_OFFICIAL_AGENT_KEY" in out
    assert AGENT_GUIDE_URL in out


def test_no_mode_prints_the_platform_key_retirement_notice(capsys):
    assert agent_main.main([]) == 1

    out = capsys.readouterr().out
    assert out.startswith(PLATFORM_KEY_RETIRED)
    _assert_points_to_the_official_key(out)


def test_no_mode_with_a_platform_api_key_set_still_only_prints_the_notice(monkeypatch, capsys):
    monkeypatch.setenv("ALTRUAGENT_API_KEY", "sk_agent_old_platform_key")

    assert agent_main.main([]) == 1

    out = capsys.readouterr().out
    assert out.startswith(PLATFORM_KEY_RETIRED)
    assert "sk_agent_old_platform_key" not in out


def test_main_without_argv_parses_no_arguments(capsys):
    assert agent_main.main() == 1
    assert capsys.readouterr().out.startswith(PLATFORM_KEY_RETIRED)


@pytest.mark.parametrize("argv", [["--claim", CLAIM_TOKEN], ["--claim", "-"], ["--claim"], [f"--claim={CLAIM_TOKEN}"]])
def test_claim_prints_the_claim_codes_retirement_notice(argv, capsys):
    assert agent_main.main(argv) == 1

    out = capsys.readouterr().out
    assert out.splitlines()[0] == f"{CLAIM_CODES_RETIRED}."
    assert out.startswith("Testing claim codes were retired; run with --tournament and your Official Agent Key")
    _assert_points_to_the_official_key(out)
    assert CLAIM_TOKEN not in out


def test_claim_with_agent_override_still_only_prints_the_notice(capsys):
    assert agent_main.main(["--claim", CLAIM_TOKEN, "--agent", "examples.llm_agent"]) == 1
    assert capsys.readouterr().out.startswith(CLAIM_CODES_RETIRED)


def test_claim_token_in_the_environment_prints_the_claim_notice(monkeypatch, capsys):
    monkeypatch.setenv("ALTRUAGENT_CLAIM_TOKEN", CLAIM_TOKEN)

    assert agent_main.main([]) == 1

    out = capsys.readouterr().out
    assert out.startswith(CLAIM_CODES_RETIRED) and CLAIM_TOKEN not in out


def test_agent_without_a_mode_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exc_info:
        agent_main.main(["--agent", "examples.llm_agent"])

    assert exc_info.value.code == 2
    assert "--tournament" in capsys.readouterr().err


def test_help_describes_the_one_flow(capsys):
    with pytest.raises(SystemExit) as exc_info:
        agent_main.main(["--help"])

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "--tournament" in out and "--check-tournament" in out and "--agent" in out
    assert "ALTRUAGENT_OFFICIAL_AGENT_KEY" in out
    assert "Testing and tournament games" in out
    assert AGENT_GUIDE_URL in out
    # Retired modes are gone from the help text.
    assert "--claim" not in out
    assert "ALTRUAGENT_API_KEY" not in out
    assert "seatclaim" not in out


def test_platform_notice_mentions_the_retired_variable():
    assert "ALTRUAGENT_API_KEY" in PLATFORM_KEY_RETIRED
