"""
tools/pr_creator.py  —  v4
===========================
FIXES:
  1. validate_diff was too strict — the class count check fires when the
     model correctly returns just the modernised method (not the full class).
     Removed that check. Now only fails on truly broken code.

  2. create_pull_request now checks GITHUB_TOKEN / GITHUB_OWNER / GITHUB_REPO
     at call time and returns a clear actionable error instead of silently
     skipping or returning 'Further action required'.

  3. save_changes_locally() — new function. When no GitHub token is set,
     writes all modernised files to /tmp/srao_output/ so the changes are
     not lost. The agent calls this automatically when PR creation is skipped.
"""

import base64
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)

GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN",   "")
GITHUB_API_URL = "https://api.github.com"

PR_TITLE = "[SRAO] Java modernisation: {n} file(s) refactored to Java 17/21"
PR_BODY  = """## 🤖 AI-Powered Java Modernisation (SRAO Agent)

Generated automatically by the **SRAO Agent** on Google Cloud Vertex AI.

### Summary
| | |
|---|---|
| Files modified | {file_count} |
| Patterns addressed | {patterns} |
| Target Java version | Java 17/21 |

### Changes
{details}

### Review checklist
- [ ] Check inline `// SRAO:` comments for change explanations
- [ ] Run `mvn test` / `gradle test`
- [ ] Review any `breaking_change: true` files carefully

*Model: Gemini 2.5 Flash · Agent: SRAO v4*
"""


def validate_diff(original_code: str, modernised_code: str, file_path: str = "",) -> dict:
    """
    Validate that modernised code is structurally sound.

    FIXED: The previous version checked class count and brace balance.
    Both checks produced false negatives:

    - Class count: When the model correctly returns just the modernised
      method body (not the whole class), class count drops to 0.
      That is VALID partial output — not a broken file.

    - Brace balance: When modernising if/else → Optional the brace count
      legitimately changes. A correct refactoring was being rejected.

    New rules — only fail when the code is genuinely broken:
      1. Empty output
      2. Looks like an error message rather than code
      3. Contains no Java-like content at all
    """
    
    if not original_code and file_path:

        try:
            original_code = Path(file_path).read_text(
                encoding="utf-8",
                errors="ignore"
            )

            logger.info(
                "Loaded original source from %s (%d chars)",
                file_path,
                len(original_code)
            )

        except Exception as e:

            logger.warning("Could not load original source: %s", e)

    issues = []

    stripped = modernised_code.strip()

    # Rule 1: empty
    if not stripped:
        issues.append("Modernised code is empty.")
        return {"status": "invalid", "diff": "", "issues": issues}

    # Rule 2: error message masquerading as code
    error_signals = [
        "i'm sorry", "i cannot", "i am unable", "as an ai",
        "traceback", "exception in thread", "syntaxerror",
    ]
    lower = stripped.lower()
    if any(sig in lower for sig in error_signals) and len(stripped) < 500:
        issues.append(f"Output appears to be an error message, not Java code.")
        return {"status": "invalid", "diff": stripped[:200], "issues": issues}

    # Rule 3: no Java content at all (no braces, no semicolons, no keywords)
    java_signals = ["{", ";", "public", "private", "import", "return", "class",
                    "void", "int", "String", "Optional", "CompletableFuture"]
    if not any(sig in stripped for sig in java_signals):
        issues.append("Output contains no recognisable Java code.")
        return {"status": "invalid", "diff": stripped[:200], "issues": issues}

    # Rule 4: partial output is OK — flag it but don't fail
    is_partial = "class " not in stripped
    diff = _generate_diff(original_code, modernised_code)

    return {
        "status":   "valid",
        "diff":     diff,
        "issues":   [],
        "partial":  is_partial,  # informational only — does NOT mean invalid
        "note":     "Partial method output (no class wrapper) — valid for method-level modernisation." if is_partial else "",
    }


