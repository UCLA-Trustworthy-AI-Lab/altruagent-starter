"""Single-match execution primitive.

Owns exactly one match's play loop: fetch state, decide (via contestant-
supplied logic) whether a decision is needed right now, submit it, repeat
until the match ends. This is deliberately small — no session discovery, no
polling/backoff engine, no concurrency. Those are later milestones.

The contestant contract is two parameters and a return value, nothing more:

    def choose_action(state: GameState, context: DecisionContext) -> int:
        return state.legal_actions[0]

No base class, no decorator, no registration. `run_match`/`run_game` also
accept an object exposing a `.choose_action(state, context)` method instead
of a plain function, resolved with one `callable()` check and one
`getattr()` — no signature/arity introspection.

Turn detection is driven entirely by `GameState.next_actions`, never by
`legal_actions`/`current_player` alone — verified against Agent_ACP's
`gameapi/src/gameapi/domain/next_actions.py`: `legal_actions` can be
populated even during a MESSAGING phase where `/step` is blocked, so only
`next_actions` correctly reflects whether a move is actually valid right now.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Protocol, Union

from .errors import PlatformError
from .game import GameSession
from .models import DecisionContext, GameState, Match

DEFAULT_WAIT_SECONDS = 5.0

_MESSAGING_ACTIONS = frozenset({"send_message", "terminate_messaging"})


class _Resign:
    """Unique sentinel type. Never instantiate another one — always use the
    exported ``RESIGN`` singleton and compare with ``is``.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "RESIGN"


RESIGN = _Resign()

Decision = Union[int, _Resign]


class ChoosesAction(Protocol):
    """Structural type for an object-based decision handler: anything with a
    ``choose_action(state, context)`` method. Not used for runtime
    isinstance checks (see ``_resolve_decision_fn``) — this exists only to
    give type checkers something to check plain-function agents against.
    """

    def choose_action(self, state: GameState, context: DecisionContext) -> Decision: ...


DecisionFn = Callable[[GameState, DecisionContext], Decision]


class RunnerError(Exception):
    """Base class for all altruagent.runner errors."""


class DecisionError(RunnerError):
    """The contestant decision function misbehaved: it raised, returned a
    type other than int/RESIGN, or returned an action not present in
    ``state.legal_actions``. Fails fast, on purpose — a deterministic
    contestant bug should be visible immediately during local development,
    not silently retried on the next cycle.
    """


class UnsupportedGameFlowError(RunnerError):
    """The match's `next_actions` call for a flow this runner doesn't drive
    yet — a messaging phase, or a next_actions kind it doesn't recognize.
    Not a contestant bug: the match may be real and legitimate, this runner
    just doesn't know how to play it (yet).
    """


def _resolve_decision_fn(choose_action: Any) -> DecisionFn:
    """Accept either a plain callable, or an object exposing a callable
    ``choose_action`` attribute. Two simple checks, no reflection.
    """
    if callable(choose_action):
        return choose_action
    method = getattr(choose_action, "choose_action", None)
    if callable(method):
        return method
    raise DecisionError(
        f"{choose_action!r} is not callable and has no callable 'choose_action' "
        "method. Expected a function like choose_action(state, context), or an "
        "object exposing one."
    )


def _invoke_decision(decision_fn: DecisionFn, state: GameState, context: DecisionContext) -> Any:
    try:
        return decision_fn(state, context)
    except Exception as exc:
        raise DecisionError(
            f"choose_action raised {exc!r} for session {context.session_id!r}."
        ) from exc


def _validate_decision(decision: Any, state: GameState) -> Decision:
    if decision is RESIGN:
        return RESIGN
    # bool is a subclass of int in Python (isinstance(True, int) is True) —
    # reject it explicitly so a stray True/False is never mistaken for a
    # legal action index.
    if isinstance(decision, bool) or not isinstance(decision, int):
        raise DecisionError(
            "choose_action must return an int from state.legal_actions, or "
            f"altruagent.RESIGN — got {decision!r} ({type(decision).__name__})."
        )
    if decision not in state.legal_actions:
        raise DecisionError(
            f"choose_action returned {decision!r}, which is not in "
            f"state.legal_actions={state.legal_actions!r}."
        )
    return decision


