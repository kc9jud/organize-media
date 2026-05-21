#!/usr/bin/env python3
"""
organize_media.py — Recursively copy images and videos to a dated directory structure.

Output format: DEST/YYYY/MM/YYYY-MM-DD hh-mm-ss NNNN.ext
Datetime is sourced from embedded EXIF/metadata; falls back to file mtime.

Dependencies:
    pip install pillow exifread hachoir rich

Usage:
    python organize_media.py SOURCE [SOURCE …] DEST [--dry-run] [--move]
        [--verbose] [--exclude DIR] [--workers N] [--cache PATH]
"""

import argparse
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

console = Console(stderr=True)

# ── supported extensions ──────────────────────────────────────────────────────

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".heic", ".heif",
    ".bmp", ".gif", ".webp", ".cr2", ".cr3", ".nef", ".arw",
    ".orf", ".rw2", ".dng", ".raf", ".srw", ".pef",
}

VIDEO_EXTS = {
    ".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp", ".wmv",
    ".flv", ".mts", ".m2ts", ".ts", ".mpg", ".mpeg", ".webm",
}

ALL_EXTS = IMAGE_EXTS | VIDEO_EXTS


# ── EXIF cache ────────────────────────────────────────────────────────────────

class ExifCache:
    """
    SQLite-backed cache of EXIF datetimes keyed by normalised absolute path.
    Entries are validated against the file's mtime; stale entries are evicted
    and recomputed on access.

    Contains all EXIF extraction logic as private static methods.

    Thread model:
      - Reads use a per-thread connection opened lazily via threading.local,
        so concurrent reads in worker threads need no locking.
      - Writes (put, commit) use a single shared write connection protected
        by a lock.  WAL mode allows readers and the single writer to proceed
        concurrently at the SQLite level.
    DB writes are batched; call commit() to flush.
    """

    _DDL = """
        CREATE TABLE IF NOT EXISTS exif_cache (
            path  TEXT PRIMARY KEY,
            mtime REAL NOT NULL,
            dt    TEXT NOT NULL
        )
    """

    def __init__(self, db_path: str, write_conn: sqlite3.Connection,
                 write_lock: threading.Lock) -> None:
        self._db_path   = db_path
        self._write_conn = write_conn
        self._write_lock = write_lock
        self._local      = threading.local()
        self._read_conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        with self._write_lock:
            self._write_conn.execute(self._DDL)

    def _read_conn(self) -> sqlite3.Connection:
        """Return this thread's read connection, opening it on first use."""
        if not hasattr(self._local, "conn"):
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
            with self._conns_lock:
                self._read_conns.append(conn)
        return self._local.conn

    def close_read_conns(self) -> None:
        """Close all per-thread read connections opened by this cache."""
        with self._conns_lock:
            for conn in self._read_conns:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._read_conns.clear()

    # ── public interface ──────────────────────────────────────────────────────

    def get(self, path: Path) -> datetime:
        """
        Return the EXIF datetime for path, using the cache when valid.
        The entry is valid when the stored mtime matches path.stat().st_mtime.
        On a miss or stale entry, re-extracts and stores the result.
        """
        key   = str(path.resolve())
        mtime = path.stat().st_mtime

        row = self._read_conn().execute(
            "SELECT mtime, dt FROM exif_cache WHERE path = ?", (key,)
        ).fetchone()

        if row is not None:
            cached_mtime, cached_dt = row
            if cached_mtime == mtime:
                return datetime.fromisoformat(cached_dt)
            # Stale — evict and fall through.
            with self._write_lock:
                self._write_conn.execute(
                    "DELETE FROM exif_cache WHERE path = ?", (key,))

        dt = self._get_uncached(path)
        # Two threads can both reach here concurrently on the same path (both
        # saw a miss, both extracted).  The second INSERT OR REPLACE in put()
        # overwrites with an identical value — correct but redundant work.
        # Acceptable given that EXIF extraction is fast.
        self.put(path, dt)
        return dt

    def put(self, path: Path, dt: datetime) -> None:
        """Insert or replace a cache entry. Does not commit (batched)."""
        key   = str(path.resolve())
        mtime = path.stat().st_mtime
        with self._write_lock:
            self._write_conn.execute(
                "INSERT OR REPLACE INTO exif_cache (path, mtime, dt) VALUES (?, ?, ?)",
                (key, mtime, dt.isoformat()),
            )

    def commit(self) -> None:
        with self._write_lock:
            self._write_conn.commit()

    # ── private EXIF extraction ───────────────────────────────────────────────

    @staticmethod
    def _parse_exif_dt(value: str) -> datetime | None:
        """Parse 'YYYY:MM:DD HH:MM:SS' or 'YYYY-MM-DD HH:MM:SS'."""
        for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(value[:19], fmt)
            except (ValueError, TypeError):
                continue
        return None

    @staticmethod
    def _dt_from_image_exif(path: Path) -> datetime | None:
        """Try Pillow first (fast), then exifread (wider raw support)."""
        try:
            from PIL import Image
            from PIL.ExifTags import IFD
            img = Image.open(path)
            exif_data = img.getexif()
            if exif_data:
                # DateTimeOriginal (0x9003) and DateTimeDigitized (0x9004) are
                # stored in the Exif Sub-IFD, not IFD0.  DateTime (0x0132) is
                # in IFD0 and represents the last-modification time — least
                # preferred but still better than mtime.
                exif_sub = exif_data.get_ifd(IFD.Exif)
                for tag_id, source in (
                    (0x9003, exif_sub),   # DateTimeOriginal  — capture time
                    (0x9004, exif_sub),   # DateTimeDigitized — digitisation time
                    (0x0132, exif_data),  # DateTime          — IFD0 modification time
                ):
                    raw = source.get(tag_id)
                    if raw is not None:
                        dt = ExifCache._parse_exif_dt(str(raw))
                        if dt:
                            return dt
        except Exception:
            pass

        try:
            import exifread
            with open(path, "rb") as f:
                # No stop_tag: ensures DateTimeDigitized is reachable even
                # when DateTimeOriginal is absent, regardless of IFD order.
                tags = exifread.process_file(f, details=False)
            for key in ("EXIF DateTimeOriginal", "EXIF DateTimeDigitized", "Image DateTime"):
                if key in tags:
                    dt = ExifCache._parse_exif_dt(str(tags[key]))
                    if dt:
                        return dt
        except Exception:
            pass

        return None

    @staticmethod
    def _dt_from_video_metadata(path: Path) -> datetime | None:
        """Use hachoir to pull creation date from video container metadata."""
        try:
            from hachoir.parser import createParser
            from hachoir.metadata import extractMetadata

            parser = createParser(str(path))
            if parser is None:
                return None
            with parser:
                meta = extractMetadata(parser)
            if meta is None:
                return None
            for attr in ("creation_date", "last_modification", "date_time_original"):
                val = meta.getValues(attr)
                if val:
                    v = val[0]
                    if isinstance(v, datetime):
                        return v
                    dt = ExifCache._parse_exif_dt(str(v))
                    if dt:
                        return dt
        except Exception:
            pass
        return None

    @classmethod
    def _get_uncached(cls, path: Path) -> datetime:
        """Extract datetime from EXIF/metadata; fall back to file mtime."""
        ext = path.suffix.lower()
        dt: datetime | None = None

        if ext in IMAGE_EXTS:
            dt = cls._dt_from_image_exif(path)
        elif ext in VIDEO_EXTS:
            dt = cls._dt_from_video_metadata(path)

        if dt is None:
            dt = datetime.fromtimestamp(path.stat().st_mtime)

        return dt


