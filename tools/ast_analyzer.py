"""
tools/ast_analyzer.py
──────────────────────
Analyses a Java source file for legacy patterns that can be modernised.
Provides complete file context to the generation layer to prevent fragmentation bugs.
"""

import re
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

# ── Pattern registry ──────────────────────────────────────────────────────────
PATTERNS = [
    (
        "RAW_THREAD",
        "Raw Thread/Runnable usage — replace with Virtual Threads (Project Loom)",
        "HIGH",
        r"\bnew\s+Thread\s*\(|implements\s+Runnable\b",
        "Java 21",
    ),
    (
        "SYNCHRONIZED_BLOCK",
        "synchronized block/method — consider java.util.concurrent alternatives",
        "HIGH",
        r"\bsynchronized\s*[\(\{]",
        "Java 21",
    ),
    (
        "BLOCKING_SLEEP",
        "Thread.sleep() — indicates blocking wait, use reactive/async pattern",
        "HIGH",
        r"Thread\.sleep\s*\(",
        "Java 8+",
    ),
    (
        "COMPLETABLE_FUTURE_MISSING",
        "Blocking I/O in service layer — wrap with CompletableFuture",
        "HIGH",
        r"\.(get|getInputStream|readLine)\s*\(",
        "Java 8+",
    ),
    (
        "POJO_CLASS",
        "Mutable POJO with getters/setters — candidate for Record class",
        "MEDIUM",
        r"private\s+\w+\s+\w+\s*;\s*\n.*public\s+\w+\s+get\w+\s*\(\s*\)",
        "Java 16+",
    ),
    (
        "NULL_CHECK",
        "Explicit null check — replace with Optional<T>",
        "MEDIUM",
        r"if\s*\(\s*\w+\s*==\s*null\s*\)|if\s*\(\s*null\s*==\s*\w+\s*\)",
        "Java 8+",
    ),
    (
        "RAW_TYPE",
        "Raw type usage (no generics) — add proper type parameters",
        "MEDIUM",
        r"\b(List|Map|Set|Collection|ArrayList|HashMap|HashSet)\s+\w+\s*=\s*new\s+\1\s*\(\)",
        "Java 8+",
    ),
    (
        "INSTANCEOF_CAST",
        "instanceof + explicit cast — use pattern matching instanceof",
        "MEDIUM",
        r"instanceof\s+\w+\s*\)\s*\{\s*\n\s*\w+\s+\w+\s*=\s*\(\w+\)",
        "Java 16+",
    ),
    (
        "FOR_LOOP",
        "Traditional for-loop — replace with Stream API",
        "LOW",
        r"\bfor\s*\(\s*(int\s+\w+\s*=\s*0|\w+\s+\w+\s*:\s*\w+)",
        "Java 8+",
    ),
    (
        "STRING_CONCAT",
        "String concatenation in loop — use StringBuilder or String.join()",
        "LOW",
        r'(\w+)\s*\+=\s*"',
        "Java 8+",
    ),
    (
        "STRING_BUFFER",
        "StringBuffer — replace with StringBuilder (non-thread-safe context)",
        "LOW",
        r"\bnew\s+StringBuffer\s*\(",
        "Java 8+",
    ),
    (
        "MULTILINE_STRING",
        "Multi-line String concatenation — use Text Blocks",
        "LOW",
        r'"[^"]*\\n[^"]*"\s*\+\s*"',
        "Java 15+",
    ),
]

SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


def analyze_java_file(file_path: str) -> dict:
    """
    Analyse a single Java source file for legacy patterns.
    Sends full file scope to prevent LLM fragmentation failures.
    """
    try:
        source = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return {"status": "error", "message": f"File not found: {file_path}"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}

    lines = source.splitlines()
    findings = []

    for pattern_id, description, severity, regex, target_java in PATTERNS:
        try:
            matches = list(re.finditer(regex, source, re.MULTILINE))
        except re.error:
            continue

        if not matches:
            continue

        line_numbers = []
        for m in matches:
            line_no = source[: m.start()].count("\n") + 1
            line_numbers.append(line_no)

        # SRAO: Maintain strict structural context by making 'snippet' the complete file text
        # This gives Gemini visibility over variable maps, braces, and dependencies
        findings.append(
            {
                "pattern_id":   pattern_id,
                "description":  description,
                "severity":     severity,
                "line_numbers":  line_numbers,
                "target_java":   target_java,
                "snippet":       source,  # Full file context payload passed downstream
            }
        )

    # Sort findings by target severity prioritizations
    findings.sort(key=lambda f: SEVERITY_ORDER.get(f["severity"], 9))

    summary = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for f in findings:
        summary[f["severity"]] = summary.get(f["severity"], 0) + 1

    return {
        "status":           "success",
        "file":             file_path,
        "findings":         findings,
        "total_findings":   len(findings),
        "severity_summary": summary,
    }


def classify_severity(findings: list) -> dict:
    """Summarise and prioritise findings across multiple files."""
    file_severity: dict[str, set] = {}

    for finding in findings:
        fname = finding.get("file", "unknown")
        sev   = finding.get("severity", "LOW")
        file_severity.setdefault(fname, set()).add(sev)

    high   = [f for f, sevs in file_severity.items() if "HIGH"   in sevs]
    medium = [f for f, sevs in file_severity.items() if "MEDIUM" in sevs and "HIGH" not in sevs]
    low    = [f for f, sevs in file_severity.items() if sevs == {"LOW"}]

    return {
        "high_priority_files":   sorted(high),
        "medium_priority_files": sorted(medium),
        "low_priority_files":    sorted(low),
        "total_patterns":        len(findings),
        "recommended_order":     sorted(high) + sorted(medium) + sorted(low),
    }
