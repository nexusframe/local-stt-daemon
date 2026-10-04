"""Control socket: JSON Lines over a Unix socket (docs/10-cli-ipc-status.md §10.2).

The server turns requests into controller events carrying a `Future` and writes the
controller's response; it never touches daemon state itself (04 §4.2). `subscribe` (task 2.5)
turns a connection into a stream: the controller publishes state changes and job events, and
each subscriber's own connection thread writes them. The client side is used by the CLI.
"""

import contextlib
import json
import logging
import os
import queue
import select
import socket
import socketserver
import struct
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any

from local_stt.events import (
    CancelRequested,
    ContinuousToggle,
    Event,
    PttPressed,
    PttReleased,
    ReloadRequested,
    StatusRequested,
)

log = logging.getLogger("local_stt.ipc")

SOCKET_NAME = "control.sock"
MAX_LINE = 64 * 1024  # longer requests are rejected and the connection closed (14 §14.2)
REPLY_TIMEOUT_S = 5.0  # waiting for the controller (10 §10.2)
CLIENT_TIMEOUT_S = REPLY_TIMEOUT_S + 2.0
IDLE_CONNECTION_S = 60.0  # a client that sends nothing is disconnected
SUBSCRIBER_BACKLOG = 256  # messages a subscriber may fall behind before it is disconnected

Response = dict[str, Any]


class IpcError(Exception):
    """The control socket cannot be used."""


class DaemonNotRunning(IpcError):
    """No daemon listens on the socket (CLI exit code 3)."""


class AnotherInstanceRunning(IpcError):
    """A daemon already listens on the socket (startup: exit code 1)."""


def socket_path(environ: Mapping[str, str] = os.environ) -> Path:
    runtime = environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        raise IpcError("XDG_RUNTIME_DIR is not set")
    return Path(runtime) / "local-stt" / SOCKET_NAME


def error(code: str, message: str) -> Response:
    return {"ok": False, "error": code, "message": message}


def request_event(request: Any, reply: "Future[Response]") -> Event | Response:
    """A parsed request → the controller event, or an error response for a bad request."""
    if not isinstance(request, dict):
        return error("bad_request", "a request must be a JSON object")
    cmd = request.get("cmd")
    if cmd == "status":
        return StatusRequested(reply)
    if cmd == "ptt":
        action = request.get("action")
        if action == "start":
            return PttPressed(time.monotonic(), reply)
        if action == "stop":
            return PttReleased(time.monotonic(), reply)
        return error("bad_request", 'ptt needs "action": "start" or "stop"')
    if cmd == "cancel":
        return CancelRequested(reply)
    if cmd == "reload":
        return ReloadRequested(reply)
    if cmd == "toggle":
        return ContinuousToggle(reply)  # rejected by the controller until v0.2
    return error("unknown_command", f"unknown command {cmd!r}")


class _Subscription:
    def __init__(self) -> None:
        self.messages: queue.Queue[Response | None] = queue.Queue(SUBSCRIBER_BACKLOG)
        self.dropped = False


