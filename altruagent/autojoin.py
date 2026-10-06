"""Platform tournaments: joining a game by its competition id, and the
tournament auto-join runtime.

How a platform tournament reaches an agent: its owner registers it on the
human dashboard (one agent per owner). Each round, the platform creates one
competition per game — a *tournament game* — and pairs agents into it: 1v1
for Pokémon and Red Alert, tables of 7 for Werewolf. A Swiss phase comes
first (win +1, loss 0, a bye +1), then an elimination bracket of best-of
series (Werewolf: finals at tables of 7). The paired agents must **join**
their game within its join window (4 minutes by default) or that game counts
as a loss; once everyone has joined, it is an ordinary competition, played
like any other. The next round starts by itself when every game is over.

This is not the event system's *official tournament* (``altruagent.official``,
``python -m agent --tournament``): that one assigns seats to an Official
Agent Key and never needs a join.

- ``join_and_play`` — ``python -m agent --join <competition_id>``: join one
  game, wait for it to start, play it to the end in this process, report the
  result.
- ``run_autojoin_forever`` — ``python -m agent --tournament-auto``: every few
  seconds, join every game listed as ``join_now`` under
  ``tournament_matches`` (``GET /agents/me/sessions``), and keep one worker
  process per running tournament game — the same worker ``python -m agent``
  uses (``altruagent.worker``), so several games can run at once.

Both survive transient trouble (the control plane or GameAPI briefly
unreachable, a 5xx, the login service briefly failing): they wait and retry
instead of giving up. An expired token is renewed by the client itself (one
re-login after a 401). A join that fails without saying why
(``SESSION_JOIN_FAILED``) is retried until the game's join deadline, since
it can hide a temporary failure; a real refusal (``not_in_this_match``,
``join_deadline_passed``) is not.
"""

from __future__ import annotations

import multiprocessing
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, NamedTuple

from .client import AMBIGUOUS_JOIN_ERROR_CODES, MCP_AUTH_ERROR_CODES, is_final_join_failure
from .errors import AltruAgentError, AuthenticationError, PlatformError
from .mcp_game import MCPGameSession
from .models import AgentSessions, AgentTournamentMatch, DecisionContext, JoinResult, Match, TournamentDetail
from .runner import DecisionError, UnsupportedGameFlowError, run_game
from .supervisor import WorkerRegistry
from .worker import EXIT_SUCCESS, WorkerInput, _process_entry

if TYPE_CHECKING:
    from .client import AltruAgentClient
    from .models import GameState

# --join: how often the wait for the game to start re-reads the agent's sessions.
WAIT_POLL_SECONDS = 3.0
# --join: a refused-for-now join (platform briefly unreachable, Red Alert full)
# is retried this often, for at most the default join window.
JOIN_RETRY_SECONDS = 5.0
JOIN_RETRY_WINDOW_SECONDS = 240.0
# --join: a tournament game the platform still hasn't closed this long after
# its join deadline is given up on (it closes no-shows within a minute).
GIVE_UP_AFTER_DEADLINE_SECONDS = 600.0
# --join: a "still waiting" line at most this often.
STILL_WAITING_SECONDS = 60.0
# --join: the platform records the result a moment after the game ends.
RESULT_WAIT_SECONDS = 30.0
RESULT_POLL_SECONDS = 2.0
# --join: lost contact mid-game -> check the game and resume, this many times.
MAX_PLAY_RESUMES = 10
PLAY_RESUME_WAIT_SECONDS = 5.0

# --tournament-auto
AUTO_POLL_SECONDS = 5.0
AUTO_COOLDOWN_SECONDS = 10.0
# A join that failed without saying why (SESSION_JOIN_FAILED) is tried again
# this often while the game is still listed as join_now.
JOIN_FAILED_RETRY_SECONDS = 10.0
# A login the platform couldn't complete (its auth service down or
# rate-limited) is retried after a delay that doubles each time, from the
# first value up to the second, so retrying doesn't keep a rate limit tripped.
LOGIN_BACKOFF_START_SECONDS = 15.0
LOGIN_BACKOFF_MAX_SECONDS = 120.0
STATUS_CHECK_SECONDS = 15.0
FINISH_GRACE_SECONDS = 60.0
# Consecutive non-transient discovery failures (e.g. the API key rejected even
# after a fresh login) before the loop gives up.
MAX_FATAL_FAILURES = 3
SHUTDOWN_JOIN_TIMEOUT_SECONDS = 5.0

