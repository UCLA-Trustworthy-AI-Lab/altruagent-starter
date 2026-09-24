"""Unit tests for altruagent.runner (run_game / run_match) — MCP-first.

All gameplay is driven through a lightweight FakeMCPGameSession test double —
run_game only needs an object exposing get_state()/wait_for_update()/
get_legal_actions()/play_action()/send_message()/get_messages()/resign()/
get_result(), so there
is no need to mock HTTP for these; MCPGameSession's own transport is covered
by tests/test_mcp_game.py and tests/test_mcp_transport.py.
"""

from __future__ import annotations

import pytest

from altruagent.mcp_transport import MCPToolError
from altruagent.models import DecisionContext, GameState, LegalAction, Match
from altruagent.runner import (
    RESIGN,
    TERMINATE_MESSAGING,
    DecisionError,
    SendMessage,
    UnsupportedGameFlowError,
    run_game,
    run_match,
)

SESSION_ID = "session-1"


class FakeMCPGameSession:
    """Scripted stand-in for MCPGameSession. Queue return values (or
    exception instances, raised) per method — each method pops its own
    queue, so a test only needs to set up the calls it actually expects.
    """

    def __init__(self, session_id: str = SESSION_ID) -> None:
        self.session_id = session_id
        self.game_server_url = "http://fake:8000"
        self._state_queue: list = []
        self._legal_actions_queue: list = []
        self._play_action_queue: list = []
        self._send_message_queue: list = []
        self._resign_queue: list = []
        self._result_queue: list = []
        self.play_action_calls: list[dict] = []
        self.send_message_calls: list[dict] = []
        self.wait_calls: list[dict] = []
        self.get_state_calls = 0
        self.get_legal_actions_calls = 0
        self.resign_calls = 0
        # False simulates a server that predates wait_for_update.
        self.supports_wait = True

    # -- queueing helpers (chainable) ----------------------------------

    def queue_state(self, *items) -> "FakeMCPGameSession":
        self._state_queue.extend(items)
        return self

    def queue_legal_actions(self, *items) -> "FakeMCPGameSession":
        self._legal_actions_queue.extend(items)
        return self

    def queue_play_action(self, *items) -> "FakeMCPGameSession":
        self._play_action_queue.extend(items)
        return self

    def queue_send_message(self, *items) -> "FakeMCPGameSession":
        self._send_message_queue.extend(items)
        return self

    def queue_resign(self, *items) -> "FakeMCPGameSession":
        self._resign_queue.extend(items)
        return self

    def queue_result(self, *items) -> "FakeMCPGameSession":
        self._result_queue.extend(items)
        return self

    @staticmethod
    def _pop(queue: list):
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    # -- MCPGameSession-shaped interface --------------------------------

    def get_state(self) -> GameState:
        self.get_state_calls += 1
        return self._pop(self._state_queue)

    def wait_for_update(
        self,
        *,
        since_version,
        since_message_seq=None,
        since_is_current_actor=None,
        since_phase=None,
        timeout_seconds=None,
    ) -> GameState:
        """Pops from the same state queue as get_state — a wait's result is
        just the next state.
        """
        self.wait_calls.append(
            {
                "since_version": since_version,
                "since_message_seq": since_message_seq,
                "since_is_current_actor": since_is_current_actor,
                "since_phase": since_phase,
                "timeout_seconds": timeout_seconds,
            }
        )
        if not self.supports_wait:
            raise MCPToolError(
                "MCP tool 'wait_for_update' failed at the protocol level: "
                "Unknown tool: wait_for_update",
                status_code=None,
                error_code=None,
            )
        return self._pop(self._state_queue)

    def get_legal_actions(self) -> dict:
        self.get_legal_actions_calls += 1
        return self._pop(self._legal_actions_queue)

    def play_action(self, *, action_id=None, action=None, state_version) -> dict:
        self.play_action_calls.append(
            {"action_id": action_id, "action": action, "state_version": state_version}
        )
        return self._pop(self._play_action_queue)

    def send_message(self, *, message_type: str, content=None, recipients=None) -> dict:
        self.send_message_calls.append(
            {"message_type": message_type, "content": content, "recipients": recipients}
        )
        return self._pop(self._send_message_queue)

    def get_messages(self, *, since: int = -1) -> dict:
        raise AssertionError("get_messages not exercised by these tests")

    def resign(self) -> dict:
        self.resign_calls += 1
        return self._pop(self._resign_queue)

    def get_result(self) -> dict:
        return self._pop(self._result_queue)


def make_mcp_state(**overrides) -> GameState:
    payload = {
        "session_id": SESSION_ID,
        "game_type": "tic_tac_toe",
        "status": "in_progress",
        "state_version": 0,
        "observation": "...",
        "phase": "moving",
        "messaging_enabled": False,
        "terminated_messaging": [],
        "new_messages": [],
        "current_actor": {"agent_id": "agent-1", "position": 0},
        "is_current_actor": True,
        "is_terminal": False,
    }
    payload.update(overrides)
    return GameState.from_mcp_state(payload)


