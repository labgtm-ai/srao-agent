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
from typing import Optional

import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig

logger = logging.getLogger(__name__)

PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "your-project-id")
LOCATION   = os.environ.get("GCP_LOCATION",   "us-central1")
MODEL_NAME = "gemini-2.0-flash-001"

MAX_RETRIES = 3

# ── Prompt templates ──────────────────────────────────────────────────────────

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
1. Produce ONLY the modernised Java code — no prose, no markdown explanation inside the code block.
2. Preserve the exact method/class signature, access modifiers, and package structure.
3. Add a brief inline comment (// SRAO: ...) explaining the change on the first modified line.
4. The refactored code must compile with Java {java_version}.
5. Do NOT change unrelated code outside the pattern's scope.

OUTPUT FORMAT — return ONLY this JSON (no markdown fences around the JSON):
{{
  "modernised_code": "<complete refactored code>",
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
Return the same JSON format as before.
"""


def modernize_code_snippet(
    file_path: str,
    legacy_code: str,
    pattern_id: str,
    description: str,
    target_java: str,
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
    model = GenerativeModel(
        MODEL_NAME,
        generation_config=GenerationConfig(
            temperature=0.1,        # Low temperature for deterministic code
            top_p=0.95,
            max_output_tokens=4096,
        ),
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

            if parsed:
                return {
                    "status":          "success",
                    "modernised_code": parsed.get("modernised_code", ""),
                    "explanation":     parsed.get("explanation", ""),
                    "breaking_change": parsed.get("breaking_change", False),
                    "imports_added":   parsed.get("imports_added", []),
                    "attempts":        attempt,
                }

            logger.warning("Attempt %d: could not parse JSON response", attempt)
            previous_attempt  = raw_text
            validation_error  = "Response was not valid JSON"

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
    """Extract and parse JSON from the model response."""
    import json

    # Strip markdown code fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find a JSON object anywhere in the text
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return None


def _extract_version_number(target_java: str) -> str:
    """Extract the numeric version from a string like 'Java 21' or 'Java 17/21'."""
    match = re.search(r"(\d+)", target_java)
    return match.group(1) if match else "21"
