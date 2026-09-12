"""Milestone 5 FINAL ACCEPTANCE TEST — developer/manual tool only. NOT RUN
as part of `pytest` or CI; invoke it by hand, deliberately, against the real
deployed platform.

Proves the messaging-enabled contestant workflow works through the actual
contestant-facing entry point, `python -m agent` — never by calling
`run_match`/`run_game`/`run_once_concurrent` directly (those are proven at
the unit level in tests/test_runner.py; this script's whole point is
end-to-end confidence in the real command a contestant actually runs).

Two scenarios, each played out with two *real, separate* `python -m agent`
OS subprocesses (one per player) against one shared `repeated_pd`
competition (messaging enabled by the platform's own default preset — see
Agent_ACP/backend/src/services/competitionPresets.ts):

  A. Both subprocesses run `examples/basic_agent.py`'s content — no
     `choose_message` defined anywhere. Proves the default
     auto-`TERMINATE_MESSAGING` behavior alone is enough to finish a
     messaging-enabled match, with zero contestant messaging code.

  B. Both subprocesses run `examples/messaging_agent.py`'s content — a
     stateful agent that actually calls `SendMessage`. Proves the real
     `send_message` wire path (not just `terminate_messaging`) works
     end-to-end through the real runtime.

Running two independent `python -m agent` processes at once (one per
player) also exercises them concurrently — a weaker, more externally
realistic form of the concurrency Milestone 4C's own smoke test already
proved at the single-agent/multi-match level (`--concurrent` in
scripts/smoke_game.py); this script does not re-prove that internal
mechanism, to keep this test focused on messaging correctness.

Setup mirrors scripts/smoke_game.py: your existing primary agent
(ALTRUAGENT_CONTROL_URL/ALTRUAGENT_API_KEY) plus one temporary signed-up-
and-claimed opponent agent (claiming needs a human/admin bearer token,
prompted via getpass — never echoed, stored, or logged). The temporary
agent is left claimed afterward; there is no safe deletion endpoint.

Contestant file handling: exactly like smoke_game.py's `--concurrent` mode,
this temporarily replaces `agent/agent.py`'s *contents* with one of the two
example files' contents (backed up first, always restored in `finally`,
with the same leftover-backup self-healing check at startup) — this is the
one proven-reliable way to make a real `python -m agent` subprocess run
specific, deterministic contestant logic without touching
altruagent.worker/supervisor/runner.

Bounded by design: explicit per-scenario match-completion timeout, explicit
subprocess-startup timeout, and subprocess termination + `.wait()` in a
`finally` block regardless of outcome. Uses `subprocess.Popen`/`.terminate()`/
`.wait(timeout=...)` throughout — Windows-safe (no POSIX signal handling,
no shell=True). Never calls the unauthenticated `/games/{id}/cancel` — on a
timeout, this only stops the subprocesses; the match itself may be left
incomplete on the server (a known platform gap: there is no safe "abandon
this match" endpoint today — see the Milestone 5 completion report's
remaining-platform-work list).

Run (after copying .env.example -> .env and filling in your own agent's
credentials, exactly like scripts/smoke_game.py):

    python scripts/acceptance_test.py
"""

from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Callable

# Allow running this script directly without having pip-installed the project.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402

from altruagent.client import AltruAgentClient  # noqa: E402

NUM_ROUNDS = 3  # small on purpose — bounds how many messaging<->moving cycles this test waits through
HTTP_TIMEOUT_SECONDS = 10.0
COMPETITION_POLL_TIMEOUT_SECONDS = 60.0
COMPETITION_POLL_INTERVAL_SECONDS = 3.0
SUBPROCESS_STARTUP_TIMEOUT_SECONDS = 30.0
MATCH_COMPLETION_TIMEOUT_SECONDS = 180.0
MATCH_POLL_INTERVAL_SECONDS = 3.0
SUBPROCESS_TERMINATE_TIMEOUT_SECONDS = 10.0

EXAMPLES_DIR = REPO_ROOT / "examples"
AGENT_FILE = REPO_ROOT / "agent" / "agent.py"
AGENT_FILE_BACKUP = AGENT_FILE.with_suffix(".py.smoke_test_backup")

SCENARIOS = {
    "default": EXAMPLES_DIR / "basic_agent.py",       # no choose_message -> auto-terminate
    "messaging": EXAMPLES_DIR / "messaging_agent.py",  # real SendMessage flow
}


