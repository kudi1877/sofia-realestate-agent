"""Small git helpers for dashboard artifact commits."""

from pathlib import Path
from typing import List
import subprocess

from loguru import logger


def changed_files(repo: Path) -> List[str]:
    """Return dirty paths in repo, relative to repo root."""
    result = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [line[3:] for line in result.stdout.splitlines() if line.strip()]


def commit_and_push(repo: Path, *, files: List[str], message: str) -> bool:
    """Stage given files, commit, and push. No-op if no relevant diff."""
    try:
        diff = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain", *files],
            capture_output=True,
            text=True,
            check=False,
        )
        if not diff.stdout.strip():
            return True

        subprocess.run(
            ["git", "-C", str(repo), "add", *files],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", message],
            check=True,
            capture_output=True,
            text=True,
        )
        push = subprocess.run(
            ["git", "-C", str(repo), "push", "origin", "main"],
            capture_output=True,
            text=True,
            check=False,
        )
        if push.returncode != 0:
            logger.error(f"Push failed for {files}: {push.stderr.strip()}")
            return False
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"git op failed: {e.stderr.strip() if e.stderr else e}")
        return False
