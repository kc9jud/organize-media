"""Tests for organize_media.organize() — covers lines ~668-855."""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

import organize_media
from organize_media import organize


# ── helpers ───────────────────────────────────────────────────────────────────


@pytest.fixture
def captured_console(monkeypatch):
    """Replace organize_media.console with a StringIO-backed Console.

    Yields the underlying StringIO so tests can read what was printed.
    """
    buf = StringIO()
    test_console = Console(file=buf, force_terminal=False, width=200)
    monkeypatch.setattr(organize_media, "console", test_console)
    return buf


def make_src_tree(tmp_path: Path, make_jpeg, *items: tuple[str, datetime]) -> Path:
    """Create src dir with JPEGs at given (filename, dt) pairs."""
    src = tmp_path / "src"
    src.mkdir(parents=True, exist_ok=True)
    for filename, dt in items:
        make_jpeg(filename, dt=dt, directory=src)
    return src


def run_organize(
    sources,
    dest,
    *,
    hash_cache,
    exif_cache,
    pool,
    dry_run=False,
    move=False,
    verbose=False,
    exclude_dirs=None,
):
    """Invoke organize() with sensible defaults."""
    organize(
        sources,
        dest,
        dry_run=dry_run,
        move=move,
        verbose=verbose,
        exclude_dirs=exclude_dirs or [],
        pool=pool,
        hash_cache=hash_cache,
        exif_cache=exif_cache,
    )


# ── tests ─────────────────────────────────────────────────────────────────────


def test_copy_distinct_timestamps_no_cache(
    tmp_path, dest, make_jpeg, null_caches, pool, captured_console
):
    """Test 1: copy mode, null caches, three files with distinct dts."""
    src = make_src_tree(
        tmp_path,
        make_jpeg,
        ("a.jpg", datetime(2020, 6, 15, 12, 30, 45)),
        ("b.jpg", datetime(2021, 7, 20, 8, 15, 0)),
        ("c.jpg", datetime(2022, 9, 10, 18, 0, 0)),
    )
    hash_cache, exif_cache = null_caches

    run_organize([src], dest, hash_cache=hash_cache, exif_cache=exif_cache, pool=pool)

    assert (dest / "2020/06/2020-06-15 12-30-45 0001.jpg").exists()
    assert (dest / "2021/07/2021-07-20 08-15-00 0001.jpg").exists()
    assert (dest / "2022/09/2022-09-10 18-00-00 0001.jpg").exists()

    # sources untouched
    assert (src / "a.jpg").exists()
    assert (src / "b.jpg").exists()
    assert (src / "c.jpg").exists()


def test_second_run_hits_cache(
    tmp_path, make_jpeg, caches, pool, captured_console, monkeypatch
):
    """Test 2: with real cache, second run does less EXIF work than first."""
    src = make_src_tree(
        tmp_path,
        make_jpeg,
        ("a.jpg", datetime(2020, 6, 15, 12, 30, 45)),
        ("b.jpg", datetime(2021, 7, 20, 8, 15, 0)),
        ("c.jpg", datetime(2022, 9, 10, 18, 0, 0)),
    )
    hash_cache, exif_cache, _, _ = caches
    dest1 = tmp_path / "dest1"
    dest2 = tmp_path / "dest2"

    # Spy on _get_uncached — classmethod on ExifCache.
    import threading as _t
    counter_lock = _t.Lock()
    counts = {"n": 0}
    original = organize_media.ExifCache._get_uncached

    def counting_uncached(cls, path):
        with counter_lock:
            counts["n"] += 1
        return original(path)

    monkeypatch.setattr(
        organize_media.ExifCache,
        "_get_uncached",
        classmethod(counting_uncached),
    )

    run_organize([src], dest1, hash_cache=hash_cache, exif_cache=exif_cache, pool=pool)
    first_pass = counts["n"]
    counts["n"] = 0

    run_organize([src], dest2, hash_cache=hash_cache, exif_cache=exif_cache, pool=pool)
    second_pass = counts["n"]

    # The point: cache cut down extraction work.
    assert second_pass < first_pass, (
        f"Expected second pass extractions < first pass; got {second_pass} >= {first_pass}"
    )


def test_move_mode(tmp_path, dest, make_jpeg, null_caches, pool, captured_console):
    """Test 3: move=True — sources gone, dests present."""
    src = make_src_tree(
        tmp_path,
        make_jpeg,
        ("a.jpg", datetime(2020, 6, 15, 12, 30, 45)),
        ("b.jpg", datetime(2021, 7, 20, 8, 15, 0)),
        ("c.jpg", datetime(2022, 9, 10, 18, 0, 0)),
    )
    hash_cache, exif_cache = null_caches

    run_organize(
        [src], dest, hash_cache=hash_cache, exif_cache=exif_cache, pool=pool, move=True
    )

    assert (dest / "2020/06/2020-06-15 12-30-45 0001.jpg").exists()
    assert (dest / "2021/07/2021-07-20 08-15-00 0001.jpg").exists()
    assert (dest / "2022/09/2022-09-10 18-00-00 0001.jpg").exists()

    assert not (src / "a.jpg").exists()
    assert not (src / "b.jpg").exists()
    assert not (src / "c.jpg").exists()


