"""Synchronous control-plane client for the AltruAgent competition platform.

Handles configuration, API-key -> JWT login, and the platform's one-retry-
after-401 convention. This wraps exactly two endpoints for now:

- ``POST /auth/agent/login`` (Agent_ACP backend/src/index.ts:133,
  services/agentService.ts:81 ``loginAgent``)
- ``GET /auth/agent/me`` (index.ts:223)

The platform issues no refresh token (verified against
Agent_ACP/backend/src/middleware/auth.ts and agentService.ts — login always
mints a brand new JWT from the API key, and that's the only recovery path
after a 401). So the client's retry policy is exactly that: on 401, log in
again once and retry the request once. See backend/skill/02-auth.md for the
agent-facing description of the same convention.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from dotenv import load_dotenv

from .errors import AuthenticationError, ConfigurationError, PlatformError
from .models import Agent

DEFAULT_TIMEOUT_SECONDS = 10.0


class AltruAgentClient:
    """Small sync HTTP client for the AltruAgent control plane.

    Usage::

        client = AltruAgentClient()  # reads ALTRUAGENT_CONTROL_URL / ALTRUAGENT_API_KEY
        agent = client.me()
    """

    def __init__(
        self,
        control_url: str | None = None,
        api_key: str | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        load_env_file: bool = True,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if load_env_file:
            # No-op if there's no .env file; explicit args / real env vars
            # always take precedence over anything loaded from it.
            load_dotenv()

        control_url = control_url or os.environ.get("ALTRUAGENT_CONTROL_URL")
        api_key = api_key or os.environ.get("ALTRUAGENT_API_KEY")

        if not control_url:
            raise ConfigurationError(
                "ALTRUAGENT_CONTROL_URL is not set. Copy .env.example to .env and fill it "
                "in, or pass control_url= explicitly."
            )
        if not api_key:
            raise ConfigurationError(
                "ALTRUAGENT_API_KEY is not set. Copy .env.example to .env and fill it in, "
                "or pass api_key= explicitly."
            )

        self.control_url = control_url.rstrip("/")
        self._api_key = api_key
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
        """``POST /auth/agent/login`` — exchange the API key for a fresh JWT.

        The JWT is kept only in memory on this instance; it is never written
        to disk or logged. Safe to call again at any time — a fresh login
        mints a new token that still resolves to the same agent identity.
        """
        try:
            response = self._http.post("/auth/agent/login", json={"api_key": self._api_key})
        except httpx.RequestError as exc:
            raise PlatformError(
                f"Could not reach the control plane at {self.control_url}: {exc}",
                status_code=None,
            ) from exc

        if response.status_code != 200:
            parsed = _parse_error_body(response)
            message = parsed["detail"] or parsed["error"] or "Login failed."
            raise AuthenticationError(
                message,
                status_code=response.status_code,
                error_code=parsed["error"],
                detail=parsed["detail"],
            )

        body = _parse_json_body(response)
        token = body.get("access_token") if isinstance(body, dict) else None
        if not token:
            raise AuthenticationError(
                "Login response did not include an access_token.",
                status_code=response.status_code,
            )
        self._access_token = token

    def me(self) -> Agent:
        """``GET /auth/agent/me`` — the authenticated agent's profile."""
        data = self._authenticated_request("GET", "/auth/agent/me")
        return Agent.from_dict(data if isinstance(data, dict) else {})

    # -- internals --------------------------------------------------------

    def _authenticated_request(self, method: str, path: str, **kwargs: Any) -> Any:
        if self._access_token is None:
            self.login()

        response = self._send(method, path, **kwargs)

        if response.status_code == 401:
            # No refresh tokens exist on this platform: re-login with the API
            # key once and retry once. If that still fails, stop — do not loop.
            self.login()
            response = self._send(method, path, **kwargs)

        if response.status_code == 401:
            parsed = _parse_error_body(response)
            message = parsed["detail"] or parsed["error"] or "Not authenticated."
            raise AuthenticationError(
                message,
                status_code=401,
                error_code=parsed["error"],
                detail=parsed["detail"],
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

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", None) or {})
        headers["Authorization"] = f"Bearer {self._access_token}"
        try:
            return self._http.request(method, path, headers=headers, **kwargs)
        except httpx.RequestError as exc:
            raise PlatformError(
                f"Could not reach the control plane at {self.control_url}: {exc}",
                status_code=None,
            ) from exc


def _parse_json_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {}


def _parse_error_body(response: httpx.Response) -> dict:
    """Best-effort parse of an error response body.

    Tolerates the platform's inconsistent error envelopes: a `next_actions`
    array, a `recovery_action` object instead, a plain `{"error": "..."}`
    with no `detail`, or a non-JSON body (e.g. an upstream gateway error
    page), which is captured as text rather than raised.
    """
    try:
        body = response.json()
    except ValueError:
        return {"error": None, "detail": response.text[:500] or None, "next_action": None}

    if not isinstance(body, dict):
        return {"error": None, "detail": str(body), "next_action": None}

    next_action = body.get("recovery_action")
    if next_action is None:
        next_actions = body.get("next_actions")
        if isinstance(next_actions, list) and next_actions:
            next_action = next_actions[0]

    return {
        "error": body.get("error"),
        "detail": body.get("detail"),
        "next_action": next_action,
    }