class AcceptanceTestError(RuntimeError):
    """Raised for any acceptance-test failure. Message is always safe to
    print — never include secrets when raising this."""


def _checkpoint(label: str) -> None:
    print(f"[OK] {label}")


def _fail(message: str) -> None:
    raise AcceptanceTestError(message)


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


# -- agent signup/claim/competition setup (same shape as scripts/smoke_game.py) --


def signup_temporary_agent(http: httpx.Client, control_url: str) -> tuple[str, str, str]:
    name = f"acceptance-test-{uuid.uuid4().hex[:10]}"
    response = http.post(
        f"{control_url}/auth/agent/signup",
        json={"name": name, "description": "Milestone 5 acceptance-test opponent (safe to ignore/delete)"},
    )
    body = _json_or_fail(response, "temporary agent signup")
    api_key = body.get("api_key")
    claim_token = body.get("claim_token")
    if not api_key or not claim_token:
        _fail("Signup response did not include both api_key and claim_token.")
    return name, api_key, claim_token


def claim_temporary_agent(http: httpx.Client, control_url: str, claim_token: str, human_token: str) -> None:
    response = http.post(
        f"{control_url}/auth/human/claim",
        json={"claim_token": claim_token},
        headers={"Authorization": f"Bearer {human_token}"},
    )
    _json_or_fail(response, "claiming the temporary agent")


def create_repeated_pd_competition(http: httpx.Client, control_url: str, human_token: str) -> str:
    """POST /admin/competitions/create with preset=repeated_pd.

    The preset (Agent_ACP backend/src/services/competitionPresets.ts) forces
    messaging_enabled=True and max_participants=2 — exactly what this test
    needs, with no messaging_config assembled by hand.
    """
    response = http.post(
        f"{control_url}/admin/competitions/create",
        json={"preset": "repeated_pd", "num_rounds": NUM_ROUNDS},
        headers={"Authorization": f"Bearer {human_token}"},
    )
    body = _json_or_fail(response, "repeated_pd competition creation")
    session_id = body.get("session_id")
    if not session_id:
        _fail("Competition creation response did not include session_id.")
    return session_id


def join_competition(client: AltruAgentClient, session_id: str) -> None:
    client.request("POST", f"/competitions/{session_id}/join")


