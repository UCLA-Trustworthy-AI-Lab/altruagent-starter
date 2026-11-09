# MCP session reuse

This page describes how the starter's gameplay client talks to GameAPI's MCP
endpoint: the problem session reuse fixes, the lifecycle it uses now, what
changed, and the measurements behind it. The code is
`altruagent/mcp_transport.py`; its module docstring is the reference.

## The original problem

The runtime is synchronous, and the official `mcp` Python SDK is async-only.
`mcp_transport.call_tool` bridged the two by running `asyncio.run()` around a
brand-new transport and `ClientSession` for **every tool call**. Each call
therefore sent four HTTP requests to GameAPI:

| Request | Why |
|---|---|
| `initialize` | new session |
| `notifications/initialized` | new session |
| `tools/call` | the call the agent asked for |
| `tools/list` | `ClientSession.call_tool` lists tools once per session to validate structured output, and every session was new |

Each request also paid for a new HTTP client: a TCP connection, an SSL
context, proxy detection, and per-request JWT validation on GameAPI. Only one
request in four was useful work.

## Session lifecycle now

`MCPConnection` keeps one initialized `ClientSession` per GameAPI `/mcp`
endpoint and reuses it for every call.

1. **First call.** The connection starts its own event loop on one daemon
   thread (`mcp-session <url>`). A *host task* on that loop opens
   `streamablehttp_client` and `ClientSession` and runs `initialize`. The host
   task alone enters and exits those context managers, as anyio requires.
2. **Every call.** The caller's thread submits `session.call_tool(...)` to the
   loop with `asyncio.run_coroutine_threadsafe` and blocks on the answer. The
   public API stays synchronous. Several threads may have calls in flight on
   one session; MCP matches answers to callers by request id.
3. **First call on a session.** The SDK sends one `tools/list` and caches the
   tools' output schemas for that session's lifetime. That cache is safe
   because GameAPI's 11 tools are fixed. Tool *results* are never cached:
   game state, legal actions, messages and results always come from a fresh
   `tools/call`.
4. **Failure.** In the SDK, an HTTP failure of any request (an error status, a
   dropped connection, a read timeout) ends the transport's task group, so the
   session dies with it. The host task records the root cause; every call
   waiting on that session fails with it, unwrapped exactly as before; the next
   call starts a fresh session. A call that hits the 45 s session timeout
   retires its session, so a request still stuck on the wire can't later take
   down an unrelated call.
5. **End.** `AltruAgentClient.close()` closes the client's sessions. Each
   worker already closes its client when its match ends, success or error.
   Closing cancels calls still in flight (they raise `MCPToolError`), ends the
   host, closes its `httpx.AsyncClient`, stops the loop and joins the thread.
   Garbage collection of the client and interpreter exit do the same. A closed
   connection reopens on its next call.

**Recovery is bounded: at most one automatic retry per call**, and only when
the call certainly had no effect or is a pure read.

| Failure | What happens |
|---|---|
| HTTP 401 (expired or rejected token) | Re-login once on the calling thread (`_current_access_token(force_relogin=True)`, same policy as before), then retry once on a new session. A refused re-login (seat taken, assignment ended) reaches the caller unchanged. |
| `Session terminated` (a stateful server forgot the session; GameAPI is stateless today) | Retry once on a new session. |
| Connection dropped on a session that had already answered, during a read-only tool (`get_game_state`, `wait_for_update`, `get_legal_actions`, `get_messages`, `get_result`, `get_game_config`, `list_games`, `get_agent_status`) | Retry once on a new session. This covers a kept-alive socket the server or a proxy closed while the agent was thinking. |
| Same, during `play_action`, `send_message` or `resign` | **Not retried.** Raised as a transient `MCPToolError`. The runner re-reads the state and carries on, as before, so a move is never sent twice. |
| A 401 or `Session terminated` that ended the session while *another* call was in flight on it, under `play_action`, `send_message` or `resign` | **Not retried.** The cause may be the other call's request, and this write may have reached the server. Raised as a transient `MCPToolError`, so the runner re-reads. (The runner makes one call at a time per seat; this covers code that shares one client between threads.) A lone write's 401 is still retried after re-login: that 401 was its own auth check, so the tool never ran. |
| Timeouts, 5xx, 429, anything else | Raised as before. The next call opens a new session; the runner keeps its own backoff and 90 s give-up. |

