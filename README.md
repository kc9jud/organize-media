# organize_media

Recursively copy or move images and videos into a dated `YYYY/MM/` directory
structure, using EXIF/container metadata for the timestamp. Single-file
Python CLI with persistent caching, SHA-1 deduplication, threaded EXIF
extraction, and an in-place reorganize mode.

## Features

- **Many formats.** JPEG, PNG, HEIC, TIFF, WebP, common raw formats (CR2,
  CR3, NEF, ARW, DNG, RAF, RW2, ORF, SRW, PEF), and the usual video
  containers (MP4, MOV, MKV, AVI, M2TS, …).
- **Robust date extraction.** Pillow Exif Sub-IFD → Pillow IFD0 → exifread
  → hachoir (video container) → file mtime fallback.
- **Reflink-aware copies** via GNU `cp --reflink=auto --preserve=all` —
  near-instant on ZFS / Btrfs / XFS reflink-capable filesystems.
- **Atomic destination claim.** Race-safe path assignment via a runtime-
  probed no-clobber primitive (see *Architecture*), with `O_EXCL` fallback
  for systems where every flag-only `cp` no-clobber variant is broken
  (notably coreutils 9.4).
- **SHA-1 dedup.** Source-vs-source and source-vs-destination, with EXIF-
  unreadable files grouped under a `(None, size)` sentinel.
- **Persistent SQLite cache** for EXIF datetimes and SHA-1 digests,
  validated by mtime and (size, datetime) respectively.
- **In-place reorganize mode** that walks an existing destination, recomputes
  the canonical path for every file from EXIF, and moves the misplaced
  ones — pruning empty directories left behind.

## Requirements

- **Linux** with GNU coreutils. BSD/macOS coreutils are not compatible.
- **Python ≥ 3.12.**
- **Dependencies:** `pillow`, `exifread`, `hachoir`, `rich`.
- For development: `pytest`, `pytest-timeout`, `piexif`, and `git-lfs`
  (test fixtures are checked in via LFS).

## Install

Using [uv](https://github.com/astral-sh/uv) (recommended):

```bash
uv venv
uv pip install -e .
```

Or with plain pip:

```bash
python -m venv .venv
.venv/bin/pip install -e .
```

## Usage

```bash
# Copy media from one or more sources into a destination
python organize_media.py SOURCE [SOURCE ...] DEST [OPTIONS]
```

Common options:

| Flag                | Purpose                                                    |
| ------------------- | ---------------------------------------------------------- |
| `--dry-run`         | Preview without writing anything                           |
| `--move`            | Move instead of copy                                       |
| `--reorganize`      | In-place mode (walks `DEST`; `SOURCE` ignored)             |
| `--cache PATH`      | Persistent SQLite cache for EXIF + hash results            |
| `--exclude DIR`     | Treat `DIR` as already-processed (repeatable)              |
| `--workers N`       | Thread-pool size (default: `min(32, cpu_count + 4)`)       |
| `--verbose`         | Log every copy/move and every pruned directory             |

Examples:

```bash
# Copy phone dump into the archive
python organize_media.py ~/Downloads/phone-dump /mnt/photos

# Move (rather than copy), with a cache for repeat runs
python organize_media.py ~/incoming /mnt/photos --move --cache ~/.cache/organize_media.sqlite

# Dry-run, ignoring an already-archived folder
python organize_media.py ~/incoming /mnt/photos --dry-run --exclude /mnt/photos/2023

# In-place reorganize after editing EXIF tags
python organize_media.py /mnt/photos /mnt/photos --reorganize --verbose
```

## Output Filename Format

```
DEST/YYYY/MM/YYYY-MM-DD hh-mm-ss NNNN.ext
```

`NNNN` is a per-month counter that disambiguates collisions. `--reorganize`
ignores `NNNN` when checking placement; only `YYYY/MM/` and the
`YYYY-MM-DD hh-mm-ss` stem prefix matter.

## Date Extraction Priority

1. Pillow Exif **Sub-IFD** — `DateTimeOriginal` (0x9003), `DateTimeDigitized` (0x9004)
2. Pillow IFD0 — `DateTime` (0x0132)
3. `exifread` — broader raw-format support
4. `hachoir` — video container metadata (`creation_date`, etc.)
5. File `mtime` (fallback)

## Caching

Pass `--cache PATH` to enable two SQLite-backed caches in the same file:

- `exif_cache(path PK, mtime REAL, dt TEXT)` — keyed on absolute path,
  validated by `st_mtime`.
- `hash_cache(path PK, sha1 TEXT, size INTEGER, dt TEXT)` — keyed on
  absolute path, validated by `(size, datetime)`.

WAL mode is enabled. After a successful copy, the cache records the
**destination** path too — so re-runs against the same source/destination
tree are fast.

## Architecture

Single ~1200-line file. The pipeline in `organize()`:

1. **Scan** sources and excludes via `collect_media()` (one `stat` per file).
2. **Size grouping** — files with unique sizes skip EXIF until copy time.
3. **EXIF read** (parallel) for size-collision files.
4. **Dedup** — `find_duplicates()` does a map-reduce over `(datetime, size)`
   groups; identical content is collapsed to one winner.
5. **Copy/move** (parallel) — workers call `claim_dest_path` which atomically
   reserves `…NNNN.ext`.
6. **Report.**

`claim_dest_path` is race-safe because the primitive passed to it provides
atomic no-clobber semantics. The primitive is built once per run by
`make_primitive(move=…)`, which probes the relevant tool for a flag set
whose skip signal is detectable. On systems where no flag-only strategy
works (notably **cp on coreutils 9.4**, where every no-clobber variant
silently returns `rc=0`), the primitive falls back to an `O_EXCL` pre-claim
plus `cp -f` / `mv -f` overwriting the empty placeholder. The probe runs
once at function entry, before the thread pool starts, so no lock is
required.

`reorganize()` walks `DEST`, reads EXIF for every file, and moves any file
whose path doesn't match its canonical `(YYYY/MM/, stem)`. Empty parent
directories are pruned (the immediate parent only — grandparents are
left alone).

## Development

```bash
# Install dev deps (includes piexif for synthetic JPEG fixtures)
uv pip install -e '.[test]'

# Run the suite
.venv/bin/pytest -v

# Coverage
uv pip install pytest-cov
.venv/bin/pytest --cov=organize_media --cov-report=term-missing
```

Fixtures live in `tests/fixtures/` and are tracked via **git LFS**. New
clones must `git lfs install` once, then a normal checkout will fetch the
JPEG/CR2/MP4 binaries. The raw inputs used to derive the fixtures live
in `tests/fixtures/_raw/` (gitignored); see the docstrings in
`tests/conftest.py` and the development guide in `CLAUDE.md` for the
exiftool/ffmpeg invocations used to build them.

## Known Constraints

- Linux + GNU coreutils only.
- `--reorganize` reads EXIF for every file (no size-dedup shortcut).
- Concurrent independent invocations against the same `DEST` are safe but
  may produce gaps in the `NNNN` sequence.
- `(None, size)` sentinel groups can match EXIF-unreadable files with no
  semantic timestamp relationship — correct but potentially noisy.

## License

See repository root for license terms.
