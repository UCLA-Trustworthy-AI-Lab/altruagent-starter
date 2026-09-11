"""GameSession — a handle to one already-known GameAPI match.

Wraps exactly three endpoints, verified directly against
Agent_ACP/gameapi/src/gameapi/routes/games.py:

- ``GET /games/{session_id}``        (games.py:333, ``get_game_state``)
- ``POST /games/{session_id}/step``  (games.py:358, ``step_game``)
- ``POST /games/{session_id}/resign`` (games.py:477, ``resign_game``)

All three require the same bearer JWT the control plane issued (GameAPI
validates it independently via JWKS — see
gameapi/src/gameapi/auth/jwt_validator.py — but it's the identical Supabase
token). ``GameSession`` never manages its own auth: every call goes through
``AltruAgentClient.request()``, which already knows how to log in and retry
once on a 401. This intentionally does not implement messaging, spectating,
replay, or cancel — those are out of scope for this milestone.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .errors import ConfigurationError
from .models import GameState

if TYPE_CHECKING:
    from .client import AltruAgentClient

_HAS_SCHEME = re.compile(r"^https?://", re.IGNORECASE)


def _normalize_game_server_url(game_server_url: str) -> str:
    """Apply the platform's own game_server_url normalization rule.

    The control plane returns this as a bare host, not a full URL — see
    Agent_ACP/backend/src/services/gameAPIService.ts:79-81
    (``getGameAPIServerUrl`` strips the scheme before returning it) — and
    documents the fix-up itself in backend/skill/03-competitions.md
    ("If host-only, prepend http://"). This does the same thing client-side.
    """
    value = (game_server_url or "").strip()
    if not value:
        raise ConfigurationError("game_server_url must not be empty.")
    if not _HAS_SCHEME.match(value):
        value = f"http://{value}"
    return value.rstrip("/")


class GameSession:
    """One concrete GameAPI match, identified by ``session_id`` +
    ``game_server_url``. Construct via ``AltruAgentClient.game(...)`` rather
    than directly.
    """

    def __init__(self, client: "AltruAgentClient", *, session_id: str, game_server_url: str) -> None:
        if not session_id:
            raise ConfigurationError("session_id must not be empty.")
        self._client = client
        self.session_id = session_id
        self.game_server_url = _normalize_game_server_url(game_server_url)

    def _url(self, suffix: str = "") -> str:
        return f"{self.game_server_url}/games/{self.session_id}{suffix}"

    def state(self) -> GameState:
        """``GET /games/{session_id}`` — the current state, from this agent's
        point of view (``legal_actions`` is empty unless it's this agent's turn).
        """
        data = self._client.request("GET", self._url())
        return GameState.from_dict(data if isinstance(data, dict) else {})

    def step(self, action: int) -> GameState:
        """``POST /games/{session_id}/step`` — submit a move.

        ``action`` must be one of the integers in the latest ``state().legal_actions``
        — the server re-validates this regardless (400 ``invalid_action`` if not).
        """
        data = self._client.request("POST", self._url("/step"), json={"action": action})
        return GameState.from_dict(data if isinstance(data, dict) else {})

    def resign(self) -> GameState:
        """``POST /games/{session_id}/resign`` — concede the game.

        Allowed at any time, in any phase, regardless of whose turn it is.
        """
        data = self._client.request("POST", self._url("/resign"), json={})
        return GameState.from_dict(data if isinstance(data, dict) else {})
