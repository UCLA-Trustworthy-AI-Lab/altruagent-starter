"""Synchronous bridge to Agent_ACP's MCP gameplay server, built on the
OFFICIAL `mcp` Python SDK client — not a hand-rolled JSON-RPC-over-HTTP
implementation (see "why" below).

History: an earlier version of this module hand-rolled the JSON-RPC 2.0
envelope directly over ``AltruAgentClient.request()``. It was replaced with
the official ``mcp.client.streamable_http.streamablehttp_client`` +
``mcp.ClientSession`` pattern Agent_ACP's own scripts use, which removes any
doubt about hand-rolled-protocol correctness. (The "Invalid Host header"
HTTP 421 first seen against the deployed server was a platform-side
allowed-hosts issue, since fixed in Agent_ACP; no client change was needed.)

Session reuse
-------------
The rest of this starter (``altruagent.runner``, ``altruagent.worker``,
contestant code) is deliberately synchronous, while the official SDK is
async-only. Until session reuse, every tool call therefore ran
``asyncio.run()`` around a brand-new transport and ``ClientSession``, which
cost four HTTP requests per tool call against GameAPI::

    initialize  ->  notifications/initialized  ->  tools/call  ->  tools/list

(``ClientSession.call_tool`` lists the tools once per session to validate a
tool's structured output, and every session was fresh.) Only one of those
four requests is the call the agent asked for.

Now one ``MCPConnection`` keeps a single initialized ``ClientSession`` per
GameAPI ``/mcp`` endpoint and reuses it for every call:

- **Event loop.** Each connection runs its own asyncio event loop on one
  daemon thread, started on the first call. The SDK's context managers
  (``streamablehttp_client``, ``ClientSession``) are entered and exited by a
  single *host task* on that loop, which is what anyio's task groups require.
  Synchronous callers hand each call to the loop with
  ``asyncio.run_coroutine_threadsafe`` and block on the answer, so the public
  interface stays synchronous and calls from several threads can be in
  flight on one session at once (MCP multiplexes them by request id).
- **What is cached.** Only the initialized session, and with it the SDK's own
  per-session cache of tool output schemas (one ``tools/list`` per session).
  GameAPI registers a fixed tool set, so that cache is safe for a session's
  lifetime. No tool *result* is ever cached: game state, legal actions,
  messages and results always come from a fresh ``tools/call``.
- **Why reuse is safe.** GameAPI runs FastMCP with ``stateless_http=True``:
  the server keeps no per-connection session, so a session is not tied to a
  match (the match is the ``session_id`` tool argument) and an idle session
  costs the server nothing between turns.
- **Failures.** In the SDK, any HTTP failure of one request (an HTTP error
  status, a dropped connection, a read timeout) ends the whole transport's
  task group, so the session dies with it. The host task records why; every
  call waiting on that session fails with that root cause (unwrapped below
  exactly as before), and the next call starts a fresh session. A call that
  timed out at the session level retires its session too, so a request still
  stuck on the wire can't later take down an unrelated call.
- **Bounded recovery.** At most one automatic retry per call, only when the
  call certainly had no effect or is a pure read:
    * HTTP 401: log in again once (``AltruAgentClient``'s one-retry-after-401
      policy) and retry once on a new session (the 401 is the auth check, so
      the tool never ran);
    * the server no longer knows the session (``Session terminated``, only
      possible if GameAPI ever becomes stateful): retry once on a new session;
    * a *reused* session's connection dropped (e.g. a kept-alive socket the
      server or a proxy closed while idle) during a read-only tool: retry
      once on a new session. A move, message or resignation is never sent
      twice: those failures go to the runner, which re-reads the state.
    The first two also skip a move, message or resignation whose session
    failed while another call was in flight on it: the 401 (or lost session)
    may have been the other call's, and the write may have landed. (The
    runner makes one call at a time; this matters to code that shares one
    client between threads.)
  Everything else surfaces as ``MCPToolError`` just as before, and the runner
  keeps its own backoff and give-up policy.
- **Lifetime.** ``call_tool(client, url, ...)`` keeps one connection per
  ``(client, url)``. ``AltruAgentClient.close()`` closes them (every worker
  closes its client when its match ends), as does garbage collection of the
  client and interpreter exit. ``MCPConnection.close()`` cancels the host,
  closes the HTTP client, stops the loop and joins its thread; a closed
  connection starts again on its next call.
- **Opt-out.** ``ALTRUAGENT_MCP_SESSION_REUSE=0`` restores one session per
  call (same code path, closed after each call).

JWT refresh: this bypasses ``AltruAgentClient.request()`` (the official SDK
owns its own HTTP transport), so the request()-level one-retry-after-401
logic can't be reused directly — but the *policy* is preserved exactly,
through ``AltruAgentClient._current_access_token()`` (same client, same
auth state, not a second auth system): fetch the current token, try once;
on a 401 (surfaced by the SDK's own ``response.raise_for_status()`` as
``httpx.HTTPStatusError`` — confirmed in mcp/client/streamable_http.py),
force a fresh login and retry exactly once, exactly like ``request()``. The
re-login runs on the calling thread, so its own errors (a refused seat, an
ended assignment) reach the caller unchanged. A session's bearer token is
fixed when the session starts: a call made after the client's token changed
(a re-login anywhere on that client) starts a new session with the current
token, and a token rejected later simply ends that session through the 401
path above.
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import json
import os
import threading
import weakref
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Callable

import httpx
from mcp import ClientSession, McpError, types
from mcp.client.streamable_http import streamablehttp_client
from pydantic_core import PydanticSerializationError

from .errors import PlatformError

if TYPE_CHECKING:
    from .client import AltruAgentClient

# How long one tool call may take. The SDK's own defaults are 30 s to connect
# and 300 s to read, so a connection that silently died (a Wi-Fi or VPN
# switch, a laptop waking up) would freeze the agent for five minutes. The
# longest healthy call is wait_for_update: the server holds it at most 25 s
# (GameAPI's WAIT_MAX_TIMEOUT_SECONDS), so the read timeout stays well above
# that plus network time. The session timeout is a backstop for an answer
# that trickles in without ever finishing. A call that times out raises
# MCPToolError (no error code), which the runner retries in place.
MCP_CONNECT_TIMEOUT_SECONDS = 10.0
MCP_READ_TIMEOUT_SECONDS = 40.0
MCP_SESSION_TIMEOUT_SECONDS = 45.0

# How long close() waits for a session to shut down cleanly before cancelling it.
MCP_CLOSE_TIMEOUT_SECONDS = 5.0

# ``ALTRUAGENT_MCP_SESSION_REUSE=0`` (or false/no/off): one session per call.
SESSION_REUSE_ENV = "ALTRUAGENT_MCP_SESSION_REUSE"

# Tools that only read: safe to send again if a reused session's connection
# dropped under them. play_action, send_message and resign are deliberately
# absent, and so is any tool not named here.
READ_ONLY_TOOLS = frozenset({
    "get_game_state",
    "wait_for_update",
    "get_legal_actions",
    "get_messages",
    "get_result",
    "get_game_config",
    "list_games",
    "get_agent_status",
})

# The SDK's own error codes (mcp.shared.session / mcp.client.streamable_http).
_SDK_REQUEST_TIMEOUT = int(httpx.codes.REQUEST_TIMEOUT)
_SDK_SESSION_TERMINATED = 32600


class MCPToolError(PlatformError):
    """A tool call reached the server but failed — either a structured
    application-level error the tool itself returned (``error_code`` is one
    of MCP's own codes, e.g. ``STALE_STATE``/``GAME_ALREADY_COMPLETE``), or a
    protocol/transport-level failure (``error_code`` is ``None`` in that
    case — including an HTTP-level rejection, surfaced with its
    ``status_code``).

    ``protocol_error`` is True when the server answered, but with an MCP tool
    error (``isError``) rather than a result: the tool's arguments failed
    validation, or the tool itself crashed. GameAPI sends every game error
    as a normal result with an ``error`` code, so this is a call the server
    can't handle, likely to fail the same way again. It is not a connection
    problem (``errors.is_transient_error`` is False for it).

    ``local_error`` is True when the call never left this process: its
    arguments can't be sent as JSON (for example a numpy number, bytes, or an
    object of the agent's own class inside a move). Neither a connection nor
    a server problem; sending the same arguments again fails the same way
    (``errors.is_transient_error`` is False for it too).
    """

    def __init__(
        self, message: str, *, protocol_error: bool = False, local_error: bool = False, **kwargs: Any
    ) -> None:
        super().__init__(message, **kwargs)
        self.protocol_error = protocol_error
        self.local_error = local_error


def _unsendable_arguments(name: str, exc: BaseException) -> MCPToolError:
    return MCPToolError(
        f"MCP tool {name!r} can't send its arguments as JSON: {exc}",
        status_code=None,
        error_code=None,
        local_error=True,
    )


def _check_arguments(name: str, arguments: dict) -> None:
    """Raise ``MCPToolError(local_error=True)`` if the SDK couldn't send
    ``arguments``: the same JSON conversion ``ClientSession.call_tool`` does,
    done before any connection is opened. Inside the SDK that failure would
    surface wrapped in anyio ExceptionGroups, indistinguishable from a
    dropped connection.
    """
    try:
        types.CallToolRequestParams(name=name, arguments=arguments).model_dump(
            by_alias=True, mode="json", exclude_none=True
        )
    except ValueError as exc:  # PydanticSerializationError, UnicodeDecodeError, ValidationError
        raise _unsendable_arguments(name, exc) from exc


def _find_exception(exc: BaseException, kind: type | tuple[type, ...]) -> BaseException | None:
    """The first ``kind`` exception anywhere in anyio's ExceptionGroup wrapping."""
    if isinstance(exc, kind):
        return exc
    for sub in getattr(exc, "exceptions", None) or ():
        found = _find_exception(sub, kind)
        if found is not None:
            return found
    return None


def _find_serialization_error(exc: BaseException) -> PydanticSerializationError | None:
    """A ``PydanticSerializationError`` anywhere in anyio's ExceptionGroup
    wrapping: the SDK failed to turn this call's arguments into JSON (a
    backstop for ``_check_arguments``)."""
    return _find_exception(exc, PydanticSerializationError)


def _parse_result(name: str, result: types.CallToolResult) -> dict:
    """Turn one ``tools/call`` answer into the payload dict, or raise
    ``MCPToolError`` for a tool error or an application-level error code."""
    if result.isError:
        raise MCPToolError(
            f"MCP tool {name!r} failed at the protocol level: {_extract_text(result)}",
            status_code=None,
            error_code=None,
            protocol_error=True,
        )

    payload = result.structuredContent
    if payload is None:
        text = _extract_text(result)
        try:
            payload = json.loads(text) if text else {}
        except ValueError:
            payload = {}

    if isinstance(payload, dict) and "error" in payload:
        raise MCPToolError(
            payload.get("detail") or f"MCP tool {name!r} returned error {payload['error']!r}.",
            status_code=None,
            error_code=payload["error"],
            next_action=(payload.get("next_actions") or [None])[0],
        )

    return payload if isinstance(payload, dict) else {}


def _extract_text(result: Any) -> str:
    content = getattr(result, "content", None)
    if content:
        text = getattr(content[0], "text", None)
        if text is not None:
            return str(text)
    return str(result)


def _find_http_status_error(exc: BaseException) -> httpx.HTTPStatusError | None:
    """Unwrap anyio's ExceptionGroup wrapping.

    Confirmed empirically (not assumed): ``streamablehttp_client`` runs its
    read/write loops inside ``anyio.create_task_group()``
    (mcp/client/streamable_http.py), so an HTTP-level failure — including
    the "Invalid Host header" 421 this module's docstring describes — never
    arrives as a bare ``httpx.HTTPStatusError``; it's always wrapped in an
    ``ExceptionGroup``/``BaseExceptionGroup``, one or more levels deep. A
    plain ``except httpx.HTTPStatusError`` here would silently never match,
    which would have made the 401-retry-once behavior in ``call_tool`` dead
    code — probed directly against a real ``httpx.MockTransport`` before
    trusting this, not inferred from documentation.
    """
    return _find_exception(exc, httpx.HTTPStatusError)


def _wrap_http_status_error(name: str, exc: httpx.HTTPStatusError) -> MCPToolError:
    """Build a clean ``MCPToolError`` from an HTTP-level failure.

    ``exc.response`` is a *streaming* response: the SDK's own transport
    (``mcp.client.streamable_http``'s ``stream_within_origin``) calls
    ``raise_for_status()`` while the response is still open, then closes it
    via its own ``finally: await response.aclose()`` as this exception
    unwinds — before it ever reaches ``call_tool()``'s except clause, and
    without the body ever having been read. Accessing ``.text``/``.content``
    on a closed, never-read streaming response raises
    ``httpx.ResponseNotRead`` — confirmed empirically (a real 421/401
    response body is fully drained by the time it gets here) — which would
    otherwise mask the original HTTP status behind an unrelated crash. We
    have no way to read the body earlier (that would mean patching the SDK's
    own transport), so fall back to ``str(exc)`` instead: httpx's own
    ``HTTPStatusError`` message already includes the status code, reason
    phrase, and URL, which is enough to act on even without the body.
    """
    try:
        body = exc.response.text
        detail = body[:500] if body else str(exc)
    except httpx.ResponseNotRead:
        detail = str(exc)
    return MCPToolError(
        f"MCP tool {name!r} request failed with HTTP {exc.response.status_code}: {detail}",
        status_code=exc.response.status_code,
        error_code=None,
    )


def _is_session_terminated(exc: BaseException) -> bool:
    return isinstance(exc, McpError) and exc.error.code == _SDK_SESSION_TERMINATED


def _is_dropped_connection(exc: BaseException) -> bool:
    """The connection itself failed (reset, closed, refused) — not a timeout,
    which already waited the full read timeout and is left to the runner."""
    found = _find_exception(exc, httpx.TransportError)
    return found is not None and not isinstance(found, httpx.TimeoutException)


class _ConnectionClosed(Exception):
    """The session ended under a call without a recorded cause: the
    connection was closed (``MCPConnection.close()``) while the call was in
    flight."""


class _AttemptFailed(Exception):
    """One attempt failed. ``cause`` is the root cause (unwrapped by
    ``MCPConnection.call_tool`` exactly as the per-call transport did);
    ``reused`` says whether the session had already answered a call;
    ``shared`` says the session died while another call was in flight on it
    too, so ``cause`` may belong to that other call's request, not this one's."""

    def __init__(self, cause: BaseException, *, reused: bool, shared: bool = False) -> None:
        super().__init__(repr(cause))
        self.cause = cause
        self.reused = reused
        self.shared = shared


class _Overlap:
    """Whether a call shared its session with another call in flight."""

    __slots__ = ("shared",)

    def __init__(self, shared: bool) -> None:
        self.shared = shared


class _Host:
    """One initialized ``ClientSession`` and the task that owns its context
    managers. Touched only from the connection's event-loop thread."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.session: ClientSession | None = None
        self.task: asyncio.Task | None = None
        self.ready = asyncio.Event()
        self.stop = asyncio.Event()
        self.failure: BaseException | None = None
        self.inflight = 0
        # One marker per call in flight; its ``shared`` turns True once
        # another call overlaps it.
        self.active: list[_Overlap] = []
        self.completed = 0
        self.retired = False


class MCPConnection:
    """A reusable MCP client session to one GameAPI ``/mcp`` endpoint, for
    synchronous callers. See the module docstring for the lifecycle.

    ``client`` is anything with ``_current_access_token(force_relogin=...)``
    (an ``AltruAgentClient``). Thread-safe: several threads may call
    ``call_tool`` at once. ``httpx_client_factory`` is a test-only seam (the
    ``streamablehttp_client`` parameter of that name).

    ``initializations`` counts sessions started (each one ``initialize``
    handshake): a number for tests and benchmarks.
    """

    def __init__(self, client: "AltruAgentClient", mcp_url: str, *, httpx_client_factory=None) -> None:
        self._client = client
        self.mcp_url = mcp_url
        self._httpx_client_factory = httpx_client_factory
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        # Loop-thread state.
        self._current: _Host | None = None
        self._hosts: set[_Host] = set()
        self._calls: set[asyncio.Task] = set()
        self.initializations = 0
        _all_connections.add(self)  # closed at exit, reset after a fork

    def __repr__(self) -> str:
        return f"MCPConnection({self.mcp_url!r}, open={self._loop is not None})"

    def __enter__(self) -> "MCPConnection":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- synchronous API ----------------------------------------------------

    def call_tool(self, name: str, arguments: dict) -> dict:
        """Call one MCP tool and return its parsed result payload as a dict.

        Raises ``MCPToolError`` for application-level tool errors and for
        transport/HTTP-level failures, so callers need one except clause. A
        401 triggers exactly one re-login + retry; see the module docstring
        for the other (bounded, at most one) retries.
        """
        _check_arguments(name, arguments)
        token = self._client._current_access_token()
        try:
            return _parse_result(name, self._attempt(name, arguments, token))
        except _AttemptFailed as failed:
            exc = failed.cause
            retry_token = token
            if _find_serialization_error(exc) is not None:
                raise _unsendable_arguments(name, _find_serialization_error(exc)) from exc
            http_exc = _find_http_status_error(exc)
            retryable = (http_exc is not None and http_exc.response.status_code == 401) or _is_session_terminated(exc)
            if retryable and failed.shared and name not in READ_ONLY_TOOLS:
                # The session failed while another call was in flight on it,
                # so this cause may be that call's, and this move, message or
                # resignation may have reached the server. Never send it
                # again: a transient error makes the runner re-read the state.
                raise MCPToolError(
                    f"MCP tool {name!r} failed: its session ended under a concurrent call ({exc!r}); "
                    "not resent, since it may have reached the server",
                    status_code=None,
                    error_code=None,
                ) from exc
            if http_exc is not None and http_exc.response.status_code == 401:
                retry_token = self._client._current_access_token(force_relogin=True)
            elif http_exc is not None:
                raise _wrap_http_status_error(name, http_exc) from exc
            elif _is_session_terminated(exc):
                pass
            elif failed.reused and name in READ_ONLY_TOOLS and _is_dropped_connection(exc):
                pass
            else:
                raise MCPToolError(
                    f"MCP tool {name!r} failed: {exc!r}", status_code=None, error_code=None
                ) from exc
            first_failure = exc

        try:
            return _parse_result(name, self._attempt(name, arguments, retry_token))
        except _AttemptFailed as failed:
            exc2 = failed.cause
            serialization_exc = _find_serialization_error(exc2)
            if serialization_exc is not None:
                raise _unsendable_arguments(name, serialization_exc) from exc2
            http_exc2 = _find_http_status_error(exc2)
            if http_exc2 is not None:
                raise _wrap_http_status_error(name, http_exc2) from exc2
            after = "re-login" if retry_token != token else "reconnecting"
            raise MCPToolError(
                f"MCP tool {name!r} failed after {after}: {exc2!r} (first attempt: {first_failure!r})",
                status_code=None,
                error_code=None,
            ) from exc2

    def close(self, timeout: float = MCP_CLOSE_TIMEOUT_SECONDS) -> None:
        """End the session, close its HTTP client, stop the event loop and
        join its thread. Idempotent; the next call starts a new session."""
        with self._lock:
            loop, thread = self._loop, self._thread
            self._loop = self._thread = None
        if loop is None or thread is None:
            return
        if threading.current_thread() is thread:  # pragma: no cover - never done by this package
            loop.call_soon(loop.stop)
            return
        try:
            asyncio.run_coroutine_threadsafe(self._shutdown(timeout), loop).result(timeout + 1.0)
        except Exception:  # noqa: BLE001 - best effort; the loop is stopped below either way
            pass
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout)
        if not thread.is_alive():
            loop.close()

    # -- event loop -----------------------------------------------------------

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                thread = threading.Thread(
                    target=self._run_loop, args=(loop,), name=f"mcp-session {self.mcp_url}", daemon=True
                )
                thread.start()
                self._loop, self._thread = loop, thread
            return self._loop

    @staticmethod
    def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    def _attempt(self, name: str, arguments: dict, token: str) -> types.CallToolResult:
        future = asyncio.run_coroutine_threadsafe(self._acall(name, arguments, token), self._ensure_loop())
        try:
            return future.result()
        except concurrent.futures.CancelledError as exc:
            # close() from another thread cancelled this call.
            raise _AttemptFailed(_ConnectionClosed("the MCP connection was closed"), reused=False) from exc
        except BaseException:
            # KeyboardInterrupt (or anything else) while waiting: don't leave
            # the call running on the loop.
            future.cancel()
            raise

    # -- loop-thread side -------------------------------------------------------

    def _transport_kwargs(self, token: str) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "headers": {"Authorization": f"Bearer {token}"},
            # The SDK builds httpx.Timeout(timeout, read=sse_read_timeout) from these.
            "timeout": MCP_CONNECT_TIMEOUT_SECONDS,
            "sse_read_timeout": MCP_READ_TIMEOUT_SECONDS,
        }
        if self._httpx_client_factory is not None:
            # Test-only seam: lets unit tests exercise the real
            # streamablehttp_client / ClientSession code paths against an
            # httpx.MockTransport-backed AsyncClient instead of real sockets.
            kwargs["httpx_client_factory"] = self._httpx_client_factory
        return kwargs

    async def _host_main(self, host: _Host) -> None:
        """Own one session from ``initialize`` until it is retired or fails."""
        try:
            async with streamablehttp_client(self.mcp_url, **self._transport_kwargs(host.token)) as (read, write, _):
                async with ClientSession(
                    read, write, read_timeout_seconds=timedelta(seconds=MCP_SESSION_TIMEOUT_SECONDS)
                ) as session:
                    await session.initialize()
                    host.session = session
                    host.ready.set()
                    await host.stop.wait()
        except Exception as exc:  # noqa: BLE001 - recorded; each waiting call re-raises it
            host.failure = exc

    def _retire(self, host: _Host) -> None:
        """No new calls on ``host``; it ends once its calls in flight finish."""
        host.retired = True
        if self._current is host:
            self._current = None
        if host.inflight == 0:
            host.stop.set()

    async def _get_host(self, token: str) -> _Host:
        host = self._current
        if host is not None and not host.retired and host.token != token:
            # The client logged in again since this session started (e.g. a
            # REST call's 401 retry): move to the current token now, as the
            # per-call transport did, instead of waiting for this one's 401.
            self._retire(host)
            host = None
        if host is None or host.retired or host.task.done():
            # Nothing awaits between the check above and this assignment, so
            # concurrent calls on this loop share one new session.
            host = _Host(token)
            host.task = asyncio.get_running_loop().create_task(self._host_main(host))
            host.task.add_done_callback(lambda _task, host=host: self._hosts.discard(host))
            self._hosts.add(host)
            self._current = host
            self.initializations += 1
        if not host.ready.is_set():
            ready = asyncio.ensure_future(host.ready.wait())
            try:
                await asyncio.wait({ready, host.task}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                ready.cancel()
            if not host.ready.is_set():
                raise _AttemptFailed(host.failure or _ConnectionClosed("the MCP session ended"), reused=False)
        return host

    async def _acall(self, name: str, arguments: dict, token: str) -> types.CallToolResult:
        task = asyncio.current_task()
        self._calls.add(task)
        try:
            return await self._acall_on_host(name, arguments, token)
        finally:
            self._calls.discard(task)

    async def _acall_on_host(self, name: str, arguments: dict, token: str) -> types.CallToolResult:
        host = await self._get_host(token)
        reused = host.completed > 0
        host.inflight += 1
        overlap = _Overlap(bool(host.active))
        for other in host.active:
            other.shared = True
        host.active.append(overlap)
        call = asyncio.ensure_future(
            host.session.call_tool(
                name, arguments, read_timeout_seconds=timedelta(seconds=MCP_SESSION_TIMEOUT_SECONDS)
            )
        )
        try:
            await asyncio.wait({call, host.task}, return_when=asyncio.FIRST_COMPLETED)
            if call.done() and not call.cancelled() and call.exception() is None:
                host.completed += 1
                return call.result()
            if not call.done():
                # The session died with this call still waiting for its answer.
                call.cancel()
                await asyncio.gather(call, return_exceptions=True)
                raise _AttemptFailed(
                    host.failure or _ConnectionClosed("the MCP session ended"),
                    reused=reused,
                    shared=overlap.shared,
                )
            exc = (None if call.cancelled() else call.exception()) or _ConnectionClosed("the MCP call was cancelled")
            if isinstance(exc, McpError) and exc.error.code == types.CONNECTION_CLOSED:
                # The session is going down; report why, not just that it did.
                await asyncio.wait({host.task}, timeout=1.0)
                raise _AttemptFailed(host.failure or exc, reused=reused, shared=overlap.shared)
            if isinstance(exc, McpError) and exc.error.code in (_SDK_REQUEST_TIMEOUT, _SDK_SESSION_TERMINATED):
                self._retire(host)
            raise _AttemptFailed(exc, reused=reused)
        except asyncio.CancelledError:
            call.cancel()
            raise
        finally:
            host.inflight -= 1
            host.active.remove(overlap)
            if host.retired and host.inflight == 0:
                host.stop.set()

    async def _shutdown(self, timeout: float) -> None:
        for task in list(self._calls):
            # Calls in flight: their callers get "connection closed".
            task.cancel()
        hosts = list(self._hosts)
        self._current = None
        for host in hosts:
            self._retire(host)
            host.stop.set()
        tasks = [host.task for host in hosts if host.task is not None]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=timeout)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=1.0)


# -- per-client connections for call_tool() -------------------------------------------

_registry_lock = threading.Lock()
# client -> {(mcp_url, httpx_client_factory): MCPConnection}
_registry: "weakref.WeakKeyDictionary[Any, dict[tuple[str, Any], MCPConnection]]" = weakref.WeakKeyDictionary()
_all_connections: "weakref.WeakSet[MCPConnection]" = weakref.WeakSet()


def session_reuse_enabled() -> bool:
    """False when ``ALTRUAGENT_MCP_SESSION_REUSE`` is 0/false/no/off."""
    return os.environ.get(SESSION_REUSE_ENV, "1").strip().lower() not in {"0", "false", "no", "off"}


def _close_all_in(connections: dict) -> None:
    for connection in list(connections.values()):
        connection.close()
    connections.clear()


def _shared_connection(client: Any, mcp_url: str, httpx_client_factory) -> MCPConnection | None:
    """This client's connection to ``mcp_url``, created on first use; None
    when the client can't be weakly referenced (then calls don't share)."""
    key = (mcp_url, httpx_client_factory)
    with _registry_lock:
        try:
            connections = _registry.get(client)
        except TypeError:
            return None
        if connections is None:
            connections = {}
            try:
                _registry[client] = connections
            except TypeError:
                return None
            # Close this client's connections once the client is gone. The
            # connection itself only holds a weak proxy, so it never keeps
            # the client alive.
            weakref.finalize(client, _close_all_in, connections)
        connection = connections.get(key)
        if connection is None:
            connection = MCPConnection(
                weakref.proxy(client), mcp_url, httpx_client_factory=httpx_client_factory
            )
            connections[key] = connection
        return connection


def close_connections(client: Any) -> None:
    """Close every MCP session ``call_tool`` opened for ``client`` (called by
    ``AltruAgentClient.close()``). Safe to call more than once."""
    with _registry_lock:
        try:
            connections = _registry.pop(client, None)
        except TypeError:
            connections = None
    if connections:
        _close_all_in(connections)


@atexit.register
def _close_all_connections() -> None:
    for connection in list(_all_connections):
        connection.close(timeout=1.0)


def _reset_connections_after_fork() -> None:
    """In a forked child: the parent's event-loop threads don't exist here,
    so drop every connection's loop and sessions (without touching the
    parent's sockets); each one starts afresh on its next call. The runtime
    itself only ever uses ``spawn``; this covers agent code that forks."""
    global _registry_lock
    _registry_lock = threading.Lock()
    for connection in list(_all_connections):
        connection._lock = threading.Lock()
        connection._loop = connection._thread = None
        connection._current = None
        connection._hosts = set()
        connection._calls = set()


if hasattr(os, "register_at_fork"):  # not on Windows
    os.register_at_fork(after_in_child=_reset_connections_after_fork)


def call_tool(
    client: "AltruAgentClient",
    mcp_url: str,
    name: str,
    arguments: dict,
    *,
    httpx_client_factory=None,
) -> dict:
    """Call one MCP tool and return its parsed result payload as a plain dict.

    ``mcp_url`` is ``{game_server_url}/mcp`` (see ``MCPGameSession``). Raises
    ``MCPToolError`` (a ``PlatformError`` subclass) for both application-level
    tool errors and transport/HTTP-level failures, so callers only need one
    except clause. A 401 triggers exactly one re-login + retry, reusing
    ``AltruAgentClient``'s own auth state — see the module docstring.

    Reuses one initialized MCP session per ``(client, mcp_url)`` across calls
    (see "Session reuse" above); ``AltruAgentClient.close()`` ends it. With
    ``ALTRUAGENT_MCP_SESSION_REUSE=0``, each call opens and closes its own.

    ``httpx_client_factory`` is a test-only seam (see ``MCPConnection``) —
    production callers never pass it.
    """
    connection = _shared_connection(client, mcp_url, httpx_client_factory) if session_reuse_enabled() else None
    if connection is not None:
        return connection.call_tool(name, arguments)
    with MCPConnection(client, mcp_url, httpx_client_factory=httpx_client_factory) as one_shot:
        return one_shot.call_tool(name, arguments)