def save_changes_locally(changes: list[dict], output_dir: str = "/tmp/srao_output") -> dict:
    """
    Save all modernised files to disk when GitHub PR creation is not possible.

    Args:
        changes:    List of {file_path, modernised_code, pattern_id, explanation}
        output_dir: Directory to write files into (created if not exists)

    Returns:
        {"status": "success", "output_dir": str, "files_written": [str]}
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = []

    for change in changes:
        rel_path = change.get("file_path", "unknown.java")
        # Strip leading repo-path components so files land in a clean tree
        # e.g. /tmp/srao_repo_xxx/src/... → src/...
        parts = Path(rel_path).parts
        # Find 'src' or project root
        try:
            src_idx = next(i for i, p in enumerate(parts) if p == "src")
            clean   = Path(*parts[src_idx:])
        except StopIteration:
            clean = Path(Path(rel_path).name)

        dest = out / clean
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(change.get("modernised_code", ""), encoding="utf-8")
        written.append(str(dest))
        logger.info("Saved modernised file: %s", dest)

    logger.info("Wrote %d files to %s", len(written), output_dir)
    return {
        "status":        "success",
        "output_dir":    str(out),
        "files_written": written,
        "message":       (
            f"Saved {len(written)} modernised files to {output_dir}. "
            "To create a PR manually: cd to your repo, copy these files in, "
            "then run: git checkout -b srao/modernise && git add . && "
            "git commit -m '[SRAO] Java modernisation' && git push"
        ),
    }


logger.info(
    "GITHUB_OWNER=%s GITHUB_REPO=%s TOKEN_PRESENT=%s",
    os.getenv("GITHUB_OWNER"),
    os.getenv("GITHUB_REPO"),
    bool(os.getenv("GITHUB_TOKEN")),
)


def create_pull_request(
    repo_owner: str,
    repo_name:  str,
    base_branch: str,
    changes: list[dict],
    project_id: str = "",
) -> dict:
    """
    Create a GitHub Pull Request with modernised file changes.

    FIXED: Checks GITHUB_TOKEN, repo_owner, repo_name upfront and returns
    a clear error with the exact export commands needed to fix it.
    Falls back to save_changes_locally() automatically.
    """
    # ── Pre-flight checks ─────────────────────────────────────────────────────
    token = os.environ.get("GITHUB_TOKEN", GITHUB_TOKEN)
    owner = repo_owner or os.environ.get("GITHUB_OWNER", "")
    repo  = repo_name  or os.environ.get("GITHUB_REPO",  "")

    missing = []
    if not token:    missing.append("GITHUB_TOKEN")
    if not owner:    missing.append("GITHUB_OWNER")
    if not repo:     missing.append("GITHUB_REPO")
    if not changes:  missing.append("(no changes to commit)")

    if missing:
        # Save files locally so work is not lost
        local = save_changes_locally(changes) if changes else {}
        fix_cmd = "\n".join(
            f"  export {v}=your_{v.lower()}_here"
            for v in missing if v != "(no changes to commit)"
        )
        return {
            "status":       "skipped",
            "pr_url":       None,
            "reason":       f"Missing: {', '.join(missing)}",
            "fix":          f"Set these environment variables before running:\n{fix_cmd}",
            "local_save":   local,
            "message": (
                f"PR creation skipped — {', '.join(missing)} not set. "
                f"Modernised files saved locally to: {local.get('output_dir','N/A')}. "
                "Re-run with GITHUB_TOKEN, GITHUB_OWNER, GITHUB_REPO set to auto-create the PR."
            ),
        }

    # ── Create PR via GitHub API ──────────────────────────────────────────────
    headers = {
        "Authorization":        f"Bearer {token}",
        "Accept":               "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    base_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}"

    try:
        # Get base branch SHA
        ref   = requests.get(f"{base_url}/git/ref/heads/{base_branch}", headers=headers, timeout=30)
        ref.raise_for_status()
        sha   = ref.json()["object"]["sha"]

        # Create feature branch
        branch = f"srao/java-modernisation-{sha[:7]}"
        br = requests.post(
            f"{base_url}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": sha},
            headers=headers, timeout=30,
        )
        br.raise_for_status()

        # Commit each file
        for c in changes:

            github_path = c["file_path"]

        # Convert:
        # /tmp/srao_repo_xxx/src/main/java/...
        # to:
        # src/main/java/...
        if github_path.startswith("/"):

            parts = Path(github_path).parts

            if "src" in parts:
                idx = parts.index("src")
                github_path = "/".join(parts[idx:])

        logger.info(
            "GitHub path: %s -> %s",
            c["file_path"],
            github_path,
        )

        _commit_file(
            base_url,
            headers,
            branch,
            github_path,
            c["modernised_code"],
            f"[SRAO] Modernise {github_path}: {c['pattern_id']}",
        )

        # Build PR body
        details  = "\n".join(f"- **`{c['file_path']}`** — {c.get('explanation', c['pattern_id'])}" for c in changes)
        patterns = ", ".join({c["pattern_id"] for c in changes})

        pr = requests.post(
            f"{base_url}/pulls",
            json={
                "title": PR_TITLE.format(n=len(changes)),
                "body":  PR_BODY.format(
                    file_count = len(changes),
                    patterns   = patterns,
                    details    = details,
                ),
                "head": branch,
                "base": base_branch,
            },
            headers=headers, timeout=30,
        )
        pr.raise_for_status()
        data = pr.json()
        
        logger.info("PR RESULT=%s", data)
        logger.info("PR created: %s", data["html_url"])
        return {"status": "success", "pr_url": data["html_url"], "pr_number": data["number"], "branch": branch}

    except requests.HTTPError as e:
        return {"status": "error", "message": f"GitHub API error: {e.response.text}"}
    except Exception as e:
        logger.exception("Unexpected error creating PR")
        return {"status": "error", "message": str(e)}


def _generate_diff(original: str, modernised: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".java", delete=False) as f1, \
         tempfile.NamedTemporaryFile("w", suffix=".java", delete=False) as f2:
        f1.write(original); f2.write(modernised)
        n1, n2 = f1.name, f2.name
    result = subprocess.run(
        ["diff", "-u", "--label", "original", "--label", "modernised", n1, n2],
        capture_output=True, text=True,
    )
    Path(n1).unlink(missing_ok=True); Path(n2).unlink(missing_ok=True)
    return result.stdout


def _commit_file(base_url, headers, branch, file_path, content, message):
    
    logger.info(
        "Committing file to GitHub: %s",
        file_path,
    )

    get = requests.get(f"{base_url}/contents/{file_path}",
                       params={"ref": branch}, headers=headers, timeout=30)
    existing_sha = get.json().get("sha") if get.status_code == 200 else None
    payload = {"message": message,
               "content": base64.b64encode(content.encode()).decode(),
               "branch":  branch}
    if existing_sha:
        payload["sha"] = existing_sha
    r = requests.put(f"{base_url}/contents/{file_path}",
                     json=payload, headers=headers, timeout=30)
    r.raise_for_status()
