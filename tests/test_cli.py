"""CLI tests for organize_media.main (lines 1022-1108)."""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "organize_media.py"


def run_cli(*args, cwd=None, stdin_data=None):
    venv_python = PROJECT_ROOT / ".venv" / "bin" / "python"
    py = str(venv_python) if venv_python.exists() else sys.executable
    return subprocess.run(
        [py, str(SCRIPT), *map(str, args)],
        capture_output=True,
        text=True,
        input=stdin_data,
        cwd=cwd or PROJECT_ROOT,
    )


def test_exclude_not_a_directory(tmp_path, make_jpeg, dest):
    src = tmp_path / "src"
    make_jpeg("a.jpg", dt=datetime(2020, 6, 15, 12, 30, 45), directory=src)

    bogus = "/nonexistent/path"
    result = run_cli(str(src), str(dest), "--exclude", bogus)

    assert result.returncode != 0
    assert f"Error: --exclude '{bogus}' is not a directory." in result.stderr


def test_reorganize_dest_not_a_directory(tmp_path):
    dest_file = tmp_path / "not_a_dir.txt"
    dest_file.write_bytes(b"hello")

    result = run_cli(str(dest_file), "--reorganize")

    assert result.returncode != 0
    assert "Error: destination" in result.stderr


def test_source_not_a_directory(tmp_path, dest):
    src_file = tmp_path / "afile.txt"
    src_file.write_bytes(b"hi")

    result = run_cli(str(src_file), str(dest))

    assert result.returncode != 0
    assert "Error: source" in result.stderr


def test_cache_creates_db(tmp_path, make_jpeg, dest):
    src = tmp_path / "src"
    make_jpeg("img.jpg", dt=datetime(2020, 6, 15, 12, 30, 45), directory=src)

    cache_path = tmp_path / "cache.sqlite"
    assert not cache_path.exists()

    result = run_cli(str(src), str(dest), "--cache", str(cache_path))

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert cache_path.exists(), "cache db should be created when --cache is passed"


def test_no_cache_no_db(tmp_path, make_jpeg, dest):
    src = tmp_path / "src"
    make_jpeg("img.jpg", dt=datetime(2020, 6, 15, 12, 30, 45), directory=src)

    # Use tmp_path as cwd so we can scan it for sqlite files post-run.
    work_cwd = tmp_path / "work"
    work_cwd.mkdir()

    result = run_cli(str(src), str(dest), cwd=work_cwd)

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"

    # No .sqlite anywhere inside the working tree (cwd or tmp_path).
    for root in (work_cwd, tmp_path):
        sqlite_files = list(root.rglob("*.sqlite"))
        assert sqlite_files == [], f"unexpected sqlite files: {sqlite_files}"


def test_happy_path_smoke(tmp_path, make_jpeg, dest):
    src = tmp_path / "src"
    make_jpeg("photo.jpg", dt=datetime(2020, 6, 15, 12, 30, 45), directory=src)

    result = run_cli(str(src), str(dest))

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    expected = dest / "2020" / "06" / "2020-06-15 12-30-45 0001.jpg"
    assert expected.exists(), f"expected {expected} to exist; dest tree: {list(dest.rglob('*'))}"


# ── stdin source (-) ──────────────────────────────────────────────────────────


def test_stdin_source_files_dry_run(tmp_path, make_jpeg, dest):
    a = make_jpeg("a.jpg", dt=datetime(2021, 3, 10, 8, 0, 0), directory=tmp_path / "src")
    b = make_jpeg("b.jpg", dt=datetime(2021, 4, 20, 9, 0, 0), directory=tmp_path / "src")

    result = run_cli("--dry-run", "--", "-", str(dest), stdin_data=f"{a}\n{b}\n")

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "Found 2 media file(s)" in result.stderr


def test_stdin_source_dir_dry_run(tmp_path, make_jpeg, dest):
    src = tmp_path / "src"
    make_jpeg("a.jpg", dt=datetime(2021, 3, 10, 8, 0, 0), directory=src)
    make_jpeg("b.jpg", dt=datetime(2021, 4, 20, 9, 0, 0), directory=src / "sub")

    result = run_cli("--dry-run", "--", "-", str(dest), stdin_data=f"{src}\n")

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "Found 2 media file(s)" in result.stderr


def test_stdin_source_non_media_skipped(tmp_path, dest):
    txt = tmp_path / "notes.txt"
    txt.write_text("hello")

    result = run_cli("--dry-run", "--", "-", str(dest), stdin_data=f"{txt}\n")

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "No media files found" in result.stderr


def test_stdin_source_empty_no_media(dest):
    result = run_cli("--dry-run", "--", "-", str(dest), stdin_data="")

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "No media files found" in result.stderr


def test_stdin_source_mutual_exclusion(tmp_path, dest):
    src = tmp_path / "src"
    src.mkdir()

    result = run_cli("--dry-run", "-", str(src), str(dest), stdin_data="")

    assert result.returncode != 0
    assert "'-' must be the only source" in result.stderr


def test_stdin_source_reorganize_rejected(tmp_path):
    result = run_cli("--reorganize", "--dry-run", "--", "-", str(tmp_path), stdin_data="")

    assert result.returncode != 0
    assert "--reorganize does not accept '-'" in result.stderr
