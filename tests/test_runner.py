"""Unit tests for altruagent.runner (run_game / run_match).

All gameplay is driven through a lightweight FakeGameSession test double —
run_game only needs an object with state()/step()/resign(), so there is no
need to mock HTTP for these; GameSession's own transport is already covered
by tests/test_game.py.
"""

from __future__ import annotations

import pytest

from altruagent.errors import PlatformError
from altruagent.models import DecisionContext, GameState, Match
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


class FakeGameSession:
    """Scripted stand-in for GameSession. Queue GameState objects (returned
    in order by state()/step()/resign()/send_message()/terminate_messaging())
    or exception instances (raised).
    """

    def __init__(self, session_id: str = SESSION_ID) -> None:
        self.session_id = session_id
        self.game_server_url = "http://fake:8000"
        self._queue: list = []
        self.state_calls = 0
        self.step_calls: list[int] = []
        self.resign_calls = 0
        self.send_message_calls: list[tuple[str, list[int]]] = []
        self.terminate_messaging_calls = 0

    def queue(self, *items) -> "FakeGameSession":
        self._queue.extend(items)
        return self

    def _next(self):
        item = self._queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def state(self) -> GameState:
        self.state_calls += 1
        return self._next()

    def step(self, action: int) -> GameState:
        self.step_calls.append(action)
        return self._next()

    def resign(self) -> GameState:
        self.resign_calls += 1
        return self._next()

    def send_message(self, content: str, recipients: list[int] | None = None) -> GameState:
        self.send_message_calls.append((content, list(recipients or [])))
        return self._next()

    def terminate_messaging(self) -> GameState:
        self.terminate_messaging_calls += 1
        return self._next()


def make_state(**overrides) -> GameState:
    payload = {
        "session_id": SESSION_ID,
        "game_name": "tic_tac_toe",
        "status": "active",
        "observation": "...",
        "current_player": {"name": "Me"},
        "legal_actions": [0, 1, 2],
        "legal_actions_str": ["a", "b", "c"],
        "is_terminal": False,
        "returns": None,
        "move_count": 0,
        "termination_reason": None,
        "messaging_enabled": False,
        "phase": "moving",
        "next_actions": [
            {"action": "make_move", "endpoint": "POST /step", "hint": "Go.", "required_fields": ["action"]}
        ],
    }
    payload.update(overrides)
    return GameState.from_dict(payload)


def waiting_state(**overrides) -> GameState:
    overrides.setdefault("legal_actions", [])
    overrides.setdefault(
        "next_actions", [{"action": "wait_for_opponent", "endpoint": "GET /games/x", "hint": "Wait."}]
    )
    return make_state(**overrides)


def terminal_state(**overrides) -> GameState:
    overrides.setdefault("is_terminal", True)
    overrides.setdefault("legal_actions", [])
    overrides.setdefault("current_player", None)
    overrides.setdefault("returns", {"Me": 1.0, "Them": -1.0})
    overrides.setdefault("next_actions", [{"action": "game_over", "hint": "Done."}])
    return make_state(**overrides)


def messaging_state(**overrides) -> GameState:
    """A MESSAGING-phase state where this viewer has not yet terminated —
    real GameAPI always pairs send_message + terminate_messaging together
    here (see next_actions.py's compute_next_actions).
    """
    overrides.setdefault("messaging_enabled", True)
    overrides.setdefault("phase", "messaging")
    overrides.setdefault(
        "next_actions",
        [
            {"action": "send_message", "hint": "chat or terminate", "required_fields": ["type"]},
            {"action": "terminate_messaging", "hint": "end round", "required_fields": ["type"]},
        ],
    )
    return make_state(**overrides)


CONTEXT = DecisionContext(
    session_id=SESSION_ID, tournament_id=None, game_type="tic_tac_toe", agent_id="agent-1"
)


def no_sleep(_seconds: float) -> None:
    pass


# -- decision contract ------------------------------------------------------


def test_plain_callable_decision_works():
    game = FakeGameSession().queue(make_state(), terminal_state())
    result = run_game(game, CONTEXT, lambda state, ctx: state.legal_actions[0], sleep=no_sleep)

    assert result.is_terminal is True
    assert game.step_calls == [0]


