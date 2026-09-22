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

    def build(
        self,
        summary_reports: List[Dict[str, Any]],
        repo_url: str = "Target Project Base",
        validation_results: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Orchestration adapter bridging multi-agent data loops to static
        output report files.

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

            file_name = (
                item.get("file")
                or finding.get("file")
                or "unknown.java"
            )

            findings.append({
                "file": file_name,
                "pattern_id": finding.get(
                    "pattern_id",
                    "MODERNIZE"
                ),
                "severity": finding.get(
                    "severity",
                    "LOW"
                ),
                "description": finding.get(
                    "description",
                    "Legacy Code pattern match."
                ),
                "target_java": finding.get(
                    "target_java",
                    "Java 17/21"
                ),
                "line_numbers": finding.get(
                    "line_numbers",
                    []
                )
            })

            changes.append({
                "file_path": file_name,
                "pattern_id": finding.get(
                    "pattern_id",
                    "MODERNIZE"
                ),
                "explanation": (
                    result.get("explanation")
                    or item.get("explanation")
                    or "Refactored Java statements."
                )
            })

            if item.get("pull_request_url"):
                pr_url = item.get("pull_request_url")

        return generate_report(
            repo_url=repo_url,
            findings=findings,
            changes=changes,
            pr_url=pr_url,
            output_dir=self.output_dir,
            validation_results=validation_results,
        )


def generate_report(
    repo_url: str,
    findings: list[dict],
    changes: list[dict],
    pr_url: Optional[str] = None,
    output_dir: str = "/tmp/srao_output",
    validation_results: Optional[dict] = None,
) -> dict:
    """
    Generate a unified JSON + Markdown codebase modernisation
    status assessment report.
    """

    ts = datetime.now(timezone.utc).isoformat()

    validation_results = validation_results or {}

    # Aggregate tracking statistics from migration nodes
    severity_count = {
        "HIGH": 0,
        "MEDIUM": 0,
        "LOW": 0
    }

    pattern_count: dict[str, int] = {}

    for f in findings:
        sev = f.get("severity", "LOW")
        severity_count[sev] = severity_count.get(sev, 0) + 1

        pid = f.get("pattern_id", "UNKNOWN")
        pattern_count[pid] = pattern_count.get(pid, 0) + 1

    summary = {
        "repo_url": repo_url,
        "timestamp": ts,
        "total_findings": len(findings),
        "severity_breakdown": severity_count,
        "top_patterns": sorted(
            pattern_count.items(),
            key=lambda x: -x[1]
        )[:5],
        "files_modernised": len(changes),
        "pr_url": pr_url,

        # Validation results
        "validation": {
            "baseline_compile": validation_results.get(
                "baseline_compile"
            ),
            "production_compile": validation_results.get(
                "production_compile"
            ),
            "test_compile": validation_results.get(
                "test_compile"
            ),
            "unit_tests": validation_results.get(
                "unit_tests"
            ),
            "global_build": validation_results.get(
                "global_build"
            ),
            "spring_boot": validation_results.get(
                "spring_boot"
            ),
            "static_analysis": validation_results.get(
                "static_analysis"
            ),
            "static_analysis_tool": validation_results.get(
                "static_analysis_tool",
                "NOT_RUN"
            ),
        },

        # Repository test configuration
        "test_configuration": {
            "present": validation_results.get(
                "test_config_present",
                False
            ),
            "config_file": validation_results.get(
                "test_config_file"
            ),
        },

        # Unit test execution failure information
        "unit_test_failure_reason": validation_results.get(
            "unit_test_failure_reason"
        ),
    }

    # Write JSON report
    report_data = {
        "summary": summary,
        "findings": findings,
        "changes": changes
    }

    json_path = Path(output_dir) / "srao_report.json"

    json_path.write_text(
        json.dumps(
            report_data,
            indent=2
        ),
        encoding="utf-8"
    )

    # Generate Markdown report
    md_path = Path(output_dir) / "srao_report.md"

    md_path.write_text(
        _build_markdown(
            summary,
            findings,
            changes
        ),
        encoding="utf-8"
    )

    logger.info(
        "Modernization reports compiled successfully to workspace: %s, %s",
        json_path,
        md_path
    )

    return {
        "status": "success",
        "json_report": str(json_path),
        "markdown_report": str(md_path),
        "summary": summary,
    }


def _build_markdown(
    summary: dict,
    findings: list,
    changes: list
) -> str:
    """
    Assembles a valid, presentation-ready markdown payload representation.
    """

    lines = [
        "# 📊 SRAO Modernisation Report",

        f"\n**Repository Target Path:** `{summary['repo_url']}`",

        f"**Pipeline Generation Time:**  "
        f"`{summary['timestamp']}`",

        f"**Automated Pull Request:** "
        f"{summary.get('pr_url') or '`N/A (Local Export Fallback Mode Enabled)`'}",

        "\n---\n",

        "## 📈 Metrics Dashboard Summary",

        "| Analysis Metric Category | Evaluated Value Breakdown |",
        "|:---|:---|",

        f"| **Total Structural Patterns Flagged** | "
        f"{summary['total_findings']} |",

        f"| 🚨 HIGH Severity Architectural Items | "
        f"{summary['severity_breakdown'].get('HIGH', 0)} |",

        f"| ⚠️ MEDIUM Severity Code Structures | "
        f"{summary['severity_breakdown'].get('MEDIUM', 0)} |",

        f"| ℹ️ LOW Severity Technical Debt Lines | "
        f"{summary['severity_breakdown'].get('LOW', 0)} |",

        f"| ✨ Source Files Verified & Modernised | "
        f"{summary['files_modernised']} |",

        "\n## 🎯 Target Legacy Antipattern Top List Map",
    ]

    for pattern, count in summary.get(
        "top_patterns",
        []
    ):
        lines.append(
            f"- **`{pattern}`**: "
            f"Identified {count} occurrence(s)"
        )

    # ============================================================
    # Validation Summary
    # ============================================================

    validation = summary.get(
        "validation",
        {}
    )

    def validation_status(value):
        if value is True:
            return "✅ PASS"
        if value is False:
            return "❌ FAIL"
        return "➖ NOT RUN"

    lines += [
        "\n## ✅ Validation Summary",
        "",
        "| Validation Gate | Status |",
        "|:---|:---|",

        f"| Baseline Compile | "
        f"{validation_status(validation.get('baseline_compile'))} |",

        f"| Production Compile | "
        f"{validation_status(validation.get('production_compile'))} |",

        f"| Test Compile | "
        f"{validation_status(validation.get('test_compile'))} |",

        f"| Unit Test Execution | "
        f"{validation_status(validation.get('unit_tests'))} |",

        f"| Global Maven Build | "
        f"{validation_status(validation.get('global_build'))} |",

        f"| Spring Boot Startup | "
        f"{validation_status(validation.get('spring_boot'))} |",

        f"| Static Analysis "
        f"({validation.get('static_analysis_tool', 'N/A')}) | "
        f"{validation_status(validation.get('static_analysis'))} |",
    ]

    # ============================================================
    # Test Configuration
    # ============================================================

    test_configuration = summary.get(
        "test_configuration",
        {}
    )

    lines += [
        "",
        "## 🧪 Test Environment Validation",
        "",
    ]

    if test_configuration.get("present"):
        lines.append(
            "**Repository Test Configuration:** Detected"
        )

        lines.append(
            f"**Configuration Used:** "
            f"`{test_configuration.get('config_file')}`"
        )

    else:
        lines.append(
            "**Repository Test Configuration:** Not detected"
        )

    # ============================================================
    # Unit Test Failure Reason
    # ============================================================

    if validation.get("unit_tests") is False:

        failure_reason = summary.get(
            "unit_test_failure_reason"
        )

        lines += [
            "",
            "### ⚠️ Unit Test Execution Failure",
            "",
            "The unit tests did not complete successfully.",
            "",
            "**Failure Reason:**",
            "",
            "```text",
            failure_reason
            or "No specific failure reason was available.",
            "```",
        ]

    elif validation.get("unit_tests") is True:

        lines += [
            "",
            "**Unit Test Execution:** PASS",
        ]

    # ============================================================
    # Findings
    # ============================================================

    lines += [
        "\n## 🔍 Granular Structural Static Findings Log"
    ]

    for f in findings:

        lines.append(
            f"\n### File Target Link: "
            f"`{f.get('file', 'unknown')}`"
        )

        lines.append(
            f"- **Design Pattern Token:** "
            f"`{f.get('pattern_id')}` "
            f"`[{f.get('severity')}]`"
        )

        lines.append(
            f"- **Transformation Intent:** "
            f"{f.get('description')}"
        )

        lines.append(
            f"- **Target Implementation Standard:** "
            f"{f.get('target_java')}"
        )

        lines.append(
            f"- **File Match Line Indexes:** "
            f"`{f.get('line_numbers', [])}`"
        )

    # ============================================================
    # Changes
    # ============================================================

    lines += [
        "\n## 🛠️ Modernized Syntax Delta Specifications Applied"
    ]

    for c in changes:

        lines.append(
            f"\n### Complete Update Vector: "
            f"`{c.get('file_path', 'unknown')}`"
        )

        lines.append(
            f"- **Fixed Pattern Reference:** "
            f"`{c.get('pattern_id')}`"
        )

        lines.append(
            f"- **Refactoring Logic Verification Summary:** "
            f"{c.get('explanation', 'Syntax translation approved.')}"
        )

    return "\n".join(lines)
