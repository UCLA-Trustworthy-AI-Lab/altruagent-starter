"""Offline Werewolf arena: the platform's simplified rules, played locally.

Re-implements the game as GAMES.md describes it (7 players: 2 wolves, 1 seer,
4 villagers; peaceful first night; one discussion window per day with up to
5 chats of at most 50 words each; hidden sequential day vote, plurality,
ties lynch nobody; wolves win at parity or after 3 days in a row with no
elimination; a 2-wolf disagreement goes to the lower seat). Each agent sees
GameState objects shaped like GameAPI's, so the same agent code runs here
and on the platform.

    python -m sim.werewolf_sim --games 500            # all matchups below
    python -m sim.werewolf_sim --games 200 --seed 7 --verbose 1

What it reports, per matchup, is the win rate of the seat(s) our agent
holds, broken down by the role it was dealt, next to the same number for the
baseline agent holding that seat. The private-observation wording here is our
own guess at the platform's; the agent's parser is written to cope with
variations, but check a real game's log (WEREWOLF_DEBUG=1) to confirm.
"""

from __future__ import annotations

import argparse
import random
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

from altruagent import TERMINATE_MESSAGING, DecisionContext, GameState, SendMessage
from altruagent.models import LegalAction
from altruagent.runner import _validate_decision, _validate_message_decision

N = 7
ABSTAIN = 7
MAX_CHATS = 5
MAX_WORDS = 50
MAX_CHAT_PASSES = 12  # stands in for the 2-minute window


@dataclass
class Result:
    winner: str  # "wolves" / "village"
    roles: list[str]
    days: int
    transcript: list[str] = field(default_factory=list)


