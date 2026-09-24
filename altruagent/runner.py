"""Single-match execution primitive — MCP-first.

Owns exactly one match's play loop: fetch state, decide (via contestant-
supplied logic) whether a decision is needed right now, submit it, repeat
until the match ends. This is the ONLY production gameplay path — it plays
through ``MCPGameSession`` (``altruagent.mcp_game``), Agent_ACP's generic
MCP gameplay contract, never the lower-level REST ``GameSession``
(``altruagent.game``, kept only for ``scripts/check_game.py``'s manual
debugging). This file contains no per-game branching (no ``if game_type ==
"pokemon"``/``"avalon"``/...) — every game (OpenSpiel-family or a structured
RuntimeAdapter like Pokémon) is driven through the same four generic
observations: ``is_terminal``, ``phase`` (only the literal value
``"messaging"`` is special-cased), ``is_current_actor``, and
``legal_actions`` — confirmed, not assumed, to be returned under those exact
key names by every adapter currently in Agent_ACP
(``gameapi/src/gameapi/runtime_adapters/{openspiel_adapter,pokemon_adapter}.py``).

The contestant contract is two parameters and a return value, nothing more:

    def choose_action(state: GameState, context: DecisionContext) -> int:
        return state.legal_actions[0]

No base class, no decorator, no registration. `run_match`/`run_game` also
accept an object exposing a `.choose_action(state, context)` method instead
of a plain function, resolved with one `callable()` check and one
`getattr()` — no signature/arity introspection.

`choose_action` may return, and this runner normalizes without ever
guessing or fuzzy-coercing:

    - a `LegalAction` from `state.legal_actions` (the universal pattern:
      `return state.legal_actions[0]` works for every game)
    - that `LegalAction`'s `action_id` string
    - a plain `int`, ONLY accepted when `str(that int)` exactly equals some
      current legal action's `action_id` (this is what makes OpenSpiel-family
      games' historical `return 0`-style agents keep working unchanged — and
      why it correctly REJECTS an int for a structured game like Pokémon,
      whose action_ids are `"move:0"`, not `"0"`)
    - a structured `dict`, passed straight through as the MCP `action`
      payload for constructive actions (e.g. Pokémon's `submit_team`) that
      can't be enumerated as one of `state.legal_actions` — the SDK performs
      no game-specific validation of it; the server is authoritative
    - `altruagent.RESIGN`

Turn detection is driven by `GameState.is_current_actor`/`phase`/
`is_terminal` (plus Werewolf's `eliminated` flag) rather than MCP's
`next_actions` hint, so the loop stays independent of hint wording.
`next_actions` is still parsed onto `GameState` but is supplementary/
informational only.

Waiting is a server-side long-poll: when it isn't this agent's turn, the
runner calls `wait_for_update`, which returns as soon as anything changes
(a move, a new message, whose turn it is, the phase, or the game ending).
Each wait lasts at most `WAIT_FOR_UPDATE_TIMEOUT_SECONDS` (then it simply
waits again); against a server that predates that tool, the runner sleeps
`wait_seconds` and re-reads state instead.

Messaging works the same way: `phase == "messaging"` is the one phase value
this runner recognizes by exact string match; every other phase value (a
future adapter's `"moving"`, `"draft"`, `"teambuild"`, or anything else)
falls through to the identical "it's my turn -> enumerate legal actions ->
choose_action" path with zero adapter-specific code — this is what lets
Pokémon's draft/teambuild phases work without this runner knowing Pokémon
exists. `choose_message` is optional — a contestant that only defines
`choose_action` gets `TERMINATE_MESSAGING` automatically every round (see
`_default_choose_message`), so Werewolf (whose discussion window opens once
per day) can be played move-only with zero new contestant code. `choose_message` is never invoked for a game whose adapter
never reports `phase == "messaging"` (confirmed: Pokémon's phases are
`draft`/`draft_complete`/`teambuild`/`moving`, never `"messaging"`).

`state_version` (MCP's optimistic-concurrency counter) is entirely
runner-owned: taken from the same read that produced `state.legal_actions`
(the server only embeds legal actions whose `state_version` matches the
state's), never something `choose_action`/`choose_message` supply or manage.
"""

