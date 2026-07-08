"""
tools/repo_scanner.py
─────────────────────
Clones a Git repository and enumerates Java source files.
Used by the SRAO agent as the first step in the pipeline.
"""

import os
import subprocess
import tempfile
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def scan_repository(
    repo_url: str,
    branch: str = "main",
    target_dir: Optional[str] = None,
) -> dict:
    """
    Clone a Git repository and return metadata about its Java files.

    Args:
        repo_url:   HTTPS or SSH URL of the Git repository.
                    e.g. "https://github.com/org/my-service.git"
        branch:     Branch to checkout (default: "main").
        target_dir: Local path to clone into. Uses a temp dir if not specified.

    Returns:
        {
          "status":      "success" | "error",
          "repo_path":   str   – local path of cloned repo,
          "java_files":  list  – relative paths of all .java files,
          "total_files": int,
          "message":     str
        }
    """
    clone_dir = target_dir or tempfile.mkdtemp(prefix="srao_repo_")

    try:
        logger.info("Cloning %s (branch=%s) → %s", repo_url, branch, clone_dir)
        result = subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", branch, repo_url, clone_dir],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            return {
                "status": "error",
                "message": f"git clone failed: {result.stderr.strip()}",
            }

        java_files = _find_java_files(clone_dir)
        logger.info("Found %d Java files in %s", len(java_files), clone_dir)

        return {
            "status":      "success",
            "repo_path":   clone_dir,
            "java_files":  java_files,
            "total_files": len(java_files),
            "message":     f"Repository cloned. Found {len(java_files)} Java source files.",
        }

    except subprocess.TimeoutExpired:
        return {"status": "error", "message": "git clone timed out after 120 seconds."}
    except Exception as exc:
        logger.exception("Unexpected error during repo scan")
        return {"status": "error", "message": str(exc)}


def list_java_files(repo_path: str, exclude_tests: bool = False) -> dict:
    """
    List all Java files in an already-cloned repository.

    Args:
        repo_path:     Local path of the cloned repository.
        exclude_tests: If True, skip files under src/test/.

    Returns:
        {
          "status":     "success" | "error",
          "java_files": list of relative file paths,
          "count":      int
        }
    """
    try:
        files = _find_java_files(repo_path, exclude_tests=exclude_tests)
        return {"status": "success", "java_files": files, "count": len(files)}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _find_java_files(root: str, exclude_tests: bool = False) -> list[str]:
    """Walk the directory tree and collect .java file paths (relative to root)."""
    root_path = Path(root)
    java_files = []

    for path in root_path.rglob("*.java"):
        relative = path.relative_to(root_path).as_posix()
        if exclude_tests and ("src/test" in relative or "src/it" in relative):
            continue
        # Skip generated sources
        if "target/generated-sources" in relative or "build/generated" in relative:
            continue
        java_files.append(relative)

    # Sort: main sources first, then test sources, alphabetically within each group
    java_files.sort(key=lambda p: (1 if "src/test" in p else 0, p))
    return java_files
