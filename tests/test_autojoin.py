"""Unit tests for altruagent.autojoin — joining a platform tournament game by
id (``join_and_play`` and its parts) and the ``--tournament-auto`` loop.

A scripted FakeClient stands in for AltruAgentClient (sessions, joins,
competition rows, tournament detail) and FakeProcess for
multiprocessing.Process, so every path is deterministic; ``sleep``/``now``/
``clock`` are injected. No network, no processes.
"""

from __future__ import annotations

import types

import httpx
import pytest

from altruagent import autojoin
from altruagent.autojoin import (
    AutoJoinState,
    GameNeverStarted,
    JoinRefused,
    describe_final_standing,
    describe_outcome,
    is_transient,
    join_and_play,
    join_failure_is_retryable,
    join_with_retry,
    play_to_end,
    run_autojoin_forever,
    run_autojoin_once,
    wait_for_start,
)
from altruagent.client import AltruAgentClient
from altruagent.errors import AuthenticationError, PlatformError
from altruagent.mcp_transport import MCPToolError
from altruagent.models import AgentSessions, AgentTournamentMatch, JoinResult, Match, TournamentDetail
from altruagent.runner import DecisionError
from altruagent.worker import EXIT_MATCH_FAILURE, EXIT_SUCCESS

from test_supervisor import FakeProcessFactory

AGENT_ID = "agent-a"
T1 = "t-1"


def row(session_id="game-1", *, status="join_now", tournament_id=T1, deadline="2026-10-06T12:04:00Z", seconds_left=200):
    return AgentTournamentMatch.from_dict({
        "tournament_id": tournament_id, "tournament_name": "Autumn Cup", "round_label": "Swiss round 1 of 3",
        "match_id": "r1-m1", "session_id": session_id, "game_type": "pokemon_vgc_doubles_draft", "game_no": 1,
        "join_deadline_at": deadline, "seconds_left": seconds_left, "status": status,
        "opponents": [{"agent_id": "agent-b", "agent_name": "Bravo"}],
    })


def comp(session_id="game-1", status="in_progress", tournament_id=T1, **extra) -> Match:
    return Match.from_dict({"session_id": session_id, "status": status, "tournament_id": tournament_id,
                            "game_type": "pokemon_vgc_doubles_draft", **extra})


def sessions(*, rows=(), waiting=(), active=(), completed=()) -> AgentSessions:
    return AgentSessions(waiting=list(waiting), active=list(active), completed=list(completed),
                         tournament_matches=list(rows))


class FakeClient:
    """Scripted: each ``sessions()`` call takes the next entry of
    ``sessions_script`` (the last one repeats); an entry that is an exception
    is raised. Joins answer from ``join_answers`` the same way."""

    def __init__(self, sessions_script=(), *, join_answers=(), competitions=None, tournaments=()):
        self.sessions_script = list(sessions_script)
        self.join_answers = list(join_answers)
        self.competitions = competitions or {}
        self.tournaments = list(tournaments)
        self.joins: list[str] = []
        self.sessions_calls = 0
        self.tournament_calls = 0
        self.competition_calls: list[str] = []

    @staticmethod
    def _next(script, count):
        answer = script[min(count, len(script) - 1)]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def sessions(self):
        self.sessions_calls += 1
        return self._next(self.sessions_script, self.sessions_calls - 1)

    def join_competition(self, session_id):
        self.joins.append(session_id)
        answer = self._next(self.join_answers, len(self.joins) - 1) if self.join_answers else {"status": "waiting"}
        return JoinResult.from_dict(answer, session_id=session_id, transport="mcp")

    def competition(self, session_id):
        self.competition_calls.append(session_id)
        answers = self.competitions.get(session_id, [{}])
        return self._next(answers, len(self.competition_calls) - 1 if len(answers) > 1 else 0)

    def tournament(self, tournament_id):
        self.tournament_calls += 1
        return self._next(self.tournaments, self.tournament_calls - 1)


class Clock:
    def __init__(self, start=0.0):
        self.value = start
        self.sleeps: list[float] = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


def network_down():
    return PlatformError("Could not reach the control plane", status_code=None)


# -- is_transient / describe_outcome ------------------------------------------------------


@pytest.mark.parametrize(
    "exc, transient",
    [
        (PlatformError("no answer", status_code=None), True),
        (PlatformError("bad gateway", status_code=502), True),
        (PlatformError("slow down", status_code=429, error_code="rate_limited"), True),
        (PlatformError("busy", status_code=503, error_code="game_temporarily_unavailable"), True),
        (MCPToolError("backend down", status_code=None, error_code="BACKEND_UNAVAILABLE"), True),
        (MCPToolError("no answer", status_code=None, error_code=None), True),
        (PlatformError("refused", status_code=409, error_code="not_in_this_match"), False),
        (MCPToolError("refused", status_code=None, error_code="join_deadline_passed"), False),
        (MCPToolError("gone", status_code=None, error_code="SESSION_NOT_FOUND"), False),
        (AuthenticationError("bad key", status_code=401), False),
        (AuthenticationError("login 502", status_code=502), True),
        (AuthenticationError("Failed to create session", status_code=401, transient=True), True),
        (MCPToolError("busy", status_code=None, error_code="join_temporarily_failed"), True),
        (MCPToolError("refused?", status_code=None, error_code="SESSION_JOIN_FAILED"), False),
    ],
)
def test_is_transient(exc, transient):
    assert is_transient(exc) is transient


