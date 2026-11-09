"""Play complete matches against the LOCAL bench GameAPI (gameapi_server.py)
through the starter's real runtime (``run_game`` + ``MCPGameSession``), and
measure MCP traffic, server CPU, latency, errors and correctness.

  --mode original   the MCP transport as of --baseline-ref (default: the last
                    commit before session reuse), loaded from git, so "before"
                    is the real old code, not an imitation
  --mode reuse      the transport in this working tree

Every seat runs on its own thread with its own client, as a worker process
would. One JSON line per run goes to --out; --trace also writes one JSON
line per MCP call plus each match's end state, for check_trace.py.

Only talks to http://127.0.0.1. See scripts/mcp_bench/README.md.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import threading
import time
import types
import uuid
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import altruagent.mcp_game as mcp_game_module  # noqa: E402
from altruagent import mcp_transport  # noqa: E402
from altruagent.mcp_game import MCPGameSession  # noqa: E402
from altruagent.models import DecisionContext  # noqa: E402
from altruagent.runner import run_game  # noqa: E402

# The last commit before session reuse: its mcp_transport.py opens a new
# session for every tool call.
DEFAULT_BASELINE_REF = "686e7560708eaa595a409b330d0d1837d49ba9d5"

SEATS = {"tic_tac_toe": 2, "werewolf": 7, "repeated_pd": 2, "pokemon_gen9same": 2, "pokemon_gen9ou_draft": 2}

WRITE_TOOLS = {"play_action", "send_message", "resign"}


def load_baseline_transport(ref: str) -> types.ModuleType:
    """``altruagent/mcp_transport.py`` exactly as committed at ``ref``."""
    source = subprocess.run(
        ["git", "-C", str(REPO), "show", f"{ref}:altruagent/mcp_transport.py"],
        check=True, capture_output=True, text=True, encoding="utf-8",
    ).stdout
    module = types.ModuleType("altruagent._baseline_mcp_transport")
    module.__package__ = "altruagent"
    sys.modules[module.__name__] = module
    exec(compile(source, f"{ref[:12]}:altruagent/mcp_transport.py", "exec"), module.__dict__)
    return module


class SeatClient:
    """The token source a seat's MCP calls use (what ``AltruAgentClient``
    provides in production), minted by the local bench server."""

    def __init__(self, server: str, agent_id: str, first_ttl: int | None = None) -> None:
        self.server, self.agent_id = server, agent_id
        self.logins = 0
        self._token = self._mint(first_ttl)

    def _mint(self, ttl: int | None = None) -> str:
        self.logins += 1
        params: dict = {"agent_id": self.agent_id}
        if ttl is not None:
            params["ttl"] = ttl
        return httpx.get(f"{self.server}/_bench/mint", params=params).json()["token"]

    def _current_access_token(self, *, force_relogin: bool = False) -> str:
        if force_relogin:
            self._token = self._mint()
        return self._token


def _summary(tool: str, answer: dict) -> dict:
    """The parts of an answer the trace checks need."""
    state = answer.get("state") if isinstance(answer.get("state"), dict) else answer
    out = {
        "phase": state.get("phase"),
        "state_version": state.get("state_version"),
        "is_terminal": state.get("is_terminal"),
        "is_current_actor": state.get("is_current_actor"),
    }
    if tool == "play_action":
        out.update(accepted=answer.get("accepted"), result_version=answer.get("state_version"))
    if tool == "send_message":
        message = answer.get("message") or {}
        out.update(
            accepted=answer.get("accepted"),
            message_index=message.get("index"),
            message_type=message.get("type"),
            move_index=message.get("move_index"),
            terminated=answer.get("terminated_messaging"),
        )
    return out


class Recorder:
    def __init__(self, trace_path: str | None) -> None:
        self.lock = threading.Lock()
        self.latency: dict[str, list[float]] = {}
        self.errors: list[tuple] = []
        self.trace = open(trace_path, "a", encoding="utf-8") if trace_path else None

    def call(self, record: dict, exc: Exception | None) -> None:
        with self.lock:
            self.latency.setdefault(record["tool"], []).append(record["t1"] - record["t0"])
            if exc is not None:
                self.errors.append((record["tool"], record.get("error_code"), type(exc).__name__, str(exc)[:160]))
            if self.trace:
                self.trace.write(json.dumps(record, default=str) + "\n")

    def match(self, record: dict) -> None:
        if self.trace:
            with self.lock:
                self.trace.write(json.dumps(record, default=str) + "\n")


class TracedGame(MCPGameSession):
    def __init__(self, client, recorder: Recorder, seat: int, **kw) -> None:
        super().__init__(client, **kw)
        self.recorder, self.seat = recorder, seat

    def _call(self, tool: str, arguments: dict) -> dict:
        record = {
            "kind": "call", "session_id": self.session_id, "seat": self.seat, "tool": tool,
            "args": {k: v for k, v in arguments.items() if k in ("state_version", "message_type", "action_id")},
            "t0": time.perf_counter(),
        }
        try:
            answer = super()._call(tool, arguments)
        except Exception as exc:
            record.update(t1=time.perf_counter(), ok=False, error_code=getattr(exc, "error_code", None),
                          error=type(exc).__name__)
            self.recorder.call(record, exc)
            raise
        record.update(t1=time.perf_counter(), ok=True, **_summary(tool, answer))
        self.recorder.call(record, None)
        return answer


def choose(state, context):
    """Take the first legal action (Pokémon team preview: bring the first
    Pokémon offered). Messaging uses the runner's default: end discussion."""
    first = state.legal_actions[0]
    template = (first.input or {}).get("action") if isinstance(first.input, dict) else None
    if isinstance(template, dict) and template.get("type") == "select_lineup":
        bring = template["roster"][: template.get("bring_count", 4)]
        return {"type": "select_lineup", "bring": bring, "leads": bring[: template.get("lead_count", 2)]}
    return first


