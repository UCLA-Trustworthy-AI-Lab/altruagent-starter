"""Tests for the official tournament runtime: the supervisor loop
(``altruagent.supervisor.run_tournament_once``/``run_tournament_forever``)
with fake processes, and the per-seat worker
(``altruagent.worker.run_tournament_worker``) with a fake official client and
a recording ``run_game``. No real processes, network, or GameAPI.
"""

from __future__ import annotations

import sys
import textwrap
import types

import pytest

from altruagent.errors import AuthenticationError, PlatformError
from altruagent.mcp_game import MCPGameSession
from altruagent.models import OfficialAssignment, SeatGrant
from altruagent.official import OfficialAgentError
from altruagent.supervisor import (
    MISSING_POLLS_BEFORE_STOP,
    WAITING_MESSAGE,
    TournamentState,
    run_tournament_forever,
    run_tournament_once,
)
from altruagent.worker import (
    EXIT_MATCH_FAILURE,
    EXIT_SUCCESS,
    EXIT_UNEXPECTED,
    TournamentWorkerInput,
    _tournament_process_entry,
    run_tournament_worker,
)
from test_supervisor import FakeProcessFactory

SPEC = "agent.agent:create_agent"
EXEC = "exec-" + "q" * 40


def a(seat_id, match_id=None, game_type="pokemon_vgc_doubles_draft"):
    return OfficialAssignment(match_id=match_id or f"match-{seat_id}", seat_id=seat_id, game_type=game_type,
                              seat_position=0, seat_count=2, match_status="in_progress", seat_status="running")


class FakeOfficial:
    """Returns scripted assignment listings (a list, or an exception)."""

    def __init__(self, *listings):
        self.listings = list(listings)
        self.calls = 0

    def assignments(self):
        self.calls += 1
        listing = self.listings.pop(0) if len(self.listings) > 1 else self.listings[0]
        if isinstance(listing, Exception):
            raise listing
        return listing


class Harness:
    def __init__(self, *listings, cooldown=60.0):
        self.official = FakeOfficial(*listings)
        self.state = TournamentState()
        self.factory = FakeProcessFactory()
        self.log: list[str] = []
        self.clock = {"t": 0.0}
        self.cooldown = cooldown

    def tick(self, advance=1.0):
        self.clock["t"] += advance
        run_tournament_once(
            self.official, self.state, agent_spec=SPEC, now=lambda: self.clock["t"],
            cooldown_seconds=self.cooldown, process_factory=self.factory, log=self.log.append,
        )

    def started(self):
        return [p.args[0].seat_id for p in self.factory.processes]


# -- supervisor --------------------------------------------------------------------------


def test_no_assignments_starts_nothing_and_keeps_waiting():
    h = Harness([])

    for _ in range(5):
        h.tick()

    assert h.factory.processes == [] and h.official.calls == 5 and h.log == []


def test_one_assignment_starts_one_worker_with_primitive_input():
    h = Harness([a("seat-1")])

    h.tick()

    (process,) = h.factory.processes
    assert process.target is _tournament_process_entry and process.daemon is True
    assert process.args == (TournamentWorkerInput("seat-1", "match-seat-1", "pokemon_vgc_doubles_draft", SPEC,
                                                  h.state.execution_id),)
    assert h.log == ["Match assigned: pokemon_vgc_doubles_draft", "Starting match..."]


def test_multiple_simultaneous_assignments_each_get_a_worker():
    h = Harness([a("seat-1"), a("seat-2", game_type="werewolf"), a("seat-3", match_id="match-1")])

    h.tick()

    assert h.started() == ["seat-1", "seat-2", "seat-3"]
    assert h.state.registry.pids().keys() == {"seat-1", "seat-2", "seat-3"}


def test_one_execution_id_per_runtime_shared_by_all_its_workers():
    h = Harness([a("seat-1"), a("seat-2", game_type="werewolf"), a("seat-3")])

    h.tick()

    ids = {p.args[0].execution_id for p in h.factory.processes}
    assert ids == {h.state.execution_id}
    assert 16 <= len(h.state.execution_id) <= 256
    assert h.state.execution_id not in repr(h.factory.processes[0].args[0])  # kept out of logs


def test_each_runtime_gets_a_different_execution_id():
    assert len({TournamentState().execution_id for _ in range(20)}) == 20


