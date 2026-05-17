# Context: organize_media.py review and refactor session

## Overview

A Python script `organize_media.py` was reviewed, debugged, and substantially
refactored over a long session. The final file is the authoritative version.
This document summarises every change made and the reasoning behind each, so
a new agent can continue work with full context.

---

## What the script does

Recursively walks one or more source directories, finds image and video files,
reads EXIF/container metadata to extract a creation timestamp (falling back to
mtime), deduplicates files by SHA-1 within groups sharing the same
`(datetime, size)`, and copies or moves survivors to:

```
DEST/YYYY/MM/YYYY-MM-DD hh-mm-ss NNNN.ext
```

Two SQLite caches (EXIF and hash) make re-runs cheap. A `--reorganize` mode
walks an existing destination tree and moves any file whose path does not match
its EXIF timestamp into the correct location.

**Dependencies:** `pillow`, `exifread`, `hachoir`, `rich`

**Target filesystem:** ZFS on Linux, same-pool copies between datasets.

---

## Bugs fixed

### Fix 1 — Shadowed variable in `find_duplicates` total_bytes
The inner `for p in paths` shadowed the outer `p` in the `any(...)` guard.
Renamed inner variable to `_` and guard variable to `q`.

### Fix 2 — Phase 1/2 futures swallowed exceptions
`pool.submit(...)` results were discarded; exceptions inside workers were
silently lost. Fixed by collecting futures and calling `.result()` on each.
Later made moot by the map-reduce refactor (see below).

### Fix 3 — `claim_dest_path` infinite loop
`while True` replaced with `for n in range(1, 10_001)` with a `RuntimeError`
on exhaustion.

### Fix 4 — Unresolved paths in exclude set
`collect_media` now calls `p.resolve()` before appending, so `exclude_paths`
membership tests are reliable regardless of how source/exclude paths were
spelled on the CLI.

### Fix 5 — Pillow `_getexif()` deprecated, wrong IFD
`img._getexif()` replaced with `img.getexif()`. More importantly: the old code
searched `getexif()` (IFD0) for `DateTimeOriginal` (0x9003) and
`DateTimeDigitized` (0x9004), but **both of these tags live in the Exif
Sub-IFD, not IFD0**. `getexif()` only returns IFD0 tags. The fix reads the
sub-IFD via `exif_data.get_ifd(IFD.Exif)`:

```python
exif_sub = exif_data.get_ifd(IFD.Exif)
for tag_id, source in (
    (0x9003, exif_sub),   # DateTimeOriginal  — capture time
    (0x9004, exif_sub),   # DateTimeDigitized — digitisation time
    (0x0132, exif_data),  # DateTime          — IFD0 modification time
):
```

This was the primary cause of excessive mtime fallbacks.

### Fix 6 — exifread `stop_tag` cut scan short
`stop_tag="EXIF DateTimeOriginal"` prevented `DateTimeDigitized` from being
read when `DateTimeOriginal` was absent. Removed `stop_tag` entirely.

### Fix 7 — `collect_media` progress throttle (UX)
Progress label was only updated every 100 filesystem entries, so small
directories showed no filename. Fixed: update on every media file found until
100 files have been seen, then throttle to every 100 filesystem entries.

### Fix 8 — EXIF failure silently bypassed dedup
When `exif_cache.get(path)` raised, the file was added to `exif_errors` but
not to `dt_map`, so it could not be hash-matched against same-size siblings.
Fixed: on EXIF failure, the file is added to `dt_map` under the sentinel key
`(None, size)` so it still participates in dedup. `find_duplicates` /
`process_group` handles `dt=None` by bypassing the hash cache (which requires
a real `dt`) and hashing directly. EXIF-failed files are excluded from the
copy loop (they have no `dt` to build a destination path from).

### Fix 9 — Redundant `cache.put` after `cache.get`
`cache.get` on a miss already calls `put` internally. The explicit `put` after
`get` in the copy loop was redundant. Replaced with: get hash from `src`
**before** the move/copy (when it still exists and may be a cache hit from the
dedup phase), then `put` under `dst`'s path after.

