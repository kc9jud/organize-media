# organize_media.py — Development Transcript

This document summarises the full development history of `organize_media.py` for use as context in a new Claude Code session. The script and this transcript were developed iteratively in a Claude.ai chat session.

---

## Purpose

`organize_media.py` recursively searches one or more source directories for image and video files, and copies (or moves) them to a destination directory using the structure:

```
DEST/YYYY/MM/YYYY-MM-DD hh-mm-ss NNNN.ext
```

`NNNN` is a disambiguating integer unique within each `YYYY/MM` directory. Datetime is extracted from embedded EXIF/metadata, falling back to file mtime.

---

## Dependencies

```
pip install pillow exifread hachoir rich
```

Requires GNU `cp` (Linux/coreutils) for `--reflink=auto` and `--no-clobber`. Not compatible with macOS's BSD `cp` without `brew install coreutils`.

---

## CLI

```
python organize_media.py SOURCE [SOURCE …] DEST
    [--dry-run]
    [--move]
    [--verbose]
    [--exclude DIR]   # repeatable
    [--workers N]
    [--cache PATH]
```

- Multiple source directories are supported.
- `--exclude DIR` (repeatable): directories whose files are treated as "already copied" for deduplication purposes. `DEST` is always implicitly added to this set.
- `--workers N`: thread pool size for metadata reading and hashing. Default: `min(32, cpu_count + 4)`.
- `--cache PATH`: SQLite database for persistent EXIF and hash caching. If omitted, no caching is performed (`NullCache`/`NullExifCache` are used transparently).
- `--dry-run`: simulates the full flow including NNNN assignment without writing files. Uses `candidate.exists()` to skip already-present destination files.
- `--move`: unlinks source file after successful copy.
- `--verbose`: logs every `COPY src → dst` line above the progress bar.

---

## Architecture

### Execution phases

1. **Scan** — `collect_media(sources)` recursively finds all media files. Separate scan for exclude dirs + `dest`.
2. **Read excluded EXIF** (phase 1) — reads EXIF/metadata from exclude files and `dest` files in parallel, inserting into `dt_map` keyed by `(datetime, size)`.
3. **Read source EXIF** (phase 2) — same, for source files. Populates `file_metadata: dict[Path, tuple[datetime, int]]`. Calls `exif_cache.commit()` after.
4. **Deduplicate** (phase 3) — `find_duplicates()` hashes and deduplicates. See below.
5. **Copy/move** (phase 4) — `claim_dest_path()` atomically copies using `cp --no-clobber --reflink=auto --preserve=all`. After each successful copy, inserts a hash cache entry for the destination. Calls `cache.commit()` after.

### `dt_map`

```python
dict[(datetime, int), list[Path]]
```

Groups all files (sources + excludes) by `(EXIF datetime, file size)`. Files with different sizes can never be identical content, so they never need to be hashed against each other.

### `find_duplicates`

Map/reduce over `dt_map` groups using a `ThreadPoolExecutor`:

- **Map**: `process_group(dt, size, paths)` — processes each `(dt, size)` group independently. No shared mutable state between groups.
  - **Short-circuits** if the group has only one source file and no excludes at that `(dt, size)`.
  - Seeds `hashes` dict with exclude files first (claiming their hash slot without being added to `skipped`).
  - Processes source files in sorted order for stable winner selection.
  - Returns `(skipped: set[Path], warnings: list[str])`.
- **Reduce**: unions all skipped sets, concatenates all warning lists, prints warnings serially.

Hashes are computed on demand inside `process_group` via `cache.get()`. There is no separate hashing pass.

### `claim_dest_path`

Atomically claims a destination path:
- Finds the next candidate `YYYY-MM-DD hh-mm-ss NNNN.ext` by checking existence.
- Calls `cp --no-clobber --reflink=auto --preserve=all` (atomic at the kernel level).
- If `cp` exits 1 with no stderr (another process claimed it), increments `NNNN` and retries.
- In `--dry-run` mode, `reflink_copy()` returns `True` immediately without spawning a subprocess; `candidate.exists()` still runs so existing destination files are respected.

### Progress bars

Three styles, all using `rich.progress`:
- **Scanning**: spinner + indeterminate (no total known).
- **Metadata reading**: spinner + `MofNCompleteColumn` (file count).
- **Deduplicating/hashing**: spinner + `DownloadColumn` (bytes, human-readable). `total_bytes` excludes short-circuited groups for accurate ETA. Cache hits advance the bar by `file_size` to keep ETA accurate.

All `progress.update()` calls happen inside worker functions, not on the submission loop.

In non-verbose mode, a status task above the progress bar shows the current filename without persisting it. In verbose mode, `console.log()` prints each file permanently above the bar.

---

## Classes

### `ExifCache`

SQLite-backed. Table: `exif_cache(path TEXT PK, mtime REAL, dt TEXT)`.

Cache key: normalised absolute path. Validated by `st_mtime`. Stale entries are evicted on access.

