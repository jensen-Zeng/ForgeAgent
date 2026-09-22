"""Write deterministic SHA-256 checksums for release artifacts."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


def write_checksums(directory: Path) -> Path:
    """Hash regular release files except a previous checksum manifest."""
    resolved = directory.resolve(strict=True)
    artifacts = sorted(
        path for path in resolved.iterdir() if path.is_file() and path.name != "SHA256SUMS.txt"
    )
    if not artifacts:
        raise ValueError("release directory contains no artifacts")
    lines = [f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}" for path in artifacts]
    output = resolved / "SHA256SUMS.txt"
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    arguments = parser.parse_args()
    print(write_checksums(arguments.directory))


if __name__ == "__main__":
    main()
