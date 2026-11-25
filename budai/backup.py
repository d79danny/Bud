import fcntl
import os
import shutil
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote


@contextmanager
def exclusive(directory):
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    if directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise PermissionError(f"Use a data directory owned by this user with mode 0700: {directory}")
    fd = os.open(directory / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("BudAI is using this data directory. Stop the server before continuing.") from error
        yield


def verify(database):
    uri = "file:" + quote(str(database)) + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        result = db.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise ValueError(f"SQLite integrity check failed: {result}")
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='users' AND type='table'").fetchone():
            raise ValueError("This is not a BudAI database")


def copy_data(source, destination, restore=False):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError("Source and destination must be separate directories")
    if not (source / "bud.sqlite3").is_file():
        raise FileNotFoundError(f"BudAI database not found in {source}")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Choose a new destination: {destination}")
    with exclusive(source):
        verify(source / "bud.sqlite3")
        destination.mkdir(mode=0o700, parents=True)
        try:
            for item in source.iterdir():
                if item.name in {".lock", "browser", "qt", "bud.sqlite3", "bud.sqlite3-wal", "bud.sqlite3-shm"}:
                    continue
                if item.is_symlink():
                    continue
                if item.is_dir():
                    shutil.copytree(item, destination / item.name, symlinks=True)
                elif item.is_file():
                    shutil.copy2(item, destination / item.name)
            temporary = destination / "bud.sqlite3"
            with sqlite3.connect(source / "bud.sqlite3") as db, sqlite3.connect(temporary) as copied:
                db.backup(copied)
                if restore:
                    copied.execute("DELETE FROM sessions")
                    if copied.execute("SELECT 1 FROM sqlite_master WHERE name='pairings'").fetchone():
                        copied.execute("DELETE FROM pairings")
            temporary.chmod(0o600)
            verify(temporary)
        except BaseException:
            shutil.rmtree(destination)
            raise
    return destination