def waiting_state(**overrides) -> GameState:
    overrides.setdefault("is_current_actor", False)
    overrides.setdefault("current_actor", {"agent_id": "opponent-1", "position": 1})
    return make_mcp_state(**overrides)


def terminal_state(**overrides) -> GameState:
    overrides.setdefault("is_terminal", True)
    overrides.setdefault("is_current_actor", False)
    return make_mcp_state(**overrides)


def messaging_state(**overrides) -> GameState:
    overrides.setdefault("phase", "messaging")
    overrides.setdefault("messaging_enabled", True)
    return make_mcp_state(**overrides)


def legal_actions_result(actions: list[dict], *, state_version: int = 0) -> dict:
    return {"session_id": SESSION_ID, "state_version": state_version, "actions": actions}


def int_actions(*ints, state_version: int = 0) -> dict:
    return legal_actions_result(
        [{"action_id": str(i), "label": str(i), "input": {}} for i in ints],
        state_version=state_version,
    )


def structured_actions(*action_ids, state_version: int = 0) -> dict:
    return legal_actions_result(
        [{"action_id": aid, "label": aid, "input": {"type": "move"}} for aid in action_ids],
        state_version=state_version,
    )


def play_action_result(*, status: str = "in_progress", state_version: int = 1) -> dict:
    return {"accepted": True, "session_id": SESSION_ID, "state_version": state_version, "status": status}


def result_dict(**overrides) -> dict:
    payload = {
        "session_id": SESSION_ID,
        "is_terminal": True,
        "status": "completed",
        "returns": {"Me": 1.0, "Them": -1.0},
        "your_return": 1.0,
        "termination_reason": None,
    }
    payload.update(overrides)
    return payload


CONTEXT = DecisionContext(
    session_id=SESSION_ID, tournament_id=None, game_type="tic_tac_toe", agent_id="agent-1"
)


def no_sleep(_seconds: float) -> None:
    pass


# -- decision contract ------------------------------------------------------


def test_universal_pattern_return_first_legal_action_works():
    game = (
        FakeMCPGameSession()
        .queue_state(make_mcp_state(), terminal_state())
        .queue_legal_actions(int_actions(0, 1))
        .queue_play_action(play_action_result())
        .queue_result(result_dict())
    )
    result = run_game(game, CONTEXT, lambda s, c: s.legal_actions[0], sleep=no_sleep)

    assert result.is_terminal is True
    assert game.play_action_calls == [{"action_id": "0", "action": None, "state_version": 0}]


def test_object_with_choose_action_method_works():
    class Agent:
        def choose_action(self, state, context):
            return state.legal_actions[-1]

    game = (
        FakeMCPGameSession()
        .queue_state(make_mcp_state(), terminal_state())
        .queue_legal_actions(int_actions(0, 1, 2))
        .queue_play_action(play_action_result())
        .queue_result(result_dict())
    )
    result = run_game(game, CONTEXT, Agent(), sleep=no_sleep)

    assert result.is_terminal is True
    assert game.play_action_calls[0]["action_id"] == "2"


def test_invalid_decision_object_fails_clearly():
    game = FakeMCPGameSession()  # empty queues: get_state must never be called

    with pytest.raises(DecisionError):
        run_game(game, CONTEXT, 42, sleep=no_sleep)  # not callable, no .choose_action


def test_action_id_string_decision_accepted():
    game = (
        FakeMCPGameSession()
        .queue_state(make_mcp_state(), terminal_state())
        .queue_legal_actions(int_actions(0, 1))
        .queue_play_action(play_action_result())
        .queue_result(result_dict())
    )
    run_game(game, CONTEXT, lambda s, c: "1", sleep=no_sleep)

    assert game.play_action_calls == [{"action_id": "1", "action": None, "state_version": 0}]


def test_legal_action_object_decision_accepted():
    def choose_action(state, context):
        return next(a for a in state.legal_actions if a.action_id == "1")

    game = (
        FakeMCPGameSession()
        .queue_state(make_mcp_state(), terminal_state())
        .queue_legal_actions(int_actions(0, 1))
        .queue_play_action(play_action_result())
        .queue_result(result_dict())
    )
    run_game(game, CONTEXT, choose_action, sleep=no_sleep)

    assert game.play_action_calls == [{"action_id": "1", "action": None, "state_version": 0}]


def test_structured_dict_decision_passed_through_verbatim():
    payload = {"type": "submit_team", "team": ["a", "b", "c", "d", "e", "f"]}
    game = (
        FakeMCPGameSession()
        .queue_state(make_mcp_state(), terminal_state())
        .queue_legal_actions(structured_actions("submit_team", state_version=7))
        .queue_play_action(play_action_result())
        .queue_result(result_dict())
    )
    run_game(game, CONTEXT, lambda s, c: payload, sleep=no_sleep)

    assert game.play_action_calls == [{"action_id": None, "action": payload, "state_version": 7}]


