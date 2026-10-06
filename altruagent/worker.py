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
from .mcp_game import MCPGameSession
from .models import Match
from .runner import DecisionError, UnsupportedGameFlowError
from .runner import run_match as _default_run_match

if TYPE_CHECKING:
    from .models import GameState
    from .official import OfficialAgentClient

# The entire parent<->child result-signaling mechanism (see
# supervisor.py) — no Queue/Pipe is used; a worker always returns one of
# these via its process exit code. EXIT_MATCH_FAILURE and EXIT_UNEXPECTED
# both lead to the same cooldown treatment in the supervisor; the
# distinction exists purely so logs can say which kind of failure occurred.
EXIT_SUCCESS = 0
EXIT_MATCH_FAILURE = 1
EXIT_UNEXPECTED = 2
# Tournament workers only: another runtime holds this seat's execution lease.
# The supervisor retries the seat once a lease could have lapsed.
EXIT_SEAT_BUSY = 3

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
    # ``MODULE[:FACTORY]`` to build the contestant from (``--agent``); None
    # keeps the default, ``agent.agent.create_agent``.
    agent_spec: str | None = None


def run_worker(
    worker_input: WorkerInput,
    *,
    client_factory: Callable[[], AltruAgentClient] = AltruAgentClient,
    match_factory: Callable[..., Match] = Match.from_dict,
    agent_module: object | None = None,
    run_match_fn: Callable[..., "GameState"] = _default_run_match,
) -> int:
    """Play exactly one match: build a client, reconstruct its ``Match``,
    call ``agent.agent.create_agent()`` (or ``worker_input.agent_spec``'s
    factory) exactly once, and hand the result to the existing, unmodified
    ``run_match``.

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
        if worker_input.agent_spec:
            from .agent_loader import load_agent_factory

            create_agent = load_agent_factory(worker_input.agent_spec)
        else:
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


class TournamentWorkerInput(NamedTuple):
    """Primitive, picklable description of one official tournament seat.
    Carries no credential: the worker reads the Official Agent Key from its
    own inherited environment/.env, exactly like ``WorkerInput`` above.
    """

    seat_id: str
    match_id: str
    game_type: str | None
    agent_spec: str
    # The supervisor's one execution id, shared by all of its workers (the
    # seat lease owner). Kept out of repr so it never lands in a log.
    execution_id: str

    def __repr__(self) -> str:
        return (
            f"TournamentWorkerInput(seat_id={self.seat_id!r}, match_id={self.match_id!r}, "
            f"game_type={self.game_type!r}, agent_spec={self.agent_spec!r})"
        )


def run_tournament_worker(
    worker_input: TournamentWorkerInput,
    *,
    official_factory: Callable[[], "OfficialAgentClient"] | None = None,
    run_game_fn: Callable[..., "GameState"] | None = None,
    lease_renew_seconds: float | None = None,
    lease_retry_seconds: float | None = None,
) -> int:
    """Play one official seat: build the contestant from ``agent_spec``
    (``create_agent()`` exactly once, in this process), request the seat's
    SeatGrant, and hand the seat to the existing ``run_game`` loop. GameAPI
    only ever sees the SeatGrant token; renewing it after a 401 re-requests
    the grant (``OfficialSeatAuth``). While the match runs, a
    ``SeatLeaseKeeper`` keeps this runtime's execution lease on the seat; if
    another runtime takes it (``seat_busy``), every further gameplay call is
    refused and the worker exits with ``EXIT_SEAT_BUSY``. Returns an
    ``EXIT_*`` code.
    """
    from .agent_loader import load_agent_factory
    from .models import DecisionContext
    from .official import (
        LEASE_RENEW_SECONDS,
        LEASE_RETRY_SECONDS,
        OfficialAgentClient,
        OfficialAgentError,
        OfficialSeatAuth,
        SeatLeaseKeeper,
        SeatLeaseLost,
    )
    from .runner import _resolve_decision_fn, run_game

    official_factory = official_factory or OfficialAgentClient
    run_game_fn = run_game_fn or run_game
    pid = os.getpid()
    label = f"[match {worker_input.game_type or 'unknown'} seat={worker_input.seat_id}]"
    print(f"{label} starting (pid={pid})")

    official = None
    game_client = None
    keeper = None
    try:
        contestant = load_agent_factory(worker_input.agent_spec)()
        _resolve_decision_fn(contestant)

        official = official_factory()
        seat_auth = OfficialSeatAuth(official, worker_input.seat_id, worker_input.execution_id)
        game_client = AltruAgentClient(control_url=official.control_url, auth=seat_auth, load_env_file=False)
        try:
            game_client.login()  # the first grant for this seat
        except OfficialAgentError as exc:
            if exc.error_code == "assignment_not_grantable":
                print(f"{label} assignment already ended; nothing to play")
                return EXIT_SUCCESS
            if exc.error_code == "seat_busy":
                print(f"{label} seat is held by another runtime; not playing it")
                return EXIT_SEAT_BUSY
            raise
        grant = seat_auth.grant

        keeper = SeatLeaseKeeper(
            official,
            worker_input.seat_id,
            worker_input.execution_id,
            renew_seconds=lease_renew_seconds or LEASE_RENEW_SECONDS,
            retry_seconds=lease_retry_seconds or LEASE_RETRY_SECONDS,
            log=lambda message: print(f"{label} {message}"),
        )
        keeper.start()
        game = _LeaseGuardedGameSession(
            game_client, lost=keeper.lost, session_id=grant.game_session_id, game_server_url=grant.gameapi_server_url
        )
        context = DecisionContext(
            session_id=grant.game_session_id,
            tournament_id=None,
            game_type=grant.game_type,
            agent_id=grant.agent_id,
            seat_position=grant.seat_position,
        )
        final_state = run_game_fn(game, context, contestant)
        score = (final_state.returns or {}).get(grant.agent_id)
        print(
            f"{label} finished termination_reason={final_state.termination_reason}"
            + (f" score={score}" if score is not None else "")
        )
        return EXIT_SUCCESS
    except KeyboardInterrupt:
        print(f"{label} interrupted, stopping")
        return EXIT_SUCCESS
    except SeatLeaseLost:
        print(f"{label} stopped: another runtime now holds this seat")
        return EXIT_SEAT_BUSY
    except _MATCH_SCOPED_ERRORS as exc:
        if keeper is not None and keeper.lost.is_set():
            print(f"{label} stopped: another runtime now holds this seat")
            return EXIT_SEAT_BUSY
        print(f"{label} match failed: {exc}")
        return EXIT_MATCH_FAILURE
    except Exception as exc:  # noqa: BLE001 - includes contestant factory errors and auth failures
        print(f"{label} unexpected error: {exc!r}")
        return EXIT_UNEXPECTED
    finally:
        if keeper is not None:
            keeper.stop()
        for client in (game_client, official):
            if client is not None:
                client.close()


class _LeaseGuardedGameSession(MCPGameSession):
    """The seat's MCP session, refusing every further GameAPI call once the
    execution lease is lost (all tools go through ``_call``), so a worker that
    lost its seat can't keep acting on it.
    """

    def __init__(self, client, *, lost, **kwargs) -> None:
        super().__init__(client, **kwargs)
        self._lost = lost

    def _call(self, tool: str, arguments: dict) -> dict:
        if self._lost.is_set():
            from .official import SeatLeaseLost

            raise SeatLeaseLost("Another runtime now holds this seat's execution lease.")
        return super()._call(tool, arguments)


def _tournament_process_entry(worker_input: TournamentWorkerInput) -> None:
    """``multiprocessing.Process`` target for a tournament seat (see
    ``_process_entry``)."""
    raise SystemExit(run_tournament_worker(worker_input))


def _process_entry(worker_input: WorkerInput) -> None:
    """The actual ``multiprocessing.Process`` target. Top-level and
    importable by name, as Windows ``spawn`` requires. Translates
    ``run_worker``'s return value into a real process exit code via
    ``SystemExit`` — ``multiprocessing.Process.exitcode`` surfaces exactly
    this value to the parent once the process has exited.
    """
    raise SystemExit(run_worker(worker_input))
