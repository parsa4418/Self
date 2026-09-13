"""Durable backup/restore for the app's existing SQLite database.

The application has a large, SQLite-specific data layer.  Rather than rewriting
hundreds of SQL statements to PostgreSQL (which would be risky), this module
uses Aiven PostgreSQL as the durable store for consistent SQLite snapshots.
The app continues using SQLite locally, while Aiven protects the data across
Render deploys/restarts.
"""
from __future__ import annotations

import base64
import hashlib
import os
import sqlite3
import threading
import time
from pathlib import Path

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None


_BACKUP_INTERVAL = max(30, int(os.getenv("REMOTE_DB_BACKUP_INTERVAL", "60")))
_LOCK = threading.Lock()
_STOP = threading.Event()


def _dsn() -> str:
    return os.getenv("DATABASE_URL", "").strip()


def enabled() -> bool:
    return bool(_dsn()) and psycopg is not None and os.getenv("REMOTE_DB_BACKUP_ENABLED", "1") != "0"


def _ensure_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_sqlite_snapshots (
            id BIGSERIAL PRIMARY KEY,
            database_name TEXT NOT NULL UNIQUE,
            sqlite_bytes BYTEA NOT NULL,
            sha256 TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    conn.commit()


def _snapshot_bytes(db_path: Path) -> bytes:
    # SQLite's online backup API gives a consistent snapshot even while the bot
    # is serving requests.
    source = sqlite3.connect(str(db_path), timeout=30)
    try:
        target = sqlite3.connect(":memory:")
        try:
            source.backup(target)
            return b"".join(target.iterdump().__str__().encode() for _ in []) or _serialize(target)
        finally:
            target.close()
    finally:
        source.close()


def _serialize(conn: sqlite3.Connection) -> bytes:
    # A portable SQLite binary image is obtained through VACUUM INTO.
    import tempfile
    fd, name = tempfile.mkstemp(prefix="sqlite-snapshot-", suffix=".db")
    os.close(fd)
    try:
        conn.execute("VACUUM INTO ?", (name,))
        return Path(name).read_bytes()
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass


def backup(db_path: str | Path) -> bool:
    if not enabled():
        return False
    path = Path(db_path)
    if not path.is_file():
        return False
    with _LOCK:
        try:
            payload = _snapshot_bytes(path)
            digest = hashlib.sha256(payload).hexdigest()
            with psycopg.connect(_dsn(), connect_timeout=10) as conn:
                _ensure_table(conn)
                conn.execute(
                    """
                    INSERT INTO app_sqlite_snapshots
                        (database_name, sqlite_bytes, sha256, created_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (database_name) DO UPDATE SET
                        sqlite_bytes = EXCLUDED.sqlite_bytes,
                        sha256 = EXCLUDED.sha256,
                        created_at = NOW()
                    """,
                    (path.name, payload, digest),
                )
                conn.commit()
            return True
        except Exception as exc:
            print(f"⚠️ Aiven DB backup failed: {type(exc).__name__}: {exc}")
            return False


def restore(db_path: str | Path) -> bool:
    if not enabled():
        return False
    path = Path(db_path)
    with _LOCK:
        try:
            with psycopg.connect(_dsn(), connect_timeout=10) as conn:
                _ensure_table(conn)
                row = conn.execute(
                    "SELECT sqlite_bytes, sha256 FROM app_sqlite_snapshots WHERE database_name=%s",
                    (path.name,),
                ).fetchone()
            if not row:
                return False
            payload, expected = bytes(row[0]), str(row[1])
            if hashlib.sha256(payload).hexdigest() != expected:
                raise RuntimeError("Aiven snapshot checksum mismatch")

            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".restore.tmp")
            tmp.write_bytes(payload)
            # Validate before replacing the live database.
            check = sqlite3.connect(str(tmp))
            try:
                check.execute("PRAGMA integrity_check").fetchone()
            finally:
                check.close()
            os.replace(tmp, path)
            return True
        except Exception as exc:
            print(f"⚠️ Aiven DB restore failed: {type(exc).__name__}: {exc}")
            return False


def start_periodic_backup(db_path: str | Path) -> threading.Thread | None:
    if not enabled():
        return None
    path = Path(db_path)

    def worker() -> None:
        # Give the application a moment to finish initial schema migration.
        time.sleep(5)
        while not _STOP.is_set():
            backup(path)
            _STOP.wait(_BACKUP_INTERVAL)

    thread = threading.Thread(target=worker, name="aiven-db-backup", daemon=True)
    thread.start()
    return thread


def stop() -> None:
    _STOP.set()
