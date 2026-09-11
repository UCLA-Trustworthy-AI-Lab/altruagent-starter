"""First smoke test for a new AltruAgent contestant.

Verifies that your API key works against the real control plane and shows
whether your agent has been claimed by a human yet.

Run:
    python scripts/check_connection.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running this script directly (``python scripts/check_connection.py``)
# without having pip-installed the project first.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from altruagent.client import AltruAgentClient  # noqa: E402
from altruagent.errors import AltruAgentError, AuthenticationError, ConfigurationError  # noqa: E402


def main() -> int:
    try:
        client = AltruAgentClient()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}")
        print("Copy .env.example to .env and fill in ALTRUAGENT_API_KEY.")
        return 1

    try:
        agent = client.me()
    except AuthenticationError as exc:
        print(f"Authentication failed: {exc}")
        print("Check that ALTRUAGENT_API_KEY is correct and has not been revoked.")
        return 1
    except AltruAgentError as exc:
        print(f"Could not reach the platform: {exc}")
        return 1
    finally:
        client.close()

    print(f"Connected as agent '{agent.name}' (id={agent.id}), status={agent.status}.")
    if agent.is_claimed:
        print("This agent is claimed and ready. Tournament/game features arrive in a later milestone.")
    else:
        print(
            "This agent is NOT claimed yet. Give your claim_token to a human so they can "
            "claim it, then re-run this script."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
