"""Example: a Werewolf agent driven by a strategy prompt you write.

    python -m agent --check-tournament --agent examples.werewolf_llm_agent
    python -m agent --match --agent examples.werewolf_llm_agent

It is the general LLM agent (examples/llm_agent.py), unchanged, plus one
addition: the text of examples/werewolf_strategy.md is added to the model's
instructions on every Werewolf decision. Edit that file to change how your
agent plays. No code changes are needed.

Like llm_agent.py it needs OPENAI_API_KEY (and optionally OPENAI_MODEL) in your
environment or in the starter's gitignored .env. The key is never printed.
Decisions in other games are passed through untouched.
"""

from __future__ import annotations

import json
from pathlib import Path

from dotenv import load_dotenv

from examples.llm.providers import provider_from_env
from examples.llm_agent import LLMAgent, _accepts_timeout

STRATEGY_FILE = Path(__file__).with_name("werewolf_strategy.md")


class StrategyProvider:
    """Wraps a model provider. On a Werewolf decision it adds the strategy text to
    the system message; every other call goes through unchanged."""

    def __init__(self, inner, strategy: str) -> None:
        self._inner = inner
        self._strategy = strategy.strip()
        self._takes_timeout = _accepts_timeout(inner)

    def complete_structured(self, messages, name, schema, **kwargs):
        if _is_werewolf(messages):
            system = dict(messages[0])
            system["content"] = f"{system['content']}\n\nHow to play Werewolf (your strategy):\n{self._strategy}"
            messages = [system, *messages[1:]]  # a copy: the caller's list is left alone
        if not self._takes_timeout:
            kwargs.pop("timeout", None)
        return self._inner.complete_structured(messages, name, schema, **kwargs)

    def __getattr__(self, item):  # anything else a game's player asks of the provider
        return getattr(self._inner, item)


def _is_werewolf(messages) -> bool:
    """The agent's first user message is JSON that carries the game type."""
    try:
        return messages[0]["role"] == "system" and json.loads(messages[1]["content"]).get("game_type") == "werewolf"
    except (IndexError, KeyError, TypeError, ValueError, AttributeError):
        return False


def create_agent() -> LLMAgent:
    load_dotenv()  # no-op if already loaded; never overrides real env vars
    strategy = STRATEGY_FILE.read_text(encoding="utf-8")
    return LLMAgent(StrategyProvider(provider_from_env(), strategy))