Contains all EXIF extraction logic as private static/class methods:
- `_parse_exif_dt(value)` — parses `YYYY:MM:DD HH:MM:SS` or `YYYY-MM-DD HH:MM:SS`.
- `_dt_from_image_exif(path)` — tries Pillow first, then exifread (for raw formats).
- `_dt_from_video_metadata(path)` — uses hachoir.
- `_get_uncached(path)` — dispatches to the above; falls back to `st_mtime`.

Public interface:
- `get(path) -> datetime` — cache lookup + fallback to `_get_uncached`. Calls `put()` on miss.
- `put(path, dt)` — insert/replace, reads `mtime` from `stat()`. Does not commit.
- `commit()` — commits shared connection.

`__init__(conn, lock)` — accepts shared `sqlite3.Connection` and `threading.Lock`.

**`NullExifCache`**: subclass. `get()` delegates to `_get_uncached()`. `put()`/`commit()` are no-ops.

### `HashCache`

SQLite-backed. Table: `hash_cache(path TEXT PK, sha1 TEXT, size INTEGER, dt TEXT)`.

Cache key: normalised absolute path. Validated by `(size, dt)`. Do **not** change this schema — it would invalidate existing caches.

Public interface:
- `get(path, dt, size) -> str` — cache lookup + fallback to `_hash_file()`. Calls `put()` on miss.
- `put(path, sha1, dt, size)` — insert/replace. Does not commit.
- `commit()` — commits shared connection.
- `_hash_file(path)` — SHA-1 in 512 MiB chunks (releases GIL in C extension, enabling true parallelism).

`__init__(conn, lock)` — accepts shared connection and lock.

**`NullCache`**: subclass. `get()` calls `_hash_file()` directly. `put()`/`commit()` are no-ops.

### Shared DB setup (`main`)

When `--cache` is given:
```python
conn = sqlite3.connect(str(args.cache), check_same_thread=False)
conn.execute("PRAGMA journal_mode=WAL")
lock = threading.Lock()
hash_cache = HashCache(conn, lock)
exif_cache = ExifCache(conn, lock)
```

Both caches share one connection and one lock. A `finally` block commits and closes. When `--cache` is omitted, `NullCache()` and `NullExifCache()` are used — no `if cache` guards needed anywhere.

---

## Key design decisions and rationale

- **`cp --reflink=auto`**: uses ZFS/btrfs block cloning to avoid using extra storage. Falls back to a regular copy on unsupported filesystems.
- **`cp --no-clobber` for race safety**: the existence check and copy are collapsed into one atomic operation. If another process claims the slot between our check and copy, `cp` exits 1 with no stderr; we increment `NNNN` and retry.
- **`--preserve=all`** on `cp`: preserves timestamps and extended attributes. Replaces an earlier `shutil.copystat()` call.
- **`(datetime, size)` as dedup key**: files with different sizes cannot be identical, so they never enter the same deduplication group and are never hashed against each other.
- **Map/reduce deduplication**: each `(dt, size)` group is processed independently by the thread pool. No shared mutable state between groups; no locks needed. Results are merged with `set.union` and list concatenation. Warnings are collected per-group and printed serially after reduce to avoid interleaving.
- **No separate hashing pass**: since hashing is cached, computing hashes on demand inside each `process_group` worker is equivalent to a separate pass after the first run, and avoids the extra complexity.
- **Short-circuit for singletons**: groups with one source file and no excludes skip hashing entirely.
- **Post-copy hash entry**: after `claim_dest_path()` succeeds, `cache.get(src, dt, size)` is called (cache hit for dedup-group members, miss+compute for singletons) and `cache.put(dst, ...)` registers the destination path. This means future runs will recognise previously-copied files in `dest` without re-hashing.
- **Exclude dirs treated as "first file"**: exclude files (including `dest` contents) are inserted into `dt_map` and seed the `hashes` dict in `process_group` before source files are processed. They are never added to `skipped` — they are not source files and are never copied. Source files matching an exclude's hash get a "matches excluded file" warning.
- **`progress.update()` inside worker functions**: all description updates happen inside the submitted callable, not on the submission loop. This ensures the displayed filename reflects work actually in progress.
- **Batched DB commits**: `put()` never commits. `commit()` is called once at the end of each phase (after EXIF reading and after hashing/deduplication). A final commit+close is in a `finally` block in `main()`.
- **Shared lock for DB access**: a single `threading.Lock` guards all SQLite operations. WAL mode is enabled so readers and writers don't block each other at the filesystem level.

---

## Warnings emitted

- **Identical content, source vs exclude**: "skipping X (matches excluded file Y)"
- **Identical content, source vs source**: "identical file skipped (SHA-1 match with Y)" + path of duplicate
- **Same `(dt, size)`, different content** (i.e. same timestamp+size but different hash): lists all distinct source files sharing that timestamp

---

## Known limitations / future work

- `--reflink=auto` and `--no-clobber` require GNU `cp` (Linux). macOS needs `gcp` from coreutils.
- Post-copy `cache.get(src, ...)` is a cache hit only if the file was in a deduplication group (i.e. shared its `(dt, size)` with another file). Singletons will be hashed at copy time.
- The `NNNN` counter is local to the running process. Concurrent independent invocations targeting the same `DEST` are safe (the `cp --no-clobber` retry loop handles collisions) but may leave gaps in the NNNN sequence.