def _end_of_match(server: str, session_id: str, seats: list[str]) -> dict:
    """Read the server's final record of a match (after the measured window):
    final state version, the full message log, the result report."""
    messages: dict[int, dict] | None = {}
    final_version = None
    for agent_id in seats:
        client = SeatClient(server, agent_id)
        game = MCPGameSession(client, session_id=session_id, game_server_url=server)
        try:
            final_version = game.get_state().state_version
            if messages is not None:
                try:
                    answer = game.get_messages(since=-1) or {}
                except Exception as exc:  # either transport's MCPToolError (--mode original loads its own class)
                    if "does not support" not in str(exc):
                        raise
                    messages = None  # a game without messaging (Pokémon)
                else:
                    for m in answer.get("messages") or []:
                        messages[m["index"]] = {k: m.get(k) for k in ("index", "sender", "type", "move_index")}
        except Exception as exc:  # noqa: BLE001 - recorded, checked later
            return {"error": repr(exc)[:300]}
        finally:
            mcp_transport.close_connections(client)
    reports = [
        c for c in httpx.get(f"{server}/_bench/completions").json()["completions"] if c["session_id"] == session_id
    ]
    return {
        "final_state_version": final_version,
        "messages": None if messages is None else sorted(messages.values(), key=lambda m: m["index"]),
        "result_reports": [c["body"] for c in reports],
    }


