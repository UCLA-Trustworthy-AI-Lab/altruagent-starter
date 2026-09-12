"""Milestone 2 + 3A/3B LIVE end-to-end smoke test — developer/manual tool only.

Proves the starter SDK can play a real match against the REAL deployed
AltruAgent platform (Agent_ACP), not a mock. This is not contestant-facing
functionality — it's an integration check for people working on the SDK
itself. It creates real, if disposable, platform state: a temporary second
agent and a temporary two-player competition or tournament.

Two modes, sharing all of their setup/cleanup machinery:

**Default (standalone competition, Milestones 2 + 3A):**

1. Uses your existing primary agent (ALTRUAGENT_CONTROL_URL / ALTRUAGENT_API_KEY).
2. Creates a temporary second agent via the real `POST /auth/agent/signup`.
3. Prompts (via getpass — never echoed, stored, or logged) for a human/admin
   bearer token, and uses it to claim the temporary agent
   (`POST /auth/human/claim`) and to create a two-player `tic_tac_toe`
   competition (`POST /admin/competitions/create`).
4. Joins both agents (`POST /competitions/{id}/join`) and polls
   `GET /competitions/{id}` (bounded timeout) until it reports
   `status=in_progress` with a `game_server_url`.
5. Milestone 3A check: calls `primary.sessions()` (`GET /agents/me/sessions`),
   confirms the new competition shows up in `.active` with the right
   `status`/`game_type`, and confirms its `game_server_url` starts out
   unresolved (this endpoint never includes it). Then opens the primary's
   `GameSession` via `discovered_match.game()` — the lazy-resolution path
   (`GET /competitions/{id}` -> cache on the `Match` -> `GameSession`) —
   instead of `client.game(...)` directly, and confirms the URL is now
   cached and normalized.
6. Fetches the initial state, determines whose turn it is from
   `next_actions`, and submits exactly one real legal move.
7. Verifies `move_count` increased, then resigns via the normal participant
   `POST /games/{id}/resign` endpoint (never the unauthenticated
   `/games/{id}/cancel`) to leave the human's hosting slot free again.

**`--tournament` (Milestone 3B):** identical agent setup (steps 1-3 above,
substituting `POST /admin/tournaments/create` for the competition create
call), then:

4. Joins both agents via the new `client.join_tournament(tournament_id)`
   (not the low-level competition-join path) and confirms the second join
   causes (or already sees) the tournament reach `in_progress`.
5. Uses `primary.sessions()` to find the *tournament-spawned* child match by
   `tournament_id` (not by an already-known `session_id` — that's the thing
   this mode proves that the default mode doesn't), then resolves it via
   `match.game()` exactly as before.
6-7. Same play-one-move / verify / resign as the default mode.
8. Best-effort (single, non-looping) check of the tournament's final status
   after cleanup — not a fragile polling loop.

The temporary agent is left claimed — the current backend has no safe
agent-deletion endpoint, so this script does not invent one.

Run:
    python scripts/smoke_game.py
    python scripts/smoke_game.py --tournament
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time
import uuid
from pathlib import Path
from typing import Callable

# Allow running this script directly without having pip-installed the project.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from altruagent.client import AltruAgentClient  # noqa: E402
from altruagent.errors import AltruAgentError, ConfigurationError  # noqa: E402

COMPETITION_GAME_TYPE = "tic_tac_toe"
DEFAULT_POLL_TIMEOUT_SECONDS = 60.0
DEFAULT_POLL_INTERVAL_SECONDS = 3.0
HTTP_TIMEOUT_SECONDS = 10.0


class SmokeTestError(RuntimeError):
    """Raised for any smoke-test failure. Message is always safe to print —
    never include secrets when raising this."""


def _checkpoint(label: str) -> None:
    print(f"[OK] {label}")


def _fail(message: str) -> None:
    raise SmokeTestError(message)


def _json_or_fail(response: httpx.Response, what: str) -> dict:
    try:
        body = response.json()
    except ValueError:
        body = None
    if response.status_code >= 400:
        detail = None
        if isinstance(body, dict):
            detail = body.get("detail") or body.get("error")
        detail = detail or (response.text[:300] if response.text else None)
        _fail(f"{what} failed (HTTP {response.status_code}): {detail}")
    if not isinstance(body, dict):
        _fail(f"{what} returned a non-JSON or unexpected response body.")
    return body


def signup_temporary_agent(http: httpx.Client, control_url: str) -> tuple[str, str, str]:
    """POST /auth/agent/signup. Returns (name, api_key, claim_token).

    Verified against Agent_ACP/backend/src/index.ts:73 and
    services/agentService.ts:20 (registerAgent).
    """
    name = f"smoke-test-{uuid.uuid4().hex[:10]}"
    response = http.post(
        f"{control_url}/auth/agent/signup",
        json={"name": name, "description": "Milestone 2 smoke-test opponent (safe to ignore/delete)"},
    )
    body = _json_or_fail(response, "temporary agent signup")
    api_key = body.get("api_key")
    claim_token = body.get("claim_token")
    if not api_key or not claim_token:
        _fail("Signup response did not include both api_key and claim_token.")
    return name, api_key, claim_token


def claim_temporary_agent(
    http: httpx.Client, control_url: str, claim_token: str, human_token: str
) -> None:
    """POST /auth/human/claim, authenticated as a human.

    Verified against Agent_ACP/backend/src/index.ts:194.
    """
    response = http.post(
        f"{control_url}/auth/human/claim",
        json={"claim_token": claim_token},
        headers={"Authorization": f"Bearer {human_token}"},
    )
    _json_or_fail(response, "claiming the temporary agent")


def create_competition(http: httpx.Client, control_url: str, human_token: str) -> str:
    """POST /admin/competitions/create. Returns session_id.

    Verified against Agent_ACP/backend/src/index.ts:684 — a plain
    {game_type, max_participants} body needs no preset (presets only exist
    for repeated_pd/avalon). Requires an authenticated human under their
    active-hosting cap (backend/src/services/hostingService.ts, default 1).
    """
    response = http.post(
        f"{control_url}/admin/competitions/create",
        json={"game_type": COMPETITION_GAME_TYPE, "max_participants": 2},
        headers={"Authorization": f"Bearer {human_token}"},
    )
    body = _json_or_fail(response, "competition creation")
    session_id = body.get("session_id")
    if not session_id:
        _fail("Competition creation response did not include session_id.")
    return session_id


def join_competition(client: AltruAgentClient, session_id: str) -> None:
    """POST /competitions/{session_id}/join for the given agent."""
    client.request("POST", f"/competitions/{session_id}/join")


def create_tournament(http: httpx.Client, control_url: str, human_token: str) -> str:
    """POST /admin/tournaments/create. Returns tournament_id.

    Verified against Agent_ACP/backend/src/index.ts:758 — only `game_type`
    and `max_participants` are currently required (unlike competitions,
    tournaments have no preset system at all). Same hosting-cap rules as
    `create_competition` apply (competitions + tournaments share one active
    per-user hosting count, backend/src/services/hostingService.ts).
    """
    response = http.post(
        f"{control_url}/admin/tournaments/create",
        json={"game_type": COMPETITION_GAME_TYPE, "max_participants": 2},
        headers={"Authorization": f"Bearer {human_token}"},
    )
    body = _json_or_fail(response, "tournament creation")
    tournament_id = body.get("tournament_id")
    if not tournament_id:
        _fail("Tournament creation response did not include tournament_id.")
    return tournament_id


def _poll_until(
    poll: Callable[[], dict],
    is_ready: Callable[[dict], bool],
    *,
    timeout_seconds: float,
    interval_seconds: float,
    sleep: Callable[[float], None],
    now: Callable[[], float],
    timeout_message: Callable[[dict], str],
) -> dict:
    """Shared bounded-polling primitive. Polls `poll()` immediately, then
    again every `interval_seconds`, until `is_ready(last)` is true or
    `timeout_seconds` has elapsed — never loops forever. `sleep`/`now` are
    injectable so callers can be unit-tested without real wall-clock waiting.
    """
    deadline = now() + timeout_seconds
    last = poll()
    while True:
        if is_ready(last):
            return last
        if now() >= deadline:
            _fail(timeout_message(last))
        sleep(interval_seconds)
        last = poll()


def wait_for_in_progress(
    poll: Callable[[], dict],
    *,
    timeout_seconds: float = DEFAULT_POLL_TIMEOUT_SECONDS,
    interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> dict:
    """Poll `poll()` (returns a competition dict) until it reports
    status=in_progress with a game_server_url, or raise SmokeTestError once
    `timeout_seconds` has elapsed. Bounded — never loops forever.

    `sleep`/`now` are injectable so this can be unit-tested without real
    wall-clock waiting (see tests/test_smoke_game.py).
    """
    return _poll_until(
        poll,
        lambda last: last.get("status") == "in_progress" and bool(last.get("game_server_url")),
        timeout_seconds=timeout_seconds,
        interval_seconds=interval_seconds,
        sleep=sleep,
        now=now,
        timeout_message=lambda last: (
            "Competition did not reach in_progress with a game_server_url "
            f"within {timeout_seconds:.0f}s (last status: {last.get('status')!r})."
        ),
    )


def wait_for_tournament_in_progress(
    poll: Callable[[], dict],
    *,
    timeout_seconds: float = DEFAULT_POLL_TIMEOUT_SECONDS,
    interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> dict:
    """Poll `poll()` (returns a tournament dict, e.g. the `"tournament"` key
    of `GET /tournaments/{id}`'s response body) until status=in_progress, or
    raise SmokeTestError once `timeout_seconds` has elapsed.

    Unlike `wait_for_in_progress`, this does not wait for a `game_server_url`
    on the tournament itself — the actual child match's URL is resolved
    separately, via `client.sessions()` + `Match.game()`, not read off the
    tournament object.
    """
    return _poll_until(
        poll,
        lambda last: last.get("status") == "in_progress",
        timeout_seconds=timeout_seconds,
        interval_seconds=interval_seconds,
        sleep=sleep,
        now=now,
        timeout_message=lambda last: (
            f"Tournament did not reach in_progress within {timeout_seconds:.0f}s "
            f"(last status: {last.get('status')!r})."
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tournament",
        action="store_true",
        help=(
            "Run the tournament-registration smoke test (Milestone 3B) instead "
            "of the default standalone-competition one (Milestones 2 + 3A)."
        ),
    )
    args = parser.parse_args()

    mode_label = "tournament" if args.tournament else "standalone competition"
    print(f"=== Milestone 2 + 3A/3B LIVE smoke test ({mode_label} mode) ===")
    print("This calls the REAL deployed AltruAgent platform. It is a developer")
    print("diagnostic tool, not contestant-facing functionality.\n")

    try:
        primary = AltruAgentClient()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}")
        return 1

    http = httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)
    opponent: AltruAgentClient | None = None
    primary_session = None
    tournament_id: str | None = None
    cleaned_up = False

    try:
        primary_agent = primary.me()
        if not primary_agent.is_claimed:
            _fail(
                f"Primary agent '{primary_agent.name}' is not claimed "
                f"(status={primary_agent.status}). Claim it before running this test."
            )
        _checkpoint(f"primary agent authenticated ({primary_agent.name})")

        control_url = primary.control_url
        name, opp_api_key, claim_token = signup_temporary_agent(http, control_url)

        human_token = getpass.getpass(
            "Human/admin bearer token (used only for this run — never stored, "
            "logged, or echoed): "
        )
        if not human_token:
            _fail("A human/admin bearer token is required to claim the temporary agent.")

        claim_temporary_agent(http, control_url, claim_token, human_token)
        opponent = AltruAgentClient(control_url=control_url, api_key=opp_api_key)
        opponent_agent = opponent.me()
        if not opponent_agent.is_claimed:
            _fail(f"Temporary agent '{name}' was not claimed successfully.")
        _checkpoint(f"temporary opponent created and claimed ({name})")

        if args.tournament:
            tournament_id = create_tournament(http, control_url, human_token)
            _checkpoint(f"tournament created (tournament_id={tournament_id})")

            primary.join_tournament(tournament_id)
            opponent.join_tournament(tournament_id)
            _checkpoint("both agents joined tournament (via client.join_tournament)")

            wait_for_tournament_in_progress(
                lambda: primary.request("GET", f"/tournaments/{tournament_id}")["tournament"]
            )
            _checkpoint("tournament reached in_progress")

            # Milestone 3B: find the tournament-spawned child match by
            # tournament_id — NOT by an already-known session_id. That's the
            # thing this mode proves that the default mode doesn't.
            discovered_match = next(
                (m for m in primary.sessions().active if m.tournament_id == tournament_id),
                None,
            )
            if discovered_match is None:
                _fail(
                    f"client.sessions() did not list an active match with "
                    f"tournament_id={tournament_id!r}."
                )
            session_id = discovered_match.session_id
            _checkpoint(
                f"active match discovered via client.sessions() (session_id={session_id})"
            )

            primary_session = discovered_match.game()
            _checkpoint("GameAPI URL resolved lazily via match.game()")

            # The opponent reuses the already-resolved, normalized URL rather
            # than repeating its own sessions()+game() lookup purely for its
            # own convenience — see check_game.py's script docstring for the
            # same "opponent may use its existing construction" pattern.
            opponent_session = opponent.game(
                session_id=session_id, game_server_url=primary_session.game_server_url
            )
        else:
            session_id = create_competition(http, control_url, human_token)
            _checkpoint(f"competition created (session_id={session_id})")

            join_competition(primary, session_id)
            join_competition(opponent, session_id)
            _checkpoint("both agents joined")

            competition = wait_for_in_progress(
                lambda: primary.request("GET", f"/competitions/{session_id}")
            )
            game_server_url = competition["game_server_url"]
            _checkpoint(f"GameAPI session started (game_server_url={game_server_url})")

            # Milestone 3A: verify discovery finds this same match before doing
            # anything else with GameAPI for the primary agent.
            discovered_match = next(
                (m for m in primary.sessions().active if m.session_id == session_id), None
            )
            if discovered_match is None:
                _fail(
                    f"client.sessions() did not list session_id={session_id!r} in "
                    "active_sessions after the competition started."
                )
            if discovered_match.status != "in_progress" or discovered_match.game_type != COMPETITION_GAME_TYPE:
                _fail(
                    "Discovered match has unexpected fields (status="
                    f"{discovered_match.status!r}, game_type={discovered_match.game_type!r})."
                )
            if discovered_match.game_server_url is not None:
                _fail(
                    "Expected the freshly discovered Match to have no resolved "
                    f"game_server_url yet, but got {discovered_match.game_server_url!r} — "
                    "GET /agents/me/sessions is not expected to include one."
                )
            _checkpoint("active match discovered via client.sessions()")

            # Lazy-resolution path: Match.game() -> GET /competitions/{id} ->
            # game_server_url -> GameSession. Deliberately NOT client.game(...)
            # directly for the primary agent — that's the thing this step proves.
            primary_session = discovered_match.game()
            if not discovered_match.game_server_url:
                _fail("discovered_match.game_server_url was not populated after match.game().")
            if not primary_session.game_server_url.startswith(("http://", "https://")):
                _fail(
                    "GameSession.game_server_url is not a normalized URL: "
                    f"{primary_session.game_server_url!r}"
                )
            _checkpoint("GameAPI URL resolved lazily via match.game()")

            opponent_session = opponent.game(session_id=session_id, game_server_url=game_server_url)

        primary_state = primary_session.state()
        _checkpoint("initial state fetched")
        mover_name = primary_state.current_player.name if primary_state.current_player else None
        print(
            f"    game_name={primary_state.game_name} phase={primary_state.phase} "
            f"current_player={mover_name} move_count={primary_state.move_count}"
        )

        if mover_name == primary_agent.name:
            mover_session, mover_state = primary_session, primary_state
        elif mover_name == opponent_agent.name:
            mover_session, mover_state = opponent_session, opponent_session.state()
        else:
            _fail(f"Could not determine which agent should move (current_player={mover_name!r}).")

        move_action_kinds = {a.action for a in mover_state.next_actions}
        if "make_move" not in move_action_kinds or not mover_state.legal_actions:
            _fail(
                "Mover's next_actions does not include make_move, or legal_actions is "
                f"empty (next_actions={sorted(move_action_kinds)}, "
                f"legal_actions={mover_state.legal_actions})."
            )

        action = mover_state.legal_actions[0]
        before_move_count = mover_state.move_count
        mover_session.step(action)
        _checkpoint(f"legal move submitted (action={action} by {mover_name})")

        refreshed_state = primary_session.state()
        if refreshed_state.move_count <= before_move_count:
            _fail(
                "move_count did not increase after the step "
                f"(before={before_move_count}, after={refreshed_state.move_count})."
            )
        _checkpoint(f"updated state fetched (move_count={refreshed_state.move_count})")

        try:
            if not refreshed_state.is_terminal:
                primary_session.resign()
            print("[OK] cleanup: match ended via resign")
        except AltruAgentError as exc:
            print(f"[WARN] cleanup resign failed (non-fatal): {exc}")
        cleaned_up = True

        if args.tournament:
            # Best-effort, single check — not a poll loop. GameAPI reports
            # the match result to the backend as a fire-and-forget background
            # task, so it may not have landed yet; that's expected, not a
            # failure of this test.
            try:
                final_tournament = primary.tournament(tournament_id)
                print(f"[INFO] tournament status after cleanup: {final_tournament.status}")
            except AltruAgentError as exc:
                print(f"[INFO] could not fetch final tournament status (non-fatal): {exc}")

        print(
            f"\n[NOTE] Temporary agent '{name}' remains claimed — the current backend "
            "has no safe agent-deletion endpoint, so it was not deleted."
        )

        if args.tournament:
            print("\nMILESTONE 3B LIVE TOURNAMENT SMOKE TEST PASSED")
        else:
            print("\nMILESTONE 3A LIVE SMOKE TEST PASSED")
        return 0

    except (SmokeTestError, AltruAgentError) as exc:
        print(f"\nSMOKE TEST FAILED: {exc}")
        return 1
    finally:
        if not cleaned_up and primary_session is not None:
            try:
                state = primary_session.state()
                if not state.is_terminal:
                    primary_session.resign()
                    print("[OK] cleanup (failure path): match resigned")
            except AltruAgentError:
                pass
        primary.close()
        if opponent is not None:
            opponent.close()
        http.close()


if __name__ == "__main__":
    raise SystemExit(main())
