"""Response models for the AltruAgent SDK.

Models are deliberately tolerant of extra/unknown fields from the backend —
new fields the platform adds later should never break parsing. ``raw`` keeps
the full server response for anything not (yet) promoted to a typed
attribute, with one exception: fields the SDK never surfaces at all (see
``Agent`` below), regardless of what the live endpoint happens to return.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .client import AltruAgentClient
    from .game import GameSession

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
class Message:
    """A single messaging-phase message, as GameAPI serializes it. Mirrors
    gameapi/src/gameapi/models/responses.py's ``MessageResponse`` exactly
    (``index``, ``sender``, ``recipients``, ``content``, ``type``,
    ``sent_at``, ``move_index``). ``type`` is ``"chat"`` or ``"terminate"``
    (see gameapi/src/gameapi/domain/__init__.py's ``MessageType``);
    ``recipients`` empty means broadcast, one entry means a targeted p2p
    message (the server rejects 2+ today — p2group is gated).
    """

    index: int
    sender: int
    recipients: list[int]
    content: str
    type: str
    sent_at: str
    move_index: int

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        return cls(
            index=data.get("index", 0),
            sender=data.get("sender", 0),
            recipients=list(data.get("recipients") or []),
            content=data.get("content", ""),
            type=data.get("type", "chat"),
            sent_at=data.get("sent_at", ""),
            move_index=data.get("move_index", 0),
        )


@dataclass
class GameState:
    """A GameAPI game state, as returned by ``GET/POST /games/{session_id}...``.

    Covers the fields needed to play a single match generically (see
    gameapi/src/gameapi/models/responses.py's ``GameStateResponse``). Richer
    per-game fields (repeated_pd's round history, Avalon's ``avalon_*``
    fields, Pokémon's extras) are not individually modeled — they're always
    available via ``raw``, which holds the complete, unmodified server
    response. Messaging fields *are* modeled (``new_messages``,
    ``terminated_messaging``, ``messaging_mode``), since a contestant needs
    them to act during a MESSAGING phase (see ``altruagent.runner``'s
    ``choose_message`` support). Two messaging config knobs the server
    computes but never returns in this response at all —
    ``word_limit_per_message`` and ``max_messages_per_phase_per_agent`` — are
    not modeled here because there is nothing to parse; a contestant that
    needs them must track its own usage.
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
    new_messages: list[Message]
    terminated_messaging: list[int]
    messaging_mode: str
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
        new_messages = [
            Message.from_dict(m) for m in (data.get("new_messages") or [])
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
            new_messages=new_messages,
            terminated_messaging=list(data.get("terminated_messaging") or []),
            messaging_mode=data.get("messaging_mode", "per_move"),
            raw=data,
        )


@dataclass
class Match:
    """A contestant-facing view over one Competition row, as returned by
    ``GET /agents/me/sessions`` (verified against Agent_ACP
    backend/src/db/competitions.ts's ``getCompetitionsForAgent`` and
    services/competitionService.ts's ``listAgentSessions``). Called "Match",
    not "Assignment" — the backend has no such concept; this simply wraps a
    raw ``competitions`` table row.

    ``game_server_url`` is never present on this endpoint's rows (confirmed —
    it is not a stored column anywhere; it's computed only by
    ``GET /competitions/{id}`` and ``GET /tournaments/{id}``). It starts
    ``None`` here and is resolved lazily, only when ``game()`` is actually
    called — see ``game()`` below.
    """

    session_id: str
    status: str
    game_type: str | None = None
    tournament_id: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    game_server_url: str | None = None
    raw: dict = field(default_factory=dict, repr=False)
    _client: Any = field(default=None, repr=False, compare=False, init=False)

    @classmethod
    def from_dict(cls, data: dict, *, client: "AltruAgentClient | None" = None) -> "Match":
        match = cls(
            session_id=data.get("session_id", ""),
            status=data.get("status", "unknown"),
            game_type=data.get("game_type"),
            tournament_id=data.get("tournament_id"),
            created_at=data.get("created_at"),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            raw=data,
        )
        match._client = client
        return match

    def game(self) -> "GameSession":
        """Return a playable ``GameSession`` for this match, resolving
        ``game_server_url`` lazily if it isn't already known.

        - If ``game_server_url`` was already resolved (cached from a prior
          call on this same ``Match`` instance), this makes no network
          request at all.
        - If not, and ``status == "in_progress"``, this makes exactly one
          request — ``GET /competitions/{session_id}`` (the same endpoint
          that computes ``game_server_url`` for a single competition) — and
          caches the result on this instance so repeated calls don't repeat
          the lookup.
        - A ``waiting`` match has no GameAPI session to open yet, and a
          ``completed`` one no longer has a playable one; both raise
          ``ValueError`` immediately, with no network request.
        """
        if self._client is None:
            raise ValueError(
                "This Match has no client attached (it wasn't returned by "
                "AltruAgentClient.sessions()), so game_server_url cannot be resolved."
            )

        if self.game_server_url:
            return self._client.game(session_id=self.session_id, game_server_url=self.game_server_url)

        if self.status != "in_progress":
            raise ValueError(
                f"Match {self.session_id!r} is {self.status!r}, not 'in_progress' — "
                "it has no playable GameAPI session right now."
            )

        data = self._client.request("GET", f"/competitions/{self.session_id}")
        if isinstance(data, dict) and data.get("session_id") not in (None, self.session_id):
            raise ValueError(
                f"GET /competitions/{self.session_id} returned session_id "
                f"{data.get('session_id')!r}, which does not match."
            )
        game_server_url = data.get("game_server_url") if isinstance(data, dict) else None
        if not game_server_url:
            raise ValueError(
                f"GET /competitions/{self.session_id} did not include a game_server_url "
                "even though the match is in_progress."
            )

        self.game_server_url = game_server_url
        return self._client.game(session_id=self.session_id, game_server_url=game_server_url)


@dataclass
class AgentSessions:
    """This agent's competition memberships, as returned by
    ``GET /agents/me/sessions``, grouped exactly as the server groups them:
    ``joined_sessions`` -> ``waiting``, ``active_sessions`` -> ``active``,
    ``completed_sessions`` -> ``completed``. Includes both standalone
    competitions and tournament-created child matches (the endpoint does not
    distinguish at the query level — see ``Match.tournament_id``).

    The backend caps this at the 50 most recently joined memberships in
    total, not per group (``getCompetitionsForAgent``'s ``.limit(50)``) — a
    long-lived agent's oldest completed matches can silently drop off before
    its current ones would.
    """

    waiting: list[Match] = field(default_factory=list)
    active: list[Match] = field(default_factory=list)
    completed: list[Match] = field(default_factory=list)
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: dict, *, client: "AltruAgentClient | None" = None) -> "AgentSessions":
        def _matches(key: str) -> list[Match]:
            return [Match.from_dict(m, client=client) for m in (data.get(key) or [])]

        return cls(
            waiting=_matches("joined_sessions"),
            active=_matches("active_sessions"),
            completed=_matches("completed_sessions"),
            raw=data,
        )


@dataclass
class TournamentViewer:
    """The calling agent's membership view of a tournament — present only on
    an authenticated ``GET /tournaments/{id}`` (see Agent_ACP
    backend/src/services/tournamentService.ts's ``getTournament``, which only
    builds a ``viewer`` object when the request carried a JWT that resolved
    to an agent).
    """

    agent_id: str | None = None
    is_tournament_participant: bool = False
    active_child_session_ids: list[str] = field(default_factory=list)
    should_join_tournament: bool | None = None
    should_wait_for_child_match: bool | None = None
    next_actions: list[NextAction] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "TournamentViewer":
        return cls(
            agent_id=data.get("agent_id"),
            is_tournament_participant=bool(data.get("is_tournament_participant", False)),
            active_child_session_ids=list(data.get("active_child_session_ids") or []),
            should_join_tournament=data.get("should_join_tournament"),
            should_wait_for_child_match=data.get("should_wait_for_child_match"),
            next_actions=[NextAction.from_dict(a) for a in (data.get("next_actions") or [])],
        )


@dataclass
class Tournament:
    """A tournament, as returned by either ``GET /tournaments`` (list — a raw
    ``tournaments`` DB row) or ``GET /tournaments/{id}`` (detail — a curated
    ``compactTournament()`` subset with a *different* field set — see
    Agent_ACP backend/src/services/tournamentService.ts). Both shapes are
    tolerated: only fields useful and common enough to model are typed;
    everything else (list-only fields like ``created_at``/``metadata``, or
    detail-only ``leaderboard``/``participants``) stays reachable via ``raw``.

    There is no ``name`` field anywhere on the backend (confirmed against
    the DB schema) — a tournament is identified only by ``tournament_id`` +
    ``game_type``.

    ``viewer`` is ``None`` unless this came from an authenticated
    ``GET /tournaments/{id}`` call that returned one (never present on a
    ``GET /tournaments`` list entry).
    """

    tournament_id: str
    status: str
    game_type: str | None = None
    max_participants: int | None = None
    current_participants: int | None = None
    max_active_matches: int | None = None
    queue_id: str | None = None
    game_server_url: str | None = None
    viewer: TournamentViewer | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: dict, *, viewer: dict | None = None) -> "Tournament":
        return cls(
            tournament_id=data.get("tournament_id", ""),
            status=data.get("status", "unknown"),
            game_type=data.get("game_type"),
            max_participants=data.get("max_participants"),
            current_participants=data.get("current_participants"),
            max_active_matches=data.get("max_active_matches"),
            queue_id=data.get("queue_id"),
            game_server_url=data.get("game_server_url"),
            viewer=TournamentViewer.from_dict(viewer) if isinstance(viewer, dict) else None,
            raw=data,
        )


@dataclass(frozen=True)
class DecisionContext:
    """The minimal identifying information handed to contestant decision
    logic alongside a ``GameState`` (see ``altruagent.runner``).

    Deliberately carries no client/session object — a decision function
    should be able to reason about the game without being handed enough
    power to mutate an unrelated match. ``session_id``/``game_type`` are
    also available on ``GameState`` itself (as ``session_id``/``game_name``)
    but are repeated here so contestant code doesn't need to thread
    ``state`` through just to log/key by them; ``tournament_id``/
    ``agent_id`` are not available anywhere else.
    """

    session_id: str
    tournament_id: str | None
    game_type: str | None
    agent_id: str
