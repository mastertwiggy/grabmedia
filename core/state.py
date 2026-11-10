"""Stato per traccia su SQLite, per il resume.

Una riga per ``Track.key`` (``provider:provider_id``). Gli stati sono
``pending``, ``running``, ``done``, ``failed``, ``skipped``. ``match_hash``
identifica *cosa* è stato scaricato (provider audio, qualità, percorso): se
cambia, la traccia va rifatta anche se risulta ``done``.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from sources.base import Track

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"
STATUSES = (PENDING, RUNNING, DONE, FAILED, SKIPPED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    key         TEXT PRIMARY KEY,
    provider    TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    title       TEXT,
    artist      TEXT,
    match_hash  TEXT,
    status      TEXT NOT NULL,
    path        TEXT,
    error       TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tracks_status ON tracks(status);
"""


@dataclass(slots=True)
class TrackRecord:
    key: str
    provider: str
    provider_id: str
    title: str | None
    artist: str | None
    match_hash: str | None
    status: str
    path: str | None
    error: str | None
    attempts: int
    created_at: float
    updated_at: float


def match_hash(track: Track, audio_provider: str, quality: str, dest: Path | str) -> str:
    """Hash stabile del match: stessa traccia + stesso provider audio + stessa
    qualità + stessa destinazione -> stesso hash -> resume possibile."""
    payload = "\x1f".join([track.key, audio_provider, quality, str(dest)])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


class StateDB:
    """Accesso thread-safe (una connessione, un lock) allo stato delle tracce."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------ util
    def _row(self, row: sqlite3.Row | None) -> TrackRecord | None:
        return TrackRecord(**dict(row)) if row is not None else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "StateDB":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ read
    def get(self, key: str) -> TrackRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tracks WHERE key = ?", (key,)).fetchone()
        return self._row(row)

    def list(self, status: str | None = None) -> list[TrackRecord]:
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM tracks WHERE status = ? ORDER BY updated_at", (status,)
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM tracks ORDER BY updated_at").fetchall()
        return [self._row(r) for r in rows]  # type: ignore[misc]

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT status, COUNT(*) AS n FROM tracks GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    def is_done(self, track: Track, expected_hash: str) -> Path | None:
        """Percorso del file se la traccia è già ``done`` con lo stesso hash e
        il file esiste ancora; altrimenti ``None``."""
        rec = self.get(track.key)
        if rec is None or rec.status != DONE or rec.match_hash != expected_hash or not rec.path:
            return None
        path = Path(rec.path)
        return path if path.exists() else None

    # ----------------------------------------------------------------- write
    def upsert(self, track: Track, *, status: str = PENDING, match_hash: str | None = None) -> TrackRecord:
        if status not in STATUSES:
            raise ValueError(f"stato sconosciuto: {status}")
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO tracks (key, provider, provider_id, title, artist, match_hash,
                                    status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    title = excluded.title,
                    artist = excluded.artist,
                    match_hash = COALESCE(excluded.match_hash, tracks.match_hash),
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (track.key, track.provider, track.provider_id, track.title, track.artist,
                 match_hash, status, now, now),
            )
        return self.get(track.key)  # type: ignore[return-value]

    def mark(
        self,
        key: str,
        status: str,
        *,
        path: Path | str | None = None,
        error: str | None = None,
        match_hash: str | None = None,
        bump_attempts: bool = False,
    ) -> None:
        if status not in STATUSES:
            raise ValueError(f"stato sconosciuto: {status}")
        with self._lock:
            self._conn.execute(
                """
                UPDATE tracks SET
                    status = ?,
                    path = COALESCE(?, path),
                    error = ?,
                    match_hash = COALESCE(?, match_hash),
                    attempts = attempts + ?,
                    updated_at = ?
                WHERE key = ?
                """,
                (status, str(path) if path else None, error, match_hash,
                 1 if bump_attempts else 0, time.time(), key),
            )

    def reset_running(self) -> int:
        """All'avvio: le tracce rimaste ``running`` da un run interrotto tornano ``pending``."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tracks SET status = ?, updated_at = ? WHERE status = ?",
                (PENDING, time.time(), RUNNING),
            )
        return cur.rowcount

    def forget(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM tracks WHERE key = ?", (key,))
