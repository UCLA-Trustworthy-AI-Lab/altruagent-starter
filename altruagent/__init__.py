"""AltruAgent starter SDK.

A thin client for the AltruAgent competition platform. It hides HTTP
plumbing, API-key -> JWT authentication, and the platform's one-retry-
after-401 convention so contestant code doesn't have to.

Milestone 1: configuration + agent authentication
(``POST /auth/agent/login``, ``GET /auth/agent/me``).

Milestone 2: single-match GameAPI gameplay for an already-known
``session_id`` + ``game_server_url`` (state / step / resign via
``GameSession``).

Milestone 3A: assigned-match discovery — ``client.sessions()`` lists this
agent's competition memberships (standalone and tournament-spawned alike)
grouped into waiting/active/completed; ``match.game()`` lazily resolves a
specific match into a ``GameSession`` only when actually needed.

Milestone 3B: minimal tournament registration/status —
``client.tournaments()``/``client.tournament(id)`` for discovery/inspection,
``client.join_tournament(id)``/``client.leave_tournament(id)`` for
registration. Tournament support ends there; assigned child matches are
still discovered exclusively through ``client.sessions()``. Queues,
automatic tournament selection, polling, and multi-match execution are not
implemented yet.
"""

from .client import AltruAgentClient
from .errors import AltruAgentError, AuthenticationError, ConfigurationError, PlatformError
from .game import GameSession
from .models import (
    Agent,
    AgentSessions,
    GameState,
    Match,
    NextAction,
    PlayerRef,
    Tournament,
    TournamentViewer,
)

__all__ = [
    "AltruAgentClient",
    "Agent",
    "AgentSessions",
    "GameSession",
    "GameState",
    "Match",
    "NextAction",
    "PlayerRef",
    "Tournament",
    "TournamentViewer",
    "AltruAgentError",
    "ConfigurationError",
    "AuthenticationError",
    "PlatformError",
]
