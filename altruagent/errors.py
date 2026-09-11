"""Exception hierarchy for the AltruAgent SDK.

The platform's error responses are not perfectly uniform across endpoints.
Most carry ``{"error": "<machine_code>", "detail": "...", "next_actions": [...]}``;
GameAPI errors instead carry a single ``recovery_action`` object; and a few
control-plane auth errors (e.g. a bad API key on login) carry only
``{"error": "<human-readable message>"}`` with no ``detail`` at all. Parsing
in ``client.py`` is defensive about all of this: unknown/missing fields
degrade to ``None`` rather than raising, and a non-JSON body (e.g. an
upstream gateway error page) is captured as raw text instead of crashing.
"""

from __future__ import annotations


class AltruAgentError(Exception):
    """Base class for all errors raised by this SDK."""


class ConfigurationError(AltruAgentError):
    """Required configuration (e.g. control URL or API key) is missing or invalid.

    Raised locally, before any request is made.
    """


class AuthenticationError(AltruAgentError):
    """Login failed, or a request was rejected as unauthenticated after one retry.

    The platform has no refresh-token grant, so this is also what you get if
    an API key is revoked or simply wrong.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.detail = detail


class PlatformError(AltruAgentError):
    """The control plane returned a non-2xx response, or could not be reached at all
    (e.g. a network failure or timeout — `status_code` is `None` in that case).
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None,
        error_code: str | None = None,
        detail: str | None = None,
        next_action: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.detail = detail
        self.next_action = next_action
