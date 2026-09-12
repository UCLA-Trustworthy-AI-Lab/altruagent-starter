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
from typing import TYPE_CHECKING, Callable

from .worker import EXIT_SUCCESS, WorkerInput, _process_entry

if TYPE_CHECKING:
    from .client import AltruAgentClient

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
