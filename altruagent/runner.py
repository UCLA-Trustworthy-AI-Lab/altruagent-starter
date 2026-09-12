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

Messaging (Milestone 5) works the same way: `next_actions` reports
`send_message` (always paired with `terminate_messaging`, per
`next_actions.py`'s `compute_next_actions`) whenever this agent hasn't yet
terminated the current messaging round; once it has, `next_actions` reports
only `wait_for_opponent` until the round ends, which the existing waiting
branch already handles unchanged. `choose_message` is optional — a
contestant that only defines `choose_action` gets `TERMINATE_MESSAGING`
automatically every round (see `_default_choose_message`), so both launch
games (`repeated_pd`, `avalon` — both `messaging_enabled=True` by default,
confirmed against `backend/src/services/competitionPresets.ts`) can be
played move-only with zero new contestant code.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Protocol, Union

from .errors import PlatformError
from .game import GameSession
from .models import DecisionContext, GameState, Match

DEFAULT_WAIT_SECONDS = 5.0

# Contestant-caused messaging failures (bad content/recipients, over quota,
# or messaging attempted on a non-messaging game) — verified exact codes
# against Agent_ACP/gameapi/src/gameapi/domain/exceptions.py. All are fail-
# fast DecisionErrors, same philosophy as an illegal choose_action result.
# `wrong_phase` is deliberately NOT in this set — see the wrong_phase race
# handling in `run_game` below.
_MESSAGE_SCOPED_ERROR_CODES = frozenset(
    {
        "invalid_recipients",
        "message_too_long",
        "too_many_words",
        "invalid_content",
        "messages_quota_exceeded",
        "messaging_disabled",
    }
)


class _Resign:
    """Unique sentinel type. Never instantiate another one — always use the
    exported ``RESIGN`` singleton and compare with ``is``.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "RESIGN"


RESIGN = _Resign()

Decision = Union[int, _Resign]


class _TerminateMessaging:
    """Unique sentinel type, analogous to ``_Resign``. Never instantiate
    another one — always use the exported ``TERMINATE_MESSAGING`` singleton
    and compare with ``is``.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "TERMINATE_MESSAGING"


TERMINATE_MESSAGING = _TerminateMessaging()


class SendMessage:
    """Returned from an optional ``choose_message`` to send a chat message
    during a MESSAGING phase.

    ``recipients`` empty/``None`` broadcasts to every other player; a single
    player index sends a targeted p2p message. The server currently rejects
    2+ recipients (p2group is gated) — that surfaces as ``DecisionError``,
    same as any other contestant-caused messaging error (see
    ``_MESSAGE_SCOPED_ERROR_CODES``).
    """

    __slots__ = ("content", "recipients")

    def __init__(self, content: str, recipients: list[int] | None = None) -> None:
        self.content = content
        self.recipients = list(recipients) if recipients else []

    def __repr__(self) -> str:
        return f"SendMessage(content={self.content!r}, recipients={self.recipients!r})"


MessageDecision = Union[SendMessage, _TerminateMessaging]


class ChoosesAction(Protocol):
    """Structural type for an object-based decision handler: anything with a
    ``choose_action(state, context)`` method (and, optionally, a
    ``choose_message(state, context)`` method — see ``MessageDecisionFn``).
    Not used for runtime isinstance checks (see ``_resolve_decision_fn``) —
    this exists only to give type checkers something to check plain-function
    agents against.
    """

    def choose_action(self, state: GameState, context: DecisionContext) -> Decision: ...


DecisionFn = Callable[[GameState, DecisionContext], Decision]
MessageDecisionFn = Callable[[GameState, DecisionContext], MessageDecision]


class RunnerError(Exception):
    """Base class for all altruagent.runner errors."""


class DecisionError(RunnerError):
    """The contestant decision function misbehaved: it raised, returned a
    type other than int/RESIGN (or, for ``choose_message``, other than
    ``SendMessage``/``TERMINATE_MESSAGING``), returned an action not present
    in ``state.legal_actions``, or produced a messaging action the server
    rejected as invalid (bad recipients/content, word/length cap, or chat
    quota exceeded — see ``_MESSAGE_SCOPED_ERROR_CODES``). Fails fast, on
    purpose — a deterministic contestant bug should be visible immediately
    during local development, not silently retried on the next cycle.
    """


class UnsupportedGameFlowError(RunnerError):
    """The match's `next_actions` reports a kind this runner doesn't
    recognize at all. Not a contestant bug: the match may be real and
    legitimate, this runner just doesn't know how to play it (yet). Does
    *not* cover messaging — `send_message`/`terminate_messaging` are fully
    supported (see `choose_message`).
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


def _resolve_message_decision_fn(choose_action: Any) -> MessageDecisionFn:
    """Look up an optional ``choose_message(state, context)`` on the same
    object/function used for ``choose_action``.

    Unlike ``_resolve_decision_fn``, a missing ``choose_message`` is not an
    error — it's the normal case for a contestant that only cares about
    moves, and defaults to always terminating the messaging round
    immediately (see ``_default_choose_message`` and the module docstring's
    backwards-compatibility note). Must be resolved from the *original*
    ``choose_action`` value the caller passed in, not from
    ``_resolve_decision_fn``'s result — for an object-based agent, that
    result is a bound ``choose_action`` method, and a bound method does not
    proxy attribute lookups back to the object it came from.
    """
    method = getattr(choose_action, "choose_message", None)
    if callable(method):
        return method
    return _default_choose_message


def _default_choose_message(state: GameState, context: DecisionContext) -> MessageDecision:
    return TERMINATE_MESSAGING


def _invoke_message_decision(
    decision_fn: MessageDecisionFn, state: GameState, context: DecisionContext
) -> Any:
    try:
        return decision_fn(state, context)
    except Exception as exc:
        raise DecisionError(
            f"choose_message raised {exc!r} for session {context.session_id!r}."
        ) from exc


def _validate_message_decision(decision: Any) -> MessageDecision:
    if decision is TERMINATE_MESSAGING:
        return decision
    if not isinstance(decision, SendMessage):
        raise DecisionError(
            "choose_message must return an altruagent.SendMessage(...) or "
            f"altruagent.TERMINATE_MESSAGING — got {decision!r} "
            f"({type(decision).__name__})."
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
    3. If ``next_actions`` says ``send_message`` (always paired with
       ``terminate_messaging`` when present — see
       ``gameapi/domain/next_actions.py``'s ``compute_next_actions``):
       resolve an optional ``choose_message``, validate the result, and
       submit it (``send_message`` for ``SendMessage``, ``terminate_messaging``
       for ``TERMINATE_MESSAGING``). Checked before ``make_move`` so a
       messaging-phase state can never be mistaken for a move-phase one.
    4. If ``next_actions`` says ``make_move``: invoke ``choose_action``,
       validate the result, and submit it (``step`` for an int, ``resign``
       for ``RESIGN``).
    5. If ``next_actions`` says only ``wait_for_opponent`` (a move-phase
       opponent turn, *or* a messaging round this agent already terminated
       — both produce the identical single ``wait_for_opponent`` action):
       sleep ``wait_seconds`` and refetch.
    6. Any other/unrecognized ``next_actions`` kind: raise
       ``UnsupportedGameFlowError`` — this runner doesn't guess.

    A server-reported ``not_your_turn`` on a move submit, or ``wrong_phase``
    on a messaging submit (both genuine races — the state changed between
    our last read and this submit, not a contestant bug), refetch state and
    continue; ``game_already_finished`` is treated as natural completion.
    A messaging submit rejected for a contestant-caused reason (bad
    recipients/content, word/length cap, chat quota exceeded — see
    ``_MESSAGE_SCOPED_ERROR_CODES``) raises ``DecisionError``. Any other
    ``PlatformError``, or a contestant decision error, propagates
    immediately — no retrying.

    Returns the final ``GameState`` once the match is terminal.
    """
    decision_fn = _resolve_decision_fn(choose_action)
    message_decision_fn = _resolve_message_decision_fn(choose_action)
    state = game.state()

    while True:
        if state.is_terminal:
            return state

        action_kinds = {a.action for a in state.next_actions}

        if "send_message" in action_kinds:
            decision = _validate_message_decision(
                _invoke_message_decision(message_decision_fn, state, context)
            )
            try:
                if decision is TERMINATE_MESSAGING:
                    state = game.terminate_messaging()
                else:
                    state = game.send_message(decision.content, recipients=decision.recipients)
            except PlatformError as exc:
                if exc.error_code == "wrong_phase":
                    # Genuine race: the round advanced (or the game ended)
                    # between our last read and this submit. Not a
                    # contestant bug; refetch and re-evaluate.
                    state = game.state()
                    continue
                if exc.error_code == "game_already_finished":
                    return game.state()
                if exc.error_code in _MESSAGE_SCOPED_ERROR_CODES:
                    raise DecisionError(
                        f"choose_message produced an invalid messaging action "
                        f"for session {context.session_id!r}: {exc}"
                    ) from exc
                raise
            continue

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