class NullExifCache(ExifCache):
    """No-op EXIF cache — extracts fresh on every call, never reads/writes DB."""

    def __init__(self) -> None:  # type: ignore[override]
        pass

    def get(self, path: Path) -> datetime:
        return self._get_uncached(path)

    def put(self, path: Path, dt: datetime) -> None:
        pass

    def commit(self) -> None:
        pass

    def close_read_conns(self) -> None:
        pass


# ── hash cache ────────────────────────────────────────────────────────────────

class HashCache:
    """
    SQLite-backed cache of SHA-1 digests keyed by normalised absolute path.
    Entries are validated against the file's size and EXIF datetime; stale
    entries are evicted and recomputed on access.

    Thread model: same as ExifCache — thread-local read connections, single
    shared write connection with a lock.  DB writes are batched; call
    commit() to flush.
    """

    _DDL = """
        CREATE TABLE IF NOT EXISTS hash_cache (
            path TEXT PRIMARY KEY,
            sha1 TEXT NOT NULL,
            size INTEGER NOT NULL,
            dt   TEXT NOT NULL
        )
    """

    def __init__(self, db_path: str, write_conn: sqlite3.Connection,
                 write_lock: threading.Lock) -> None:
        self._db_path    = db_path
        self._write_conn = write_conn
        self._write_lock = write_lock
        self._local      = threading.local()
        self._read_conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        with self._write_lock:
            self._write_conn.execute(self._DDL)

    def _read_conn(self) -> sqlite3.Connection:
        """Return this thread's read connection, opening it on first use."""
        if not hasattr(self._local, "conn"):
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
            with self._conns_lock:
                self._read_conns.append(conn)
        return self._local.conn

    def close_read_conns(self) -> None:
        """Close all per-thread read connections opened by this cache."""
        with self._conns_lock:
            for conn in self._read_conns:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._read_conns.clear()

    # ── public interface ──────────────────────────────────────────────────────

    def get(self, path: Path, dt: datetime, size: int) -> str:
        """
        Return the SHA-1 digest for path, using the cache when valid.
        The entry is valid when both size and EXIF datetime match.
        """
        key  = str(path.resolve())
        dt_s = dt.isoformat()

        row = self._read_conn().execute(
            "SELECT sha1, size, dt FROM hash_cache WHERE path = ?", (key,)
        ).fetchone()

        if row is not None:
            cached_sha1, cached_size, cached_dt = row
            if cached_size == size and cached_dt == dt_s:
                return cached_sha1
            # Stale entry — evict and fall through to re-hash.
            with self._write_lock:
                self._write_conn.execute(
                    "DELETE FROM hash_cache WHERE path = ?", (key,))

        digest = self._hash_file(path)
        # Two threads can both reach here concurrently on the same path (both
        # saw a miss, both hashed).  The second INSERT OR REPLACE in put()
        # overwrites with an identical digest — correct but redundant work.
        # The (dt, size) grouping in find_duplicates makes this unlikely for
        # large files; at most `workers` redundant hashes can occur per path.
        self.put(path, digest, dt, size)
        return digest

    def put(self, path: Path, sha1: str, dt: datetime, size: int) -> None:
        """Insert or replace a cache entry. Does not commit (batched)."""
        key  = str(path.resolve())
        dt_s = dt.isoformat()
        with self._write_lock:
            self._write_conn.execute(
                "INSERT OR REPLACE INTO hash_cache (path, sha1, size, dt) VALUES (?, ?, ?, ?)",
                (key, sha1, size, dt_s),
            )

    def commit(self) -> None:
        with self._write_lock:
            self._write_conn.commit()

    # ── internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _hash_file(path: Path) -> str:
        import hashlib
        h = hashlib.sha1()
        with open(path, "rb") as f:
            while chunk := f.read(512 << 20):
                h.update(chunk)
        return h.hexdigest()