def session_join_failed(detail="Backend request failed."):
    return MCPToolError(detail, status_code=None, error_code="SESSION_JOIN_FAILED")


@pytest.mark.parametrize(
    "exc, retryable",
    [
        (network_down(), True),
        (session_join_failed(), True),
        (PlatformError("TypeError: fetch failed", status_code=400, error_code="join_failed",
                       detail="TypeError: fetch failed"), True),
        (MCPToolError("Invalid or expired token", status_code=None, error_code="Invalid or expired token"), True),
        (MCPToolError("token", status_code=None, error_code="UNAUTHENTICATED"), True),
        (session_join_failed("Competition is full"), False),
        (session_join_failed("Competition not found"), False),
        (PlatformError("Competition is not accepting participants", status_code=400, error_code="join_failed",
                       detail="Competition is not accepting participants"), False),
        (MCPToolError("refused", status_code=None, error_code="not_in_this_match"), False),
        (MCPToolError("refused", status_code=None, error_code="join_deadline_passed"), False),
        (MCPToolError("refused", status_code=None, error_code="match_start_failed"), False),
        (AuthenticationError("Invalid API key", status_code=401), False),
    ],
)
def test_join_failure_is_retryable(exc, retryable):
    assert join_failure_is_retryable(exc) is retryable


@pytest.mark.parametrize(
    "competition, expected",
    [
        ({"status": "in_progress"}, None),
        ({"status": "completed", "winner_agent_ids": [AGENT_ID], "results": {AGENT_ID: 1}}, "You won."),
        ({"status": "completed", "winner_agent_ids": ["agent-b"], "results": {"agent-b": 1}}, "You lost."),
        ({"status": "completed", "winner_agent_id": AGENT_ID, "results": {AGENT_ID: 1}}, "You won."),
        ({"status": "completed", "winner_agent_ids": [], "results": {AGENT_ID: 0, "agent-b": 0}}, "Draw."),
        ({"status": "completed", "failure_reason": "tournament_no_show", "winner_agent_ids": [AGENT_ID]},
         "You won: not every other agent joined in time."),
        ({"status": "completed", "failure_reason": "tournament_no_show", "winner_agent_ids": ["agent-b"]},
         "You lost: your agent didn't join in time."),
        ({"status": "completed", "failure_reason": "tournament_no_show", "winner_agent_ids": []},
         "No result: nobody joined in time, so nobody scores for this game."),
        ({"status": "completed", "failure_reason": "tournament_stale", "winner_agent_ids": [], "results": {}},
         "No result (tournament_stale): the game ended without a winner."),
        ({"status": "completed", "winner_agent_ids": [], "results": {}}, "No result: the game ended without a winner."),
    ],
)
def test_describe_outcome(competition, expected):
    assert describe_outcome(competition, AGENT_ID) == expected


def test_werewolf_whole_winning_faction_won():
    competition = {"status": "completed", "winner_agent_ids": ["w1", AGENT_ID, "w3"], "results": {}}
    assert describe_outcome(competition, AGENT_ID) == "You won."


# -- join_with_retry -------------------------------------------------------------------------------


def test_join_with_retry_returns_the_join_result():
    client = FakeClient(join_answers=[{"status": "waiting", "already_joined": True}])

    result = join_with_retry(client, "game-1", sleep=pytest.fail, log=lambda m: None)

    assert client.joins == ["game-1"] and result.already_joined is True


def test_join_with_retry_retries_transient_failures_then_joins():
    clock, logs = Clock(), []
    client = FakeClient(join_answers=[network_down(), PlatformError("busy", status_code=503,
                                      error_code="game_temporarily_unavailable"), {"status": "in_progress"}])

    result = join_with_retry(client, "game-1", sleep=clock.sleep, now=clock.now, log=logs.append)

    assert result.status == "in_progress"
    assert client.joins == ["game-1"] * 3 and clock.sleeps == [5.0, 5.0]
    assert len(logs) == 1 and logs[0].startswith("Could not join yet")


def test_join_with_retry_gives_up_on_transient_failures_after_the_window():
    clock = Clock()
    client = FakeClient(join_answers=[network_down()])

    with pytest.raises(PlatformError):
        join_with_retry(client, "game-1", retry_window=12, sleep=clock.sleep, now=clock.now, log=lambda m: None)

    assert clock.sleeps == [5.0, 5.0, 5.0]


@pytest.mark.parametrize("code, phrase", [
    ("not_in_this_match", "reserved for the agents paired into it"),
    ("join_deadline_passed", "counts as a loss"),
])
def test_join_with_retry_refusal_is_explained_and_not_retried(code, phrase):
    client = FakeClient(join_answers=[MCPToolError("Refused by the platform.", status_code=None, error_code=code)])

    with pytest.raises(JoinRefused) as exc_info:
        join_with_retry(client, "game-1", sleep=pytest.fail, log=lambda m: None)

    assert exc_info.value.error_code == code
    assert phrase in str(exc_info.value) and "Refused by the platform." in str(exc_info.value)
    assert client.joins == ["game-1"]


