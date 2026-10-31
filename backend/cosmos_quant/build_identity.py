from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class RuntimeBuildIdentity(BaseModel):
    """Immutable process-level provenance for research artifacts and certificates."""

    model_config = ConfigDict(frozen=True)

    git_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_tree_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    working_tree_dirty: bool
    captured_at: datetime

    @property
    def formal_certificate_eligible(self) -> bool:
        return not self.working_tree_dirty


def _git(workspace: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=10,
    )
    return completed.stdout


def _source_tree_hash(workspace: Path, tracked_files: list[str]) -> str:
    digest = sha256()
    for relative in sorted(tracked_files):
        path = workspace / relative
        if not path.is_file():
            continue
        normalized = relative.replace("\\", "/")
        digest.update(normalized.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def capture_runtime_build_identity(workspace: Path | None = None) -> RuntimeBuildIdentity:
    """Capture Git and source identity once at process startup.

    A deployment may inject ``COSMOS_GIT_COMMIT_SHA``. The working tree is still
    inspected when a repository is present so local modifications cannot obtain a
    formal long-lived certificate.
    """

    root = (workspace or Path(__file__).resolve().parents[2]).resolve()
    injected = os.getenv("COSMOS_GIT_COMMIT_SHA", "").strip().lower()
    try:
        repository_sha = _git(root, "rev-parse", "HEAD").strip().lower()
        tracked = _git(root, "ls-files", "-z").split("\0")
        dirty = bool(_git(root, "status", "--porcelain", "--untracked-files=all").strip())
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise RuntimeError(
            "COSMOS build identity: Git repository inspection failed (UTF-8, executable or timeout)"
        ) from exc
    commit_sha = injected or repository_sha
    if len(commit_sha) != 40 or any(
        character not in "0123456789abcdef" for character in commit_sha
    ):
        raise RuntimeError("COSMOS_GIT_COMMIT_SHA must be a full 40-character hexadecimal SHA")
    return RuntimeBuildIdentity(
        git_commit_sha=commit_sha,
        source_tree_hash=_source_tree_hash(root, [item for item in tracked if item]),
        working_tree_dirty=dirty,
        captured_at=datetime.now(UTC),
    )