def test_empty_dict_decision_rejected():
    game = FakeMCPGameSession().queue_state(make_mcp_state()).queue_legal_actions(int_actions(0, 1))

    with pytest.raises(DecisionError):
        run_game(game, CONTEXT, lambda s, c: {}, sleep=no_sleep)

    assert game.play_action_calls == []


def test_int_accepted_only_when_it_matches_a_stringified_action_id():
    game = (
        FakeMCPGameSession()
        .queue_state(make_mcp_state(), terminal_state())
        .queue_legal_actions(int_actions(0, 1))
        .queue_play_action(play_action_result())
        .queue_result(result_dict())
    )
    run_game(game, CONTEXT, lambda s, c: 1, sleep=no_sleep)

    assert game.play_action_calls == [{"action_id": "1", "action": None, "state_version": 0}]


def test_int_rejected_for_structured_game_never_guessed():
    # Pokemon-shaped action_ids ("move:0") never match a bare int — this
    # must fail clearly, not silently guess which action was meant.
    game = FakeMCPGameSession().queue_state(make_mcp_state()).queue_legal_actions(
        structured_actions("move:0", "switch:1")
    )

    with pytest.raises(DecisionError):
        run_game(game, CONTEXT, lambda s, c: 0, sleep=no_sleep)

    assert game.play_action_calls == []


def test_non_matching_string_action_id_rejected():
    game = FakeMCPGameSession().queue_state(make_mcp_state()).queue_legal_actions(int_actions(0, 1))

    with pytest.raises(DecisionError):
        run_game(game, CONTEXT, lambda s, c: "99", sleep=no_sleep)

    assert game.play_action_calls == []


def test_bool_decision_rejected_even_though_it_is_a_legal_action_value():
    # isinstance(True, int) is True in Python, and True == 1 — without an
    # explicit bool guard, returning True would silently be treated as
    # legal action "1". It must not be.
    game = FakeMCPGameSession().queue_state(make_mcp_state()).queue_legal_actions(int_actions(0, 1))

    with pytest.raises(DecisionError):
        run_game(game, CONTEXT, lambda s, c: True, sleep=no_sleep)

    assert game.play_action_calls == []


def test_unrecognized_type_decision_rejected():
    game = FakeMCPGameSession().queue_state(make_mcp_state()).queue_legal_actions(int_actions(0, 1))

    with pytest.raises(DecisionError):
        run_game(game, CONTEXT, lambda s, c: 3.14, sleep=no_sleep)

    assert game.play_action_calls == []


# -- flow ---------------------------------------------------------------


def test_make_move_invokes_decision_exactly_once_for_that_state():
    calls = []

    def choose_action(state, context):
        calls.append(state)
        return state.legal_actions[0]

    game = (
        FakeMCPGameSession()
        .queue_state(make_mcp_state(), terminal_state())
        .queue_legal_actions(int_actions(0, 1))
        .queue_play_action(play_action_result())
        .queue_result(result_dict())
    )
    run_game(game, CONTEXT, choose_action, sleep=no_sleep)

    assert len(calls) == 1


def test_wait_for_opponent_does_not_invoke_decision_or_fetch_legal_actions():
    def choose_action(state, context):
        raise AssertionError("choose_action should not be called while waiting")

    sleep_calls = []
    game = (
        FakeMCPGameSession()
        .queue_state(waiting_state(state_version=4), terminal_state())
        .queue_result(result_dict())
    )
    result = run_game(game, CONTEXT, choose_action, sleep=sleep_calls.append)

    assert result.is_terminal is True
    # Long-polls the server rather than sleeping client-side.
    # Sends what it last saw, so a turn/phase change that lands before the
    # call still wakes it.
    assert game.wait_calls == [
        {
            "since_version": 4,
            "since_message_seq": None,
            "since_is_current_actor": False,
            "since_phase": "moving",
            "timeout_seconds": 20.0,
        }
    ]
    assert sleep_calls == []
    assert game.get_legal_actions_calls == 0


def test_wait_falls_back_to_sleep_when_server_lacks_wait_for_update():
    sleep_calls = []
    game = (
        FakeMCPGameSession()
        .queue_state(waiting_state(), waiting_state(), terminal_state())
        .queue_result(result_dict())
    )
    game.supports_wait = False
    result = run_game(game, CONTEXT, lambda s, c: RESIGN, sleep=sleep_calls.append)

    assert result.is_terminal is True
    # Tried once, then remembered the server doesn't have it.
    assert len(game.wait_calls) == 1
    assert sleep_calls == [5.0, 5.0]  # DEFAULT_WAIT_SECONDS