def test_join_with_retry_unknown_refusal_keeps_the_platform_message():
    client = FakeClient(join_answers=[MCPToolError("Competition is full", status_code=None, error_code="SESSION_JOIN_FAILED")])

    with pytest.raises(JoinRefused, match="Competition is full"):
        join_with_retry(client, "game-1", sleep=pytest.fail, log=lambda m: None)


def test_join_with_retry_retries_a_join_that_failed_without_saying_why():
    """SESSION_JOIN_FAILED can hide a temporary failure (a database blip
    behind the control plane): retried, not reported as a refusal."""
    clock, logs = Clock(), []
    client = FakeClient(join_answers=[session_join_failed(), session_join_failed(), {"status": "waiting"}])

    result = join_with_retry(client, "game-1", sleep=clock.sleep, now=clock.now, log=logs.append)

    assert result.status == "waiting" and client.joins == ["game-1"] * 3
    assert len(logs) == 1 and logs[0].startswith("Could not join yet")


def test_join_with_retry_reports_an_unexplained_failure_once_the_window_is_over():
    clock = Clock()
    client = FakeClient(join_answers=[session_join_failed("Backend request failed.")])

    with pytest.raises(JoinRefused, match="Backend request failed."):
        join_with_retry(client, "game-1", retry_window=12, sleep=clock.sleep, now=clock.now, log=lambda m: None)

    assert clock.sleeps == [5.0, 5.0, 5.0]


def test_join_with_retry_retries_a_token_the_control_plane_rejected_inside_the_join():
    clock = Clock()
    client = FakeClient(join_answers=[
        MCPToolError("Invalid or expired token", status_code=None, error_code="Invalid or expired token"),
        {"status": "waiting"},
    ])

    assert join_with_retry(client, "game-1", sleep=clock.sleep, now=clock.now, log=lambda m: None).status == "waiting"
    assert client.joins == ["game-1", "game-1"]


def test_join_with_retry_rides_out_a_login_the_platform_could_not_complete():
    clock = Clock()
    client = FakeClient(join_answers=[
        AuthenticationError("Failed to create session", status_code=401, transient=True),
        {"status": "waiting"},
    ])

    assert join_with_retry(client, "game-1", sleep=clock.sleep, now=clock.now, log=lambda m: None).status == "waiting"


def test_join_with_retry_rejected_api_key_propagates():
    client = FakeClient(join_answers=[AuthenticationError("Invalid API key", status_code=401)])

    with pytest.raises(AuthenticationError):
        join_with_retry(client, "game-1", sleep=pytest.fail, log=lambda m: None)


# -- wait_for_start -----------------------------------------------------------------------------


def test_wait_for_start_returns_the_active_match_and_prints_the_pairing():
    clock, logs = Clock(1_000.0), []
    client = FakeClient([sessions(rows=[row(status="joined_waiting")], waiting=[comp(status="waiting")]),
                         sessions(rows=[row(status="in_progress")], active=[comp()])])

    kind, match = wait_for_start(client, "game-1", sleep=clock.sleep, clock=clock.now, log=logs.append)

    assert kind == "active" and match.session_id == "game-1"
    assert clock.sleeps == [3.0]
    assert logs[0] == 'Swiss round 1 of 3 of "Autumn Cup" (pokemon_vgc_doubles_draft). Opponent(s): Bravo.'
    assert logs[1].startswith("Waiting for the other agent(s) to join (join deadline 12:04:00 UTC, 200s left)")


def test_wait_for_start_returns_a_game_closed_as_a_no_show():
    closed = comp(status="completed", failure_reason="tournament_no_show", winner_agent_ids=[AGENT_ID])
    client = FakeClient([sessions(completed=[closed])])

    kind, competition = wait_for_start(client, "game-1", sleep=pytest.fail, log=lambda m: None)

    assert kind == "completed" and competition["failure_reason"] == "tournament_no_show"


def test_wait_for_start_rides_out_transient_failures():
    clock, logs = Clock(), []
    client = FakeClient([network_down(), network_down(), sessions(active=[comp()])])

    kind, _ = wait_for_start(client, "game-1", sleep=clock.sleep, clock=clock.now, log=logs.append)

    assert kind == "active"
    assert logs == ["Could not check the game (Could not reach the control plane); will keep retrying.",
                    "Connection recovered."]


def test_wait_for_start_rejected_key_propagates():
    client = FakeClient([AuthenticationError("Invalid API key", status_code=401)])

    with pytest.raises(AuthenticationError):
        wait_for_start(client, "game-1", sleep=pytest.fail, log=lambda m: None)


def test_wait_for_start_asks_for_an_unlisted_competition_directly():
    client = FakeClient([sessions()], competitions={"game-1": [
        {"session_id": "game-1", "status": "in_progress", "game_server_url": "gameapi.example.test",
         "tournament_id": T1}]})

    kind, match = wait_for_start(client, "game-1", sleep=pytest.fail, log=lambda m: None)

    assert kind == "active" and match.game_server_url == "gameapi.example.test" and match.tournament_id == T1


