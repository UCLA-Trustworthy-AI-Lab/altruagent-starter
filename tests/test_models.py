"""Unit tests for altruagent.models."""

from __future__ import annotations

import pytest

from altruagent.models import (
    Agent,
    AgentRef,
    AgentSessions,
    AgentTournamentMatch,
    GameState,
    JoinResult,
    LegalAction,
    Match,
    Message,
    NextAction,
    TournamentDetail,
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
    # REST's separate legal_actions/legal_actions_str int/str lists are
    # synthesized into LegalAction entries — the same shape MCP's
    # get_legal_actions returns natively (see from_mcp_state below) — so
    # contestant code never has to care which transport produced a GameState.
    state = GameState.from_dict(
        _base_game_state_payload(legal_actions=[0, 4, 8], legal_actions_str=["a", "b", "c"])
    )

    assert [a.action_id for a in state.legal_actions] == ["0", "4", "8"]
    assert [a.label for a in state.legal_actions] == ["a", "b", "c"]
    assert all(isinstance(a, LegalAction) for a in state.legal_actions)


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


def test_match_tournament_id_preserved_for_tournament_games():
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


# -- LegalAction / GameState.from_mcp_state (Milestone 6: MCP-first) -------


def test_legal_action_from_dict_openspiel_shape():
    action = LegalAction.from_dict(
        {"action_id": "0", "label": "cooperate", "input": {"session_id": "s-1", "action_id": "0"}}
    )

    assert action.action_id == "0"
    assert action.label == "cooperate"
    assert action.input == {"session_id": "s-1", "action_id": "0"}


def test_legal_action_from_dict_structured_shape():
    # Pokemon-shaped: action_id is not int-coercible, input carries a richer
    # structured payload than OpenSpiel-family games ever need.
    action = LegalAction.from_dict(
        {
            "action_id": "move:0",
            "label": "Use Thunderbolt",
            "input": {"type": "move", "slot": 0, "move_id": "thunderbolt", "base_power": 90},
        }
    )

    assert action.action_id == "move:0"
    assert action.input["type"] == "move"
    assert action.input["base_power"] == 90


def _mcp_state_payload(**overrides) -> dict:
    payload = {
        "session_id": "session-1",
        "game_type": "tic_tac_toe",
        "runtime_adapter": "openspiel",
        "status": "in_progress",
        "state_version": 3,
        "observation": "...",
        "phase": "moving",
        "messaging_enabled": False,
        "terminated_messaging": [],
        "new_messages": [],
        "current_actor": {"agent_id": "agent-1", "position": 0},
        "is_current_actor": True,
        "is_terminal": False,
        "legal_action_count": 3,
    }
    payload.update(overrides)
    return payload


def test_game_state_from_mcp_state_parses_generic_fields():
    state = GameState.from_mcp_state(_mcp_state_payload())

    assert state.session_id == "session-1"
    assert state.game_name == "tic_tac_toe"
    assert state.state_version == 3
    assert state.phase == "moving"
    assert state.is_current_actor is True
    assert state.is_terminal is False
    assert state.current_player.name == "agent-1"
    # get_game_state alone carries no legal_actions (a separate tool) —
    # confirmed empty until the runner merges in a get_legal_actions result.
    assert state.legal_actions == []


def test_game_state_from_mcp_state_merges_legal_actions_and_its_state_version():
    legal_actions = {
        "session_id": "session-1",
        "state_version": 4,  # freshest — the one that should win
        "actions": [
            {"action_id": "0", "label": "a", "input": {}},
            {"action_id": "1", "label": "b", "input": {}},
        ],
    }
    state = GameState.from_mcp_state(_mcp_state_payload(state_version=3), legal_actions=legal_actions)

    assert state.state_version == 4
    assert [a.action_id for a in state.legal_actions] == ["0", "1"]


def test_game_state_from_mcp_state_merges_result_for_terminal_fields():
    # get_game_state/play_action never carry returns/termination_reason —
    # confirmed against openspiel_adapter.py; only get_result/resign do.
    result = {
        "session_id": "session-1",
        "is_terminal": True,
        "status": "completed",
        "returns": {"Alice": 1.0, "Bob": -1.0},
        "your_return": 1.0,
        "termination_reason": "completed",
    }
    state = GameState.from_mcp_state(
        _mcp_state_payload(is_terminal=True, is_current_actor=False), result=result
    )

    assert state.is_terminal is True
    assert state.returns == {"Alice": 1.0, "Bob": -1.0}
    assert state.termination_reason == "completed"


def test_game_state_from_mcp_state_pokemon_shaped_never_reports_messaging():
    # Confirmed against pokemon_adapter.py: phases are draft/draft_complete/
    # teambuild/moving, never "messaging" — messaging_enabled is always False.
    state = GameState.from_mcp_state(
        _mcp_state_payload(
            game_type="pokemon_gen9ou_draft",
            phase="draft",
            messaging_enabled=False,
            current_actor=None,
            is_current_actor=True,
        )
    )

    assert state.phase == "draft"
    assert state.messaging_enabled is False


def test_game_state_from_mcp_state_new_messages_parsed_as_message_objects():
    state = GameState.from_mcp_state(
        _mcp_state_payload(
            phase="messaging",
            messaging_enabled=True,
            new_messages=[
                {
                    "message_id": "session-1:0",
                    "index": 0,
                    "sender": 1,
                    "recipients": [],
                    "content": "hello",
                    "type": "chat",
                    "sent_at": "2026-01-01T00:00:00Z",
                    "move_index": 2,
                }
            ],
        )
    )

    assert len(state.new_messages) == 1
    assert state.new_messages[0].content == "hello"
    assert state.new_messages[0].sender == 1


# -- Platform tournaments: AgentTournamentMatch / TournamentDetail / JoinResult ------
# Shapes follow Agent_ACP backend/src/models/swissTournament.ts.


def tournament_match_row(**overrides) -> dict:
    row = {
        "tournament_id": "t-1",
        "tournament_name": "Autumn Cup",
        "round_label": "Swiss round 2 of 4",
        "match_id": "r2-m3",
        "session_id": "game-7",
        "game_type": "pokemon_vgc_doubles_draft",
        "game_no": 1,
        "join_deadline_at": "2026-10-06T12:04:00.000Z",
        "seconds_left": 187,
        "status": "join_now",
        "opponents": [{"agent_id": "agent-b", "agent_name": "Bulbasaur Bot"}],
    }
    row.update(overrides)
    return row


def tournament_detail_payload(**overrides) -> dict:
    payload = {
        "tournament_id": "t-1",
        "name": "Autumn Cup",
        "description": None,
        "game_type": "pokemon_vgc_doubles_draft",
        "game_family": "pokemon",
        "game_label": "Pokémon (VGC doubles draft)",
        "status": "in_progress",
        "phase": "swiss",
        "scheduled_start_at": None,
        "started_at": "2026-10-06T12:00:00.000Z",
        "completed_at": None,
        "created_at": "2026-10-05T09:00:00.000Z",
        "config": {"swiss_rounds": 4, "top_cut": 4, "best_of": 3, "finals_games": 4, "join_window_seconds": 240},
        "max_participants": 128,
        "participant_count": 9,
        "current_round": {"index": 2, "phase": "swiss", "number": 2, "label": "Swiss round 2 of 4"},
        "planned_rounds": 6,
        "champion": None,
        "participants": [],
        "standings": [
            {"rank": 1, "agent_id": "agent-a", "agent_name": "Alpha", "points": 2, "wins": 2, "losses": 0,
             "draws": 0, "byes": 0, "no_shows": 0, "games_played": 2, "buchholz": 1, "in_top_cut": True},
            {"rank": 2, "agent_id": "agent-b", "agent_name": "Bulbasaur Bot", "points": 1, "wins": 0, "losses": 1,
             "draws": 0, "byes": 1, "no_shows": 1, "games_played": 1, "buchholz": 2, "in_top_cut": True},
        ],
        "rounds": [{"index": 1, "phase": "swiss", "number": 1, "label": "Swiss round 1 of 4", "matches": []}],
        "bracket": None,
        "finals": None,
        "final_ranking": None,
        "events": [{"id": "e3", "at": "2026-10-06T12:00:00.000Z", "kind": "round_started", "message": "…"}],
        "viewer": None,
    }
    payload.update(overrides)
    return payload


def test_agent_tournament_match_from_dict_parses_every_field():
    row = AgentTournamentMatch.from_dict(tournament_match_row())

    assert row.tournament_id == "t-1"
    assert row.tournament_name == "Autumn Cup"
    assert row.round_label == "Swiss round 2 of 4"
    assert row.match_id == "r2-m3"
    assert row.session_id == "game-7"
    assert row.game_type == "pokemon_vgc_doubles_draft"
    assert row.game_no == 1
    assert row.join_deadline_at == "2026-10-06T12:04:00.000Z"
    assert row.seconds_left == 187
    assert row.status == "join_now"
    assert row.needs_join is True
    assert row.opponents == [AgentRef(agent_id="agent-b", agent_name="Bulbasaur Bot")]
    assert row.raw["match_id"] == "r2-m3"


@pytest.mark.parametrize("status", ["joined_waiting", "in_progress"])
def test_agent_tournament_match_needs_join_only_for_join_now(status):
    assert AgentTournamentMatch.from_dict(tournament_match_row(status=status)).needs_join is False


def test_agent_tournament_match_tolerates_missing_and_odd_fields():
    row = AgentTournamentMatch.from_dict({"session_id": "game-1", "seconds_left": -3, "opponents": None})

    assert row.session_id == "game-1"
    assert row.seconds_left == 0
    assert row.game_no == 1
    assert row.status == "unknown"
    assert row.needs_join is False
    assert row.opponents == []


def test_werewolf_table_lists_every_tablemate_as_an_opponent():
    opponents = [{"agent_id": f"agent-{i}", "agent_name": f"Wolf {i}"} for i in range(6)]
    row = AgentTournamentMatch.from_dict(tournament_match_row(game_type="werewolf", match_id="r1-t2", opponents=opponents))

    assert [o.agent_name for o in row.opponents] == [f"Wolf {i}" for i in range(6)]


def test_agent_sessions_parses_tournament_matches():
    sessions = AgentSessions.from_dict(
        {
            "joined_sessions": [],
            "active_sessions": [{"session_id": "game-6", "status": "in_progress", "tournament_id": "t-1"}],
            "completed_sessions": [],
            "tournament_matches": [
                tournament_match_row(),
                tournament_match_row(session_id="game-6", status="in_progress", tournament_id="t-2"),
                "not-a-row",
            ],
        }
    )

    assert [r.session_id for r in sessions.tournament_matches] == ["game-7", "game-6"]
    assert sessions.tournament_matches[1].tournament_id == "t-2"
    assert sessions.active[0].tournament_id == "t-1"


def test_agent_sessions_without_tournament_matches_gives_empty_list():
    assert AgentSessions.from_dict({"joined_sessions": []}).tournament_matches == []


def test_tournament_detail_from_dict_parses_summary_and_standings():
    detail = TournamentDetail.from_dict(tournament_detail_payload())

    assert detail.tournament_id == "t-1"
    assert detail.name == "Autumn Cup"
    assert detail.status == "in_progress"
    assert detail.phase == "swiss"
    assert detail.game_type == "pokemon_vgc_doubles_draft"
    assert detail.game_label == "Pokémon (VGC doubles draft)"
    assert detail.participant_count == 9
    assert detail.max_participants == 128
    assert detail.current_round == "Swiss round 2 of 4"
    assert detail.planned_rounds == 6
    assert detail.config["best_of"] == 3
    assert detail.champion is None
    assert detail.final_ranking is None
    assert detail.is_finished is False
    assert [s.agent_name for s in detail.standings] == ["Alpha", "Bulbasaur Bot"]
    second = detail.standings[1]
    assert (second.rank, second.points, second.wins, second.losses, second.byes, second.no_shows) == (2, 1, 0, 1, 1, 1)
    assert second.buchholz == 2 and second.in_top_cut is True
    # Not individually modeled, still reachable.
    assert detail.raw["rounds"][0]["label"] == "Swiss round 1 of 4"
    assert detail.raw["events"][0]["kind"] == "round_started"


def test_tournament_detail_completed_has_champion_and_final_ranking():
    detail = TournamentDetail.from_dict(
        tournament_detail_payload(
            status="completed",
            phase="completed",
            champion={"agent_id": "agent-a", "agent_name": "Alpha"},
            final_ranking=[
                {"rank": 1, "agent_id": "agent-a", "agent_name": "Alpha", "points": 3},
                {"rank": 2, "agent_id": "agent-b", "agent_name": "Bulbasaur Bot", "points": 2},
            ],
        )
    )

    assert detail.is_finished is True
    assert detail.champion == AgentRef(agent_id="agent-a", agent_name="Alpha")
    assert [row["agent_id"] for row in detail.final_ranking] == ["agent-a", "agent-b"]


@pytest.mark.parametrize("status, finished", [("registration", False), ("in_progress", False),
                                              ("completed", True), ("cancelled", True)])
def test_tournament_detail_is_finished(status, finished):
    assert TournamentDetail.from_dict({"status": status}).is_finished is finished


def test_tournament_detail_tolerates_a_minimal_payload():
    detail = TournamentDetail.from_dict({"tournament_id": "t-1"})

    assert detail.status == "unknown"
    assert detail.current_round is None
    assert detail.standings == []
    assert detail.final_ranking is None
    assert detail.champion is None


def test_join_result_from_mcp_payload():
    result = JoinResult.from_dict(
        {"status": "waiting", "session_id": "game-7", "game_type": "werewolf", "runtime_adapter": "openspiel",
         "position": 3, "current_participants": 4, "max_participants": 7,
         "next_actions": [{"tool": "get_agent_status", "hint": "Poll until the session starts."}]},
        session_id="game-7",
        transport="mcp",
    )

    assert (result.session_id, result.status, result.already_joined) == ("game-7", "waiting", False)
    assert (result.position, result.current_participants, result.max_participants) == (3, 4, 7)
    assert result.transport == "mcp"


def test_join_result_from_rest_payload_already_joined():
    result = JoinResult.from_dict(
        {"success": True, "already_joined": True, "session_id": "game-7", "status": "in_progress",
         "position": 1, "participants": 2, "max_participants": 2},
        session_id="game-7",
        transport="rest",
    )

    assert result.already_joined is True
    assert result.status == "in_progress"
    assert result.current_participants == 2
    assert result.transport == "rest"


def test_message_from_dict_prefers_platform_seq_over_legacy_index():
    assert Message.from_dict({"seq": 14, "index": 2}).index == 14
    assert Message.from_dict({"index": 2}).index == 2