def play_match(server, game_name, tag, index, recorder, results, first_ttl=None) -> None:
    session_id = str(uuid.uuid4())
    seats = [f"{tag}-m{index}-s{seat}" for seat in range(SEATS[game_name])]
    body = {"session_id": session_id, "game_name": game_name, "player_user_ids": seats,
            "player_names": [f"P{i}" for i in range(len(seats))], "timeout_seconds": 600}
    if game_name == "werewolf":
        body.update({"messaging_enabled": True, "messaging_mode": "per_move", "max_messages_per_phase_per_agent": 5,
                     "blind_messaging": False, "word_limit_per_message": 50, "messaging_time_limit_seconds": 120})
    httpx.post(f"{server}/games/create", json=body, timeout=30).raise_for_status()
    finals, failures, clients = {}, [], []

    def seat(i: int, agent_id: str) -> None:
        client = SeatClient(server, agent_id, first_ttl)
        clients.append(client)
        game = TracedGame(client, recorder, i, session_id=session_id, game_server_url=server)
        context = DecisionContext(session_id=session_id, tournament_id=None, game_type=game_name,
                                  agent_id=agent_id, seat_position=i)
        try:
            finals[i] = run_game(game, context, choose, log=lambda line: None)
        except Exception as exc:  # noqa: BLE001
            failures.append((agent_id, repr(exc)[:300]))
        finally:
            mcp_transport.close_connections(client)  # what AltruAgentClient.close() does

    threads = [threading.Thread(target=seat, args=(i, a)) for i, a in enumerate(seats)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    results.append({
        "kind": "match", "session_id": session_id, "game": game_name, "seats": seats,
        "all_terminal": len(finals) == len(seats) and all(f.is_terminal for f in finals.values()),
        "finals": {i: {"termination_reason": str(f.termination_reason), "returns": f.returns,
                       "state_version": f.state_version} for i, f in finals.items()},
        "failures": failures,
        "relogins": sum(c.logins - 1 for c in clients),
    })


def pct(values, p):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round((len(ordered) - 1) * p)))] if ordered else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server", default="http://127.0.0.1:8791")
    parser.add_argument("--mode", choices=["original", "reuse"], required=True)
    parser.add_argument("--baseline-ref", default=DEFAULT_BASELINE_REF, help="git ref for --mode original")
    parser.add_argument("--game", default="tic_tac_toe", choices=sorted(SEATS))
    parser.add_argument("--matches", type=int, default=1, help="matches played at the same time")
    parser.add_argument("--first-ttl", type=int, default=None,
                        help="lifetime in seconds of each seat's first token: forces a mid-match 401 and re-login")
    parser.add_argument("--out", default=None, help="append the run summary (one JSON line) here")
    parser.add_argument("--trace", default=None, help="append one JSON line per call and per match here")
    args = parser.parse_args()
    if not args.server.startswith(("http://127.0.0.1", "http://localhost")):
        parser.error("--server must be the local bench server")

    if args.mode == "original":
        mcp_game_module.call_tool = load_baseline_transport(args.baseline_ref).call_tool

    recorder = Recorder(args.trace)
    httpx.post(f"{args.server}/_bench/reset")
    before = httpx.get(f"{args.server}/_bench/counts").json()
    results: list[dict] = []
    tag = f"{args.mode[:4]}{uuid.uuid4().hex[:6]}"
    started = time.perf_counter()
    threads = [
        threading.Thread(target=play_match, args=(args.server, args.game, tag, i, recorder, results, args.first_ttl))
        for i in range(args.matches)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - started
    after = httpx.get(f"{args.server}/_bench/counts").json()

    # Outside the measured window: the server's own record of each match.
    for result in results:
        result.update(server=_end_of_match(args.server, result["session_id"], result["seats"]))
        result["mode"] = args.mode
        recorder.match(result)

    c = after["counts"]
    tool_calls = sum(v for k, v in c.items() if k.startswith("tool:"))
    non_wait = [s for tool, xs in recorder.latency.items() if tool != "wait_for_update" for s in xs]
    all_calls = [s for xs in recorder.latency.values() for s in xs]
    m = args.matches
    report = {
        "mode": args.mode, "game": args.game, "concurrent_matches": m, "wall_seconds": round(wall, 2),
        "server_cpu_seconds": round(after["cpu_seconds"] - before["cpu_seconds"], 3),
        "http_requests_to_mcp": c.get("http_mcp", 0),
        "initialize": c.get("method:initialize", 0),
        "notifications_initialized": c.get("method:notifications/initialized", 0),
        "tools_list": c.get("method:tools/list", 0),
        "tools_call": c.get("method:tools/call", 0),
        "per_match": {
            "initialize": round(c.get("method:initialize", 0) / m, 2),
            "tools_list": round(c.get("method:tools/list", 0) / m, 2),
            "tools_call": round(tool_calls / m, 2),
            "http_requests": round(c.get("http_mcp", 0) / m, 2),
        },
        "http_requests_per_tool_call": round(c.get("http_mcp", 0) / tool_calls, 3) if tool_calls else None,
        "client_calls": len(all_calls),
        "latency_ms_excluding_wait_for_update": {
            "n": len(non_wait),
            "mean": round(1000 * statistics.mean(non_wait), 2) if non_wait else None,
            "p50": round(1000 * pct(non_wait, 0.5), 2) if non_wait else None,
            "p95": round(1000 * pct(non_wait, 0.95), 2) if non_wait else None,
        },
        "client_errors": len(recorder.errors),
        "client_error_codes": sorted({f"{e[0]}:{e[1] or e[2]}" for e in recorder.errors}),
        "matches_completed": sum(r["all_terminal"] and not r["failures"] for r in results),
        "relogins": sum(r["relogins"] for r in results),
        "match_failures": [f for r in results for f in r["failures"]][:5],
    }
    print(json.dumps(report, indent=1))
    if args.out:
        with open(args.out, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(report) + "\n")


if __name__ == "__main__":
    main()
