"""Tests for `organize_media.find_duplicates`.

Covers organize_media.py:545-663 (the duplicate-detection map/reduce step).
"""

from __future__ import annotations

from datetime import datetime
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

import organize_media
from organize_media import find_duplicates, make_progress


# ── helpers ───────────────────────────────────────────────────────────────────

@pytest.fixture
def captured_console(monkeypatch) -> StringIO:
    """Replace organize_media.console with one that writes into a StringIO.

    `find_duplicates` calls module-level `console.print(...)`; patching the
    module attribute lets us inspect warnings without touching stdout.
    """
    buf = StringIO()
    new = Console(file=buf, force_terminal=False, width=200, soft_wrap=True)
    monkeypatch.setattr("organize_media.console", new)
    return buf


def _hash_cache(caches):
    """Pull HashCache out of the (hash, exif, conn, lock) caches fixture."""
    return caches[0]


# ── tests ─────────────────────────────────────────────────────────────────────

def test_single_file_group_short_circuited(caches, pool, write_bytes, monkeypatch, captured_console):
    """One file in its group, no excludes → no hashing, empty skipped set."""
    hash_cache = _hash_cache(caches)

    # Spy: if any hashing occurs, fail loudly.
    def boom_get(*args, **kwargs):
        raise AssertionError("hash_cache.get should not be called for short-circuit")

    def boom_hash(path):
        raise AssertionError("hash_cache._hash_file should not be called for short-circuit")

    monkeypatch.setattr(hash_cache, "get", boom_get)
    monkeypatch.setattr(hash_cache, "_hash_file", boom_hash)

    p = write_bytes("solo.bin", b"alone")
    dt = datetime(2024, 1, 1, 12, 0, 0)
    dt_map = {(dt, p.stat().st_size): [p]}

    with make_progress(bytes=True) as progress:
        skipped = find_duplicates(
            dt_map, progress,
            exclude_paths=set(), pool=pool, hash_cache=hash_cache,
        )

    assert skipped == set()


def test_two_identical_files_later_skipped(caches, pool, write_bytes, captured_console):
    """Same content, same (dt, size) → lex-later path is skipped."""
    hash_cache = _hash_cache(caches)
    payload = b"identical-content-xyz" * 4

    # Names chosen so sort order is unambiguous.
    p_first  = write_bytes("a_first.bin",  payload)
    p_second = write_bytes("z_second.bin", payload)

    dt = datetime(2024, 6, 1, 9, 30, 0)
    size = p_first.stat().st_size
    assert size == p_second.stat().st_size

    dt_map = {(dt, size): [p_second, p_first]}  # unsorted on purpose

    with make_progress(bytes=True) as progress:
        skipped = find_duplicates(
            dt_map, progress,
            exclude_paths=set(), pool=pool, hash_cache=hash_cache,
        )

    # sorted([p_first, p_second]) → p_first wins (alphabetically first),
    # p_second is the duplicate that gets skipped.
    assert skipped == {p_second}

    out = captured_console.getvalue()
    assert "identical file skipped" in out
    assert p_first.name in out


def test_two_distinct_files_same_dt_size_kept_with_warning(caches, pool, write_bytes, captured_console):
    """Same (dt, size) but different bytes → both kept, warning emitted."""
    hash_cache = _hash_cache(caches)

    bytes_a = b"A" * 100
    bytes_b = b"B" * 100
    p_a = write_bytes("a.bin", bytes_a)
    p_b = write_bytes("b.bin", bytes_b)
    assert p_a.stat().st_size == p_b.stat().st_size == 100

    dt = datetime(2023, 3, 14, 15, 9, 26)
    dt_map = {(dt, 100): [p_a, p_b]}

    with make_progress(bytes=True) as progress:
        skipped = find_duplicates(
            dt_map, progress,
            exclude_paths=set(), pool=pool, hash_cache=hash_cache,
        )

    assert skipped == set()

    out = captured_console.getvalue()
    assert "share timestamp" in out
    # The dt label should also appear in the warning.
    assert "2023-03-14 15:09:26" in out


