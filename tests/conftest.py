"""Shared test fixtures for the organize_media test suite."""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from io import BytesIO
from pathlib import Path

import pytest

from organize_media import ExifCache, HashCache, NullCache, NullExifCache


FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def dest(tmp_path: Path) -> Path:
    """Per-test destination root — not created up front, so dry-run tests
    can assert it never came into being."""
    return tmp_path / "dest"


@pytest.fixture
def cache_db(tmp_path: Path) -> Path:
    return tmp_path / "cache.sqlite"


@pytest.fixture
def caches(cache_db: Path) -> Iterator[tuple[HashCache, ExifCache, sqlite3.Connection, threading.Lock]]:
    """A real (HashCache, ExifCache) pair backed by a tmp SQLite db."""
    write_conn = sqlite3.connect(str(cache_db), check_same_thread=False)
    write_conn.execute("PRAGMA journal_mode=WAL")
    write_lock = threading.Lock()
    hash_cache = HashCache(str(cache_db), write_conn, write_lock)
    exif_cache = ExifCache(str(cache_db), write_conn, write_lock)
    try:
        yield hash_cache, exif_cache, write_conn, write_lock
    finally:
        hash_cache.close_read_conns()
        exif_cache.close_read_conns()
        write_conn.commit()
        write_conn.close()


@pytest.fixture
def null_caches() -> tuple[NullCache, NullExifCache]:
    return NullCache(), NullExifCache()


@pytest.fixture
def pool() -> Iterator[ThreadPoolExecutor]:
    with ThreadPoolExecutor(max_workers=4) as p:
        yield p


@pytest.fixture
def make_jpeg(tmp_path: Path) -> Callable[..., Path]:
    """Factory: write a small Pillow JPEG with the requested EXIF tag.

    Usage: make_jpeg(name, dt=datetime(...), tag="DateTimeOriginal" | "DateTime" | None)
    `tag=None` writes a JPEG with no EXIF.
    `dt=None` is allowed when `tag=None`.
    """
    from PIL import Image

    def _make(
        name: str,
        *,
        dt: datetime | None = None,
        tag: str | None = "DateTimeOriginal",
        size: tuple[int, int] = (8, 8),
        colour: tuple[int, int, int] = (255, 0, 0),
        directory: Path | None = None,
    ) -> Path:
        target_dir = directory if directory is not None else tmp_path
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / name

        img = Image.new("RGB", size, colour)

        if tag is None or dt is None:
            img.save(path, format="JPEG")
            return path

        import piexif

        dt_str = dt.strftime("%Y:%m:%d %H:%M:%S").encode("ascii")
        if tag == "DateTimeOriginal":
            exif = {"Exif": {piexif.ExifIFD.DateTimeOriginal: dt_str}, "0th": {}}
        elif tag == "DateTimeDigitized":
            exif = {"Exif": {piexif.ExifIFD.DateTimeDigitized: dt_str}, "0th": {}}
        elif tag == "DateTime":
            exif = {"0th": {piexif.ImageIFD.DateTime: dt_str}, "Exif": {}}
        else:
            raise ValueError(f"unknown tag: {tag!r}")
        exif_bytes = piexif.dump(exif)
        img.save(path, format="JPEG", exif=exif_bytes)
        return path

    return _make


@pytest.fixture
def copy_fixture(tmp_path: Path) -> Callable[..., Path]:
    """Factory: copy a checked-in fixture into the tmp tree.

    Usage: copy_fixture("jpeg_subifd_2020.jpg", into=tmp_path / "src", mtime=...)
    """

    def _copy(
        name: str,
        *,
        into: Path | None = None,
        as_name: str | None = None,
        mtime: float | None = None,
    ) -> Path:
        target_dir = into if into is not None else tmp_path
        target_dir.mkdir(parents=True, exist_ok=True)
        dst = target_dir / (as_name or name)
        shutil.copy2(FIXTURES_DIR / name, dst)
        if mtime is not None:
            os.utime(dst, (mtime, mtime))
        return dst

    return _copy


@pytest.fixture
def write_bytes(tmp_path: Path) -> Callable[..., Path]:
    """Factory: write arbitrary bytes to a file inside tmp_path."""

    def _w(name: str, data: bytes | None = None, *, size: int | None = None, into: Path | None = None) -> Path:
        target_dir = into if into is not None else tmp_path
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / name
        if data is None:
            data = (b"x" * size) if size is not None else b""
        path.write_bytes(data)
        return path

    return _w