class Subscribers:
    """Fan-out of published messages; `publish()` never blocks the controller."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: set[_Subscription] = set()

    def add(self) -> _Subscription:
        sub = _Subscription()
        with self._lock:
            self._subs.add(sub)
        return sub

    def remove(self, sub: _Subscription) -> None:
        with self._lock:
            self._subs.discard(sub)

    def publish(self, message: Response) -> None:
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            try:
                sub.messages.put_nowait(message)
            except queue.Full:
                log.warning("ipc: a subscriber fell behind; disconnecting it")
                sub.dropped = True
                self.remove(sub)

    def close(self) -> None:
        """Ends every stream (daemon shutdown)."""
        with self._lock:
            subs, self._subs = list(self._subs), set()
        for sub in subs:
            sub.dropped = True  # seen within a second even if the wake-up does not fit
            with contextlib.suppress(queue.Full):
                sub.messages.put_nowait(None)


def peer_uid(sock: socket.socket) -> int:
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return int(uid)


class _Handler(socketserver.StreamRequestHandler):
    server: "_Server"
    timeout = IDLE_CONNECTION_S

    def handle(self) -> None:
        uid = peer_uid(self.request)
        if uid != self.server.uid:
            log.warning("rejected IPC connection from uid %d", uid)
            return
        try:
            while True:
                line = self.rfile.readline(MAX_LINE + 1)
                if not line:
                    return
                if len(line) > MAX_LINE:
                    self._send(error("too_long", f"requests are limited to {MAX_LINE} bytes"))
                    return
                if _is_subscribe(line):
                    return self._stream()
                self._send(self._respond(line))
        except (TimeoutError, ConnectionError):
            return

    def _stream(self) -> None:
        """`subscribe`: the current state first, then every published message until the
        client disconnects or falls behind (10 §10.2)."""
        sub = self.server.subscribers.add()
        try:
            initial = self._respond(b'{"cmd": "status"}')
            if not initial.get("ok"):
                return self._send(initial)
            self._send({"event": "state", "status": initial["status"]})
            while not sub.dropped:
                try:
                    message = sub.messages.get(timeout=1.0)
                except queue.Empty:
                    if _closed_by_peer(self.request):
                        return
                    continue
                if message is None:
                    return
                self._send(message)
        finally:
            self.server.subscribers.remove(sub)

    def _respond(self, line: bytes) -> Response:
        try:
            request = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            return error("bad_request", f"invalid JSON: {e}")
        reply: Future[Response] = Future()
        event = request_event(request, reply)
        if isinstance(event, dict):
            return event
        log.debug("ipc: %s", request.get("cmd"))
        self.server.post(event)
        try:
            return reply.result(self.server.reply_timeout_s)
        except FutureTimeout:
            log.error("ipc: no controller response to %s", request.get("cmd"))
            return error("timeout", "the daemon did not respond in time")

    def _send(self, response: Response) -> None:
        self.wfile.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(
        self,
        path: Path,
        post: Callable[[Event], None],
        reply_timeout_s: float,
        subscribers: Subscribers,
    ):
        self.post = post
        self.subscribers = subscribers
        self.reply_timeout_s = reply_timeout_s
        self.uid = os.getuid()
        super().__init__(str(path), _Handler)

    def handle_error(self, request: Any, client_address: Any) -> None:
        log.exception("IPC request failed")


class IpcServer:
    """The `ipc-server` thread (02 §2.2) plus one thread per connection."""

    def __init__(
        self,
        path: Path,
        post: Callable[[Event], None],
        *,
        reply_timeout_s: float = REPLY_TIMEOUT_S,
        poll_interval_s: float = 0.5,  # how quickly stop() is noticed; socketserver's default
    ):
        self.path = path
        self._poll_interval_s = poll_interval_s
        self._post = post
        self._reply_timeout_s = reply_timeout_s
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self.subscribers = Subscribers()

    def publish(self, message: Response) -> None:
        """Sends a message to every `subscribe` connection (controller thread, non-blocking)."""
        self.subscribers.publish(message)

    def start(self) -> None:
        """Binds the socket (raises AnotherInstanceRunning or OSError) and starts serving."""
        directory = self.path.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        if self.path.exists() or self.path.is_symlink():
            if _listening(self.path):
                raise AnotherInstanceRunning(f"another instance is running ({self.path})")
            log.info("removing stale socket %s", self.path)
            self.path.unlink()
        self._server = _Server(self.path, self._post, self._reply_timeout_s, self.subscribers)
        os.chmod(self.path, 0o600)
        self.restart_thread()
        log.info("listening on %s", self.path)

    def restart_thread(self) -> None:
        """Serves the bound socket in a new `ipc-server` thread; also used after the previous
        one died (E14: non-critical threads are restarted)."""
        if self._server is None:
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            args=(self._poll_interval_s,),
            name="ipc-server",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stops accepting connections and removes the socket file."""
        if self._server is None:
            return
        self.subscribers.close()
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        self.path.unlink(missing_ok=True)


def _is_subscribe(line: bytes) -> bool:
    try:
        request = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(request, dict) and request.get("cmd") == "subscribe"


def _closed_by_peer(sock: socket.socket) -> bool:
    """True once the client has closed its end (a subscriber sends nothing after subscribing)."""
    readable, _, _ = select.select([sock], [], [], 0)
    if not readable:
        return False
    try:
        return sock.recv(1, socket.MSG_PEEK) == b""
    except OSError:
        return True


def _listening(path: Path) -> bool:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        try:
            s.connect(str(path))
        except OSError:
            return False
    return True


def call(
    request: Mapping[str, Any], *, path: Path | None = None, timeout_s: float = CLIENT_TIMEOUT_S
) -> Response:
    """Sends one request and returns the daemon's response (CLI side)."""
    target = path if path is not None else socket_path()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout_s)
        try:
            s.connect(str(target))
        except (FileNotFoundError, ConnectionRefusedError) as e:
            raise DaemonNotRunning("daemon not running") from e
        try:
            s.sendall(json.dumps(request).encode() + b"\n")
            line = s.makefile("rb").readline(MAX_LINE + 1)
        except OSError as e:  # includes the timeout
            raise IpcError(f"no response from the daemon: {e}") from e
    if not line:
        raise IpcError("the daemon closed the connection without a response")
    try:
        response = json.loads(line)
    except json.JSONDecodeError as e:
        raise IpcError(f"invalid response from the daemon: {e}") from e
    if not isinstance(response, dict):
        raise IpcError("invalid response from the daemon")
    return response


def subscribe(*, path: Path | None = None) -> Iterator[Response]:
    """Yields the daemon's `subscribe` stream (CLI side) until the daemon closes it; the first
    message is the current state, or an error response."""
    target = path if path is not None else socket_path()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(CLIENT_TIMEOUT_S)
        try:
            s.connect(str(target))
        except (FileNotFoundError, ConnectionRefusedError) as e:
            raise DaemonNotRunning("daemon not running") from e
        s.sendall(b'{"cmd": "subscribe"}\n')
        stream = s.makefile("rb")
        first = True
        while True:
            try:
                line = stream.readline(MAX_LINE + 1)
            except OSError as e:
                raise IpcError(f"no response from the daemon: {e}") from e
            if not line:
                return
            try:
                message = json.loads(line)
            except json.JSONDecodeError as e:
                raise IpcError(f"invalid message from the daemon: {e}") from e
            yield message
            if first:
                first = False
                s.settimeout(None)  # events may be minutes apart
