"""Tests for organize_media.claim_dest_path (lines 455-493)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytest

from organize_media import claim_dest_path


DT = datetime(2020, 6, 15, 12, 30, 45)


def test_basic_placement(dest: Path, write_bytes) -> None:
    """Default reflink_copy primitive places file at 2020/06/...0001.jpg."""
    src = write_bytes("src.jpg", data=b"hello world")
    result = claim_dest_path(dest, DT, src)

    expected = dest / "2020" / "06" / "2020-06-15 12-30-45 0001.jpg"
    assert result == expected
    assert result.exists()
    assert result.read_bytes() == b"hello world"


def test_nnnn_increment(dest: Path, write_bytes) -> None:
    """Pre-existing 0001 and 0002 cause the function to return 0003."""
    src = write_bytes("src.jpg", data=b"payload")
    month_dir = dest / "2020" / "06"
    month_dir.mkdir(parents=True)
    (month_dir / "2020-06-15 12-30-45 0001.jpg").write_bytes(b"")
    (month_dir / "2020-06-15 12-30-45 0002.jpg").write_bytes(b"")

    result = claim_dest_path(dest, DT, src)

    expected = month_dir / "2020-06-15 12-30-45 0003.jpg"
    assert result == expected
    assert result.exists()
    assert result.read_bytes() == b"payload"


def test_dry_run(dest: Path, write_bytes) -> None:
    """dry_run=True returns candidate path with no filesystem side effects."""
    src = write_bytes("src.jpg", data=b"x")
    assert not dest.exists()

    result = claim_dest_path(dest, DT, src, dry_run=True)

    expected = dest / "2020" / "06" / "2020-06-15 12-30-45 0001.jpg"
    assert result == expected
    assert not result.exists()
    # mkdir is gated on `not dry_run`, so dest itself must not have come into being
    assert not dest.exists()


@pytest.mark.xfail(
    reason=(
        "Production bug exposed by this test: under GNU coreutils 9.4 "
        "`cp --no-clobber` returns rc=0 silently when the destination already "
        "exists (only emits a portability warning on stderr).  `reflink_copy` "
        "therefore returns True for both real copies and skipped collisions, "
        "and `claim_dest_path` returns the same path to multiple racing "
        "callers.  Fix in organize_media.py:reflink_copy: switch to "
        "`--update=none-fail` (cp 9.4+) or compare inode/content after the call."
    ),
    strict=True,
)
def test_concurrent_claim(dest: Path, write_bytes) -> None:
    """20 concurrent threads each get a distinct path, all under same month dir."""
    src = write_bytes("src.jpg", data=b"shared-content")

    with ThreadPoolExecutor(max_workers=20) as exe:
        futures = [exe.submit(claim_dest_path, dest, DT, src) for _ in range(20)]
        results = [f.result() for f in futures]

    # All distinct
    assert len(set(results)) == 20
    # All exist
    for p in results:
        assert p.exists(), f"missing: {p}"
    # All share the same month dir
    month_dir = dest / "2020" / "06"
    for p in results:
        assert p.parent == month_dir
    # Filenames carry NNNN counters; no duplicates
    stems = [p.stem for p in results]  # e.g. "2020-06-15 12-30-45 0007"
    nnnns = [s.rsplit(" ", 1)[-1] for s in stems]
    assert len(set(nnnns)) == 20
    # Each NNNN is 4 digits between 0001 and 9999
    for n in nnnns:
        assert n.isdigit() and len(n) == 4
        assert 1 <= int(n) <= 9999


@pytest.mark.timeout(10)
def test_exhaustion_raises_runtime_error(dest: Path, write_bytes) -> None:
    """Primitive that always returns False (race-lost) raises after 10,000 tries."""
    src = write_bytes("src.jpg", data=b"x")

    def always_taken(src_arg: Path, dst_arg: Path) -> bool:
        return False

    with pytest.raises(RuntimeError):
        claim_dest_path(dest, DT, src, primitive=always_taken)


def test_primitive_receives_candidate(dest: Path, write_bytes) -> None:
    """Custom primitive is called exactly once with (src, returned_path)."""
    src = write_bytes("src.jpg", data=b"x")
    calls: list[tuple[Path, Path]] = []

    def cap(src_arg: Path, dst_arg: Path) -> bool:
        calls.append((src_arg, dst_arg))
        return True

    result = claim_dest_path(dest, DT, src, primitive=cap)

    assert len(calls) == 1
    got_src, got_dst = calls[0]
    assert got_src == src
    assert got_dst == result
    expected = dest / "2020" / "06" / "2020-06-15 12-30-45 0001.jpg"
    assert result == expected


def test_primitive_false_on_first_forces_nnnn_0002(dest: Path, write_bytes) -> None:
    """Primitive returning False for 0001 (simulated race) forces fallthrough to 0002."""
    src = write_bytes("src.jpg", data=b"x")
    calls: list[Path] = []

    def stateful(src_arg: Path, dst_arg: Path) -> bool:
        calls.append(dst_arg)
        if "0001" in dst_arg.name:
            return False
        return True

    result = claim_dest_path(dest, DT, src, primitive=stateful)

    expected = dest / "2020" / "06" / "2020-06-15 12-30-45 0002.jpg"
    assert result == expected
    # Primitive was called for 0001 (False) and then 0002 (True)
    assert len(calls) == 2
    assert "0001" in calls[0].name
    assert "0002" in calls[1].name
