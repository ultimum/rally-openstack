"""Initialize container directories and preserve operator files."""

import contextlib
import os
import pathlib
import sqlite3

from .progress import progress
from .state import project_lock
from .state import write_new


DEFAULT_CONFIG = "/etc/ultimum/ultimum.yaml"
DATA_DIR = "/data/ultimum"


def initialize(config_path=DEFAULT_CONFIG, data_dir=DATA_DIR, legacy_db=None):
    config_path = pathlib.Path(config_path)
    data_dir = pathlib.Path(data_dir)
    config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in ("home", "db", "keys", "state", "results"):
        directory = data_dir / name
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        progress(f"READY directory: {directory}", "init")
    source_dir = pathlib.Path(__file__).resolve().parent.parent
    for source, target in (
        ("ultimum.yaml.example", config_path),
        ("rally.conf", config_path.parent / "rally.conf"),
    ):
        try:
            if target.exists():
                raise FileExistsError()
            write_new(target, (source_dir / source).read_text())
        except FileExistsError:
            progress(f"REUSE configuration: {target}", "init")
        else:
            progress(f"CREATE configuration: {target}", "init")
    if legacy_db and pathlib.Path(legacy_db).is_file():
        target = data_dir / "db/rally.sqlite"
        with project_lock(data_dir / "state", "database-migration"):
            if not target.exists():
                progress(
                    "COPY existing Rally database to " + str(target), "init"
                )
                temporary = target.with_suffix(".migrating")
                try:
                    with contextlib.closing(
                        sqlite3.connect(
                            pathlib.Path(legacy_db).as_uri() + "?mode=ro",
                            uri=True,
                        )
                    ) as old:
                        with contextlib.closing(
                            sqlite3.connect(temporary)
                        ) as new:
                            old.backup(new)
                    os.chmod(temporary, 0o600)
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
    progress(
        f"Edit {config_path}; provide the admin OpenRC at "
        f"{config_path.parent / 'admin.rc'}, then run ultimum-rally prepare",
        "init",
    )
