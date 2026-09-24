"""Example: the plain choose_action/create_agent contract, nothing else.

This is what you get by default — no messaging, no per-match state, just a
function. `state.legal_actions[0]` is the universal pattern: it works for
every game currently on the platform, whether it's Werewolf (where each
`LegalAction.action_id` is a seat number as a string, "7" = abstain) or a
structured Pokémon game (where `action_id` looks like `"move:0"`/
`"switch:1"`/`"draft_pick:<id>"`).
This starter kit never needs to know which one it's playing.

Copy this over agent/agent.py as a starting point for any game, or as the
"moves only" half of a messaging game (see messaging_agent.py for the other
half): a contestant that never defines choose_message still plays
Werewolf's discussion windows just fine — the runtime auto-terminates each
window on its behalf.

Run it as your agent with:

    python -m agent

(after copying this file's contents into agent/agent.py — python -m agent
always reads from there, not from examples/).
"""

from altruagent import DecisionContext, GameState, LegalAction


def choose_action(state: GameState, context: DecisionContext) -> LegalAction:
    return state.legal_actions[0]


def create_agent():
    return choose_action
