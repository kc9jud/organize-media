"""Tests for ExifCache and NullExifCache (organize_media.py:62-279)."""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import pytest

from organize_media import ExifCache, NullExifCache


# ── _parse_exif_dt ────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "value,expected",
    [
        ("2020:06:15 12:30:45", datetime(2020, 6, 15, 12, 30, 45)),
        ("2020-06-15 12:30:45", datetime(2020, 6, 15, 12, 30, 45)),
        # Truncated to 19 chars — extra trailing junk dropped.
        ("2020:06:15 12:30:45 extra junk", datetime(2020, 6, 15, 12, 30, 45)),
        ("not a date", None),
        ("", None),
    ],
)
def test_parse_exif_dt(value: str, expected: datetime | None) -> None:
    assert ExifCache._parse_exif_dt(value) == expected


# ── _dt_from_image_exif ───────────────────────────────────────────────────────

def test_dt_from_image_exif_subifd(copy_fixture) -> None:
    path = copy_fixture("jpeg_subifd_2020.jpg")
    assert ExifCache._dt_from_image_exif(path) == datetime(2020, 6, 15, 12, 30, 45)


def test_dt_from_image_exif_ifd0_only(copy_fixture) -> None:
    path = copy_fixture("jpeg_ifd0_only_2021.jpg")
    assert ExifCache._dt_from_image_exif(path) == datetime(2021, 3, 10, 9, 15, 0)


def test_dt_from_image_exif_no_exif(copy_fixture) -> None:
    path = copy_fixture("jpeg_no_exif.jpg")
    assert ExifCache._dt_from_image_exif(path) is None


def test_dt_from_image_exif_cr2_exifread_fallback(copy_fixture) -> None:
    """Canon CR2 — Pillow may not parse; exifread fallback should succeed."""
    path = copy_fixture("raw_exifread.cr2")
    assert ExifCache._dt_from_image_exif(path) == datetime(2019, 11, 20, 17, 45, 30)


# ── _dt_from_video_metadata ───────────────────────────────────────────────────

def test_dt_from_video_metadata_mp4(copy_fixture) -> None:
    path = copy_fixture("video_hachoir_2022.mp4")
    assert ExifCache._dt_from_video_metadata(path) == datetime(2022, 8, 4, 14, 0, 0)


def test_dt_from_video_metadata_garbage(write_bytes) -> None:
    path = write_bytes("garbage.mp4", data=os.urandom(100))
    assert ExifCache._dt_from_video_metadata(path) is None


# ── _get_uncached ─────────────────────────────────────────────────────────────

def test_get_uncached_falls_back_to_mtime(copy_fixture) -> None:
    known_mtime = 1_600_000_000.0  # 2020-09-13 12:26:40 UTC
    path = copy_fixture("jpeg_no_exif.jpg", mtime=known_mtime)
    dt, source = ExifCache._get_uncached(path)
    assert dt == datetime.fromtimestamp(known_mtime)
    assert source == "mtime"


# ── ExifCache.get happy path + persistence ────────────────────────────────────

def test_exif_cache_get_happy_path_and_persists(caches, cache_db, copy_fixture) -> None:
    _, exif_cache, _, _ = caches
    path = copy_fixture("jpeg_subifd_2020.jpg")

    dt = exif_cache.get(path)
    assert dt == datetime(2020, 6, 15, 12, 30, 45)

    exif_cache.commit()

    # Open a fresh connection and confirm one row with matching dt.
    fresh = sqlite3.connect(str(cache_db))
    try:
        rows = fresh.execute("SELECT path, mtime, dt FROM exif_cache").fetchall()
    finally:
        fresh.close()

    assert len(rows) == 1
    row_path, row_mtime, row_dt = rows[0]
    assert row_path == str(path.resolve())
    assert row_mtime == path.stat().st_mtime
    assert datetime.fromisoformat(row_dt) == datetime(2020, 6, 15, 12, 30, 45)


# ── ExifCache.get cache hit ───────────────────────────────────────────────────

def test_exif_cache_get_hit_skips_extraction(caches, copy_fixture, monkeypatch) -> None:
    _, exif_cache, _, _ = caches
    path = copy_fixture("jpeg_subifd_2020.jpg")

    first = exif_cache.get(path)
    assert first == datetime(2020, 6, 15, 12, 30, 45)
    # SQLite default isolation hides uncommitted writes from sibling read
    # connections.  In production `commit()` runs at phase boundaries; here
    # we force it so the second `get` can see the row inserted by the first.
    exif_cache.commit()

    def boom(*args, **kwargs):
        pytest.fail("_get_uncached should not be called on a cache hit")

    monkeypatch.setattr(ExifCache, "_get_uncached", boom)

    second = exif_cache.get(path)
    assert second == datetime(2020, 6, 15, 12, 30, 45)


# ── get_with_source: cache hit returns 'exif', skips re-extraction ────────────

def test_get_with_source_cache_hit_skips_extraction(caches, copy_fixture, monkeypatch) -> None:
    _, exif_cache, _, _ = caches
    path = copy_fixture("jpeg_subifd_2020.jpg")

    first_dt, first_src = exif_cache.get_with_source(path)
    assert first_dt == datetime(2020, 6, 15, 12, 30, 45)
    assert first_src == "exif"
    exif_cache.commit()

    def boom(*args, **kwargs):
        pytest.fail("_get_uncached should not be called on a cache hit")

    monkeypatch.setattr(ExifCache, "_get_uncached", boom)

    second_dt, second_src = exif_cache.get_with_source(path)
    assert second_dt == datetime(2020, 6, 15, 12, 30, 45)
    assert second_src == "exif"