def test_wait_for_update_protocol_error_other_than_unknown_tool_propagates():
    game = FakeMCPGameSession().queue_state(
        waiting_state(),
        MCPToolError("transport failed", status_code=None, error_code=None),
    )

    with pytest.raises(MCPToolError):
        run_game(game, CONTEXT, lambda s, c: RESIGN, sleep=no_sleep)


def test_wait_passes_highest_seen_message_seq():
    state = waiting_state(
        state_version=3,
        new_messages=[{"seq": 5, "sender": 1, "content": "hi"}, {"seq": 8, "sender": 2, "content": "yo"}],
    )
    game = FakeMCPGameSession().queue_state(state, terminal_state()).queue_result(result_dict())
    run_game(game, CONTEXT, lambda s, c: RESIGN, sleep=no_sleep)

    assert game.wait_calls[0]["since_message_seq"] == 8


def test_terminal_state_exits_cleanly_and_fetches_result():
    def choose_action(state, context):
        raise AssertionError("choose_action should not be called on a terminal state")

    game = FakeMCPGameSession().queue_state(terminal_state()).queue_result(result_dict())
    result = run_game(game, CONTEXT, choose_action, sleep=no_sleep)

    assert result.is_terminal is True
    assert result.returns == {"Me": 1.0, "Them": -1.0}
    assert game.get_legal_actions_calls == 0


def test_repeated_wait_then_make_move_transition():
    calls = []

    def choose_action(state, context):
        calls.append(state)
        return state.legal_actions[0]

    sleep_calls = []
    game = (
        FakeMCPGameSession()
        .queue_state(waiting_state(), waiting_state(), make_mcp_state(), terminal_state())
        .queue_legal_actions(int_actions(0, 1))
        .queue_play_action(play_action_result())
        .queue_result(result_dict())
    )
    result = run_game(game, CONTEXT, choose_action, sleep=sleep_calls.append)

    assert result.is_terminal is True
    assert len(game.wait_calls) == 2
    assert sleep_calls == []
    assert len(calls) == 1


# -- resign -----------------------------------------------------------------


def test_resign_calls_resign_not_play_action():
    game = (
        FakeMCPGameSession()
        .queue_state(make_mcp_state())
        .queue_legal_actions(int_actions(0, 1))
        .queue_resign(result_dict(termination_reason="resignation"))
    )
    result = run_game(game, CONTEXT, lambda s, c: RESIGN, sleep=no_sleep)

    assert result.is_terminal is True
    assert result.termination_reason == "resignation"
    assert game.resign_calls == 1
    assert game.play_action_calls == []


def test_resign_still_works_when_choose_message_is_defined():
    class Agent:
        def choose_action(self, state, context):
            return RESIGN

        def choose_message(self, state, context):
            return TERMINATE_MESSAGING

    game = (
        FakeMCPGameSession()
        .queue_state(make_mcp_state())
        .queue_legal_actions(int_actions(0, 1))
        .queue_resign(result_dict(termination_reason="resignation"))
    )
    result = run_game(game, CONTEXT, Agent(), sleep=no_sleep)

    assert result.is_terminal is True
    assert game.resign_calls == 1


# -- messaging ----------------------------------------------------------


def test_messaging_phase_never_invokes_choose_action():
    def choose_action(state, context):
        raise AssertionError("choose_action must not run during a messaging phase")

    game = (
        FakeMCPGameSession()
        .queue_state(messaging_state(), terminal_state())
        .queue_send_message({"accepted": True, "phase": "moving"})
        .queue_result(result_dict())
    )
    result = run_game(game, CONTEXT, choose_action, sleep=no_sleep)

    assert result.is_terminal is True
    assert game.play_action_calls == []


def test_default_choose_message_auto_terminates_when_absent():
    # No choose_message defined anywhere (bare function choose_action) — the
    # existing simple starter agent must still finish a messaging-enabled
    # match without any new code.
    game = (
        FakeMCPGameSession()
        .queue_state(messaging_state(), terminal_state())
        .queue_send_message({"accepted": True, "phase": "moving"})
        .queue_result(result_dict())
    )
    result = run_game(game, CONTEXT, lambda s, c: RESIGN, sleep=no_sleep)

    assert result.is_terminal is True
    assert game.send_message_calls == [
        {"message_type": "terminate", "content": None, "recipients": None}
    ]


def test_default_choose_message_auto_terminates_for_object_without_it():
    class Agent:
        def choose_action(self, state, context):
            return state.legal_actions[0]

    game = (
        FakeMCPGameSession()
        .queue_state(messaging_state(), terminal_state())
        .queue_send_message({"accepted": True, "phase": "moving"})
        .queue_result(result_dict())
    )
    result = run_game(game, CONTEXT, Agent(), sleep=no_sleep)

    assert result.is_terminal is True
    assert game.send_message_calls[0]["message_type"] == "terminate"


