"""Show that a refused Werewolf call changes nothing on the server.

Plays one Werewolf match against the LOCAL bench GameAPI (gameapi_server.py)
step by step. At each point where a call must be refused, it reads every
seat's state and the full message log, sends the call, checks the refusal
code, and reads everything again. The two snapshots must be identical:

  send_message(terminate) while the game is moving    -> WRONG_PHASE
      (the race seen in benchmarks: a seat re-sends "end discussion" just
      after the last seat's one ended the round)
  play_action during discussion                       -> WRONG_PHASE
  play_action with an old state_version               -> STALE_STATE
  play_action by a seat whose turn it isn't           -> NOT_YOUR_TURN, or
                                                         another refusal

  python scripts/mcp_bench/probe_refusals.py [--server http://127.0.0.1:8791]

Exit status 1 if a refused call changed anything.
"""

from __future__ import annotations

import argparse
import sys
import uuid

import httpx

from bench_client import SeatClient
from altruagent import mcp_transport
from altruagent.mcp_game import MCPGameSession
from altruagent.mcp_transport import MCPToolError

# Fields that change with the clock, not with the game.
VOLATILE = ("time", "deadline", "seconds", "_at", "elapsed", "expires", "remaining")


def _stable(value):
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items() if not any(word in k for word in VOLATILE)}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


def snapshot(games: list[MCPGameSession]) -> list:
    return [(_stable(g.get_state().raw), _stable(g.get_messages(since=-1))) for g in games]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server", default="http://127.0.0.1:8791")
    parser.add_argument("--rounds", type=int, default=6, help="discussion rounds to probe")
    args = parser.parse_args()
    if not args.server.startswith(("http://127.0.0.1", "http://localhost")):
        parser.error("--server must be the local bench server")

    session_id = str(uuid.uuid4())
    seats = [f"probe{uuid.uuid4().hex[:6]}-s{i}" for i in range(7)]
    httpx.post(f"{args.server}/games/create", timeout=30, json={
        "session_id": session_id, "game_name": "werewolf", "player_user_ids": seats,
        "player_names": [f"P{i}" for i in range(7)], "timeout_seconds": 600,
        "messaging_enabled": True, "messaging_mode": "per_move", "max_messages_per_phase_per_agent": 5,
        "blind_messaging": False, "word_limit_per_message": 50, "messaging_time_limit_seconds": 120,
    }).raise_for_status()
    clients = [SeatClient(args.server, a) for a in seats]
    games = [MCPGameSession(c, session_id=session_id, game_server_url=args.server) for c in clients]
    failures, probes = [], 0

    def probe(label: str, expected: set[str], call) -> None:
        nonlocal probes
        before = snapshot(games)
        try:
            call()
        except MCPToolError as exc:
            code = exc.error_code
        else:
            code = "ACCEPTED"
        after = snapshot(games)
        changed = before != after
        probes += 1
        ok = code in expected and not changed
        print(f"{'ok  ' if ok else 'FAIL'} {label}: {code}; state and messages {'CHANGED' if changed else 'unchanged'}")
        if not ok:
            failures.append(label)

    try:
        rounds = 0
        while rounds < args.rounds:
            states = [g.get_state() for g in games]
            if states[0].is_terminal:
                break
            alive = [i for i, s in enumerate(states) if not s.raw.get("eliminated")]
            if states[0].phase == "messaging":
                actor = alive[0]
                version = states[actor].state_version
                probe(f"round {rounds}: seat {actor} plays during discussion", {"WRONG_PHASE"},
                      lambda: games[actor].play_action(action_id="0", action=None, state_version=version))
                for i in alive:
                    games[i].send_message(message_type="terminate")
                late = alive[-1]
                probe(f"round {rounds}: seat {late} ends discussion again after the round closed",
                      {"WRONG_PHASE"}, lambda: games[late].send_message(message_type="terminate"))
                rounds += 1
                continue
            movers = [i for i in alive if states[i].is_current_actor]
            if not movers:
                break
            mover = movers[0]
            state = states[mover]
            if state.legal_actions is None or not state.legal_actions:
                state.legal_actions = games[mover].get_state().legal_actions
            action = state.legal_actions[0]
            if state.state_version > 0:
                probe(f"round {rounds}: seat {mover} moves with an old state_version", {"STALE_STATE"},
                      lambda: games[mover].play_action(action_id=action.action_id,
                                                       state_version=state.state_version - 1))
            idle = [i for i in alive if not states[i].is_current_actor]
            if idle:
                other = idle[0]
                probe(f"round {rounds}: seat {other} moves out of turn",
                      {"NOT_YOUR_TURN", "INVALID_ACTION", "STALE_STATE"},
                      lambda: games[other].play_action(action_id=action.action_id,
                                                       state_version=state.state_version))
            games[mover].play_action(action_id=action.action_id,
                                     state_version=state.state_version)
    finally:
        for c in clients:
            mcp_transport.close_connections(c)

    print(f"\n{probes - len(failures)}/{probes} refused calls left the game unchanged")
    return 1 if failures or not probes else 0


if __name__ == "__main__":
    sys.exit(main())
