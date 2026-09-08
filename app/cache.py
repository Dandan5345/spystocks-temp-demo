from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

CACHE_DB = Path(__file__).resolve().parents[1] / "data" / "sec-cache.sqlite3"

_connection: sqlite3.Connection | None = None
# to_thread runs these on arbitrary pool threads; one sqlite connection is not safe to
# use from several at once, so every statement goes through this lock. The work under it
# is a single small row, which is nothing next to the network calls it saves.
_db_lock = threading.Lock()
_locks_lock = asyncio.Lock()
_key_locks: dict[str, asyncio.Lock] = {}


def _connect() -> sqlite3.Connection:
    global _connection
    if _connection is None:
        CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
        _connection = sqlite3.connect(CACHE_DB, check_same_thread=False)
        # WAL keeps reads from blocking the writes happening on other filings.
        _connection.execute("PRAGMA journal_mode=WAL")
        _connection.execute("PRAGMA synchronous=NORMAL")
        _connection.execute(
            "CREATE TABLE IF NOT EXISTS entries ("
            "  key TEXT PRIMARY KEY,"
            "  value TEXT NOT NULL,"
            "  expires_at REAL"
            ")"
        )
        _connection.commit()
    return _connection


def _read(key: str) -> Any | None:
    try:
        with _db_lock:
            row = _connect().execute(
                "SELECT value, expires_at FROM entries WHERE key = ?", (key,)
            ).fetchone()
    except sqlite3.Error:
        # A broken cache must never break a lookup; fall through to a live fetch.
        return None
    if not row:
        return None
    value, expires_at = row
    if expires_at is not None and expires_at < time.time():
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None


def _write(key: str, value: Any, ttl: float | None) -> None:
    expires_at = time.time() + ttl if ttl is not None else None
    try:
        payload = json.dumps(value)
    except (TypeError, ValueError):
        return
    try:
        with _db_lock:
            connection = _connect()
            connection.execute(
                "INSERT INTO entries (key, value, expires_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, expires_at = excluded.expires_at",
                (key, payload, expires_at),
            )
            connection.commit()
    except sqlite3.Error:
        return


async def get(key: str) -> Any | None:
    return await asyncio.to_thread(_read, key)


async def set(key: str, value: Any, *, ttl: float | None = None) -> None:
    await asyncio.to_thread(_write, key, value, ttl)


async def key_lock(key: str) -> asyncio.Lock:
    """One lock per cache key, so N concurrent misses become one fetch, not N."""
    async with _locks_lock:
        lock = _key_locks.get(key)
        if lock is None:
            lock = _key_locks[key] = asyncio.Lock()
        return lock


async def cached(key: str, producer, *, ttl: float | None = None) -> Any:
    """Return the cached value for `key`, otherwise await `producer()` and store it.

    `producer` returning None is not cached, so a transient SEC failure doesn't get
    remembered as an answer.
    """
    hit = await get(key)
    if hit is not None:
        return hit
    async with await key_lock(key):
        hit = await get(key)
        if hit is not None:
            return hit
        value = await producer()
        if value is not None:
            await set(key, value, ttl=ttl)
        return value