# ── get_with_source: mtime fallback is not cached ─────────────────────────────

def test_get_with_source_mtime_not_cached(caches, cache_db, copy_fixture) -> None:
    _, exif_cache, _, _ = caches
    path = copy_fixture("jpeg_no_exif.jpg")

    dt, source = exif_cache.get_with_source(path)
    assert source == "mtime"
    exif_cache.commit()

    fresh = sqlite3.connect(str(cache_db))
    try:
        row = fresh.execute(
            "SELECT COUNT(*) FROM exif_cache WHERE path = ?",
            (str(path.resolve()),),
        ).fetchone()
    finally:
        fresh.close()

    assert row[0] == 0, "mtime-fallback datetime must not be written to the cache"


# ── ExifCache.get stale (mtime changed) ───────────────────────────────────────

def test_exif_cache_get_stale_reextracts(caches, cache_db, make_jpeg, tmp_path) -> None:
    _, exif_cache, _, _ = caches
    target = tmp_path / "stale.jpg"

    # Write JPEG_A with dt=2020.
    make_jpeg(
        "stale.jpg",
        dt=datetime(2020, 1, 1, 0, 0, 0),
        tag="DateTimeOriginal",
        directory=tmp_path,
    )
    first = exif_cache.get(target)
    assert first == datetime(2020, 1, 1, 0, 0, 0)

    # Overwrite same path with JPEG_B (dt=2021). Touch mtime to a distinct value
    # to make sure cached_mtime != current mtime.
    make_jpeg(
        "stale.jpg",
        dt=datetime(2021, 5, 5, 5, 5, 5),
        tag="DateTimeOriginal",
        directory=tmp_path,
    )
    new_mtime = target.stat().st_mtime + 100.0
    os.utime(target, (new_mtime, new_mtime))

    second = exif_cache.get(target)
    assert second == datetime(2021, 5, 5, 5, 5, 5)

    exif_cache.commit()

    fresh = sqlite3.connect(str(cache_db))
    try:
        rows = fresh.execute(
            "SELECT mtime, dt FROM exif_cache WHERE path = ?",
            (str(target.resolve()),),
        ).fetchall()
    finally:
        fresh.close()

    assert len(rows) == 1
    cached_mtime, cached_dt = rows[0]
    assert cached_mtime == new_mtime
    assert datetime.fromisoformat(cached_dt) == datetime(2021, 5, 5, 5, 5, 5)


# ── commit() durability ───────────────────────────────────────────────────────

def test_commit_durability(caches, cache_db, copy_fixture) -> None:
    _, exif_cache, _, _ = caches
    path = copy_fixture("jpeg_subifd_2020.jpg")

    exif_cache.put(path, datetime(2020, 6, 15, 12, 30, 45))
    exif_cache.commit()

    fresh = sqlite3.connect(str(cache_db))
    try:
        (count,) = fresh.execute("SELECT COUNT(*) FROM exif_cache").fetchone()
    finally:
        fresh.close()

    assert count == 1


# ── close_read_conns ──────────────────────────────────────────────────────────

def test_close_read_conns(caches, copy_fixture) -> None:
    _, exif_cache, _, _ = caches
    path = copy_fixture("jpeg_subifd_2020.jpg")

    exif_cache.get(path)  # opens a per-thread read conn

    assert len(exif_cache._read_conns) >= 1
    exif_cache.close_read_conns()
    assert len(exif_cache._read_conns) == 0


# ── Thread safety ─────────────────────────────────────────────────────────────

def test_thread_safety_concurrent_gets(caches, cache_db, copy_fixture) -> None:
    from concurrent.futures import ThreadPoolExecutor

    _, exif_cache, _, _ = caches
    path = copy_fixture("jpeg_subifd_2020.jpg")
    expected = datetime(2020, 6, 15, 12, 30, 45)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(exif_cache.get, path) for _ in range(8)]
        results = [f.result() for f in futures]

    assert all(r == expected for r in results)

    exif_cache.commit()

    fresh = sqlite3.connect(str(cache_db))
    try:
        (count,) = fresh.execute("SELECT COUNT(*) FROM exif_cache").fetchone()
    finally:
        fresh.close()

    assert count == 1


# ── NullExifCache ─────────────────────────────────────────────────────────────

def test_null_exif_cache_matches_get_uncached_and_is_noop(
    null_caches, copy_fixture, tmp_path
) -> None:
    _, null_exif = null_caches
    path = copy_fixture("jpeg_subifd_2020.jpg")

    direct, source = ExifCache._get_uncached(path)
    via_null = null_exif.get(path)
    assert direct == via_null == datetime(2020, 6, 15, 12, 30, 45)
    assert source == "exif"

    # All write-side operations should be silent no-ops.
    null_exif.put(path, datetime(2020, 6, 15, 12, 30, 45))
    null_exif.commit()
    null_exif.close_read_conns()

    # No cache.sqlite should have been created anywhere in tmp_path.
    assert not (tmp_path / "cache.sqlite").exists()
    for p in tmp_path.rglob("*.sqlite"):
        pytest.fail(f"unexpected sqlite file created by NullExifCache: {p}")
