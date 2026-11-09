"""An isolated, local GameAPI for the MCP session-reuse benchmark.

Runs Agent_ACP's GameAPI app, unmodified, on 127.0.0.1. Every external
dependency is replaced by something local or switched off:

  - a local JWKS server and RS256 key, so GameAPI validates a real JWT on
    every request (keys are generated in memory for each run; nothing is
    read from or written to disk);
  - a local fake backend that records GameAPI's match-result reports;
  - no S3 game logs, no internal token, no OpenRA runner;
  - Pokémon matches use a local Showdown server (``--showdown-ws``).

Both helpers come from Agent_ACP's own local dev harness,
``gameapi/scripts/redalert_local_e2e.py``.

An outer ASGI layer counts every HTTP request to ``/mcp`` by JSON-RPC method
(and tool name), and adds a few local-only endpoints:

  GET  /_bench/counts                    counts, plus this process's CPU seconds
  POST /_bench/reset                     zero the counts
  GET  /_bench/mint?agent_id=..&ttl=..   a bearer token for that agent id
  GET  /_bench/completions               the result reports GameAPI sent

Run it with Agent_ACP's GameAPI virtualenv, from outside the Agent_ACP
checkout, so nothing is written into it:

  PYTHONDONTWRITEBYTECODE=1 <Agent_ACP>/gameapi/.venv/Scripts/python.exe \\
      scripts/mcp_bench/gameapi_server.py --gameapi-dir <Agent_ACP>/gameapi

See scripts/mcp_bench/README.md.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import threading
from pathlib import Path
from urllib.parse import parse_qs

sys.dont_write_bytecode = True

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument(
    "--gameapi-dir",
    default=os.environ.get("AGENT_ACP_GAMEAPI_DIR"),
    help="Agent_ACP's gameapi/ directory (or set AGENT_ACP_GAMEAPI_DIR)",
)
parser.add_argument("--port", type=int, default=8791)
parser.add_argument(
    "--showdown-ws",
    default="ws://127.0.0.1:8000/showdown/websocket",
    help="a LOCAL Pokémon Showdown websocket (only Pokémon matches use it)",
)
args = parser.parse_args()
if not args.gameapi_dir:
    parser.error("--gameapi-dir (or AGENT_ACP_GAMEAPI_DIR) is required")
if not args.showdown_ws.startswith(("ws://127.0.0.1", "ws://localhost")):
    parser.error("--showdown-ws must be a local server")

sys.path.insert(0, str(Path(args.gameapi_dir) / "scripts"))
from redalert_local_e2e import FakeBackend, JwksServer, SigningKey  # noqa: E402

key = SigningKey.generate()
jwks = JwksServer(key, "127.0.0.1", 0).start()
backend = FakeBackend("127.0.0.1", 0).start()
os.environ.update(
    {
        "GAMEAPI_JWKS_URL": jwks.jwks_url,
        "GAMEAPI_BACKEND_URL": backend.url,
        "GAMEAPI_S3_GAME_LOGS_BUCKET": "",
        "GAMEAPI_INTERNAL_TOKEN": "",
        "GAMEAPI_OPENRA_RUNNER_URL": "",
        "GAMEAPI_POKEMON_SHOWDOWN_WS": args.showdown_ws,
        "GAMEAPI_LOG_LEVEL": "WARNING",
        # The benchmark plays many matches from one address.
        "GAMEAPI_RATE_LIMIT_STATE": "100000/minute",
    }
)

import uvicorn  # noqa: E402
from gameapi.app import app as gameapi_app  # noqa: E402

lock = threading.Lock()
counts: collections.Counter = collections.Counter()


def _count(body: bytes) -> None:
    try:
        message = json.loads(body) if body else {}
    except ValueError:
        message = {}
    messages = message if isinstance(message, list) else [message]
    with lock:
        counts["http_mcp"] += 1
        for m in messages:
            method = m.get("method", "?") if isinstance(m, dict) else "?"
            counts[f"method:{method}"] += 1
            if method == "tools/call":
                counts[f"tool:{(m.get('params') or {}).get('name')}"] += 1


async def _json(send, status, payload):
    data = json.dumps(payload, default=str).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": data})


async def app(scope, receive, send):
    if scope["type"] != "http":
        return await gameapi_app(scope, receive, send)
    path = scope["path"]
    if path.startswith("/_bench/"):
        if path == "/_bench/counts":
            with lock:
                snapshot = dict(counts)
            times = os.times()
            return await _json(send, 200, {"counts": snapshot, "cpu_seconds": times.user + times.system})
        if path == "/_bench/reset":
            with lock:
                counts.clear()
            return await _json(send, 200, {"ok": True})
        if path == "/_bench/mint":
            q = parse_qs(scope.get("query_string", b"").decode())
            token = key.mint(q["agent_id"][0], ttl_seconds=int(q.get("ttl", ["43200"])[0]))
            return await _json(send, 200, {"token": token})
        if path == "/_bench/completions":
            return await _json(send, 200, {"completions": list(backend.completions)})
        return await _json(send, 404, {"error": "not_found"})
    if path.rstrip("/") == "/mcp" and scope["method"] == "POST":
        chunks: list[bytes] = []

        async def counting_receive():
            message = await receive()
            if message["type"] == "http.request":
                chunks.append(message.get("body", b""))
                if not message.get("more_body"):
                    _count(b"".join(chunks))
            return message

        return await gameapi_app(scope, counting_receive, send)
    if path.rstrip("/") == "/mcp":
        with lock:
            counts["http_mcp"] += 1
            counts[f"http_{scope['method']}"] += 1
    return await gameapi_app(scope, receive, send)


print(f"Local GameAPI bench server on http://127.0.0.1:{args.port}", flush=True)
uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning")).run()
