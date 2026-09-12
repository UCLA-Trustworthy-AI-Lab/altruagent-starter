"""Entry point for `python -m agent`.

Authenticates using the existing `.env`/environment configuration,
discovers matches assigned to this agent, and plays them one at a time
through `agent.agent.choose_action` via the committed runtime
(`altruagent.runtime.run_forever`). Sequential only — see the README for
why. Stops cleanly on Ctrl+C; does not resign or otherwise touch any match
on shutdown.

This file is deliberately thin — the actual discovery/execution loop lives
in `altruagent.runtime`, and match play itself in `altruagent.runner`.
"""

from __future__ import annotations

from typing import Callable

from altruagent.client import AltruAgentClient
from altruagent.errors import AltruAgentError, ConfigurationError
from altruagent.runtime import run_forever

from . import agent as agent_module


def _resolve_choose_action(module: object) -> Callable:
    """Look up ``choose_action`` on the given agent module. Fails clearly
    (no fallback, no signature inspection, no alternate names) if it's
    missing or not callable.
    """
    choose_action = getattr(module, "choose_action", None)
    if not callable(choose_action):
        raise ValueError(
            "agent/agent.py must define a callable choose_action(state, context) "
            "function."
        )
    return choose_action


def main() -> int:
    try:
        choose_action = _resolve_choose_action(agent_module)
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
    print("Watching for assigned matches — one at a time. Press Ctrl+C to stop.\n")

    try:
        run_forever(client, choose_action, agent_id=me.id)
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