class Game:
    def __init__(self, agents: list[Any], rng: random.Random, verbose: bool = False) -> None:
        self.agents = agents
        self.rng = rng
        roles = ["wolf", "wolf", "seer"] + ["villager"] * 4
        rng.shuffle(roles)
        self.roles = roles
        self.names = [f"Player{i}" for i in range(N)]
        self.alive = set(range(N))
        self.dead: list[dict] = []
        self.vote_history: list[dict] = []
        self.seer_log: list[tuple[int, int, str]] = []
        self.day = 0
        self.version = 0
        self.seq = 0
        self.window: list[dict] = []
        self.no_elim_days = 0
        self.verbose = verbose
        self.transcript: list[str] = []
        self.contexts = [
            DecisionContext(session_id="sim", tournament_id=None, game_type="werewolf", agent_id=f"a{i}", seat_position=i)
            for i in range(N)
        ]

    # -- views ------------------------------------------------------------------------------

    def _observation(self, i: int) -> str:
        text = f"You are Player{i}. Your role: {self.roles[i]}."
        if self.roles[i] == "wolf":
            mates = [j for j in range(N) if j != i and self.roles[j] == "wolf"]
            text += " Your fellow wolf is " + ", ".join(f"Player{j}" for j in mates) + "."
        if self.roles[i] == "seer":
            for night, target, verdict in self.seer_log:
                text += f" Night {night}: you investigated Player{target}: {verdict}."
        return text

    def _state(self, i: int, *, phase: str, sub_phase: str | None, legal: list[int] | None) -> GameState:
        self.version += 1
        labels = {"night_wolf": "Kill", "night_seer": "Investigate", "day_vote": "Vote to eliminate"}
        actions = None
        if legal is not None:
            actions = {
                "state_version": self.version,
                "actions": [
                    {"action_id": str(a), "label": "Abstain" if a == ABSTAIN else f"{labels[sub_phase]} Player{a}", "input": {}}
                    for a in legal
                ],
            }
        visible = [m for m in self.window if not m["recipients"] or i in m["recipients"] or m["sender"] == i]
        raw = {
            "session_id": "sim",
            "game_type": "werewolf",
            "phase": phase,
            "is_current_actor": legal is not None,
            "state_version": self.version,
            "observation": self._observation(i),
            "your_position": i,
            "players": [{"position": j, "name": self.names[j], "agent_id": f"a{j}"} for j in range(N)],
            "eliminated": i not in self.alive,
            "game_state": {
                "phase": sub_phase,
                "day": self.day,
                "alive": [self.names[j] for j in sorted(self.alive)],
                "dead": [dict(d) for d in self.dead],
                "vote_history": [dict(v, votes=dict(v["votes"])) for v in self.vote_history],
            },
            "new_messages": [dict(m) for m in visible],
            "messaging_seconds_left": 60,
        }
        if actions is not None:
            raw["legal_actions"] = actions
        return GameState.from_mcp_state(raw)

    def _ask(self, i: int, sub_phase: str, legal: list[int]) -> int:
        state = self._state(i, phase="moving", sub_phase=sub_phase, legal=legal)
        decision = self.agents[i].choose_action(state, self.contexts[i])
        action_id = _validate_decision(decision, state.legal_actions).action_id
        return int(action_id)

    def _log(self, line: str) -> None:
        self.transcript.append(line)
        if self.verbose:
            print(line)

    # -- phases -----------------------------------------------------------------------------

    def _winner(self) -> str | None:
        wolves = sum(1 for s in self.alive if self.roles[s] == "wolf")
        if wolves == 0:
            return "village"
        if wolves >= len(self.alive) - wolves or self.no_elim_days >= 3:
            return "wolves"
        return None

    def _night(self, peaceful: bool) -> None:
        kill_choices: list[tuple[int, int]] = []
        if not peaceful:
            for w in sorted(s for s in self.alive if self.roles[s] == "wolf"):
                legal = sorted(s for s in self.alive if self.roles[s] != "wolf")
                kill_choices.append((w, self._ask(w, "night_wolf", legal)))
        seer = next((s for s in self.alive if self.roles[s] == "seer"), None)
        if seer is not None:
            legal = sorted(s for s in self.alive if s != seer)
            target = self._ask(seer, "night_seer", legal)
            verdict = "wolf" if self.roles[target] == "wolf" else "villager"
            self.seer_log.append((self.day + 1, target, verdict))
            self._log(f"  night {self.day + 1}: seer P{seer} checks P{target} -> {verdict}")
        self.day += 1
        if kill_choices:
            victim = min(t for _, t in kill_choices) if len({t for _, t in kill_choices}) > 1 else kill_choices[0][1]
            self.alive.discard(victim)
            self.dead.append({"name": self.names[victim], "role": self.roles[victim], "cause": "night_kill", "day": self.day})
            self._log(f"  dawn {self.day}: P{victim} ({self.roles[victim]}) killed by wolves {kill_choices}")

    def _discussion(self) -> None:
        self.window = []
        done: set[int] = set()
        sent: dict[int, int] = defaultdict(int)
        for _ in range(MAX_CHAT_PASSES):
            order = sorted(self.alive - done)
            if not order:
                break
            self.rng.shuffle(order)
            for i in order:
                agent = self.agents[i]
                method = getattr(agent, "choose_message", None)
                state = self._state(i, phase="messaging", sub_phase="day_vote", legal=None)
                decision = method(state, self.contexts[i]) if method else TERMINATE_MESSAGING
                decision = _validate_message_decision(decision)
                if decision is TERMINATE_MESSAGING:
                    done.add(i)
                    continue
                assert isinstance(decision, SendMessage)
                words = len(decision.content.split())
                if words > MAX_WORDS or sent[i] >= MAX_CHATS:
                    raise AssertionError(f"P{i} broke the chat limits ({words} words, {sent[i] + 1} chats)")
                recipients = list(decision.recipients or [])
                if len(recipients) > 1 or i in recipients:
                    raise AssertionError(f"P{i} sent to invalid recipients {recipients}")
                sent[i] += 1
                self.seq += 1
                self.window.append({"seq": self.seq, "sender": i, "recipients": recipients, "content": decision.content,
                                    "type": "chat", "move_index": self.version})
                to = f" -> P{recipients[0]}" if recipients else ""
                self._log(f"    P{i} ({self.roles[i]}){to}: {decision.content}")

    def _vote(self) -> None:
        votes: dict[int, int] = {}
        for i in sorted(self.alive):
            legal = sorted(s for s in self.alive if s != i) + [ABSTAIN]
            votes[i] = self._ask(i, "day_vote", legal)
        self.window = []
        self.vote_history.append({"day": self.day, "votes": {
            self.names[v]: ("abstain" if t == ABSTAIN else self.names[t]) for v, t in votes.items()}})
        tally: dict[int, int] = defaultdict(int)
        for t in votes.values():
            if t != ABSTAIN:
                tally[t] += 1
        top = max(tally.values(), default=0)
        leaders = [t for t, c in tally.items() if c == top]
        if top == 0 or len(leaders) > 1:
            self.no_elim_days += 1
            self._log(f"  day {self.day} vote: no lynch {dict(votes)}")
            return
        out = leaders[0]
        self.no_elim_days = 0
        self.alive.discard(out)
        self.dead.append({"name": self.names[out], "role": self.roles[out], "cause": "vote", "day": self.day})
        self._log(f"  day {self.day} vote: P{out} ({self.roles[out]}) lynched {dict(votes)}")

    def play(self) -> Result:
        self._log(f"roles: {self.roles}")
        self._night(peaceful=True)
        while True:
            self._discussion()
            self._vote()
            winner = self._winner()
            if winner:
                break
            self._night(peaceful=False)
            winner = self._winner()
            if winner:
                break
            if self.day > 20:
                winner = "wolves"
                break
        self._log(f"winner: {winner}")
        return Result(winner, list(self.roles), self.day, self.transcript)


