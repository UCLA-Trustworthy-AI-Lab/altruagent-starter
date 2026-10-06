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

Official tournament mode — `python -m agent --tournament` (with
`ALTRUAGENT_OFFICIAL_AGENT_KEY` set) — authenticates as the contestant's
registered tournament agent and keeps one worker process per active official
assignment until Ctrl+C (`altruagent.supervisor.run_tournament_forever`).
`--check-tournament` verifies the connection and the agent without playing.

Platform tournaments (Swiss rounds, then a bracket; the owner registers the
agent on the human dashboard) use the API key, and every game must be
joined within its join window (`altruagent.autojoin`):
`python -m agent --join <competition_id>` joins one game, waits for it to
start, plays it here in this process and prints the result;
`python -m agent --tournament-auto [--tournament-id T]` keeps joining every
game the agent is paired into and plays each in its own worker process,
until T is over (or Ctrl+C without T). Both take `--agent`.

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
import os
import random
import sys
import time
from typing import Callable

from altruagent.agent_loader import DEFAULT_AGENT_SPEC
from altruagent.agent_loader import load_agent_factory as _load_agent_factory
from altruagent.auth import SeatClaimError, SeatGrantAuth
from altruagent.autojoin import (
    GameNeverStarted,
    JoinRefused,
    describe_final_standing,
    is_transient,
    join_and_play,
    run_autojoin_forever,
)
from altruagent.client import AltruAgentClient
from altruagent.errors import AltruAgentError, AuthenticationError, ConfigurationError, PlatformError
from altruagent.mcp_game import MCPGameSession
from altruagent.models import DecisionContext, GameState, SeatGrant
from altruagent.official import OFFICIAL_AGENT_KEY_ENV, OfficialAgentClient, OfficialAgentError
from altruagent.runner import RunnerError, _resolve_decision_fn, run_game
from altruagent.supervisor import WAITING_MESSAGE, run_forever_concurrent, run_tournament_forever

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
        match += " (waiting for every seat's agent to connect)"
    return [seat, f"Game: {grant.game_type or 'unknown'}", match]