def test_object_with_choose_action_method_works():
    class Agent:
        def choose_action(self, state, context):
            return state.legal_actions[-1]

    game = FakeGameSession().queue(make_state(), terminal_state())
    result = run_game(game, CONTEXT, Agent(), sleep=no_sleep)

    assert result.is_terminal is True
    assert game.step_calls == [2]


def test_invalid_decision_object_fails_clearly():
    game = FakeGameSession()  # empty queue: state() must never be called

    with pytest.raises(DecisionError):
        run_game(game, CONTEXT, 42, sleep=no_sleep)  # not callable, no .choose_action

    assert game.state_calls == 0


# -- flow ---------------------------------------------------------------


def test_make_move_invokes_decision_exactly_once_for_that_state():
    calls = []

    def choose_action(state, context):
        calls.append(state)
        return state.legal_actions[0]

    game = FakeGameSession().queue(make_state(), terminal_state())
    run_game(game, CONTEXT, choose_action, sleep=no_sleep)

    assert len(calls) == 1


def test_wait_for_opponent_does_not_invoke_decision():
    def choose_action(state, context):
        raise AssertionError("choose_action should not be called while waiting")

    sleep_calls = []
    game = FakeGameSession().queue(waiting_state(), terminal_state())
    result = run_game(game, CONTEXT, choose_action, sleep=sleep_calls.append)

    assert result.is_terminal is True
    assert sleep_calls == [5.0]  # DEFAULT_WAIT_SECONDS
    assert game.step_calls == []


def test_terminal_state_exits_cleanly_without_any_decision():
    def choose_action(state, context):
        raise AssertionError("choose_action should not be called on a terminal state")

    game = FakeGameSession().queue(terminal_state())
    result = run_game(game, CONTEXT, choose_action, sleep=no_sleep)

    assert result.is_terminal is True
    assert game.step_calls == []


def test_repeated_wait_then_make_move_transition():
    calls = []

    def choose_action(state, context):
        calls.append(state)
        return state.legal_actions[0]

    sleep_calls = []
    game = FakeGameSession().queue(
        waiting_state(), waiting_state(), make_state(), terminal_state()
    )
    result = run_game(game, CONTEXT, choose_action, sleep=sleep_calls.append)

    assert result.is_terminal is True
    assert len(sleep_calls) == 2
    assert len(calls) == 1


def test_make_move_to_terminal_in_one_step():
    game = FakeGameSession().queue(make_state(), terminal_state())
    result = run_game(game, CONTEXT, lambda s, c: s.legal_actions[0], sleep=no_sleep)

    assert result.is_terminal is True
    assert result.returns == {"Me": 1.0, "Them": -1.0}


# -- validation -----------------------------------------------------------


def test_legal_int_accepted():
    game = FakeGameSession().queue(make_state(legal_actions=[0, 1]), terminal_state())
    result = run_game(game, CONTEXT, lambda s, c: 1, sleep=no_sleep)

    assert result.is_terminal is True
    assert game.step_calls == [1]


def test_non_int_decision_rejected():
    game = FakeGameSession().queue(make_state())

    with pytest.raises(DecisionError):
        run_game(game, CONTEXT, lambda s, c: "cooperate", sleep=no_sleep)

    assert game.step_calls == []


def test_illegal_int_decision_rejected():
    game = FakeGameSession().queue(make_state(legal_actions=[0, 1]))

    with pytest.raises(DecisionError):
        run_game(game, CONTEXT, lambda s, c: 99, sleep=no_sleep)

    assert game.step_calls == []


def test_bool_decision_rejected_even_though_it_is_a_legal_action_value():
    # isinstance(True, int) is True in Python, and True == 1 — without an
    # explicit bool guard, returning True would silently be treated as
    # legal action 1. It must not be.
    game = FakeGameSession().queue(make_state(legal_actions=[0, 1]))

    with pytest.raises(DecisionError):
        run_game(game, CONTEXT, lambda s, c: True, sleep=no_sleep)

    assert game.step_calls == []


# -- resign -----------------------------------------------------------------


