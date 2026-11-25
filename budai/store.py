import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, password TEXT NOT NULL, totp TEXT,
    totp_used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label TEXT NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, seen TEXT,
    revoked TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires INTEGER NOT NULL, device INTEGER REFERENCES devices(id), seen INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pairings (
    token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, action TEXT NOT NULL, detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    parent INTEGER REFERENCES folders(id) ON DELETE CASCADE,
    title TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL, folder INTEGER REFERENCES folders(id) ON DELETE SET NULL,
    created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, pinned INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL, message TEXT NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'semantic',
    confidence REAL NOT NULL DEFAULT 1, source TEXT NOT NULL DEFAULT 'user',
    conversation INTEGER, corrects INTEGER, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    superseded INTEGER NOT NULL DEFAULT 0, vector TEXT, embedding_model TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_index USING fts5(
    content, content='memories', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS memory_insert AFTER INSERT ON memories BEGIN
    INSERT INTO memory_index(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memory_delete AFTER DELETE ON memories BEGIN
    INSERT INTO memory_index(memory_index, rowid, content) VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS memory_update AFTER UPDATE OF content ON memories BEGIN
    INSERT INTO memory_index(memory_index, rowid, content) VALUES ('delete', old.id, old.content);
    INSERT INTO memory_index(rowid, content) VALUES (new.id, new.content);
END;
CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, detail TEXT NOT NULL,
    component TEXT NOT NULL DEFAULT 'app', severity TEXT NOT NULL DEFAULT 'error', trace TEXT,
    conversation INTEGER, task INTEGER, metadata TEXT
);
CREATE TABLE IF NOT EXISTS login_attempts (
    source TEXT NOT NULL, created INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS attempts_source ON login_attempts(source, created);
CREATE TABLE IF NOT EXISTS settings (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL, mime TEXT NOT NULL, kind TEXT NOT NULL, bytes INTEGER NOT NULL,
    created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, conversation INTEGER
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL, prompt TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued', due INTEGER NOT NULL, interval INTEGER, priority INTEGER NOT NULL,
    depends INTEGER REFERENCES tasks(id) ON DELETE SET NULL, attempts INTEGER NOT NULL DEFAULT 0,
    conversation INTEGER, result TEXT, error TEXT, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY, task INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, state TEXT NOT NULL, detail TEXT
);
CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    conversation INTEGER, role TEXT NOT NULL, assignment TEXT NOT NULL, state TEXT NOT NULL,
    created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, elapsed_ms INTEGER, report TEXT
);
CREATE TABLE IF NOT EXISTS improvements (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL, kind TEXT NOT NULL, proposal TEXT NOT NULL, state TEXT NOT NULL,
    evidence TEXT, version INTEGER NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS research (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    query TEXT NOT NULL, record TEXT NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS media_jobs (
    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    workflow TEXT NOT NULL, status TEXT NOT NULL, remote_id TEXT, outputs TEXT, error TEXT, progress TEXT,
    created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS conversation_owner ON conversations(user_id);
CREATE INDEX IF NOT EXISTS message_history ON messages(conversation_id,id);
CREATE INDEX IF NOT EXISTS memory_owner ON memories(user_id,category);
CREATE INDEX IF NOT EXISTS tasks_ready ON tasks(state,due,priority);
"""

UPGRADES = {
    "media_jobs": {"progress": "TEXT"},
    "users": {"totp": "TEXT", "totp_used": "INTEGER NOT NULL DEFAULT 0"},
    "sessions": {"device": "INTEGER REFERENCES devices(id)", "seen": "INTEGER NOT NULL DEFAULT 0"},
    "conversations": {
        "folder": "INTEGER REFERENCES folders(id) ON DELETE SET NULL", "created": "TEXT",
        "pinned": "INTEGER NOT NULL DEFAULT 0", "archived": "INTEGER NOT NULL DEFAULT 0",
    },
    "messages": {"created": "TEXT"},
    "memories": {
        "category": "TEXT NOT NULL DEFAULT 'semantic'", "confidence": "REAL NOT NULL DEFAULT 1",
        "source": "TEXT NOT NULL DEFAULT 'user'", "conversation": "INTEGER",
        "corrects": "INTEGER", "created": "TEXT", "superseded": "INTEGER NOT NULL DEFAULT 0",
        "vector": "TEXT", "embedding_model": "TEXT",
    },
    "errors": {
        "component": "TEXT NOT NULL DEFAULT 'app'", "severity": "TEXT NOT NULL DEFAULT 'error'",
        "trace": "TEXT", "conversation": "INTEGER", "task": "INTEGER", "metadata": "TEXT",
    },
}


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        if directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
            raise PermissionError(f"{directory} must be owned by the BudAI user with mode 0700")
        self.directory = directory
        self.path = directory / "bud.sqlite3"
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(fd).st_uid != os.getuid():
                raise PermissionError(f"{self.path} must be owned by the BudAI user")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            for table, columns in UPGRADES.items():
                existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
                if existing:
                    for name, definition in columns.items():
                        if name not in existing:
                            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def all(self, sql, args=()):
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, args)]

    def one(self, sql, args=()):
        with self.connect() as db:
            row = db.execute(sql, args).fetchone()
            return dict(row) if row is not None else None

    def run(self, sql, args=()):
        with self.connect() as db:
            return db.execute(sql, args).lastrowid

    def audit(self, account, action, detail=""):
        self.run("INSERT INTO audit(user_id,action,detail) VALUES (?,?,?)", (account, action, detail))

    def preferences(self, account):
        from .config import Preferences

        row = self.one("SELECT value FROM settings WHERE user_id=?", (account,))
        if row:
            return Preferences.model_validate_json(row["value"])
        import psutil

        available = psutil.virtual_memory().available
        context = 2048 if available < 8 * 1024**3 else 4096
        physical = psutil.cpu_count(logical=False) or len(os.sched_getaffinity(0))
        threads = max(1, min(physical, len(os.sched_getaffinity(0)), 512))
        return Preferences(context=context, threads=threads)

    def record(self, conversation, message):
        return self.run(
            "INSERT INTO messages(conversation_id,role,message,created) VALUES (?,?,?,CURRENT_TIMESTAMP)",
            (conversation, message["role"], json.dumps(message, ensure_ascii=False)),
        )
