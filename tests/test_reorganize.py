"""Tests for organize_media.reorganize and its helpers (lines 858-1017).

Covers _correct_path, _file_is_correctly_placed, and reorganize().
"""

from __future__ import annotations

import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Callable

import pytest
from rich.console import Console

import organize_media
from organize_media import (
    ExifCache,
    HashCache,
    _correct_path,
    _file_is_correctly_placed,
    reorganize,
)


DT = datetime(2020, 6, 15, 12, 30, 45)


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def captured_console(monkeypatch: pytest.MonkeyPatch) -> StringIO:
    """Replace organize_media.console with a Console writing to a StringIO."""
    buf = StringIO()
    fake = Console(file=buf, force_terminal=False, width=200, record=False)
    monkeypatch.setattr(organize_media, "console", fake)
    return buf


# ── helpers ──────────────────────────────────────────────────────────────────

def place_at(
    make_jpeg: Callable[..., Path],
    dest_root: Path,
    dt: datetime,
    *,
    subpath: Path | str | None = None,
    nnnn: int = 1,
    stem_dt: datetime | None = None,
    ext: str = ".jpg",
) -> Path:
    """Create a JPEG with EXIF=dt, placed at a chosen location inside dest_root.

    By default the file is placed at its canonical month dir with the canonical
    stem.  Pass `subpath` (relative to dest_root) to override the directory and
    `stem_dt` to override the stem prefix.
    """
    stem_source = stem_dt if stem_dt is not None else dt
    stem_base = stem_source.strftime("%Y-%m-%d %H-%M-%S")
    filename = f"{stem_base} {nnnn:04d}{ext}"

    if subpath is None:
        target_dir = dest_root / dt.strftime("%Y") / dt.strftime("%m")
    else:
        target_dir = dest_root / Path(subpath)
    target_dir.mkdir(parents=True, exist_ok=True)

    # make_jpeg writes into tmp_path by default; build there, then move into place.
    tmp_path = make_jpeg(f"_stage_{stem_base}_{nnnn:04d}{ext}", dt=dt)
    target = target_dir / filename
    shutil.move(str(tmp_path), str(target))
    return target


# ── _correct_path ────────────────────────────────────────────────────────────

def test_correct_path_shape(tmp_path: Path) -> None:
    """_correct_path returns (YYYY/MM dir, 'YYYY-MM-DD HH-MM-SS' stem)."""
    month_dir, stem_base = _correct_path(tmp_path, DT, tmp_path / "anything.jpg")
    assert month_dir == tmp_path / "2020" / "06"
    assert stem_base == "2020-06-15 12-30-45"


# ── _file_is_correctly_placed ────────────────────────────────────────────────

@pytest.mark.parametrize(
    "relpath, expected",
    [
        ("2020/06/2020-06-15 12-30-45 0001.jpg", True),
        ("2020/06/2020-06-15 12-30-45 0042.jpg", True),
        ("2020/07/2020-06-15 12-30-45 0001.jpg", False),  # wrong month dir
        ("2020/06/2021-06-15 12-30-45 0001.jpg", False),  # wrong stem prefix
    ],
)
def test_file_is_correctly_placed(tmp_path: Path, relpath: str, expected: bool) -> None:
    path = tmp_path / relpath
    assert _file_is_correctly_placed(path, tmp_path, DT) is expected


# ── reorganize: all correctly placed ─────────────────────────────────────────