def test_resign_calls_resign_not_step():
    game = FakeGameSession().queue(make_state(), terminal_state(termination_reason="resignation"))
    result = run_game(game, CONTEXT, lambda s, c: RESIGN, sleep=no_sleep)

    assert result.is_terminal is True
    assert game.resign_calls == 1
    assert game.step_calls == []


# -- messaging ----------------------------------------------------------


def test_messaging_phase_never_invokes_choose_action():
    # legal_actions non-empty on purpose: proves the runner defers to
    # next_actions, not legal_actions, to decide whether to act.
    def choose_action(state, context):
        raise AssertionError("choose_action must not run during a messaging phase")

    game = FakeGameSession().queue(
        messaging_state(legal_actions=[0, 1]),
        terminal_state(),
    )
    result = run_game(game, CONTEXT, choose_action, sleep=no_sleep)

    assert result.is_terminal is True
    assert game.step_calls == []


def test_default_choose_message_auto_terminates_when_absent():
    # No choose_message defined anywhere (bare function choose_action) — the
    # existing simple starter agent must still be able to finish a
    # messaging-enabled match without any new code.
    game = FakeGameSession().queue(
        messaging_state(),
        terminal_state(),
    )
    result = run_game(game, CONTEXT, lambda s, c: RESIGN, sleep=no_sleep)

    assert result.is_terminal is True
    assert game.terminate_messaging_calls == 1
    assert game.send_message_calls == []


def test_default_choose_message_auto_terminates_for_object_without_it():
    class Agent:
        def choose_action(self, state, context):
            return state.legal_actions[0]

    game = FakeGameSession().queue(messaging_state(), terminal_state())
    result = run_game(game, CONTEXT, Agent(), sleep=no_sleep)

    assert result.is_terminal is True
    assert game.terminate_messaging_calls == 1


def test_custom_choose_message_sends_chat():
    class Agent:
        def choose_action(self, state, context):
            return state.legal_actions[0]

        def choose_message(self, state, context):
            return SendMessage("let's cooperate", recipients=[1])

    game = FakeGameSession().queue(messaging_state(), terminal_state())
    result = run_game(game, CONTEXT, Agent(), sleep=no_sleep)

    assert result.is_terminal is True
    assert game.send_message_calls == [("let's cooperate", [1])]
    assert game.terminate_messaging_calls == 0


def test_custom_choose_message_can_terminate_explicitly():
    class Agent:
        def choose_action(self, state, context):
            return state.legal_actions[0]

        def choose_message(self, state, context):
            return TERMINATE_MESSAGING

    game = FakeGameSession().queue(messaging_state(), terminal_state())
    result = run_game(game, CONTEXT, Agent(), sleep=no_sleep)

    assert result.is_terminal is True
    assert game.terminate_messaging_calls == 1


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
    game = FakeGameSession().queue(messaging_state(), terminal_state())
    run_game(game, CONTEXT, agent, sleep=no_sleep)

    assert agent.messages_sent == 1


def test_repeated_messaging_rounds_across_the_match():
    # repeated_pd reopens messaging after every round (messaging_mode
    # per_all_moves) — the loop must handle this more than once.
    class Agent:
        def __init__(self):
            self.rounds_seen = 0

        def choose_action(self, state, context):
            return state.legal_actions[0]

        def choose_message(self, state, context):
            self.rounds_seen += 1
            return TERMINATE_MESSAGING

    agent = Agent()
    game = FakeGameSession().queue(
        messaging_state(),
        make_state(),
        messaging_state(),
        terminal_state(),
    )
    result = run_game(game, CONTEXT, agent, sleep=no_sleep)

    assert result.is_terminal is True
    assert agent.rounds_seen == 2
    assert game.terminate_messaging_calls == 2
    assert game.step_calls == [0]


