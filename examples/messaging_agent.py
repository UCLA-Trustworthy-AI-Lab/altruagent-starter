"""Example: a stateful agent for repeated_pd that actually negotiates.

Shows the messaging half of the contract that basic_agent.py's default
behavior skips:

- create_agent() returns a fresh object per match (not a bare function),
  so per-match state (here: which round we've already messaged in) never
  leaks between matches.
- choose_message is optional, next to choose_action, on that same object.
- SendMessage(content, recipients=None) to chat (empty/no recipients ==
  broadcast to every other player); altruagent.TERMINATE_MESSAGING to vote
  the current messaging round closed.
- state.new_messages holds what other players sent since you last checked.

repeated_pd's default preset caps chat at 1 message per agent per round
(see Agent_ACP's REPEATED_PD_MESSAGING_CONFIG) — this agent respects that by
sending exactly one broadcast per round, then terminating. The platform
doesn't report that cap back to you, so track your own usage; this agent
does it with one dict keyed by move_count (repeated_pd's round marker).

Run it as your agent with:

    python -m agent

(after copying this file's contents into agent/agent.py).
"""

from altruagent import DecisionContext, GameState, SendMessage, TERMINATE_MESSAGING


class RepeatedPDAgent:
    def __init__(self) -> None:
        # move_count is repeated_pd's round marker — it doesn't change while
        # a single round's messaging phase is open, so "have I already sent
        # my one message for this move_count" is enough to avoid resending.
        self._messaged_for_move_count: int | None = None

    def choose_action(self, state: GameState, context: DecisionContext) -> int:
        # Always cooperate. Swap this for real strategy — e.g. mirror the
        # opponent's last move from state.raw["round_history"] (tit-for-tat).
        return state.legal_actions[0]

    def choose_message(self, state: GameState, context: DecisionContext):
        for message in state.new_messages:
            print(f"[{context.session_id}] opponent says: {message.content!r}")

        if self._messaged_for_move_count == state.move_count:
            return TERMINATE_MESSAGING

        self._messaged_for_move_count = state.move_count
        return SendMessage("Let's both cooperate this round.")


def create_agent():
    return RepeatedPDAgent()