def test_wait_for_start_gives_up_long_after_the_join_deadline():
    iso = "2026-10-06T12:04:00Z"
    deadline = autojoin._parse_time(iso).timestamp()
    clock = Clock(deadline - 10)
    client = FakeClient([sessions(rows=[row(status="joined_waiting", deadline=iso)], waiting=[comp(status="waiting")])])

    with pytest.raises(GameNeverStarted):
        wait_for_start(client, "game-1", give_up_after_deadline=60, sleep=clock.sleep, clock=clock.now, log=lambda m: None)

    assert clock.now() > deadline + 60


def test_wait_for_start_waits_indefinitely_for_a_non_tournament_competition():
    clock = Clock()
    script = [sessions(waiting=[comp(status="waiting", tournament_id=None)])] * 400 + [
        sessions(active=[comp(tournament_id=None)])]
    client = FakeClient(script)

    kind, _ = wait_for_start(client, "game-1", sleep=clock.sleep, clock=clock.now, log=lambda m: None)

    assert kind == "active" and len(clock.sleeps) == 400


# -- play_to_end -----------------------------------------------------------------------------------


class Game:
    def __init__(self, client, *, session_id, game_server_url):
        self.session_id, self.game_server_url = session_id, game_server_url


def playable(session_id="game-1"):
    match = comp(session_id)
    match.game_server_url = "https://gameapi.example.test"
    return match


def test_play_to_end_resolves_the_game_server_from_the_competition():
    seen = []

    def run_game_fn(game, context, contestant):
        seen.append(game.game_server_url)
        return types.SimpleNamespace(termination_reason="normal", returns={})

    client = FakeClient(competitions={"game-1": [{"status": "in_progress", "game_server_url": "gameapi.example.test"}]})
    match = comp("game-1")
    play_to_end(client, match, "c", agent_id=AGENT_ID, game_factory=Game, run_game_fn=run_game_fn,
                sleep=pytest.fail, log=lambda m: None)

    assert seen == ["gameapi.example.test"] and match.game_server_url == "gameapi.example.test"


def test_play_to_end_returns_none_for_a_game_that_already_ended():
    client = FakeClient(competitions={"game-1": [{"status": "completed"}]})

    assert play_to_end(client, comp("game-1"), "c", agent_id=AGENT_ID, game_factory=Game, run_game_fn=pytest.fail,
                       sleep=pytest.fail, log=lambda m: None) is None


def test_play_to_end_runs_the_game_with_the_contestant():
    runs = []

    def run_game_fn(game, context, contestant):
        runs.append((game.session_id, context.tournament_id, context.agent_id, contestant))
        return types.SimpleNamespace(termination_reason="normal", returns={AGENT_ID: 1.0})

    final = play_to_end(FakeClient(), playable(), "contestant", agent_id=AGENT_ID, game_factory=Game,
                        run_game_fn=run_game_fn, sleep=pytest.fail, log=lambda m: None)

    assert final.returns == {AGENT_ID: 1.0}
    assert runs == [("game-1", T1, AGENT_ID, "contestant")]


def test_play_to_end_resumes_after_a_lost_connection_with_the_same_contestant():
    clock, logs, contestants = Clock(), [], []
    answers = [network_down(), types.SimpleNamespace(termination_reason="normal", returns={})]

    def run_game_fn(game, context, contestant):
        contestants.append(contestant)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    client = FakeClient(competitions={"game-1": [{"status": "in_progress"}]})
    final = play_to_end(client, playable(), "same", agent_id=AGENT_ID, game_factory=Game,
                        run_game_fn=run_game_fn, sleep=clock.sleep, log=logs.append)

    assert final.termination_reason == "normal"
    assert contestants == ["same", "same"] and clock.sleeps == [5.0]
    assert logs[0].startswith("Lost contact with the game")


def test_play_to_end_returns_none_when_the_game_ended_while_disconnected():
    def run_game_fn(game, context, contestant):
        raise network_down()

    client = FakeClient(competitions={"game-1": [{"status": "completed"}]})

    assert play_to_end(client, playable(), "c", agent_id=AGENT_ID, game_factory=Game, run_game_fn=run_game_fn,
                       sleep=lambda s: None, log=lambda m: None) is None


def test_play_to_end_stops_after_max_resumes():
    def run_game_fn(game, context, contestant):
        raise network_down()

    client = FakeClient(competitions={"game-1": [{"status": "in_progress"}]})

    with pytest.raises(PlatformError):
        play_to_end(client, playable(), "c", agent_id=AGENT_ID, game_factory=Game, run_game_fn=run_game_fn,
                    max_resumes=2, sleep=lambda s: None, log=lambda m: None)


@pytest.mark.parametrize("error", [DecisionError("bad move"),
                                   MCPToolError("gone", status_code=None, error_code="SESSION_NOT_FOUND")])
def test_play_to_end_does_not_retry_real_failures(error):
    def run_game_fn(game, context, contestant):
        raise error

    with pytest.raises(type(error)):
        play_to_end(FakeClient(), playable(), "c", agent_id=AGENT_ID, game_factory=Game, run_game_fn=run_game_fn,
                    sleep=pytest.fail, log=lambda m: None)