### Fix 10 — `file_metadata` dict was redundant
`file_metadata` stored `(dt, size)` for collision files so the copy loop could
avoid calling `exif_cache.get(src)` again. But `exif_cache.get` for a
recently-read file is a cache hit and equally cheap. `file_metadata` removed
entirely; the copy loop always calls `exif_cache.get(src)`.

### Fix 11 — `shutil` unused import removed

---

## Architectural changes

### Hierarchical filtering (stat → EXIF → hash)

Previously: EXIF was read for every source file and every excluded file
unconditionally.

Now:
1. **Stat sweep** — `collect_media` returns `(path, size)`, paying only one
   `stat` per file.
2. **Size grouping** — files with a unique size across sources and excludes are
   "deferred uniques": they skip EXIF entirely until copy time (cache hit on
   re-runs). Only files in size-collision groups enter the EXIF phase.
3. **EXIF for collision files only** — phases 3 and 4.
4. **Hash within `(dt, size)` groups** — unchanged.
5. **Copy/move** — deferred uniques get EXIF read on-demand (always a cache
   hit after the first run).

### Map-reduce for EXIF phases

The EXIF reader was originally a closure that wrote into shared `dt_map` and
`exif_errors` dicts under locks. Replaced with a pure worker function
`read_exif(path, size, ...) -> (path, size, dt|None, exc|None)` and a
`reduce_exif` function that folds results into `dt_map` and `exif_errors` on
the main thread. No locks needed for these structures.

Same pattern applied to `reorganize`'s EXIF sweep.

### Map-reduce for copy/move phase

The copy/move loop was sequential. Now parallelised:
- `copy_one(src) -> (src, dst|None, exc|None)` — does EXIF lookup, hash,
  claim, cache put; submitted to the shared pool.
- `move_one(src, dt) -> (src, candidate|None, exc|None)` — same for
  reorganize.
- Results reduced on main thread via `as_completed`.

### Single shared `ThreadPoolExecutor`

Previously each phase created its own `ThreadPoolExecutor`. Now a single pool
is created in `main()` and passed into `organize`, `reorganize`, and
`find_duplicates`. This allows thread-local SQLite read connections (see below)
to live for the full run rather than being torn down and recreated each phase.

### Thread-local SQLite read connections

Both `ExifCache` and `HashCache` previously used a single shared connection
protected by a `threading.Lock` for all reads and writes. Since the database
is in WAL mode, concurrent reads don't need serialisation at the SQLite level —
but the `sqlite3` connection object itself is not thread-safe.

New model:
- **Reads:** `threading.local()` stores a per-thread read connection, opened
  lazily on first use. No lock on the read path.
- **Writes** (`put`, `commit`): single shared write connection protected by
  `threading.Lock`. WAL allows one writer + many readers concurrently.

Known race: two threads can both see a cache miss, both compute (EXIF or
SHA-1), and both call `put`. The second `INSERT OR REPLACE` overwrites with an
identical value — correct but redundant work. Documented in comments; not
worth fixing given EXIF extraction is fast and hash collisions in `(dt, size)`
groups are rare.

### `counters` removed from `claim_dest_path`

`claim_dest_path` used to maintain a `counters: dict[Path, int]` to remember
the last-used NNNN for each month directory, avoiding repeated `exists()` probes
from `n=1`. This was a sequential optimisation incompatible with parallel
workers (multiple threads writing to the same month directory would race on the
dict).

Removed entirely. `claim_dest_path` now probes from `n=1` on every call,
relying on `candidate.exists()` as a cheap pre-check and the atomic
`--no-clobber` primitive for correctness. In the common case (files spread
across many months) there is no contention and probing from 1 is cheap.

---

## `--reorganize` mode

Added as a flag on the existing script (not a separate script).

**Why not a separate script:** both modes share `ExifCache`, `HashCache`,
`collect_media`, `claim_dest_path`, progress bar style, and CLI boilerplate.

**Why `--move dest/ dest/` wouldn't work:** `organize` treats `dest` as an
implicit exclude, so every file would exclude itself. The semantics are also
different: reorganize checks whether a file is *already* correctly placed and
leaves it alone if so.