**Token changes.** A session keeps the bearer token it started with. When a
call finds that the client's current token differs (the client re-logged in
through another path, such as a REST call's 401 retry), that call retires the
session and starts a new one with the current token. Each call therefore uses
the client's current token, as it did with one session per call, without
first waiting for the old token to be refused.

**Processes.** Sessions live in module state, so separate processes never
share one. The runtime starts every worker with `spawn`. A child forked by
agent code (`os.register_at_fork`) drops the parent's connections, whose loop
threads don't exist in the child, and opens its own on first use.

**Why reuse is safe against GameAPI:** the MCP server is FastMCP with
`stateless_http=True` and `json_response=True`. The server holds no
per-connection session, a session is not tied to a match (the match is the
`session_id` tool argument), and every request is authenticated on its own.
An idle session between turns costs the server nothing.

## Implementation changes and compatibility

| File | Change |
|---|---|
| `altruagent/mcp_transport.py` | New `MCPConnection` (the session lifecycle above). `call_tool(client, mcp_url, name, arguments, *, httpx_client_factory=None)` keeps its signature and errors, and now reuses one connection per `(client, mcp_url)` from a weak registry. New `close_connections(client)`, `session_reuse_enabled()` and `READ_ONLY_TOOLS`. The error-unwrapping helpers are unchanged. |
| `altruagent/client.py` | `AltruAgentClient.close()` also closes the client's MCP sessions. |
| `README.md` | Configuration row and one paragraph in "How the runtime works". |
| `tests/test_mcp_session_reuse.py` | 32 new tests (below). |
| `scripts/mcp_bench/` | The benchmark and correctness tooling (see "Reproducing" below). |

Compatibility:

- No public signature changed. `MCPGameSession`, `run_game`, `run_match`, the
  workers, the supervisor and contestant code are untouched. Every existing
  test passes unchanged, including the tests that monkeypatch
  `altruagent.mcp_game.call_tool`.
- `MCPToolError` fields (`status_code`, `error_code`, `protocol_error`,
  `local_error`) and transient classification are unchanged. Messages after a
  retry now also name the first attempt's error.
- **Opt-out:** `ALTRUAGENT_MCP_SESSION_REUSE=0` (also `false`, `no`, `off`)
  opens and closes a session around every call. It uses the same code, so
  errors and retries behave the same. Use it as a tournament-day kill switch.
- **Threads:** each process gains one daemon thread per GameAPI endpoint it
  plays on (normally one per worker process). It never blocks process exit.
- **Calling from async code:** a call made from inside a running event loop
  now works; the old `asyncio.run()` raised there. The calling loop is blocked
  for the call's duration.
- **SDK:** the code uses only the SDK's public client API (`streamablehttp_client`,
  `ClientSession.initialize/call_tool`, `McpError`, `types`), the same as
  before, inside the pinned `mcp>=1.12,<2` range. It was tested on 1.30.0.
  `streamablehttp_client` is deprecated in 1.30 in favour of
  `streamable_http_client(http_client=...)`; moving to that is a separate
  change once the lower pin rises.

## Benchmarks

**Setup:**

- **Server:** Agent_ACP GameAPI (local checkout, branch
  `feature/gameapi-mcp-optimization`, run unmodified and read-only) on
  127.0.0.1, one uvicorn process. Local JWKS with real RS256 tokens (real
  per-request JWT validation) and a local fake backend; nothing left the
  machine.
- **Counting:** a counting ASGI wrapper outside the app counted every `/mcp`
  request by JSON-RPC method, and read GameAPI's process CPU time.
- **Clients:** complete tic-tac-toe matches played through the starter's real
  `run_game` + `MCPGameSession`. "Original" is `mcp_transport.py` as of
  commit `686e756` (the last commit before this change), loaded from git
  unchanged. All seats ran as threads of one client process.