# -- join_and_play --------------------------------------------------------------------------------


def test_join_and_play_joins_waits_plays_and_reports_the_result():
    logs = []
    final_row = {"session_id": "game-1", "status": "completed", "winner_agent_ids": [AGENT_ID], "results": {AGENT_ID: 1}}
    client = FakeClient([sessions(rows=[row(status="joined_waiting")], waiting=[comp(status="waiting")]),
                         sessions(active=[playable()])],
                        join_answers=[{"status": "waiting"}],
                        competitions={"game-1": [final_row]})

    def run_game_fn(game, context, contestant):
        return types.SimpleNamespace(termination_reason="normal", returns={AGENT_ID: 1.0})

    clock = Clock()
    result = join_and_play(client, "game-1", "contestant", agent_id=AGENT_ID, game_factory=Game,
                           run_game_fn=run_game_fn, sleep=clock.sleep, now=clock.now, clock=clock.now, log=logs.append)

    assert client.joins == ["game-1"]
    assert result.outcome == "You won." and result.final_state.returns == {AGENT_ID: 1.0}
    assert logs[0] == "Joined competition game-1."
    assert "The game has started. Connecting..." in logs
    assert logs[-3:] == ["Match finished (termination_reason=normal).", "Your score: 1.0", "Result: You won."]


def test_join_and_play_reports_a_win_by_no_show_without_playing():
    logs = []
    closed = comp(status="completed", failure_reason="tournament_no_show", winner_agent_ids=[AGENT_ID])
    client = FakeClient([sessions(completed=[closed])], join_answers=[{"status": "waiting"}])

    result = join_and_play(client, "game-1", "c", agent_id=AGENT_ID, run_game_fn=pytest.fail,
                           sleep=pytest.fail, log=logs.append)

    assert result.final_state is None
    assert logs[-1] == "Result: You won: not every other agent joined in time."


def test_join_and_play_rereads_a_no_show_whose_winners_are_not_written_yet():
    half_closed = comp(status="completed", failure_reason="tournament_no_show", winner_agent_ids=[])
    client = FakeClient([sessions(completed=[half_closed])], join_answers=[{"status": "waiting"}],
                        competitions={"game-1": [{"status": "completed", "failure_reason": "tournament_no_show",
                                                  "winner_agent_ids": [AGENT_ID]}]})

    result = join_and_play(client, "game-1", "c", agent_id=AGENT_ID, run_game_fn=pytest.fail,
                           sleep=lambda s: None, log=lambda m: None)

    assert result.outcome == "You won: not every other agent joined in time."


def test_join_and_play_notes_a_rest_join():
    logs = []
    client = FakeClient([sessions(completed=[comp(status="completed", winner_agent_ids=["agent-b"], results={"x": 1})])])
    client.join_competition = lambda sid: JoinResult.from_dict({"status": "waiting", "already_joined": True},
                                                               session_id=sid, transport="rest")

    join_and_play(client, "game-1", "c", agent_id=AGENT_ID, run_game_fn=pytest.fail, sleep=pytest.fail, log=logs.append)

    assert logs[0] == "Joined competition game-1 (already joined) through the REST API (no MCP endpoint is configured)."
    assert logs[-1] == "Result: You lost."

    client.mcp_url = "https://gameapi.example.test/mcp"
    logs.clear()
    join_and_play(client, "game-1", "c", agent_id=AGENT_ID, run_game_fn=pytest.fail, sleep=pytest.fail, log=logs.append)
    assert logs[0].endswith("through the REST API (the MCP endpoint couldn't be reached).")


# -- --tournament-auto: run_autojoin_once -----------------------------------------------------------


def tick(client, state=None, *, tournament_id=None, factory=None, now=lambda: 0.0, agent_spec="examples.llm_agent"):
    state = state or AutoJoinState()
    factory = factory or FakeProcessFactory()
    logs: list[str] = []
    result = run_autojoin_once(client, state, agent_id=AGENT_ID, agent_spec=agent_spec, tournament_id=tournament_id,
                               now=now, process_factory=factory, log=logs.append)
    return state, factory, logs, result


def test_auto_joins_every_join_now_game_right_away():
    client = FakeClient([sessions(rows=[row("game-1"), row("game-2", tournament_id="t-2"),
                                        row("game-3", status="joined_waiting"), row("game-4", status="in_progress")])])

    _, _, logs, _ = tick(client)

    assert client.joins == ["game-1", "game-2"]
    assert logs[0] == ('Swiss round 1 of 3 of "Autumn Cup": joining game game-1 '
                       "(opponent(s): Bravo; 200s left to join)...")
    assert logs[1] == "Joined game game-1; waiting for the other agent(s)."


def test_auto_with_a_tournament_id_only_touches_that_tournament():
    client = FakeClient([sessions(rows=[row("game-1"), row("game-2", tournament_id="t-2")],
                                  active=[comp("game-5"), comp("game-6", tournament_id="t-2")])])

    _, factory, _, _ = tick(client, tournament_id=T1)

    assert client.joins == ["game-1"]
    assert [p.args[0].session_id for p in factory.processes] == ["game-5"]


