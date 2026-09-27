"""Entry point for `python -m agent`.

Authenticates using the existing `.env`/environment configuration,
discovers matches assigned to this agent, and plays them CONCURRENTLY — one
independent worker process per active match — through
`agent.agent.create_agent()` via the committed supervisor
(`altruagent.supervisor.run_forever_concurrent`). Stops cleanly on Ctrl+C;
does not resign or otherwise touch any match on shutdown.

Claim mode — `python -m agent --claim seatclaim_...` (or `--claim -` to be
prompted without echo, or `ALTRUAGENT_CLAIM_TOKEN` in the environment) —
instead claims exactly ONE seat of a self-hosted tournament test match and
plays it right here, in this process: no API key, no `me()`/`sessions()`
discovery, no supervisor or worker processes. The process itself is the
isolation boundary, so self-play is simply one terminal per seat.
`--agent MODULE[:FACTORY]` picks a different factory than
`agent.agent:create_agent` for that one seat.

This file is deliberately thin — discovery/worker lifecycle lives in
`altruagent.supervisor`, one match's play loop in `altruagent.runner`, and
each worker's own setup in `altruagent.worker`.

IMPORTANT (Windows multiprocessing): the `if __name__ == "__main__":` guard
at the bottom of this file is not just style — `multiprocessing`'s `spawn`
start method (required on Windows, used here unconditionally so behavior is
identical everywhere) re-imports this exact module in every worker process.
Without the guard, each freshly-spawned worker would re-run `main()` itself
and spawn further workers recursively.
"""

from __future__ import annotations

import argparse
import getpass
import importlib
import os
import sys
from typing import Callable

from altruagent.auth import SeatClaimError, SeatGrantAuth
from altruagent.client import AltruAgentClient
from altruagent.errors import AltruAgentError, ConfigurationError
from altruagent.models import DecisionContext, SeatGrant
from altruagent.runner import RunnerError, _resolve_decision_fn, run_game
from altruagent.supervisor import run_forever_concurrent

from . import agent as agent_module


def _resolve_create_agent(module: object) -> Callable:
    """Look up ``create_agent`` on the given agent module. Fails clearly
    (no fallback name, no signature inspection) if it's missing or not
    callable. Deliberately does NOT call it here — construction happens
    once per match, inside that match's own worker process, not once in
    the parent (see altruagent.worker.run_worker).
    """
    create_agent = getattr(module, "create_agent", None)
    if not callable(create_agent):
        raise ValueError(
            "agent/agent.py must define a callable create_agent() function "
            "that returns your decision logic (a function, or an object "
            "exposing choose_action(state, context))."
        )
    return create_agent


def _run_discovery() -> int:
    """The normal workflow: authenticate with the API key, then keep one
    worker process per assigned active match. Unchanged by claim mode.
    """
    try:
        _resolve_create_agent(agent_module)  # validated eagerly; not invoked here
    except ValueError as exc:
        print(f"Startup error: {exc}")
        return 1

    try:
        client = AltruAgentClient()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}")
        print("Copy .env.example to .env and fill in ALTRUAGENT_API_KEY.")
        return 1

    try:
        me = client.me()
    except AltruAgentError as exc:
        print(f"Could not authenticate: {exc}")
        client.close()
        return 1

    if not me.is_claimed:
        print(
            f"Agent '{me.name}' is not claimed yet (status={me.status}). "
            "Have a human claim it with your claim_token before running this."
        )
        client.close()
        return 1

    print(f"Authenticated as agent '{me.name}' ({me.id}).")
    print(
        "Watching for assigned matches — each gets its own process. "
        "Press Ctrl+C to stop.\n"
    )

    try:
        run_forever_concurrent(client, agent_id=me.id)
        return 0
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    except AltruAgentError as exc:
        print(f"\nStopped due to an unrecoverable error: {exc}")
        return 1
    finally:
        client.close()


CLAIM_TOKEN_ENV = "ALTRUAGENT_CLAIM_TOKEN"
_PROMPT = "-"