# -- baseline opponents -----------------------------------------------------------------------


class FirstLegal:
    """The starter's placeholder: always the first legal action."""

    def choose_action(self, state: GameState, context: DecisionContext) -> LegalAction:
        return state.legal_actions[0]


class RandomAgent:
    def __init__(self, rng: random.Random) -> None:
        self.rng = rng

    def choose_action(self, state: GameState, context: DecisionContext) -> LegalAction:
        options = [a for a in state.legal_actions if a.action_id != str(ABSTAIN)] or state.legal_actions
        return self.rng.choice(options)


class HeuristicAgent:
    """A reasonable, honest-ish opponent, roughly what a decent LLM agent does:
    the seer claims and reports every result, villagers follow seer accusations
    (else vote randomly), wolves kill and vote randomly among villagers and
    never fake-claim."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.accused: dict[int, int] = {}  # target -> day
        self.cleared: set[int] = set()
        self.spoke_day: int | None = None
        self.seen: set[int] = set()

    def _parse(self, state: GameState) -> tuple[int, str, dict[int, str], set[int], int]:
        raw = state.raw
        me = raw["your_position"]
        obs = state.observation
        role = obs.split("Your role: ")[1].split(".")[0]
        results: dict[int, str] = {}
        mates: set[int] = set()
        for part in obs.split("Night ")[1:]:
            target = int(part.split("Player")[1].split(":")[0])
            results[target] = part.split(": ")[-1].rstrip(". ")
        if "fellow wolf is" in obs:
            mates = {int(x) for x in obs.split("fellow wolf is ")[1].replace("Player", "").rstrip(".").split(", ")}
        day = raw["game_state"]["day"]
        for m in state.new_messages:
            if m.index in self.seen or m.sender == me:
                continue
            self.seen.add(m.index)
            if "seer" in m.content.lower():
                for chunk in m.content.split("Player")[1:]:
                    digits = chunk[:1]
                    if digits.isdigit():
                        if "wolf" in chunk[:20].lower():
                            self.accused[int(digits)] = day
                        else:
                            self.cleared.add(int(digits))
        return me, role, results, mates, day

    def choose_message(self, state: GameState, context: DecisionContext):
        me, role, results, mates, day = self._parse(state)
        if self.spoke_day == day:
            return TERMINATE_MESSAGING
        self.spoke_day = day
        if role == "seer" and results:
            text = "I am the seer. " + " ".join(f"Player{t} is a {v}." for t, v in results.items())
            return SendMessage(text)
        return SendMessage("I am a villager. Let's find the wolves.")

    def choose_action(self, state: GameState, context: DecisionContext) -> LegalAction:
        me, role, results, mates, day = self._parse(state)
        legal = {int(a.action_id): a for a in state.legal_actions}
        options = [s for s in legal if s != ABSTAIN]
        phase = state.raw["game_state"]["phase"]
        if role == "wolf":
            pick = [s for s in options if s not in mates] or options
            return legal[self.rng.choice(pick)]
        if role == "seer":
            if phase == "night_seer":
                fresh = [s for s in options if s not in results] or options
                return legal[self.rng.choice(fresh)]
            wolves = [s for s in options if results.get(s) == "wolf"]
            if wolves:
                return legal[wolves[0]]
            pick = [s for s in options if s not in results] or options
            return legal[self.rng.choice(pick)]
        accused = [s for s in options if s in self.accused]
        if accused:
            return legal[accused[0]]
        pick = [s for s in options if s not in self.cleared] or options
        return legal[self.rng.choice(pick)]


# -- arena ------------------------------------------------------------------------------------

Factory = Callable[[random.Random], Any]


def ours(rng: random.Random) -> Any:
    from agent.werewolf import WerewolfAgent

    return WerewolfAgent(rng=random.Random(rng.random()))


BASELINES: dict[str, Factory] = {
    "first_legal": lambda rng: FirstLegal(),
    "random": lambda rng: RandomAgent(random.Random(rng.random())),
    "heuristic": lambda rng: HeuristicAgent(random.Random(rng.random())),
}


def run_matchup(hero: Factory, villain: Factory, *, hero_seats: int, games: int, seed: int, verbose: int = 0):
    """Win rate (and games) of the hero's seats, by role.

    Game ``g`` always deals the same roles and picks the same hero seats for a
    given seed, whoever the hero is, so two heroes can be compared game for
    game (paired), which cuts the noise a lot.
    """
    wins: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    village_wins = 0
    for g in range(games):
        deal = random.Random(seed * 1_000_003 + g)
        seats = set(deal.sample(range(N), hero_seats))
        agent_rng = random.Random(seed * 7_919 + g)
        agents = [hero(agent_rng) if i in seats else villain(agent_rng) for i in range(N)]
        result = Game(agents, deal, verbose=g < verbose).play()
        village_wins += result.winner == "village"
        for i in seats:
            role = result.roles[i]
            won = (result.winner == "wolves") == (role == "wolf")
            wins[role][0] += won
            wins[role][1] += 1
            wins["all"][0] += won
            wins["all"][1] += 1
    return wins, village_wins / games


def _fmt(wins: dict[str, list[int]]) -> str:
    parts = []
    for role in ("all", "villager", "seer", "wolf"):
        won, played = wins.get(role, [0, 0])
        parts.append(f"{role}: {won / played:5.1%} (n={played})" if played else f"{role}:   -  ")
    return " | ".join(parts)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--games", type=int, default=300)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--verbose", type=int, default=0, help="print the first N games' transcripts")
    args = parser.parse_args(argv)

    print(f"{args.games} games per matchup, seed {args.seed}\n")
    for name, villain in BASELINES.items():
        base, base_village = run_matchup(villain, villain, hero_seats=1, games=args.games, seed=args.seed)
        hero, hero_village = run_matchup(ours, villain, hero_seats=1, games=args.games, seed=args.seed, verbose=args.verbose)
        print(f"1 seat vs 6x {name}")
        print(f"  {name:>11} seat: {_fmt(base)}")
        print(f"  {'ours':>11} seat: {_fmt(hero)}")
    selfplay, village = run_matchup(ours, ours, hero_seats=N, games=args.games, seed=args.seed)
    print(f"\nself-play (7x ours): village wins {village:.1%}")


if __name__ == "__main__":
    main()
