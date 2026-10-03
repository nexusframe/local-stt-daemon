"""Generation-wide cancellation shared by the pipeline and the injector (docs/08 §8.3).

One token belongs to one pipeline generation. `PipelineWorker.cancel_all()` cancels it under
the same short lock the injector holds while checking validity and marking the start of an
input operation, so no operation of an old generation starts after cancellation.
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager


class Cancelled(Exception):
    """The token was cancelled before an input operation could start."""


class CancellationToken:
    def __init__(self, lock: threading.Lock):
        self._lock = lock  # shared with PipelineWorker.cancel_all()
        self._event = threading.Event()
        self._in_operation = False

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def in_operation(self) -> bool:
        return self._in_operation

    def wait(self, timeout_s: float) -> bool:
        """Sleeps up to `timeout_s`; returns True as soon as the token is cancelled."""
        return self._event.wait(timeout_s)

    @contextmanager
    def operation(self) -> Iterator[None]:
        """Marks one input operation (an XTest sequence, a `type` chunk) as in progress.

        Raises `Cancelled` instead of starting it. The marker stays set until the block
        exits; the lock is held only for the check, not for the operation itself.
        """
        with self._lock:
            if self._event.is_set():
                raise Cancelled
            self._in_operation = True
        try:
            yield
        finally:
            with self._lock:
                self._in_operation = False

    def cancel_locked(self) -> bool:
        """Cancels the token; the caller holds the shared lock. Returns whether an input
        operation was in progress at that moment."""
        self._event.set()
        return self._in_operation
