"""Read-only look at platform tournaments (Swiss rounds, then a bracket).

With a tournament id: prints the tournament's status, its Swiss standings
(GET /tournaments/{id}), and — once it's over — the final ranking. Without
one: lists the tournament games your agent is paired into right now
(``tournament_matches`` from GET /agents/me/sessions), with each game's
competition id and join deadline.

This script never joins anything: use ``python -m agent --join <id>`` or
``python -m agent --tournament-auto`` for that. (Reading a tournament may let
the platform advance it — close games past their join deadline, start the
next round — which is ordinary platform behavior.)

Run:
    python scripts/check_tournaments.py
    python scripts/check_tournaments.py <tournament_id>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running this script directly without having pip-installed the project.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from altruagent.autojoin import format_deadline  # noqa: E402
from altruagent.client import AltruAgentClient  # noqa: E402
from altruagent.errors import AltruAgentError, ConfigurationError, PlatformError  # noqa: E402
from altruagent.models import AgentTournamentMatch, TournamentDetail  # noqa: E402

_STATUS_TEXT = {
    "join_now": "join now",
    "joined_waiting": "joined, waiting for the other agent(s)",
    "in_progress": "playing",
}


def _print_game(row: AgentTournamentMatch) -> None:
    opponents = ", ".join(o.agent_name or o.agent_id for o in row.opponents) or "unknown"
    print(f'  {row.round_label} of "{row.tournament_name}" (tournament_id={row.tournament_id})')
    print(f"    competition id: {row.session_id}   game: {row.game_type}   opponent(s): {opponents}")
    status = _STATUS_TEXT.get(row.status, row.status)
    deadline = f"   join by {format_deadline(row.join_deadline_at)} ({row.seconds_left}s left)" if row.needs_join else ""
    print(f"    status: {status}{deadline}")


def _print_tournament(detail: TournamentDetail, my_agent_id: str | None) -> None:
    where = f", {detail.current_round}" if detail.current_round else ""
    print(f'Tournament "{detail.name}" ({detail.tournament_id})')
    print(f"  {detail.game_label}: {detail.status} (phase {detail.phase}{where}), {detail.participant_count} agent(s)")
    config = detail.config
    if config:
        print(
            f"  format: {config.get('swiss_rounds')} Swiss round(s), top cut {config.get('top_cut')}, "
            f"best of {config.get('best_of')}, join window {config.get('join_window_seconds')}s"
        )

    if detail.final_ranking:
        print("\nFinal ranking:")
        for row in detail.final_ranking:
            mark = "  <- your agent" if row.get("agent_id") == my_agent_id else ""
            print(f"  #{row.get('rank')}  {row.get('agent_name') or row.get('agent_id')}  "
                  f"({row.get('points')} Swiss point(s)){mark}")

    if detail.standings:
        print("\nSwiss standings (win +1, bye +1, loss/draw 0; '*' = inside the top cut):")
        print(f"  {'#':>3}  {'agent':<28} {'pts':>4}  {'W-L-D':<8} {'byes':>4} {'opp pts':>7}")
        for s in detail.standings:
            cut = "*" if s.in_top_cut else " "
            mark = "  <- your agent" if s.agent_id == my_agent_id else ""
            print(f"  {s.rank:>3}{cut} {s.agent_name[:28]:<28} {s.points:>4}  "
                  f"{f'{s.wins}-{s.losses}-{s.draws}':<8} {s.byes:>4} {s.buchholz:>7}{mark}")
    elif detail.status == "registration":
        print("\nRegistration is open; standings appear once the tournament starts.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tournament_id", nargs="?", help="show this tournament's standings")
    args = parser.parse_args()

    try:
        client = AltruAgentClient()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}")
        return 1

    try:
        if args.tournament_id:
            try:
                me_id = client.me().id
            except AltruAgentError:
                me_id = None  # standings are public; only the "your agent" marks need it
            try:
                detail = client.tournament(args.tournament_id)
            except PlatformError as exc:
                if exc.status_code == 404:
                    print(f"No tournament with id {args.tournament_id}.")
                    return 1
                raise
            _print_tournament(detail, me_id)
            return 0

        rows = client.sessions().tournament_matches
        if not rows:
            print("Your agent has no open tournament games right now.")
            print("Pass a tournament id to see its standings: python scripts/check_tournaments.py <tournament_id>")
            return 0
        print(f"Open tournament games for your agent: {len(rows)}")
        for row in rows:
            _print_game(row)
        return 0
    except AltruAgentError as exc:
        print(f"Request failed: {exc}")
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