def test_auto_refused_join_is_reported_once_and_never_retried():
    client = FakeClient([sessions(rows=[row("game-1")])],
                        join_answers=[MCPToolError("Too late.", status_code=None, error_code="join_deadline_passed")])
    state, _, logs, _ = tick(client)
    tick(client, state)

    assert client.joins == ["game-1"]
    assert state.refused == {"game-1": "join_deadline_passed"}
    assert logs[-1].startswith("Could not join game game-1: the join window for this game has closed")


def test_auto_join_that_failed_without_saying_why_is_retried_until_the_deadline():
    """A Supabase blip or a Lambda 500 behind the join arrives as
    SESSION_JOIN_FAILED: never stored as refused; tried again every 10 s
    while the game is still listed as join_now."""
    clock = Clock()
    client = FakeClient([sessions(rows=[row("game-1")])],
                        join_answers=[session_join_failed(), session_join_failed(), {"status": "waiting"}])
    state, _, logs, _ = tick(client, now=clock.now)

    assert state.refused == {} and client.joins == ["game-1"]
    assert logs[-1] == ("Could not join game game-1 yet (Backend request failed.); trying again in 10s, "
                        "until its join deadline.")

    clock.value += 5
    tick(client, state, now=clock.now)
    assert client.joins == ["game-1"]  # still cooling down

    clock.value += 5
    tick(client, state, now=clock.now)
    assert client.joins == ["game-1"] * 2 and state.refused == {}

    clock.value += 10
    _, _, logs, _ = tick(client, state, now=clock.now)
    assert client.joins == ["game-1"] * 3
    assert logs[-1] == "Joined game game-1; waiting for the other agent(s)."
    assert state.join_retry_at == {}


def test_auto_unexplained_join_failure_stops_once_the_game_is_no_longer_join_now():
    clock = Clock()
    client = FakeClient([sessions(rows=[row("game-1")]), sessions()], join_answers=[session_join_failed()])
    state, _, _, _ = tick(client, now=clock.now)

    clock.value += 60
    tick(client, state, now=clock.now)
    tick(client, state, now=clock.now)

    assert client.joins == ["game-1"]
    assert state.join_retry_at == {}


def test_auto_unexplained_join_failure_turns_into_a_refusal_once_the_window_closes():
    clock = Clock()
    client = FakeClient([sessions(rows=[row("game-1")])], join_answers=[
        session_join_failed(), MCPToolError("Too late.", status_code=None, error_code="join_deadline_passed")])
    state, _, _, _ = tick(client, now=clock.now)
    clock.value += 10
    tick(client, state, now=clock.now)
    clock.value += 10
    tick(client, state, now=clock.now)

    assert client.joins == ["game-1", "game-1"]
    assert state.refused == {"game-1": "join_deadline_passed"}


def test_auto_known_final_join_failure_is_still_a_refusal():
    client = FakeClient([sessions(rows=[row("game-1")])], join_answers=[session_join_failed("Competition is full")])
    state, _, logs, _ = tick(client)
    tick(client, state, now=lambda: 100.0)

    assert client.joins == ["game-1"]
    assert state.refused == {"game-1": "SESSION_JOIN_FAILED"}
    assert logs[-1] == "Could not join game game-1: Competition is full"


def test_auto_token_rejected_inside_the_join_is_retried_not_refused():
    clock = Clock()
    client = FakeClient([sessions(rows=[row("game-1")])], join_answers=[
        MCPToolError("Invalid or expired token", status_code=None, error_code="Invalid or expired token"),
        {"status": "waiting"}])
    state, _, _, _ = tick(client, now=clock.now)
    clock.value += 10
    tick(client, state, now=clock.now)

    assert state.refused == {} and client.joins == ["game-1", "game-1"]


def test_auto_transient_join_failure_is_retried_next_tick():
    client = FakeClient([sessions(rows=[row("game-1")])], join_answers=[network_down(), {"status": "in_progress"}])
    state, _, logs, _ = tick(client)
    _, _, logs2, _ = tick(client, state)

    assert client.joins == ["game-1", "game-1"]
    assert "trying again in a few seconds" in logs[-1]
    assert logs2[1] == "Joined game game-1; it has started."


def test_auto_plays_a_game_its_own_join_filled_in_the_same_tick():
    client = FakeClient([sessions(rows=[row("game-1")]), sessions(rows=[row("game-1", status="in_progress")],
                                                                  active=[comp("game-1")])],
                        join_answers=[{"status": "in_progress"}])
    state, factory, logs, _ = tick(client)
    tick(client, state, factory=factory)

    assert [p.args[0].session_id for p in factory.processes] == ["game-1"]  # once, not again next tick
    assert factory.processes[0].args[0].tournament_id == T1
    assert logs[-1].startswith("Game game-1 (pokemon_vgc_doubles_draft) has started; playing it")


