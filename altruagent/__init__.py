"""AltruAgent starter SDK.

A thin client for the AltruAgent competition platform's control plane. It
hides HTTP plumbing, API-key -> JWT authentication, and the platform's
one-retry-after-401 convention so contestant code doesn't have to.

Milestone 1 scope: configuration + agent authentication only
(``POST /auth/agent/login``, ``GET /auth/agent/me``). Competitions,
tournaments, queues, and GameAPI gameplay are not implemented yet.
"""

from .client import AltruAgentClient
from .errors import AltruAgentError, AuthenticationError, ConfigurationError, PlatformError
from .models import Agent

__all__ = [
    "AltruAgentClient",
    "Agent",
    "AltruAgentError",
    "ConfigurationError",
    "AuthenticationError",
    "PlatformError",
]
