"""Per-match worker process — the child side of Milestone 4C's
process-per-match concurrency model.

``run_worker`` and ``_process_entry`` must stay top-level, importable
functions (never a closure/lambda/bound method) — Windows has no ``fork``;
``multiprocessing``'s ``spawn`` start method launches a genuinely fresh
Python interpreter for every worker and re-imports this module by name to
find its target, rather than copying the parent's memory. That's also
exactly what gives each worker its own isolated module-level state for
free: a contestant's ``agent.agent`` module is reimported from scratch in
every worker process, so even careless module-level globals (not just a
``create_agent()``-returned instance) never leak between matches.

Only primitive, picklable data crosses the process boundary (``WorkerInput``
below) — confirmed empirically during the 4C design audit that
``AltruAgentClient``/``GameSession``/a client-attached ``Match`` are not
picklable (``httpx.Client`` holds a real ``_thread.RLock``), so nothing here
ever tries to pass one. Each worker builds its own ``AltruAgentClient()``
from its own inherited environment/`.env` (the same config-loading path
every other entry point already uses) rather than receiving the API key
through ``WorkerInput`` at all.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Callable, NamedTuple

from .client import AltruAgentClient
from .errors import PlatformError
from .models import Match
from .runner import DecisionError, UnsupportedGameFlowError
from .runner import run_match as _default_run_match

if TYPE_CHECKING:
    from .models import GameState

# The entire parent<->child result-signaling mechanism (see
# supervisor.py) — no Queue/Pipe is used; a worker always returns one of
# these via its process exit code. EXIT_MATCH_FAILURE and EXIT_UNEXPECTED
# both lead to the same cooldown treatment in the supervisor; the
# distinction exists purely so logs can say which kind of failure occurred.
EXIT_SUCCESS = 0
EXIT_MATCH_FAILURE = 1
EXIT_UNEXPECTED = 2

_MATCH_SCOPED_ERRORS = (DecisionError, UnsupportedGameFlowError, PlatformError)


class WorkerInput(NamedTuple):
    """Primitive, picklable description of one match for a worker process to
    reconstruct and play — exactly the fields ``DecisionContext`` needs.
    Deliberately does not include ``control_url``/``api_key``: the worker
    builds its own client from its own inherited environment instead (see
    module docstring).
    """

    session_id: str
    tournament_id: str | None
    game_type: str | None
    agent_id: str


def run_worker(
    worker_input: WorkerInput,
    *,
    client_factory: Callable[[], AltruAgentClient] = AltruAgentClient,
    match_factory: Callable[..., Match] = Match.from_dict,
    agent_module: object | None = None,
    run_match_fn: Callable[..., "GameState"] = _default_run_match,
) -> int:
    """Play exactly one match: build a client, reconstruct its ``Match``,
    call ``agent.agent.create_agent()`` exactly once, and hand the result to
    the existing, unmodified ``run_match``.

    Returns an exit code (``EXIT_*`` above) rather than raising — this is
    what ``_process_entry`` turns into a real process exit code, and it's
    also why this function is safe and useful to call directly (no real
    process involved) from tests: every failure path is captured as a
    return value, never an uncaught exception escaping to the caller.

    ``client_factory``/``match_factory``/``agent_module``/``run_match_fn``
    all default to the real production pieces; tests override them to avoid
    any real network call or dependency on a real ``agent/agent.py``.
    """
    pid = os.getpid()
    print(f"[worker pid={pid}] starting session_id={worker_input.session_id}")

    client: AltruAgentClient | None = None
    try:
        if agent_module is None:
            import agent.agent as agent_module  # the contestant's own code

        create_agent = getattr(agent_module, "create_agent", None)
        if not callable(create_agent):
            print(
                f"[worker pid={pid}] agent.agent.create_agent is missing or "
                "not callable — nothing to play this match with."
            )
            return EXIT_UNEXPECTED

        client = client_factory()
        match = match_factory(
            {
                "session_id": worker_input.session_id,
                "tournament_id": worker_input.tournament_id,
                "game_type": worker_input.game_type,
                "status": "in_progress",
            },
            client=client,
        )

        contestant = create_agent()
        final_state = run_match_fn(match, worker_input.agent_id, contestant)
        print(
            f"[worker pid={pid}] match finished session_id={worker_input.session_id} "
            f"termination_reason={final_state.termination_reason}"
        )
        return EXIT_SUCCESS
    except KeyboardInterrupt:
        # Windows delivers Ctrl+C to the whole console process group, so
        # this worker sees it directly too, independent of whether the
        # parent's own cleanup reaches it in time. Exit quietly — no
        # traceback, no resign, no further API calls.
        print(f"[worker pid={pid}] interrupted, stopping")
        return EXIT_SUCCESS
    except _MATCH_SCOPED_ERRORS as exc:
        print(
            f"[worker pid={pid}] match failed session_id={worker_input.session_id}: {exc}"
        )
        return EXIT_MATCH_FAILURE
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see EXIT_UNEXPECTED
        print(f"[worker pid={pid}] unexpected error: {exc!r}")
        return EXIT_UNEXPECTED
    finally:
        if client is not None:
            client.close()


def _process_entry(worker_input: WorkerInput) -> None:
    """The actual ``multiprocessing.Process`` target. Top-level and
    importable by name, as Windows ``spawn`` requires. Translates
    ``run_worker``'s return value into a real process exit code via
    ``SystemExit`` — ``multiprocessing.Process.exitcode`` surfaces exactly
    this value to the parent once the process has exited.
    """
    raise SystemExit(run_worker(worker_input))
