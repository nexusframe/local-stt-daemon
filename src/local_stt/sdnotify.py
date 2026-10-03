"""systemd notification protocol over `$NOTIFY_SOCKET` (docs/11 §11.5).

A raw AF_UNIX datagram socket instead of a `systemd-python` dependency. Outside systemd
(`NOTIFY_SOCKET` unset) every call is a no-op.
"""

import logging
import os
import socket
from collections.abc import Mapping

log = logging.getLogger("local_stt.sdnotify")


class SdNotifier:
    def __init__(self, environ: Mapping[str, str] = os.environ):
        address = environ.get("NOTIFY_SOCKET", "")
        # "@name" is a socket in the abstract namespace (leading NUL byte).
        self._address = "\0" + address[1:] if address.startswith("@") else address

    @property
    def enabled(self) -> bool:
        return bool(self._address)

    def ready(self) -> bool:
        return self.notify("READY=1")

    def status(self, text: str) -> bool:
        """`STATUS=<text>` shown by `systemctl --user status`; one line only."""
        return self.notify("STATUS=" + " ".join(text.splitlines()))

    def stopping(self) -> bool:
        return self.notify("STOPPING=1")

    def notify(self, *assignments: str) -> bool:
        """Sends newline-separated `KEY=value` assignments; False if not sent.

        A failed send is logged, never raised: the service keeps working, and systemd reports a
        missing READY=1 itself (start timeout).
        """
        if not self._address:
            return False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as sock:
                sock.sendto("\n".join(assignments).encode(), self._address)
        except OSError as e:
            log.warning("sd_notify %s failed: %s", assignments[0].split("=", 1)[0], e)
            return False
        return True
