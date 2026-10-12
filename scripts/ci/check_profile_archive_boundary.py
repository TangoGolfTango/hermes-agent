#!/usr/bin/env python3
"""Reject profile export archives before publication.

``.gitignore`` and ``.dockerignore`` are useful first-line filters, but both
can be bypassed (for example with ``git add -f`` or a non-standard build
context).  This check is the blocking, executable policy at the CI and image
publication boundaries.  It intentionally checks the filesystem rather than
Git's index so a generated archive cannot enter a build after checkout.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

_PROFILE_ARCHIVE_SUFFIXES = (".tar.gz", ".tgz")

_KNOWN_DESKTOP_BUILDER_INPUT_ARCHIVES = frozenset({
    "7zip-darwin-arm64.tar.gz",
    "7zip-darwin-x64.tar.gz",
    "7zip-linux-arm64.tar.gz",
    "7zip-linux-x64.tar.gz",
    "7zip-win32-arm64.tar.gz",
    "7zip-win32-x64.tar.gz",
    "icons-bundle.tar.gz",
})


def _is_managed_desktop_input(relpath: Path) -> bool:
    """Return True only for recognized third-party packager tool inputs."""
    posix_parts = relpath.as_posix().split("/")
    # Expected under .cache/desktop-inputs/packager/builder/...
    if len(posix_parts) >= 4 and posix_parts[:3] == [".cache", "desktop-inputs", "packager"]:
        if posix_parts[3] == "builder" and relpath.name in _KNOWN_DESKTOP_BUILDER_INPUT_ARCHIVES:
            return True
    return False


def find_forbidden_profile_archives(root: Path) -> list[Path]:
    """Return profile archive paths anywhere in the checkout."""
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"repository root is not a directory: {root}")

    offenders: list[Path] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirpath = Path(directory)
        dirnames[:] = [
            name
            for name in dirnames
            if name not in {".git", ".venv", "venv", "node_modules", "__pycache__"}
        ]
        for name in (*dirnames, *filenames):
            if name.casefold().endswith(_PROFILE_ARCHIVE_SUFFIXES):
                rel = (dirpath / name).relative_to(root)
                if not _is_managed_desktop_input(rel):
                    offenders.append(rel)

    return sorted(offenders, key=lambda path: path.as_posix().casefold())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reject profile export archives in the checkout."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repository root to inspect (default: current directory)",
    )
    args = parser.parse_args(argv)

    try:
        offenders = find_forbidden_profile_archives(args.root)
    except ValueError as exc:
        parser.error(str(exc))

    if not offenders:
        print("No profile export archives detected in the checkout.")
        return 0

    print(
        "::error::profile export archives are forbidden "
        "in source and Docker build contexts"
    )
    for path in offenders:
        print(f"  {path.as_posix()}")
    print(
        "Move the archive outside the checkout or pass an explicit external "
        "output path to the profile export command."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
