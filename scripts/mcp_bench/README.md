# MCP session-reuse benchmark

These scripts measure what the starter's MCP gameplay client costs GameAPI,
before and after session reuse, and check that matches stay correct. Results
and analysis are in [docs/mcp-session-reuse.md](../../docs/mcp-session-reuse.md).

Everything runs on 127.0.0.1. The scripts never contact the platform, a
deployed GameAPI or any AWS service. The client refuses any `--server` that
isn't local.

| Script | What it does |
|---|---|
| `gameapi_server.py` | Runs Agent_ACP's GameAPI app, unmodified, on 127.0.0.1. It adds a local JWKS (real RS256 JWTs, validated on every request), a local fake backend that records result reports, and a counting layer for `/mcp` requests by JSON-RPC method. |
| `bench_client.py` | Plays complete matches through the starter's real `run_game` + `MCPGameSession`, one thread per seat. `--mode original` loads `altruagent/mcp_transport.py` from git as of `--baseline-ref` (default: commit `686e756`, the last before session reuse). `--mode reuse` uses the working tree. It prints traffic, server CPU, latency, errors and completion. |
| `check_trace.py` | Checks a `--trace` file against the server's own records: no move lost or applied twice, the message log matches the accepted sends, all seats and the result report agree, and every refusal was a race followed by a fresh read. |
| `probe_refusals.py` | Steps through a Werewolf match. It sends calls the server must refuse (`WRONG_PHASE`, `STALE_STATE`, `NOT_YOUR_TURN`) and shows that every seat's state and the message log are identical before and after each one. |

## Requirements

- A local checkout of Agent_ACP, with GameAPI's virtualenv set up
  (`<Agent_ACP>/gameapi/.venv`). The server imports the app and the local
  JWKS and fake-backend helpers from `gameapi/scripts/redalert_local_e2e.py`.
  Nothing is written into the checkout: run the server from outside it, with
  `PYTHONDONTWRITEBYTECODE=1`.
- This repo's own environment for the client scripts.
- Pokémon games only: a local Showdown server, e.g. Agent_ACP's
  `pokemon-showdown-local` container listening on port 8000.

No credentials are needed. Signing keys are generated in memory on each run,
and each seat's token is minted by the local server (`/_bench/mint`).

## Reproduce the measurements

Start the server and leave it running (bash; adjust the venv path on Linux or macOS):

```bash
export AGENT_ACP_GAMEAPI_DIR=/path/to/Agent_ACP/gameapi
PYTHONDONTWRITEBYTECODE=1 "$AGENT_ACP_GAMEAPI_DIR/.venv/Scripts/python.exe" \
    scripts/mcp_bench/gameapi_server.py --port 8791
```

In another shell, from the repo root:

```bash
# Before/after, tic-tac-toe, 1, 10 and 25 matches at once (the docs use 2 runs of each)
for n in 1 10 25; do
  for mode in original reuse; do
    python scripts/mcp_bench/bench_client.py --mode $mode --game tic_tac_toe --matches $n --out results.jsonl
  done
done

# Other games
python scripts/mcp_bench/bench_client.py --mode reuse --game werewolf --matches 2 --out results.jsonl
python scripts/mcp_bench/bench_client.py --mode reuse --game repeated_pd --matches 2 --out results.jsonl
python scripts/mcp_bench/bench_client.py --mode reuse --game pokemon_gen9same --out results.jsonl
python scripts/mcp_bench/bench_client.py --mode reuse --game pokemon_gen9ou_draft --out results.jsonl

# Token expiry mid-match: each seat's first token lives 4 s, so GameAPI answers 401
python scripts/mcp_bench/bench_client.py --mode reuse --game werewolf --matches 2 --first-ttl 4 --out results.jsonl
```

Each run appends one JSON line to `--out`. The fields are:

- `per_match`: `initialize`, `tools_list`, `tools_call` and HTTP requests per match;
- `http_requests_per_tool_call`;
- `server_cpu_seconds` and `wall_seconds`;
- client latency, excluding `wait_for_update`;
- `client_errors`, `matches_completed` and `relogins`.

## Check correctness

```bash
python scripts/mcp_bench/bench_client.py --mode reuse --game werewolf --matches 2 --trace trace.jsonl
python scripts/mcp_bench/bench_client.py --mode original --game werewolf --matches 2 --trace trace.jsonl
python scripts/mcp_bench/check_trace.py trace.jsonl      # exit status 1 on any failure
python scripts/mcp_bench/probe_refusals.py               # exit status 1 if a refusal changed anything
```

The end-of-match reads that `check_trace.py` uses happen after the measured
window, so they don't affect the traffic counts.

## Reading the numbers

- With reuse, each seat costs one `initialize`, one
  `notifications/initialized` and one `tools/list` per match. HTTP requests
  per tool call are therefore `(calls + 3 × seats) / calls`, against 4.00
  before.
- Server CPU is the whole GameAPI process, including its background loops.
- All seats share one client process and the machine with the server, so
  latency under load includes that contention. Production seats run in
  separate processes on other machines.
- Werewolf roles and Pokémon battles are random, so tool-call counts vary
  between runs. Compare per-call ratios.
