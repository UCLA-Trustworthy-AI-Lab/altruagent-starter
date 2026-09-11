"""Read-only discovery smoke test: lists this agent's assigned matches.

Calls GET /agents/me/sessions (via client.sessions()) and prints a summary.
This never joins, steps, resigns, or otherwise mutates anything.

Run:
    python scripts/check_sessions.py

With --inspect-active, additionally fetches (read-only) GameAPI state for
each active match via match.game().state() — still no moves are submitted:
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
            "Also fetch GameAPI state for each active match via "
            "match.game().state() — read-only, submits no moves."
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
            print("\nInspecting active matches (read-only — fetches state, submits no moves):")
            for m in sessions.active:
                try:
                    state = m.game().state()
                    mover = state.current_player.name if state.current_player else None
                    print(
                        f"  session_id={m.session_id} phase={state.phase} "
                        f"current_player={mover} legal_actions={state.legal_actions}"
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