- **Repetitions:** each figure is the mean of 2 repetitions per cell.

| Concurrent matches | Client | MCP HTTP requests / match | `initialize` / match | `tools/list` / match | `tools/call` / match | HTTP requests / tool call | GameAPI CPU (s) | Wall (s) | Tool-call latency p50 / mean (ms)* | Errors | Matches correct |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | original | 76 | 19 | 19 | 19 | 4.00 | 0.88 | 8.9 | 732 / 767 | 0 | 2/2 |
| 1 | **reuse** | **25** | **2** | **2** | 19 | **1.32** | **0.48** | **2.5** | **19 / 157** | 0 | 2/2 |
| 10 | original | 76 | 19 | 19 | 19 | 4.00 | 13.66 | 45.5 | 3477 / 3720 | 0 | 20/20 |
| 10 | **reuse** | **24.8** | **2** | **2** | 18.8 | **1.32** | **5.32** | **14.6** | **420 / 1083** | 0 | 20/20 |
| 25 | original | 76 | 19 | 19 | 19 | 4.00 | 36.48 | 109.8 | 9048 / 9139 | 0 | 50/50 |
| 25 | **reuse** | **24.7** | **2** | **2** | 18.7 | **1.32** | **15.20** | **38.1** | **1152 / 2712** | 0 | 50/50 |

\* Client-side time per tool call, excluding `wait_for_update` (a long poll).
The reuse mean includes each seat's first call, which pays for `initialize`
and `tools/list`.

**What this shows:**

- **HTTP traffic per match −67%:** from 76 to about 25 requests. That is
  1.32 HTTP requests per tool call instead of 4.00, in line with the expected
  `(calls + 3 × seats) / calls`. `initialize` and `tools/list` drop from one per
  tool call to one per seat per match (2 for tic-tac-toe).
- **GameAPI CPU 2.4–2.6× lower** under concurrent load (1.8× for a single
  match, where fixed background work weighs more). The figure includes
  GameAPI's own background loops, so the true per-request saving is somewhat
  larger. This is consistent with the ~3.4× from GameAPI's own reuse benchmark.
- **Wall time about 3× shorter** at every level. Median tool-call latency is
  8–40× lower.
- **Correctness:** every match finished `completed`, with no client errors and
  no refused moves, in both modes.

**Caveats:** all 50 seats of the 25-match run share one client process and
one laptop with the server. Latency under load reflects that contention, and
the original client's per-call SSL context and connection setup on the client
side as well. In production each seat is its own process on its own machine,
so absolute latencies will differ. The request counts and the direction of the
CPU result do not depend on this.

### Other games and token refresh on the real server

These ran on the same local GameAPI, with Pokémon battles on the local
Showdown container. Each row is one run. Tool-call counts differ between
modes because Werewolf roles and Pokémon battles are random, so compare the
per-call ratio.

| Game (matches at once) | Client | `initialize` / match | MCP HTTP / match | HTTP / tool call | GameAPI CPU (s) | Wall (s) | Result |
|---|---|---|---|---|---|---|---|
| Werewolf, 7 seats, messaging (2) | original | 192.5 | 770 | 4.00 | 12.16 | 76.3 | 2/2 completed |
| Werewolf, 7 seats, messaging (2) | **reuse** | **7** | **224** | **1.10** | **3.11** | **11.3** | 2/2 completed |
| Repeated PD (2) | original | 45 | 180 | 4.00 | 3.30 | 27.5 | 2/2 completed |
| Repeated PD (2) | **reuse** | **2** | **51** | **1.13** | **1.28** | **4.6** | 2/2 completed |
| Pokémon gen9 singles: team preview + Showdown battle (1) | original | 456 | 1824 | 4.00 | 11.17 (24 ms/call) | 165.7 | completed |
| Pokémon gen9 singles: team preview + Showdown battle (1) | **reuse** | **2** | **150** | **1.04** | **1.56 (11 ms/call)** | **5.1** | completed |
| Pokémon gen9 OU draft: draft + battle (1) | reuse | 2 | 278 | 1.02 | 1.84 | 14.5 | completed |
| Werewolf, tokens expire after 4 s (2) | reuse | 14 | 242 | 1.21 | 3.83 | 17.1 | 2/2 completed, 14 re-logins (one per seat) |
| Pokémon singles, tokens expire after 8 s (1) | reuse | 4 | 188 | 1.07 | 1.73 | 10.7 | completed, 2 re-logins |