def test_source_matches_exclude_is_skipped(caches, pool, write_bytes, tmp_path, captured_console):
    """A source whose content matches an excluded file is skipped."""
    hash_cache = _hash_cache(caches)

    payload = b"shared-payload-content"
    src  = write_bytes("source.bin",   payload, into=tmp_path / "src")
    excl = write_bytes("excluded.bin", payload, into=tmp_path / "excl")

    dt = datetime(2022, 12, 25, 8, 0, 0)
    size = src.stat().st_size
    assert size == excl.stat().st_size

    dt_map = {(dt, size): [src, excl]}

    with make_progress(bytes=True) as progress:
        skipped = find_duplicates(
            dt_map, progress,
            exclude_paths={excl}, pool=pool, hash_cache=hash_cache,
        )

    assert skipped == {src}

    out = captured_console.getvalue()
    assert "matches excluded file" in out
    assert excl.name in out


def test_none_dt_sentinel_bypasses_cache_get(caches, pool, write_bytes, monkeypatch, captured_console):
    """(None, size) groups should call _hash_file directly, never .get()."""
    hash_cache = _hash_cache(caches)

    payload = b"no-exif-payload" * 3
    p1 = write_bytes("none_a.bin", payload)
    p2 = write_bytes("none_b.bin", payload)
    size = p1.stat().st_size

    get_calls: list = []
    hash_calls: list[Path] = []

    orig_get = hash_cache.get

    def spy_get(*args, **kwargs):
        get_calls.append((args, kwargs))
        return orig_get(*args, **kwargs)

    orig_hash = hash_cache._hash_file

    def spy_hash(path):
        hash_calls.append(path)
        return orig_hash(path)

    monkeypatch.setattr(hash_cache, "get", spy_get)
    monkeypatch.setattr(hash_cache, "_hash_file", spy_hash)

    dt_map = {(None, size): [p1, p2]}

    with make_progress(bytes=True) as progress:
        skipped = find_duplicates(
            dt_map, progress,
            exclude_paths=set(), pool=pool, hash_cache=hash_cache,
        )

    assert get_calls == []  # cache.get never invoked for None-dt groups
    assert len(hash_calls) == 2  # both files hashed via _hash_file
    # Identical content → later path skipped.
    assert skipped == {max(p1, p2)}


def test_none_dt_distinct_files_both_kept(caches, pool, write_bytes, captured_console):
    """(None, size) with distinct content → no skip, no SHA-1 collision."""
    hash_cache = _hash_cache(caches)

    p_a = write_bytes("none_distinct_a.bin", b"X" * 50)
    p_b = write_bytes("none_distinct_b.bin", b"Y" * 50)
    assert p_a.stat().st_size == p_b.stat().st_size == 50

    dt_map = {(None, 50): [p_a, p_b]}

    with make_progress(bytes=True) as progress:
        skipped = find_duplicates(
            dt_map, progress,
            exclude_paths=set(), pool=pool, hash_cache=hash_cache,
        )

    assert skipped == set()
    # The "share timestamp" warning uses "unknown (EXIF unreadable)" for None dt.
    out = captured_console.getvalue()
    assert "unknown (EXIF unreadable)" in out


def test_parallel_groups(caches, pool, write_bytes, captured_console):
    """Four independent dup-groups processed in parallel; one skip per group."""
    hash_cache = _hash_cache(caches)

    expected_skipped: set[Path] = set()
    dt_map: dict = {}

    for i in range(4):
        payload = f"group-{i}-content-{'q' * (i + 1)}".encode() * 4
        kept = write_bytes(f"g{i}_keep.bin", payload)
        dup  = write_bytes(f"g{i}_zdup.bin", payload)  # lex-later → skipped
        size = kept.stat().st_size
        dt = datetime(2020, 1, 1, 0, 0, i)  # distinct dt per group
        dt_map[(dt, size)] = [dup, kept]
        # sorted([kept, dup]) → kept first (g{i}_keep < g{i}_zdup), so dup is skipped.
        expected_skipped.add(dup)

    with make_progress(bytes=True) as progress:
        skipped = find_duplicates(
            dt_map, progress,
            exclude_paths=set(), pool=pool, hash_cache=hash_cache,
        )

    assert skipped == expected_skipped
    assert len(skipped) == 4