def test_auto_starts_one_worker_per_running_tournament_game_with_the_agent_spec():
    client = FakeClient([sessions(active=[comp("game-1"), comp("casual", tournament_id=None)])])
    state, factory, logs, _ = tick(client)
    tick(client, state, factory=factory)

    assert len(factory.processes) == 1
    worker_input = factory.processes[0].args[0]
    assert (worker_input.session_id, worker_input.tournament_id, worker_input.agent_id, worker_input.agent_spec) == (
        "game-1", T1, AGENT_ID, "examples.llm_agent")
    assert logs[-1].startswith("Game game-1 (pokemon_vgc_doubles_draft) has started; playing it")


def test_auto_failed_worker_is_retried_after_the_cooldown():
    clock = Clock()
    client = FakeClient([sessions(active=[comp("game-1")])])
    state, factory, _, _ = tick(client, now=clock.now)
    factory.processes[0].finish(EXIT_MATCH_FAILURE)

    _, _, logs, _ = tick(client, state, factory=factory, now=clock.now)
    assert len(factory.processes) == 1 and "stopped with an error" in logs[0]

    clock.value += 10
    tick(client, state, factory=factory, now=clock.now)
    assert len(factory.processes) == 2


def test_auto_reports_each_finished_game_once():
    done = comp("game-1", status="completed", winner_agent_ids=[AGENT_ID], results={AGENT_ID: 1})
    client = FakeClient([sessions(active=[comp("game-1")]), sessions(completed=[done])])
    state, factory, _, _ = tick(client)
    factory.processes[0].finish(EXIT_SUCCESS)

    _, _, logs, _ = tick(client, state, factory=factory)
    _, _, logs2, _ = tick(client, state, factory=factory)

    assert logs == ["Game game-1 is over. You won."]
    assert logs2 == []


def test_auto_does_not_report_a_game_whose_worker_is_still_running():
    done = comp("game-1", status="completed", winner_agent_ids=[AGENT_ID], results={AGENT_ID: 1})
    client = FakeClient([sessions(active=[comp("game-1")]), sessions(completed=[done])])
    state, factory, _, _ = tick(client)

    _, _, logs, _ = tick(client, state, factory=factory)

    assert logs == []


def test_auto_ignores_finished_games_it_never_saw():
    done = comp("old-game", status="completed", winner_agent_ids=[AGENT_ID], results={AGENT_ID: 1})
    _, _, logs, _ = tick(FakeClient([sessions(completed=[done])]))

    assert logs == []


def test_auto_transient_discovery_failure_skips_the_tick_and_logs_once():
    client = FakeClient([network_down(), network_down(), sessions()])
    state, _, logs, result = tick(client)
    _, _, logs2, _ = tick(client, state)
    _, _, logs3, _ = tick(client, state)

    assert result is None
    assert len(logs) == 1 and logs2 == [] and logs3 == ["Connection recovered."]


def test_auto_rejected_key_gives_up_after_repeated_failures():
    client = FakeClient([AuthenticationError("Invalid API key", status_code=401)])
    state, _, _, _ = tick(client)
    tick(client, state)

    with pytest.raises(AuthenticationError):
        tick(client, state)


def login_unavailable():
    return AuthenticationError("Failed to create session", status_code=401, error_code="Failed to create session",
                               transient=True)


def test_auto_login_the_platform_could_not_complete_backs_off_and_never_gives_up():
    """'Failed to create session' (the auth service's sign-in limit, or an
    outage) is not a bad key: the loop waits, doubling the delay up to 2
    minutes, and makes no request at all in between."""
    clock = Clock()
    client = FakeClient([login_unavailable()] * 7 + [sessions(rows=[row("game-1")])])
    state, logs = AutoJoinState(), []
    attempts_at = []
    for _ in range(200):
        before = client.sessions_calls
        run_autojoin_once(client, state, agent_id=AGENT_ID, now=clock.now, process_factory=FakeProcessFactory(),
                          log=logs.append)
        if client.sessions_calls != before:
            attempts_at.append(clock.value)
        if client.joins:
            break
        clock.sleep(5.0)

    assert [b - a for a, b in zip(attempts_at, attempts_at[1:])] == [15, 30, 60, 120, 120, 120, 120]
    assert client.joins == ["game-1"]
    assert sum("Could not log in (Failed to create session)" in line for line in logs) == 7
    assert "Connection recovered." in logs
    assert state.login_backoff == 0.0


def test_auto_login_failure_while_joining_backs_off_and_skips_the_other_joins():
    clock = Clock()
    client = FakeClient([sessions(rows=[row("game-1"), row("game-2")])],
                        join_answers=[login_unavailable(), {"status": "waiting"}])
    state, _, logs, _ = tick(client, now=clock.now)

    assert client.joins == ["game-1"] and state.refused == {}
    assert "Trying again in 15s." in logs[-1]

    clock.value += 10
    tick(client, state, now=clock.now)
    assert client.sessions_calls == 1  # still backing off

    clock.value += 5
    tick(client, state, now=clock.now)
    assert client.joins == ["game-1", "game-1", "game-2"]


def test_auto_hands_its_token_to_each_game_worker_without_showing_it():
    client = FakeClient([sessions(active=[comp("game-1")])])
    client.cached_access_token = lambda: "jwt-parent"

    _, factory, _, _ = tick(client)

    worker_input = factory.processes[0].args[0]
    assert worker_input.access_token == "jwt-parent"
    assert "jwt-parent" not in repr(worker_input)


