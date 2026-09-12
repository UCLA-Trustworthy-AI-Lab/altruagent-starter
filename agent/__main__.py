"""Entry point for `python -m agent`.

Authenticates using the existing `.env`/environment configuration,
discovers matches assigned to this agent, and plays them CONCURRENTLY — one
independent worker process per active match — through
`agent.agent.create_agent()` via the committed supervisor
(`altruagent.supervisor.run_forever_concurrent`). Stops cleanly on Ctrl+C;
does not resign or otherwise touch any match on shutdown.

This file is deliberately thin — discovery/worker lifecycle lives in
`altruagent.supervisor`, one match's play loop in `altruagent.runner`, and
each worker's own setup in `altruagent.worker`.

IMPORTANT (Windows multiprocessing): the `if __name__ == "__main__":` guard
at the bottom of this file is not just style — `multiprocessing`'s `spawn`
start method (required on Windows, used here unconditionally so behavior is
identical everywhere) re-imports this exact module in every worker process.
Without the guard, each freshly-spawned worker would re-run `main()` itself
and spawn further workers recursively.
"""

from __future__ import annotations

from typing import Callable

from altruagent.client import AltruAgentClient
from altruagent.errors import AltruAgentError, ConfigurationError
from altruagent.supervisor import run_forever_concurrent

from . import agent as agent_module


def _resolve_create_agent(module: object) -> Callable:
    """Look up ``create_agent`` on the given agent module. Fails clearly
    (no fallback name, no signature inspection) if it's missing or not
    callable. Deliberately does NOT call it here — construction happens
    once per match, inside that match's own worker process, not once in
    the parent (see altruagent.worker.run_worker).
    """
    create_agent = getattr(module, "create_agent", None)
    if not callable(create_agent):
        raise ValueError(
            "agent/agent.py must define a callable create_agent() function "
            "that returns your decision logic (a function, or an object "
            "exposing choose_action(state, context))."
        )
    return create_agent


def main() -> int:
    try:
        _resolve_create_agent(agent_module)  # validated eagerly; not invoked here
    except ValueError as exc:
        print(f"Startup error: {exc}")
        return 1

    try:
        client = AltruAgentClient()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}")
        print("Copy .env.example to .env and fill in ALTRUAGENT_API_KEY.")
        return 1

    try:
        me = client.me()
    except AltruAgentError as exc:
        print(f"Could not authenticate: {exc}")
        client.close()
        return 1

    if not me.is_claimed:
        print(
            f"Agent '{me.name}' is not claimed yet (status={me.status}). "
            "Have a human claim it with your claim_token before running this."
        )
        client.close()
        return 1

    print(f"Authenticated as agent '{me.name}' ({me.id}).")
    print(
        "Watching for assigned matches — each gets its own process. "
        "Press Ctrl+C to stop.\n"
    )

    try:
        run_forever_concurrent(client, agent_id=me.id)
        return 0
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    except AltruAgentError as exc:
        print(f"\nStopped due to an unrecoverable error: {exc}")
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