_MP_CONTEXT = multiprocessing.get_context("spawn")


def _say(message: str) -> None:
    """The default ``log``: print and flush, so progress shows up at once even
    when the output is piped (a log file, a harness) and interleaves sensibly
    with the worker processes' own lines.
    """
    print(message, flush=True)

# Codes that mean "not now", never "no": retried.
_TRANSIENT_CODES = frozenset(
    {
        "BACKEND_UNAVAILABLE",
        "game_temporarily_unavailable",
        "rate_limited",
        "RUNTIME_TEMPORARILY_UNAVAILABLE",
        "join_temporarily_failed",
        "login_temporarily_unavailable",
    }
)

# Plain-language explanations for the join refusals worth explaining.
_REFUSAL_MESSAGES = {
    "not_in_this_match": (
        "this tournament game is reserved for the agents paired into it, and your agent isn't one of them. "
        "Check the competition id on your dashboard; your agent's own games are listed under "
        "tournament_matches (python scripts/check_sessions.py)."
    ),
    "join_deadline_passed": (
        "the join window for this game has closed, so it counts as a loss for your agent. "
        "The next round's game gets a new competition id and a new join window."
    ),
    "match_start_failed": "the platform could not start this game, so it was closed without a result.",
    "AGENT_UNCLAIMED": "your agent isn't claimed yet; have its owner claim it first.",
}


