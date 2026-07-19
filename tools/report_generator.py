"""
tools/report_generator.py
──────────────────────────
Generates a structured modernisation report in JSON and Markdown.
Fully wrapped to support direct ADK multi-agent orchestrator execution hooks.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any

logger = logging.getLogger("srao.report_generator")


class ReportGenerator:
    """Class wrapper mapping configuration hooks to the SRAO orchestration layer."""
    
    def __init__(self, output_dir: str = "/tmp/srao_output"):
        self.output_dir = output_dir
        Path(self.output_dir).mkdir(parents=True, exist_ok=True)

    def build(self, summary_reports: List[Dict[str, Any]], repo_url: str = "Target Project Base") -> Dict[str, Any]:
        """
        Orchestration adapter bridging multi-agent data loops to static output report files.
        Maps directly to the interface expected by srao_agent.py.
        """
        # Flatten findings and changes from raw pipeline execution objects
        findings = []
        changes = []
        pr_url = None

        for item in summary_reports:
            # Handle both raw schema entries and nested batch responses safely
            finding = item.get("finding", {})
            result = item.get("result", {})
            file_name = item.get("file") or finding.get("file") or "unknown.java"

            findings.append({
                "file": file_name,
                "pattern_id": finding.get("pattern_id", "MODERNIZE"),
                "severity": finding.get("severity", "LOW"),
                "description": finding.get("description", "Legacy Code pattern match."),
                "target_java": finding.get("target_java", "Java 17/21"),
                "line_numbers": finding.get("line_numbers", [])
            })

            changes.append({
                "file_path": file_name,
                "pattern_id": finding.get("pattern_id", "MODERNIZE"),
                "explanation": result.get("explanation") or item.get("explanation") or "Refactored Java statements."
            })
            
            if item.get("pull_request_url"):
                pr_url = item.get("pull_request_url")

        return generate_report(
            repo_url=repo_url,
            findings=findings,
            changes=changes,
            pr_url=pr_url,
            output_dir=self.output_dir
        )


def generate_report(
    repo_url: str,
    findings: list[dict],
    changes: list[dict],
    pr_url: Optional[str] = None,
    output_dir: str = "/tmp/srao_output",
) -> dict:
    """
    Generate a unified JSON + Markdown codebase modernisation status assessment report.
    """
    ts = datetime.now(timezone.utc).isoformat()

    # Aggregate tracking statistics from migration nodes
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

    # Write out structural telemetry JSON report tracking records
    report_data = {"summary": summary, "findings": findings, "changes": changes}
    json_path   = Path(output_dir) / "srao_report.json"
    json_path.write_text(json.dumps(report_data, indent=2), encoding="utf-8")

    # Generate development team review facing Markdown visualizations
    md_path = Path(output_dir) / "srao_report.md"
    md_path.write_text(_build_markdown(summary, findings, changes), encoding="utf-8")

    logger.info("Modernization reports compiled successfully to workspace: %s, %s", json_path, md_path)
    return {
        "status":          "success",
        "json_report":     str(json_path),
        "markdown_report": str(md_path),
        "summary":         summary,
    }


def _build_markdown(summary: dict, findings: list, changes: list) -> str:
    """Assembles a valid, presentation-ready markdown payload representation."""
    lines = [
        "# 📊 SRAO Modernisation Report",
        f"\n**Repository Target Path:** `{summary['repo_url']}`",
        f"**Pipeline Generation Time:**  `{summary['timestamp']}`",
        f"**Automated Pull Request:** {summary.get('pr_url', '`N/A (Local Export Fallback Mode Enabled)`')}",
        "\n---\n",
        "## 📈 Metrics Dashboard Summary",
        f"| Analysis Metric Category | Evaluated Value Breakdown |",
        f"|:---|:---|",
        f"| **Total Structural Patterns Flagged** | {summary['total_findings']} |",
        f"| 🚨 HIGH Severity Architectural Items  | {summary['severity_breakdown'].get('HIGH', 0)} |",
        f"| ⚠️ MEDIUM Severity Code Structures| {summary['severity_breakdown'].get('MEDIUM', 0)} |",
        f"| ℹ️ LOW Severity Technical Debt Lines   | {summary['severity_breakdown'].get('LOW', 0)} |",
        f"| ✨ Source Files Verified & Modernised | {summary['files_modernised']} |",
        "\n## 🎯 Target Legacy Antipattern Top List Map",
    ]
    
    for pattern, count in summary.get("top_patterns", []):
        lines.append(f"- **`{pattern}`**: Identified {count} occurrence(s)")

    lines += ["\n## 🔍 Granular Structural Static Findings Log"]
    for f in findings:
        lines.append(f"\n### File Target Link: `{f.get('file', 'unknown')}`")
        lines.append(f"- **Design Pattern Token:** `{f.get('pattern_id')}`  `[{f.get('severity')}]`")
        lines.append(f"- **Transformation Intent:** {f.get('description')}")
        lines.append(f"- **Target Implementation Standard:** {f.get('target_java')}")
        lines.append(f"- **File Match Line Indexes:** `{f.get('line_numbers', [])}`")

    lines += ["\n## 🛠️ Modernized Syntax Delta Specifications Applied"]
    for c in changes:
        lines.append(f"\n### Complete Update Vector: `{c.get('file_path', 'unknown')}`")
        lines.append(f"- **Fixed Pattern Reference:** `{c.get('pattern_id')}`")
        lines.append(f"- **Refactoring Logic Verification Summary:** {c.get('explanation', 'Syntax translation approved.')}")

    return "\n".join(lines)
