"""Synchronous control-plane client for the AltruAgent competition platform.

Handles configuration, token login (API key -> JWT by default; see
``auth.py`` for the pluggable strategies, including a claimed Testing seat),
and the platform's one-retry-after-401 convention. Wraps the control-plane
auth endpoints directly:

- ``POST /auth/agent/login`` (Agent_ACP backend/src/index.ts:133,
  services/agentService.ts:81 ``loginAgent``)
- ``GET /auth/agent/me`` (index.ts:223)

...and exposes a small reusable authenticated-request helper (``request``)
that ``GameSession`` (see ``game.py``) builds on to talk to a GameAPI
``game_server_url`` using the *same* JWT and the *same* one-retry-after-401
behavior — there is only ever one authentication system, not one per host.

The platform issues no refresh token (verified against
Agent_ACP/backend/src/middleware/auth.ts and agentService.ts — login always
mints a brand new JWT from the API key, and that's the only recovery path
after a 401). So the client's retry policy is exactly that: on 401, log in
again once and retry the request once. See backend/skill/02-auth.md for the
agent-facing description of the same convention, confirmed identical on the
GameAPI side by gameapi/src/gameapi/auth/jwt_validator.py (same Supabase
JWT, validated independently via JWKS).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import httpx
from dotenv import load_dotenv

from ._responses import _parse_error_body, _parse_json_body
from .auth import ApiKeyAuth, AuthStrategy
from .errors import AuthenticationError, ConfigurationError, PlatformError
from .game import _normalize_game_server_url
from .mcp_transport import call_tool
from .models import Agent, AgentSessions, JoinResult, TournamentDetail

if TYPE_CHECKING:
    from .game import GameSession
    from .mcp_game import MCPGameSession

DEFAULT_TIMEOUT_SECONDS = 10.0

# Where join_session lives when nothing else says so: the deployed control
# plane's GameAPI (the host GET /competitions/{id} reports as game_server_url
# for the deployed platform's running matches), with its MCP mount.
MCP_URL_ENV = "ALTRUAGENT_MCP_URL"
KNOWN_MCP_URLS = {
    "https://api.altruagent-game.com": "https://gameapi.altruagent-game.com/mcp",
}


# join_session failures that say nothing about the join itself (no tool
# answer at all, or GameAPI couldn't reach the control plane): the REST
# route is tried instead.
_MCP_JOIN_FALLBACK_CODES = frozenset({None, "BACKEND_UNAVAILABLE"})

# GameAPI's catch-all for a failed join (and the backend's own, over REST):
# a real refusal, *or* a temporary failure behind it (a database blip, a
# Lambda error, a Red Alert start that got no answer). Only the message tells
# them apart, so these are treated as "maybe temporary" — over MCP the join
# is made once more through the control plane's REST route, and the
# auto-join runtimes keep retrying until the game's join deadline (after
# which the platform answers join_deadline_passed, a final refusal).
AMBIGUOUS_JOIN_ERROR_CODES = frozenset({"SESSION_JOIN_FAILED", "join_failed"})

# The control plane's final join refusals that only arrive as one of the
# codes above, recognizable by their message (Agent_ACP
# backend/src/services/competitionService.ts joinCompetitionAsAgent).
_FINAL_JOIN_FAILURE_MESSAGES = (
    "Competition not found",
    "Competition is not accepting participants",
    "Competition is full",
    "Agent not found",
    "Agent must be claimed",
)

# What a backend token rejection looks like when it arrives *inside* a
# join_session answer instead of as an HTTP 401 (GameAPI accepted the token,
# the control plane then didn't): the backend's own 401 strings, passed
# through as the tool's error code, or a standard code for them. The client's
# usual re-login-after-401 never sees these, so join_competition logs in
# again and makes the join through the REST route.
MCP_AUTH_ERROR_CODES = frozenset({
    "UNAUTHENTICATED",
    "Invalid or expired token",
    "Agent authentication required",
    "Invalid token",
    "Missing or invalid authorization header",
})


def is_final_join_failure(exc: BaseException) -> bool:
    """True when an ambiguous join failure (``AMBIGUOUS_JOIN_ERROR_CODES``)
    carries one of the control plane's known final refusal messages
    (competition not found, not accepting participants, full, ...).
    """
    text = f"{getattr(exc, 'detail', None) or ''} {exc}"
    return any(message in text for message in _FINAL_JOIN_FAILURE_MESSAGES)


def _normalize_mcp_url(value: str) -> str:
    """``ALTRUAGENT_MCP_URL`` may be a bare GameAPI host or its full ``/mcp``
    URL; either way the result is a full URL ending in ``/mcp`` (scheme rule
    as for ``game_server_url``).
    """
    url = _normalize_game_server_url(value)
    return url if url.endswith("/mcp") else f"{url}/mcp"


class AltruAgentClient:
    """Small sync HTTP client for the AltruAgent control plane.

    Usage::

        client = AltruAgentClient()  # reads ALTRUAGENT_CONTROL_URL / ALTRUAGENT_API_KEY
        agent = client.me()

    ``auth=`` swaps how the bearer token is obtained (see ``altruagent.auth``)
    — e.g. ``AltruAgentClient(auth=SeatGrantAuth(claim_token))`` for one
    self-hosted Testing seat, which needs no API key at all. Omitted, the
    client uses ``ApiKeyAuth`` with ``api_key``/``ALTRUAGENT_API_KEY``,
    exactly as before.
    """

    def __init__(
        self,
        control_url: str | None = None,
        api_key: str | None = None,
        *,
        auth: AuthStrategy | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        load_env_file: bool = True,
        transport: httpx.BaseTransport | None = None,
        mcp_url: str | None = None,
    ) -> None:
        if load_env_file:
            # No-op if there's no .env file; explicit args / real env vars
            # always take precedence over anything loaded from it.
            load_dotenv()

        if auth is not None and api_key is not None:
            raise ConfigurationError("Pass either api_key= or auth=, not both.")

        control_url = control_url or os.environ.get("ALTRUAGENT_CONTROL_URL")

        if not control_url:
            raise ConfigurationError(
                "ALTRUAGENT_CONTROL_URL is not set. Copy .env.example to .env and fill it "
                "in, or pass control_url= explicitly."
            )
        if auth is None:
            api_key = api_key or os.environ.get("ALTRUAGENT_API_KEY")
            if not api_key:
                raise ConfigurationError(
                    "ALTRUAGENT_API_KEY is not set. Copy .env.example to .env and fill it in, "
                    "or pass api_key= explicitly."
                )
            auth = ApiKeyAuth(api_key)

        self.control_url = control_url.rstrip("/")
        self.auth = auth
        self._mcp_url = mcp_url
        self._access_token: str | None = None
        self._http = httpx.Client(base_url=self.control_url, timeout=timeout, transport=transport)

    def __enter__(self) -> "AltruAgentClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # -- public API -----------------------------------------------------

    def login(self) -> None:
        """Obtain a fresh bearer token through this client's auth strategy —
        by default ``POST /auth/agent/login`` with the API key (``ApiKeyAuth``);
        for a Testing seat, a claim/renewal (``SeatGrantAuth``).

        The token is kept only in memory on this instance; it is never written
        to disk or logged. Safe to call again at any time — a fresh login
        mints a new token that still resolves to the same identity.
        """
        self._access_token = self.auth.login(self._http, self.control_url)

    def _current_access_token(self, *, force_relogin: bool = False) -> str:
        """Return this client's current JWT — the single source of auth
        state for both REST (``request()``, above) and MCP traffic
        (``altruagent.mcp_transport.call_tool``, which cannot reuse
        ``request()`` directly since the official ``mcp`` SDK owns its own
        HTTP transport). Logs in if there's no token yet, or if
        ``force_relogin=True`` — the MCP transport's own one-retry-after-401
        recovery, mirroring ``request()``'s exactly, so there is still only
        ever one login/retry policy, not a second auth system.
        """
        if self._access_token is None or force_relogin:
            self.login()
        return self._access_token

    def cached_access_token(self) -> str | None:
        """The JWT this client currently holds, without logging in (``None``
        before the first login). ``--tournament-auto`` hands it to each game
        worker it starts, so a worker needn't sign in again just to begin.
        """
        return self._access_token

    def use_access_token(self, token: str) -> None:
        """Start from ``token`` (a JWT minted for this same agent, e.g. by a
        parent process) instead of logging in first. Nothing changes after
        that: a 401 still means one fresh login and one retry.
        """
        self._access_token = token

    def me(self) -> Agent:
        """``GET /auth/agent/me`` — the authenticated agent's profile."""
        data = self.request("GET", "/auth/agent/me")
        return Agent.from_dict(data if isinstance(data, dict) else {})

    def sessions(self) -> AgentSessions:
        """``GET /agents/me/sessions`` — this agent's competition memberships,
        grouped into waiting/active/completed (see Agent_ACP
        backend/src/index.ts:253, ``listAgentSessions``).

        Exactly one control-plane request. Deliberately does **not** resolve
        ``game_server_url`` for any match, and makes no GameAPI calls — that
        endpoint doesn't carry it (it's not a stored column anywhere), and
        resolving it eagerly for every returned match would turn a cheap
        discovery call into N+1 requests. Call ``match.game()`` on whichever
        specific match you actually want to play; it resolves lazily and
        only then.
        """
        data = self.request("GET", "/agents/me/sessions")
        return AgentSessions.from_dict(data if isinstance(data, dict) else {}, client=self)

    def tournament(self, tournament_id: str) -> TournamentDetail:
        """``GET /tournaments/{id}`` — one tournament: its status and phase,
        Swiss standings, and (once complete) the final ranking. The rounds,
        bracket, finals and event log are in ``.raw``.

        Public on the platform; sent with this agent's JWT anyway (the route
        accepts an optional token). A tournament id that doesn't exist (or a
        retired round-robin tournament) is a ``PlatformError`` with
        ``status_code == 404``. The server may use this read to advance the
        tournament (close games past their join deadline, start the next
        round) — that's platform behavior, not something to compensate for.
        """
        data = self.request("GET", f"/tournaments/{tournament_id}")
        return TournamentDetail.from_dict(data if isinstance(data, dict) else {})

    def competition(self, session_id: str) -> dict:
        """``GET /competitions/{id}`` — one competition's row, as a plain dict:
        ``status``, ``tournament_id``, and once it's over ``winner_agent_ids``,
        ``results`` and ``failure_reason``. ``game_server_url`` is included
        only while it's ``in_progress``.
        """
        data = self.request("GET", f"/competitions/{session_id}")
        return data if isinstance(data, dict) else {}

    @property
    def mcp_url(self) -> str | None:
        """The platform's MCP endpoint for session tools like ``join_session``
        (gameplay tools use each match's own ``game_server_url`` instead):
        ``mcp_url=`` if given, else ``ALTRUAGENT_MCP_URL``, else the deployed
        platform's endpoint when ``control_url`` is the deployed control plane.
        ``None`` when none of those applies (e.g. a local backend without
        ``ALTRUAGENT_MCP_URL``) — ``join_competition`` then joins through the
        control plane's REST route instead.
        """
        configured = self._mcp_url or os.environ.get(MCP_URL_ENV)
        if configured:
            return _normalize_mcp_url(configured)
        return KNOWN_MCP_URLS.get(self.control_url)

    def join_competition(self, session_id: str) -> JoinResult:
        """Join one competition by id — a tournament game the platform paired
        this agent into (``AgentSessions.tournament_matches``, or the id the
        owner's dashboard shows), or any open competition.

        MCP first: calls the ``join_session`` tool at ``mcp_url``. Only if
        that endpoint is unknown or can't be used at all (a network or HTTP
        failure, no tool answer) does it make the same join through the
        control plane's ``POST /competitions/{id}/join``, which needs no
        GameAPI. Joining is idempotent: an agent that is already in the
        competition gets success with ``already_joined=True``.

        A refusal raises ``PlatformError`` (``MCPToolError`` over MCP) with
        the platform's ``error_code`` — for tournament games:
        ``not_in_this_match`` (this game is reserved for other agents) and
        ``join_deadline_passed`` (its join window closed; the game counts as
        a loss). Over MCP a backend failure without its own code arrives as
        ``SESSION_JOIN_FAILED``; over REST as ``join_failed``. Those two can
        also hide a temporary failure, so over MCP the join is made once more
        through the REST route, unless the message is a known final refusal
        (``is_final_join_failure``). A token the control plane rejected
        inside a ``join_session`` answer (``MCP_AUTH_ERROR_CODES``) means one
        fresh login, then the REST route — which re-logs in once more on a
        401 and raises ``AuthenticationError`` only if that 401 persists.
        """
        mcp_url = self.mcp_url
        if mcp_url:
            try:
                payload = call_tool(self, mcp_url, "join_session", {"session_id": session_id})
                return JoinResult.from_dict(payload, session_id=session_id, transport="mcp")
            except PlatformError as exc:
                if exc.error_code in MCP_AUTH_ERROR_CODES:
                    # The control plane rejected the token GameAPI accepted
                    # (it expired in between, or the auth service blipped):
                    # a fresh token, then the join made directly.
                    self.login()
                elif exc.error_code in AMBIGUOUS_JOIN_ERROR_CODES:
                    if is_final_join_failure(exc):
                        raise  # a real refusal; the REST route would say the same
                    # Maybe temporary (a backend blip GameAPI could only
                    # report generically): ask the control plane directly.
                elif exc.error_code not in _MCP_JOIN_FALLBACK_CODES:
                    raise  # the platform answered: a real refusal, the REST route would say the same
                # No tool answer (endpoint unreachable, HTTP error), or GameAPI
                # couldn't reach the control plane: the join itself is a
                # control-plane operation, so make it there directly.
        data = self.request("POST", f"/competitions/{session_id}/join")
        return JoinResult.from_dict(data if isinstance(data, dict) else {}, session_id=session_id, transport="rest")

    def game(self, session_id: str, game_server_url: str) -> "GameSession":
        """Open a handle to one already-known GameAPI match.

        Both ``session_id`` and ``game_server_url`` must be supplied
        explicitly — this does not discover them (``client.sessions()`` and
        ``Match.game()`` do that). ``game_server_url`` is accepted with or without a
        scheme: the real control plane hands it back as a bare host (see
        Agent_ACP/backend/src/services/gameAPIService.ts's
        ``getGameAPIServerUrl``), so ``http://`` is prepended automatically
        if missing, matching the platform's own documented normalization
        rule (backend/skill/03-competitions.md).
        """
        from .game import GameSession  # local import: game.py imports this module

        return GameSession(self, session_id=session_id, game_server_url=game_server_url)

    def mcp_game(self, session_id: str, game_server_url: str) -> "MCPGameSession":
        """Open a handle to one already-known match, played through MCP —
        the production gameplay transport (see ``altruagent.mcp_game``).
        ``Match.game()`` calls this for you with a lazily-resolved
        ``game_server_url``; call this directly only if you already have
        both values some other way.
        """
        from .mcp_game import MCPGameSession  # local import: mirrors .game() above

        return MCPGameSession(self, session_id=session_id, game_server_url=game_server_url)

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        """Send an authenticated request using this agent's JWT.

        ``url`` may be a path relative to the control plane (e.g.
        ``"/auth/agent/me"``) or a full absolute URL on a different host
        (e.g. a GameAPI ``game_server_url``) — httpx uses an absolute URL
        as-is regardless of this client's configured base URL. Either way,
        the same JWT and the same one-retry-after-401 behavior apply: there
        is only one authentication system for this client, not one per host.

        Returns the parsed JSON response body. Raises ``AuthenticationError``
        if the request is still unauthenticated after one re-login, or
        ``PlatformError`` for any other non-2xx response or network failure.
        """
        if self._access_token is None:
            self.login()

        response = self._send(method, url, **kwargs)

        if response.status_code == 401:
            # No refresh tokens exist on this platform: re-login with the API
            # key once and retry once. If that still fails, stop — do not loop.
            self.login()
            response = self._send(method, url, **kwargs)

        if response.status_code == 401:
            # Rejected again with a token a login has *just* minted: the key
            # was accepted, so this is the platform's token check failing
            # (its auth service briefly unreachable), not a bad key.
            parsed = _parse_error_body(response)
            message = parsed["detail"] or parsed["error"] or "Not authenticated."
            raise AuthenticationError(
                message,
                status_code=401,
                error_code=parsed["error"],
                detail=parsed["detail"],
                transient=True,
            )
        if response.status_code >= 400:
            parsed = _parse_error_body(response)
            message = (
                parsed["detail"]
                or parsed["error"]
                or f"Request failed with status {response.status_code}."
            )
            raise PlatformError(
                message,
                status_code=response.status_code,
                error_code=parsed["error"],
                detail=parsed["detail"],
                next_action=parsed["next_action"],
            )

        return _parse_json_body(response)

    # -- internals --------------------------------------------------------

    def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", None) or {})
        headers["Authorization"] = f"Bearer {self._access_token}"
        try:
            return self._http.request(method, url, headers=headers, **kwargs)
        except httpx.RequestError as exc:
            raise PlatformError(f"Could not reach {url}: {exc}", status_code=None) from exc

