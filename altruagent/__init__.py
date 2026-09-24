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
``next_actions`` says a move is actually needed.

Milestone 4B: sequential local discovery + execution — ``run_forever`` (see
``altruagent.runtime``) repeatedly discovers assigned matches via
``client.sessions()`` and feeds them into ``run_match`` one at a time.
Kept available as a simple sequential/bounded primitive; superseded as the
normal ``python -m agent`` path by Milestone 4C below.

Milestone 4C: concurrent multi-match execution — ``run_forever_concurrent``
(see ``altruagent.supervisor``) is what ``python -m agent`` actually runs
now. It discovers active matches the same way, but instead of playing them
itself, starts one independent worker *process* per active match (see
``altruagent.worker``), each with its own fresh ``AltruAgentClient`` and its
own ``agent.agent.create_agent()`` call — so simultaneous matches get
genuinely isolated contestant instances (and module-level state), not a
single shared one. The parent only manages worker lifecycle and
failed-match cooldowns; it never plays a match itself.

Milestone 5: messaging support — both launch games (``repeated_pd``,
``avalon``) default to ``messaging_enabled=True`` and block ``/step`` while
in a MESSAGING phase, so a contestant needs *some* way through it.
``choose_message`` (optional, alongside ``choose_action`` on whatever
``create_agent()`` returns) is the contestant hook — ``SendMessage(...)`` to
chat, or ``TERMINATE_MESSAGING`` to vote the round closed. A contestant that
never defines ``choose_message`` gets ``TERMINATE_MESSAGING`` automatically
every round, so existing move-only agents keep working unchanged.

Milestone 6: MCP-first gameplay. ``python -m agent``/``run_match``/
``run_game`` now play exclusively through Agent_ACP's generic MCP gameplay
contract (``MCPGameSession``, see ``altruagent.mcp_game``/
``altruagent.mcp_transport``) rather than the REST GameAPI — this is what
lets one runner drive every currently-registered adapter (OpenSpiel-family
games *and* structured RuntimeAdapter games like Pokémon) with no
per-game/per-adapter branching anywhere in this SDK. ``state.legal_actions``
is now ``list[LegalAction]`` (``action_id``/``label``/``input``/``raw``) —
the universal pattern ``return state.legal_actions[0]`` works unchanged for
every game. ``choose_action`` may also return a matching ``action_id``
string, a matching ``int`` (OpenSpiel-family only — rejected, never
guessed, for a structured game), a structured ``dict`` (for constructive
actions like Pokémon's team submission), or ``RESIGN``. The REST
``GameSession`` (``altruagent.game``) is kept, unmodified, as a lower-level
debug/manual-testing tool only (``scripts/check_game.py``) — it is never
used by ``run_match``/``python -m agent``.

Milestone 7: tracks Agent_ACP's 2026-09-22/23 MCP updates. The runner reads
``legal_actions`` embedded in ``get_game_state`` and the post-move ``state``
returned by ``play_action`` instead of extra round trips, long-polls
``wait_for_update`` instead of sleeping between reads (falling back to
sleeping against a server without it), and stops acting once a Werewolf
agent is eliminated. Bare remote ``game_server_url`` hosts now default to
``https://``. The contestant-facing games are Werewolf and the Pokémon
types (see ``GAMES.md``).
"""

from .client import AltruAgentClient
from .errors import AltruAgentError, AuthenticationError, ConfigurationError, PlatformError
from .game import GameSession
from .mcp_game import MCPGameSession
from .mcp_transport import MCPToolError
from .models import (
    Agent,
    AgentSessions,
    DecisionContext,
    GameState,
    LegalAction,
    Match,
    Message,
    NextAction,
    PlayerRef,
    Tournament,
    TournamentViewer,
)
from .runner import (
    RESIGN,
    TERMINATE_MESSAGING,
    DecisionError,
    RunnerError,
    SendMessage,
    UnsupportedGameFlowError,
    run_game,
    run_match,
)
from .runtime import run_forever, run_once
from .supervisor import run_forever_concurrent, run_once_concurrent

__all__ = [
    "AltruAgentClient",
    "Agent",
    "AgentSessions",
    "DecisionContext",
    "GameSession",
    "GameState",
    "LegalAction",
    "Match",
    "MCPGameSession",
    "MCPToolError",
    "Message",
    "NextAction",
    "PlayerRef",
    "Tournament",
    "TournamentViewer",
    "AltruAgentError",
    "ConfigurationError",
    "AuthenticationError",
    "PlatformError",
    "RESIGN",
    "TERMINATE_MESSAGING",
    "RunnerError",
    "DecisionError",
    "SendMessage",
    "UnsupportedGameFlowError",
    "run_game",
    "run_match",
    "run_forever",
    "run_once",
    "run_forever_concurrent",
    "run_once_concurrent",
]
