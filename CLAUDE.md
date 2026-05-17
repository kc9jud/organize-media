# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Single-file Python CLI (`organize_media.py`) that copies/moves images and videos into a dated `YYYY/MM/stem` directory structure using EXIF metadata. Supports deduplication via SHA-1, persistent SQLite caching, and in-place reorganization of already-organized destinations.

## Running

```bash
# Copy media from SOURCE(s) to DEST
python organize_media.py SOURCE [SOURCE ...] DEST [OPTIONS]

# Key options
--dry-run           # Preview without writing
--move              # Move instead of copy
--reorganize        # In-place correction mode (SOURCE ignored, walks DEST)
--cache PATH        # SQLite cache path for EXIF + hash results
--workers N         # Thread pool size (default: min(32, cpu_count+4))
--exclude DIR       # Treat DIR as already processed (repeatable)
--verbose           # Log every file operation
```

No build step. No test suite. No lint config.

## Dependencies

```bash
pip install pillow exifread hachoir rich
```

**Linux only.** Uses GNU `cp --reflink=auto --no-clobber` and `mv --no-clobber`. BSD/macOS coreutils not compatible.

## Architecture

Single file, ~1000 lines. Key sections:

**Caches** (`:62`, `:266`) — `ExifCache` and `HashCache` wrap SQLite with thread-local read connections and a single write lock. `NullExifCache`/`NullCache` are no-op drop-ins when `--cache` omitted.

**`organize()` (`:632`)** — Main 6-phase pipeline: scan → EXIF → hash → dedup → copy/move → report. Threaded via `concurrent.futures.ThreadPoolExecutor`.

**`reorganize()` (`:845`)** — In-place mode: walks DEST, recomputes canonical path for each file via EXIF, moves misplaced files.

**`claim_dest_path()` (`:419`)** — Atomically reserves a destination path by racing with `O_CREAT|O_EXCL`, appending `_NNNN` counter on collision.

**`reflink_copy()` (`:378`)** — Delegates to `cp --reflink=auto --no-clobber` for CoW on supported filesystems.

## Output Filename Format

```
DEST/YYYY/MM/YYYY-MM-DD hh-mm-ss NNNN.ext
```

`NNNN` is a per-month disambiguating counter. `--reorganize` ignores NNNN when checking placement — only `YYYY/MM/` dir and `YYYY-MM-DD hh-mm-ss` stem prefix matter.

## Date Extraction Priority

Pillow Exif Sub-IFD (`DateTimeOriginal` 0x9003, `DateTimeDigitized` 0x9004) → Pillow IFD0 (`DateTime` 0x0132) → exifread → hachoir (video) → file mtime.

**Critical:** `DateTimeOriginal` and `DateTimeDigitized` live in the **Exif Sub-IFD**, not IFD0. Must use `exif_data.get_ifd(IFD.Exif)` to read them. IFD0 via `getexif()` alone will miss these tags and fall through to mtime.

## Architecture Details

**Execution phases in `organize()`:**
1. Scan sources + excludes via `collect_media()` (returns `(path, size)`, single `stat` per file)
2. Size grouping — files with unique size across all inputs are "deferred uniques" and skip EXIF until copy time
3. EXIF read (parallel, map-reduce) — only for size-collision files
4. `find_duplicates()` — map-reduce over `(dt, size)` groups; each group processed independently, no inter-group locks
5. Copy/move (parallel, map-reduce) — `copy_one`/`move_one` workers, results reduced on main thread
6. Report

**Deduplication key:** `(datetime, size)` — different sizes never hash against each other. EXIF-failed files use `(None, size)` sentinel and bypass hash cache.

**`claim_dest_path()` race safety:** probes from `n=1` each call (no shared counter state). `cp --no-clobber` is atomic; lost race returns false (empty stderr, non-zero exit), caller increments NNNN and retries. Raises `RuntimeError` after 10,000 attempts.

**`mv --no-clobber` vs `cp` + unlink:** `mv` uses `renameat2(RENAME_NOREPLACE)` for same-filesystem moves (atomic, no TOCTOU). For cross-filesystem, GNU `mv` also uses `REFLINK_AUTO` in its copy fallback — strictly better than manual `cp` + `unlink`.

**Post-copy cache:** after successful copy, `cache.get(src)` (cache hit for dedup-group members, miss+compute for singletons) then `cache.put(dst)` — future runs recognize already-copied files without re-hashing.

**Thread model:** single shared `ThreadPoolExecutor` for all phases. Caches use `threading.local()` for per-thread read connections (no lock on reads); single shared write connection + `threading.Lock` for writes. Known benign race: two threads can both miss cache, both compute, second `INSERT OR REPLACE` overwrites with identical value.

## Cache Schemas

Do **not** change these schemas — doing so invalidates existing cache files:

- `exif_cache(path TEXT PK, mtime REAL, dt TEXT)` — validated by `st_mtime`
- `hash_cache(path TEXT PK, sha1 TEXT, size INTEGER, dt TEXT)` — validated by `(size, dt.isoformat())`

Both tables in same SQLite file. WAL mode enabled.

## Known Constraints

- `--reorganize` reads EXIF for every file (no size-dedup shortcut) since every file needs a timestamp to compute its canonical path.
- Concurrent independent invocations targeting same DEST are safe (atomic primitives handle races) but may produce gaps in NNNN sequence.
- `(None, size)` sentinel groups may produce "same content" matches between EXIF-failed files with no timestamp relationship — correct but potentially noisy.
- Target filesystem is ZFS on Linux; same-pool copies between datasets benefit from reflink.
