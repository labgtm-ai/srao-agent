"""
tools/ast_analyzer.py
──────────────────────
Analyses a Java source file for legacy patterns that can be modernised.
Provides complete file context to the generation layer to prevent fragmentation bugs.
Filters rules dynamically using version compliance thresholds and architectural package context.
"""

import re
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

# ── Pattern registry with Minimum Version Requirements ────────────────────────
# Structure: (pattern_id, description, severity, regex, target_java, min_required_version)
PATTERNS = [
    (
        "RAW_THREAD",
        "Raw Thread/Runnable usage — replace with Virtual Threads (Project Loom)",
        "HIGH",
        r"\bnew\s+Thread\s*\(|implements\s+Runnable\b",
        "Java 21",
        21, 
    ),
    (
        "SYNCHRONIZED_BLOCK",
        "synchronized block/method — consider java.util.concurrent alternatives",
        "HIGH",
        r"\bsynchronized\s*[\(\{]",
        "Java 21",
        21, 
    ),
    (
        "BLOCKING_SLEEP",
        "Thread.sleep() — indicates blocking wait, use reactive/async pattern",
        "HIGH",
        r"Thread\.sleep\s*\(",
        "Java 8+",
        8,  
    ),
        # Change ONLY this pattern row inside the PATTERNS registry array:
    (
        "COMPLETABLE_FUTURE_MISSING",
        "Blocking streaming I/O in layer — wrap with CompletableFuture handles",
        "HIGH",
        r"\.(getInputStream|getReader|readLine|readAllBytes)\s*\(", # SRAO FIX: Removed plain .get to prevent List.get() false positives
        "Java 8+",
        8,  
    ),
    (
        "MANUAL_MAPPER",
        "Manual boilerplate object mapping detected — replace with MapStruct or automated ModelMapper components",
        "MEDIUM",
        r"(\w+)\.set(\w+)\s*\(\s*\w+\.get\2\s*\(\s*\)\s*\)",
        "Java 8+",
        8, # Detects structural patterns like userDto.setName(user.getName()) inside data mapping blocks
    ),
    (
        "POJO_CLASS",
        "Mutable POJO with getters/setters — candidate for immutable Record class",
        "MEDIUM",
        r"private\s+\w+\s+\w+\s*;\s*\n.*public\s+\w+\s+get\w+\s*\(\s*\)",
        "Java 16+",
        16, # Java Records finalized in JDK 16 (Dynamic gate restriction)
    ),
    (
        "NULL_CHECK",
        "Explicit null check — replace with Optional<T>",
        "MEDIUM",
        r"if\s*\(\s*\w+\s*==\s*null\s*\)|if\s*\(\s*null\s*==\s*\w+\s*\)",
        "Java 8+",
        8,  
    ),
    (
        "RAW_TYPE",
        "Raw type usage (no generics) — add proper type parameters",
        "MEDIUM",
        r"\b(List|Map|Set|Collection|ArrayList|HashMap|HashSet)\s+\w+\s*=\s*new\s+\1\s*\(\)",
        "Java 8+",
        8,  
    ),
    (
        "INSTANCEOF_CAST",
        "instanceof + explicit cast — use pattern matching instanceof",
        "MEDIUM",
        r"instanceof\s+\w+\s*\)\s*\{\s*\n\s*\w+\s+\w+\s*=\s*\(\w+\)",
        "Java 16+",
        16, 
    ),
    (
        "FOR_LOOP",
        "Traditional for-loop — replace with Stream API",
        "LOW",
        r"\bfor\s*\(\s*(int\s+\w+\s*=\s*0|\w+\s+\w+\s*:\s*\w+)",
        "Java 8+",
        8,  
    ),
    (
        "STRING_CONCAT",
        "String concatenation in loop — use StringBuilder or String.join()",
        "LOW",
        r'(\w+)\s*\+=\s*"',
        "Java 8+",
        8,  
    ),
    (
        "STRING_BUFFER",
        "StringBuffer — replace with StringBuilder (non-thread-safe context)",
        "LOW",
        r"\bnew\s+StringBuffer\s*\(",
        "Java 8+",
        8,  
    ),
    (
        "MULTILINE_STRING",
        "Multi-line String concatenation — use Text Blocks",
        "LOW",
        r'"[^"]*\\n[^"]*"\s*\+\s*"',
        "Java 15+",
        15, 
    ),
    (
        "ANONYMOUS_INNER_CLASS",
        "Anonymous inner class detected — replace with Lambda expression",
        "MEDIUM",
        r"new\s+\w+\s*\(\s*\)\s*\{",
        "Java 8+",
        8,
    ),

    (
        "HASHTABLE",
        "Legacy Hashtable usage — replace with HashMap or ConcurrentHashMap",
        "MEDIUM",
        r"\bHashtable\b",
        "Java 8+",
        8,
    ),

    (
        "VECTOR",
        "Legacy Vector usage — replace with ArrayList or immutable collections",
        "MEDIUM",
        r"\bVector\b",
        "Java 8+",
        8,
    ),

    (
        "ENUMERATION",
        "Legacy Enumeration usage — replace with Iterator or Streams",
        "LOW",
        r"\bEnumeration\b",
        "Java 8+",
        8,
    ),

    (
        "DATE_API",
        "Legacy Date API usage — replace with java.time API",
        "MEDIUM",
        r"\bDate\b",
        "Java 8+",
        8,
    ),

    (
        "CALENDAR_API",
        "Legacy Calendar API usage — replace with java.time API",
        "MEDIUM",
        r"\bCalendar\b",
        "Java 8+",
        8,
    ),

    (
        "SIMPLE_DATE_FORMAT",
        "SimpleDateFormat usage — replace with DateTimeFormatter",
        "LOW",
        r"\bSimpleDateFormat\b",
        "Java 8+",
        8,
    ),

    (
        "CALLBACK_INTERFACE",
        "Callback interface usage — replace with CompletableFuture",
        "MEDIUM",
        r"interface\s+\w*Callback|implements\s+\w*Callback|new\s+\w*Callback\s*\(",
        "Java 8+",
        8,
    ),

    (
        "UNCHECKED_CAST",
        "Unchecked type casting detected",
        "HIGH",
        r"\(\s*[\w<>?,\s]+\s*\)\s*\w+",
        "Java 8+",
        8,
    ),

    (
        "SWITCH_STATEMENT",
        "Traditional switch statement — replace with switch expressions",
        "LOW",
        r"\bswitch\s*\(",
        "Java 14+",
        14,
    ),

    (
        "FIELD_INJECTION",
        "Spring field injection detected — replace with constructor injection",
        "MEDIUM",
        r"@Autowired\s*\n\s*private",
        "Java 8+",
        8,
    ),

    (
        "GENERIC_EXCEPTION",
        "Generic Exception catch detected — use specific exception types",
        "LOW",
        r"catch\s*\(\s*Exception\s+\w+\s*\)",
        "Java 8+",
        8,
    ),

    (
        "SYSTEM_OUT",
        "System.out.println detected — replace with logging framework",
        "LOW",
        r"System\.out\.println\s*\(",
        "Java 8+",
        8,
    ),
]

SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


def analyze_java_file(file_path: str, target_version: int) -> dict:
    """
    Analyse a single Java source file for legacy patterns up to target_version.
    Includes a fast-pass short circuit for pure boilerplate POJO/DTO data holders.
    """
    try:
        source = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return {"status": "error", "message": f"File not found: {file_path}"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}

    findings = []
    
    # Check if the file path indicates a data object (DTO, Request, Response, Entity)
    normalized_path = file_path.lower().replace("\\", "/")
    is_data_object = any(token in normalized_path for token in ["/dto/", "/request", "/response", "/entity"])

    # ── POJO/DTO FAST-PASS SHORT CIRCUIT ──
    # If it's a data object, check if it's a pure boilerplate bean (private fields + getters/setters)
    if is_data_object and target_version >= 16:
        has_pojo_pattern = re.search(r"private\s+\w+\s+\w+\s*;\s*\n.*public\s+\w+\s+get\w+\s*\(\s*\)", source)
        
        # Check if it lacks service annotations or complex business logic markers
        is_pure_bean = not any(marker in source for marker in ["@Service", "@Controller", "@Autowired", "Repository", "ExecutorService"])
        
        if has_pojo_pattern and is_pure_bean:
            logger.info(f"⚡ Fast-Pass Triggered: Isulating exclusive Record upgrade for pure data bean: {file_path}")
            
            # Find line number for the class fields/getters to show a clean log anchor
            line_no = source[:has_pojo_pattern.start()].count("\n") + 1
            
            return {
                "status": "success",
                "file": file_path,
                "findings": [{
                    "pattern_id": "POJO_CLASS",
                    "description": "Mutable POJO with getters/setters — candidate for immutable Record class",
                    "severity": "MEDIUM",
                    "line_numbers": [line_no],
                    "target_java": "Java 16+",
                    "snippet": source
                }],
                "total_findings": 1,
                "severity_summary": {"HIGH": 0, "MEDIUM": 1, "LOW": 0}
            }

    # ── STANDARD MULTI-PATTERN EVALUATION LOOPS ──
    # (If the file isn't a pure bean, it falls through to check everything normally as before)
    is_mapper_class = "mapper" in normalized_path

    for pattern_id, description, severity, regex, target_java, min_required_version in PATTERNS:
        if min_required_version > target_version:
            continue

        if is_data_object and pattern_id in ["COMPLETABLE_FUTURE_MISSING", "RAW_THREAD", "SYNCHRONIZED_BLOCK", "BLOCKING_SLEEP"]:
            continue

        if pattern_id == "MANUAL_MAPPER" and not is_mapper_class:
            continue

        try:
            matches = list(re.finditer(regex, source, re.MULTILINE))
        except re.error:
            continue

        if not matches:
            continue

        line_numbers = [source[: m.start()].count("\n") + 1 for m in matches]

        findings.append({
            "pattern_id":   pattern_id,
            "description":  description,
            "severity":     severity,
            "line_numbers":  line_numbers,
            "target_java":   target_java,
            "snippet":       source,  
        })

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