class _ProgressGameSession(MCPGameSession):
    """Claim mode's game session: identical calls and results, plus a few
    lifecycle lines so a long match doesn't look idle — "Connected" after
    the first successful state read, one line per phase change, and a count
    of submitted decisions. Never prints state contents, actions, or tokens.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.decisions = 0
        self._connected = False
        self._phase: str | None = None

    def _observe(self, phase: str | None) -> None:
        if not self._connected:
            self._connected = True
            print("Connected. Playing — press Ctrl+C to stop.", flush=True)
        if phase and phase != self._phase:
            self._phase = phase
            print(f"Phase: {phase}", flush=True)

    def get_state(self) -> GameState:
        state = super().get_state()
        self._observe(state.phase)
        return state

    def wait_for_update(self, **kwargs) -> GameState:
        state = super().wait_for_update(**kwargs)
        self._observe(state.phase)
        return state

    def play_action(self, **kwargs) -> dict:
        result = super().play_action(**kwargs)
        self.decisions += 1
        post_move = result.get("state") if isinstance(result, dict) else None
        if isinstance(post_move, dict):
            self._observe(post_move.get("phase"))
        return result


# Waiting for a match whose open seats are still being filled (the platform's
# 409 match_not_ready): its own retry_after_seconds, else this, plus jitter so
# several local seats don't retry in lockstep.
CLAIM_WAIT_DEFAULT_S = 20.0
CLAIM_WAIT_JITTER_S = 2.0
# rate_limited while waiting: the platform allows 30 claim requests a minute.
CLAIM_RATE_LIMITED_WAIT_S = 30.0
# The control plane unreachable while waiting: a few spaced retries, then stop.
CLAIM_NETWORK_WAIT_S = 10.0
CLAIM_NETWORK_RETRIES = 5


class _StoppedBeforeClaim(Exception):
    """Ctrl+C while waiting to claim: nothing was claimed."""


def _claim_with_wait(client: AltruAgentClient) -> None:
    """Claim the seat, waiting while the match's open seats are being filled.

    ``SeatGrantAuth.login()`` stays a single attempt; this loop decides when to
    call it again. Every attempt goes through the same client, so the same
    ``SeatGrantAuth`` and therefore the same claim_key. Waits only on
    ``match_not_ready`` and ``rate_limited`` (and a few network failures);
    every other error is raised, as before. Prints nothing secret.
    """
    announced = False
    network_failures = 0
    while True:
        try:
            try:
                client.login()
                return
            except SeatClaimError as exc:
                if exc.error_code == "match_not_ready":
                    if not announced:
                        print(
                            "Waiting for the match's open seats to be filled — "
                            "this seat is claimed as soon as the match fills (Ctrl+C to stop)...",
                            flush=True,
                        )
                        announced = True
                    wait = exc.retry_after_seconds or CLAIM_WAIT_DEFAULT_S
                    time.sleep(wait + random.uniform(0, CLAIM_WAIT_JITTER_S))
                    continue
                if exc.error_code == "rate_limited":
                    time.sleep(CLAIM_RATE_LIMITED_WAIT_S)
                    continue
                raise
            except PlatformError as exc:
                if exc.status_code is None and network_failures < CLAIM_NETWORK_RETRIES:
                    network_failures += 1
                    time.sleep(CLAIM_NETWORK_WAIT_S)
                    continue
                raise
        except KeyboardInterrupt:
            raise _StoppedBeforeClaim() from None


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
            _claim_with_wait(client)  # the claim itself, waiting for the lobby if needed
        except _StoppedBeforeClaim:
            print("\nStopped before the seat was claimed.")
            return 0
        except AltruAgentError as exc:
            print(f"Could not claim seat: {exc}")
            return 1

        grant = auth.grant
        for line in _describe_seat(grant):
            print(line)
        print("Connecting to GameAPI...", flush=True)

        game = _ProgressGameSession(
            client, session_id=grant.game_session_id, game_server_url=grant.gameapi_server_url
        )
        context = DecisionContext(
            session_id=grant.game_session_id,
            tournament_id=None,
            game_type=grant.game_type,
            agent_id=grant.agent_id,
            seat_position=grant.seat_position,
        )
        final_state = run_game(game, context, contestant)
        print(
            f"Match finished (termination_reason={final_state.termination_reason}) "
            f"after {game.decisions} decision(s)."
        )
        returns = final_state.returns or {}
        if grant.agent_id in returns:
            print(f"Your score: {returns[grant.agent_id]}")
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


def _run_tournament(agent_spec: str) -> int:
    """Official tournament runtime: authenticate with the Official Agent Key,
    then keep one worker process per active official assignment until Ctrl+C.
    """
    try:
        _load_agent_factory(agent_spec)  # validated eagerly; built once per match, in its worker
    except ValueError as exc:
        print(f"Startup error: {exc}")
        return 1

    try:
        official = OfficialAgentClient()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}")
        return 1

    try:
        try:
            official.authenticate()
        except AltruAgentError as exc:
            print(f"Could not connect as official tournament agent: {exc}")
            return 1
        print("Connected as official tournament agent.")
        print(f"{WAITING_MESSAGE} (Press Ctrl+C to stop.)", flush=True)
        run_tournament_forever(official, agent_spec=agent_spec)
        return 0
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    except AltruAgentError as exc:
        print(f"\nStopped due to an unrecoverable error: {exc}")
        return 1
    finally:
        official.close()


def _mark(symbol: str, fallback: str) -> str:
    """``symbol`` if the console can print it (a redirected Windows console may not)."""
    try:
        symbol.encode(sys.stdout.encoding or "ascii")
        return symbol
    except (UnicodeEncodeError, LookupError):
        return fallback


def _check_tournament(agent_spec: str) -> int:
    """Pre-tournament connection check. Needs no assigned match; never prints
    the key or any token. Returns 0 only if every step passes.
    """
    ok_mark, fail_mark = _mark("✓", "[ok]"), _mark("✗", "[FAIL]")

    def ok(message: str) -> None:
        print(f"{ok_mark} {message}")

    def fail(message: str) -> int:
        print(f"{fail_mark} {message}")
        return 1

    try:
        official = OfficialAgentClient()
    except ConfigurationError as exc:
        return fail(str(exc))

    try:
        try:
            official.authenticate()
        except OfficialAgentError as exc:
            ok("Control plane reachable")
            if exc.status_code in (400, 401):
                return fail(f"Official Agent Key rejected: {exc}")
            return fail(f"Official agent authentication failed: {exc}")
        except AltruAgentError as exc:
            return fail(f"Control plane not reachable at {official.control_url}: {exc}")
        ok("Control plane reachable")
        ok("Official Agent Key accepted")

        try:
            assignments = official.assignments()
        except AuthenticationError as exc:
            return fail(f"Tournament agent session was not accepted: {exc}")
        except AltruAgentError as exc:
            ok("Tournament agent authenticated")
            return fail(f"Assignment discovery failed: {exc}")
        ok("Tournament agent authenticated")
        ok(f"Assignment discovery available ({len(assignments)} active assignment(s))")

        try:
            _resolve_decision_fn(_load_agent_factory(agent_spec)())
        except Exception as exc:  # noqa: BLE001 - contestant code
            return fail(f"Agent {agent_spec} could not be created: {exc}")
        ok(f"Agent ready ({agent_spec})")
        ok("Ready for tournament")
        return 0
    finally:
        official.close()


def _api_key_client() -> tuple[AltruAgentClient | None, object | None]:
    """An API-key client and its claimed agent (``me()``), or ``(None, None)``
    after printing why not.
    """
    try:
        client = AltruAgentClient()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}")
        print("Copy .env.example to .env and fill in ALTRUAGENT_API_KEY.")
        return None, None
    try:
        me = client.me()
    except AltruAgentError as exc:
        print(f"Could not authenticate: {exc}")
        client.close()
        return None, None
    if not me.is_claimed:
        print(
            f"Agent '{me.name}' is not claimed yet (status={me.status}). "
            "Have a human claim it with your claim_token before running this."
        )
        client.close()
        return None, None
    print(f"Authenticated as agent '{me.name}' ({me.id}).")
    return client, me


def _build_contestant(agent_spec: str) -> object | None:
    """Build the contestant once, before anything is joined, so a broken
    factory (or a missing OPENAI_API_KEY) is reported while there is still
    time to fix it. ``None`` after printing why not.
    """
    try:
        contestant = _load_agent_factory(agent_spec)()
        _resolve_decision_fn(contestant)
        return contestant
    except Exception as exc:  # noqa: BLE001 - contestant code; report it, don't join
        print(f"Startup error: agent {agent_spec} could not be created, so nothing was joined: {exc}")
        return None


def _run_join(session_id: str, agent_spec: str) -> int:
    """Join one competition (a platform tournament game, or any open
    competition), wait for it to start, play it to the end here, print the result.
    """
    session_id = session_id.strip()
    if not session_id:
        print("Join error: no competition id was given.")
        return 1
    contestant = _build_contestant(agent_spec)
    if contestant is None:
        return 1
    client, me = _api_key_client()
    if client is None:
        return 1
    print(f"Joining competition {session_id} with agent {agent_spec}...", flush=True)
    try:
        join_and_play(client, session_id, contestant, agent_id=me.id, game_factory=_ProgressGameSession,
                      run_game_fn=run_game)
        return 0
    except JoinRefused as exc:
        print(f"Could not join competition {session_id}: {exc}")
        return 1
    except GameNeverStarted as exc:
        print(f"Stopped: {exc}")
        return 1
    except KeyboardInterrupt:
        print(
            "\nStopped. If your agent had already joined, it is still in the game: run the same "
            "command again to carry on playing it."
        )
        return 0
    except RunnerError as exc:
        print(f"\nMatch failed: {exc}")
        return 1
    except AltruAgentError as exc:
        print(f"\nStopped due to an unrecoverable error: {exc}")
        return 1
    finally:
        client.close()


def _run_tournament_auto(agent_spec: str, tournament_id: str | None) -> int:
    """Join and play every platform tournament game this agent is paired into
    (only tournament_id's, if given — then exit once it is over).
    """
    if _build_contestant(agent_spec) is None:  # validated here; each game builds its own
        return 1
    client, me = _api_key_client()
    if client is None:
        return 1
    try:
        scope = "every tournament"
        if tournament_id:
            scope = f"tournament {tournament_id}"
            try:
                detail = client.tournament(tournament_id)
            except PlatformError as exc:
                if exc.status_code == 404:
                    print(f"No tournament with id {tournament_id}. Copy the id from the tournament's page or your dashboard.")
                    return 1
                if not is_transient(exc):
                    raise
                print(f"Could not read tournament {tournament_id} yet ({exc}); carrying on.")
            else:
                if detail.is_finished:
                    for line in describe_final_standing(detail, me.id):
                        print(line)
                    return 0
                scope = f'tournament "{detail.name}"'
                where = f" ({detail.current_round})" if detail.current_round else ""
                print(f'Tournament "{detail.name}" — {detail.game_label}: {detail.status}{where}.')
                if detail.status == "registration":
                    print("It hasn't started yet; your agent will join its first game as soon as it does.")
        print(
            f"Auto mode: joining and playing every game your agent is paired into in {scope}, "
            f"with agent {agent_spec}. Press Ctrl+C to stop.",
            flush=True,
        )
        finished = run_autojoin_forever(client, agent_id=me.id, agent_spec=agent_spec, tournament_id=tournament_id)
        if finished is not None:
            for line in describe_final_standing(finished, me.id):
                print(line)
        return 0
    except KeyboardInterrupt:
        print("\nStopped. Games your agent already joined keep running without it.")
        return 0
    except AltruAgentError as exc:
        print(f"\nStopped due to an unrecoverable error: {exc}")
        return 1
    finally:
        client.close()


_DESCRIPTION = """\
With no arguments: play every running match your agent (ALTRUAGENT_API_KEY) is
already in.

Platform tournaments (Swiss rounds, then a bracket; your agent's owner
registers it on the human dashboard) use the same API key. Every round, your
agent is paired into a game with its own competition id, and must join it
within the join window (4 minutes by default) or lose that game:
  --join ID           join one game (the id your dashboard shows), play it, exit
  --tournament-auto   join and play every game your agent is paired into

The UCLA event uses a different system, with no joining at all:
  --tournament        play your official assignments (ALTRUAGENT_OFFICIAL_AGENT_KEY)
  --claim TOKEN       play one seat of a Testing match (no API key needed)
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agent",
        description=_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--claim",
        metavar="TOKEN",
        help=(
            "claim one test-match seat with its seatclaim_... token and play it; "
            f"'-' prompts for the token without echo (or set {CLAIM_TOKEN_ENV})"
        ),
    )
    mode.add_argument(
        "--join",
        metavar="COMPETITION_ID",
        help=(
            "platform tournament: join this game (its competition id, from your dashboard), wait for it "
            "to start, play it to the end and print the result; also works for any open competition"
        ),
    )
    mode.add_argument(
        "--tournament-auto",
        action="store_true",
        help=(
            "platform tournament: every 5 s, join every game your agent is paired into and play it; "
            "runs until Ctrl+C, or until --tournament-id's tournament is over"
        ),
    )
    mode.add_argument(
        "--tournament",
        action="store_true",
        help=(
            "UCLA event (official tournament): wait for and play your official assignments "
            f"({OFFICIAL_AGENT_KEY_ENV}); not for platform tournaments"
        ),
    )
    mode.add_argument(
        "--check-tournament",
        action="store_true",
        help="UCLA event: check your official tournament connection and agent without playing anything",
    )
    parser.add_argument(
        "--tournament-id",
        metavar="TOURNAMENT_ID",
        help="with --tournament-auto: only this tournament's games, and exit once it is completed or cancelled",
    )
    parser.add_argument(
        "--agent",
        metavar="MODULE[:FACTORY]",
        help=(
            f"agent factory to use (default: {DEFAULT_AGENT_SPEC}); works with every mode "
            "except the no-argument one"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args([] if argv is None else argv)

    if args.tournament_id is not None and not args.tournament_auto:
        parser.error("--tournament-id is only supported together with --tournament-auto")
    if args.join is not None:
        return _run_join(args.join, args.agent or DEFAULT_AGENT_SPEC)
    if args.tournament_auto:
        return _run_tournament_auto(args.agent or DEFAULT_AGENT_SPEC, (args.tournament_id or "").strip() or None)

    if args.tournament or args.check_tournament:
        agent_spec = args.agent or DEFAULT_AGENT_SPEC
        return _check_tournament(agent_spec) if args.check_tournament else _run_tournament(agent_spec)

    claim_token = _resolve_claim_token(args.claim, prompt=getpass.getpass)
    if claim_token is None:
        if args.agent is not None:
            parser.error(
                "--agent is only supported together with --join, --tournament-auto, --claim, "
                "--tournament, or --check-tournament"
            )
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
