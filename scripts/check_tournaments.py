"""Read-only tournament discovery/inspection.

Lists public tournaments (GET /tournaments) and, with --id, inspects one in
detail (GET /tournaments/{id}) including this agent's viewer membership.

Note: GET /tournaments/{id} is NOT guaranteed side-effect-free on the current
backend — it can trigger a queue-linked tournament's start (if its timer has
expired) and reconcile any child match GameAPI already finished (recomputing
the leaderboard / scheduling the next batch / completing the tournament),
all as a side effect of an ordinary GET (verified against Agent_ACP's
tournamentService.ts). This script does not compensate for that; it simply
reflects whatever the server returns.

This script never joins or leaves a tournament — see
client.join_tournament()/client.leave_tournament() for that.

Run:
    python scripts/check_tournaments.py
    python scripts/check_tournaments.py --id <tournament_id>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running this script directly without having pip-installed the project.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from altruagent.client import AltruAgentClient  # noqa: E402
from altruagent.errors import AltruAgentError, ConfigurationError  # noqa: E402
from altruagent.models import Tournament  # noqa: E402


def _print_tournament_summary(t: Tournament) -> None:
    participants = f"{t.current_participants}/{t.max_participants}"
    queue_note = f" queue_id={t.queue_id}" if t.queue_id else ""
    print(
        f"  tournament_id={t.tournament_id} game_type={t.game_type} "
        f"status={t.status} participants={participants}{queue_note}"
    )


def _print_tournament_detail(t: Tournament) -> None:
    _print_tournament_summary(t)
    print(f"  max_active_matches={t.max_active_matches} game_server_url={t.game_server_url}")
    if t.viewer is None:
        print("  viewer: none (no agent JWT was recognized for this call)")
        return
    v = t.viewer
    print(
        f"  viewer: is_participant={v.is_tournament_participant} "
        f"active_child_session_ids={v.active_child_session_ids}"
    )
    for action in v.next_actions:
        print(f"    next_action: {action.action} — {action.hint}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--id",
        metavar="TOURNAMENT_ID",
        help=(
            "Inspect one tournament in detail via GET /tournaments/{id} "
            "(see the side-effect note above) instead of listing all of them."
        ),
    )
    args = parser.parse_args()

    try:
        client = AltruAgentClient()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}")
        return 1

    try:
        if args.id:
            tournament = client.tournament(args.id)
            print(f"Tournament {args.id}:")
            _print_tournament_detail(tournament)
        else:
            tournaments = client.tournaments()
            print(f"Active tournaments: {len(tournaments)}")
            for t in tournaments:
                _print_tournament_summary(t)
        return 0
    except AltruAgentError as exc:
        print(f"Request failed: {exc}")
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