from __future__ import annotations

import time
from typing import Any, Callable, NamedTuple, Protocol, Union

from .mcp_game import MCPGameSession
from .mcp_transport import MCPToolError
from .models import DecisionContext, GameState, LegalAction, Match

# The sleep between reads against a server without wait_for_update.
DEFAULT_WAIT_SECONDS = 5.0

# wait_for_update's long-poll timeout; it returns earlier as soon as anything
# changes. 20 s is the server default, under the MCP client's 30 s read
# timeout. This relies on the server honoring since_is_current_actor/
# since_phase (Agent_ACP #24): without them, a whose-turn/phase change that
# lands before the call — every Pokémon battle turn for the second seat to
# submit — is only noticed at the timeout.
WAIT_FOR_UPDATE_TIMEOUT_SECONDS = 20.0

# Confirmed exact codes against Agent_ACP's gameapi/src/gameapi/mcp_server/errors.py
# (`_DOMAIN_ERROR_CODES`, plus the documented fallback of an unlisted domain
# exception's own `.error` attribute uppercased, e.g. WrongPhaseError's
# "wrong_phase" -> "WRONG_PHASE"). All four represent "the match state moved
# since the last read" — a genuine race, never a contestant bug — so all four
# just refetch state and re-enter the decision loop from the top.
# PLAYER_ELIMINATED (Werewolf) belongs here too: this agent was eliminated
# after the read it acted on; the refetched state carries `eliminated`, and
# the loop then only waits for the game to end.
_RACE_ERROR_CODES = frozenset(
    {
        "STALE_STATE",
        "NOT_YOUR_TURN",
        "WRONG_PHASE",
        "GAME_ALREADY_COMPLETE",
        "PLAYER_ELIMINATED",
    }
)

# Contestant-caused messaging failures (bad content/recipients, over quota,
# or messaging attempted on a non-messaging game) — fail-fast DecisionErrors,
# same philosophy as an illegal choose_action result.
_MESSAGE_SCOPED_ERROR_CODES = frozenset(
    {
        "INVALID_RECIPIENTS",
        "MESSAGE_TOO_LONG",
        "TOO_MANY_WORDS",
        "INVALID_CONTENT",
        "MESSAGES_QUOTA_EXCEEDED",
        "MESSAGING_DISABLED",
    }
)

# Contestant-caused action failures.
_ACTION_SCOPED_ERROR_CODES = frozenset({"INVALID_ACTION"})

# The runner believed messaging/a capability was available (phase said so)
# but the adapter disagrees — a genuine runner/adapter mismatch, not a
# contestant bug and not a race; never silently retried.
_CAPABILITY_ERROR_CODE = "RUNTIME_UNAVAILABLE"

_MESSAGING_PHASE = "messaging"


class _Resign:
    """Unique sentinel type. Never instantiate another one — always use the
    exported ``RESIGN`` singleton and compare with ``is``.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "RESIGN"


RESIGN = _Resign()


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


Decision = Union[int, str, LegalAction, dict, _Resign]
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
    type/value this runner doesn't recognize (see the module docstring for
    exactly what's accepted), or produced an action/message the server
    rejected as invalid (bad recipients/content, word/length cap, chat quota
    exceeded, malformed structured action — see ``_MESSAGE_SCOPED_ERROR_CODES``/
    ``_ACTION_SCOPED_ERROR_CODES``). Fails fast, on purpose — a deterministic
    contestant bug should be visible immediately during local development,
    not silently retried on the next cycle.
    """


class UnsupportedGameFlowError(RunnerError):
    """The match reported a phase/capability this runner expected the
    adapter to support, but the adapter disagreed (MCP's
    ``RUNTIME_UNAVAILABLE``) — a genuine adapter/runner mismatch, not a
    contestant bug. Not raised merely because ``choose_message`` is absent
    (see ``_default_choose_message``) — only when the runner's own
    phase-based detection turns out to be wrong for a given adapter.
    """


