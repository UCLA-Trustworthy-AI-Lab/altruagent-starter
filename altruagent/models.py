"""Response models for the AltruAgent SDK.

Models are deliberately tolerant of extra/unknown fields from the backend —
new fields the platform adds later should never break parsing. ``raw`` keeps
the full server response for anything not (yet) promoted to a typed
attribute, with one exception: fields the SDK never surfaces at all (see
``Agent`` below), regardless of what the live endpoint happens to return.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The real GET /auth/agent/me and POST /auth/human/claim responses currently
# include these (they're the agent's raw DB row — see
# Agent_ACP/backend/src/models/agent.ts and index.ts's `/auth/agent/me`
# handler, which spreads the row as-is). They're one-way hashes, not the
# actual api_key/claim_token secrets, but contestant code has no reason to
# ever see them, so they're stripped before `Agent.raw` is populated.
_SENSITIVE_FIELDS = ("api_key_hash", "claim_token_hash")


@dataclass
class Agent:
    """The authenticated agent, as returned by ``GET /auth/agent/me``."""

    id: str
    name: str
    status: str  # "unclaimed" or "claimed"
    description: str | None = None
    claimed_by_user_id: str | None = None
    claimed_at: str | None = None
    created_at: str | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def is_claimed(self) -> bool:
        return self.status == "claimed"

    @classmethod
    def from_dict(cls, data: dict) -> "Agent":
        raw = {k: v for k, v in data.items() if k not in _SENSITIVE_FIELDS}
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            status=data.get("status", "unknown"),
            description=data.get("description"),
            claimed_by_user_id=data.get("claimed_by_user_id"),
            claimed_at=data.get("claimed_at"),
            created_at=data.get("created_at"),
            raw=raw,
        )


@dataclass
class NextAction:
    """Machine-readable "what can I do right now" guidance from GameAPI.

    Mirrors gameapi/src/gameapi/models/responses.py's ``NextAction`` exactly
    (``action``, ``endpoint``, ``hint``, ``required_fields``). A single state
    response can carry more than one of these — e.g. during a messaging round,
    ``compute_next_actions`` (gameapi/src/gameapi/domain/next_actions.py)
    returns both ``send_message`` and ``terminate_messaging`` together — so
    this is never collapsed down to a single value.
    """

    action: str
    hint: str
    endpoint: str | None = None
    required_fields: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "NextAction":
        return cls(
            action=data.get("action", ""),
            hint=data.get("hint", ""),
            endpoint=data.get("endpoint"),
            required_fields=list(data.get("required_fields") or []),
        )


@dataclass
class PlayerRef:
    """A player reference as GameAPI serializes it: just a display name.

    See gameapi/src/gameapi/domain/__init__.py's ``Player`` dataclass and
    ``GameStateResponse.current_player: Player | None``.
    """

    name: str


@dataclass
class GameState:
    """A GameAPI game state, as returned by ``GET/POST /games/{session_id}...``.

    Covers the fields needed to play a single match generically (see
    gameapi/src/gameapi/models/responses.py's ``GameStateResponse``). Richer
    games (repeated_pd's round history, Avalon's ``avalon_*`` fields,
    Pokémon's extras, messaging fields) are not individually modeled yet —
    they're always available via ``raw``, which holds the complete,
    unmodified server response.
    """

    session_id: str
    game_name: str
    status: str
    observation: str
    current_player: PlayerRef | None
    legal_actions: list[int]
    legal_actions_str: list[str]
    is_terminal: bool
    returns: dict[str, float] | None
    move_count: int
    termination_reason: str | None
    messaging_enabled: bool
    phase: str
    next_actions: list[NextAction]
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: dict) -> "GameState":
        current_player_data = data.get("current_player")
        current_player = (
            PlayerRef(name=current_player_data["name"])
            if current_player_data
            else None
        )
        next_actions = [
            NextAction.from_dict(a) for a in (data.get("next_actions") or [])
        ]
        return cls(
            session_id=data.get("session_id", ""),
            game_name=data.get("game_name", ""),
            status=data.get("status", "unknown"),
            observation=data.get("observation", ""),
            current_player=current_player,
            legal_actions=list(data.get("legal_actions") or []),
            legal_actions_str=list(data.get("legal_actions_str") or []),
            is_terminal=bool(data.get("is_terminal", False)),
            returns=data.get("returns"),
            move_count=data.get("move_count", 0),
            termination_reason=data.get("termination_reason"),
            messaging_enabled=bool(data.get("messaging_enabled", False)),
            phase=data.get("phase", "moving"),
            next_actions=next_actions,
            raw=data,
        )