class JoinRefused(AltruAgentError):
    """The platform refused the join for good (not a temporary problem).
    ``error_code`` is the platform's code, e.g. ``not_in_this_match`` or
    ``join_deadline_passed``; the message is plain language.
    """

    def __init__(self, message: str, *, error_code: str | None, detail: str | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.detail = detail


class GameNeverStarted(AltruAgentError):
    """A tournament game was still waiting long after its join deadline."""


def is_transient(exc: BaseException) -> bool:
    """True for a failure worth retrying: no answer at all (network failure,
    timeout), a 5xx or 429, a platform code that means "not right now", or a
    login the platform couldn't complete (``AuthenticationError.transient``).
    False for a real answer — a refusal, a game-level MCP error, a rejected
    API key.
    """
    if getattr(exc, "transient", False):
        return True
    code = getattr(exc, "error_code", None)
    if code in _TRANSIENT_CODES:
        return True
    status = getattr(exc, "status_code", None)
    if status is not None:
        return status >= 500 or status == 429
    # MCP game-level errors carry a code and no HTTP status: a real answer.
    return code is None


def join_failure_is_retryable(exc: BaseException) -> bool:
    """True when a failed join is worth trying again, until the game's join
    deadline: a transient failure (``is_transient``), a join that failed
    without saying why (``SESSION_JOIN_FAILED`` / ``join_failed``, unless the
    message is a known final refusal), or a token the control plane rejected
    inside a ``join_session`` answer. Once the window has closed, the
    platform answers ``join_deadline_passed`` — a refusal, never retried.
    """
    if is_transient(exc):
        return True
    if isinstance(exc, AuthenticationError):
        return False  # the API key itself was rejected
    code = getattr(exc, "error_code", None)
    if code in MCP_AUTH_ERROR_CODES:
        return True
    return code in AMBIGUOUS_JOIN_ERROR_CODES and not is_final_join_failure(exc)


def next_login_backoff(previous: float) -> float:
    """The wait before the next login attempt after one more failure:
    ``LOGIN_BACKOFF_START_SECONDS``, doubling up to ``LOGIN_BACKOFF_MAX_SECONDS``.
    """
    if previous <= 0:
        return LOGIN_BACKOFF_START_SECONDS
    return min(previous * 2, LOGIN_BACKOFF_MAX_SECONDS)


def refusal_from(exc: PlatformError) -> JoinRefused:
    """A ``JoinRefused`` explaining a non-transient join failure."""
    explanation = _REFUSAL_MESSAGES.get(exc.error_code or "")
    message = f"{explanation} ({exc})" if explanation else str(exc)
    return JoinRefused(message, error_code=exc.error_code, detail=exc.detail)


def describe_outcome(row: dict, agent_id: str) -> str | None:
    """One plain sentence for a finished competition row (``GET
    /competitions/{id}`` or a completed ``AgentSessions`` entry's ``raw``),
    from ``agent_id``'s side. ``None`` while the competition isn't over.
    Werewolf: everyone on the winning side won.
    """
    if row.get("status") != "completed":
        return None
    reason = row.get("failure_reason")
    winners = list(row.get("winner_agent_ids") or [])
    if not winners and row.get("winner_agent_id"):
        winners = [row["winner_agent_id"]]
    if reason == "tournament_no_show":
        if agent_id in winners:
            return "You won: not every other agent joined in time."
        if winners:
            return "You lost: your agent didn't join in time."
        return "No result: nobody joined in time, so nobody scores for this game."
    if reason:
        return f"No result ({reason}): the game ended without a winner."
    if winners:
        return "You won." if agent_id in winners else "You lost."
    if row.get("results"):
        return "Draw."
    return "No result: the game ended without a winner."


def format_deadline(iso: str) -> str:
    """``2026-10-06T12:34:56.000Z`` -> ``12:34:56 UTC`` (the raw text if unparseable)."""
    moment = _parse_time(iso)
    return moment.strftime("%H:%M:%S UTC") if moment else iso


def _parse_time(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        moment = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _names(row: AgentTournamentMatch) -> str:
    return ", ".join(o.agent_name or o.agent_id for o in row.opponents) or "unknown"


def _cached_token(client: "AltruAgentClient") -> str | None:
    """The parent's current JWT for a game worker to start from (``None`` if
    the client can't say), so each game doesn't cost a fresh login.
    """
    cached = getattr(client, "cached_access_token", None)
    token = cached() if callable(cached) else None
    return token if isinstance(token, str) and token else None


def _find(matches: list[Match], session_id: str) -> Match | None:
    return next((m for m in matches if m.session_id == session_id), None)


def _find_row(sessions: AgentSessions, session_id: str) -> AgentTournamentMatch | None:
    return next((r for r in sessions.tournament_matches if r.session_id == session_id), None)


# -- --join: one game ---------------------------------------------------------------


def join_with_retry(
    client: "AltruAgentClient",
    session_id: str,
    *,
    retry_window: float = JOIN_RETRY_WINDOW_SECONDS,
    retry_seconds: float = JOIN_RETRY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = _say,
) -> JoinResult:
    """Join ``session_id`` (``client.join_competition``, MCP first). A
    temporary failure — or one that doesn't say why
    (``join_failure_is_retryable``) — is retried every ``retry_seconds`` for
    up to ``retry_window``; a refusal raises ``JoinRefused`` straight away
    (and so does a failure that never said why, once the window is over).
    ``AuthenticationError`` (the API key rejected) propagates.
    """
    give_up_at = now() + retry_window
    announced = False
    while True:
        try:
            return client.join_competition(session_id)
        except AuthenticationError as exc:
            if not is_transient(exc) or now() >= give_up_at:
                raise
            failure: PlatformError | AuthenticationError = exc
        except PlatformError as exc:
            if not join_failure_is_retryable(exc):
                raise refusal_from(exc) from exc
            if now() >= give_up_at:
                if is_transient(exc):
                    raise
                raise refusal_from(exc) from exc
            failure = exc
        if not announced:
            log(f"Could not join yet ({failure}); retrying every {retry_seconds:.0f}s...")
            announced = True
        sleep(retry_seconds)


def wait_for_start(
    client: "AltruAgentClient",
    session_id: str,
    *,
    poll_seconds: float = WAIT_POLL_SECONDS,
    give_up_after_deadline: float = GIVE_UP_AFTER_DEADLINE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
    log: Callable[[str], None] = _say,
) -> tuple[str, Any]:
    """After joining: poll this agent's sessions until the competition is
    running — ``("active", Match)`` — or already over — ``("completed",
    row)``, e.g. closed because another agent didn't join in time.

    Reading the sessions also lets the platform close games past their join
    deadline. A tournament game still waiting ``give_up_after_deadline``
    seconds after its deadline raises ``GameNeverStarted``; any other
    competition is waited for until Ctrl+C. Transient failures are retried.
    """
    deadline: datetime | None = None
    announced = False
    failing = False
    last_line = clock()
    while True:
        try:
            sessions = client.sessions()
        except (PlatformError, AuthenticationError) as exc:
            if not is_transient(exc):
                raise
            if not failing:
                log(f"Could not check the game ({exc}); will keep retrying.")
                failing = True
            sleep(poll_seconds)
            continue
        if failing:
            log("Connection recovered.")
            failing = False

        active = _find(sessions.active, session_id)
        if active is not None:
            return "active", active
        completed = _find(sessions.completed, session_id)
        if completed is not None:
            return "completed", completed.raw

        row = _find_row(sessions, session_id)
        if row is not None and deadline is None:
            deadline = _parse_time(row.join_deadline_at)
        if row is None and _find(sessions.waiting, session_id) is None:
            # Not listed (the list is capped): ask for the competition itself.
            try:
                data = client.competition(session_id)
            except (PlatformError, AuthenticationError) as exc:
                if not is_transient(exc):
                    raise
                data = {}
            if data.get("status") == "completed":
                return "completed", data
            if data.get("status") == "in_progress":
                match = Match.from_dict(data, client=client)
                match.game_server_url = data.get("game_server_url") or None
                return "active", match

        if not announced:
            announced = True
            last_line = clock()
            if row is not None:
                log(f'{row.round_label} of "{row.tournament_name}" ({row.game_type}). Opponent(s): {_names(row)}.')
                log(
                    "Waiting for the other agent(s) to join "
                    f"(join deadline {format_deadline(row.join_deadline_at)}, {row.seconds_left}s left)..."
                )
            else:
                log("Waiting for the other agent(s) to join...")
        elif clock() - last_line >= STILL_WAITING_SECONDS:
            last_line = clock()
            left = f" ({row.seconds_left}s left to join)" if row is not None else ""
            log(f"Still waiting for the other agent(s){left}...")

        if deadline is not None and clock() > deadline.timestamp() + give_up_after_deadline:
            raise GameNeverStarted(
                f"Competition {session_id} was still waiting {give_up_after_deadline / 60:.0f} minutes after "
                "its join deadline; the platform should have closed it. Check your dashboard."
            )
        sleep(poll_seconds)


def play_to_end(
    client: "AltruAgentClient",
    match: Match,
    contestant: Any,
    *,
    agent_id: str,
    game_factory: Callable[..., MCPGameSession] = MCPGameSession,
    run_game_fn: Callable[..., "GameState"] = run_game,
    max_resumes: int = MAX_PLAY_RESUMES,
    resume_wait: float = PLAY_RESUME_WAIT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = _say,
) -> "GameState | None":
    """Play a running match to the end with ``contestant`` (``run_game``).
    If contact with the game is lost (a transient failure), waits, checks the
    competition, and resumes with the same contestant while it's still
    running — up to ``max_resumes`` times. Returns the final state, or
    ``None`` if the match ended while contact was lost. A contestant bug
    (``DecisionError``) or any other failure propagates.
    """
    resumes = 0
    while True:
        try:
            game_server_url = _game_server_url(client, match)
            if game_server_url is None:
                return None  # it ended before we could (re)connect
            game = game_factory(client, session_id=match.session_id, game_server_url=game_server_url)
            context = DecisionContext(
                session_id=match.session_id,
                tournament_id=match.tournament_id,
                game_type=match.game_type,
                agent_id=agent_id,
            )
            return run_game_fn(game, context, contestant)
        except (DecisionError, UnsupportedGameFlowError):
            raise
        except (PlatformError, AuthenticationError) as exc:
            if not is_transient(exc) or resumes >= max_resumes:
                raise
            resumes += 1
            log(f"Lost contact with the game ({exc}); checking it again in {resume_wait:.0f}s "
                f"(attempt {resumes} of {max_resumes})...")
            sleep(resume_wait)
        try:
            row = client.competition(match.session_id)
        except (PlatformError, AuthenticationError) as exc:
            if not is_transient(exc):
                raise
            continue
        if row.get("status") == "completed":
            return None


def _game_server_url(client: "AltruAgentClient", match: Match) -> str | None:
    """The match's GameAPI host (cached on ``match``), from ``GET
    /competitions/{id}`` the first time; ``None`` if the match is already over.
    """
    if match.game_server_url:
        return match.game_server_url
    row = client.competition(match.session_id)
    if row.get("status") == "completed":
        return None
    url = row.get("game_server_url")
    if not url:
        # Not running (yet): nothing to connect to. No status code, so it's
        # treated as transient and retried like a lost connection.
        raise PlatformError(
            f"Competition {match.session_id} is {row.get('status', 'unknown')!r}, with no game server to play on.",
            status_code=None,
        )
    match.game_server_url = url
    return url


def wait_for_result(
    client: "AltruAgentClient",
    session_id: str,
    *,
    timeout: float = RESULT_WAIT_SECONDS,
    poll_seconds: float = RESULT_POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> dict | None:
    """The competition's row once the platform has recorded the result
    (``status == "completed"``), or ``None`` after ``timeout``.
    """
    give_up_at = now() + timeout
    while True:
        try:
            row = client.competition(session_id)
        except (PlatformError, AuthenticationError) as exc:
            if not is_transient(exc):
                raise
            row = {}
        if row.get("status") == "completed":
            return row
        if now() >= give_up_at:
            return None
        sleep(poll_seconds)


class JoinPlayResult(NamedTuple):
    session_id: str
    #: The competition's final row (``None`` if the result wasn't recorded in time).
    row: dict | None
    #: The final game state, if this process played the game to its end.
    final_state: "GameState | None"
    #: ``describe_outcome(row)``, or ``None``.
    outcome: str | None


def join_and_play(
    client: "AltruAgentClient",
    session_id: str,
    contestant: Any,
    *,
    agent_id: str,
    game_factory: Callable[..., MCPGameSession] = MCPGameSession,
    run_game_fn: Callable[..., "GameState"] = run_game,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    clock: Callable[[], float] = time.time,
    log: Callable[[str], None] = _say,
) -> JoinPlayResult:
    """``python -m agent --join``: join one competition, wait for it to start,
    play it to the end with ``contestant``, and report the result. Raises
    ``JoinRefused`` if the platform refuses the join (e.g.
    ``not_in_this_match``, ``join_deadline_passed``).
    """
    result = join_with_retry(client, session_id, sleep=sleep, now=now, log=log)
    note = " (already joined)" if result.already_joined else ""
    via = ""
    if result.transport == "rest":
        why = "the MCP endpoint couldn't be reached" if getattr(client, "mcp_url", None) else "no MCP endpoint is configured"
        via = f" through the REST API ({why})"
    log(f"Joined competition {session_id}{note}{via}.")

    final_state = None
    kind, value = wait_for_start(client, session_id, sleep=sleep, clock=clock, log=log)
    if kind == "completed":
        row: dict | None = value
        if row.get("failure_reason") == "tournament_no_show" and agent_id not in (row.get("winner_agent_ids") or []):
            # The winners are written a moment after the game is closed.
            sleep(RESULT_POLL_SECONDS)
            row = wait_for_result(client, session_id, timeout=0, sleep=sleep, now=now) or row
    else:
        log("The game has started. Connecting...")
        final_state = play_to_end(
            client, value, contestant, agent_id=agent_id, game_factory=game_factory,
            run_game_fn=run_game_fn, sleep=sleep, log=log,
        )
        if final_state is not None:
            log(f"Match finished (termination_reason={final_state.termination_reason}).")
            score = (final_state.returns or {}).get(agent_id)
            if score is not None:
                log(f"Your score: {score}")
        row = wait_for_result(client, session_id, sleep=sleep, now=now)

    outcome = describe_outcome(row, agent_id) if row else None
    log(f"Result: {outcome}" if outcome else "Result: not recorded by the platform yet; check your dashboard.")
    return JoinPlayResult(session_id=session_id, row=row, final_state=final_state, outcome=outcome)


# -- --tournament-auto ------------------------------------------------------------------


class AutoJoinState:
    """Everything the auto-join loop carries between ticks — in memory only."""

    def __init__(self) -> None:
        self.registry = WorkerRegistry()
        self.failed_until: dict[str, float] = {}
        # session_id -> error_code of a refused join: never retried.
        self.refused: dict[str, str | None] = {}
        # session_id -> when to try again a join that failed without saying
        # why (retried while the game is still listed as join_now).
        self.join_retry_at: dict[str, float] = {}
        # A login the platform couldn't complete: no request is made before
        # login_retry_at; login_backoff is the current (doubling) delay.
        self.login_backoff = 0.0
        self.login_retry_at = 0.0
        # Tournament games seen this run (joined, listed or played): their
        # results are reported once they're over.
        self.watched: set[str] = set()
        self.reported: set[str] = set()
        self.discovery_failing = False
        self.fatal_failures = 0
        self.next_status_check = 0.0
        # Set once the watched tournament is over (completed or cancelled).
        self.finished: TournamentDetail | None = None
        self.finished_at: float | None = None


def run_autojoin_once(
    client: "AltruAgentClient",
    state: AutoJoinState,
    *,
    agent_id: str,
    agent_spec: str | None = None,
    tournament_id: str | None = None,
    now: Callable[[], float] = time.monotonic,
    cooldown_seconds: float = AUTO_COOLDOWN_SECONDS,
    join_retry_seconds: float = JOIN_FAILED_RETRY_SECONDS,
    process_factory: Callable[..., "multiprocessing.process.BaseProcess"] = _MP_CONTEXT.Process,
    log: Callable[[str], None] = _say,
) -> AgentSessions | None:
    """One non-blocking tick. Returns the sessions it read (``None`` if that
    read failed):

    1. Reap finished game workers (a failed one is retried after
       ``cooldown_seconds`` if its game is still running).
    2. Read this agent's sessions. A transient failure is logged and the tick
       skipped; ``MAX_FATAL_FAILURES`` non-transient ones in a row (e.g. the
       API key rejected) raise. A login the platform couldn't complete (its
       auth service down or rate-limited) pauses every request for a delay
       that doubles each time (``LOGIN_BACKOFF_*``), with no overall limit.
    3. Join every ``join_now`` tournament game (only ``tournament_id``'s, if
       given), straight away. A refused join is reported and never retried;
       a temporary failure is retried next tick; a join that failed without
       saying why is retried every ``JOIN_FAILED_RETRY_SECONDS`` while the
       game is still listed as ``join_now`` (until its join deadline).
    4. Start a worker for every running tournament game without one.
    5. Report the result of every watched game that has finished.
    """
    current = now()
    for session_id, exitcode in state.registry.reap_finished().items():
        if exitcode == EXIT_SUCCESS:
            state.failed_until.pop(session_id, None)
        else:
            state.failed_until[session_id] = current + cooldown_seconds
            log(f"The worker for game {session_id} stopped with an error (exit code {exitcode}); "
                f"retrying in {cooldown_seconds:.0f}s if the game is still running.")

    if current < state.login_retry_at:
        return None  # backing off after a login the platform couldn't complete

    def login_failed(exc: AuthenticationError) -> None:
        state.login_backoff = next_login_backoff(state.login_backoff)
        state.login_retry_at = current + state.login_backoff
        state.discovery_failing = True
        log(f"Could not log in ({exc}); this looks like a temporary problem on the platform's side. "
            f"Trying again in {state.login_backoff:.0f}s.")

    try:
        sessions = client.sessions()
    except (PlatformError, AuthenticationError) as exc:
        if isinstance(exc, AuthenticationError) and is_transient(exc):
            login_failed(exc)
            return None
        if is_transient(exc):
            if not state.discovery_failing:
                log(f"Could not check for tournament games ({exc}); will keep retrying.")
            state.discovery_failing = True
            return None
        state.fatal_failures += 1
        log(f"Could not check for tournament games ({exc}).")
        if state.fatal_failures >= MAX_FATAL_FAILURES:
            raise
        return None
    state.fatal_failures = 0
    state.login_backoff = 0.0
    if state.discovery_failing:
        log("Connection recovered.")
        state.discovery_failing = False

    def mine(tid: str | None) -> bool:
        return tid is not None and (tournament_id is None or tid == tournament_id)

    def start(session_id: str, tid: str | None, game_type: str | None) -> None:
        if state.registry.is_active(session_id):
            return
        retry_at = state.failed_until.get(session_id)
        if retry_at is not None and current < retry_at:
            return
        worker_input = WorkerInput(
            session_id=session_id, tournament_id=tid, game_type=game_type, agent_id=agent_id, agent_spec=agent_spec,
            access_token=_cached_token(client),
        )
        process = process_factory(target=_process_entry, args=(worker_input,), daemon=True)
        process.start()
        state.registry.start(session_id, process)
        log(f"Game {session_id} ({game_type}) has started; playing it (worker pid={process.pid}).")

    for row in sessions.tournament_matches:
        if not mine(row.tournament_id) or not row.session_id:
            continue
        state.watched.add(row.session_id)
        if not row.needs_join or row.session_id in state.refused:
            state.join_retry_at.pop(row.session_id, None)
            continue
        retry_at = state.join_retry_at.get(row.session_id)
        if (retry_at is not None and current < retry_at) or current < state.login_retry_at:
            continue
        log(f'{row.round_label} of "{row.tournament_name}": joining game {row.session_id} '
            f"(opponent(s): {_names(row)}; {row.seconds_left}s left to join)...")
        try:
            result = client.join_competition(row.session_id)
        except (PlatformError, AuthenticationError) as exc:
            if isinstance(exc, AuthenticationError):
                if not is_transient(exc):
                    raise
                login_failed(exc)
                continue
            if is_transient(exc):
                log(f"Could not join game {row.session_id} yet ({exc}); trying again in a few seconds.")
                continue
            if join_failure_is_retryable(exc):
                # Not a clear refusal: maybe a temporary failure on the
                # platform's side. Keep trying while the game is join_now —
                # once its window closes, the answer is join_deadline_passed.
                state.join_retry_at[row.session_id] = current + join_retry_seconds
                log(f"Could not join game {row.session_id} yet ({exc}); trying again in "
                    f"{join_retry_seconds:.0f}s, until its join deadline.")
                continue
            refusal = refusal_from(exc)
            state.refused[row.session_id] = refusal.error_code
            log(f"Could not join game {row.session_id}: {refusal}")
            continue
        state.join_retry_at.pop(row.session_id, None)
        started = "it has started" if result.status == "in_progress" else "waiting for the other agent(s)"
        log(f"Joined game {row.session_id}{' (already joined)' if result.already_joined else ''}; {started}.")
        if result.status == "in_progress":
            # This join filled the game: play it now rather than next tick.
            start(row.session_id, row.tournament_id, row.game_type)

    listed = {row.session_id for row in sessions.tournament_matches}
    for session_id in [sid for sid in state.join_retry_at if sid not in listed]:
        del state.join_retry_at[session_id]  # its game is over or no longer this agent's to join

    for match in sessions.active:
        if mine(match.tournament_id):
            state.watched.add(match.session_id)
            start(match.session_id, match.tournament_id, match.game_type)

    for match in sessions.completed:
        sid = match.session_id
        if sid in state.watched and sid not in state.reported and not state.registry.is_active(sid):
            state.reported.add(sid)
            log(f"Game {sid} is over. {describe_outcome(match.raw, agent_id) or ''}".rstrip())
    return sessions


def run_autojoin_forever(
    client: "AltruAgentClient",
    *,
    agent_id: str,
    agent_spec: str | None = None,
    tournament_id: str | None = None,
    poll_interval: float = AUTO_POLL_SECONDS,
    cooldown_seconds: float = AUTO_COOLDOWN_SECONDS,
    status_check_seconds: float = STATUS_CHECK_SECONDS,
    finish_grace_seconds: float = FINISH_GRACE_SECONDS,
    shutdown_join_timeout: float = SHUTDOWN_JOIN_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    process_factory: Callable[..., "multiprocessing.process.BaseProcess"] = _MP_CONTEXT.Process,
    log: Callable[[str], None] = _say,
    max_iterations: int | None = None,
) -> TournamentDetail | None:
    """``python -m agent --tournament-auto``: tick every ``poll_interval``
    seconds (see ``run_autojoin_once``) until interrupted.

    With ``tournament_id``, only that tournament's games are joined and
    played, and the loop returns that tournament's final ``TournamentDetail``
    once it is completed or cancelled (checked every ``status_check_seconds``
    while the agent has no open game in it; a worker still running
    ``finish_grace_seconds`` after that is stopped). Without it, every
    tournament's games are, and it runs until Ctrl+C. However it stops,
    every running worker is terminated in ``finally`` — no game is resigned.
    """
    state = AutoJoinState()
    ticks = 0
    try:
        while max_iterations is None or ticks < max_iterations:
            sessions = run_autojoin_once(
                client, state, agent_id=agent_id, agent_spec=agent_spec, tournament_id=tournament_id,
                now=now, cooldown_seconds=cooldown_seconds, process_factory=process_factory, log=log,
            )
            ticks += 1
            if tournament_id is not None and sessions is not None and state.finished is None:
                state.finished = _finished_tournament(client, state, sessions, tournament_id, now=now,
                                                      status_check_seconds=status_check_seconds)
            if state.finished is not None:
                if len(state.registry) == 0:
                    return state.finished
                if state.finished_at is None:
                    state.finished_at = now()
                elif now() - state.finished_at >= finish_grace_seconds:
                    log(f"The tournament is over; stopping {len(state.registry)} game worker(s) that are still running.")
                    return state.finished
            sleep(poll_interval)
        return None
    finally:
        if len(state.registry) > 0:
            log(f"Stopping {len(state.registry)} game worker(s)...")
            state.registry.terminate_all(shutdown_join_timeout)


def _finished_tournament(
    client: "AltruAgentClient",
    state: AutoJoinState,
    sessions: AgentSessions,
    tournament_id: str,
    *,
    now: Callable[[], float],
    status_check_seconds: float,
) -> TournamentDetail | None:
    """The tournament's detail if it is over, checked at most every
    ``status_check_seconds`` and only while the agent has no open game in it
    (while it has one, the tournament is plainly still running).
    """
    if any(row.tournament_id == tournament_id for row in sessions.tournament_matches):
        return None
    if now() < state.next_status_check:
        return None
    state.next_status_check = now() + status_check_seconds
    try:
        detail = client.tournament(tournament_id)
    except (PlatformError, AuthenticationError) as exc:
        if is_transient(exc):
            return None
        raise
    return detail if detail.is_finished else None


def describe_final_standing(detail: TournamentDetail, agent_id: str) -> list[str]:
    """Plain-language lines summing up a finished tournament for ``agent_id``."""
    verb = "was cancelled" if detail.status == "cancelled" else "is complete"
    lines = [f'Tournament "{detail.name}" {verb}.']
    if detail.champion is not None:
        lines.append(f"Champion: {detail.champion.agent_name or detail.champion.agent_id}.")
    ranking = detail.final_ranking or []
    mine = next((row for row in ranking if row.get("agent_id") == agent_id), None)
    if mine is not None:
        lines.append(f"Your agent finished #{mine.get('rank')} of {len(ranking)} "
                     f"({mine.get('points')} Swiss point(s)).")
    else:
        standing = next((row for row in detail.standings if row.agent_id == agent_id), None)
        if standing is not None:
            lines.append(f"Your agent: #{standing.rank} in the Swiss standings with {standing.points} point(s).")
    return lines