class NullCache(HashCache):
    """No-op hash cache — hashes every file from scratch, never reads/writes DB."""

    def __init__(self) -> None:  # type: ignore[override]
        pass

    def get(self, path: Path, dt: datetime, size: int) -> str:
        return self._hash_file(path)

    def put(self, path, sha1, dt, size) -> None:
        pass

    def commit(self) -> None:
        pass

    def close_read_conns(self) -> None:
        pass


# ── destination path builder ──────────────────────────────────────────────────

# ── no-clobber primitive factory ──────────────────────────────────────────────
# Atomic destination-claim with adaptive strategy.  Order of preference:
#
#   1. Native cp/mv no-clobber flags whose skip semantics we can detect
#      from rc + stderr alone — preserves renameat2(RENAME_NOREPLACE)
#      atomicity for mv and avoids a placeholder file for cp.
#         (--update=none-fail, "not replacing")  ← coreutils 9.5+
#         (--no-clobber,       "not replacing")  ← mv 9.4 and similar
#         (--no-clobber,       None)             ← pre-9.4 "rc!=0 empty stderr"
#
#   2. O_EXCL pre-claim fallback when no flag passes the probe.  Python
#      atomically creates an empty placeholder at dst; the winning thread
#      then overwrites it with `cp -f` / `mv -f`.  Used only as a last
#      resort because it loses renameat2 atomicity and briefly exposes an
#      empty file to outside observers.
#
# The probe runs once per make_primitive() call.  GNU coreutils 9.4
# specifically broke flag-only detection for cp: both `--no-clobber` and
# `--update=none` return rc=0 silently on skip (cp 9.4 emits only a
# portability warning).  That's why the fallback exists.


_STRATEGIES: tuple[tuple[tuple[str, ...], str | None], ...] = (
    (("--update=none-fail",), "not replacing"),
    (("--no-clobber",),       "not replacing"),
    (("--no-clobber",),       None),
)