def _poll_until(
    poll: Callable[[], dict],
    is_ready: Callable[[dict], bool],
    *,
    timeout_seconds: float,
    interval_seconds: float,
    timeout_message: Callable[[dict], str],
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last = poll()
    while True:
        if is_ready(last):
            return last
        if time.monotonic() >= deadline:
            _fail(timeout_message(last))
        time.sleep(interval_seconds)
        last = poll()


def wait_for_in_progress(poll: Callable[[], dict]) -> dict:
    return _poll_until(
        poll,
        lambda last: last.get("status") == "in_progress",
        timeout_seconds=COMPETITION_POLL_TIMEOUT_SECONDS,
        interval_seconds=COMPETITION_POLL_INTERVAL_SECONDS,
        timeout_message=lambda last: (
            f"Competition did not reach in_progress within "
            f"{COMPETITION_POLL_TIMEOUT_SECONDS:.0f}s (last status: {last.get('status')!r})."
        ),
    )


def wait_for_completed(poll: Callable[[], dict]) -> dict:
    return _poll_until(
        poll,
        lambda last: last.get("status") == "completed",
        timeout_seconds=MATCH_COMPLETION_TIMEOUT_SECONDS,
        interval_seconds=MATCH_POLL_INTERVAL_SECONDS,
        timeout_message=lambda last: (
            f"Match did not reach completed within {MATCH_COMPLETION_TIMEOUT_SECONDS:.0f}s "
            f"(last status: {last.get('status')!r}) — this is a real hard failure, not "
            "cleaned up further: this test never calls /games/{id}/cancel."
        ),
    )


# -- contestant file swap (identical mechanism to smoke_game.py --concurrent) --


def _install_scenario_agent(scenario: str) -> None:
    if AGENT_FILE_BACKUP.exists():
        print(
            "[WARN] Found a leftover agent.py.smoke_test_backup from a previous "
            "run that didn't finish cleanly — restoring the real agent.py from "
            "it before continuing."
        )
        AGENT_FILE_BACKUP.replace(AGENT_FILE)
    source_path = SCENARIOS[scenario]
    AGENT_FILE_BACKUP.write_text(AGENT_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    AGENT_FILE.write_text(source_path.read_text(encoding="utf-8"), encoding="utf-8")


def _restore_real_agent() -> None:
    if AGENT_FILE_BACKUP.exists():
        AGENT_FILE_BACKUP.replace(AGENT_FILE)


# -- one scenario ------------------------------------------------------------


def run_scenario(
    scenario: str,
    primary: AltruAgentClient,
    primary_api_key: str,
    control_url: str,
    opponent_api_key: str,
) -> None:
    print(f"\n=== Scenario: {scenario} ({SCENARIOS[scenario].name}) ===")

    human_token = getpass.getpass(
        f"[{scenario}] Paste a human/admin bearer token (used only in-memory, never logged): "
    )
    if not human_token:
        _fail("A human/admin bearer token is required to create a competition.")

    http = httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)
    try:
        session_id = create_repeated_pd_competition(http, control_url, human_token)
        _checkpoint(f"created repeated_pd competition {session_id}")

        join_competition(primary, session_id)
        opponent_client = AltruAgentClient(
            control_url=control_url, api_key=opponent_api_key, load_env_file=False
        )
        try:
            join_competition(opponent_client, session_id)
        finally:
            opponent_client.close()
        _checkpoint("both agents joined")

        wait_for_in_progress(lambda: http.get(f"{control_url}/competitions/{session_id}").json())
        _checkpoint("competition in_progress")
    finally:
        http.close()

    _install_scenario_agent(scenario)
    processes: list[subprocess.Popen] = []
    try:
        for label, api_key in (("primary", primary_api_key), ("opponent", opponent_api_key)):
            env = {**os.environ, "ALTRUAGENT_CONTROL_URL": control_url, "ALTRUAGENT_API_KEY": api_key}
            process = subprocess.Popen(
                [sys.executable, "-m", "agent"],
                cwd=str(REPO_ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            processes.append(process)
            print(f"[{scenario}] started `python -m agent` for {label}, pid={process.pid}")

        # Startup sanity check: both processes must still be alive shortly
        # after launch (an immediate exit means a config/import error).
        time.sleep(min(SUBPROCESS_STARTUP_TIMEOUT_SECONDS, 5.0))
        for process in processes:
            if process.poll() is not None:
                _fail(
                    f"`python -m agent` exited immediately (code {process.returncode}) "
                    f"during scenario {scenario!r} — see its output above/below."
                )

        control_url_client = httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)
        try:
            wait_for_completed(lambda: control_url_client.get(f"{control_url}/competitions/{session_id}").json())
        finally:
            control_url_client.close()
        _checkpoint(f"scenario {scenario!r} match completed via real python -m agent subprocesses")
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=SUBPROCESS_TERMINATE_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=SUBPROCESS_TERMINATE_TIMEOUT_SECONDS)
        _restore_real_agent()


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()

    try:
        primary = AltruAgentClient()
    except Exception as exc:  # ConfigurationError
        print(f"Configuration error: {exc}")
        return 1

    control_url = primary.control_url
    # AltruAgentClient() already called load_dotenv(), which populates
    # os.environ from .env if it wasn't already set — read it back the same
    # way rather than reaching into the client's private _api_key attribute.
    primary_api_key = os.environ.get("ALTRUAGENT_API_KEY")
    if not primary_api_key:
        print("Configuration error: ALTRUAGENT_API_KEY is not set.")
        return 1

    http = httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)
    try:
        name, opponent_api_key, claim_token = signup_temporary_agent(http, control_url)
        _checkpoint(f"signed up temporary opponent agent '{name}'")
        human_token = getpass.getpass(
            "Paste a human/admin bearer token to claim the temporary opponent "
            "(used only in-memory, never logged): "
        )
        if not human_token:
            _fail("A human/admin bearer token is required to claim the temporary agent.")
        claim_temporary_agent(http, control_url, claim_token, human_token)
        _checkpoint("temporary opponent claimed")
    finally:
        http.close()

    try:
        for scenario in ("default", "messaging"):
            run_scenario(scenario, primary, primary_api_key, control_url, opponent_api_key)
        print("\nMILESTONE 5 ACCEPTANCE TEST PASSED (both scenarios)")
        return 0
    except AcceptanceTestError as exc:
        print(f"\nACCEPTANCE TEST FAILED: {exc}")
        return 1
    finally:
        _restore_real_agent()  # double safety net, on top of run_scenario's own finally
        primary.close()


if __name__ == "__main__":
    raise SystemExit(main())