class _PlayAction(NamedTuple):
    """Normalized form of a validated ``choose_action`` decision — exactly
    one of ``action_id``/``action`` is set, matching MCP's own
    ``play_action`` precedence (``action`` wins if both are given).
    """

    action_id: str | None
    action: dict | None


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


def _resolve_message_decision_fn(choose_action: Any) -> MessageDecisionFn:
    """Look up an optional ``choose_message(state, context)`` on the same
    object/function used for ``choose_action``.

    Unlike ``_resolve_decision_fn``, a missing ``choose_message`` is not an
    error — it's the normal case for a contestant that only cares about
    moves, and defaults to always terminating the messaging round
    immediately (see ``_default_choose_message``). Must be resolved from the
    *original* ``choose_action`` value the caller passed in, not from
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


def _invoke_decision(decision_fn: DecisionFn, state: GameState, context: DecisionContext) -> Any:
    try:
        return decision_fn(state, context)
    except Exception as exc:
        raise DecisionError(
            f"choose_action raised {exc!r} for session {context.session_id!r}."
        ) from exc


def _invoke_message_decision(
    decision_fn: MessageDecisionFn, state: GameState, context: DecisionContext
) -> Any:
    try:
        return decision_fn(state, context)
    except Exception as exc:
        raise DecisionError(
            f"choose_message raised {exc!r} for session {context.session_id!r}."
        ) from exc


def _validate_decision(decision: Any, legal_actions: list[LegalAction]) -> Union[_Resign, _PlayAction]:
    if decision is RESIGN:
        return RESIGN

    # bool is a subclass of int in Python (isinstance(True, int) is True) —
    # reject it explicitly before the int branch below would otherwise treat
    # it as a legal action lookup.
    if isinstance(decision, bool):
        raise DecisionError(
            "choose_action must return a LegalAction, its action_id (str), a "
            "matching int, a structured dict action, or altruagent.RESIGN — "
            f"got {decision!r} (bool)."
        )

    if isinstance(decision, LegalAction):
        candidate = decision.action_id
    elif isinstance(decision, str):
        candidate = decision
    elif isinstance(decision, int):
        candidate = str(decision)
    elif isinstance(decision, dict):
        if not decision:
            raise DecisionError(
                "choose_action returned an empty dict — provide the structured "
                "action payload the server expects (e.g. "
                "{'type': 'submit_team', 'team': [...]})."
            )
        return _PlayAction(action_id=None, action=decision)
    else:
        raise DecisionError(
            "choose_action must return a LegalAction from state.legal_actions, "
            "its action_id (str), a matching int, a structured dict action, or "
            f"altruagent.RESIGN — got {decision!r} ({type(decision).__name__})."
        )

    matched = next((a for a in legal_actions if a.action_id == candidate), None)
    if matched is None:
        raise DecisionError(
            f"choose_action returned {decision!r}, which does not match any "
            f"current legal action's action_id (tried {candidate!r} against "
            f"{[a.action_id for a in legal_actions]!r}). An int is only valid "
            "for OpenSpiel-family games whose action_ids are stringified "
            "integers — never guessed/coerced for other games."
        )
    return _PlayAction(action_id=matched.action_id, action=None)


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


def _last_message_seq(state: GameState, extra: dict | None = None) -> int | None:
    """Highest message ``seq`` this agent has seen: the state's
    ``new_messages``, plus (after a ``send_message``) that call's own
    ``message``/``new_messages``. Passed to ``wait_for_update`` so a message
    that arrived before the wait started still wakes it.
    """
    seqs = [m.index for m in state.new_messages]
    if extra:
        messages = list(extra.get("new_messages") or [])
        if isinstance(extra.get("message"), dict):
            messages.append(extra["message"])
        seqs += [m["seq"] for m in messages if isinstance(m, dict) and isinstance(m.get("seq"), int)]
    return max(seqs) if seqs else None


def _is_unknown_tool_error(exc: MCPToolError) -> bool:
    """A server predating ``wait_for_update`` rejects it at the protocol
    level (FastMCP's ``Unknown tool: ...``), with no error_code.
    """
    return exc.error_code is None and "unknown tool" in str(exc).lower()


def _terminal_game_state(last_state: GameState, result: dict) -> GameState:
    """Merge a ``get_result()``/``resign()`` result (authoritative for
    ``returns``/``termination_reason``, confirmed absent from ``get_state()``/
    ``play_action()``) onto the last known state's raw dict (for ``phase``/
    ``observation`` context, still valid once terminal).
    """
    return GameState.from_mcp_state(last_state.raw, result=result)


def run_game(
    game: MCPGameSession,
    context: DecisionContext,
    choose_action: DecisionFn,
    *,
    wait_seconds: float = DEFAULT_WAIT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> GameState:
    """Play one already-open ``MCPGameSession`` to completion.

    Loop, once per iteration:

    1. Read state (``get_game_state``; afterwards each step below hands back
       the next state itself).
    2. If terminal, fetch the result (``get_result``) and return the merged
       final state.
    3. If this agent was eliminated (Werewolf), it can no longer act or
       chat: wait (step 6) until the game ends.
    4. If ``phase == "messaging"``: resolve an optional ``choose_message``,
       validate the result, and submit it (``send_message`` with
       ``message_type="chat"`` or ``"terminate"``). If the round hasn't
       actually advanced yet (``send_message``'s own result still reports
       ``phase != "moving"`` — e.g. this agent terminated but others
       haven't), wait (step 6) for the next message or the phase change.
    5. Else if ``is_current_actor``: take ``legal_actions`` from the state
       (the server embeds them when this agent can act; ``get_legal_actions``
       is only a fallback when it didn't), invoke ``choose_action``, validate
       the result, and submit it (``play_action`` for a matched/structured
       action, ``resign`` for ``RESIGN``). ``play_action``'s result carries
       the post-move state, which becomes the next state directly.
    6. Else (not this agent's turn): ``wait_for_update`` — returns as soon as
       anything changes, or after ``WAIT_FOR_UPDATE_TIMEOUT_SECONDS`` with
       the unchanged state (the loop just waits again). Against a server without that tool,
       sleeps ``wait_seconds`` and re-reads instead.

    A race error (``STALE_STATE``/``NOT_YOUR_TURN``/``WRONG_PHASE``/
    ``GAME_ALREADY_COMPLETE``/``PLAYER_ELIMINATED`` — the state changed
    between our last read and this submit, not a contestant bug) refetches
    state and continues. ``RUNTIME_UNAVAILABLE`` (the adapter doesn't
    actually support a capability the runner expected) raises
    ``UnsupportedGameFlowError``. A contestant-caused messaging/action error
    raises ``DecisionError``. Any other error propagates immediately — no
    retrying.

    Returns the final ``GameState`` once the match is terminal.
    """
    decision_fn = _resolve_decision_fn(choose_action)
    message_decision_fn = _resolve_message_decision_fn(choose_action)
    long_poll_supported = True

    def wait(current: GameState, message_seq: int | None) -> GameState:
        nonlocal long_poll_supported
        if long_poll_supported:
            try:
                return game.wait_for_update(
                    since_version=current.state_version,
                    since_message_seq=message_seq,
                    since_is_current_actor=bool(current.is_current_actor),
                    since_phase=current.phase,
                    timeout_seconds=WAIT_FOR_UPDATE_TIMEOUT_SECONDS,
                )
            except MCPToolError as exc:
                if not _is_unknown_tool_error(exc):
                    raise
                long_poll_supported = False
        sleep(wait_seconds)
        return game.get_state()

    state = game.get_state()

    while True:
        if state.is_terminal:
            result = game.get_result()
            return _terminal_game_state(state, result)

        if state.raw.get("eliminated"):
            state = wait(state, _last_message_seq(state))
            continue

        if state.phase == _MESSAGING_PHASE:
            decision = _validate_message_decision(
                _invoke_message_decision(message_decision_fn, state, context)
            )
            try:
                if decision is TERMINATE_MESSAGING:
                    result = game.send_message(message_type="terminate")
                else:
                    result = game.send_message(
                        message_type="chat",
                        content=decision.content,
                        recipients=decision.recipients,
                    )
            except MCPToolError as exc:
                if exc.error_code in _RACE_ERROR_CODES:
                    state = game.get_state()
                    continue
                if exc.error_code == _CAPABILITY_ERROR_CODE:
                    raise UnsupportedGameFlowError(
                        f"Match {context.session_id!r} reported phase="
                        f"{_MESSAGING_PHASE!r}, but its adapter does not support "
                        "messaging tools — a runner/adapter mismatch, not a "
                        "contestant bug."
                    ) from exc
                if exc.error_code in _MESSAGE_SCOPED_ERROR_CODES:
                    raise DecisionError(
                        f"choose_message produced an invalid messaging action "
                        f"for session {context.session_id!r}: {exc}"
                    ) from exc
                raise
            # send_message's own result already carries the post-call phase.
            # Terminating is idempotent server-side, so a contestant that
            # already terminated this round (or the default auto-terminate)
            # would just get a no-op success back while the others are still
            # talking — wait for the next message or the phase flip instead
            # of re-sending.
            if result.get("phase") == "moving":
                state = game.get_state()
            else:
                state = wait(state, _last_message_seq(state, result))
            continue

        if state.is_current_actor:
            if state.raw.get("legal_actions") is None:
                # The server omits legal_actions when a move landed between
                # its state and legal-actions reads; fetch them directly.
                legal = game.get_legal_actions()
                state.legal_actions = [
                    LegalAction.from_dict(a) for a in legal.get("actions") or []
                ]
                state.state_version = int(legal.get("state_version", state.state_version))

            decision = _validate_decision(
                _invoke_decision(decision_fn, state, context), state.legal_actions
            )
            try:
                if decision is RESIGN:
                    result = game.resign()
                    return _terminal_game_state(state, result)
                result = game.play_action(
                    action_id=decision.action_id,
                    action=decision.action,
                    state_version=state.state_version,
                )
            except MCPToolError as exc:
                if exc.error_code in _RACE_ERROR_CODES:
                    state = game.get_state()
                    continue
                if exc.error_code == _CAPABILITY_ERROR_CODE:
                    raise UnsupportedGameFlowError(
                        f"Match {context.session_id!r} reported it was this "
                        "agent's turn, but its adapter rejected the move "
                        "capability — a runner/adapter mismatch, not a "
                        "contestant bug."
                    ) from exc
                if exc.error_code in _ACTION_SCOPED_ERROR_CODES:
                    raise DecisionError(
                        f"choose_action produced an invalid action for session "
                        f"{context.session_id!r}: {exc}"
                    ) from exc
                raise
            # While the game continues, play_action returns the post-move
            # state; once it's over (or on an older server) it doesn't.
            post_move = result.get("state") if isinstance(result, dict) else None
            state = (
                GameState.from_mcp_state(post_move)
                if isinstance(post_move, dict)
                else game.get_state()
            )
            continue

        state = wait(state, _last_message_seq(state))


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

    A thin convenience wrapper around ``run_game``: opens the match via
    ``match.game()`` — the production ``MCPGameSession`` path (reusing its
    existing lazy ``game_server_url`` resolution and caching rather than
    duplicating that logic here; never ``match.rest_game()``, the debug-only
    REST path) — and builds the ``DecisionContext`` from the ``Match``'s own
    fields plus the caller-supplied ``agent_id`` (a ``Match`` has no notion
    of "which agent is playing it").
    """
    game = match.game()
    context = DecisionContext(
        session_id=match.session_id,
        tournament_id=match.tournament_id,
        game_type=match.game_type,
        agent_id=agent_id,
    )
    return run_game(game, context, choose_action, wait_seconds=wait_seconds, sleep=sleep)
