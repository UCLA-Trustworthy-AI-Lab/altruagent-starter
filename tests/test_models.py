"""Unit tests for altruagent.models."""

from __future__ import annotations

import pytest

from altruagent.models import (
    Agent,
    AgentSessions,
    GameState,
    Match,
    Message,
    NextAction,
    Tournament,
    TournamentViewer,
)


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


# -- messaging (Milestone 5) ----------------------------------------------


def test_message_from_dict_parses_all_fields():
    message = Message.from_dict(
        {
            "index": 3,
            "sender": 0,
            "recipients": [1],
            "content": "let's cooperate",
            "type": "chat",
            "sent_at": "2026-01-01T00:00:00Z",
            "move_index": 2,
        }
    )

    assert message.index == 3
    assert message.sender == 0
    assert message.recipients == [1]
    assert message.content == "let's cooperate"
    assert message.type == "chat"
    assert message.move_index == 2


def test_message_from_dict_tolerates_missing_fields():
    message = Message.from_dict({})

    assert message.index == 0
    assert message.sender == 0
    assert message.recipients == []
    assert message.content == ""
    assert message.type == "chat"
    assert message.move_index == 0


def test_game_state_parses_new_messages_as_typed_message_objects():
    state = GameState.from_dict(
        _base_game_state_payload(
            messaging_enabled=True,
            phase="messaging",
            messaging_mode="per_all_moves",
            terminated_messaging=[1],
            new_messages=[
                {
                    "index": 0,
                    "sender": 1,
                    "recipients": [],
                    "content": "hello",
                    "type": "chat",
                    "sent_at": "2026-01-01T00:00:00Z",
                    "move_index": 0,
                }
            ],
        )
    )

    assert state.messaging_mode == "per_all_moves"
    assert state.terminated_messaging == [1]
    assert len(state.new_messages) == 1
    assert isinstance(state.new_messages[0], Message)
    assert state.new_messages[0].content == "hello"
    assert state.new_messages[0].sender == 1


def test_game_state_messaging_fields_default_when_absent():
    payload = _base_game_state_payload()
    state = GameState.from_dict(payload)

    assert state.new_messages == []
    assert state.terminated_messaging == []
    assert state.messaging_mode == "per_move"
    assert state.messaging_enabled is False
    assert state.phase == "moving"


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


# -- Tournament / TournamentViewer -----------------------------------------


def test_tournament_from_dict_list_shape():
    # GET /tournaments list entries are raw DB rows: no viewer, no
    # game_server_url (never a stored column).
    tournament = Tournament.from_dict(
        {
            "tournament_id": "t-1",
            "game_type": "tic_tac_toe",
            "status": "waiting",
            "max_participants": 2,
            "current_participants": 1,
            "max_active_matches": 1,
            "queue_id": None,
            "created_by_user_id": "user-1",
            "metadata": None,
            "created_at": "2026-01-01T00:00:00Z",
        }
    )

    assert tournament.tournament_id == "t-1"
    assert tournament.status == "waiting"
    assert tournament.current_participants == 1
    assert tournament.game_server_url is None
    assert tournament.viewer is None
    assert tournament.raw["created_by_user_id"] == "user-1"


def test_tournament_from_dict_detail_shape_with_viewer():
    # GET /tournaments/{id}'s `tournament` sub-object (compactTournament) can
    # include game_server_url; `viewer` is passed separately (it's a sibling
    # key in the response body, not nested inside `tournament`).
    tournament = Tournament.from_dict(
        {
            "tournament_id": "t-1",
            "game_type": "tic_tac_toe",
            "status": "in_progress",
            "max_participants": 2,
            "current_participants": 2,
            "max_active_matches": 1,
            "queue_id": None,
            "game_server_url": "host:8000",
        },
        viewer={
            "agent_id": "agent-1",
            "is_tournament_participant": True,
            "active_child_session_ids": ["session-1"],
            "should_join_tournament": False,
            "should_wait_for_child_match": False,
            "next_actions": [
                {"action": "play_child_session", "endpoint": "GET .../games/session-1", "hint": "Play it."}
            ],
        },
    )

    assert tournament.game_server_url == "host:8000"
    assert tournament.viewer is not None
    assert tournament.viewer.agent_id == "agent-1"
    assert tournament.viewer.is_tournament_participant is True
    assert tournament.viewer.active_child_session_ids == ["session-1"]
    assert [a.action for a in tournament.viewer.next_actions] == ["play_child_session"]


def test_tournament_viewer_absent_gives_none():
    tournament = Tournament.from_dict({"tournament_id": "t-1", "status": "waiting"})

    assert tournament.viewer is None


def test_tournament_tolerates_missing_optional_fields():
    tournament = Tournament.from_dict({"tournament_id": "t-1", "status": "waiting"})

    assert tournament.game_type is None
    assert tournament.max_participants is None
    assert tournament.current_participants is None
    assert tournament.queue_id is None
    assert tournament.game_server_url is None


def test_tournament_preserves_unknown_fields_in_raw():
    tournament = Tournament.from_dict(
        {"tournament_id": "t-1", "status": "waiting", "leaderboard": [], "messaging_config": {"messaging_enabled": True}}
    )

    assert tournament.raw["leaderboard"] == []
    assert tournament.raw["messaging_config"] == {"messaging_enabled": True}


def test_tournament_has_no_name_attribute():
    # There is no `name` field anywhere on the backend's tournaments table —
    # guard against ever accidentally assuming one exists.
    tournament = Tournament.from_dict({"tournament_id": "t-1", "status": "waiting", "name": "Ignored"})

    assert not hasattr(tournament, "name")
    # If a "name" key is ever present in a payload (it shouldn't be), it's
    # only reachable via raw, never promoted to a typed attribute.
    assert tournament.raw.get("name") == "Ignored"


def test_tournament_viewer_from_dict_defaults():
    viewer = TournamentViewer.from_dict({})

    assert viewer.agent_id is None
    assert viewer.is_tournament_participant is False
    assert viewer.active_child_session_ids == []
    assert viewer.next_actions == []
