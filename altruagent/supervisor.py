"""Concurrent multi-match supervisor — the engine behind Milestone 4C's
``python -m agent``.

Non-blocking parent loop: repeatedly discovers this agent's active matches
via ``client.sessions()``, and keeps one independent worker *process*
running per active ``session_id`` (see ``altruagent.worker`` for the child
side, and why processes rather than threads/asyncio — module-level
contestant state and crash isolation both need a real OS-process boundary,
not just a Python-level one).

The parent never plays a match itself. It only starts/tracks/reaps worker
processes and owns the one thing that's meaningless at the per-worker
level: failed-match cooldown. A worker is one-shot (one match, then it
exits) — cooldown is inherently a "should I start a *new* one for this
session_id soon after the last one failed" decision, which only the
long-lived supervisor can make.

Explicitly uses the ``spawn`` multiprocessing context everywhere (never the
platform default) so behavior is identical and Windows-compatible
regardless of what OS this ever runs on — Windows has no ``fork`` at all.
"""

from __future__ import annotations

import multiprocessing
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

from .errors import PlatformError
from .official import new_execution_id
from .worker import (
    EXIT_SEAT_BUSY,
    EXIT_SUCCESS,
    TournamentWorkerInput,
    WorkerInput,
    _process_entry,
    _tournament_process_entry,
)

if TYPE_CHECKING:
    from .client import AltruAgentClient
    from .models import OfficialAssignment
    from .official import OfficialAgentClient

DEFAULT_DISCOVERY_INTERVAL_SECONDS = 15.0
DEFAULT_COOLDOWN_SECONDS = 60.0
DEFAULT_SHUTDOWN_JOIN_TIMEOUT_SECONDS = 5.0

_MP_CONTEXT = multiprocessing.get_context("spawn")


def _log(message: str) -> None:
    print(f"[agent] {message}")


class WorkerRegistry:
    """Tracks live worker processes keyed by ``session_id``.

    A thin, deliberately dumb bookkeeping object — no policy lives here
    (cooldown/eligibility decisions stay in ``run_once_concurrent``). Public
    (not underscore-prefixed) so tests can construct and inspect one
    directly without going through a full supervisor tick.
    """

    def __init__(self) -> None:
        self._processes: dict[str, "multiprocessing.process.BaseProcess"] = {}

    def is_active(self, session_id: str) -> bool:
        return session_id in self._processes

    def pids(self) -> dict[str, int]:
        """Currently-tracked ``{session_id: pid}`` — a read-only view used
        by callers (and the developer smoke test) that want to verify real,
        distinct OS processes are running, not just that the registry
        thinks something is active.
        """
        return {session_id: process.pid for session_id, process in self._processes.items()}

    def start(self, session_id: str, process: "multiprocessing.process.BaseProcess") -> None:
        self._processes[session_id] = process

    def reap_finished(self) -> dict[str, int]:
        """Remove every worker whose process has exited since the last
        call, and return ``{session_id: exitcode}`` for each. Calls
        ``.join()`` on each (already-exited) process to promptly reclaim OS
        resources — cheap and instant on a process that's already dead.
        """
        finished: dict[str, int] = {}
        for session_id, process in list(self._processes.items()):
            if not process.is_alive():
                process.join()
                finished[session_id] = process.exitcode
                del self._processes[session_id]
        return finished

    def terminate(self, session_id: str, timeout: float) -> None:
        """Forcibly stop one worker (same semantics as ``terminate_all``)."""
        process = self._processes.pop(session_id, None)
        if process is None:
            return
        if process.is_alive():
            process.terminate()
        process.join(timeout)

    def keys(self) -> list[str]:
        return list(self._processes)

    def terminate_all(self, timeout: float) -> None:
        """Forcibly stop every remaining worker and reclaim it. Uses
        ``.terminate()`` (immediate, no grace period) rather than asking
        nicely — on shutdown we specifically do NOT want a worker to get
        the chance to make one more API call (e.g. a "graceful" resign);
        each worker also independently handles its own ``KeyboardInterrupt``
        (see ``altruagent.worker``), so in the common case this just cleans
        up stragglers that didn't exit fast enough on their own.
        """
        for process in self._processes.values():
            if process.is_alive():
                process.terminate()
        for process in self._processes.values():
            process.join(timeout)
        self._processes.clear()

    def __len__(self) -> int:
        return len(self._processes)


