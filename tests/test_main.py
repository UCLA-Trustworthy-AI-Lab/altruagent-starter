"""Unit tests for agent/__main__.py's small validation helpers and startup
error handling. Does not invoke run_forever or touch the network.
"""

from __future__ import annotations

import types

import pytest

import agent.__main__ as agent_main
from altruagent.errors import ConfigurationError


def test_resolve_choose_action_missing_fails_clearly():
    fake_module = types.SimpleNamespace()

    with pytest.raises(ValueError):
        agent_main._resolve_choose_action(fake_module)


def test_resolve_choose_action_non_callable_fails_clearly():
    fake_module = types.SimpleNamespace(choose_action="not callable")

    with pytest.raises(ValueError):
        agent_main._resolve_choose_action(fake_module)


def test_resolve_choose_action_accepts_a_real_function():
    def choose_action(state, context):
        return 0

    fake_module = types.SimpleNamespace(choose_action=choose_action)

    assert agent_main._resolve_choose_action(fake_module) is choose_action


def test_main_reports_configuration_error_clearly(monkeypatch):
    def raise_config_error(*args, **kwargs):
        raise ConfigurationError("ALTRUAGENT_API_KEY is not set.")

    monkeypatch.setattr(agent_main, "AltruAgentClient", raise_config_error)

    assert agent_main.main() == 1
