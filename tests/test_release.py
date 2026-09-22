"""Tests for release artifact integrity helpers."""

import hashlib
import re
from pathlib import Path

import pytest
from scripts.write_checksums import write_checksums


def test_checksums_are_sorted_and_ignore_old_manifest(tmp_path: Path) -> None:
    (tmp_path / "b.whl").write_bytes(b"wheel")
    (tmp_path / "a.tar.gz").write_bytes(b"source")
    (tmp_path / "SHA256SUMS.txt").write_text("stale", encoding="utf-8")

    output = write_checksums(tmp_path)
    lines = output.read_text(encoding="utf-8").splitlines()

    assert [line.split("  ")[1] for line in lines] == ["a.tar.gz", "b.whl"]
    assert lines[0].startswith(hashlib.sha256(b"source").hexdigest())


def test_checksums_reject_empty_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no artifacts"):
        write_checksums(tmp_path)


def test_constraints_are_exact_unique_pins() -> None:
    root = Path(__file__).parents[1]
    lines = [
        line
        for line in (root / "requirements" / "constraints.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line and not line.startswith("#")
    ]
    names = [re.split(r"==", line, maxsplit=1)[0].casefold().replace("_", "-") for line in lines]

    assert all("==" in line for line in lines)
    assert len(names) == len(set(names))
    assert {"httpx", "pydantic", "pytest-cov", "ruff"}.issubset(names)


def test_release_workflow_has_manual_environment_and_integrity_checks() -> None:
    root = Path(__file__).parents[1]
    workflow = (root / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "environment: release" in workflow
    assert "twine check" in workflow
    assert "write_checksums.py" in workflow
    assert "tag {tag!r} does not match" in workflow
