"""Check a --trace file from bench_client.py for correctness, not speed.

For every match it checks, against the server's own records:

  moves     every accepted play_action used a distinct state_version and
            advanced it by exactly one, and the number of accepted moves
            equals the server's final state_version: no move was lost or
            applied twice, and no refused call advanced the game
            (turn-based OpenSpiel games: tic_tac_toe, werewolf, repeated_pd)
  messages  the server's message log is exactly the set of messages
            send_message answered as recorded, and no seat ended discussion
            twice in one round: no refused send_message left a trace
  results   every seat saw the same final result, and GameAPI reported it
            exactly once, with the same returns
  refusals  every refused call was a race (the game moved on between the
            seat's last read and its call), and the seat's next call read
            the state again before it acted

For each WRONG_PHASE it also records which read the seat acted on, and
whether its send_message was a first "end discussion" or a repeat (the
runner's default agent re-sends it, a no-op, each time it wakes up during
a round it already ended).

  python scripts/mcp_bench/check_trace.py TRACE.jsonl [TRACE.jsonl ...]

Exit status 1 if any check fails.
"""

from __future__ import annotations

import collections
import json
import sys

RACE_CODES = {"STALE_STATE", "NOT_YOUR_TURN", "WRONG_PHASE", "GAME_ALREADY_COMPLETE", "PLAYER_ELIMINATED"}
READS = {"get_game_state", "wait_for_update", "get_legal_actions", "get_messages", "get_result"}
WRITES = {"play_action", "send_message", "resign"}
VERSIONED_GAMES = {"tic_tac_toe", "werewolf", "repeated_pd"}


def load(paths):
    calls, matches = collections.defaultdict(list), []
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                record = json.loads(line)
                if record["kind"] == "call":
                    calls[record["session_id"]].append(record)
                else:
                    matches.append(record)
    return calls, matches


def check_match(match: dict, calls: list[dict]) -> tuple[list[str], dict]:
    problems: list[str] = []
    stats = collections.Counter()
    server = match.get("server") or {}
    if "error" in server:
        return [f"could not read the server's record: {server['error']}"], stats
    if not match["all_terminal"] or match["failures"]:
        problems.append(f"match did not complete: {match['failures']}")
    calls = sorted(calls, key=lambda c: c["t0"])
    by_seat = collections.defaultdict(list)
    for c in calls:
        by_seat[c["seat"]].append(c)

    # moves
    moves = [c for c in calls if c["tool"] == "play_action" and c["ok"]]
    stats["moves"] = len(moves)
    if match["game"] in VERSIONED_GAMES:
        sent = [c["args"].get("state_version") for c in moves]
        if len(set(sent)) != len(sent):
            problems.append(f"two accepted moves used the same state_version: {sorted(sent)}")
        for c in moves:
            if c.get("result_version") is not None and c["result_version"] != c["args"]["state_version"] + 1:
                problems.append(f"move at version {c['args']['state_version']} answered version {c['result_version']}")
        if len(moves) != server["final_state_version"]:
            problems.append(f"{len(moves)} accepted moves, but the server's final version is {server['final_state_version']}")

    # messages (None: the game has no messaging)
    recorded = {c["message_index"] for c in calls if c["tool"] == "send_message" and c["ok"] and c.get("message_index") is not None}
    log = server["messages"] if server["messages"] is not None else []
    logged = {m["index"] for m in log}
    stats["messages"] = len(logged)
    if recorded != logged:
        problems.append(f"message log differs from accepted sends: only logged {sorted(logged - recorded)}, "
                        f"only answered {sorted(recorded - logged)}")
    terminates = collections.Counter((m["sender"], m["move_index"]) for m in log if m["type"] == "terminate")
    twice = [k for k, n in terminates.items() if n > 1]
    if twice:
        problems.append(f"a seat ended discussion twice in one round: {twice}")

    # results
    finals = list(match["finals"].values())
    if any((f["termination_reason"], f["returns"]) != (finals[0]["termination_reason"], finals[0]["returns"]) for f in finals):
        problems.append(f"seats disagree on the result: {finals}")
    reports = server["result_reports"]
    if len(reports) != 1:
        problems.append(f"GameAPI reported the result {len(reports)} times")
    elif finals:
        reported = sorted(reports[0]["results"].values())
        seen = sorted((finals[0]["returns"] or {}).values())
        if reported != seen or reports[0]["termination_reason"] != finals[0]["termination_reason"]:
            problems.append(f"reported result {reports[0]} differs from what seats saw {finals[0]}")

    # refusals and recovery
    for seat, seq in by_seat.items():
        last_read = None
        terminated_round = None  # move_index of the round this seat already ended
        for i, c in enumerate(seq):
            if c["tool"] in READS and c["ok"]:
                last_read = c
            if c["tool"] == "send_message" and c["ok"] and c.get("message_type") == "terminate":
                terminated_round = c.get("move_index")
            if c["ok"]:
                continue
            code = c.get("error_code")
            stats[f"refused {c['tool']} {code}"] += 1
            if code not in RACE_CODES:
                problems.append(f"seat {seat}: {c['tool']} failed with {code or c.get('error')}, not a race")
                continue
            following = seq[i + 1] if i + 1 < len(seq) else None
            if following is None or following["tool"] not in READS:
                problems.append(f"seat {seat}: after {code} on {c['tool']} the next call was "
                                f"{following and following['tool']}, not a read")
            if code == "WRONG_PHASE" and c["tool"] == "send_message":
                acted_on = (last_read or {}).get("phase")
                stats[f"WRONG_PHASE send_message acted on a {acted_on!r} read"] += 1
                repeat = terminated_round is not None and terminated_round == (last_read or {}).get("state_version")
                stats["WRONG_PHASE was a repeated end-discussion" if repeat else "WRONG_PHASE was a first end-discussion"] += 1
                if acted_on != "messaging":
                    problems.append(f"seat {seat}: send_message refused although its last read said {acted_on!r}")
                if following and following.get("ok") and following.get("phase") == "messaging" \
                        and following.get("state_version") == (last_read or {}).get("state_version"):
                    problems.append(f"seat {seat}: after WRONG_PHASE the re-read still shows the same discussion round")
    return problems, stats


def main(paths: list[str]) -> int:
    calls, matches = load(paths)
    failed = 0
    totals = collections.Counter()
    for match in matches:
        problems, stats = check_match(match, calls.get(match["session_id"], []))
        totals.update(stats)
        totals[f"{match.get('mode', '?')} {match['game']} matches"] += 1
        label = f"{match.get('mode', '?'):8} {match['game']:20} {match['session_id'][:8]}"
        if problems:
            failed += 1
            print(f"FAIL {label}")
            for p in problems:
                print(f"     - {p}")
        else:
            refused = sum(v for k, v in stats.items() if k.startswith("refused"))
            print(f"ok   {label}  moves={stats['moves']} messages={stats['messages']} refused(race)={refused}")
    print()
    for key, value in sorted(totals.items()):
        print(f"  {key}: {value}")
    print(f"\n{len(matches) - failed}/{len(matches)} matches pass every check")
    return 1 if failed or not matches else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
