"""Tests for HashCache and NullCache (organize_media.py:282-409)."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytest

from organize_media import HashCache, NullCache


# ── 1. _hash_file known values ────────────────────────────────────────────────

def test_hash_file_empty_bytes(write_bytes):
    path = write_bytes("empty.bin", data=b"")
    assert HashCache._hash_file(path) == "da39a3ee5e6b4b0d3255bfef95601890afd80709"


def test_hash_file_hello(write_bytes):
    path = write_bytes("hello.bin", data=b"hello")
    assert HashCache._hash_file(path) == "aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d"


# ── 2. HashCache.get happy path + persistence ─────────────────────────────────

def test_get_happy_path_and_persists(caches, cache_db, write_bytes):
    hash_cache, _exif, _wc, _wl = caches
    data = b"happy path bytes"
    path = write_bytes("happy.bin", data=data)
    dt = datetime(2020, 1, 2, 3, 4, 5)
    size = path.stat().st_size

    expected = hashlib.sha1(data).hexdigest()
    got = hash_cache.get(path, dt, size)
    assert got == expected

    hash_cache.commit()

    # Open a fresh independent connection and verify one row.
    conn = sqlite3.connect(str(cache_db))
    try:
        rows = conn.execute(
            "SELECT path, sha1, size, dt FROM hash_cache"
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    db_path, db_sha1, db_size, db_dt = rows[0]
    assert db_path == str(path.resolve())
    assert db_sha1 == expected
    assert db_size == size
    assert db_dt == dt.isoformat()


# ── 3. Cache hit: second call must not invoke _hash_file ──────────────────────

def test_cache_hit_does_not_rehash(caches, write_bytes, monkeypatch):
    hash_cache, _exif, _wc, _wl = caches
    data = b"cached please"
    path = write_bytes("cached.bin", data=data)
    dt = datetime(2021, 6, 7, 8, 9, 10)
    size = path.stat().st_size

    first = hash_cache.get(path, dt, size)
    expected = hashlib.sha1(data).hexdigest()
    assert first == expected
    # Force commit so the second `get`'s sibling read connection can see the
    # row.  Without this the second call misses the cache and would re-hash.
    hash_cache.commit()

    def boom(_p):
        raise AssertionError("_hash_file should not be called on cache hit")

    monkeypatch.setattr("organize_media.HashCache._hash_file", staticmethod(boom))

    second = hash_cache.get(path, dt, size)
    assert second == expected


# ── 4. Stale by size: re-hash when size changes ───────────────────────────────

def test_stale_by_size_rehashes(caches, write_bytes):
    hash_cache, _exif, _wc, _wl = caches
    path = write_bytes("stale_size.bin", data=b"a" * 100)
    dt = datetime(2022, 1, 1, 0, 0, 0)

    first = hash_cache.get(path, dt, size=100)
    assert first == hashlib.sha1(b"a" * 100).hexdigest()

    # Replace contents with different bytes AND different size.
    new_data = b"b" * 200
    path.write_bytes(new_data)

    second = hash_cache.get(path, dt, size=200)
    assert second == hashlib.sha1(new_data).hexdigest()
    assert second != first


# ── 5. Stale by dt: re-hash when dt changes ───────────────────────────────────

def test_stale_by_dt_rehashes(caches, write_bytes, monkeypatch):
    hash_cache, _exif, _wc, _wl = caches
    data = b"timestamp drift"
    path = write_bytes("stale_dt.bin", data=data)
    size = path.stat().st_size
    dt1 = datetime(2023, 3, 3, 3, 3, 3)
    dt2 = datetime(2024, 4, 4, 4, 4, 4)
    expected = hashlib.sha1(data).hexdigest()

    calls: list[Path] = []
    orig = HashCache._hash_file

    def spy(p):
        calls.append(p)
        return orig(p)

    monkeypatch.setattr("organize_media.HashCache._hash_file", staticmethod(spy))

    first = hash_cache.get(path, dt1, size)
    assert first == expected
    assert len(calls) == 1

    second = hash_cache.get(path, dt2, size)
    assert second == expected
    # Stale dt forces re-hash: spy should be invoked a second time.
    assert len(calls) == 2


# ── 6. Large file chunked read ────────────────────────────────────────────────

def test_large_file_chunked_read(write_bytes):
    payload = os.urandom(1 << 20)  # 1 MiB
    path = write_bytes("big.bin", data=payload)
    assert HashCache._hash_file(path) == hashlib.sha1(payload).hexdigest()


# ── 7. Thread safety: concurrent get on same path ─────────────────────────────

def test_thread_safety_same_path(caches, cache_db, write_bytes):
    hash_cache, _exif, _wc, _wl = caches
    data = b"concurrent" * 1000
    path = write_bytes("concurrent.bin", data=data)
    dt = datetime(2025, 5, 5, 5, 5, 5)
    size = path.stat().st_size
    expected = hashlib.sha1(data).hexdigest()

    results: list[str] = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        return hash_cache.get(path, dt, size)

    with ThreadPoolExecutor(max_workers=8) as p:
        futures = [p.submit(worker) for _ in range(8)]
        for f in futures:
            results.append(f.result())

    assert results == [expected] * 8

    hash_cache.commit()

    conn = sqlite3.connect(str(cache_db))
    try:
        rows = conn.execute(
            "SELECT path, sha1 FROM hash_cache"
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    assert rows[0] == (str(path.resolve()), expected)


# ── 8. NullCache: no db, hashes fresh, no-op writes ───────────────────────────

def test_null_cache_behaviour(null_caches, cache_db, write_bytes):
    null_hash, _null_exif = null_caches
    data = b"null cache content"
    path = write_bytes("null.bin", data=data)
    dt = datetime(2026, 6, 6, 6, 6, 6)
    size = path.stat().st_size
    expected = hashlib.sha1(data).hexdigest()

    # get() returns the real hash.
    assert null_hash.get(path, dt, size) == expected
    assert null_hash.get(path, dt, size) == expected

    # No db file was created.
    assert not cache_db.exists()

    # put / commit / close_read_conns are no-ops (do not raise).
    null_hash.put(path, expected, dt, size)
    null_hash.commit()
    null_hash.close_read_conns()

    # Still no db.
    assert not cache_db.exists()
