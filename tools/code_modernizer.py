"""
tools/code_modernizer.py
─────────────────────────
Calls Vertex AI Gemini to generate modernised Java code for a given
legacy snippet and pattern. Uses structured prompting with RAG context.
Includes a retry loop for CI validation failures.
"""

import logging
import os
import re
import json
from typing import Optional
from pathlib import Path

import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig

logger = logging.getLogger(__name__)

PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "your-project-id")
LOCATION   = os.environ.get("GCP_LOCATION",   "us-central1")
MODEL_NAME = "gemini-2.5-flash"

MAX_RETRIES = 3

# ── Prompt templates ──────────────────────────────────────────────────────────

# SRAO: Escaped literal braces and aligned format variables to guarantee execution safety
MODERNIZE_PROMPT = """
You are an expert Java architect specialising in modernising legacy Java code to Java 17/21.

TASK: Refactor the legacy Java code snippet below.

PATTERN TO FIX: {pattern_id}
DESCRIPTION:    {description}
TARGET VERSION: {target_java}

LEGACY CODE:
```java
{legacy_code}
```

DOCUMENTATION & EXAMPLES:
{rag_context}

REQUIREMENTS:
1. Produce ONLY valid Java source code inside the 'modernised_code' structural property.
2. Preserve the exact method/class signature, access modifiers, and package structure.
3. Add a brief inline comment (// SRAO: ...) explaining the change on the first modified line.
4. The refactored code must compile cleanly with Java {java_version}.
5. Do NOT change unrelated code outside the pattern's scope.

OUTPUT FORMAT — Return your response matching this strict schema structure:
{{
  "modernised_code": "<complete refactored code with escaped newlines>",
  "explanation":     "<1-2 sentence explanation of what changed and why>",
  "breaking_change": false,
  "imports_added":   ["<fully qualified import if any>"]
}}
"""

RETRY_PROMPT = """
The previous refactoring attempt failed CI validation.

ORIGINAL LEGACY CODE:
```java
{legacy_code}
```

PREVIOUS ATTEMPT (failed):
```java
{previous_attempt}
```

VALIDATION ERROR:
{validation_error}

Please fix the issue and produce a corrected version.
Return your response matching the exact same structural JSON schema as before.
"""


def modernize_code_snippet(
    file_path: str,
    description: str,
    target_java: str,
    legacy_code: str = "",
    pattern_id: str = "",
    rag_context: str = "",
    previous_attempt: Optional[str] = None,
    validation_error: Optional[str] = None,
) -> dict:
    """
    Generate modernised Java code for a legacy snippet using Gemini.

    Args:
        file_path:         Path of the source file (for context).
        legacy_code:       The original Java code to modernise.
        pattern_id:        SRAO pattern identifier (e.g. "RAW_THREAD").
        description:       Human-readable description of the pattern.
        target_java:       Target Java version (e.g. "Java 21").
        rag_context:       Retrieved documentation and examples from RAG.
        previous_attempt:  Prior failed refactoring (for retry loop).
        validation_error:  CI/compile error from the prior attempt.

    Returns:
        {
          "status":          "success" | "error",
          "modernised_code": str,
          "explanation":     str,
          "breaking_change": bool,
          "imports_added":   [str],
          "attempts":        int
        }
    """
    
    vertexai.init(project=PROJECT_ID, location=LOCATION)
    
    # SRAO: Explicitly defined structural output schema for Gemini 2.5 Flash to guarantee un-fenced JSON responses
    response_schema = {
        "type": "OBJECT",
        "properties": {
            "modernised_code": {"type": "STRING", "description": "The complete refactored modern Java code."},
            "explanation": {"type": "STRING", "description": "A short summary outlining what changes were applied."},
            "breaking_change": {"type": "BOOLEAN", "description": "True if public method or framework bindings were updated."},
            "imports_added": {
                "type": "ARRAY", 
                "items": {"type": "STRING"}, 
                "description": "List of fully qualified dependencies introduced by this modernization execution."
            }
        },
        "required": ["modernised_code", "explanation", "breaking_change", "imports_added"]
    }

    model = GenerativeModel(
        MODEL_NAME,
        generation_config=GenerationConfig(
            temperature=0.1,
            top_p=0.95,
            max_output_tokens=8192,  # Increased token threshold to prevent generation truncation on large source files
            response_mime_type="application/json",
            response_schema=response_schema
        ),
    )

    # Load source from disk if not supplied
    if not legacy_code and file_path:
        try:
            legacy_code = Path(file_path).read_text(
                encoding="utf-8",
                errors="ignore"
            )
            logger.info(
                "Loaded source from %s (%d chars)",
                file_path,
                len(legacy_code),
            )
        except Exception as e:
            return {
                "status": "error",
                "message": f"Unable to read source file: {e}"
            }

    logger.info(
        "Modernizer input: file=%s pattern=%s code_length=%d",
        file_path,
        pattern_id,
        len(legacy_code),
    )

    java_version = _extract_version_number(target_java)

    for attempt in range(1, MAX_RETRIES + 1):
        logger.info("Modernising %s — pattern %s — attempt %d/%d",
                    file_path, pattern_id, attempt, MAX_RETRIES)

        if attempt == 1 or not previous_attempt:
            prompt = MODERNIZE_PROMPT.format(
                pattern_id=pattern_id,
                description=description,
                target_java=target_java,
                legacy_code=legacy_code,
                rag_context=rag_context or "No additional context available.",
                java_version=java_version,
            )
        else:
            prompt = RETRY_PROMPT.format(
                legacy_code=legacy_code,
                previous_attempt=previous_attempt,
                validation_error=validation_error or "Unknown error",
            )

        try:
            response  = model.generate_content(prompt)
            raw_text  = response.text.strip()
            parsed    = _parse_json_response(raw_text)

            if parsed and "modernised_code" in parsed:
                return {
                    "status":          "success",
                    "modernised_code": parsed.get("modernised_code", ""),
                    "explanation":     parsed.get("explanation", ""),
                    "breaking_change": parsed.get("breaking_change", False),
                    "imports_added":   parsed.get("imports_added", []),
                    "attempts":        attempt,
                }

            logger.warning("Attempt %d: could not parse clean schema compliant response", attempt)
            previous_attempt  = raw_text
            validation_error  = "Response schema did not contain required modernised_code parameters"

        except Exception as exc:
            logger.error("Gemini call failed on attempt %d: %s", attempt, exc)
            if attempt == MAX_RETRIES:
                return {"status": "error", "message": str(exc), "attempts": attempt}

    return {
        "status":   "error",
        "message":  f"Failed to generate valid code after {MAX_RETRIES} attempts.",
        "attempts": MAX_RETRIES,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_json_response(text: str) -> Optional[dict]:
    """Extract and parse JSON safely from structured Gemini outputs."""
    if not text:
        return None

    # SRAO: Sanitizes markdown fences if Gemini forces text markers into raw blocks
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Fallback tracking logic for deep object extractions
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                logger.error("Failed to parse fallback text group string into standard JSON structural format.")
                pass
    return None


def _extract_version_number(target_java: str) -> str:
    """Extract the numeric version from a string like 'Java 21' or 'Java 17/21'."""
    match = re.search(r"(\d+)", target_java)
    return match.group(1) if match else "21"