def _load_agent_factory(spec: str) -> Callable:
    """Import ``MODULE[:FACTORY]`` (``FACTORY`` defaults to ``create_agent``)
    and return the factory without calling it. Raises ``ValueError`` with a
    human-readable message for every way that can go wrong.
    """
    module_name, _, factory_name = spec.partition(":")
    factory_name = factory_name or "create_agent"
    if (
        not module_name
        or not all(part.isidentifier() for part in module_name.split("."))
        or not factory_name.isidentifier()
    ):
        raise ValueError(
            "--agent expects MODULE[:FACTORY] — a dotted module path such as "
            f"examples.messaging_agent or my_agents.v2:build_agent — got {spec!r}."
        )
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name and (module_name == exc.name or module_name.startswith(exc.name + ".")):
            raise ValueError(
                f"Could not find agent module {module_name!r}. Run from the starter's "
                "root directory and pass a dotted module path, not a file path."
            ) from exc
        raise ValueError(f"Importing agent module {module_name!r} failed: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - any import-time failure in contestant code
        raise ValueError(f"Importing agent module {module_name!r} raised {exc!r}.") from exc

    factory = getattr(module, factory_name, None)
    if not callable(factory):
        raise ValueError(
            f"{module_name}:{factory_name} is missing or not callable. It must be a "
            "function returning your decision logic (a function, or an object "
            "exposing choose_action(state, context))."
        )
    return factory


def _resolve_claim_token(arg: str | None, *, prompt: Callable[[str], str]) -> str | None:
    """``--claim TOKEN`` wins; ``--claim -`` prompts without echo; otherwise
    ``ALTRUAGENT_CLAIM_TOKEN`` from the real process environment (never from
    ``.env`` — a claim token is single-use and shouldn't be saved in a file).
    ``None`` means "not claim mode".
    """
    if arg == _PROMPT:
        return prompt("Seat claim token (input hidden): ")
    if arg is not None:
        return arg
    return os.environ.get(CLAIM_TOKEN_ENV) or None


def _describe_seat(grant: SeatGrant) -> list[str]:
    if isinstance(grant.seat_position, int) and isinstance(grant.seat_count, int):
        seat = f"Claimed seat {grant.seat_position + 1}/{grant.seat_count}"
    else:
        seat = "Claimed seat"
    match = f"Match: {grant.match_id or 'unknown'}"
    if grant.match_status == "starting":
        match += " (waiting for the other seats to be claimed)"
    return [seat, f"Game: {grant.game_type or 'unknown'}", match]


def _run_claim(claim_token: str, create_agent: Callable) -> int:
    """Claim one Testing seat and play it to completion in this process."""
    try:
        auth = SeatGrantAuth(claim_token)
    except SeatClaimError as exc:
        print(f"Claim error: {exc}")
        return 1

    try:
        client = AltruAgentClient(auth=auth)
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}")
        print("Claim mode needs only ALTRUAGENT_CONTROL_URL (no API key) — copy .env.example to .env.")
        return 1

    try:
        # Build and check the contestant BEFORE claiming: a claimed seat is
        # bound to this process's in-memory claim key, so a process that
        # fails after claiming can't hand the seat to a fixed-up rerun.
        try:
            contestant = create_agent()
            _resolve_decision_fn(contestant)
        except Exception as exc:  # noqa: BLE001 - contestant code; report it, don't claim
            print(f"Startup error: your agent factory failed, so the seat was not claimed: {exc!r}")
            return 1

        try:
            client.login()  # the claim itself
        except AltruAgentError as exc:
            print(f"Could not claim seat: {exc}")
            return 1

        grant = auth.grant
        for line in _describe_seat(grant):
            print(line)
        print("Connecting to GameAPI... Press Ctrl+C to stop.\n")

        game = client.mcp_game(session_id=grant.game_session_id, game_server_url=grant.gameapi_server_url)
        context = DecisionContext(
            session_id=grant.game_session_id,
            tournament_id=None,
            game_type=grant.game_type,
            agent_id=grant.agent_id,
            seat_position=grant.seat_position,
        )
        final_state = run_game(game, context, contestant)
        print(
            f"Match finished: termination_reason={final_state.termination_reason} "
            f"returns={final_state.returns}"
        )
        return 0
    except KeyboardInterrupt:
        print("\nStopped. This seat stays claimed and can't be claimed again by another process.")
        return 0
    except RunnerError as exc:
        print(f"\nMatch failed: {exc}")
        return 1
    except AltruAgentError as exc:
        print(f"\nStopped due to an unrecoverable error: {exc}")
        return 1
    finally:
        client.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agent",
        description=(
            "With no arguments: play every match assigned to your registered agent "
            "(ALTRUAGENT_API_KEY). With --claim: claim and play one tournament "
            "test-match seat (no API key needed)."
        ),
    )
    parser.add_argument(
        "--claim",
        metavar="TOKEN",
        help=(
            "claim one test-match seat with its seatclaim_... token and play it; "
            f"'-' prompts for the token without echo (or set {CLAIM_TOKEN_ENV})"
        ),
    )
    parser.add_argument(
        "--agent",
        metavar="MODULE[:FACTORY]",
        help="claim mode only: agent factory to use (default: agent.agent:create_agent)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args([] if argv is None else argv)

    claim_token = _resolve_claim_token(args.claim, prompt=getpass.getpass)
    if claim_token is None:
        if args.agent is not None:
            parser.error("--agent is only supported together with --claim")
        return _run_discovery()

    if not claim_token.strip():
        print("Claim error: no seat claim token was given.")
        return 1

    try:
        if args.agent is not None:
            create_agent = _load_agent_factory(args.agent)
        else:
            create_agent = _resolve_create_agent(agent_module)
    except ValueError as exc:
        print(f"Startup error: {exc}")
        return 1

    return _run_claim(claim_token, create_agent)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
