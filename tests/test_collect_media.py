"""Tests for organize_media.collect_media and _iter_source_paths."""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

import organize_media
from organize_media import collect_media, _iter_source_paths


def _resolved_sorted(results: list[tuple[Path, int]]) -> list[tuple[Path, int]]:
    return sorted((p.resolve(), s) for p, s in results)


def test_empty_source_dir(tmp_path: Path) -> None:
    assert collect_media([tmp_path]) == []


def test_mixed_dir_filters_and_recurses(tmp_path, write_bytes) -> None:
    a = write_bytes("a.jpg", size=123)
    b = write_bytes("b.png", size=456)
    c = write_bytes("c.mp4", size=789)

    # Non-media files — should be skipped.
    write_bytes("notes.txt", b"hello")
    write_bytes("data.json", b"{}")
    write_bytes("README.md", b"# readme")
    write_bytes("script.py", b"print()")

    nested_dir = tmp_path / "sub"
    nested = write_bytes("nested.jpeg", size=42, into=nested_dir)

    result = collect_media([tmp_path])
    got = _resolved_sorted(result)
    expected = sorted(
        [
            (a.resolve(), 123),
            (b.resolve(), 456),
            (c.resolve(), 789),
            (nested.resolve(), 42),
        ]
    )
    assert got == expected


def test_all_supported_extensions(tmp_path: Path) -> None:
    # Write one empty file per extension.
    expected_paths: set[Path] = set()
    for i, ext in enumerate(sorted(organize_media.ALL_EXTS)):
        # Use a unique stem to avoid collisions (e.g. ".tif" vs ".tiff" are distinct).
        p = tmp_path / f"file_{i}{ext}"
        p.write_bytes(b"")
        expected_paths.add(p.resolve())

    result = collect_media([tmp_path])
    got_paths = {p.resolve() for p, _ in result}
    assert got_paths == expected_paths
    # All zero-sized.
    assert all(size == 0 for _, size in result)


def test_case_insensitive_suffix_match(tmp_path, write_bytes) -> None:
    img = write_bytes("IMG.JPG", size=10)
    mov = write_bytes("MOVIE.MP4", size=20)

    result = collect_media([tmp_path])
    got = {p.resolve() for p, _ in result}
    assert got == {img.resolve(), mov.resolve()}


def test_multiple_sources(tmp_path, write_bytes) -> None:
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    f_a = write_bytes("one.jpg", size=11, into=dir_a)
    f_b = write_bytes("two.jpg", size=22, into=dir_b)

    result = collect_media([dir_a, dir_b])
    got = _resolved_sorted(result)
    expected = sorted([(f_a.resolve(), 11), (f_b.resolve(), 22)])
    assert got == expected


def test_file_vanished_race_swallows_oserror(tmp_path, write_bytes, monkeypatch) -> None:
    keep = write_bytes("keep.jpg", size=5)
    vanish = write_bytes("vanish.jpg", size=5)
    # Capture resolved paths *before* the monkeypatch — assertions below would
    # otherwise trigger the same `OSError` they're trying to detect.
    keep_resolved = keep.resolve()
    vanish_resolved = vanish.resolve()

    # `collect_media` calls `is_file()` (which calls stat internally) BEFORE
    # the try/except.  Only `resolve()` and the outer `.stat().st_size` are
    # guarded.  Simulate the post-`is_file` vanish by monkeypatching `resolve`.
    orig_resolve = Path.resolve

    def maybe_raise(self, *args, **kwargs):
        if self.name == "vanish.jpg":
            raise OSError("vanished")
        return orig_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", maybe_raise)

    result = collect_media([tmp_path])
    got = {p for p, _ in result}
    assert got == {keep_resolved}
    # Sanity: vanished file is excluded.
    assert vanish_resolved not in got


def test_returned_paths_are_absolute(tmp_path, write_bytes, monkeypatch) -> None:
    # Create a file inside tmp_path, then cd into tmp_path so we can pass a relative source.
    write_bytes("rel.jpg", size=7)
    monkeypatch.chdir(tmp_path)

    rel_source = Path(".")
    assert not rel_source.is_absolute()

    result = collect_media([rel_source])
    assert len(result) == 1
    for p, _ in result:
        assert p.is_absolute(), f"expected absolute path, got {p!r}"


# ── _iter_source_paths ────────────────────────────────────────────────────────


def test_iter_file_source(write_bytes) -> None:
    f = write_bytes("img.jpg", size=10)
    assert list(_iter_source_paths([f])) == [f]


def test_iter_dir_source(tmp_path, write_bytes) -> None:
    a = write_bytes("a.jpg", size=1)
    b = write_bytes("b.jpg", size=2, into=tmp_path / "sub")
    result = set(_iter_source_paths([tmp_path]))
    assert a in result
    assert b in result


def test_iter_stdin_files(tmp_path, write_bytes, monkeypatch) -> None:
    a = write_bytes("a.jpg", size=1)
    b = write_bytes("b.jpg", size=2)
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{a}\n{b}\n"))
    result = list(_iter_source_paths([Path("-")]))
    assert set(result) == {a, b}


def test_iter_stdin_dir_walks_recursively(tmp_path, write_bytes, monkeypatch) -> None:
    a = write_bytes("a.jpg", size=1)
    b = write_bytes("b.jpg", size=2, into=tmp_path / "sub")
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{tmp_path}\n"))
    result = set(_iter_source_paths([Path("-")]))
    assert a in result
    assert b in result


def test_iter_stdin_empty_lines_skipped(tmp_path, write_bytes, monkeypatch) -> None:
    f = write_bytes("x.jpg", size=5)
    monkeypatch.setattr("sys.stdin", io.StringIO(f"\n  \n{f}\n\n"))
    result = list(_iter_source_paths([Path("-")]))
    assert result == [f]


def test_iter_stdin_nonexistent_skipped(tmp_path, monkeypatch) -> None:
    ghost = tmp_path / "ghost.jpg"
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{ghost}\n"))
    assert list(_iter_source_paths([Path("-")])) == []


def test_iter_stdin_mixed_file_and_dir(tmp_path, write_bytes, monkeypatch) -> None:
    f = write_bytes("direct.jpg", size=1)
    nested = write_bytes("nested.jpg", size=2, into=tmp_path / "sub")
    sub = tmp_path / "sub"
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{f}\n{sub}\n"))
    result = set(_iter_source_paths([Path("-")]))
    assert f in result
    assert nested in result
