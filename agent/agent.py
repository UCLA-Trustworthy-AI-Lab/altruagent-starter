"""Your agent's decision logic.

`create_agent()` is called once per game, in that game's own process. It
returns a small router that picks the right player the first time the
runtime asks for a decision:

- **Werewolf** -> `agent.werewolf.WerewolfAgent`: Bayesian role inference
  over every possible role assignment, role-specific play (seer / wolf /
  villager), and planned table talk. Decisions never depend on an LLM; if
  `OPENAI_API_KEY` is set, an LLM only rewrites the planned chat messages
  (disable with `WEREWOLF_LLM_CHAT=0`). Set `WEREWOLF_DEBUG=1` to log every
  decision and the first private observation.
- **Any other game** -> `examples.llm_agent.LLMAgent` when `OPENAI_API_KEY`
  is set, otherwise the first legal action (which can't finish Pokémon or
  Red Alert; see GAMES.md).

Try the Werewolf agent offline against baselines before a tournament:

    python -m sim.werewolf_sim --games 500
"""

from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv

from altruagent import TERMINATE_MESSAGING, DecisionContext, GameState


class Agent:
    def __init__(self) -> None:
        self._delegate: Any = None

    def _player(self, context: DecisionContext) -> Any:
        if self._delegate is None:
            if (context.game_type or "").startswith("werewolf"):
                from agent.werewolf import WerewolfAgent
                from agent.werewolf.chat_llm import polisher_from_env

                self._delegate = WerewolfAgent(polish=polisher_from_env())
            elif os.environ.get("OPENAI_API_KEY"):
                from examples.llm_agent import create_agent as create_llm_agent

                self._delegate = create_llm_agent()
            else:
                self._delegate = _FirstLegal()
        return self._delegate

    def choose_action(self, state: GameState, context: DecisionContext):
        return self._player(context).choose_action(state, context)

    def choose_message(self, state: GameState, context: DecisionContext):
        method = getattr(self._player(context), "choose_message", None)
        return TERMINATE_MESSAGING if method is None else method(state, context)

    def on_action_result(self, result: dict, context: DecisionContext) -> None:
        method = getattr(self._delegate, "on_action_result", None)
        if method is not None:
            method(result, context)


class _FirstLegal:
    def choose_action(self, state: GameState, context: DecisionContext):
        return state.legal_actions[0]


def create_agent() -> Agent:
    load_dotenv()  # no-op if already loaded; never overrides real env vars
    return Agent()
