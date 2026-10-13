"""Create and inspect isolated Git worktrees without shell execution."""

from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_MAX_DIFF_CHARS: Final = 100_000
_MAX_FILE_BYTES: Final = 256_000
_MAX_FILES: Final = 200
_COMMIT_RE: Final = re.compile(r"^[0-9a-f]{40,64}$")
_SENSITIVE_NAMES: Final = frozenset(
    {
        ".env",
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "known_hosts",
        "netrc",
        ".netrc",
    }
)
_SENSITIVE_SUFFIXES: Final = frozenset({".key", ".pem", ".p12", ".pfx"})
_ENV_ALLOWLIST: Final = frozenset(
    {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "XDG_CONFIG_HOME",
    }
)


class WorktreeError(ValueError):
    """A worktree operation failed with a stable machine-readable code."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class IsolatedWorkspace:
    """A managed checkout tied to the commit from which it was created."""

    id: str
    source: Path
    path: Path
    branch: str
    base_commit: str


class WorktreeManager:
    """Manage independent checkouts rooted under ``source/.forge/worktrees``."""

    def __init__(self, source: Path) -> None:
        try:
            self.source = source.expanduser().resolve(strict=True)
        except OSError as error:
            raise WorktreeError("Source directory does not exist", code="invalid_source") from error
        if not self.source.is_dir():
            raise WorktreeError("Source must be a directory", code="invalid_source")
        self.storage = self.source / ".forge" / "worktrees"
        top = self._git(["rev-parse", "--show-toplevel"], code="not_git_repository")
        if Path(top.stdout.strip()).resolve() != self.source:
            raise WorktreeError("Source must be the Git repository top-level", code="not_git_root")
        self._git(["rev-parse", "--verify", "HEAD^{commit}"], code="no_commit")

    def create(self) -> IsolatedWorkspace:
        """Create a new branch and checkout from the current clean source HEAD."""
        self._reject_external_filters(self._tracked_paths())
        status = self._git(
            ["status", "--porcelain=v1", "--untracked-files=all", "-z"],
            code="git_error",
        ).stdout
        unexpected: list[str] = []
        for item in (entry for entry in status.split("\0") if len(entry) >= 4):
            status_code, path = item[:2], item[3:]
            untracked_forge_data = status_code == "??" and (
                path == ".forge" or path.startswith(".forge/")
            )
            if not untracked_forge_data:
                unexpected.append(path)
        if unexpected:
            raise WorktreeError(
                "Commit or remove source changes before creating an isolated worktree: "
                + ", ".join(unexpected[:5]),
                code="workspace_dirty",
            )
        base_commit = self._git(["rev-parse", "--verify", "HEAD^{commit}"]).stdout.strip()
        self._ensure_storage()
        identifier = str(uuid.uuid4())
        branch = f"forge/{identifier}"
        root = self.storage / identifier
        checkout = root / "checkout"
        self._ensure_directory(root)
        # Git hooks use the OS null device as the hook directory. A failed add is
        # reported with its residual path; no user data is recursively removed.
        try:
            self._git(
                ["worktree", "add", "-b", branch, str(checkout), base_commit],
                timeout=60,
                code="worktree_create_failed",
            )
        except WorktreeError as error:
            raise WorktreeError(
                f"Worktree creation failed; inspect any residual path: {root}",
                code=error.code,
            ) from error
        manifest = {
            "version": 1,
            "id": identifier,
            "source": str(self.source),
            "path": str(checkout),
            "branch": branch,
            "base_commit": base_commit,
        }
        try:
            manifest_path = root / "manifest.json"
            temporary = root / "manifest.json.tmp"
            temporary.write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, manifest_path)
        except OSError as error:
            raise WorktreeError(
                "Worktree was created but its manifest could not be written; "
                f"residual path: {root}",
                code="manifest_write_failed",
            ) from error
        return IsolatedWorkspace(identifier, self.source, checkout, branch, base_commit)

    def list(self) -> tuple[IsolatedWorkspace, ...]:
        """List and validate managed worktrees without changing Git state."""
        if not self._validate_storage(missing_ok=True):
            return ()
        entries: list[IsolatedWorkspace] = []
        try:
            children = sorted(self.storage.iterdir(), key=lambda item: item.name)
        except OSError as error:
            raise WorktreeError("Unable to list worktrees", code="worktree_list_failed") from error
        for root in children:
            if not _is_uuid(root.name) or _is_linklike(root) or not root.is_dir():
                raise WorktreeError("Invalid worktree entry", code="manifest_invalid")
            entries.append(self._load(root.name))
        return tuple(entries)

    def get(self, identifier: str) -> IsolatedWorkspace:
        """Return one validated managed worktree by canonical UUID."""
        if not _is_uuid(identifier):
            raise WorktreeError("Invalid worktree id", code="invalid_id")
        return self._load(identifier)

    def diff(self, identifier: str) -> str:
        """Return a bounded base-to-working-tree text diff, excluding secrets."""
        workspace = self.get(identifier)
        # Do not ask diff for its path list until filters are ruled out: even a
        # name-only diff may clean worktree content while comparing to the tree.
        untracked = self._git(
            ["ls-files", "--others", "--exclude-standard", "-z"], cwd=workspace.path
        ).stdout.split("\0")
        base_paths = self._git(
            ["ls-tree", "-r", "--name-only", "-z", workspace.base_commit],
            cwd=workspace.path,
        ).stdout.split("\0")
        preflight_paths = tuple(
            dict.fromkeys(
                path
                for path in (*self._tracked_paths(cwd=workspace.path), *base_paths, *untracked)
                if path
            )
        )
        self._reject_external_filters(preflight_paths, cwd=workspace.path)
        changed = self._git(
            [
                "diff",
                "--no-textconv",
                "--name-only",
                "-z",
                "--no-renames",
                workspace.base_commit,
                "--",
            ],
            cwd=workspace.path,
        ).stdout.split("\0")
        paths = list(dict.fromkeys(path for path in (*changed, *untracked) if path))
        chunks: list[str] = []
        for relative in paths[:_MAX_FILES]:
            candidate = self._safe_candidate(workspace.path, relative)
            if candidate is None or _sensitive(relative):
                continue
            if self._base_is_symlink(workspace, relative):
                continue
            if _is_linklike(candidate):
                continue
            if relative in untracked:
                chunk = _untracked_diff(candidate, relative)
            else:
                if candidate.is_file() and candidate.stat().st_size > _MAX_FILE_BYTES:
                    continue
                if self._base_file_too_large(workspace, relative):
                    continue
                if not candidate.exists():
                    chunk = self._git(
                        [
                            "diff",
                            "--no-ext-diff",
                            "--no-color",
                            "--no-textconv",
                            "--no-renames",
                            workspace.base_commit,
                            "--",
                            relative,
                        ],
                        cwd=workspace.path,
                    ).stdout
                elif not candidate.is_file():
                    continue
                else:
                    chunk = self._git(
                        [
                            "diff",
                            "--no-ext-diff",
                            "--no-color",
                            "--no-textconv",
                            "--no-renames",
                            workspace.base_commit,
                            "--",
                            relative,
                        ],
                        cwd=workspace.path,
                    ).stdout
            if chunk:
                chunks.append(chunk.rstrip())
        output = "\n".join(chunks)
        if len(paths) > _MAX_FILES:
            output += "\n...[file limit reached]"
        if len(output) > _MAX_DIFF_CHARS:
            output = output[:_MAX_DIFF_CHARS] + "\n...[truncated]"
        return output or "(no diff)"

    def _load(self, identifier: str) -> IsolatedWorkspace:
        self._validate_storage()
        root = self.storage / identifier
        manifest_path = root / "manifest.json"
        if _is_linklike(root) or _is_linklike(manifest_path) or not manifest_path.is_file():
            raise WorktreeError("Worktree manifest is missing or unsafe", code="manifest_invalid")
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise WorktreeError(
                "Worktree manifest cannot be read", code="manifest_invalid"
            ) from error
        expected = {"version", "id", "source", "path", "branch", "base_commit"}
        if not isinstance(data, dict) or set(data) != expected:
            raise WorktreeError("Worktree manifest has an invalid schema", code="manifest_invalid")
        if (
            data["version"] != 1
            or data["id"] != identifier
            or data["source"] != str(self.source)
            or data["branch"] != f"forge/{identifier}"
            or not isinstance(data["path"], str)
            or not _COMMIT_RE.fullmatch(str(data["base_commit"]))
        ):
            raise WorktreeError("Worktree manifest was changed", code="manifest_invalid")
        checkout = root / "checkout"
        if data["path"] != str(checkout) or _is_linklike(checkout) or not checkout.is_dir():
            raise WorktreeError("Worktree path is invalid", code="manifest_invalid")
        top = self._git(["rev-parse", "--show-toplevel"], cwd=checkout, code="manifest_invalid")
        branch = self._git(
            ["symbolic-ref", "--short", "HEAD"], cwd=checkout, code="manifest_invalid"
        )
        source_common = self._common_dir(self.source)
        checkout_common = self._common_dir(checkout)
        ancestor = self._git(
            ["merge-base", "--is-ancestor", data["base_commit"], "HEAD"],
            cwd=checkout,
            code="manifest_invalid",
        )
        del ancestor
        if (
            Path(top.stdout.strip()).resolve() != checkout.resolve()
            or branch.stdout.strip() != data["branch"]
            or checkout_common != source_common
        ):
            raise WorktreeError(
                "Worktree checkout does not match its manifest", code="manifest_invalid"
            )
        return IsolatedWorkspace(
            identifier, self.source, checkout, data["branch"], data["base_commit"]
        )

    def _safe_candidate(self, root: Path, relative: str) -> Path | None:
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            return None
        candidate = root.joinpath(*path.parts)
        current = root
        for part in path.parts:
            current = current / part
            if _is_linklike(current):
                return None
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(root.resolve())
        except (OSError, ValueError):
            return None
        return candidate

    def _common_dir(self, checkout: Path) -> Path:
        raw = Path(self._git(["rev-parse", "--git-common-dir"], cwd=checkout).stdout.strip())
        return (raw if raw.is_absolute() else checkout / raw).resolve()

    def _base_is_symlink(self, workspace: IsolatedWorkspace, relative: str) -> bool:
        result = self._git(
            ["ls-tree", "-r", "-z", "--full-tree", workspace.base_commit, "--", relative],
            cwd=workspace.path,
        )
        return any(record.startswith("120000 ") for record in result.stdout.split("\0"))

    def _base_file_too_large(self, workspace: IsolatedWorkspace, relative: str) -> bool:
        try:
            size = self._git(
                ["cat-file", "-s", f"{workspace.base_commit}:{relative}"], cwd=workspace.path
            ).stdout.strip()
        except WorktreeError:
            return False
        return size.isdecimal() and int(size) > _MAX_FILE_BYTES

    def _tracked_paths(self, *, cwd: Path | None = None) -> tuple[str, ...]:
        return tuple(
            path for path in self._git(["ls-files", "-z"], cwd=cwd).stdout.split("\0") if path
        )

    def _reject_external_filters(self, paths: Sequence[str], *, cwd: Path | None = None) -> None:
        """Fail closed if checkout/diff would execute a configured filter command.

        Git's clean/smudge/process filters are arbitrary local programs. Disabling
        them would silently turn LFS pointers into ordinary files, so repositories
        that actually use an external filter are unsupported for managed worktrees.
        """
        if not paths:
            return
        attributes = self._git(
            ["check-attr", "-z", "filter", "--stdin"],
            cwd=cwd,
            input_text="".join(f"{path}\0" for path in paths),
        ).stdout.split("\0")
        active_drivers = {
            attributes[index + 2].casefold()
            for index in range(0, len(attributes) - 2, 3)
            if attributes[index + 1] == "filter"
            and attributes[index + 2] not in {"", "unspecified", "unset", "set"}
        }
        if not active_drivers:
            return
        configured = self._git(
            ["config", "--null", "--get-regexp", r"^filter\..*\.(clean|smudge|process)$"],
            cwd=cwd,
            allow_returncodes=(0, 1),
        ).stdout.split("\0")
        executable_drivers: set[str] = set()
        for entry in configured:
            if "\n" not in entry:
                continue
            key, value = entry.split("\n", 1)
            match = re.fullmatch(r"filter\.(.+)\.(?:clean|smudge|process)", key)
            if match and value.strip():
                executable_drivers.add(match.group(1).casefold())
        unsafe = sorted(active_drivers & executable_drivers)
        if unsafe:
            raise WorktreeError(
                "Managed worktrees do not run configured Git filters; "
                "disable the filter or use explicit in-place mode",
                code="checkout_filter_unsupported",
            )

    def _git(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout: float = 15,
        code: str = "git_error",
        input_text: str | None = None,
        allow_returncodes: tuple[int, ...] = (0,),
    ) -> subprocess.CompletedProcess[str]:
        env = {key: value for key, value in os.environ.items() if key.upper() in _ENV_ALLOWLIST}
        env["GIT_TERMINAL_PROMPT"] = "0"
        command = [
            "git",
            "--no-pager",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "diff.external=",
            "-C",
            str(cwd or self.source),
            "--literal-pathspecs",
            *arguments,
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                input=input_text,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
                env=env,
            )
        except FileNotFoundError as error:
            raise WorktreeError("Git executable is unavailable", code="git_unavailable") from error
        except subprocess.TimeoutExpired as error:
            raise WorktreeError("Git operation timed out", code="git_timeout") from error
        if result.returncode not in allow_returncodes:
            raise WorktreeError(f"Git operation failed ({result.returncode})", code=code)
        return result

    @staticmethod
    def _ensure_directory(path: Path) -> None:
        if path.exists() or path.is_symlink():
            if _is_linklike(path) or not path.is_dir():
                raise WorktreeError("Worktree storage path is unsafe", code="unsafe_storage")
            return
        try:
            path.mkdir()
        except FileExistsError:
            if _is_linklike(path) or not path.is_dir():
                raise WorktreeError(
                    "Worktree storage path is unsafe", code="unsafe_storage"
                ) from None

    def _validate_storage(self, *, missing_ok: bool = False) -> bool:
        forge_dir = self.source / ".forge"
        if not forge_dir.exists() and not _is_linklike(forge_dir):
            if missing_ok:
                return False
            raise WorktreeError("Worktree storage path is missing", code="unsafe_storage")
        if _is_linklike(forge_dir) or not forge_dir.is_dir():
            raise WorktreeError("Worktree storage path is unsafe", code="unsafe_storage")
        if not self.storage.exists() and not _is_linklike(self.storage):
            if missing_ok:
                return False
            raise WorktreeError("Worktree storage path is missing", code="unsafe_storage")
        if _is_linklike(self.storage) or not self.storage.is_dir():
            raise WorktreeError("Worktree storage path is unsafe", code="unsafe_storage")
        return True

    def _ensure_storage(self) -> None:
        self._ensure_directory(self.source / ".forge")
        self._ensure_directory(self.storage)


def _is_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _sensitive(relative: str) -> bool:
    path = Path(relative)
    sensitive_names = _SENSITIVE_NAMES | {".git", ".forge"}
    return any(
        part.casefold() in sensitive_names
        or Path(part).suffix.casefold() in _SENSITIVE_SUFFIXES
        or part.casefold().startswith(".env.")
        for part in path.parts
    )


def _is_linklike(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def _untracked_diff(path: Path, relative: str) -> str:
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return ""
        with path.open("rb") as source:
            raw = source.read(_MAX_FILE_BYTES + 1)
        if len(raw) > _MAX_FILE_BYTES or b"\0" in raw[:8192]:
            return ""
        lines = raw.decode("utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError):
        return ""
    return "\n".join(
        difflib.unified_diff(
            (), lines, fromfile="/dev/null", tofile=f"b/{Path(relative).as_posix()}", lineterm=""
        )
    )