def test_already_terminated_messaging_round_waits_without_calling_choose_message():
    # This viewer already sent terminate this round: next_actions collapses
    # to wait_for_opponent alone (no send_message/terminate_messaging) — the
    # existing waiting branch handles this without any messaging-specific code.
    def choose_message_never(state, context):
        raise AssertionError("choose_message should not run while only waiting")

    class Agent:
        def choose_action(self, state, context):
            return state.legal_actions[0]

        def choose_message(self, state, context):
            return choose_message_never(state, context)

    sleep_calls = []
    game = FakeGameSession().queue(waiting_state(messaging_enabled=True, phase="messaging"), terminal_state())
    result = run_game(game, CONTEXT, Agent(), sleep=sleep_calls.append)

    assert result.is_terminal is True
    assert sleep_calls == [5.0]


def test_invalid_choose_message_return_value_rejected():
    class Agent:
        def choose_action(self, state, context):
            return state.legal_actions[0]

        def choose_message(self, state, context):
            return "just chat, no wrapper"

    game = FakeGameSession().queue(messaging_state())

    with pytest.raises(DecisionError):
        run_game(game, CONTEXT, Agent(), sleep=no_sleep)

    assert game.send_message_calls == []
    assert game.terminate_messaging_calls == 0


def test_choose_message_exception_chains_into_decision_error():
    class Agent:
        def choose_action(self, state, context):
            return state.legal_actions[0]

        def choose_message(self, state, context):
            raise ValueError("boom")

    game = FakeGameSession().queue(messaging_state())

    with pytest.raises(DecisionError) as exc_info:
        run_game(game, CONTEXT, Agent(), sleep=no_sleep)

    assert isinstance(exc_info.value.__cause__, ValueError)


def test_messaging_quota_exceeded_becomes_decision_error():
    class Agent:
        def choose_action(self, state, context):
            return state.legal_actions[0]

        def choose_message(self, state, context):
            return SendMessage("one too many")

    game = FakeGameSession().queue(
        messaging_state(),
        PlatformError("quota", status_code=429, error_code="messages_quota_exceeded"),
    )

    with pytest.raises(DecisionError):
        run_game(game, CONTEXT, Agent(), sleep=no_sleep)


def test_invalid_recipients_becomes_decision_error():
    class Agent:
        def choose_action(self, state, context):
            return state.legal_actions[0]

        def choose_message(self, state, context):
            return SendMessage("hi", recipients=[0, 1])

    game = FakeGameSession().queue(
        messaging_state(),
        PlatformError("bad recipients", status_code=400, error_code="invalid_recipients"),
    )

    with pytest.raises(DecisionError):
        run_game(game, CONTEXT, Agent(), sleep=no_sleep)


def test_wrong_phase_race_refetches_instead_of_blaming_contestant():
    calls = []

    class Agent:
        def choose_action(self, state, context):
            return state.legal_actions[0]

        def choose_message(self, state, context):
            calls.append(state)
            return TERMINATE_MESSAGING

    game = FakeGameSession().queue(
        messaging_state(),
        PlatformError("phase moved on", status_code=409, error_code="wrong_phase"),
        terminal_state(),
    )
    result = run_game(game, CONTEXT, Agent(), sleep=no_sleep)

    assert result.is_terminal is True
    assert len(calls) == 1  # not re-invoked — this was never a contestant bug
    assert game.state_calls == 2  # initial + the wrong_phase refetch


def test_messaging_game_already_finished_treated_as_natural_completion():
    class Agent:
        def choose_action(self, state, context):
            return state.legal_actions[0]

        def choose_message(self, state, context):
            return TERMINATE_MESSAGING

    game = FakeGameSession().queue(
        messaging_state(),
        PlatformError("done", status_code=409, error_code="game_already_finished"),
        terminal_state(termination_reason="completed"),
    )
    result = run_game(game, CONTEXT, Agent(), sleep=no_sleep)

    assert result.is_terminal is True
    assert result.termination_reason == "completed"


def test_non_messaging_game_never_touches_messaging_transport():
    # Regression guard: a normal move-only game must never call
    # send_message/terminate_messaging even if choose_message is defined.
    class Agent:
        def choose_action(self, state, context):
            return state.legal_actions[0]

        def choose_message(self, state, context):
            raise AssertionError("choose_message should never run for this game")

    game = FakeGameSession().queue(make_state(), terminal_state())
    result = run_game(game, CONTEXT, Agent(), sleep=no_sleep)

    assert result.is_terminal is True
    assert game.send_message_calls == []
    assert game.terminate_messaging_calls == 0