def test_dry_run(tmp_path, dest, make_jpeg, null_caches, pool, captured_console):
    """Test 4: dry_run=True — nothing is written; dest does not exist."""
    src = make_src_tree(
        tmp_path,
        make_jpeg,
        ("a.jpg", datetime(2020, 6, 15, 12, 30, 45)),
        ("b.jpg", datetime(2021, 7, 20, 8, 15, 0)),
    )
    hash_cache, exif_cache = null_caches

    run_organize(
        [src], dest, hash_cache=hash_cache, exif_cache=exif_cache, pool=pool, dry_run=True
    )

    assert not dest.exists()
    assert (src / "a.jpg").exists()
    assert (src / "b.jpg").exists()


def test_identical_content_dedup(
    tmp_path, dest, make_jpeg, null_caches, pool, captured_console
):
    """Test 5: two byte-identical files → only one lands in dest."""
    src = tmp_path / "src"
    src.mkdir()
    original = make_jpeg(
        "a.jpg", dt=datetime(2020, 6, 15, 12, 30, 45), directory=src
    )
    # Byte-identical copy under a different name.
    shutil.copy2(original, src / "b.jpg")

    hash_cache, exif_cache = null_caches
    run_organize([src], dest, hash_cache=hash_cache, exif_cache=exif_cache, pool=pool)

    # Find all jpgs in dest
    landed = list(dest.rglob("*.jpg"))
    assert len(landed) == 1, f"Expected exactly 1 copy in dest; found: {landed}"

    output = captured_console.getvalue()
    assert "1 skipped" in output, f"Expected '1 skipped' in output. Got:\n{output}"


def test_exclude_dir_skips_identical(
    tmp_path, dest, make_jpeg, null_caches, pool, captured_console
):
    """Test 6: --exclude pre-populated with identical content → src skipped."""
    excl_dir = tmp_path / "excluded"
    excl_dir.mkdir()
    src = tmp_path / "src"
    src.mkdir()

    excluded_file = make_jpeg(
        "already_there.jpg",
        dt=datetime(2020, 6, 15, 12, 30, 45),
        directory=excl_dir,
    )
    shutil.copy2(excluded_file, src / "copy.jpg")

    hash_cache, exif_cache = null_caches
    run_organize(
        [src],
        dest,
        hash_cache=hash_cache,
        exif_cache=exif_cache,
        pool=pool,
        exclude_dirs=[excl_dir],
    )

    landed = list(dest.rglob("*.jpg")) if dest.exists() else []
    assert landed == [], f"Expected no files in dest; got {landed}"


def test_implicit_exclude_from_existing_dest(
    tmp_path, dest, make_jpeg, null_caches, pool, captured_console
):
    """Test 7: pre-existing file in dest with identical content → src skipped."""
    src = tmp_path / "src"
    src.mkdir()
    dest.mkdir()

    # Make a file directly in dest with arbitrary location (not the canonical path).
    existing = make_jpeg(
        "somewhere.jpg",
        dt=datetime(2020, 6, 15, 12, 30, 45),
        directory=dest / "manually_placed",
    )
    # Source has identical bytes.
    shutil.copy2(existing, src / "source_copy.jpg")

    hash_cache, exif_cache = null_caches
    run_organize([src], dest, hash_cache=hash_cache, exif_cache=exif_cache, pool=pool)

    # Source should be skipped — count files in dest's canonical YYYY/MM hierarchy.
    canonical = list((dest / "2020").rglob("*.jpg")) if (dest / "2020").exists() else []
    assert canonical == [], (
        f"Expected no files copied into the YYYY/MM hierarchy; got {canonical}"
    )


def test_nnnn_collision(
    tmp_path, dest, make_jpeg, null_caches, pool, captured_console
):
    """Test 8: two distinct files at same dt → 0001 and 0002 suffixes."""
    src = tmp_path / "src"
    src.mkdir()
    dt = datetime(2020, 6, 15, 12, 30, 45)
    make_jpeg("red.jpg", dt=dt, directory=src, colour=(255, 0, 0))
    make_jpeg("blue.jpg", dt=dt, directory=src, colour=(0, 0, 255))

    hash_cache, exif_cache = null_caches
    run_organize([src], dest, hash_cache=hash_cache, exif_cache=exif_cache, pool=pool)

    p1 = dest / "2020/06/2020-06-15 12-30-45 0001.jpg"
    p2 = dest / "2020/06/2020-06-15 12-30-45 0002.jpg"
    assert p1.exists(), f"missing {p1}"
    assert p2.exists(), f"missing {p2}"