The expiry rows use real expired JWTs that GameAPI rejected with HTTP 401.
Each seat re-logged in exactly once, opened exactly one new session, and
played on to the end.

### Werewolf `WRONG_PHASE` replies: investigated, not a regression

In reuse mode, a few `send_message` calls per two-match Werewolf run were
answered `WRONG_PHASE` (0–6 per match), against none with the original
client. Every one was checked as follows. Nothing in the client or the server
was slowed down to hide them.

**Mechanism.** The runner's default agent ends discussion (`terminate`) as
soon as a discussion round starts. Until the round closes, each new message
wakes the seats that already ended it, and the runner sends `terminate` again.
That repeat is an idempotent no-op on the server. The round closes when the
last living seat's `terminate` lands. A seat that was re-sending its no-op at
that moment is answered `WRONG_PHASE`. Faster calls mean more seats are awake
and mid-call in that window. In all 16 traced cases, the refused call was such
a repeat, and the seat's last read said `messaging`.

**Server side (Agent_ACP, read only).** `send_message` and `play_action`
check the phase under the session lock and raise `WrongPhaseError` *before*
anything is recorded (`runtime_adapters/openspiel_adapter.py`, in
`send_message` before the message is appended and in `apply_action` before the
move is applied).

**Evidence:**

| Check | Result |
|---|---|
| `probe_refusals.py`: every seat's full state and the whole message log, read before and after 47 deliberately refused calls (`terminate` after the round closed, a move during discussion, a stale `state_version`, a move out of turn) | 47/47 unchanged |
| `check_trace.py` on 10 traced Werewolf matches (8 reuse, including 2 with expiring tokens; 2 original) and 2 tic-tac-toe matches | 12/12 pass every check |
| Moves: accepted `play_action`s each used a distinct `state_version`, advanced it by exactly one, and their number equals the server's final version | 186 moves, no duplicate, none lost |
| Messages: the server's message log is exactly the set of messages that `send_message` answered as recorded, and no seat ended discussion twice in one round | 132 messages, exact match |
| Results: every seat saw the same termination reason and returns, and GameAPI reported the result exactly once, with the same returns | all matches |
| Recovery: after every refusal the seat's next call was a fresh read, which showed the new phase | 16/16 |

**Conclusion:** these are normal race refusals of a no-op, caused by faster
calls. They change nothing and the runner recovers as designed, so no fix is
needed in the transport. A separate, optional runner improvement would be to
skip re-sending `terminate` for a round this seat has already ended. That
would remove most of these replies and some traffic, in either mode.

### Reproducing

The server launcher, the match driver and the two correctness checks are in
[`scripts/mcp_bench/`](../scripts/mcp_bench/README.md), with step-by-step
commands. They need a local Agent_ACP checkout and talk only to 127.0.0.1.

## Tests

`tests/test_mcp_session_reuse.py` drives the real SDK client (framing,
`initialize`, `tools/list`, anyio error wrapping) against an in-process fake
GameAPI over `httpx.MockTransport`. It opens no sockets. The fake game has
phases (`draft` → `teambuild` → `moving`), bumps `state_version` on every
move, derives legal actions from the current version, and refuses stale or
illegal moves.

