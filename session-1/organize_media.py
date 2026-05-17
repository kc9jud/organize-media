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
import shutil
import sqlite3
import subprocess
import sys
import threading
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
    All public methods are thread-safe via the shared lock.
    DB writes are batched; call commit() to flush.
    """

    _DDL = """
        CREATE TABLE IF NOT EXISTS exif_cache (
            path  TEXT PRIMARY KEY,
            mtime REAL NOT NULL,
            dt    TEXT NOT NULL
        )
    """

    def __init__(self, conn: sqlite3.Connection, lock: threading.Lock) -> None:
        self._conn = conn
        self._lock = lock
        with self._lock:
            self._conn.execute(self._DDL)

    # ── public interface ──────────────────────────────────────────────────────

    def get(self, path: Path) -> datetime:
        """
        Return the EXIF datetime for path, using the cache when valid.
        The entry is valid when the stored mtime matches path.stat().st_mtime.
        On a miss or stale entry, re-extracts and stores the result.
        """
        key   = str(path.resolve())
        mtime = path.stat().st_mtime

        with self._lock:
            row = self._conn.execute(
                "SELECT mtime, dt FROM exif_cache WHERE path = ?", (key,)
            ).fetchone()

        if row is not None:
            cached_mtime, cached_dt = row
            if cached_mtime == mtime:
                return datetime.fromisoformat(cached_dt)
            # Stale — evict and fall through.
            with self._lock:
                self._conn.execute("DELETE FROM exif_cache WHERE path = ?", (key,))

        dt = self._get_uncached(path)
        self.put(path, dt)
        return dt

    def put(self, path: Path, dt: datetime) -> None:
        """Insert or replace a cache entry. Does not commit (batched)."""
        key   = str(path.resolve())
        mtime = path.stat().st_mtime
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO exif_cache (path, mtime, dt) VALUES (?, ?, ?)",
                (key, mtime, dt.isoformat()),
            )

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

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
            from PIL.ExifTags import TAGS
            img = Image.open(path)
            exif_data = img._getexif()  # type: ignore[attr-defined]
            if exif_data:
                tag_map = {v: k for k, v in TAGS.items()}
                for tag_name in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
                    tag_id = tag_map.get(tag_name)
                    if tag_id and tag_id in exif_data:
                        dt = ExifCache._parse_exif_dt(str(exif_data[tag_id]))
                        if dt:
                            return dt
        except Exception:
            pass

        try:
            import exifread
            with open(path, "rb") as f:
                tags = exifread.process_file(f, stop_tag="EXIF DateTimeOriginal", details=False)
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


# ── hash cache ────────────────────────────────────────────────────────────────

class HashCache:
    """
    SQLite-backed cache of SHA-1 digests keyed by normalised absolute path.
    Entries are validated against the file's size and EXIF datetime; stale
    entries are evicted and recomputed on access.

    All public methods are thread-safe via the shared lock. DB writes are
    batched; call commit() to flush.
    """

    _DDL = """
        CREATE TABLE IF NOT EXISTS hash_cache (
            path TEXT PRIMARY KEY,
            sha1 TEXT NOT NULL,
            size INTEGER NOT NULL,
            dt   TEXT NOT NULL
        )
    """

    def __init__(self, conn: sqlite3.Connection, lock: threading.Lock) -> None:
        self._conn = conn
        self._lock = lock
        with self._lock:
            self._conn.execute(self._DDL)

    # ── public interface ──────────────────────────────────────────────────────

    def get(self, path: Path, dt: datetime, size: int) -> str:
        """
        Return the SHA-1 digest for path, using the cache when valid.
        The entry is valid when both size and EXIF datetime match.
        """
        key  = str(path.resolve())
        dt_s = dt.isoformat()

        with self._lock:
            row = self._conn.execute(
                "SELECT sha1, size, dt FROM hash_cache WHERE path = ?", (key,)
            ).fetchone()

        if row is not None:
            cached_sha1, cached_size, cached_dt = row
            if cached_size == size and cached_dt == dt_s:
                return cached_sha1
            # Stale entry — evict and fall through to re-hash.
            with self._lock:
                self._conn.execute("DELETE FROM hash_cache WHERE path = ?", (key,))

        digest = self._hash_file(path)
        self.put(path, digest, dt, size)
        return digest

    def put(self, path: Path, sha1: str, dt: datetime, size: int) -> None:
        """Insert or replace a cache entry. Does not commit (batched)."""
        key  = str(path.resolve())
        dt_s = dt.isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO hash_cache (path, sha1, size, dt) VALUES (?, ?, ?, ?)",
                (key, sha1, size, dt_s),
            )

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

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
        self._lock = threading.Lock()

    def get(self, path: Path, dt: datetime, size: int) -> str:
        return self._hash_file(path)

    def put(self, path, sha1, dt, size) -> None:
        pass

    def commit(self) -> None:
        pass


# ── destination path builder ──────────────────────────────────────────────────

def reflink_copy(src: Path, dst: Path, *, dry_run: bool = False) -> bool:
    """
    Attempt an atomic reflink copy using cp --no-clobber --reflink=auto
    --preserve=all. Returns True on success, False if dst already existed
    (lost the race), raises subprocess.CalledProcessError on other failures.

    In dry-run mode, skips the subprocess entirely and returns True.
    """
    if dry_run:
        return True
    result = subprocess.run(
        ["cp", "--no-clobber", "--reflink=auto", "--preserve=all", "--", str(src), str(dst)],
        capture_output=True,
    )
    if result.returncode == 0:
        return True
    if not result.stderr.strip():
        return False
    raise subprocess.CalledProcessError(result.returncode, result.args, result.stderr)


def claim_dest_path(
    dest_root: Path,
    dt: datetime,
    src: Path,
    counters: dict,
    *,
    dry_run: bool = False,
) -> Path:
    """
    Find a free destination path and atomically claim it via cp --no-clobber.
    Retries with incrementing NNNN if another process wins the race.
    """
    stem_base = dt.strftime("%Y-%m-%d %H-%M-%S")
    year_dir  = dest_root / dt.strftime("%Y")
    month_dir = year_dir  / dt.strftime("%m")
    ext       = src.suffix.lower()

    if not dry_run:
        month_dir.mkdir(parents=True, exist_ok=True)

    n = counters.get(month_dir, 1)
    while True:
        candidate = month_dir / f"{stem_base} {n:04d}{ext}"
        if not candidate.exists():
            if reflink_copy(src, candidate, dry_run=dry_run):
                counters[month_dir] = n + 1
                return candidate
        n += 1


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

def collect_media(sources: list[Path]) -> list[Path]:
    with make_progress() as progress:
        task = progress.add_task("Scanning…", total=None)
        found = []
        for source in sources:
            for p in source.rglob("*"):
                if p.is_file() and p.suffix.lower() in ALL_EXTS:
                    found.append(p)
                progress.update(task, description=f"Scanning… [dim]{p.name}[/dim]")
    return found


# ── duplicate detection ───────────────────────────────────────────────────────

def find_duplicates(
    dt_map: dict,
    progress: Progress,
    *,
    exclude_paths: set[Path],
    workers: int,
    cache: HashCache,
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
        for p in paths
        if len(paths) > 1 or any(p in exclude_paths for p in paths)
    )
    task = progress.add_task("Deduplicating…", total=total_bytes)

    GroupResult = tuple[set[Path], list[str]]  # (skipped, warnings)

    def process_group(dt: datetime, size: int, paths: list[Path]) -> GroupResult:
        src_paths  = [p for p in sorted(paths) if p not in exclude_paths]
        excl_paths = [p for p in paths if p in exclude_paths]

        # Short-circuit: single source file, no excludes sharing this (dt, size).
        if len(src_paths) <= 1 and not excl_paths:
            return set(), []

        hashes: dict[str, Path] = {}
        skipped: set[Path] = set()
        warnings: list[str] = []

        # Seed with excludes first — they claim their hash slot without being copied.
        for path in excl_paths:
            progress.update(task, description=f"Deduplicating… [dim]{path.name}[/dim]")
            h = cache.get(path, dt, size)
            progress.advance(task, size)
            hashes.setdefault(h, path)

        # Process source files in sorted order for stable winner selection.
        same_content: list[tuple[Path, Path]] = []
        for path in src_paths:
            progress.update(task, description=f"Deduplicating… [dim]{path.name}[/dim]")
            h = cache.get(path, dt, size)
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
            warnings.append(
                f"\n[bold yellow]⚠  WARNING:[/bold yellow] "
                f"{len(distinct)} files share timestamp "
                f"[yellow]{dt.strftime('%Y-%m-%d %H:%M:%S')}[/yellow] "
                f"with different content:\n{paths_fmt}"
            )

        return skipped, warnings

    # Map: process each group in parallel.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(process_group, dt, size, paths): (dt, size)
            for (dt, size), paths in dt_map.items()
        }
        results: list[GroupResult] = []
        for fut in as_completed(futures):
            results.append(fut.result())

    cache.commit()

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
    workers: int,
    cache: HashCache,
    exif_cache: ExifCache,
) -> None:
    files = collect_media(sources)
    if not files:
        console.print("No media files found.")
        return

    console.print(f"Found [bold]{len(files)}[/bold] media file(s).")
    if dry_run:
        console.print("[bold yellow]DRY RUN[/bold yellow] — no files will be written.\n")

    # ── shared EXIF-reading state ─────────────────────────────────────────────
    dt_map: dict = {}
    dt_map_lock = threading.Lock()
    exif_errors: list[tuple[Path, Exception]] = []
    exif_errors_lock = threading.Lock()

    def read_exif(
        path: Path,
        progress: Progress,
        task,
        *,
        label: str = "Reading metadata",
        into_file_metadata: dict | None = None,
    ) -> None:
        progress.update(task, description=f"{label}… [dim]{path.name}[/dim]")
        try:
            dt   = exif_cache.get(path)
            size = path.stat().st_size
            with dt_map_lock:
                dt_map.setdefault((dt, size), []).append(path)
            if into_file_metadata is not None:
                into_file_metadata[path] = (dt, size)
        except Exception as exc:
            with exif_errors_lock:
                exif_errors.append((path, exc))
        finally:
            progress.advance(task)

    # ── phase 1: scan exclude dirs + dest, read their EXIF ───────────────────
    implicit_exclude = collect_media([dest]) if dest.exists() else []
    all_exclude_files = collect_media(exclude_dirs) + implicit_exclude if exclude_dirs else implicit_exclude
    exclude_paths: set[Path] = set(all_exclude_files)

    if all_exclude_files:
        with make_progress() as progress:
            task = progress.add_task("Reading excluded metadata…", total=len(all_exclude_files))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for path in sorted(all_exclude_files):
                    pool.submit(read_exif, path, progress, task,
                                label="Reading excluded metadata")

    # ── phase 2: read EXIF from source files ─────────────────────────────────
    file_metadata: dict[Path, tuple[datetime, int]] = {}

    with make_progress() as progress:
        task = progress.add_task("Reading metadata…", total=len(files))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for src in sorted(files):
                pool.submit(read_exif, src, progress, task,
                            into_file_metadata=file_metadata)

    exif_cache.commit()

    for path, exc in exif_errors:
        console.print(f"[red]  ERROR reading[/red] {path}: {exc}")

    # ── phase 3: hash + deduplicate ───────────────────────────────────────────
    with make_progress(bytes=True) as progress:
        skipped_files = find_duplicates(
            dt_map, progress,
            exclude_paths=exclude_paths,
            workers=workers,
            cache=cache,
        )

    # ── phase 4: copy / move ──────────────────────────────────────────────────
    counters: dict = {}
    ok = errors = skipped = 0
    action_label = "Moving" if move else "Copying"

    with make_progress() as progress:
        status = progress.add_task("", total=None, visible=not verbose)
        task   = progress.add_task(f"{action_label}…", total=len(files))
        for src in sorted(files):
            if src not in file_metadata:
                errors += 1
                progress.advance(task)
                continue
            if src in skipped_files:
                skipped += 1
                progress.advance(task)
                continue
            try:
                dt, size = file_metadata[src]
                progress.update(status, description=f"[dim]{src.name}[/dim]")
                dst = claim_dest_path(dest, dt, src, counters, dry_run=dry_run)

                if not dry_run:
                    # Get hash (hit if file was in a dedup group, miss for singletons)
                    # and record an entry for the destination path.
                    src_sha1 = cache.get(src, dt, size)
                    dst_size = dst.stat().st_size
                    cache.put(dst, src_sha1, dt, dst_size)

                if move and not dry_run:
                    src.unlink()

                if verbose:
                    action = "MOVE" if move else "COPY"
                    console.log(f"[cyan]{action}[/cyan]  {src}  [dim]→[/dim]  {dst}")
                ok += 1

            except Exception as exc:
                console.print(f"[red]  ERROR[/red]  {src}: {exc}")
                errors += 1
            progress.advance(task)
        progress.update(status, visible=False)

    cache.commit()

    result_style = "bold green" if errors == 0 else "bold yellow"
    console.print(
        f"\n[{result_style}]Done.[/{result_style}] "
        f"{ok} succeeded, {skipped} skipped (identical), {errors} errors."
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Organise media into DEST/YYYY/MM/YYYY-MM-DD hh-mm-ss NNNN.ext"
    )
    parser.add_argument("sources", type=Path, nargs="+",
                        help="One or more source directories to search recursively")
    parser.add_argument("dest", type=Path,
                        help="Destination root directory")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Preview without writing")
    parser.add_argument("--move",     action="store_true",
                        help="Move instead of copy")
    parser.add_argument("--verbose",  action="store_true",
                        help="Print every file path")
    parser.add_argument("--exclude",  type=Path, action="append", default=[],
                        metavar="DIR",
                        help="Directory of already-copied files (repeatable)")
    parser.add_argument("--workers",  type=int,
                        default=min(32, (os.cpu_count() or 1) + 4),
                        metavar="N",
                        help="Thread-pool size for metadata and hashing (default: cpu_count+4)")
    parser.add_argument("--cache",    type=Path, default=None,
                        metavar="PATH",
                        help="SQLite database for persistent hash and EXIF cache (omit to disable)")
    args = parser.parse_args()

    for src in args.sources:
        if not src.is_dir():
            sys.exit(f"Error: source '{src}' is not a directory.")
    for ex in args.exclude:
        if not ex.is_dir():
            sys.exit(f"Error: --exclude '{ex}' is not a directory.")

    if not args.dry_run:
        args.dest.mkdir(parents=True, exist_ok=True)

    if args.cache:
        conn = sqlite3.connect(str(args.cache), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        lock = threading.Lock()
        hash_cache: HashCache  = HashCache(conn, lock)
        exif_cache: ExifCache  = ExifCache(conn, lock)
    else:
        conn       = None
        hash_cache = NullCache()
        exif_cache = NullExifCache()

    try:
        organize(
            args.sources,
            args.dest,
            dry_run=args.dry_run,
            move=args.move,
            verbose=args.verbose,
            exclude_dirs=args.exclude,
            workers=args.workers,
            cache=hash_cache,
            exif_cache=exif_cache,
        )
    finally:
        if conn is not None:
            conn.commit()
            conn.close()


if __name__ == "__main__":
    main()