def test_auto_with_the_real_client_survives_a_login_outage():
    """The real AltruAgentClient: the token expires, and every re-login
    answers 401 'Failed to create session' for a while. The loop keeps
    going (it used to stop for good after 3 ticks, ~15 s) and logs in at
    most once per backoff step, then carries on once logins work again."""
    calls = {"logins": 0, "sessions": 0}
    clock = Clock()
    outage = (10.0, 400.0)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/agent/login":
            calls["logins"] += 1
            if outage[0] <= clock.value < outage[1]:
                return httpx.Response(401, json={"error": "Failed to create session"})
            return httpx.Response(200, json={"access_token": f"jwt-{calls['logins']}"})
        if request.url.path == "/agents/me/sessions":
            calls["sessions"] += 1
            if request.headers["Authorization"] == "Bearer jwt-1" and clock.value >= outage[0]:
                return httpx.Response(401, json={"error": "Invalid or expired token"})
            return httpx.Response(200, json={"joined_sessions": [], "active_sessions": [], "completed_sessions": [],
                                             "tournament_matches": []})
        raise AssertionError(f"unexpected request {request.url.path}")

    client = AltruAgentClient(control_url="https://control.example.test", api_key="sk_agent_test",
                              load_env_file=False, transport=httpx.MockTransport(handler))
    logs = []

    assert run_autojoin_forever(client, agent_id=AGENT_ID, sleep=clock.sleep, now=clock.now,
                                process_factory=FakeProcessFactory(), log=logs.append, max_iterations=120) is None

    # 15 + 30 + 60 + 120 + 120 s of backoff cover the 390 s outage: 6 failed
    # logins at most, instead of one every 5 s.
    assert 2 <= calls["logins"] <= 8
    assert "Connection recovered." in logs
    assert client.cached_access_token() not in (None, "jwt-1")


# -- --tournament-auto: run_autojoin_forever --------------------------------------------------------


def detail(status):
    return TournamentDetail.from_dict({"tournament_id": T1, "name": "Autumn Cup", "status": status,
                                       "champion": {"agent_id": AGENT_ID, "agent_name": "Alpha"},
                                       "final_ranking": [{"rank": 1, "agent_id": AGENT_ID, "agent_name": "Alpha", "points": 3}]})


def test_forever_returns_once_the_tournament_is_over():
    clock = Clock()
    client = FakeClient([sessions(rows=[row(status="joined_waiting")]), sessions(), sessions()],
                        tournaments=[detail("in_progress"), detail("completed")])

    finished = run_autojoin_forever(client, agent_id=AGENT_ID, tournament_id=T1, sleep=clock.sleep, now=clock.now,
                                    process_factory=FakeProcessFactory(), log=lambda m: None, max_iterations=50)

    assert finished.status == "completed"
    # No status read while the agent had an open game; then at most every 15 s.
    assert client.tournament_calls == 2
    assert client.sessions_calls == 5


def test_forever_returns_a_cancelled_tournament_too():
    client = FakeClient([sessions()], tournaments=[detail("cancelled")])

    finished = run_autojoin_forever(client, agent_id=AGENT_ID, tournament_id=T1, sleep=lambda s: None,
                                    process_factory=FakeProcessFactory(), log=lambda m: None, max_iterations=5)

    assert finished.status == "cancelled"


def test_forever_without_a_tournament_id_never_reads_a_tournament():
    client = FakeClient([sessions()], tournaments=[AssertionError("read a tournament")])

    assert run_autojoin_forever(client, agent_id=AGENT_ID, sleep=lambda s: None,
                                process_factory=FakeProcessFactory(), log=lambda m: None, max_iterations=5) is None
    assert client.sessions_calls == 5


def test_forever_stops_lingering_workers_after_the_grace_period():
    clock, logs = Clock(), []
    factory = FakeProcessFactory()
    client = FakeClient([sessions(active=[comp("game-1", tournament_id=T1)]), sessions()],
                        tournaments=[detail("completed")])

    finished = run_autojoin_forever(client, agent_id=AGENT_ID, tournament_id=T1, sleep=clock.sleep, now=clock.now,
                                    process_factory=factory, log=logs.append, max_iterations=100)

    assert finished.status == "completed"
    assert factory.processes[0].terminated is True
    assert any(line.startswith("The tournament is over; stopping 1 game worker(s)") for line in logs)


def test_forever_terminates_workers_on_ctrl_c():
    factory = FakeProcessFactory()
    client = FakeClient([sessions(active=[comp("game-1")]), KeyboardInterrupt()])

    with pytest.raises(KeyboardInterrupt):
        run_autojoin_forever(client, agent_id=AGENT_ID, sleep=lambda s: None, process_factory=factory,
                             log=lambda m: None)

    assert factory.processes[0].terminated is True


def test_describe_final_standing():
    assert describe_final_standing(detail("completed"), AGENT_ID) == [
        'Tournament "Autumn Cup" is complete.', "Champion: Alpha.", "Your agent finished #1 of 1 (3 Swiss point(s))."]
    assert describe_final_standing(detail("cancelled"), "someone-else")[0] == 'Tournament "Autumn Cup" was cancelled.'