def test_resign_still_works_when_choose_message_is_defined():
    class Agent:
        def choose_action(self, state, context):
            return RESIGN

        def choose_message(self, state, context):
            return TERMINATE_MESSAGING

    game = FakeGameSession().queue(make_state(), terminal_state(termination_reason="resignation"))
    result = run_game(game, CONTEXT, Agent(), sleep=no_sleep)

    assert result.is_terminal is True
    assert game.resign_calls == 1


def test_terminate_messaging_only_flow_fails_as_unsupported():
    # Real GameAPI always pairs send_message + terminate_messaging together
    # (see next_actions.py) — this checks the runner's detection logic
    # handles either member of the messaging set on its own, defensively.
    game = FakeGameSession().queue(
        make_state(
            messaging_enabled=True,
            phase="messaging",
            legal_actions=[0, 1],
            next_actions=[{"action": "terminate_messaging", "hint": "end round"}],
        )
    )

    with pytest.raises(UnsupportedGameFlowError):
        run_game(game, CONTEXT, lambda s, c: RESIGN, sleep=no_sleep)

    assert game.step_calls == []
    assert game.resign_calls == 0


def test_unknown_next_action_fails_clearly():
    game = FakeGameSession().queue(
        make_state(next_actions=[{"action": "some_future_action", "hint": "?"}])
    )

    with pytest.raises(UnsupportedGameFlowError):
        run_game(game, CONTEXT, lambda s, c: 0, sleep=no_sleep)

    assert game.step_calls == []


# -- errors -----------------------------------------------------------------


def test_contestant_exception_chains_into_decision_error():
    def choose_action(state, context):
        raise ValueError("boom")

    game = FakeGameSession().queue(make_state())

    with pytest.raises(DecisionError) as exc_info:
        run_game(game, CONTEXT, choose_action, sleep=no_sleep)

    assert isinstance(exc_info.value.__cause__, ValueError)
    assert "boom" in str(exc_info.value.__cause__)


def test_not_your_turn_refetches_state_instead_of_blaming_contestant():
    calls = []

    def choose_action(state, context):
        calls.append(state)
        return state.legal_actions[0]

    game = FakeGameSession().queue(
        make_state(),
        PlatformError("stale", status_code=409, error_code="not_your_turn"),
        terminal_state(),
    )
    result = run_game(game, CONTEXT, choose_action, sleep=no_sleep)

    assert result.is_terminal is True
    assert len(calls) == 1  # not re-invoked — this was never a contestant bug
    assert game.step_calls == [0]
    assert game.state_calls == 2  # initial + the not_your_turn refetch


def test_game_already_finished_treated_as_natural_completion():
    game = FakeGameSession().queue(
        make_state(),
        PlatformError("done", status_code=409, error_code="game_already_finished"),
        terminal_state(termination_reason="completed"),
    )
    result = run_game(game, CONTEXT, lambda s, c: s.legal_actions[0], sleep=no_sleep)

    assert result.is_terminal is True
    assert result.termination_reason == "completed"


def test_unknown_platform_error_propagates():
    game = FakeGameSession().queue(
        make_state(),
        PlatformError("weird", status_code=500, error_code="server_error"),
    )

    with pytest.raises(PlatformError) as exc_info:
        run_game(game, CONTEXT, lambda s, c: s.legal_actions[0], sleep=no_sleep)

    assert exc_info.value.error_code == "server_error"


# -- context ------------------------------------------------------------


def test_context_is_passed_through_to_decision_fn():
    received = []

    def choose_action(state, context):
        received.append(context)
        return state.legal_actions[0]

    context = DecisionContext(
        session_id="session-42",
        tournament_id="tournament-7",
        game_type="tic_tac_toe",
        agent_id="agent-99",
    )
    game = FakeGameSession(session_id="session-42").queue(make_state(session_id="session-42"), terminal_state())
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
    fake_game = FakeGameSession(session_id="session-77").queue(
        make_state(session_id="session-77"), terminal_state()
    )
    match.game = lambda: fake_game  # bypass real resolution; tested elsewhere

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