def test_execution_id_stays_the_same_across_ticks_and_restarted_workers():
    h = Harness([a("seat-1")], cooldown=1.0)
    h.tick()
    first = h.state.execution_id
    h.factory.processes[0].finish(EXIT_UNEXPECTED)

    h.tick(advance=5.0)
    h.tick(advance=5.0)

    assert h.state.execution_id == first
    assert [p.args[0].execution_id for p in h.factory.processes] == [first, first]


def test_no_duplicate_worker_for_a_seat_already_being_played():
    h = Harness([a("seat-1")])

    for _ in range(4):
        h.tick()

    assert h.started() == ["seat-1"]


def test_finished_match_is_reaped_logged_and_not_restarted_while_it_lingers():
    h = Harness([a("seat-1")], [a("seat-1")], [])
    h.tick()
    h.factory.processes[0].finish(EXIT_SUCCESS)

    h.tick()  # reaped; the seat is still listed for a moment
    h.tick()

    assert h.log[-2:] == ["Match finished.", WAITING_MESSAGE]
    assert h.started() == ["seat-1"]


def test_a_new_assignment_after_a_finish_starts_immediately():
    h = Harness([a("seat-1")], [a("seat-2")])
    h.tick()
    h.factory.processes[0].finish(EXIT_SUCCESS)

    h.tick()

    assert h.started() == ["seat-1", "seat-2"]


def test_failed_worker_is_retried_after_cooldown_reconnect_path():
    h = Harness([a("seat-1")], cooldown=30.0)
    h.tick()
    h.factory.processes[0].finish(EXIT_UNEXPECTED)

    h.tick(advance=1.0)   # reaped -> cooldown
    h.tick(advance=10.0)  # still cooling down
    assert h.started() == ["seat-1"]
    assert "retrying that seat" in h.log[2]

    h.tick(advance=30.0)  # cooldown over, still assigned -> new worker (re-grant)
    assert h.started() == ["seat-1", "seat-1"]


def test_disappeared_assignment_stops_its_worker_after_grace_polls():
    h = Harness([a("seat-1"), a("seat-2")], [a("seat-2")])
    h.tick()
    seat_1 = h.factory.processes[0]

    for _ in range(MISSING_POLLS_BEFORE_STOP - 1):
        h.tick()
    assert not seat_1.terminated

    h.tick()
    assert seat_1.terminated
    assert h.state.registry.pids().keys() == {"seat-2"}
    assert "Match is no longer assigned; stopped its worker." in h.log


def test_reappearing_seat_resets_the_missing_count():
    h = Harness([a("seat-1")], [], [a("seat-1")], [], [a("seat-1")])

    for _ in range(5):
        h.tick()

    assert not h.factory.processes[0].terminated


def test_transient_discovery_failure_is_logged_once_and_recovers():
    h = Harness(PlatformError("boom", status_code=503), PlatformError("boom", status_code=503), [a("seat-1")])

    h.tick()
    h.tick()
    h.tick()

    assert sum("Could not check tournament assignments" in line for line in h.log) == 1
    assert "Assignment discovery recovered." in h.log
    assert h.started() == ["seat-1"]


def test_authentication_failure_propagates():
    h = Harness(OfficialAgentError("The Official Agent Key was not accepted.", status_code=401))

    with pytest.raises(AuthenticationError):
        h.tick()


def test_forever_keeps_polling_with_no_assignments_and_stops_workers_on_ctrl_c():
    official = FakeOfficial([a("seat-1"), a("seat-2")])
    factory = FakeProcessFactory()
    sleeps = []
    log: list[str] = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_tournament_forever(official, agent_spec=SPEC, sleep=sleep, process_factory=factory, log=log.append)

    assert official.calls == 3
    assert all(p.terminated for p in factory.processes)
    assert log[-1] == "Stopping 2 active match worker(s)..."


def test_forever_never_exits_on_its_own_when_nothing_is_assigned():
    official = FakeOfficial([])

    run_tournament_forever(official, agent_spec=SPEC, sleep=lambda s: None, max_iterations=25)

    assert official.calls == 25


# -- worker ----------------------------------------------------------------------------------


def _grant(**overrides):
    data = {"access_token": "seat-jwt", "agent_id": "synthetic-1", "gameapi_server_url": "https://gameapi.example.test",
            "game_session_id": "game-1", "match_id": "match-1", "seat_id": "seat-1", "seat_position": 1,
            "seat_count": 2, "game_type": "pokemon_vgc_doubles_draft", "match_status": "in_progress"}
    data.update(overrides)
    return SeatGrant.from_dict(data)


