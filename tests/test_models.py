"""Unit tests for altruagent.models."""

from __future__ import annotations

import pytest

from altruagent.models import Agent, AgentSessions, GameState, Match, NextAction


def test_agent_parsing_excludes_sensitive_hash_fields():
    data = {
        "id": "agent-1",
        "name": "MyAgent",
        "status": "unclaimed",
        "description": None,
        "api_key_hash": "deadbeef",
        "claim_token_hash": "cafebabe",
    }

    agent = Agent.from_dict(data)

    assert agent.id == "agent-1"
    assert agent.name == "MyAgent"
    assert agent.is_claimed is False
    assert "api_key_hash" not in agent.raw
    assert "claim_token_hash" not in agent.raw
    assert not hasattr(agent, "api_key_hash")
    assert not hasattr(agent, "claim_token_hash")


def test_agent_is_claimed_reflects_status():
    claimed = Agent.from_dict({"id": "a", "name": "A", "status": "claimed"})
    unclaimed = Agent.from_dict({"id": "b", "name": "B", "status": "unclaimed"})

    assert claimed.is_claimed is True
    assert unclaimed.is_claimed is False


def test_agent_from_dict_tolerates_unknown_extra_fields():
    agent = Agent.from_dict(
        {
            "id": "agent-1",
            "name": "MyAgent",
            "status": "claimed",
            "some_future_field": {"nested": True},
        }
    )

    assert agent.id == "agent-1"
    assert agent.raw["some_future_field"] == {"nested": True}


# -- GameState / NextAction -----------------------------------------------


def _base_game_state_payload(**overrides):
    payload = {
        "session_id": "session-1",
        "game_name": "tic_tac_toe",
        "status": "active",
        "observation": "...",
        "current_player": {"name": "Alice"},
        "legal_actions": [0, 1, 2],
        "legal_actions_str": ["top-left", "top-middle", "top-right"],
        "is_terminal": False,
        "returns": None,
        "move_count": 3,
        "termination_reason": None,
        "messaging_enabled": False,
        "phase": "moving",
        "next_actions": [
            {
                "action": "make_move",
                "endpoint": "POST /games/session-1/step",
                "hint": "It is your turn.",
                "required_fields": ["action"],
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_game_state_parses_core_fields():
    state = GameState.from_dict(_base_game_state_payload())

    assert state.session_id == "session-1"
    assert state.game_name == "tic_tac_toe"
    assert state.status == "active"
    assert state.is_terminal is False
    assert state.current_player.name == "Alice"
    assert state.move_count == 3


def test_game_state_legal_actions_parsing():
    state = GameState.from_dict(
        _base_game_state_payload(legal_actions=[0, 4, 8], legal_actions_str=["a", "b", "c"])
    )

    assert state.legal_actions == [0, 4, 8]
    assert state.legal_actions_str == ["a", "b", "c"]


def test_game_state_current_player_none_when_not_your_turn():
    state = GameState.from_dict(
        _base_game_state_payload(current_player=None, legal_actions=[])
    )

    assert state.current_player is None
    assert state.legal_actions == []


def test_game_state_next_actions_can_have_multiple_entries():
    # During a messaging round, GameAPI returns both send_message and
    # terminate_messaging together (see next_actions.py compute_next_actions).
    state = GameState.from_dict(
        _base_game_state_payload(
            messaging_enabled=True,
            phase="messaging",
            next_actions=[
                {"action": "send_message", "hint": "chat or terminate", "required_fields": ["type"]},
                {"action": "terminate_messaging", "hint": "end the round", "required_fields": ["type"]},
            ],
        )
    )

    assert [a.action for a in state.next_actions] == ["send_message", "terminate_messaging"]
    assert all(isinstance(a, NextAction) for a in state.next_actions)


def test_game_state_preserves_unknown_game_specific_extra_fields():
    state = GameState.from_dict(
        _base_game_state_payload(
            avalon_round=2,
            avalon_leader="Bob",
            round_history=[{"round": 1, "actions": {}}],
            cumulative_scores={"Alice": 2.0},
        )
    )

    assert state.raw["avalon_round"] == 2
    assert state.raw["avalon_leader"] == "Bob"
    assert state.raw["round_history"] == [{"round": 1, "actions": {}}]
    assert state.raw["cumulative_scores"] == {"Alice": 2.0}


def test_match_from_dict_basic_fields():
    match = Match.from_dict(
        {
            "session_id": "session-1",
            "game_type": "tic_tac_toe",
            "status": "waiting",
            "tournament_id": None,
            "created_at": "2026-01-01T00:00:00Z",
        }
    )

    assert match.session_id == "session-1"
    assert match.game_type == "tic_tac_toe"
    assert match.status == "waiting"
    assert match.tournament_id is None
    assert match.game_server_url is None  # never present on this endpoint's rows


def test_match_preserves_unknown_extra_fields_in_raw():
    match = Match.from_dict(
        {
            "session_id": "session-1",
            "status": "waiting",
            "winner_agent_id": None,
            "results": None,
            "runtime_adapter": "openspiel",
        }
    )

    assert match.raw["runtime_adapter"] == "openspiel"
    assert "winner_agent_id" in match.raw


def test_match_tolerates_missing_optional_fields():
    match = Match.from_dict({"session_id": "session-1", "status": "in_progress"})

    assert match.game_type is None
    assert match.tournament_id is None
    assert match.created_at is None
    assert match.started_at is None
    assert match.completed_at is None


def test_match_tournament_id_preserved_for_child_matches():
    match = Match.from_dict(
        {"session_id": "session-1", "status": "waiting", "tournament_id": "tournament-9"}
    )

    assert match.tournament_id == "tournament-9"


def test_match_game_without_client_raises_value_error():
    match = Match.from_dict({"session_id": "session-1", "status": "in_progress"})

    with pytest.raises(ValueError):
        match.game()


def test_agent_sessions_maps_server_groups_to_waiting_active_completed():
    sessions = AgentSessions.from_dict(
        {
            "joined_sessions": [{"session_id": "s-waiting", "status": "waiting"}],
            "active_sessions": [{"session_id": "s-active", "status": "in_progress"}],
            "completed_sessions": [{"session_id": "s-done", "status": "completed"}],
        }
    )

    assert [m.session_id for m in sessions.waiting] == ["s-waiting"]
    assert [m.session_id for m in sessions.active] == ["s-active"]
    assert [m.session_id for m in sessions.completed] == ["s-done"]


def test_agent_sessions_tolerates_missing_groups():
    sessions = AgentSessions.from_dict({})

    assert sessions.waiting == []
    assert sessions.active == []
    assert sessions.completed == []


def test_game_state_terminal_with_returns():
    state = GameState.from_dict(
        _base_game_state_payload(
            is_terminal=True,
            current_player=None,
            legal_actions=[],
            returns={"Alice": 1.0, "Bob": -1.0},
            termination_reason="completed",
            next_actions=[{"action": "game_over", "hint": "Game finished."}],
        )
    )

    assert state.is_terminal is True
    assert state.returns == {"Alice": 1.0, "Bob": -1.0}
    assert state.termination_reason == "completed"
    assert [a.action for a in state.next_actions] == ["game_over"]
