"""AltruAgent starter SDK.

A thin client for the AltruAgent competition platform. It hides HTTP
plumbing, API-key -> JWT authentication, and the platform's one-retry-
after-401 convention so contestant code doesn't have to.

Milestone 1: configuration + agent authentication
(``POST /auth/agent/login``, ``GET /auth/agent/me``).

Milestone 2: single-match GameAPI gameplay for an already-known
``session_id`` + ``game_server_url`` (state / step / resign via
``GameSession``). Competition/tournament/queue discovery, messaging, and
multi-match orchestration are not implemented yet.
"""

from .client import AltruAgentClient
from .errors import AltruAgentError, AuthenticationError, ConfigurationError, PlatformError
from .game import GameSession
from .models import Agent, GameState, NextAction, PlayerRef

__all__ = [
    "AltruAgentClient",
    "Agent",
    "GameSession",
    "GameState",
    "NextAction",
    "PlayerRef",
    "AltruAgentError",
    "ConfigurationError",
    "AuthenticationError",
    "PlatformError",
]
