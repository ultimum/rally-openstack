"""Private, durable resource ledger and process-wide project exclusion."""

import contextlib
import datetime as dt
import fcntl
import json
import os
import pathlib
import tempfile
import threading

from .config import InvalidError
from .progress import progress


def timestamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def write_new(path, text):
    """Atomically publish a private file without replacing existing paths."""
    path = pathlib.Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".ultimum-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.unlink(temporary)


def write_json(path, data):
    path = pathlib.Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".ultimum-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextlib.contextmanager
def project_lock(directory, key):
    pathlib.Path(directory).mkdir(parents=True, mode=0o700, exist_ok=True)
    path = pathlib.Path(directory) / (key + ".lock")
    with path.open("a+") as stream:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InvalidError(
                "Another runner is using this project/state directory"
            ) from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class Ledger:
    def __init__(self, path, initial=None):
        self.path = pathlib.Path(path)
        self.lock = threading.RLock()
        self.data = (
            initial
            if initial is not None
            else json.loads(self.path.read_text())
        )
        self.data.setdefault("resources", [])
        self.data.setdefault("evidence", {})
        self.data.setdefault("events", [])
        self.data.setdefault("restore", [])
        if initial is not None:
            self.save()

    def save(self):
        with self.lock:
            write_json(self.path, self.data)

    def update(self, **values):
        with self.lock:
            self.data.update(values)
            self.save()

    def own(self, service, path, identifier, **extra):
        with self.lock:
            resource = dict(
                service=service,
                path=path,
                id=identifier,
                deleted=False,
                **extra,
            )
            if not any(
                r["service"] == service and r["path"] == path
                for r in self.data["resources"]
            ):
                self.data["resources"].append(resource)
                self.save()
            return identifier

    def evidence(self, name, value):
        with self.lock:
            self.data["evidence"][name] = value
            self.save()

    def event(self, message):
        with self.lock:
            self.data["events"].append(
                {"time": timestamp(), "message": message}
            )
            self.save()
        progress(message, self.data.get("scenario", "runner"))
