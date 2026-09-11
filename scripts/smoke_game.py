"""Milestone 2 LIVE end-to-end smoke test — developer/manual tool only.

Proves the starter SDK can play a real match against the REAL deployed
AltruAgent platform (Agent_ACP), not a mock. This is not contestant-facing
functionality — it's an integration check for people working on the SDK
itself. It creates real, if disposable, platform state: a temporary second
agent and a temporary two-player competition.

What it does, in order (see `main()`):

1. Uses your existing primary agent (ALTRUAGENT_CONTROL_URL / ALTRUAGENT_API_KEY).
2. Creates a temporary second agent via the real `POST /auth/agent/signup`.
3. Prompts (via getpass — never echoed, stored, or logged) for a human/admin
   bearer token, and uses it to claim the temporary agent
   (`POST /auth/human/claim`) and to create a two-player `tic_tac_toe`
   competition (`POST /admin/competitions/create`).
4. Joins both agents (`POST /competitions/{id}/join`) and polls
   `GET /competitions/{id}` (bounded timeout) until it reports
   `status=in_progress` with a `game_server_url`.
5. Opens a `GameSession` per agent through the starter SDK, fetches the
   initial state, determines whose turn it is from `next_actions`, and
   submits exactly one real legal move.
6. Verifies `move_count` increased, then resigns via the normal participant
   `POST /games/{id}/resign` endpoint (never the unauthenticated
   `/games/{id}/cancel`) to leave the human's hosting slot free again.

The temporary agent is left claimed — the current backend has no safe
agent-deletion endpoint, so this script does not invent one.

Run:
    python scripts/smoke_game.py
"""

from __future__ import annotations

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
    deadline = now() + timeout_seconds
    last = poll()
    while True:
        if last.get("status") == "in_progress" and last.get("game_server_url"):
            return last
        if now() >= deadline:
            _fail(
                "Competition did not reach in_progress with a game_server_url "
                f"within {timeout_seconds:.0f}s (last status: {last.get('status')!r})."
            )
        sleep(interval_seconds)
        last = poll()


def main() -> int:
    print("=== Milestone 2 LIVE smoke test ===")
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

        primary_session = primary.game(session_id=session_id, game_server_url=game_server_url)
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
            print("[OK] cleanup: match ended via POST /games/{session_id}/resign")
        except AltruAgentError as exc:
            print(f"[WARN] cleanup resign failed (non-fatal): {exc}")
        cleaned_up = True

        print(
            f"\n[NOTE] Temporary agent '{name}' remains claimed — the current backend "
            "has no safe agent-deletion endpoint, so it was not deleted."
        )

        print("\nMILESTONE 2 LIVE SMOKE TEST PASSED")
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