def test_custom_choose_message_sends_chat():
    class Agent:
        def choose_action(self, state, context):
            return state.legal_actions[0]

        def choose_message(self, state, context):
            return SendMessage("let's cooperate", recipients=[1])

    game = (
        FakeMCPGameSession()
        # The wait's result is terminal, which ends the loop after one round.
        .queue_state(messaging_state(state_version=2), terminal_state())
        .queue_send_message(
            {"accepted": True, "phase": "messaging", "message": {"seq": 11, "type": "chat"}}
        )
        .queue_result(result_dict())
    )
    run_game(game, CONTEXT, Agent(), sleep=no_sleep)

    assert game.send_message_calls == [
        {"message_type": "chat", "content": "let's cooperate", "recipients": [1]}
    ]
    # Phase stayed "messaging" -> waits (past this agent's own message)
    # instead of busy-polling.
    assert game.wait_calls == [
        {
            "since_version": 2,
            "since_message_seq": 11,
            "since_is_current_actor": True,
            "since_phase": "messaging",
            "timeout_seconds": 20.0,
        }
    ]


def test_custom_choose_message_can_terminate_explicitly():
    class Agent:
        def choose_action(self, state, context):
            return state.legal_actions[0]

        def choose_message(self, state, context):
            return TERMINATE_MESSAGING

    game = (
        FakeMCPGameSession()
        .queue_state(messaging_state(), terminal_state())
        .queue_send_message({"accepted": True, "phase": "moving"})
        .queue_result(result_dict())
    )
    result = run_game(game, CONTEXT, Agent(), sleep=no_sleep)

    assert result.is_terminal is True
    assert game.send_message_calls[0]["message_type"] == "terminate"


def test_choose_message_resolved_off_original_object_not_bound_method():
    # Regression guard: choose_message must be looked up on the object
    # create_agent() returned, not on the bound choose_action method (bound
    # methods don't proxy attribute lookups back to their owning instance).
    class Agent:
        def __init__(self):
            self.messages_sent = 0

        def choose_action(self, state, context):
            return state.legal_actions[0]

        def choose_message(self, state, context):
            self.messages_sent += 1
            return TERMINATE_MESSAGING

    agent = Agent()
    game = (
        FakeMCPGameSession()
        .queue_state(messaging_state(), terminal_state())
        .queue_send_message({"accepted": True, "phase": "moving"})
        .queue_result(result_dict())
    )
    run_game(game, CONTEXT, agent, sleep=no_sleep)

    assert agent.messages_sent == 1


def test_repeated_messaging_rounds_across_the_match():
    # repeated_pd reopens messaging after every round — the loop must
    # handle this more than once.
    class Agent:
        def __init__(self):
            self.rounds_seen = 0

        def choose_action(self, state, context):
            return state.legal_actions[0]

        def choose_message(self, state, context):
            self.rounds_seen += 1
            return TERMINATE_MESSAGING

    agent = Agent()
    game = (
        FakeMCPGameSession()
        .queue_state(messaging_state(), make_mcp_state(), messaging_state(), terminal_state())
        .queue_legal_actions(int_actions(0, 1))
        .queue_play_action(play_action_result())
        .queue_send_message(
            {"accepted": True, "phase": "moving"}, {"accepted": True, "phase": "moving"}
        )
        .queue_result(result_dict())
    )
    result = run_game(game, CONTEXT, agent, sleep=no_sleep)

    assert result.is_terminal is True
    assert agent.rounds_seen == 2
    assert len(game.send_message_calls) == 2
    assert len(game.play_action_calls) == 1


def test_messaging_still_open_after_terminate_waits_instead_of_busy_polling():
    # This agent already terminated but others haven't — send_message's own
    # idempotent no-op still reports phase="messaging", so the runner must
    # wait rather than hammer choose_message/send_message.
    game = (
        FakeMCPGameSession()
        .queue_state(messaging_state(), terminal_state())
        .queue_send_message({"accepted": True, "phase": "messaging"})
        .queue_result(result_dict())
    )
    result = run_game(game, CONTEXT, lambda s, c: RESIGN, sleep=no_sleep)

    assert result.is_terminal is True
    assert len(game.wait_calls) == 1
    assert len(game.send_message_calls) == 1


def test_messaging_closed_by_send_refetches_immediately_without_waiting():
    game = (
        FakeMCPGameSession()
        .queue_state(messaging_state(), terminal_state())
        .queue_send_message({"accepted": True, "phase": "moving"})
        .queue_result(result_dict())
    )
    run_game(game, CONTEXT, lambda s, c: RESIGN, sleep=no_sleep)

    assert game.wait_calls == []


def test_invalid_choose_message_return_value_rejected():
    class Agent:
        def choose_action(self, state, context):
            return state.legal_actions[0]

        def choose_message(self, state, context):
            return "just chat, no wrapper"

    game = FakeMCPGameSession().queue_state(messaging_state())

    with pytest.raises(DecisionError):
        run_game(game, CONTEXT, Agent(), sleep=no_sleep)

    assert game.send_message_calls == []