def _invoke(tool: str, flags: tuple[str, ...], extra: tuple[str, ...],
            skip_marker: str | None, src: Path, dst: Path) -> bool:
    """Run `tool <flags> <extra> -- src dst`, interpret the result.

    Returns True on success, False on benign skip, raises CalledProcessError
    on real failure.  `skip_marker` None means "empty stderr (with rc != 0)
    is the skip signal" — the convention older `--no-clobber` followed.
    """
    result = subprocess.run(
        [tool, *flags, *extra, "--", str(src), str(dst)],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return True
    is_skip = (not result.stderr.strip()
               if skip_marker is None
               else skip_marker in result.stderr)
    if is_skip:
        return False
    raise subprocess.CalledProcessError(
        result.returncode, result.args, result.stderr)


def _probe(tool: str, extra: tuple[str, ...]
           ) -> tuple[tuple[str, ...], str | None] | None:
    """Return the first (flags, skip_marker) that gives correct no-clobber
    semantics for `tool`, or None if none qualifies.

    Phase 1 (non-collision): _invoke returns True, dst has src's bytes.
    Phase 2 (collision):     _invoke returns False, dst bytes unchanged.
    """
    with tempfile.TemporaryDirectory(prefix="organize_media_probe_") as td:
        td_path = Path(td)
        for flags, marker in _STRATEGIES:
            src = td_path / "src"
            dst = td_path / "dst"
            try:
                src.write_bytes(b"NEW")
                if dst.exists():
                    dst.unlink()
                ok = _invoke(tool, flags, extra, marker, src, dst)
                if not (ok and dst.exists() and dst.read_bytes() == b"NEW"):
                    continue

                src.write_bytes(b"NEW2")
                dst.write_bytes(b"ORIGINAL")
                ok = _invoke(tool, flags, extra, marker, src, dst)
                if ok or dst.read_bytes() != b"ORIGINAL":
                    continue

                return flags, marker
            except (subprocess.CalledProcessError, FileNotFoundError):
                continue
            finally:
                for p in (src, dst):
                    if p.exists():
                        p.unlink()
    return None


def make_primitive(move: bool) -> Callable[[Path, Path], bool]:
    """Build a (src, dst) -> bool primitive for use with claim_dest_path.

    Probes cp/mv for a no-clobber flag with detectable skip semantics
    (preferred — preserves renameat2 atomicity for mv).  Falls back to an
    O_EXCL placeholder + `-f` overwrite when no flag passes the probe
    (e.g. cp 9.4, where every no-clobber flag returns rc=0 silently).

    Callers invoke this from a single-threaded region (top of organize /
    reorganize), then pass the returned closure as `primitive=` into
    claim_dest_path; the closure is safe to call concurrently from the
    thread pool.

    Returns True on success, False on benign skip (lost the race), raises
    subprocess.CalledProcessError on real failures.  In fallback mode, a
    failed `cp -f`/`mv -f` unlinks the placeholder before re-raising.
    """
    if move:
        tool = "mv"
        extra: tuple[str, ...] = ()
    else:
        tool = "cp"
        extra = ("--reflink=auto", "--preserve=all")

    chosen = _probe(tool, extra)
    if chosen is not None:
        flags, marker = chosen

        def primitive(src: Path, dst: Path) -> bool:
            return _invoke(tool, flags, extra, marker, src, dst)

        return primitive

    # Fallback: O_EXCL pre-claim + plain cp/mv with -f to overwrite the
    # empty placeholder we just created.
    cmd_flags: tuple[str, ...] = ("-f", *extra)

    def primitive(src: Path, dst: Path) -> bool:
        try:
            fd = os.open(str(dst), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        os.close(fd)
        try:
            subprocess.run(
                [tool, *cmd_flags, "--", str(src), str(dst)],
                capture_output=True, text=True, check=True,
            )
            return True
        except subprocess.CalledProcessError:
            try:
                dst.unlink()
            except OSError:
                pass
            raise

    return primitive


def claim_dest_path(
    dest_root: Path,
    dt: datetime,
    src: Path,
    *,
    primitive: Callable[[Path, Path], bool],
    dry_run: bool = False,
) -> Path:
    """
    Find a free destination path and atomically claim it.

    `primitive` is required and is called as primitive(src, candidate); it
    must return True on success or False if the slot was already taken (lost
    a concurrent race), in which case the next NNNN is tried.  Callers
    build a primitive once per run via `make_primitive(move=…)`.

    Probes from n=1 on every call — no counter state is maintained, so this
    function is safe to call concurrently from multiple threads.  The exists()
    pre-check is an optimisation to avoid a subprocess call for occupied slots;
    the primitive provides the actual atomicity guarantee.

    Raises RuntimeError after 10,000 attempts.
    """
    stem_base = dt.strftime("%Y-%m-%d %H-%M-%S")
    month_dir = dest_root / dt.strftime("%Y") / dt.strftime("%m")
    ext       = src.suffix.lower()

    if not dry_run:
        month_dir.mkdir(parents=True, exist_ok=True)

    for n in range(1, 10_001):
        candidate = month_dir / f"{stem_base} {n:04d}{ext}"
        if not candidate.exists():
            if dry_run or primitive(src, candidate):
                return candidate
    raise RuntimeError(
        f"Could not claim a destination path under {month_dir} for {src.name} "
        f"after 10,000 attempts — is the directory unusually full or unwritable?"
    )


# ── progress bar style ────────────────────────────────────────────────────────

def make_progress(*, bytes: bool = False) -> Progress:
    size_col = DownloadColumn() if bytes else MofNCompleteColumn()
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(bar_width=None),
        size_col,
        TaskProgressColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
        expand=True,
    )


# ── media collection ──────────────────────────────────────────────────────────

_SCAN_UPDATE_INTERVAL = 100  # update progress label every N filesystem entries


def collect_media(sources: list[Path]) -> list[tuple[Path, int]]:
    """
    Walk sources recursively and return (resolved_path, size) for every
    media file found.  The progress label updates on every media file
    until _SCAN_UPDATE_INTERVAL files have been seen, then throttles to
    every _SCAN_UPDATE_INTERVAL filesystem entries to avoid saturating
    the terminal on large trees.
    """
    with make_progress() as progress:
        task = progress.add_task("Scanning…", total=None)
        found: list[tuple[Path, int]] = []
        seen: int = 0
        for source in sources:
            for p in source.rglob("*"):
                seen += 1
                if p.is_file() and p.suffix.lower() in ALL_EXTS:
                    try:
                        found.append((p.resolve(), p.stat().st_size))
                    except OSError:
                        pass  # file vanished between rglob and stat
                    # Update on every media file found until the throttle
                    # kicks in, so small directories always show a filename.
                    if len(found) <= _SCAN_UPDATE_INTERVAL or seen % _SCAN_UPDATE_INTERVAL == 0:
                        progress.update(task, description=f"Scanning… [dim]{p.name}[/dim]")
    return found


# ── duplicate detection ───────────────────────────────────────────────────────

def find_duplicates(
    dt_map: dict,
    progress: Progress,
    *,
    exclude_paths: set[Path],
    pool: ThreadPoolExecutor,
    hash_cache: HashCache,
) -> set[Path]:
    """
    Deduplicate source files against each other and against exclude_paths.

    Each (datetime, size) group is processed independently (map), then results
    are merged (reduce). Hashes are computed on demand inside each group worker
    via the cache. Single-file groups with no excludes are short-circuited.

    Returns the set of source paths that should not be copied.
    """
    # Total bytes across groups that won't be short-circuited.
    total_bytes = sum(
        size
        for (dt, size), paths in dt_map.items()
        for _ in paths
        if len(paths) > 1 or any(q in exclude_paths for q in paths)
    )
    task = progress.add_task("Deduplicating…", total=total_bytes)

    GroupResult = tuple[set[Path], list[str]]  # (skipped, warnings)

    def process_group(dt: datetime | None, size: int, paths: list[Path]) -> GroupResult:
        src_paths  = [p for p in sorted(paths) if p not in exclude_paths]
        excl_paths = [p for p in paths if p in exclude_paths]

        # Short-circuit: single source file, no excludes sharing this (dt, size).
        if len(src_paths) <= 1 and not excl_paths:
            return set(), []

        hashes: dict[str, Path] = {}
        skipped: set[Path] = set()
        warnings: list[str] = []

        def get_hash(path: Path) -> str:
            # When dt is None (EXIF failed) we cannot use the cache's
            # dt-based validity check, so we bypass it and hash directly.
            if dt is None:
                return hash_cache._hash_file(path)
            return hash_cache.get(path, dt, size)

        # Seed with excludes first — they claim their hash slot without being copied.
        for path in excl_paths:
            progress.update(task, description=f"Deduplicating… [dim]{path.name}[/dim]")
            h = get_hash(path)
            progress.advance(task, size)
            hashes.setdefault(h, path)

        # Process source files in sorted order for stable winner selection.
        same_content: list[tuple[Path, Path]] = []
        for path in src_paths:
            progress.update(task, description=f"Deduplicating… [dim]{path.name}[/dim]")
            h = get_hash(path)
            progress.advance(task, size)
            if h in hashes:
                same_content.append((hashes[h], path))
                skipped.add(path)
            else:
                hashes[h] = path

        # Build warnings — printed after reduce to avoid interleaving.
        for kept, duplicate in same_content:
            if kept in exclude_paths:
                warnings.append(
                    f"\n[bold yellow]⚠  WARNING:[/bold yellow] skipping [dim]{duplicate}[/dim] "
                    f"[dim](matches excluded file [cyan]{kept.name}[/cyan])[/dim]"
                )
            else:
                warnings.append(
                    f"\n[bold yellow]⚠  WARNING:[/bold yellow] identical file skipped "
                    f"[dim](SHA-1 match with [cyan]{kept.name}[/cyan]):[/dim]\n"
                    f"    [dim]{duplicate}[/dim]"
                )

        distinct = [p for p in src_paths if p not in skipped]
        if len(distinct) > 1:
            paths_fmt = "\n".join(f"    [dim]{p}[/dim]" for p in distinct)
            dt_label = (
                dt.strftime("%Y-%m-%d %H:%M:%S") if dt is not None
                else "unknown (EXIF unreadable)"
            )
            warnings.append(
                f"\n[bold yellow]⚠  WARNING:[/bold yellow] "
                f"{len(distinct)} files share timestamp "
                f"[yellow]{dt_label}[/yellow] "
                f"with different content:\n{paths_fmt}"
            )

        return skipped, warnings

    # Map: process each group in parallel.
    futures = {
        pool.submit(process_group, dt, size, paths): (dt, size)
        for (dt, size), paths in dt_map.items()
    }
    results: list[GroupResult] = []
    for fut in as_completed(futures):
        results.append(fut.result())

    hash_cache.commit()

    # Reduce: merge skipped sets and warning lists.
    all_skipped: set[Path] = set().union(*(r[0] for r in results))
    all_warnings: list[str] = [w for r in results for w in r[1]]

    for w in all_warnings:
        console.print(w)
    if all_skipped:
        console.print()

    return all_skipped


# ── main orchestration ────────────────────────────────────────────────────────

def organize(
    sources: list[Path],
    dest: Path,
    *,
    dry_run: bool,
    move: bool,
    verbose: bool,
    exclude_dirs: list[Path],
    pool: ThreadPoolExecutor,
    hash_cache: HashCache,
    exif_cache: ExifCache,
) -> None:
    # Probe and build the no-clobber primitive once, before the thread pool
    # starts — this is the only single-threaded region, so no lock is needed.
    primitive = make_primitive(move=move)

    # ── phase 1: stat sweep ───────────────────────────────────────────────────
    # Collect (path, size) for source and exclude files cheaply — no EXIF yet.
    src_items = collect_media(sources)
    if not src_items:
        console.print("No media files found.")
        return

    console.print(f"Found [bold]{len(src_items)}[/bold] media file(s).")
    if dry_run:
        console.print("[bold yellow]DRY RUN[/bold yellow] — no files will be written.\n")

    implicit_excl  = collect_media([dest]) if dest.exists() else []
    excl_items     = collect_media(exclude_dirs) + implicit_excl if exclude_dirs else implicit_excl
    exclude_paths: set[Path] = {p for p, _ in excl_items}

    # ── phase 2: size grouping ─────────────────────────────────────────────────
    # Build size → [paths] maps for sources and excludes separately.
    # A source file is a "size collision" if another source *or* an exclude
    # shares the same byte-count — those need EXIF + hash to resolve.
    # Size-unique source files skip EXIF until copy time.
    src_by_size:  dict[int, list[Path]] = defaultdict(list)
    excl_by_size: dict[int, set[Path]]  = defaultdict(set)

    for path, size in src_items:
        src_by_size[size].append(path)
    for path, size in excl_items:
        excl_by_size[size].add(path)

    collision_sizes: set[int] = {
        size for size, paths in src_by_size.items()
        if len(paths) > 1 or size in excl_by_size
    }

    # Split source files into those that need immediate EXIF and deferred uniques.
    collision_files: list[Path] = [
        p for p, size in src_items if size in collision_sizes
    ]
    deferred_count: int = sum(1 for _, size in src_items if size not in collision_sizes)
    src_size: dict[Path, int] = {p: size for p, size in src_items}

    console.print(
        f"  {len(collision_files)} file(s) in size-collision groups (need EXIF + hash), "
        f"{deferred_count} unique by size (EXIF deferred)."
    )

    # ── shared EXIF-reading state ─────────────────────────────────────────────
    # dt_map accumulates (dt, size) → [paths] for all files that enter
    # the dedup phase (collision files + excludes sharing those sizes).
    # read_exif returns its result directly; the caller reduces into dt_map
    # and exif_errors, so no locks are needed.
    dt_map: dict[tuple[datetime | None, int], list[Path]] = {}
    exif_errors: list[tuple[Path, Exception]] = []

    ExifResult = tuple[Path, int, datetime | None, Exception | None]

    def read_exif(
        path: Path,
        size: int,
        progress: Progress,
        task: TaskID,
        label: str,
    ) -> ExifResult:
        progress.update(task, description=f"{label}… [dim]{path.name}[/dim]")
        try:
            dt = exif_cache.get(path)
            return path, size, dt, None
        except Exception as exc:
            return path, size, None, exc
        finally:
            progress.advance(task)

    def reduce_exif(results: list[ExifResult]) -> None:
        """Merge worker results into dt_map and exif_errors."""
        for path, size, dt, exc in results:
            if exc is not None:
                exif_errors.append((path, exc))
                # Still enter the dedup phase under a sentinel key so this file
                # can be hash-matched against same-size siblings rather than
                # silently bypassing dedup entirely.
                dt_map.setdefault((None, size), []).append(path)
            else:
                dt_map.setdefault((dt, size), []).append(path)

    def run_exif_phase(
        items: list[tuple[Path, int]],
        label: str,
        task_label: str,
    ) -> None:
        with make_progress() as progress:
            task = progress.add_task(task_label, total=len(items))
            futures = [
                pool.submit(read_exif, p, size, progress, task, label)
                for p, size in sorted(items)
            ]
            reduce_exif([fut.result() for fut in as_completed(futures)])

    # ── phase 3: EXIF for exclude files sharing a collision size ─────────────
    excl_collision_files: list[tuple[Path, int]] = [
        (p, size) for p, size in excl_items if size in collision_sizes
    ]
    if excl_collision_files:
        run_exif_phase(excl_collision_files,
                       "Reading excluded metadata", "Reading excluded metadata…")

    # ── phase 4: EXIF for collision source files ──────────────────────────────
    if collision_files:
        run_exif_phase([(p, src_size[p]) for p in collision_files],
                       "Reading metadata", "Reading metadata…")

    exif_cache.commit()

    exif_failed: set[Path] = {path for path, _ in exif_errors}
    for path, exc in exif_errors:
        console.print(f"[red]  ERROR reading[/red] {path}: {exc}")

    # ── phase 5: hash + deduplicate collision groups ──────────────────────────
    with make_progress(bytes=True) as progress:
        skipped_files = find_duplicates(
            dt_map, progress,
            exclude_paths=exclude_paths,
            pool=pool,
            hash_cache=hash_cache,
        )

    # ── phase 6: copy / move ──────────────────────────────────────────────────
    ok = errors = skipped = 0
    action_label = "Moving" if move else "Copying"

    CopyResult = tuple[Path, Path | None, Exception | None]

    def copy_one(src: Path) -> CopyResult:
        try:
            dt   = exif_cache.get(src)
            size = src_size[src]
            sha1 = hash_cache.get(src, dt, size) if not dry_run else None
            dst  = claim_dest_path(dest, dt, src,
                                   primitive=primitive, dry_run=dry_run)
            if not dry_run:
                hash_cache.put(dst, sha1, dt, dst.stat().st_size)
            return src, dst, None
        except Exception as exc:
            return src, None, exc

    active_files = [
        p for p, _ in src_items
        if p not in skipped_files and p not in exif_failed
    ]
    skipped = sum(1 for p, _ in src_items if p in skipped_files)
    errors  = len(exif_failed & {p for p, _ in src_items})

    with make_progress() as progress:
        task = progress.add_task(f"{action_label}…", total=len(src_items))
        # Advance immediately for files that don't need a worker.
        progress.advance(task, skipped + errors)
        futures = {pool.submit(copy_one, src): src for src in active_files}
        for fut in as_completed(futures):
            src, dst, exc = fut.result()
            progress.advance(task)
            if exc is not None:
                console.print(f"[red]  ERROR[/red]  {src}: {exc}")
                errors += 1
            else:
                if verbose:
                    action = "MOVE" if move else "COPY"
                    console.log(f"[cyan]{action}[/cyan]  {src}  [dim]→[/dim]  {dst}")
                ok += 1

    hash_cache.commit()
    exif_cache.commit()

    result_style = "bold green" if errors == 0 else "bold yellow"
    console.print(
        f"\n[{result_style}]Done.[/{result_style}] "
        f"{ok} succeeded, {skipped} skipped (identical), {errors} errors."
    )


# ── reorganize (in-place correction) ─────────────────────────────────────────

def _correct_path(dest_root: Path, dt: datetime, src: Path) -> tuple[Path, str]:
    """
    Return (month_dir, stem_base) — the canonical directory and filename stem
    that this file *should* live under, given its EXIF datetime.
    """
    stem_base = dt.strftime("%Y-%m-%d %H-%M-%S")
    month_dir = dest_root / dt.strftime("%Y") / dt.strftime("%m")
    return month_dir, stem_base


def _file_is_correctly_placed(path: Path, dest_root: Path, dt: datetime) -> bool:
    """
    Return True if `path` is already in the right month directory and its
    filename stem starts with the correct datetime prefix.
    The NNNN counter value is intentionally ignored — any counter is valid
    so long as the file is in the right directory with the right stem.
    """
    month_dir, stem_base = _correct_path(dest_root, dt, path)
    return path.parent == month_dir and path.stem.startswith(stem_base)


def reorganize(
    dest: Path,
    *,
    dry_run: bool,
    verbose: bool,
    pool: ThreadPoolExecutor,
    hash_cache: HashCache,
    exif_cache: ExifCache,
) -> None:
    """
    Walk dest, read EXIF for every file, and move any file whose path does not
    match its EXIF datetime into the correct location.

    Unlike organize(), every file needs its EXIF timestamp (to compute its
    correct path), so there is no deferred-EXIF optimisation.  The move
    primitive is built once via `make_primitive(move=True)` before the
    thread pool starts; on modern Linux mv issues renameat2(RENAME_NOREPLACE)
    — a single atomic syscall that is O(1) on the same filesystem and safe
    under concurrent writers.  Empty directories left behind after moves are
    pruned.
    """
    # Probe and build the move primitive once, before the thread pool starts.
    primitive = make_primitive(move=True)

    dest = dest.resolve()
    items = collect_media([dest])
    if not items:
        console.print("No media files found in destination.")
        return

    console.print(f"Found [bold]{len(items)}[/bold] media file(s).")
    if dry_run:
        console.print("[bold yellow]DRY RUN[/bold yellow] — no files will be moved.\n")

    # ── EXIF sweep: read every file (no size-based deferral) ─────────────────
    ReorgExifResult = tuple[Path, datetime | None, Exception | None]

    def read_one(path: Path, progress: Progress, task: TaskID) -> ReorgExifResult:
        progress.update(task, description=f"Reading metadata… [dim]{path.name}[/dim]")
        try:
            dt = exif_cache.get(path)
            return path, dt, None
        except Exception as exc:
            return path, None, exc
        finally:
            progress.advance(task)

    with make_progress() as progress:
        task = progress.add_task("Reading metadata…", total=len(items))
        futures = [
            pool.submit(read_one, p, progress, task)
            for p, _ in sorted(items)
        ]
        results: list[ReorgExifResult] = [fut.result() for fut in futures]

    exif_cache.commit()

    file_dt: dict[Path, datetime] = {}
    exif_errors: list[tuple[Path, Exception]] = []
    for path, dt, exc in results:
        if exc is not None:
            exif_errors.append((path, exc))
        else:
            file_dt[path] = dt

    exif_failed: set[Path] = {p for p, _ in exif_errors}
    for path, exc in exif_errors:
        console.print(f"[red]  ERROR reading[/red] {path}: {exc}")

    # ── move misplaced files ──────────────────────────────────────────────────
    src_size: dict[Path, int] = {p: size for p, size in items}
    ok = already_ok = errors = 0
    misplaced: list[tuple[Path, datetime]] = []

    for path, _ in sorted(items):
        if path in exif_failed:
            continue
        if _file_is_correctly_placed(path, dest, file_dt[path]):
            already_ok += 1
        else:
            misplaced.append((path, file_dt[path]))

    if misplaced:
        console.print(f"  {len(misplaced)} file(s) need reorganising, "
                      f"{already_ok} already correct.")
    else:
        console.print(f"  All {already_ok} file(s) are correctly placed.")

    ReorgMoveResult = tuple[Path, Path | None, Exception | None]

    def move_one(src: Path, dt: datetime) -> ReorgMoveResult:
        try:
            sha1      = hash_cache.get(src, dt, src_size[src]) if not dry_run else None
            candidate = claim_dest_path(dest, dt, src,
                                        primitive=primitive, dry_run=dry_run)
            if not dry_run:
                exif_cache.put(candidate, dt)
                hash_cache.put(candidate, sha1, dt, candidate.stat().st_size)
            return src, candidate, None
        except Exception as exc:
            return src, None, exc

    dirs_to_prune: set[Path] = set()

    with make_progress() as progress:
        task = progress.add_task("Reorganising…", total=len(misplaced))
        futures = {
            pool.submit(move_one, src, dt): src
            for src, dt in misplaced
        }
        for fut in as_completed(futures):
            src, candidate, exc = fut.result()
            progress.advance(task)
            if exc is not None:
                console.print(f"[red]  ERROR[/red]  {src}: {exc}")
                errors += 1
            else:
                dirs_to_prune.add(src.parent)
                if verbose:
                    console.log(f"[cyan]MOVE[/cyan]  {src}  [dim]→[/dim]  {candidate}")
                ok += 1

    # ── prune empty directories left behind by moves ─────────────────────────
    if not dry_run:
        # Sort deepest-first so child dirs are removed before parents.
        for d in sorted(dirs_to_prune, key=lambda p: len(p.parts), reverse=True):
            try:
                if d != dest and not any(d.iterdir()):
                    d.rmdir()
                    if verbose:
                        console.log(f"[dim]RMDIR[/dim]  {d}")
            except OSError:
                pass  # non-empty or already gone — ignore

    hash_cache.commit()
    exif_cache.commit()

    result_style = "bold green" if errors == 0 else "bold yellow"
    console.print(
        f"\n[{result_style}]Done.[/{result_style}] "
        f"{ok} moved, {already_ok} already correct, {errors} errors."
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Organise media into DEST/YYYY/MM/YYYY-MM-DD hh-mm-ss NNNN.ext"
    )
    parser.add_argument("paths", type=Path, nargs="+", metavar="PATH",
                        help="SOURCE... DEST  (or just DEST when --reorganize is set)")
    parser.add_argument("--dry-run",     action="store_true",
                        help="Preview without writing")
    parser.add_argument("--move",        action="store_true",
                        help="Move instead of copy (organize mode only)")
    parser.add_argument("--reorganize",  action="store_true",
                        help=(
                            "Walk DEST and move any file whose path does not match "
                            "its EXIF metadata into the correct location."
                        ))
    parser.add_argument("--verbose",     action="store_true",
                        help="Print every file path")
    parser.add_argument("--exclude",     type=Path, action="append", default=[],
                        metavar="DIR",
                        help="Directory of already-copied files (repeatable, organize mode only)")
    parser.add_argument("--workers",     type=int,
                        default=min(32, (os.cpu_count() or 1) + 4),
                        metavar="N",
                        help="Thread-pool size for metadata and hashing (default: cpu_count+4)")
    parser.add_argument("--cache",       type=Path, default=None,
                        metavar="PATH",
                        help="SQLite database for persistent hash and EXIF cache (omit to disable)")
    args = parser.parse_args()

    if args.reorganize:
        if len(args.paths) != 1:
            parser.error("--reorganize takes exactly one positional argument: DEST")
        args.sources = []
        args.dest = args.paths[0]
    else:
        if len(args.paths) < 2:
            parser.error("at least one SOURCE and a DEST are required")
        args.sources = args.paths[:-1]
        args.dest = args.paths[-1]

    for ex in args.exclude:
        if not ex.is_dir():
            sys.exit(f"Error: --exclude '{ex}' is not a directory.")

    if args.reorganize:
        if not args.dest.is_dir():
            sys.exit(f"Error: destination '{args.dest}' is not a directory.")
    else:
        for src in args.sources:
            if not src.is_dir():
                sys.exit(f"Error: source '{src}' is not a directory.")
        if not args.dry_run:
            args.dest.mkdir(parents=True, exist_ok=True)

    if args.cache:
        db_path    = str(args.cache)
        write_conn = sqlite3.connect(db_path, check_same_thread=False)
        write_conn.execute("PRAGMA journal_mode=WAL")
        write_lock = threading.Lock()
        hash_cache: HashCache = HashCache(db_path, write_conn, write_lock)
        exif_cache: ExifCache = ExifCache(db_path, write_conn, write_lock)
    else:
        write_conn = None
        hash_cache = NullCache()
        exif_cache = NullExifCache()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        try:
            if args.reorganize:
                reorganize(
                    args.dest,
                    dry_run=args.dry_run,
                    verbose=args.verbose,
                    pool=pool,
                    hash_cache=hash_cache,
                    exif_cache=exif_cache,
                )
            else:
                organize(
                    args.sources,
                    args.dest,
                    dry_run=args.dry_run,
                    move=args.move,
                    verbose=args.verbose,
                    exclude_dirs=args.exclude,
                    pool=pool,
                    hash_cache=hash_cache,
                    exif_cache=exif_cache,
                )
        finally:
            hash_cache.close_read_conns()
            exif_cache.close_read_conns()
            if write_conn is not None:
                write_conn.commit()
                write_conn.close()


if __name__ == "__main__":
    main()
