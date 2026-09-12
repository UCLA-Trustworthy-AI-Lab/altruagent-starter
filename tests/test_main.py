"""Unit tests for agent/__main__.py's small validation helpers and startup
error handling. Does not invoke run_forever_concurrent, spawn any worker
process, or touch the network.
"""

from __future__ import annotations

import types

import pytest

import agent.__main__ as agent_main
from altruagent.errors import ConfigurationError


def test_resolve_create_agent_missing_fails_clearly():
    fake_module = types.SimpleNamespace()

    with pytest.raises(ValueError):
        agent_main._resolve_create_agent(fake_module)


def test_resolve_create_agent_non_callable_fails_clearly():
    fake_module = types.SimpleNamespace(create_agent="not callable")

    with pytest.raises(ValueError):
        agent_main._resolve_create_agent(fake_module)


def test_resolve_create_agent_accepts_a_real_function_without_calling_it():
    calls = []

    def create_agent():
        calls.append(1)
        return lambda state, context: 0

    fake_module = types.SimpleNamespace(create_agent=create_agent)

    resolved = agent_main._resolve_create_agent(fake_module)

    assert resolved is create_agent
    assert calls == []  # validated, not invoked — construction is per-match


def test_main_reports_configuration_error_clearly(monkeypatch):
    def raise_config_error(*args, **kwargs):
        raise ConfigurationError("ALTRUAGENT_API_KEY is not set.")

    monkeypatch.setattr(agent_main, "AltruAgentClient", raise_config_error)

    assert agent_main.main() == 1
