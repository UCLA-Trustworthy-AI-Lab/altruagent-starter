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
still discovered exclusively through ``client.sessions()``.

Milestone 4A: the contestant decision contract and a single-match execution
primitive — ``run_match``/``run_game`` (see ``altruagent.runner``) own the
state -> decide -> submit loop for one already-known match, calling
contestant-supplied decision logic (a plain ``choose_action(state, context)``
function, or an object exposing one — no base class) only when
``next_actions`` says a move is actually needed. Automatic assigned-match
discovery, polling/backoff, and multi-match concurrency are not implemented
yet — this milestone plays exactly one match, to completion, on request.
"""

from .client import AltruAgentClient
from .errors import AltruAgentError, AuthenticationError, ConfigurationError, PlatformError
from .game import GameSession
from .models import (
    Agent,
    AgentSessions,
    DecisionContext,
    GameState,
    Match,
    NextAction,
    PlayerRef,
    Tournament,
    TournamentViewer,
)
from .runner import (
    RESIGN,
    DecisionError,
    RunnerError,
    UnsupportedGameFlowError,
    run_game,
    run_match,
)

__all__ = [
    "AltruAgentClient",
    "Agent",
    "AgentSessions",
    "DecisionContext",
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
    "RESIGN",
    "RunnerError",
    "DecisionError",
    "UnsupportedGameFlowError",
    "run_game",
    "run_match",
]
