"""Example: a Werewolf agent with real rules and no AI model.

    python -m agent --match --agent examples.werewolf_agent

It needs no API key and spends no tokens. It reads what GameAPI shows this
seat (your role, your wolf ally or your seer results, who has died, who voted
for whom, what was said) and plays by simple rules:

- Never abstain by day. A day with no elimination helps the wolves.
- Villager: vote for the player the table suspects most. A player who voted
  for someone later revealed as a villager counts double.
- Seer: investigate a player you have not checked yet each night. When you
  find a wolf, say so and vote for it. With only clean results, stay quiet.
- Wolf: never vote for your ally; kill whoever has voted against a wolf most.
  (Both wolves run the same rule, so they pick the same victim without
  talking; a tie goes to the lower seat anyway.)
- With nothing to go on, vote for the lowest-numbered living player, so the
  table has a common target and a day is not wasted.
- Talk once per discussion window, then end it.

It is a baseline to beat, not a strong player. Ideas for making it better are
in the comments marked IDEA. For a model-driven agent see examples/llm_agent.py.
"""

from __future__ import annotations

import re
from collections import Counter
from types import SimpleNamespace

from altruagent import TERMINATE_MESSAGING, SendMessage, WithReasoning

ABSTAIN = "7"  # in Werewolf, action 7 is "abstain"; every other action id is a seat number
SUSPECT_WORDS = re.compile(r"suspect|suspicious|wolf|eliminate|vote", re.I)
SEAT = re.compile(r"\bPlayer(\d)\b")


