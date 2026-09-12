"""Your agent's decision logic.

No base class, no decorator, no registration — just this one function.
`choose_action` is called only when a real move is actually being requested
(the runtime already checked that it's your turn); return one action from
`state.legal_actions`. If you'd rather concede a match, return
`altruagent.RESIGN` instead of an int.

This baseline always plays the first legal action — replace it with your
own strategy, an LLM call, a class with its own state, whatever you want.
"""

from altruagent import DecisionContext, GameState


def choose_action(state: GameState, context: DecisionContext) -> int:
    return state.legal_actions[0]
