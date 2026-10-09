"""The last transcripts, in RAM only (task 5.2): `local-stt last` and `local-stt history`.

The pipeline thread adds texts and the Controller thread reads them, so a lock guards the deque.
"""

import collections
import threading


class TranscriptHistory:
    def __init__(self, size: int):
        self._lock = threading.Lock()
        self._texts: collections.deque[str] = collections.deque(maxlen=size)

    def add(self, text: str) -> None:
        with self._lock:
            self._texts.append(text)

    def get(self, n: int) -> str | None:
        """The n-th newest text (1 = the newest), or None when there is no such text."""
        with self._lock:
            if not 1 <= n <= len(self._texts):
                return None
            return self._texts[-n]

    def items(self) -> list[str]:
        """All texts, newest first."""
        with self._lock:
            return list(reversed(self._texts))

    def resize(self, size: int) -> None:
        """Live reload of `history.size`; keeps the newest texts, 0 disables the history."""
        with self._lock:
            self._texts = collections.deque(self._texts, maxlen=size)
