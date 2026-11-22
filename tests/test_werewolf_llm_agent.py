"""Tests for examples/werewolf_llm_agent.py, using a fake model provider (no network, no key)."""

import json
from types import SimpleNamespace

import examples.werewolf_llm_agent as mod


class FakeProvider:
    def __init__(self):
        self.calls = []

    def complete_structured(self, messages, name, schema, **kwargs):
        self.calls.append(messages)
        return {"action_id": "3", "reasoning_summary": "Voting for Player3, the most suspected."}


class NoTimeoutProvider:
    """A provider written before `timeout` existed."""

    def __init__(self):
        self.calls = 0

    def complete_structured(self, messages, name, schema):
        self.calls += 1
        return {}


def request(game_type):
    return [
        {"role": "system", "content": "BASE INSTRUCTIONS"},
        {"role": "user", "content": json.dumps({"game_type": game_type, "decision": "choose_action"})},
    ]


def vote_state():
    legal = [SimpleNamespace(action_id=str(s), label=f"Vote to eliminate Player{s}", input={}, raw={}) for s in (1, 2, 3)]
    legal.append(SimpleNamespace(action_id="7", label="Abstain", input={}, raw={}))
    return SimpleNamespace(
        phase="moving", legal_actions=legal, new_messages=[], state_version=1,
        raw={"observation": "=== Werewolf ===\nYour role: VILLAGER", "your_position": 0},
    )


def test_strategy_is_added_on_a_werewolf_decision_without_changing_the_callers_list():
    inner = FakeProvider()
    messages = request("werewolf")
    mod.StrategyProvider(inner, "MY STRATEGY").complete_structured(messages, "choose_action", {})
    sent = inner.calls[0][0]["content"]
    assert "BASE INSTRUCTIONS" in sent and "MY STRATEGY" in sent
    assert messages[0]["content"] == "BASE INSTRUCTIONS"  # the caller's copy is untouched


def test_other_games_pass_through_unchanged():
    inner = FakeProvider()
    mod.StrategyProvider(inner, "MY STRATEGY").complete_structured(request("pokemon_vgc_doubles_draft"), "choose_action", {})
    assert inner.calls[0][0]["content"] == "BASE INSTRUCTIONS"


def test_a_timeout_is_dropped_for_a_provider_that_does_not_take_one():
    inner = NoTimeoutProvider()
    mod.StrategyProvider(inner, "S").complete_structured(request("werewolf"), "choose_action", {}, timeout=5.0)
    assert inner.calls == 1


def test_the_agent_sends_the_strategy_file_to_the_model(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr(mod, "provider_from_env", lambda: provider)
    agent = mod.create_agent()
    move = agent.choose_action(vote_state(), SimpleNamespace(game_type="werewolf", seat_position=0))
    system = provider.calls[0][0]["content"]
    assert "Never abstain by day" in system  # a line from werewolf_strategy.md
    chosen = move.action if hasattr(move, "action") else move
    assert chosen.action_id == "3"
