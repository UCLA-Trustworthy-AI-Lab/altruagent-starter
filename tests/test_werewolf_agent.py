"""Tests for examples/werewolf_agent.py, using plain stand-ins for GameState."""

from types import SimpleNamespace

from altruagent import TERMINATE_MESSAGING, SendMessage, WithReasoning
from examples.werewolf_agent import create_agent

NAMES = [f"bot (Player{i}, Self-hosted)" for i in range(7)]
PLAYERS = [{"position": i, "name": n} for i, n in enumerate(NAMES)]


def action(seat, label):
    return SimpleNamespace(action_id=str(seat), label=label, input={}, raw={})


def vote_actions(me, living):
    acts = [action(s, f"Vote to eliminate Player{s}") for s in living if s != me]
    return acts + [action(7, "Abstain")]


def make_state(me, observation, *, alive=range(7), dead=(), history=(), legal=(), messages=(), version=1, day=1):
    game = {
        "day": day,
        "alive": [NAMES[i] for i in alive],
        "dead": [{"day": d, "player": NAMES[p], "role": r, "cause": "vote"} for d, p, r in dead],
        "vote_history": [
            {"day": d, "votes": {NAMES[v]: (NAMES[t] if t is not None else "abstain") for v, t in votes.items()},
             "eliminated": NAMES[e] if e is not None else None}
            for d, votes, e in history
        ],
    }
    raw = {"your_position": me, "observation": observation, "players": PLAYERS, "game_state": game}
    msgs = [SimpleNamespace(index=i, sender=s, recipients=[], content=c) for i, (s, c) in enumerate(messages, start=1)]
    return SimpleNamespace(raw=raw, legal_actions=list(legal), new_messages=msgs, state_version=version, phase="moving")


def ctx(game="werewolf", seat=0):
    return SimpleNamespace(game_type=game, seat_position=seat)


def seat_of(move):
    inner = move.action if isinstance(move, WithReasoning) else move
    return int(inner.action_id)


VILLAGER = "=== Werewolf ===\nYour role: VILLAGER\nYou do not know anyone else's role."


def test_villager_with_no_information_votes_lowest_seat_and_never_abstains():
    state = make_state(3, VILLAGER, legal=vote_actions(3, range(7)))
    move = create_agent().choose_action(state, ctx(seat=3))
    assert seat_of(move) == 0


def test_seat_zero_does_not_vote_for_itself():
    state = make_state(0, VILLAGER, legal=vote_actions(0, range(7)))
    assert seat_of(create_agent().choose_action(state, ctx())) == 1


def test_wolf_never_votes_for_its_ally():
    obs = "=== Werewolf ===\nYour role: WOLF\nYour fellow wolves: Player0"
    state = make_state(4, obs, legal=vote_actions(4, range(7)))
    assert seat_of(create_agent().choose_action(state, ctx(seat=4))) == 1


def test_wolf_kills_the_player_who_voted_against_wolves():
    obs = "=== Werewolf ===\nYour role: WOLF\nYour fellow wolves: Player1"
    history = [(1, {2: 1, 3: 6, 5: 4}, None)]  # Player2 voted for the wolf Player1
    legal = [action(s, f"Kill Player{s}") for s in (0, 2, 3, 5, 6)]
    state = make_state(4, obs, history=history, legal=legal, day=2)
    assert seat_of(create_agent().choose_action(state, ctx(seat=4))) == 2


def test_seer_investigates_a_player_not_checked_yet():
    obs = "=== Werewolf ===\nYour role: SEER\nYour investigations: Player0 is villager, Player1 is villager"
    legal = [action(s, f"Investigate Player{s}") for s in range(7) if s != 3]
    state = make_state(3, obs, legal=legal)
    assert seat_of(create_agent().choose_action(state, ctx(seat=3))) == 2


def test_seer_votes_for_and_announces_a_wolf():
    obs = "=== Werewolf ===\nYour role: SEER\nYour investigations: Player0 is villager, Player5 is wolf"
    state = make_state(3, obs, legal=vote_actions(3, range(7)), day=2)
    agent = create_agent()
    assert seat_of(agent.choose_action(state, ctx(seat=3))) == 5
    spoken = agent.choose_message(state, ctx(seat=3))
    assert isinstance(spoken, SendMessage) and "Player5" in spoken.content and "wolf" in spoken.content
    assert len(spoken.content.split()) <= 40


def test_seer_with_only_clean_results_stays_quiet():
    obs = "=== Werewolf ===\nYour role: SEER\nYour investigations: Player0 is villager"
    state = make_state(3, obs)
    assert create_agent().choose_message(state, ctx(seat=3)) is TERMINATE_MESSAGING


def test_a_wrong_vote_makes_that_voter_the_target():
    # Day 1: Player2 and Player3 voted out Player6, who was a villager.
    history = [(1, {2: 6, 3: 6, 4: 1}, 6)]
    state = make_state(0, VILLAGER, alive=range(6), dead=[(1, 6, "villager")], history=history,
                       legal=vote_actions(0, range(6)), day=2)
    # ties on the wrong-vote count go to the lower seat: Player2
    assert seat_of(create_agent().choose_action(state, ctx())) == 2


def test_chat_suspicion_moves_the_vote():
    messages = [(4, "I suspect Player5, they are quiet."), (6, "Yes, vote Player5 today.")]
    state = make_state(0, VILLAGER, messages=messages, legal=vote_actions(0, range(7)))
    agent = create_agent()
    agent.choose_message(state, ctx())  # reads the messages
    assert seat_of(agent.choose_action(state, ctx())) == 5


def test_speaks_once_per_window_then_ends_it():
    agent = create_agent()
    state = make_state(0, VILLAGER, version=7)
    first = agent.choose_message(state, ctx())
    assert isinstance(first, SendMessage) and len(first.content.split()) <= 40
    assert agent.choose_message(state, ctx()) is TERMINATE_MESSAGING
    next_window = make_state(0, VILLAGER, version=11, day=2)
    assert isinstance(agent.choose_message(next_window, ctx()), SendMessage)


def test_other_games_get_the_first_legal_action():
    state = make_state(0, VILLAGER, legal=[action(9, "x"), action(8, "y")])
    assert agent_move(state, "pokemon_vgc_doubles_draft").action_id == "9"


def agent_move(state, game):
    return create_agent().choose_action(state, ctx(game=game))


def test_an_unexpected_state_falls_back_to_a_non_abstain_move():
    state = make_state(0, VILLAGER, legal=vote_actions(0, range(7)))
    state.raw["game_state"] = "not a dict"  # makes the view fail
    move = create_agent().choose_action(state, ctx())
    assert move.action_id != "7"
