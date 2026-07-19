"""
tools/repo_scanner.py
─────────────────────
Clones a Git repository, enumerates Java source files, applies modernized patches,
and executes localized compilation checks for code validation.
"""

import os
import subprocess
import tempfile
import logging
import re
from typing import Optional, Tuple, List
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def scan_repository(
    repo_url: str,
    branch: str = "main",
    target_dir: Optional[str] = None,
) -> dict:
    """
    Clone a Git repository or use a local directory path, returning metadata about its Java files.

    Args:
        repo_url:   HTTPS, SSH URL, or a local directory path of the repository.
                    e.g. "https://github.com/org/my-service.git" or "/workspace/my-app"
        branch:     Branch to checkout (default: "main"). Ignored if a local path is provided.
        target_dir: Local path to clone into. Uses a temp dir if not specified.

    Returns:
        {
          "status":      "success" | "error",
          "repo_path":   str   – local path of cloned or verified repo,
          "java_files":  list  – relative paths of all .java files,
          "total_files": int,
          "message":     str
        }
    """
    # SRAO: Handle scenarios where a developer passes an absolute local file pathway instead of a remote git address
    if os.path.exists(repo_url) and os.path.isdir(repo_url):
        logger.info("Using existing local folder directory pathway path: %s", repo_url)
        java_files = _find_java_files(repo_url)
        return {
            "status":      "success",
            "repo_path":   str(Path(repo_url).resolve()),
            "java_files":  java_files,
            "total_files": len(java_files),
            "message":     f"Local repository identified. Found {len(java_files)} Java source files.",
        }

    clone_dir = target_dir or tempfile.mkdtemp(prefix="srao_repo_")

    try:
        logger.info("Cloning %s (branch=%s) → %s", repo_url, branch, clone_dir)
        result = subprocess.run(
            ["git", "clone", "--branch", branch, repo_url, clone_dir],
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


# ── Pipeline Synchronization Hooks ───────────────────────────────────────────

def apply_patch(repo_root: str, relative_file_path: str, modernized_code: str) -> bool:
    """Writes the modernized code content string directly to the target file path location."""
    try:
        full_path = Path(repo_root) / relative_file_path
        
        # SRAO: Create backup of the file to allow instant rollback capabilities if validation steps fail
        backup_path = full_path.with_suffix(".java.bak")
        if not backup_path.exists():
            full_path.rename(backup_path)
            
        full_path.write_text(modernized_code, encoding="utf-8")
        logger.info("Applied modernization patch update sequence to: %s", relative_file_path)
        return True
    except Exception as e:
        logger.error("Failed to write updated patch file modifications to system disk: %s", e)
        return False


def revert_file_changes(repo_root: str, relative_file_path: str) -> None:
    """Restores the backup file state if validation compiler checks break down."""
    full_path = Path(repo_root) / relative_file_path
    backup_path = full_path.with_suffix(".java.bak")
    
    if backup_path.exists():
        if full_path.exists():
            full_path.unlink()
        backup_path.rename(full_path)
        logger.info("Reverted workspace file alterations back to baseline configuration for: %s", relative_file_path)


def clean_backup_files(repo_root: str, relative_file_path: str) -> None:
    """Purges the backup snapshot item when code compilation checks clear successfully."""
    full_path = Path(repo_root) / relative_file_path
    backup_path = full_path.with_suffix(".java.bak")
    if backup_path.exists():
        backup_path.unlink()

def prepare_maven_target_version(
    repo_root: str,
    target_version: int
) -> tuple[bool, str, Optional[dict]]:
    """
    Updates pom.xml once so the project compiles against the
    Java version selected by the user.
    """

    pom_path = Path(repo_root) / "pom.xml"

    if not pom_path.exists():
        return True, "No pom.xml found. Skipping Maven version update.", None

    try:
        original_content = pom_path.read_text(
            encoding="utf-8",
            errors="replace"
        )

        updated_content = original_content
        version = str(target_version)

        def update_property(
            pom_content: str,
            property_name: str,
            property_value: str
        ) -> str:
            pattern = (
                rf"<{re.escape(property_name)}>"
                rf"\s*[^<]*\s*"
                rf"</{re.escape(property_name)}>"
            )

            replacement = (
                f"<{property_name}>"
                f"{property_value}"
                f"</{property_name}>"
            )

            if re.search(pattern, pom_content):
                return re.sub(
                    pattern,
                    replacement,
                    pom_content
                )

            if "</properties>" in pom_content:
                return pom_content.replace(
                    "</properties>",
                    (
                        f"        <{property_name}>"
                        f"{property_value}"
                        f"</{property_name}>\n"
                        "    </properties>"
                    ),
                    1
                )

            properties_block = (
                "\n    <properties>\n"
                f"        <{property_name}>"
                f"{property_value}"
                f"</{property_name}>\n"
                "    </properties>\n"
            )

            if "<dependencies>" in pom_content:
                return pom_content.replace(
                    "<dependencies>",
                    properties_block + "\n    <dependencies>",
                    1
                )

            return pom_content.replace(
                "</project>",
                properties_block + "\n</project>",
                1
            )

        # Update or add standard Maven Java properties.
        updated_content = update_property(
            updated_content,
            "java.version",
            version
        )

        updated_content = update_property(
            updated_content,
            "maven.compiler.release",
            version
        )

        updated_content = update_property(
            updated_content,
            "maven.compiler.source",
            version
        )

        updated_content = update_property(
            updated_content,
            "maven.compiler.target",
            version
        )

        # Update compiler plugin configuration when explicitly present.
        updated_content = re.sub(
            r"<source>\s*(?:1\.)?\d+\s*</source>",
            f"<source>{version}</source>",
            updated_content
        )

        updated_content = re.sub(
            r"<target>\s*(?:1\.)?\d+\s*</target>",
            f"<target>{version}</target>",
            updated_content
        )

        updated_content = re.sub(
            r"<release>\s*(?:1\.)?\d+\s*</release>",
            f"<release>{version}</release>",
            updated_content
        )

        if updated_content == original_content:
            return (
                True,
                f"pom.xml already targets Java {target_version}.",
                None
            )

        pom_path.write_text(
            updated_content,
            encoding="utf-8"
        )

        logger.info(
            "Updated pom.xml to target Java %s",
            target_version
        )

        pom_change = {
            "file_path": "pom.xml",
            "file": "pom.xml",
            "modernised_code": updated_content,
            "target_version": target_version,
            "pattern_ids": ["BUILD_JAVA_VERSION"],
            "severity_summary": {
                "HIGH": 0,
                "MEDIUM": 1,
                "LOW": 0
            },
            "explanation": (
                "Updated Maven compiler configuration to "
                f"Java {target_version}."
            )
        }

        return (
            True,
            f"pom.xml updated to Java {target_version}.",
            pom_change
        )

    except Exception as exc:
        logger.exception("Failed to update pom.xml.")

        return (
            False,
            f"Failed to update pom.xml: {exc}",
            None
        )


def run_target_baseline_compile(
    repo_root: str
) -> tuple[bool, str]:
    """
    Runs one clean compilation after pom.xml is updated and before
    any Java source modernization starts.
    """

    pom_path = Path(repo_root) / "pom.xml"

    if not pom_path.exists():
        return True, "No pom.xml found."

    try:
        logger.info(
            "Running baseline Maven compilation using updated pom.xml."
        )

        result = subprocess.run(
            [
                "mvn",
                "clean",
                "compile",
                "-DskipTests=true"
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=180
        )

        output = (
            result.stdout
            + "\n"
            + result.stderr
        )

        if result.returncode == 0:
            logger.info(
                "Baseline Maven compilation completed successfully."
            )
            return True, output

        logger.error(
            "Baseline Maven compilation failed."
        )

        return False, output

    except subprocess.TimeoutExpired:
        return (
            False,
            "Baseline Maven compilation timed out after 180 seconds."
        )

    except Exception as exc:
        return (
            False,
            f"Baseline Maven compilation failed to execute: {exc}"
        )

def run_compile_validation(
    repo_root: str,
    relative_file_path: str
) -> tuple[bool, str]:
    """
    Runs Maven compilation using the target version already configured
    in the cloned project's pom.xml.
    """

    root_path = Path(repo_root)

    if not (root_path / "pom.xml").exists():
        return True, ""

    try:
        logger.info(
            "Running Maven compile validation after updating %s",
            relative_file_path
        )

        result = subprocess.run(
            [
                "mvn",
                "compile",
                "-DskipTests=true",
                "-Dmaven.compiler.useIncrementalCompilation=true"
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=120
        )

        output = (
            (result.stdout or "")
            + "\n"
            + (result.stderr or "")
        )

        if result.returncode == 0:
            return True, ""

        return False, output

    except subprocess.TimeoutExpired:
        return False, "Maven compilation timed out after 120 seconds."

    except Exception as exc:
        return False, f"Maven compilation failed to execute: {exc}"

def run_test_compile_validation(
    repo_root: str
) -> tuple[bool, str]:
    """
    Compiles main and test source code without executing tests.
    """

    root_path = Path(repo_root)

    if not (root_path / "pom.xml").exists():
        return True, ""

    try:
        logger.info("Running Maven test compilation validation.")

        result = subprocess.run(
            [
                "mvn",
                "test-compile",
                "-DskipTests=true"
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=180
        )

        output = (
            (result.stdout or "")
            + "\n"
            + (result.stderr or "")
        )

        if result.returncode == 0:
            return True, output

        return False, output

    except subprocess.TimeoutExpired:
        return False, "Maven test compilation timed out after 180 seconds."

    except Exception as exc:
        return False, f"Maven test compilation failed to execute: {exc}"

def extract_failing_test_files(
    repo_root: str,
    compile_log: str
) -> list[str]:
    """
    Extracts failing Java test file paths from Maven compiler output.
    """

    repo_path = Path(repo_root).resolve()
    failing_files = set()

    pattern = re.compile(
        r"\[ERROR\]\s+(.+?\.java):\[\d+,\d+\]"
    )

    for match in pattern.finditer(compile_log):
        raw_path = Path(match.group(1).strip())

        try:
            relative_path = raw_path.resolve().relative_to(repo_path)
            normalized = relative_path.as_posix()
        except (ValueError, OSError):
            normalized = raw_path.as_posix()

        if (
            normalized.startswith("src/test/")
            and normalized.endswith(".java")
        ):
            failing_files.add(normalized)

    return sorted(failing_files)

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

def run_static_analysis_validation(
    repo_root: str
) -> tuple[bool, str, str]:
    """
    Runs Checkstyle or PMD before PR creation.

    Priority:
        1. Checkstyle, when configured in pom.xml
        2. PMD, when configured in pom.xml
        3. Skip cleanly when neither plugin is configured

    Returns:
        success: True when analysis passes or is not configured
        output: Maven output or skip message
        tool_name: CHECKSTYLE, PMD, or SKIPPED
    """

    root_path = Path(repo_root)
    pom_path = root_path / "pom.xml"

    if not pom_path.exists():
        return (
            True,
            "No pom.xml found. Static analysis skipped.",
            "SKIPPED"
        )

    try:
        pom_content = pom_path.read_text(
            encoding="utf-8",
            errors="replace"
        ).lower()
    except Exception as exc:
        return (
            False,
            f"Unable to read pom.xml for static analysis: {exc}",
            "UNKNOWN"
        )

    # ------------------------------------------------------------
    # Checkstyle
    # ------------------------------------------------------------

    if (
        "maven-checkstyle-plugin" in pom_content
        or "checkstyle" in pom_content
    ):
        logger.info(
            "🔍 Static Analysis Gate: Running Maven Checkstyle."
        )

        try:
            result = subprocess.run(
                [
                    "mvn",
                    "checkstyle:check",
                    "-DskipTests=true"
                ],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=180
            )

            output = (
                (result.stdout or "")
                + "\n"
                + (result.stderr or "")
            )

            if result.returncode == 0:
                logger.info(
                    "✅ Checkstyle validation passed."
                )
                return True, output, "CHECKSTYLE"

            logger.error(
                "❌ Checkstyle validation failed."
            )
            return False, output, "CHECKSTYLE"

        except subprocess.TimeoutExpired:
            return (
                False,
                "Checkstyle execution timed out after 180 seconds.",
                "CHECKSTYLE"
            )

        except Exception as exc:
            return (
                False,
                f"Checkstyle execution failed: {exc}",
                "CHECKSTYLE"
            )

    # ------------------------------------------------------------
    # PMD
    # ------------------------------------------------------------

    if (
        "maven-pmd-plugin" in pom_content
        or "<artifactid>pmd" in pom_content
    ):
        logger.info(
            "🔍 Static Analysis Gate: Running Maven PMD."
        )

        try:
            result = subprocess.run(
                [
                    "mvn",
                    "pmd:check",
                    "-DskipTests=true"
                ],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=180
            )

            output = (
                (result.stdout or "")
                + "\n"
                + (result.stderr or "")
            )

            if result.returncode == 0:
                logger.info(
                    "✅ PMD validation passed."
                )
                return True, output, "PMD"

            logger.error(
                "❌ PMD validation failed."
            )
            return False, output, "PMD"

        except subprocess.TimeoutExpired:
            return (
                False,
                "PMD execution timed out after 180 seconds.",
                "PMD"
            )

        except Exception as exc:
            return (
                False,
                f"PMD execution failed: {exc}",
                "PMD"
            )

    logger.info(
        "ℹ️ No Checkstyle or PMD plugin is configured. "
        "Static analysis gate skipped."
    )

    return (
        True,
        "No Checkstyle or PMD plugin configured.",
        "SKIPPED"
    )