**How it works:**
1. Scan dest with `collect_media`.
2. Read EXIF for every file (no hierarchical deferral — every file needs a
   timestamp to compute its correct path).
3. Classify: `_file_is_correctly_placed` checks that the file is in the right
   `YYYY/MM/` directory and its stem starts with the correct
   `YYYY-MM-DD hh-mm-ss` prefix. Counter value is intentionally ignored.
4. Submit `move_one` tasks for misplaced files via the shared pool.
5. Collect results; prune empty directories left behind, deepest-first.

**Uses `mv --no-clobber`** (via `mv_no_clobber`) rather than `reflink_copy`,
since source and destination are always within the same `dest` tree (same
filesystem). On modern Linux, `mv --no-clobber` issues `renameat2(RENAME_NOREPLACE)` — a single atomic syscall with no TOCTOU gap.

---

## Copy/move primitive design

### `reflink_copy(src, dst) -> bool`
Calls `cp --no-clobber --reflink=auto --preserve=all`. Returns `True` on
success, `False` if dst already existed (empty stderr, non-zero exit = lost
race), raises `CalledProcessError` on real errors. Used for organize copy mode.

### `mv_no_clobber(src, dst) -> bool`
Calls `mv --no-clobber`. Same True/False/raise contract. Used for organize
`--move` and reorganize. On modern Linux, `mv --no-clobber` uses
`renameat2(RENAME_NOREPLACE)` — atomic, no TOCTOU gap. For cross-filesystem
moves, `mv` falls back to a reflink copy + unlink (GNU coreutils has used
`REFLINK_AUTO` in its fallback copy path since 2014).

**Why not `cp` + `unlink` for `--move`:** `mv` is strictly better — atomic
for same-filesystem renames, and cleans up partial destination files on failure
for cross-filesystem copies. The earlier argument that `cp` preserves reflinking
was wrong: `mv` also uses `REFLINK_AUTO` in its copy fallback.

### `claim_dest_path(dest_root, dt, src, *, primitive, dry_run) -> Path`
Builds `DEST/YYYY/MM/YYYY-MM-DD hh-mm-ss NNNN.ext`, probes from `n=1`,
calls `primitive(src, candidate)` for each unoccupied slot. Thread-safe with
no shared state. Raises `RuntimeError` after 10,000 attempts.

---

## Cache design

### `ExifCache`
- Keyed by resolved absolute path.
- Validated by `st_mtime` — stale entries are evicted and recomputed.
- Extraction order: Pillow (Exif Sub-IFD for DTO/DTD, IFD0 for DT) →
  exifread (no `stop_tag`) → file mtime.
- Thread model: thread-local read connections, shared write connection + lock.

### `HashCache`
- Keyed by resolved absolute path.
- Validated by `(size, dt.isoformat())`.
- Thread model: same as ExifCache.
- `get(path, dt, size)` bypasses cache when `dt is None` (EXIF-failed files),
  hashing directly via `_hash_file`.

### `NullExifCache` / `NullCache`
No-op subclasses used when `--cache` is not specified.

---

## CLI

```
python organize_media.py SOURCE [SOURCE …] DEST [options]

Options:
  --dry-run       Preview without writing
  --move          Move instead of copy (organize mode)
  --reorganize    Walk DEST and correct misplaced files; SOURCE ignored
  --verbose       Print every file path
  --exclude DIR   Directory of already-copied files (repeatable)
  --workers N     Thread pool size (default: cpu_count+4, max 32)
  --cache PATH    SQLite file for persistent EXIF and hash cache
```

---

## Current file state

The final `organize_media.py` is at `organize_media.py` in the working
directory. It is approximately 1075 lines. All changes described above are
present. No known outstanding issues.

Potential future work discussed but not implemented:
- The `--reorganize` classification step (`_file_is_correctly_placed`) ignores
  the NNNN counter, so a file at `2022-01-01 09-00-00 0003.jpg` when it
  "should" be `0001.jpg` is considered correctly placed. This is intentional —
  the counter value has no semantic meaning.
- The `(None, size)` sentinel groups in `dt_map` (for EXIF-failed files) cause
  hash comparisons between files with no timestamp relationship. This is
  correct but may produce spurious "same content" matches in degenerate cases.
