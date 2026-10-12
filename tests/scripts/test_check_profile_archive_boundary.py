"""Behavioral tests for the profile archive CI guard."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "ci"
    / "check_profile_archive_boundary.py"
)


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_clean_root_passes(tmp_path):
    result = _run(tmp_path)

    assert result.returncode == 0
    assert "No profile export archives" in result.stdout


def test_root_profile_archive_fails_without_printing_contents(tmp_path):
    default_archive = tmp_path / "default.tar.gz"
    alternate_archive = tmp_path / "backup.TGZ"
    default_archive.write_bytes(b"profile webhook secret must never be printed")
    alternate_archive.write_bytes(b"another archive")

    result = _run(tmp_path)

    assert result.returncode == 1
    assert "default.tar.gz" in result.stdout
    assert "backup.TGZ" in result.stdout
    assert "profile webhook secret" not in result.stdout
    assert "another archive" not in result.stdout


def test_nested_profile_archive_is_also_rejected(tmp_path):
    nested = tmp_path / "fixtures"
    nested.mkdir()
    (nested / "fixture.tar.gz").write_bytes(b"test fixture")

    result = _run(tmp_path)

    assert result.returncode == 1
    assert "fixtures/fixture.tar.gz" in result.stdout


def test_cached_desktop_packaging_inputs_allowed(tmp_path):
    cache_dir = tmp_path / ".cache" / "desktop-inputs" / "packager" / "builder" / "7zip@1.0.0"
    cache_dir.mkdir(parents=True)
    (cache_dir / "7zip-darwin-arm64.tar.gz").write_bytes(b"binary tool payload")

    result = _run(tmp_path)

    assert result.returncode == 0
    assert "No profile export archives" in result.stdout


def test_user_profile_archive_under_cache_is_rejected(tmp_path):
    cache_dir = tmp_path / ".cache" / "desktop-inputs"
    cache_dir.mkdir(parents=True)
    (cache_dir / "user-profile.tar.gz").write_bytes(b"leaked profile export")

    result = _run(tmp_path)

    assert result.returncode == 1
    assert ".cache/desktop-inputs/user-profile.tar.gz" in result.stdout


def test_unrecognized_archive_under_builder_cache_is_rejected(tmp_path):
    builder_cache = tmp_path / ".cache" / "desktop-inputs" / "packager" / "builder" / "custom"
    builder_cache.mkdir(parents=True)
    (builder_cache / "unexpected.tar.gz").write_bytes(b"unexpected archive")

    result = _run(tmp_path)

    assert result.returncode == 1
    assert ".cache/desktop-inputs/packager/builder/custom/unexpected.tar.gz" in result.stdout