def test_reorganize_all_correctly_placed(
    dest: Path,
    make_jpeg,
    null_caches,
    pool: ThreadPoolExecutor,
    captured_console: StringIO,
) -> None:
    """When all files are already in their canonical locations, nothing moves."""
    hash_cache, exif_cache = null_caches

    dt1 = datetime(2020, 6, 15, 12, 30, 45)
    dt2 = datetime(2021, 3,  4,  9, 10, 11)
    p1 = place_at(make_jpeg, dest, dt1)
    p2 = place_at(make_jpeg, dest, dt2)

    snapshot = {p1: p1.read_bytes(), p2: p2.read_bytes()}

    reorganize(
        dest,
        dry_run=False,
        verbose=False,
        pool=pool,
        hash_cache=hash_cache,
        exif_cache=exif_cache,
    )

    assert p1.exists() and p2.exists()
    assert p1.read_bytes() == snapshot[p1]
    assert p2.read_bytes() == snapshot[p2]

    out = captured_console.getvalue()
    assert "already correct" in out
    assert "2 already correct" in out or "All 2 file(s) are correctly placed" in out


# ── reorganize: wrong month dir ──────────────────────────────────────────────

def test_reorganize_wrong_month_dir(
    dest: Path,
    make_jpeg,
    null_caches,
    pool: ThreadPoolExecutor,
    captured_console: StringIO,
) -> None:
    """A file in the wrong YYYY/MM directory is moved to the correct one."""
    hash_cache, exif_cache = null_caches

    placed = place_at(make_jpeg, dest, DT, subpath="2019/01")
    assert placed == dest / "2019" / "01" / "2020-06-15 12-30-45 0001.jpg"

    reorganize(
        dest,
        dry_run=False,
        verbose=False,
        pool=pool,
        hash_cache=hash_cache,
        exif_cache=exif_cache,
    )

    expected = dest / "2020" / "06" / "2020-06-15 12-30-45 0001.jpg"
    assert expected.exists()
    assert not placed.exists()
    # Empty immediate parent of the moved file is pruned.
    # `reorganize` only prunes the direct `src.parent` of moved files; it does
    # not walk up to also clean grandparents (`dest/2019`), so that level is
    # left behind even when empty.
    assert not (dest / "2019" / "01").exists()


# ── reorganize: right dir, wrong stem ────────────────────────────────────────

def test_reorganize_wrong_stem(
    dest: Path,
    make_jpeg,
    null_caches,
    pool: ThreadPoolExecutor,
    captured_console: StringIO,
) -> None:
    """A file in the right month dir but with the wrong stem prefix is renamed."""
    hash_cache, exif_cache = null_caches

    wrong_stem_dt = datetime(2020, 6, 14, 11, 22, 33)
    placed = place_at(make_jpeg, dest, DT, stem_dt=wrong_stem_dt)
    # Lives in 2020/06 (correct dir) but stem prefix is 2020-06-14...
    assert placed.parent == dest / "2020" / "06"
    assert placed.name.startswith("2020-06-14 11-22-33")

    reorganize(
        dest,
        dry_run=False,
        verbose=False,
        pool=pool,
        hash_cache=hash_cache,
        exif_cache=exif_cache,
    )

    expected = dest / "2020" / "06" / "2020-06-15 12-30-45 0001.jpg"
    assert expected.exists()
    assert not placed.exists()


# ── reorganize: timezone-offset stems are left alone ─────────────────────────

def test_reorganize_tz_offset_stem_not_moved(
    dest: Path,
    make_jpeg,
    null_caches,
    pool: ThreadPoolExecutor,
    captured_console: StringIO,
) -> None:
    """File whose stem differs from EXIF by a clean TZ offset is not moved."""
    hash_cache, exif_cache = null_caches

    tz_stem_dt = DT - timedelta(hours=5)  # 5h before EXIF — plausible UTC offset
    placed = place_at(make_jpeg, dest, DT, stem_dt=tz_stem_dt)
    original_location = placed

    reorganize(
        dest,
        dry_run=False,
        verbose=False,
        pool=pool,
        hash_cache=hash_cache,
        exif_cache=exif_cache,
    )

    assert original_location.exists()