class FakeWorkerOfficial:
    instances: list["FakeWorkerOfficial"] = []

    def __init__(self, grant_result=None):
        self.control_url = "https://control.example.test"
        self.grant_result = grant_result if grant_result is not None else _grant()
        self.grants: list[str] = []
        self.execution_ids: list[str] = []
        self.renewals: list[tuple[str, str]] = []
        self.closed = False
        FakeWorkerOfficial.instances.append(self)

    def grant(self, seat_id, execution_id):
        self.execution_ids.append(execution_id)
        self.grants.append(seat_id)
        if isinstance(self.grant_result, Exception):
            raise self.grant_result
        return self.grant_result

    def renew_lease(self, seat_id, execution_id):
        self.renewals.append((seat_id, execution_id))
        return {"seat_id": seat_id, "execution_lease_expires_at": "x"}

    def close(self):
        self.closed = True


def _worker(spec=SPEC, grant_result=None, run_game_fn=None):
    runs = []

    def fake_run_game(game, context, contestant):
        runs.append((game, context, contestant))
        return types.SimpleNamespace(termination_reason="normal", returns={"synthetic-1": 1.0})

    officials = []

    def factory():
        officials.append(FakeWorkerOfficial(grant_result))
        return officials[-1]

    code = run_tournament_worker(TournamentWorkerInput("seat-1", "match-1", "pokemon_vgc_doubles_draft", spec, EXEC),
                                 official_factory=factory, run_game_fn=run_game_fn or fake_run_game)
    return code, runs, officials


def _write_module(tmp_path, monkeypatch, name, body):
    (tmp_path / f"{name}.py").write_text(textwrap.dedent(body), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, name, raising=False)


def test_worker_grants_the_seat_and_plays_it_through_run_game(capsys):
    code, runs, officials = _worker()

    assert code == EXIT_SUCCESS
    (game, context, contestant), = runs
    assert isinstance(game, MCPGameSession)
    assert (game.session_id, game.game_server_url) == ("game-1", "https://gameapi.example.test")
    assert game._client.auth.seat_id == "seat-1"
    assert (context.session_id, context.agent_id, context.seat_position, context.game_type) == (
        "game-1", "synthetic-1", 1, "pokemon_vgc_doubles_draft")
    assert officials[0].grants == ["seat-1"] and officials[0].closed
    out = capsys.readouterr().out
    assert "finished termination_reason=normal score=1.0" in out
    assert "seat-jwt" not in out


def test_each_worker_builds_its_own_contestant(monkeypatch, tmp_path):
    _write_module(tmp_path, monkeypatch, "tournament_counting_agent", """
        created = []

        class Agent:
            def choose_action(self, state, context):
                return state.legal_actions[0]

        def create_agent():
            created.append(Agent())
            return created[-1]
    """)

    _, runs_a, _ = _worker(spec="tournament_counting_agent")
    _, runs_b, _ = _worker(spec="tournament_counting_agent")

    module = sys.modules["tournament_counting_agent"]
    assert len(module.created) == 2
    assert runs_a[0][2] is module.created[0] and runs_b[0][2] is module.created[1]


def test_worker_agent_override_spec():
    _, runs, _ = _worker(spec="examples.smoke_agent")

    assert runs[0][2].__module__ == "examples.smoke_agent"


def test_worker_restart_requests_a_fresh_grant_each_time():
    _, _, first = _worker()
    _, _, second = _worker()

    assert first[0].grants == ["seat-1"] and second[0].grants == ["seat-1"]
    assert first[0] is not second[0]


def test_worker_exits_cleanly_when_the_assignment_already_ended():
    code, runs, _ = _worker(grant_result=OfficialAgentError("ended", status_code=409, error_code="assignment_not_grantable"))

    assert code == EXIT_SUCCESS and runs == []


def test_worker_factory_error_fails_before_any_grant(monkeypatch, tmp_path):
    _write_module(tmp_path, monkeypatch, "tournament_broken_agent", "def create_agent():\n    raise RuntimeError('no key')\n")

    code, runs, officials = _worker(spec="tournament_broken_agent")

    assert code == EXIT_UNEXPECTED and runs == [] and officials == []


def test_worker_match_failure_exit_code():
    def failing(game, context, contestant):
        raise PlatformError("GameAPI error", status_code=500)

    code, _, officials = _worker(run_game_fn=failing)

    assert code == EXIT_MATCH_FAILURE and officials[0].closed


def test_worker_ctrl_c_exits_quietly():
    def interrupted(game, context, contestant):
        raise KeyboardInterrupt

    code, _, _ = _worker(run_game_fn=interrupted)

    assert code == EXIT_SUCCESS