def run_game(
    game: GameSession,
    context: DecisionContext,
    choose_action: DecisionFn,
    *,
    wait_seconds: float = DEFAULT_WAIT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> GameState:
    """Play one already-open ``GameSession`` to completion.

    Loop, once per iteration:

    1. Fetch state.
    2. If terminal, return it.
    3. If ``next_actions`` says ``make_move``: invoke ``choose_action``,
       validate the result, and submit it (``step`` for an int, ``resign``
       for ``RESIGN``).
    4. If ``next_actions`` says only ``wait_for_opponent``: sleep
       ``wait_seconds`` and refetch.
    5. If ``next_actions`` indicates a messaging phase, or an unrecognized
       kind: raise ``UnsupportedGameFlowError`` — this runner doesn't guess.

    A server-reported ``not_your_turn`` on the submit (a genuine race, not a
    contestant bug) refetches state and continues; ``game_already_finished``
    is treated as natural completion. Any other ``PlatformError``, or a
    contestant decision error, propagates immediately — no retrying.

    Returns the final ``GameState`` once the match is terminal.
    """
    decision_fn = _resolve_decision_fn(choose_action)
    state = game.state()

    while True:
        if state.is_terminal:
            return state

        action_kinds = {a.action for a in state.next_actions}

        if "make_move" in action_kinds:
            decision = _validate_decision(
                _invoke_decision(decision_fn, state, context), state
            )
            try:
                if decision is RESIGN:
                    return game.resign()
                # step() already returns the fresh post-move state, so we
                # reuse it directly below rather than re-fetching.
                state = game.step(decision)
            except PlatformError as exc:
                if exc.error_code == "not_your_turn":
                    # Stale read — the state we decided from is no longer
                    # current. Not a contestant bug; refetch and re-evaluate.
                    state = game.state()
                    continue
                if exc.error_code == "game_already_finished":
                    # The match ended between our last read and this submit
                    # (e.g. the opponent resigned, or a server-side timeout
                    # auto-completed it). Natural completion, not an error.
                    return game.state()
                raise
            continue

        if "wait_for_opponent" in action_kinds:
            sleep(wait_seconds)
            state = game.state()
            continue

        if action_kinds & _MESSAGING_ACTIONS:
            raise UnsupportedGameFlowError(
                f"Match {context.session_id!r} requires messaging "
                f"(next_actions={sorted(action_kinds)}), which this runner "
                "does not support yet."
            )

        raise UnsupportedGameFlowError(
            f"Match {context.session_id!r} has unrecognized next_actions "
            f"{sorted(action_kinds)!r} — this runner doesn't know how to proceed."
        )


def run_match(
    match: Match,
    agent_id: str,
    choose_action: DecisionFn,
    *,
    wait_seconds: float = DEFAULT_WAIT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> GameState:
    """Play one ``Match`` (as returned by ``AltruAgentClient.sessions()``)
    to completion, using ``choose_action`` for every decision.

    A thin convenience wrapper around ``run_game``: opens the ``GameSession``
    via ``match.game()`` — reusing its existing lazy ``game_server_url``
    resolution and caching rather than duplicating that logic here — and
    builds the ``DecisionContext`` from the ``Match``'s own fields plus the
    caller-supplied ``agent_id`` (a ``Match`` has no notion of "which agent
    is playing it").
    """
    game = match.game()
    context = DecisionContext(
        session_id=match.session_id,
        tournament_id=match.tournament_id,
        game_type=match.game_type,
        agent_id=agent_id,
    )
    return run_game(game, context, choose_action, wait_seconds=wait_seconds, sleep=sleep)
