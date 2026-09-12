"""Example: the plain choose_action/create_agent contract, nothing else.

This is what you get by default — no messaging, no per-match state, just a
function. Copy this over agent/agent.py as a starting point for any
non-messaging game, or as the "moves only" half of a messaging game (see
messaging_agent.py for the other half): a contestant that never defines
choose_message still plays messaging-enabled games like repeated_pd/avalon
just fine — the runtime auto-terminates each messaging round on its behalf.

Run it as your agent with:

    python -m agent

(after copying this file's contents into agent/agent.py — python -m agent
always reads from there, not from examples/).
"""

from altruagent import DecisionContext, GameState


def choose_action(state: GameState, context: DecisionContext) -> int:
    return state.legal_actions[0]


def create_agent():
    return choose_action
