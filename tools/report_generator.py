"""
tools/report_generator.py
──────────────────────────
Generates a structured modernisation report in JSON and Markdown.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def generate_report(
    repo_url: str,
    findings: list[dict],
    changes: list[dict],
    pr_url: Optional[str] = None,
    output_dir: str = "/tmp",
) -> dict:
    """
    Generate a JSON + Markdown modernisation report.

    Args:
        repo_url:   The repository that was analysed.
        findings:   List of analysis findings from ast_analyzer.
        changes:    List of applied code changes.
        pr_url:     URL of the created Pull Request (if any).
        output_dir: Directory to write report files.

    Returns:
        {
          "status":          "success" | "error",
          "json_report":     str  – path to JSON report,
          "markdown_report": str  – path to Markdown report,
          "summary":         dict
        }
    """
    ts = datetime.now(timezone.utc).isoformat()

    # Aggregate stats
    severity_count = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    pattern_count: dict[str, int] = {}
    for f in findings:
        sev = f.get("severity", "LOW")
        severity_count[sev] = severity_count.get(sev, 0) + 1
        pid = f.get("pattern_id", "UNKNOWN")
        pattern_count[pid] = pattern_count.get(pid, 0) + 1

    summary = {
        "repo_url":          repo_url,
        "timestamp":         ts,
        "total_findings":    len(findings),
        "severity_breakdown": severity_count,
        "top_patterns":      sorted(pattern_count.items(), key=lambda x: -x[1])[:5],
        "files_modernised":  len(changes),
        "pr_url":            pr_url,
    }

    # Write JSON report
    report_data = {"summary": summary, "findings": findings, "changes": changes}
    json_path   = Path(output_dir) / "srao_report.json"
    json_path.write_text(json.dumps(report_data, indent=2), encoding="utf-8")

    # Write Markdown report
    md_path = Path(output_dir) / "srao_report.md"
    md_path.write_text(_build_markdown(summary, findings, changes), encoding="utf-8")

    logger.info("Reports written: %s, %s", json_path, md_path)
    return {
        "status":          "success",
        "json_report":     str(json_path),
        "markdown_report": str(md_path),
        "summary":         summary,
    }


def _build_markdown(summary: dict, findings: list, changes: list) -> str:
    lines = [
        "# SRAO Modernisation Report",
        f"\n**Repository:** {summary['repo_url']}",
        f"**Generated:**  {summary['timestamp']}",
        f"**Pull Request:** {summary.get('pr_url', 'N/A')}",
        "\n---\n",
        "## Summary",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total findings | {summary['total_findings']} |",
        f"| HIGH severity  | {summary['severity_breakdown'].get('HIGH', 0)} |",
        f"| MEDIUM severity| {summary['severity_breakdown'].get('MEDIUM', 0)} |",
        f"| LOW severity   | {summary['severity_breakdown'].get('LOW', 0)} |",
        f"| Files modernised | {summary['files_modernised']} |",
        "\n## Top Patterns Found",
    ]
    for pattern, count in summary.get("top_patterns", []):
        lines.append(f"- **{pattern}**: {count} occurrence(s)")

    lines += ["\n## Detailed Findings"]
    for f in findings:
        lines.append(f"\n### `{f.get('file', 'unknown')}`")
        lines.append(f"- **Pattern:** `{f.get('pattern_id')}`  [{f.get('severity')}]")
        lines.append(f"- **Description:** {f.get('description')}")
        lines.append(f"- **Target Java:** {f.get('target_java')}")
        lines.append(f"- **Lines:** {f.get('line_numbers', [])}")

    lines += ["\n## Applied Changes"]
    for c in changes:
        lines.append(f"\n### `{c.get('file_path', 'unknown')}`")
        lines.append(f"- **Pattern:** `{c.get('pattern_id')}`")
        lines.append(f"- **Explanation:** {c.get('explanation', '')}")

    return "\n".join(lines)