def test_reorganize_tz_offset_plus_drift_not_moved(
    dest: Path,
    make_jpeg,
    null_caches,
    pool: ThreadPoolExecutor,
    captured_console: StringIO,
) -> None:
    """File whose stem differs from EXIF by a TZ offset plus small drift is not moved."""
    hash_cache, exif_cache = null_caches

    # 5h offset + 3s drift — within tolerance
    tz_stem_dt = DT - timedelta(hours=5, seconds=3)
    placed = place_at(make_jpeg, dest, DT, stem_dt=tz_stem_dt)
    original_location = placed

    reorganize(
        dest,
        dry_run=False,
        verbose=False,
        pool=pool,
        hash_cache=hash_cache,
        exif_cache=exif_cache,
    )

    assert original_location.exists()


def test_reorganize_non_tz_offset_stem_is_moved(
    dest: Path,
    make_jpeg,
    null_caches,
    pool: ThreadPoolExecutor,
    captured_console: StringIO,
) -> None:
    """File whose stem differs from EXIF by a non-TZ-shaped offset is still moved."""
    hash_cache, exif_cache = null_caches

    # 7-minute offset — not a multiple of 15 min, not a TZ offset
    non_tz_stem_dt = DT + timedelta(minutes=7)
    placed = place_at(make_jpeg, dest, DT, stem_dt=non_tz_stem_dt)

    reorganize(
        dest,
        dry_run=False,
        verbose=False,
        pool=pool,
        hash_cache=hash_cache,
        exif_cache=exif_cache,
    )

    expected = dest / "2020" / "06" / "2020-06-15 12-30-45 0001.jpg"
    assert expected.exists()
    assert not placed.exists()


# ── reorganize: non-empty old dir is not pruned ──────────────────────────────

def test_reorganize_non_empty_old_dir_not_pruned(
    dest: Path,
    make_jpeg,
    null_caches,
    pool: ThreadPoolExecutor,
    captured_console: StringIO,
) -> None:
    """A non-empty source dir (after the move) must survive prune."""
    hash_cache, exif_cache = null_caches

    # Misplaced: actual EXIF dt = DT (2020-06-15), placed in 2019/01
    misplaced = place_at(make_jpeg, dest, DT, subpath="2019/01")

    # A second file legitimately belongs in 2019/01 — created via canonical placement
    canonical_2019 = datetime(2019, 1, 20, 8, 0, 0)
    canonical_neighbor = place_at(make_jpeg, dest, canonical_2019)
    assert canonical_neighbor.parent == dest / "2019" / "01"

    reorganize(
        dest,
        dry_run=False,
        verbose=False,
        pool=pool,
        hash_cache=hash_cache,
        exif_cache=exif_cache,
    )

    # Misplaced one was moved
    expected_moved = dest / "2020" / "06" / "2020-06-15 12-30-45 0001.jpg"
    assert expected_moved.exists()
    assert not misplaced.exists()
    # Neighbor still in place
    assert canonical_neighbor.exists()
    # The 2019/01 dir survives because it's still non-empty
    assert (dest / "2019" / "01").is_dir()
    assert (dest / "2019").is_dir()


# ── reorganize: dry run ──────────────────────────────────────────────────────

def test_reorganize_dry_run(
    dest: Path,
    make_jpeg,
    null_caches,
    pool: ThreadPoolExecutor,
    captured_console: StringIO,
) -> None:
    """dry_run=True moves nothing and prunes no directories."""
    hash_cache, exif_cache = null_caches

    placed = place_at(make_jpeg, dest, DT, subpath="2019/01")
    original_bytes = placed.read_bytes()

    reorganize(
        dest,
        dry_run=True,
        verbose=False,
        pool=pool,
        hash_cache=hash_cache,
        exif_cache=exif_cache,
    )

    # File untouched at original location
    assert placed.exists()
    assert placed.read_bytes() == original_bytes
    # No move target
    assert not (dest / "2020" / "06" / "2020-06-15 12-30-45 0001.jpg").exists()
    # Old dir not pruned
    assert (dest / "2019" / "01").is_dir()

    out = captured_console.getvalue()
    assert "DRY RUN" in out