def run_once_concurrent(
    client: "AltruAgentClient",
    *,
    registry: WorkerRegistry,
    failed_until: dict,
    agent_id: str,
    now: Callable[[], float] = time.monotonic,
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    process_factory: Callable[..., "multiprocessing.process.BaseProcess"] = _MP_CONTEXT.Process,
) -> None:
    """One non-blocking supervisor tick.

    1. Reap any workers that have finished since the last tick, applying a
       cooldown to any that exited with a non-success code.
    2. Discover this agent's current sessions and, for every ``active``
       match that doesn't already have a live worker and isn't in
       cooldown, start one. ``waiting``/``completed`` matches are never
       considered — only ``sessions.active`` is inspected.

    Never blocks on any worker; always returns immediately, regardless of
    how many matches are in flight.
    """
    current_time = now()

    for session_id, exitcode in registry.reap_finished().items():
        if exitcode != EXIT_SUCCESS:
            failed_until[session_id] = current_time + cooldown_seconds
            _log(
                f"worker for session_id={session_id} exited with code {exitcode} — "
                f"cooling down for {cooldown_seconds:.0f}s"
            )
        else:
            failed_until.pop(session_id, None)
            _log(f"worker for session_id={session_id} finished")

    sessions = client.sessions()
    for match in sessions.active:
        if registry.is_active(match.session_id):
            continue
        retry_at = failed_until.get(match.session_id)
        if retry_at is not None and current_time < retry_at:
            continue

        worker_input = WorkerInput(
            session_id=match.session_id,
            tournament_id=match.tournament_id,
            game_type=match.game_type,
            agent_id=agent_id,
        )
        process = process_factory(target=_process_entry, args=(worker_input,), daemon=True)
        process.start()
        registry.start(match.session_id, process)
        _log(
            f"started worker pid={process.pid} for session_id={match.session_id} "
            f"game_type={match.game_type} tournament_id={match.tournament_id}"
        )


def run_forever_concurrent(
    client: "AltruAgentClient",
    *,
    agent_id: str,
    discovery_interval: float = DEFAULT_DISCOVERY_INTERVAL_SECONDS,
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    shutdown_join_timeout: float = DEFAULT_SHUTDOWN_JOIN_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    process_factory: Callable[..., "multiprocessing.process.BaseProcess"] = _MP_CONTEXT.Process,
    max_iterations: int | None = None,
) -> None:
    """Repeatedly discover this agent's active matches and keep one worker
    process per active ``session_id`` running, until interrupted (or, for
    tests, until ``max_iterations`` ticks have elapsed).

    Whatever ends the loop — ``KeyboardInterrupt``, an unexpected exception
    (e.g. the parent's own discovery call hitting ``AuthenticationError``,
    which is *not* caught here and is treated as fatal — see module
    docstring), or ``max_iterations`` being reached — every still-running
    worker is terminated and joined (bounded by ``shutdown_join_timeout``)
    in a ``finally`` block before anything propagates further. No match is
    ever resigned on shutdown.
    """
    registry = WorkerRegistry()
    failed_until: dict = {}
    ticks = 0
    try:
        while max_iterations is None or ticks < max_iterations:
            run_once_concurrent(
                client,
                registry=registry,
                failed_until=failed_until,
                agent_id=agent_id,
                now=now,
                cooldown_seconds=cooldown_seconds,
                process_factory=process_factory,
            )
            ticks += 1
            sleep(discovery_interval)
    finally:
        if len(registry) > 0:
            _log(f"stopping {len(registry)} active worker(s)...")
            registry.terminate_all(shutdown_join_timeout)


# -- official tournament runtime -------------------------------------------------