def test_choose_message_exception_chains_into_decision_error():
    class Agent:
        def choose_action(self, state, context):
            return state.legal_actions[0]

        def choose_message(self, state, context):
            raise ValueError("boom")

    game = FakeMCPGameSession().queue_state(messaging_state())

    with pytest.raises(DecisionError) as exc_info:
        run_game(game, CONTEXT, Agent(), sleep=no_sleep)

    assert isinstance(exc_info.value.__cause__, ValueError)


def test_messaging_quota_exceeded_becomes_decision_error():
    class Agent:
        def choose_action(self, state, context):
            return state.legal_actions[0]

        def choose_message(self, state, context):
            return SendMessage("one too many")

    game = (
        FakeMCPGameSession()
        .queue_state(messaging_state())
        .queue_send_message(MCPToolError("quota", status_code=None, error_code="MESSAGES_QUOTA_EXCEEDED"))
    )

    with pytest.raises(DecisionError):
        run_game(game, CONTEXT, Agent(), sleep=no_sleep)


def test_invalid_recipients_becomes_decision_error():
    class Agent:
        def choose_action(self, state, context):
            return state.legal_actions[0]

        def choose_message(self, state, context):
            return SendMessage("hi", recipients=[0, 1])

    game = (
        FakeMCPGameSession()
        .queue_state(messaging_state())
        .queue_send_message(MCPToolError("bad recipients", status_code=None, error_code="INVALID_RECIPIENTS"))
    )

    with pytest.raises(DecisionError):
        run_game(game, CONTEXT, Agent(), sleep=no_sleep)


def test_messaging_runtime_unavailable_becomes_unsupported_game_flow_error():
    class Agent:
        def choose_action(self, state, context):
            return state.legal_actions[0]

        def choose_message(self, state, context):
            return TERMINATE_MESSAGING

    game = (
        FakeMCPGameSession()
        .queue_state(messaging_state())
        .queue_send_message(MCPToolError("unsupported", status_code=None, error_code="RUNTIME_UNAVAILABLE"))
    )

    with pytest.raises(UnsupportedGameFlowError):
        run_game(game, CONTEXT, Agent(), sleep=no_sleep)


def test_non_messaging_game_never_touches_messaging_transport():
    # Regression guard: a normal move-only game must never call send_message
    # even if choose_message is defined.
    class Agent:
        def choose_action(self, state, context):
            return state.legal_actions[0]

        def choose_message(self, state, context):
            raise AssertionError("choose_message should never run for this game")

    game = (
        FakeMCPGameSession()
        .queue_state(make_mcp_state(), terminal_state())
        .queue_legal_actions(int_actions(0, 1))
        .queue_play_action(play_action_result())
        .queue_result(result_dict())
    )
    result = run_game(game, CONTEXT, Agent(), sleep=no_sleep)

    assert result.is_terminal is True
    assert game.send_message_calls == []


def test_structured_game_phase_never_equals_messaging_marker():
    # Pokemon-shaped: phase is "draft"/"teambuild"/"moving", never
    # "messaging" — the generic runner treats any non-"messaging" phase the
    # same way (enumerate legal actions, invoke choose_action), with zero
    # adapter-specific code.
    def choose_action(state, context):
        raise AssertionError("choose_message-triggering path must not run")

    game = (
        FakeMCPGameSession()
        .queue_state(make_mcp_state(phase="draft"), terminal_state())
        .queue_legal_actions(structured_actions("draft_pick:card-7"))
        .queue_play_action(play_action_result())
        .queue_result(result_dict())
    )
    result = run_game(game, CONTEXT, lambda s, c: s.legal_actions[0], sleep=no_sleep)

    assert result.is_terminal is True
    assert game.play_action_calls == [{"action_id": "draft_pick:card-7", "action": None, "state_version": 0}]


# -- state_version ------------------------------------------------------


def test_state_version_from_legal_actions_used_in_play_action_not_stale_get_state():
    game = (
        FakeMCPGameSession()
        .queue_state(make_mcp_state(state_version=1), terminal_state())
        .queue_legal_actions(int_actions(0, 1, state_version=9))
        .queue_play_action(play_action_result(state_version=10))
        .queue_result(result_dict())
    )
    run_game(game, CONTEXT, lambda s, c: s.legal_actions[0], sleep=no_sleep)

    assert game.play_action_calls == [{"action_id": "0", "action": None, "state_version": 9}]


