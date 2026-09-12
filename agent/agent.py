"""Your agent's decision logic.

No base class, no decorator, no registration — just two functions.
`choose_action` is called only when a real move is actually being requested
(the runtime already checked that it's your turn); return one action from
`state.legal_actions`. If you'd rather concede a match, return
`altruagent.RESIGN` instead of an int.

`create_agent()` is the one thing the runtime looks for at startup — it's
called exactly once per match (in that match's own independent process),
and whatever it returns is reused for every turn of that one match. For a
plain function like the one below, that's just returning the function
itself:

    def create_agent():
        return choose_action

If you want per-match state (a history, a running tally, anything), return
a fresh object instead — the runtime calling create_agent() again for the
next match is what gives you a fresh instance automatically:

    class MyAgent:
        def __init__(self):
            self.history = []
        def choose_action(self, state, context):
            ...

    def create_agent():
        return MyAgent()

This baseline always plays the first legal action — replace it with your
own strategy, an LLM call, whatever you want.

Some games (e.g. repeated_pd, avalon) also have a messaging phase before/
between moves. You don't have to do anything about it: this agent will
automatically vote to end each messaging round and move on. If you want to
actually negotiate, add an optional `choose_message(state, context)` method
next to `choose_action` — see examples/messaging_agent.py.
"""

from altruagent import DecisionContext, GameState


def choose_action(state: GameState, context: DecisionContext) -> int:
    return state.legal_actions[0]


def create_agent():
    return choose_action
