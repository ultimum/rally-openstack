"""Immediate operator progress, with a heartbeat during long operations."""

import contextlib
import datetime as dt
import re
import threading
import time


_lock = threading.RLock()
_latest = (time.monotonic(), "runner", "Starting")


def progress(message, scope="prepare"):
    global _latest
    # Log descriptions and resource references, never API bodies or secrets.
    message = re.sub(r"[\x00-\x1f\x7f]", " ", str(message))
    with _lock:
        _latest = (time.monotonic(), scope, message)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%SZ")
        print(f"[{stamp}] [{scope}] {message}", flush=True)


@contextlib.contextmanager
def watch(interval=15):
    """Report the last operation during silence; stop with the caller."""
    stopped = threading.Event()

    def heartbeat():
        while not stopped.wait(interval):
            with _lock:
                started, scope, message = _latest
                elapsed = time.monotonic() - started
                if elapsed >= interval:
                    stamp = dt.datetime.now(dt.timezone.utc).strftime(
                        "%H:%M:%SZ"
                    )
                    print(
                        f"[{stamp}] [{scope}] WAIT {int(elapsed)}s: {message}",
                        flush=True,
                    )

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join()
