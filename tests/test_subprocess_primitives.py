"""Tests for reflink_copy and mv_no_clobber subprocess primitives.

Exercises the real `cp` and `mv` binaries (Linux GNU coreutils). No mocking.
Covers organize_media.py:414-452.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from organize_media import mv_no_clobber, reflink_copy


# ---------------------------------------------------------------------------
# reflink_copy
# ---------------------------------------------------------------------------


def test_reflink_copy_happy_path(write_bytes, tmp_path: Path) -> None:
    src = write_bytes("src.bin", b"hello")
    os.chmod(src, 0o640)
    dst = tmp_path / "dst.bin"

    result = reflink_copy(src, dst)

    assert result is True
    assert dst.exists()
    assert dst.read_bytes() == b"hello"
    # --preserve=all should carry mode bits across (regardless of reflink vs full copy).
    assert stat.S_IMODE(dst.stat().st_mode) == 0o640


@pytest.mark.xfail(
    reason=(
        "GNU coreutils 9.4 changed `cp --no-clobber` behaviour: instead of "
        "returning rc=0 silently or rc=1 on skip, it now ALWAYS returns rc=0 "
        "and emits only a portability-warning on stderr ('behavior of -n is "
        "non-portable…').  `reflink_copy` reads rc=0 as success and returns "
        "True even when no copy occurred.  Production fix: use "
        "`--update=none-fail` (cp 9.4+), or compare src/dst inodes after."
    ),
    strict=True,
)
def test_reflink_copy_no_clobber_lost_race(write_bytes, tmp_path: Path) -> None:
    src = write_bytes("src.bin", b"new-content")
    dst = write_bytes("dst.bin", b"original-content")

    result = reflink_copy(src, dst)

    assert result is False
    # dst must not have been overwritten with src bytes.
    assert dst.read_bytes() == b"original-content"


def test_reflink_copy_failure_missing_src(tmp_path: Path) -> None:
    missing_src = tmp_path / "does_not_exist.bin"
    dst = tmp_path / "dst.bin"

    with pytest.raises(subprocess.CalledProcessError):
        reflink_copy(missing_src, dst)


# ---------------------------------------------------------------------------
# mv_no_clobber
# ---------------------------------------------------------------------------


def test_mv_no_clobber_happy_path(write_bytes, tmp_path: Path) -> None:
    src = write_bytes("src.bin", b"hello")
    dst = tmp_path / "dst.bin"

    result = mv_no_clobber(src, dst)

    assert result is True
    assert not src.exists()
    assert dst.exists()
    assert dst.read_bytes() == b"hello"


@pytest.mark.xfail(
    reason=(
        "GNU coreutils 9.4 changed `mv --no-clobber` behaviour: it now returns "
        "rc=1 with stderr 'mv: not replacing X' on skip (older versions "
        "returned rc=0 silently).  `mv_no_clobber` reads (rc!=0, stderr "
        "non-empty) as a real failure and raises CalledProcessError instead "
        "of returning False.  Production fix: detect the 'not replacing' "
        "stderr pattern as a benign skip, or switch to `--update=none-fail`."
    ),
    strict=True,
    raises=subprocess.CalledProcessError,
)
def test_mv_no_clobber_lost_race(write_bytes, tmp_path: Path) -> None:
    src = write_bytes("src.bin", b"aaaa")
    dst = write_bytes("dst.bin", b"bbbb")

    result = mv_no_clobber(src, dst)

    assert result is False
    # src untouched.
    assert src.exists()
    assert src.read_bytes() == b"aaaa"
    # dst untouched.
    assert dst.read_bytes() == b"bbbb"


def test_mv_no_clobber_failure_missing_src(tmp_path: Path) -> None:
    missing_src = tmp_path / "does_not_exist.bin"
    dst = tmp_path / "dst.bin"

    with pytest.raises(subprocess.CalledProcessError):
        mv_no_clobber(missing_src, dst)