DEFAULT_TOURNAMENT_POLL_SECONDS = 10.0
# A seat must be missing from this many consecutive successful assignment
# listings before its still-running worker is stopped — a match that just
# ended can drop off the list a moment before its worker notices.
MISSING_POLLS_BEFORE_STOP = 2
WAITING_MESSAGE = "Waiting for your next game..."
# After seat_busy, retry the seat once the backend's 30 s execution lease
# could have lapsed (a previous run releasing it, or this runtime's own
# re-authenticated session); a seat another live runtime keeps renewing
# just stays busy.
SEAT_BUSY_RETRY_SECONDS = 35.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# Names and labels come from the server (opponent names are chosen by other
# contestants), so they're cleaned before reaching the terminal: no control
# or escape characters, and a bounded length.
_MAX_TEXT = 80


def _clean(text: str | None) -> str:
    cleaned = "".join(ch for ch in (text or "") if ch.isprintable()).strip()
    return cleaned if len(cleaned) <= _MAX_TEXT else cleaned[: _MAX_TEXT - 3].rstrip() + "..."


def _parse_utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_wait(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def describe_assignment(assignment: "OfficialAssignment", *, now: datetime | None = None) -> list[str]:
    """The lines logged when a game is picked up: the game, then (when the
    server sent them) Testing vs tournament, the tournament and round, the
    opponents, and the connect deadline with the time left. Never prints ids
    or tokens.
    """
    game = _clean(assignment.game_type) or "unknown game"
    context = (assignment.context or "").lower()
    kind = {"testing": " (Testing)", "tournament": " (tournament)"}.get(context, "")
    lines = [f"Match assigned: {game}{kind}"]

    name, round_label = _clean(assignment.tournament_name), _clean(assignment.round_label)
    if name and round_label:
        lines.append(f"  Tournament: {name}, {round_label}")
    elif name:
        lines.append(f"  Tournament: {name}")
    elif round_label:
        lines.append(f"  Round: {round_label}")

    opponents = [cleaned for cleaned in (_clean(o) for o in assignment.opponents) if cleaned]
    if opponents:
        lines.append(f"  Opponents: {', '.join(opponents)}")

    if assignment.connect_deadline_at:
        deadline = _parse_utc(assignment.connect_deadline_at)
        if deadline is None:
            lines.append(f"  Connect by: {_clean(assignment.connect_deadline_at)}")
        else:
            at = deadline.strftime("%Y-%m-%d %H:%M:%S UTC")
            left = (deadline - (now or _utc_now())).total_seconds()
            when = f"{_format_wait(left)} left" if left > 0 else "deadline passed"
            lines.append(f"  Connect by: {at} ({when})")
    return lines


class TournamentState:
    """Everything the tournament loop carries between ticks — in memory only.

    ``execution_id`` is generated once per tournament runtime (one
    ``python -m agent --tournament`` process) and handed to every worker it
    starts: it's how the backend tells this runtime's seat leases apart from
    another runtime's. Never persisted or logged.
    """

    def __init__(self) -> None:
        self.execution_id = new_execution_id()
        self.registry = WorkerRegistry()
        self.failed_until: dict[str, float] = {}
        self.missing_polls: dict[str, int] = {}
        self.discovery_failing = False


def run_tournament_once(
    official: "OfficialAgentClient",
    state: TournamentState,
    *,
    agent_spec: str,
    now: Callable[[], float] = time.monotonic,
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    shutdown_join_timeout: float = DEFAULT_SHUTDOWN_JOIN_TIMEOUT_SECONDS,
    process_factory: Callable[..., "multiprocessing.process.BaseProcess"] = _MP_CONTEXT.Process,
    log: Callable[[str], None] = print,
    wall_now: Callable[[], datetime] = _utc_now,
) -> None:
    """One non-blocking tournament tick, keyed by ``seat_id`` throughout:

    1. Reap finished workers (a failure puts that seat in cooldown; it is
       retried later, which re-requests its grant — the reconnect path).
    2. List this agent's active assignments. A transient control-plane
       failure is logged and the tick skipped (the runtime keeps waiting);
       an authentication failure propagates — it would affect every seat.
    3. Start one worker per listed seat that has none and isn't cooling down,
       logging what was picked up (``describe_assignment``).
    4. Stop workers whose seat has left the list for
       ``MISSING_POLLS_BEFORE_STOP`` consecutive listings.
    """
    current_time = now()
    for seat_id, exitcode in state.registry.reap_finished().items():
        state.missing_polls.pop(seat_id, None)
        # Either way the seat waits out a cooldown before any new worker: a
        # finished seat can linger in the listing for a moment, and a failed
        # one is retried (re-granted) only if it's still assigned afterwards.
        state.failed_until[seat_id] = current_time + cooldown_seconds
        if exitcode == EXIT_SUCCESS:
            log("Match finished.")
        elif exitcode == EXIT_SEAT_BUSY:
            state.failed_until[seat_id] = current_time + SEAT_BUSY_RETRY_SECONDS
            log(f"Another runtime is playing this match with your Official Agent Key; checking again in {SEAT_BUSY_RETRY_SECONDS:.0f}s.")
        else:
            log(f"Match ended with an error (exit code {exitcode}); retrying that seat in {cooldown_seconds:.0f}s if it is still assigned.")
        if len(state.registry) == 0:
            log(WAITING_MESSAGE)

    try:
        assignments = official.assignments()
    except PlatformError as exc:
        if not state.discovery_failing:
            log(f"Could not check tournament assignments ({exc}); will keep retrying.")
        state.discovery_failing = True
        return
    if state.discovery_failing:
        log("Assignment discovery recovered.")
        state.discovery_failing = False

    listed = {assignment.seat_id for assignment in assignments}
    for assignment in assignments:
        seat_id = assignment.seat_id
        if not seat_id or state.registry.is_active(seat_id):
            continue
        retry_at = state.failed_until.get(seat_id)
        if retry_at is not None and current_time < retry_at:
            continue
        worker_input = TournamentWorkerInput(
            seat_id=seat_id, match_id=assignment.match_id, game_type=assignment.game_type,
            agent_spec=agent_spec, execution_id=state.execution_id,
            tournament_id=assignment.tournament_id,
        )
        process = process_factory(target=_tournament_process_entry, args=(worker_input,), daemon=True)
        process.start()
        state.registry.start(seat_id, process)
        for line in describe_assignment(assignment, now=wall_now()):
            log(line)
        log("Starting match...")

    for seat_id in state.registry.keys():
        if seat_id in listed:
            state.missing_polls.pop(seat_id, None)
            continue
        state.missing_polls[seat_id] = state.missing_polls.get(seat_id, 0) + 1
        if state.missing_polls[seat_id] >= MISSING_POLLS_BEFORE_STOP:
            state.registry.terminate(seat_id, shutdown_join_timeout)
            state.missing_polls.pop(seat_id, None)
            log("Match is no longer assigned; stopped its worker.")
            if len(state.registry) == 0:
                log(WAITING_MESSAGE)


def run_tournament_forever(
    official: "OfficialAgentClient",
    *,
    agent_spec: str,
    poll_interval: float = DEFAULT_TOURNAMENT_POLL_SECONDS,
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    shutdown_join_timeout: float = DEFAULT_SHUTDOWN_JOIN_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    process_factory: Callable[..., "multiprocessing.process.BaseProcess"] = _MP_CONTEXT.Process,
    log: Callable[[str], None] = print,
    max_iterations: int | None = None,
) -> None:
    """Keep one worker per active official assignment until interrupted.
    Never exits just because there are no assignments. However it stops
    (Ctrl+C, a fatal auth error, or ``max_iterations`` in tests), every
    running worker is terminated in ``finally`` — no match is resigned.
    """
    state = TournamentState()
    ticks = 0
    try:
        while max_iterations is None or ticks < max_iterations:
            run_tournament_once(
                official, state, agent_spec=agent_spec, now=now, cooldown_seconds=cooldown_seconds,
                shutdown_join_timeout=shutdown_join_timeout, process_factory=process_factory, log=log,
            )
            ticks += 1
            sleep(poll_interval)
    finally:
        if len(state.registry) > 0:
            log(f"Stopping {len(state.registry)} active match worker(s)...")
            state.registry.terminate_all(shutdown_join_timeout)
