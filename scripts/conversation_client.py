#!/usr/bin/env python3
"""Minimal conversation-mode client of the daemon's event stream (docs/10-cli-ipc-status.md §10.2).

    python3 scripts/conversation_client.py [--seconds 60] [--out events.jsonl]

Connects to the control socket, starts conversation mode with transcripts, prints every
event, and disconnects after --seconds or on Ctrl+C. The disconnect ends conversation mode.
With --out, every event is also written as one JSON line, with `rx`: the monotonic time when
the client read it. Each line from the daemon has `seq` (1, 2, … for this connection) and
`t_sent` (the daemon's time just before the write). The daemon times use the same clock
(CLOCK_MONOTONIC), so on Linux `time.perf_counter()` and `time.monotonic()` of the client can
be compared with them: `rx - t_sent` is the socket delay.
Uses only the standard library.
"""

import argparse
import json
import os
import socket
import time
from pathlib import Path

RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
SOCKET = Path(RUNTIME_DIR) / "local-stt" / "control.sock"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    out = args.out.open("w", encoding="utf-8") if args.out else None

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(SOCKET))
    request = {"cmd": "subscribe", "transcripts": True, "conversation": True}
    sock.sendall(json.dumps(request).encode() + b"\n")
    lines = sock.makefile("r", encoding="utf-8")
    deadline = time.monotonic() + args.seconds
    try:
        first = json.loads(lines.readline())
        if first.get("event") != "state":  # an error response, e.g. "busy": the daemon closes
            print("conversation not started:", first)
            return 1
        print(f"conversation on: speak now ({args.seconds:.0f} s, Ctrl+C ends)", flush=True)
        while (left := deadline - time.monotonic()) > 0:
            sock.settimeout(left)
            try:
                line = lines.readline()
            except TimeoutError:
                break
            if not line:  # the daemon closed the stream
                break
            event = json.loads(line)
            event["rx"] = time.monotonic()
            print(json.dumps(event, ensure_ascii=False), flush=True)
            if out:
                out.write(json.dumps(event, ensure_ascii=False) + "\n")
                out.flush()
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()  # the disconnect ends conversation mode (reason "client")
        if out:
            out.close()
    print("disconnected", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