# ── reorganize: EXIF-failed file ─────────────────────────────────────────────

def test_reorganize_exif_failure(
    dest: Path,
    make_jpeg,
    null_caches,
    pool: ThreadPoolExecutor,
    captured_console: StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ExifCache.get raises for a file, reorganize leaves it untouched and
    reports an error."""
    hash_cache, exif_cache = null_caches

    placed = place_at(make_jpeg, dest, DT, subpath="2019/01")
    original_location = placed
    original_bytes = placed.read_bytes()

    boom_target = str(placed.resolve())

    real_get_with_source = exif_cache.get_with_source

    def fake_get_with_source(path: Path) -> tuple[datetime, str]:
        if str(path.resolve()) == boom_target:
            raise RuntimeError("synthetic EXIF failure")
        return real_get_with_source(path)

    monkeypatch.setattr(exif_cache, "get_with_source", fake_get_with_source)

    reorganize(
        dest,
        dry_run=False,
        verbose=False,
        pool=pool,
        hash_cache=hash_cache,
        exif_cache=exif_cache,
    )

    # File untouched
    assert original_location.exists()
    assert original_location.read_bytes() == original_bytes
    # Did not get moved elsewhere
    assert not (dest / "2020" / "06" / "2020-06-15 12-30-45 0001.jpg").exists()

    out = captured_console.getvalue()
    assert "ERROR reading" in out
    assert "synthetic EXIF failure" in out


# ── reorganize: cache rows updated to new path ───────────────────────────────

def test_reorganize_cache_updated_on_move(
    dest: Path,
    make_jpeg,
    caches,
    pool: ThreadPoolExecutor,
    captured_console: StringIO,
    cache_db: Path,
) -> None:
    """After moving a misplaced file, cache rows exist for the new path.

    Note: organize_media.reorganize only `put`s the new path. Old rows pointing
    at the original (now-missing) path are not asserted to be deleted — the
    code may leave them in place. We don't assert deletion here.
    """
    hash_cache, exif_cache, write_conn, _write_lock = caches

    placed = place_at(make_jpeg, dest, DT, subpath="2019/01")
    placed_resolved = str(placed.resolve())

    reorganize(
        dest,
        dry_run=False,
        verbose=False,
        pool=pool,
        hash_cache=hash_cache,
        exif_cache=exif_cache,
    )

    expected_new = dest / "2020" / "06" / "2020-06-15 12-30-45 0001.jpg"
    assert expected_new.exists()
    new_resolved = str(expected_new.resolve())

    # Use a fresh read connection — the cache fixture's read conns may be
    # bound to threads we don't control here.
    conn = sqlite3.connect(str(cache_db))
    try:
        exif_row = conn.execute(
            "SELECT path, dt FROM exif_cache WHERE path = ?", (new_resolved,)
        ).fetchone()
        hash_row = conn.execute(
            "SELECT path, sha1, size, dt FROM hash_cache WHERE path = ?", (new_resolved,)
        ).fetchone()

        assert exif_row is not None, "expected exif_cache row for moved-to path"
        assert exif_row[1] == DT.isoformat()

        assert hash_row is not None, "expected hash_cache row for moved-to path"
        assert hash_row[2] == expected_new.stat().st_size
        assert hash_row[3] == DT.isoformat()
        # sha1 is a 40-char hex digest
        assert isinstance(hash_row[1], str) and len(hash_row[1]) == 40

        # If a row for the original path exists, it now points at a missing file
        # (acceptable per code; we don't enforce its removal — just document).
        stale_exif = conn.execute(
            "SELECT path FROM exif_cache WHERE path = ?", (placed_resolved,)
        ).fetchone()
        if stale_exif is not None:
            assert not Path(stale_exif[0]).exists()
    finally:
        conn.close()
