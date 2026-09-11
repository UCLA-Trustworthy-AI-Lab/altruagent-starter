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
