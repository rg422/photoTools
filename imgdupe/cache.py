"""SQLite-backed signature cache so re-scans of a library are near instant."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from .hashing import ImageSignature

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signatures (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    -- Stored as hex text: a 64-bit unsigned hash overflows SQLite's signed INTEGER.
    phash TEXT NOT NULL,
    dhash TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    mode TEXT NOT NULL,
    fmt TEXT NOT NULL,
    sharpness REAL NOT NULL,
    colorfulness REAL NOT NULL,
    content_md5 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def default_cache_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
    directory = Path(base) / "imgdupe"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "signatures.db"


class SignatureCache:
    """Keyed on path plus size and mtime, so an edited file is re-analysed."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else default_cache_path()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        stored = self._conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        if stored is None:
            self._conn.execute("INSERT INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
        elif stored[0] != str(SCHEMA_VERSION):
            self._conn.execute("DELETE FROM signatures")
            self._conn.execute("UPDATE meta SET value=? WHERE key='schema'", (str(SCHEMA_VERSION),))
        self._conn.commit()

    def get(self, path: str, size: int, mtime: float) -> ImageSignature | None:
        row = self._conn.execute(
            "SELECT size, mtime, phash, dhash, width, height, mode, fmt,"
            " sharpness, colorfulness, content_md5 FROM signatures WHERE path = ?",
            (path,),
        ).fetchone()
        if row is None or row[0] != size or abs(row[1] - mtime) > 1e-6:
            return None
        return ImageSignature(
            phash=int(row[2], 16), dhash=int(row[3], 16), width=row[4], height=row[5],
            mode=row[6], fmt=row[7], sharpness=row[8], colorfulness=row[9],
            content_md5=row[10],
        )

    def put(self, path: str, size: int, mtime: float, sig: ImageSignature) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO signatures VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (path, size, mtime, f"{sig.phash:016x}", f"{sig.dhash:016x}", sig.width, sig.height,
             sig.mode, sig.fmt, sig.sharpness, sig.colorfulness, sig.content_md5),
        )

    def forget(self, path: str) -> None:
        self._conn.execute("DELETE FROM signatures WHERE path = ?", (path,))
        self._conn.commit()

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()