def test_stale_state_refetches_and_does_not_replay_the_decision_blindly():
    calls = []

    def choose_action(state, context):
        calls.append(state.state_version)
        return state.legal_actions[0]

    game = (
        FakeMCPGameSession()
        .queue_state(make_mcp_state(), make_mcp_state(state_version=5), terminal_state())
        .queue_legal_actions(int_actions(0, 1, state_version=0), int_actions(0, 1, state_version=5))
        .queue_play_action(
            MCPToolError("stale", status_code=None, error_code="STALE_STATE"), play_action_result()
        )
        .queue_result(result_dict())
    )
    result = run_game(game, CONTEXT, choose_action, sleep=no_sleep)

    assert result.is_terminal is True
    assert calls == [0, 5]  # re-decided against fresh state, not replayed
    assert len(game.play_action_calls) == 2


def test_not_your_turn_race_refetches_state_instead_of_blaming_contestant():
    calls = []

    def choose_action(state, context):
        calls.append(state)
        return state.legal_actions[0]

    game = (
        FakeMCPGameSession()
        .queue_state(make_mcp_state(), terminal_state())
        .queue_legal_actions(int_actions(0, 1))
        .queue_play_action(MCPToolError("stale read", status_code=None, error_code="NOT_YOUR_TURN"))
        .queue_result(result_dict())
    )
    result = run_game(game, CONTEXT, choose_action, sleep=no_sleep)

    assert result.is_terminal is True
    assert len(calls) == 1  # not re-invoked — this was never a contestant bug


def test_game_already_complete_race_treated_as_natural_completion():
    game = (
        FakeMCPGameSession()
        .queue_state(make_mcp_state(), terminal_state(termination_reason="completed"))
        .queue_legal_actions(int_actions(0, 1))
        .queue_play_action(MCPToolError("done", status_code=None, error_code="GAME_ALREADY_COMPLETE"))
        .queue_result(result_dict(termination_reason="completed"))
    )
    result = run_game(game, CONTEXT, lambda s, c: s.legal_actions[0], sleep=no_sleep)

    assert result.is_terminal is True
    assert result.termination_reason == "completed"


def test_action_runtime_unavailable_becomes_unsupported_game_flow_error():
    game = (
        FakeMCPGameSession()
        .queue_state(make_mcp_state())
        .queue_legal_actions(int_actions(0, 1))
        .queue_play_action(MCPToolError("unsupported", status_code=None, error_code="RUNTIME_UNAVAILABLE"))
    )

    with pytest.raises(UnsupportedGameFlowError):
        run_game(game, CONTEXT, lambda s, c: s.legal_actions[0], sleep=no_sleep)


def test_invalid_action_error_becomes_decision_error():
    game = (
        FakeMCPGameSession()
        .queue_state(make_mcp_state())
        .queue_legal_actions(int_actions(0, 1))
        .queue_play_action(MCPToolError("bad action", status_code=None, error_code="INVALID_ACTION"))
    )

    with pytest.raises(DecisionError):
        run_game(game, CONTEXT, lambda s, c: s.legal_actions[0], sleep=no_sleep)


def test_unknown_error_code_propagates_as_mcp_tool_error():
    game = (
        FakeMCPGameSession()
        .queue_state(make_mcp_state())
        .queue_legal_actions(int_actions(0, 1))
        .queue_play_action(MCPToolError("weird", status_code=None, error_code="SOME_OTHER_ERROR"))
    )

    with pytest.raises(MCPToolError) as exc_info:
        run_game(game, CONTEXT, lambda s, c: s.legal_actions[0], sleep=no_sleep)

    assert exc_info.value.error_code == "SOME_OTHER_ERROR"


# -- errors -----------------------------------------------------------------


def test_contestant_exception_chains_into_decision_error():
    def choose_action(state, context):
        raise ValueError("boom")

    game = FakeMCPGameSession().queue_state(make_mcp_state()).queue_legal_actions(int_actions(0, 1))

    with pytest.raises(DecisionError) as exc_info:
        run_game(game, CONTEXT, choose_action, sleep=no_sleep)

    assert isinstance(exc_info.value.__cause__, ValueError)
    assert "boom" in str(exc_info.value.__cause__)


# -- context ------------------------------------------------------------


def test_context_is_passed_through_to_decision_fn():
    received = []

    def choose_action(state, context):
        received.append(context)
        return state.legal_actions[0]

    context = DecisionContext(
        session_id="session-42", tournament_id="tournament-7", game_type="tic_tac_toe", agent_id="agent-99"
    )
    game = (
        FakeMCPGameSession(session_id="session-42")
        .queue_state(make_mcp_state(session_id="session-42"), terminal_state())
        .queue_legal_actions(int_actions(0, 1))
        .queue_play_action(play_action_result())
        .queue_result(result_dict())
    )
    run_game(game, context, choose_action, sleep=no_sleep)

    assert len(received) == 1
    assert received[0].session_id == "session-42"
    assert received[0].tournament_id == "tournament-7"
    assert received[0].game_type == "tic_tac_toe"
    assert received[0].agent_id == "agent-99"