class WerewolfAgent:
    def __init__(self) -> None:
        self._day = None
        self._suspicion: Counter = Counter()  # seat -> how often others pointed at it today
        self._seen: set = set()  # message indexes already counted
        self._window = None  # state_version of the discussion window we are in
        self._said = False  # have we spoken in this window?

    # -- moves -----------------------------------------------------------------

    def choose_action(self, state, context):
        if (context.game_type or "") != "werewolf":
            return state.legal_actions[0]
        try:
            return self._choose_action(state, context)
        except Exception as exc:  # a starter should keep playing; the line below says what went wrong
            print(f"[werewolf_agent] using a default move after an error: {exc!r}")
            return next((a for a in state.legal_actions if a.action_id != ABSTAIN), state.legal_actions[0])

    def _choose_action(self, state, context):
        view = self._view(state, context)
        self._remember(state, view)
        options = {int(a.action_id): a for a in state.legal_actions if a.action_id != ABSTAIN}
        if not options:
            return state.legal_actions[0]
        kind = _kind(next(a.label for a in state.legal_actions if a.action_id != ABSTAIN))

        if kind == "kill":
            wolves = view.allies | {view.me}
            threat = Counter()
            for _, votes, _ in view.history:
                for voter, target in votes.items():
                    if target in wolves:
                        threat[voter] += 1
            pick = max(options, key=lambda s: (threat[s], -s))
            return WithReasoning(options[pick], f"Killing Player{pick}: the biggest threat to the wolves.")

        if kind == "investigate":
            todo = [s for s in options if s not in view.checks]
            pick = min(todo or options)
            return WithReasoning(options[pick], f"Checking Player{pick}, whom I have not investigated yet.")

        # a day vote
        candidates = set(options)
        if view.role == "wolf":
            candidates -= view.allies
        if view.role == "seer":
            known_wolves = [s for s in candidates if view.checks.get(s) == "wolf"]
            if known_wolves:
                pick = min(known_wolves)
                return WithReasoning(options[pick], f"I investigated Player{pick} and they are a wolf.")
            candidates = {s for s in candidates if view.checks.get(s) != "villager"} or candidates
        candidates = candidates or set(options)
        pick = self._most_suspected(candidates, view)
        return WithReasoning(options[pick], f"Voting for Player{pick}, the most suspected at the table.")

    # -- messaging ---------------------------------------------------------------

    def choose_message(self, state, context):
        if (context.game_type or "") != "werewolf":
            return TERMINATE_MESSAGING
        try:
            view = self._view(state, context)
            self._remember(state, view)
            if state.state_version != self._window:  # a new discussion window
                self._window, self._said = state.state_version, False
            if self._said:
                return TERMINATE_MESSAGING
            self._said = True
            text = self._speech(view)
            return SendMessage(text) if text else TERMINATE_MESSAGING
        except Exception as exc:
            print(f"[werewolf_agent] ending the discussion after an error: {exc!r}")
            return TERMINATE_MESSAGING

    def _speech(self, view) -> str:
        if view.role == "seer":
            wolves = [s for s in view.living if view.checks.get(s) == "wolf"]
            if wolves:
                return f"I am the seer. I checked Player{wolves[0]} and they are a wolf. Vote Player{wolves[0]} today."
            return ""  # IDEA: claim the seer role earlier, or share a clean result to protect an innocent
        candidates = {s for s in view.living if s != view.me and not (view.role == "wolf" and s in view.allies)}
        if not candidates:
            return ""
        pick = self._most_suspected(candidates, view)
        if _wrong_votes(view)[pick]:
            return f"Player{pick} voted for someone who turned out to be a villager. I suspect Player{pick}."
        if self._suspicion[pick]:
            return f"Several of us pointed at Player{pick}. I will vote Player{pick}."
        # IDEA: a wolf could message its ally privately here: SendMessage("kill Player3", [ally])
        return f"No evidence yet. I will vote Player{pick} so the day is not wasted. Who do you suspect?"

    # -- reading the game --------------------------------------------------------

    def _most_suspected(self, candidates, view) -> int:
        wrong = _wrong_votes(view)
        return max(candidates, key=lambda s: (2 * wrong[s] + self._suspicion[s], -s))

    def _remember(self, state, view) -> None:
        if view.day != self._day:  # suspicion is per day
            self._day, self._suspicion = view.day, Counter()
        for message in state.new_messages:
            if message.index in self._seen:
                continue
            self._seen.add(message.index)
            if message.sender == view.me or not SUSPECT_WORDS.search(message.content):
                continue
            for seat in set(SEAT.findall(message.content)):
                self._suspicion[int(seat)] += 1  # IDEA: weigh who is speaking, and what they claim

    def _view(self, state, context):
        raw = state.raw
        obs = raw.get("observation") or ""
        found = re.search(r"Your role:\s*(\w+)", obs)
        seat_of = {p["name"]: p["position"] for p in raw.get("players") or [] if "name" in p and "position" in p}
        game = raw.get("game_state") or {}
        history = []
        for entry in game.get("vote_history") or []:
            votes = {seat_of[v]: seat_of.get(t) for v, t in (entry.get("votes") or {}).items() if v in seat_of}
            history.append((entry.get("day"), votes, seat_of.get(entry.get("eliminated"))))
        return SimpleNamespace(
            me=raw.get("your_position", context.seat_position),
            role=found.group(1).lower() if found else "villager",
            allies={int(s) for s in SEAT.findall(_after(obs, "Your fellow wolves:"))},
            checks={int(s): r for s, r in re.findall(r"Player(\d) is (wolf|villager)", _after(obs, "Your investigations:"))},
            living=sorted(seat_of[n] for n in game.get("alive") or [] if n in seat_of),
            dead={seat_of[d["player"]]: d.get("role") for d in game.get("dead") or [] if d.get("player") in seat_of},
            history=history,
            day=game.get("day"),
        )


def _after(text: str, prefix: str) -> str:
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):]
    return ""


def _kind(label: str) -> str:
    first = label.split()[0].lower() if label.split() else ""
    return "kill" if first == "kill" else "investigate" if first == "investigate" else "vote"


def _wrong_votes(view) -> Counter:
    """Seat -> how many times it voted for a player later revealed as a villager."""
    wrong: Counter = Counter()
    for _, votes, eliminated in view.history:
        if eliminated is not None and view.dead.get(eliminated) == "villager":
            for voter, target in votes.items():
                if target == eliminated and voter != view.me:
                    wrong[voter] += 1
    return wrong


def create_agent() -> WerewolfAgent:
    return WerewolfAgent()