| # | Area | Tests |
|---|---|---|
| 1 | One initialization for many calls | 20 calls → 1 `initialize`, 1 `tools/list`, 23 HTTP requests; one session per client and endpoint via `call_tool`; the opt-out restores one session per call. |
| 2 | Legal actions change across phases | Every phase's actions and version are read fresh, never repeated. A server-side change between two identical reads is seen. |
| 3 | Full gameplay | A whole match through `run_game` on one session, with no stale or illegal moves. |
| 4 | Reconnection | A dropped reused session is retried once for a read. A dropped `play_action`, `send_message` or `resign` is not resent, and the next call reconnects. A timed-out move is not resent. A drop on a fresh session is left to the runner. A 502 ends the session and the next call reinitializes. `run_game` survives a mid-match drop. A timed-out call retires its session. |
| 5 | Auth | A mid-session token expiry triggers one re-login and the new session is reused afterwards. A token renewed elsewhere on the client is used from the next call, with no 401. A refused re-login reaches the caller unchanged. A second 401 stops. A lone write is still retried after its own 401. |
| 6 | Simultaneous matches | 4 matches in parallel threads, one session each, all correct. Two seat clients in one process never share a session or a token. A forked child starts its own sessions. |
| 7 | Concurrent calls | 8 threads × 25 calls on one session, every answer matched to its caller, 1 `initialize`. A move or message whose session another call's 401 ended is not resent, while the read that got the 401 is retried. A call from inside a running event loop. |
| 8 | Cleanup | `close()` joins the loop thread, is idempotent and reopens on the next call. Closing during a call fails that call promptly. Failed sessions leave no hosts behind. `AltruAgentClient.close()` ends sessions. A garbage-collected client releases its session. |

All existing tests pass unchanged, and the full suite passes (799 tests). No
test opens a socket: every MCP test goes through `httpx.MockTransport`.

## Limitations and follow-ups

- **One failure ends all calls on that session.** That is how the SDK
  transport works: one request's HTTP error cancels its task group. With one
  seat per process and mostly sequential calls this rarely matters. A
  real-time agent issuing parallel calls will see a sibling call fail with the
  same transient error, and the runner handles that as any transient error.
- **`tools/list` still runs once per session.** Skipping it would mean
  pre-filling the SDK's private `_tool_output_schemas`, which is not a
  supported API.
- **Werewolf agents now race the discussion window more often.** This is
  harmless and handled (see above), but it shows as extra `WRONG_PHASE` lines
  in the logs.
- **GameAPI finding (not caused by this change):** in local Pokémon OU draft
  runs, 4 of about 12 matches ended `battle_start_failed`. GameAPI logged
  "no legal draft pick for seat … at pick 9/10": its Item Clause reserve did
  not leave the seat a legal card. Draft legality (`draft_state.py`,
  `available_card_ids_for_seat`) depends only on the random pool and the
  picks made, and both client modes completed drafts in alternating runs.
  The benchmark agent always takes the first offered card, which may make
  this more likely than real agents do. Worth reporting to Agent_ACP.
- **Not measured:** Red Alert and Honor of Kings, which need their game
  runners. Their real-time loops use the same `call_tool` path.
- **A client that is never closed keeps its session thread** until garbage
  collection frees the client. Workers always close their clients. Code that
  creates clients ad hoc should close them, or use them as context managers.
- **The benchmark needs a local Agent_ACP checkout** with its GameAPI
  virtualenv (and Docker for Pokémon). It is not part of the test suite.

## Updates for other docs and runtimes

- **Official MCP integration docs (Agent_ACP `backend/skill`, GameAPI docs):**
  recommend one `ClientSession` per agent process, initialized once and
  reused for every call, rather than connect, call, disconnect. Say that tool
  definitions are stable within a session, so one `tools/list` is enough, and
  that state and legal actions must be read on every turn. Document that
  re-sending the bearer token on each request is how refresh works: there is
  no server session to renew, and a 401 means "log in again, then open a new
  session".
- **Other agent runtimes** (not audited in this change; worth checking
  `altruagent-aws-runtime` and Agent_ACP's `scripts/*_mcp_play.py`): any
  client that opens a `streamablehttp_client`/`ClientSession` per call has
  the same 4× overhead. Runtimes built on this starter, including the LLM
  examples, get reuse automatically through `call_tool`.
  Async runtimes can simply keep one `ClientSession` open for the match.
  Sync runtimes can use this module's pattern: one loop thread and one host
  task.
- **Server side (Agent_ACP, not changed here):** the server's own version
  lookup and logging optimisations on the Agent_ACP feature branch are
  independent of this change and stack with it.
