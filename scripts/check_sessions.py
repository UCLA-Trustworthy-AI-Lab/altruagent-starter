"""Read-only discovery smoke test: lists this agent's assigned matches.

Calls GET /agents/me/sessions (via client.sessions()) and prints a summary.
This never joins, steps, resigns, or otherwise mutates anything.

Run:
    python scripts/check_sessions.py

With --inspect-active, additionally fetches (read-only) state for each
active match through MCP (the same production gameplay transport
python -m agent uses) via match.game().get_state() — still no moves are
submitted:
    python scripts/check_sessions.py --inspect-active
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running this script directly without having pip-installed the project.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from altruagent.client import AltruAgentClient  # noqa: E402
from altruagent.errors import AltruAgentError, ConfigurationError  # noqa: E402
from altruagent.models import Match  # noqa: E402


def _print_match(match: Match) -> None:
    print(
        f"  session_id={match.session_id} game_type={match.game_type} "
        f"status={match.status} tournament_id={match.tournament_id}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inspect-active",
        action="store_true",
        help=(
            "Also fetch state for each active match through MCP via "
            "match.game().get_state() — read-only, submits no moves."
        ),
    )
    args = parser.parse_args()

    try:
        client = AltruAgentClient()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}")
        return 1

    try:
        sessions = client.sessions()

        print(f"Waiting matches: {len(sessions.waiting)}")
        print(f"Active matches: {len(sessions.active)}")
        print(f"Completed matches: {len(sessions.completed)}")

        if sessions.waiting:
            print("\nWaiting:")
            for m in sessions.waiting:
                _print_match(m)
        if sessions.active:
            print("\nActive:")
            for m in sessions.active:
                _print_match(m)
        if sessions.completed:
            print("\nCompleted:")
            for m in sessions.completed:
                _print_match(m)

        if args.inspect_active and sessions.active:
            print("\nInspecting active matches via MCP (read-only — fetches state, submits no moves):")
            for m in sessions.active:
                try:
                    game = m.game()
                    state = game.get_state()
                    # get_game_state alone never carries legal_actions (a
                    # separate MCP tool) — only fetch it when there's
                    # actually something to enumerate, same as the runner.
                    action_ids: list[str] = []
                    if state.is_current_actor:
                        action_ids = [a["action_id"] for a in game.get_legal_actions().get("actions", [])]
                    print(
                        f"  session_id={m.session_id} phase={state.phase} "
                        f"is_current_actor={state.is_current_actor} legal_actions={action_ids}"
                    )
                except AltruAgentError as exc:
                    print(f"  session_id={m.session_id}: failed to inspect ({exc})")

        return 0
    except AltruAgentError as exc:
        print(f"Request failed: {exc}")
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
