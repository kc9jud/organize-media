"""Tests for the `make_primitive` factory and the closures it returns.

Exercises real `cp` and `mv` (Linux GNU coreutils).  No mocking of the
subprocess layer.  Covers organize_media.py's no-clobber primitive section.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from organize_media import make_primitive


# Module-scope primitives so the probe / closure construction runs once
# per file, not once per test.
@pytest.fixture(scope="module")
def copy_primitive():
    return make_primitive(move=False)


@pytest.fixture(scope="module")
def move_primitive():
    return make_primitive(move=True)


# ---------------------------------------------------------------------------
# copy
# ---------------------------------------------------------------------------


def test_copy_happy_path(copy_primitive, write_bytes, tmp_path: Path) -> None:
    src = write_bytes("src.bin", b"hello")
    os.chmod(src, 0o640)
    dst = tmp_path / "dst.bin"

    result = copy_primitive(src, dst)

    assert result is True
    assert dst.exists()
    assert dst.read_bytes() == b"hello"
    # --preserve=all carries mode bits across (regardless of reflink vs full copy).
    assert stat.S_IMODE(dst.stat().st_mode) == 0o640


def test_copy_collision_returns_false(copy_primitive, write_bytes, tmp_path: Path) -> None:
    src = write_bytes("src.bin", b"new-content")
    dst = write_bytes("dst.bin", b"original-content")

    result = copy_primitive(src, dst)

    assert result is False
    # dst must not have been overwritten with src bytes.
    assert dst.read_bytes() == b"original-content"


def test_copy_failure_missing_src(copy_primitive, tmp_path: Path) -> None:
    missing_src = tmp_path / "does_not_exist.bin"
    dst = tmp_path / "dst.bin"

    with pytest.raises(subprocess.CalledProcessError):
        copy_primitive(missing_src, dst)
    # The O_EXCL placeholder must be cleaned up after a failed copy.
    assert not dst.exists()


# ---------------------------------------------------------------------------
# move
# ---------------------------------------------------------------------------


def test_move_happy_path(move_primitive, write_bytes, tmp_path: Path) -> None:
    src = write_bytes("src.bin", b"hello")
    dst = tmp_path / "dst.bin"

    result = move_primitive(src, dst)

    assert result is True
    assert not src.exists()
    assert dst.exists()
    assert dst.read_bytes() == b"hello"


def test_move_collision_returns_false(move_primitive, write_bytes, tmp_path: Path) -> None:
    src = write_bytes("src.bin", b"aaaa")
    dst = write_bytes("dst.bin", b"bbbb")

    result = move_primitive(src, dst)

    assert result is False
    # src untouched.
    assert src.exists()
    assert src.read_bytes() == b"aaaa"
    # dst untouched.
    assert dst.read_bytes() == b"bbbb"


def test_move_failure_missing_src(move_primitive, tmp_path: Path) -> None:
    missing_src = tmp_path / "does_not_exist.bin"
    dst = tmp_path / "dst.bin"

    with pytest.raises(subprocess.CalledProcessError):
        move_primitive(missing_src, dst)
    # Placeholder cleaned up.
    assert not dst.exists()


# ---------------------------------------------------------------------------
# factory shape
# ---------------------------------------------------------------------------


def test_make_primitive_returns_distinct_closures():
    """Each call returns a new closure; the move flag selects cp vs mv."""
    a = make_primitive(move=False)
    b = make_primitive(move=False)
    c = make_primitive(move=True)
    assert a is not b
    assert a is not c


def test_copy_primitive_passable_to_claim_dest_path(tmp_path, write_bytes) -> None:
    """A primitive built by make_primitive satisfies claim_dest_path's contract."""
    from datetime import datetime
    from organize_media import claim_dest_path

    src = write_bytes("src.jpg", b"x")
    dest = tmp_path / "dest"
    out = claim_dest_path(dest, datetime(2020, 1, 1, 0, 0, 0), src,
                          primitive=make_primitive(move=False))
    assert out.exists() and out.read_bytes() == b"x"


def test_move_primitive_passable_to_claim_dest_path(tmp_path, write_bytes) -> None:
    from datetime import datetime
    from organize_media import claim_dest_path

    src = write_bytes("src.jpg", b"y")
    dest = tmp_path / "dest"
    out = claim_dest_path(dest, datetime(2021, 2, 2, 1, 1, 1), src,
                          primitive=make_primitive(move=True))
    assert out.exists() and not src.exists()