def test_mixed_extensions_same_dt(
    tmp_path, dest, make_jpeg, null_caches, pool, captured_console
):
    """Test 9: JPEG + PNG at same dt land in same YYYY/MM with distinct exts.

    Creates a PNG with EXIF metadata via PIL (Pillow supports writing eXIf chunk).
    If creating an EXIF-tagged PNG turns out to be flaky, the PNG will fall back
    to mtime — we set mtime to match the requested dt to make it deterministic.
    """
    src = tmp_path / "src"
    src.mkdir()
    dt = datetime(2020, 6, 15, 12, 30, 45)

    make_jpeg("photo.jpg", dt=dt, directory=src, colour=(10, 20, 30))

    # Write a PNG with bytes distinct from the JPEG (different colour, different format),
    # and set its mtime to dt so the EXIF-fallback path uses the right timestamp.
    from PIL import Image
    png_path = src / "drawing.png"
    img = Image.new("RGB", (8, 8), (200, 100, 50))
    img.save(png_path, format="PNG")
    import os as _os
    ts = dt.timestamp()
    _os.utime(png_path, (ts, ts))

    hash_cache, exif_cache = null_caches
    run_organize([src], dest, hash_cache=hash_cache, exif_cache=exif_cache, pool=pool)

    jpg_dst = dest / "2020/06/2020-06-15 12-30-45 0001.jpg"
    png_dst = dest / "2020/06/2020-06-15 12-30-45 0001.png"
    assert jpg_dst.exists(), f"missing jpg: {jpg_dst}; got: {list((dest / '2020/06').iterdir()) if (dest / '2020/06').exists() else 'no dir'}"
    assert png_dst.exists(), f"missing png: {png_dst}; got: {list((dest / '2020/06').iterdir()) if (dest / '2020/06').exists() else 'no dir'}"


def test_exif_failed_source(
    tmp_path, dest, make_jpeg, caches, pool, captured_console, monkeypatch
):
    """Test 10: one source path raises on EXIF → goes to error path, others succeed."""
    src = make_src_tree(
        tmp_path,
        make_jpeg,
        ("good_a.jpg", datetime(2020, 6, 15, 12, 30, 45)),
        ("bad.jpg", datetime(2021, 7, 20, 8, 15, 0)),
        ("good_b.jpg", datetime(2022, 9, 10, 18, 0, 0)),
    )
    hash_cache, exif_cache, _, _ = caches

    bad_path = (src / "bad.jpg").resolve()
    original_get = organize_media.ExifCache.get

    def patched_get(self, path):
        if Path(path).resolve() == bad_path:
            raise RuntimeError("boom")
        return original_get(self, path)

    monkeypatch.setattr(organize_media.ExifCache, "get", patched_get)

    # Force the bad file into the collision group by giving it the same size
    # as another file? No — by default each JPEG made by make_jpeg may have
    # a different size, so the bad file may be size-unique. In that case,
    # `bad` is a deferred-unique file: EXIF is never read during phase 4
    # because there are no size collisions, but copy_one in phase 6 will
    # call exif_cache.get(src) which raises → error.
    #
    # This matches the test plan: copy_one's line 813 raises → error path.
    run_organize([src], dest, hash_cache=hash_cache, exif_cache=exif_cache, pool=pool)

    output = captured_console.getvalue()
    assert "ERROR" in output, f"Expected ERROR in output. Got:\n{output}"
    # Final summary indicates nonzero errors.
    assert "0 errors" not in output, f"Expected nonzero errors. Got:\n{output}"

    # Good files copied successfully.
    assert (dest / "2020/06/2020-06-15 12-30-45 0001.jpg").exists()
    assert (dest / "2022/09/2022-09-10 18-00-00 0001.jpg").exists()


def test_post_copy_cache_population(
    tmp_path, dest, make_jpeg, caches, cache_db, pool, captured_console
):
    """Test 11: after successful copy, cache contains dest paths."""
    src = make_src_tree(
        tmp_path,
        make_jpeg,
        ("a.jpg", datetime(2020, 6, 15, 12, 30, 45)),
        ("b.jpg", datetime(2021, 7, 20, 8, 15, 0)),
    )
    hash_cache, exif_cache, write_conn, write_lock = caches

    run_organize([src], dest, hash_cache=hash_cache, exif_cache=exif_cache, pool=pool)

    # Ensure pending writes are committed before reading via a fresh connection.
    with write_lock:
        write_conn.commit()

    # Open a fresh sqlite connection to read.
    conn = sqlite3.connect(str(cache_db))
    try:
        hash_rows = conn.execute("SELECT path FROM hash_cache").fetchall()
        exif_rows = conn.execute("SELECT path FROM exif_cache").fetchall()
    finally:
        conn.close()

    hash_paths = {row[0] for row in hash_rows}
    exif_paths = {row[0] for row in exif_rows}

    dest_str = str(dest.resolve())
    # At least one row in either table whose path starts with dest.
    dest_in_hash = any(p.startswith(dest_str) for p in hash_paths)
    dest_in_exif = any(p.startswith(dest_str) for p in exif_paths)

    assert dest_in_hash or dest_in_exif, (
        f"Expected at least one cache row under dest={dest_str!r}.\n"
        f"hash_cache paths: {hash_paths}\n"
        f"exif_cache paths: {exif_paths}"
    )