def test_run_match_builds_context_from_match_and_delegates_to_game():
    match = Match.from_dict(
        {
            "session_id": "session-77",
            "status": "in_progress",
            "tournament_id": "tournament-3",
            "game_type": "tic_tac_toe",
        }
    )
    fake_game = (
        FakeMCPGameSession(session_id="session-77")
        .queue_state(make_mcp_state(session_id="session-77"), terminal_state())
        .queue_legal_actions(int_actions(0, 1))
        .queue_play_action(play_action_result())
        .queue_result(result_dict())
    )
    match.game = lambda: fake_game  # bypass real resolution; tested in test_sessions.py

    received = []

    def choose_action(state, context):
        received.append(context)
        return state.legal_actions[0]

    result = run_match(match, "agent-1", choose_action, sleep=no_sleep)

    assert result.is_terminal is True
    assert received[0].session_id == "session-77"
    assert received[0].tournament_id == "tournament-3"
    assert received[0].game_type == "tic_tac_toe"
    assert received[0].agent_id == "agent-1"


def test_run_match_never_constructs_a_rest_gamesession():
    # Proves no hidden REST fallback at the run_match level: match.game()
    # (not match.rest_game()) is the only thing run_match calls.
    match = Match.from_dict({"session_id": "s-1", "status": "in_progress", "game_type": "tic_tac_toe"})
    fake_game = (
        FakeMCPGameSession(session_id="s-1")
        .queue_state(make_mcp_state(session_id="s-1"), terminal_state())
        .queue_legal_actions(int_actions(0, 1))
        .queue_play_action(play_action_result())
        .queue_result(result_dict())
    )
    game_calls = []
    match.game = lambda: (game_calls.append(1) or fake_game)

    def rest_game_should_not_be_called():
        raise AssertionError("run_match must never call Match.rest_game()")

    match.rest_game = rest_game_should_not_be_called

    run_match(match, "agent-1", lambda s, c: s.legal_actions[0], sleep=no_sleep)

    assert game_calls == [1]


# -- current MCP payloads: embedded legal_actions, post-move state ------------


def embedded(state_version: int, *ints) -> dict:
    return int_actions(*ints, state_version=state_version)


def test_embedded_legal_actions_used_without_get_legal_actions_call():
    game = (
        FakeMCPGameSession()
        .queue_state(make_mcp_state(state_version=3, legal_actions=embedded(3, 0, 1)), terminal_state())
        .queue_play_action(play_action_result())
        .queue_result(result_dict())
    )
    run_game(game, CONTEXT, lambda s, c: s.legal_actions[1], sleep=no_sleep)

    assert game.get_legal_actions_calls == 0
    assert game.play_action_calls == [{"action_id": "1", "action": None, "state_version": 3}]


def test_play_action_post_move_state_used_without_refetch():
    post_move = {
        "session_id": SESSION_ID,
        "state_version": 4,
        "phase": "moving",
        "is_current_actor": True,
        "is_terminal": False,
        "legal_actions": embedded(4, 7),
    }
    result = dict(play_action_result(state_version=4), state=post_move)
    game = (
        FakeMCPGameSession()
        .queue_state(make_mcp_state(state_version=3, legal_actions=embedded(3, 0)), terminal_state())
        .queue_play_action(result, play_action_result(status="completed"))
        .queue_result(result_dict())
    )
    run_game(game, CONTEXT, lambda s, c: s.legal_actions[0], sleep=no_sleep)

    # Second move came straight from the first play_action's `state`.
    assert game.play_action_calls == [
        {"action_id": "0", "action": None, "state_version": 3},
        {"action_id": "7", "action": None, "state_version": 4},
    ]
    # Initial read + one after the final (completed, state-less) move.
    assert game.get_state_calls == 2


def test_eliminated_agent_never_acts_or_chats_and_waits_for_the_end():
    def never(state, context):
        raise AssertionError("an eliminated agent must not be asked to decide")

    class Agent:
        choose_action = staticmethod(never)
        choose_message = staticmethod(never)

    game = (
        FakeMCPGameSession()
        .queue_state(messaging_state(eliminated=True, is_current_actor=False), terminal_state())
        .queue_result(result_dict())
    )
    result = run_game(game, CONTEXT, Agent(), sleep=no_sleep)

    assert result.is_terminal is True
    assert game.send_message_calls == []
    assert len(game.wait_calls) == 1


def test_player_eliminated_race_refetches_instead_of_crashing():
    game = (
        FakeMCPGameSession()
        .queue_state(make_mcp_state(legal_actions=embedded(0, 0)), terminal_state())
        .queue_play_action(
            MCPToolError("eliminated", status_code=None, error_code="PLAYER_ELIMINATED")
        )
        .queue_result(result_dict())
    )
    result = run_game(game, CONTEXT, lambda s, c: s.legal_actions[0], sleep=no_sleep)

    assert result.is_terminal is True
