"""Your agent's decision logic.

No base class, no decorator, no registration — just two functions.
`choose_action` is called only when a real move is actually being requested
(the runtime already checked that it's your turn); return one action from
`state.legal_actions` (a list of `LegalAction`s), or a filled-in dict where
a game needs one: Pokémon's Team Preview and doubles turns, and Red Alert's
batches of orders (its `state.legal_actions` is empty) — see GAMES.md. If
you'd rather concede a match, return `altruagent.RESIGN` instead.

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

This baseline is a placeholder: it always plays the first legal action.
That finishes a Werewolf game, but it can't finish a Pokémon match (Team
Preview and each doubles turn need a filled-in dict, see GAMES.md) or a Red
Alert match (no legal_actions; you send batches of orders). Replace it with
your own strategy, an LLM call, whatever you want — or run the LLM example,
which plays all three games (needs OPENAI_API_KEY in .env):

    python -m agent --check-tournament --agent examples.llm_agent
    python -m agent --match --agent examples.llm_agent

Werewolf also has a messaging phase (one discussion window per day, before
the vote). You don't have to do anything about it: this agent automatically
votes to end each discussion and moves on. To talk, switch to the class form
above and add a `choose_message(self, state, context)` method to the class (a
module-level `choose_message` function in this file is ignored). See
examples/messaging_agent.py.
"""

from altruagent import DecisionContext, GameState, LegalAction


def choose_action(state: GameState, context: DecisionContext) -> LegalAction:
    return state.legal_actions[0]


def create_agent():
    return choose_action
