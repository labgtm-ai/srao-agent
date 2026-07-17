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
    Generate modernised Java code for a legacy snippet using Gemini and write it to disk.

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

    # ── SRAO PATH GUARD REPAIR LAYER ──
    # Safely handle relative vs absolute sandbox path structures within the Cloud Shell execution root
    target_path = None
    if file_path:
        target_path = Path(file_path)
        if not target_path.is_absolute():
            target_path = Path(os.getcwd()) / file_path

    # Load source from disk if not supplied
        # ── SRAO BACKUP PATH LOADING GATEWAY ──
    # If legacy_code is empty or missing due to an LLM tool-calling schema bug,
    # force it to load directly from the absolute file system sandbox path!
    if not legacy_code or legacy_code.strip() == "":
        if file_path:
            try:
                target_path = Path(file_path)
                if not target_path.is_absolute():
                    target_path = Path(os.getcwd()) / file_path
                
                if target_path.exists():
                    legacy_code = target_path.read_text(encoding="utf-8", errors="ignore")
                    logger.info(f"🔄 SRAO Recovery: Tool parameter was empty. Successfully recovered code from disk ({len(legacy_code)} chars)")
            except Exception as e:
                logger.error(f"❌ SRAO Recovery Failed: Unable to read file from disk: {e}")


    logger.info(
        "Modernizer input: file=%s pattern=%s code_length=%d",
        str(target_path) if target_path else file_path,
        pattern_id,
        len(legacy_code),
    )

    java_version = _extract_version_number(target_java)

    for attempt in range(1, MAX_RETRIES + 1):
        logger.info("Modernising %s — pattern %s — attempt %d/%d",
                    str(target_path) if target_path else file_path, pattern_id, attempt, MAX_RETRIES)

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

            # ── NEW SRAO LIVE DEBUG PRINT DUMP ──
            print(f"\n====================================================")
            print(f"🔮 DEBUG: Raw Gemini Response for {pattern_id} (Attempt {attempt}):")
            print(raw_text[:1500]) # Prints the first 1500 characters of the raw json response
            print("====================================================\n")
            parsed    = _parse_json_response(raw_text)

            if parsed and "modernised_code" in parsed:
                generated_java_code = parsed.get("modernised_code", "")
                
                # ── SRAO WRITE BLOCK LAYER ──
                # This explicitly overwrites the modified code string directly to the target file path location on disk.
                if target_path and generated_java_code:
                    try:
                        # Ensure any parent folders exist, though they should in a cloned repo layout
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        target_path.write_text(generated_java_code, encoding="utf-8")
                        logger.info("💾 SRAO Disk Writer: Successfully applied refactoring modifications to disk: %s", str(target_path))
                    except Exception as disk_err:
                        logger.error("❌ SRAO Disk Writer failed to commit file updates: %s", disk_err)
                        return {"status": "error", "message": f"File write failure on disk: {disk_err}", "attempts": attempt}

                return {
                    "status":          "success",
                    "modernised_code": generated_java_code,
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
            matched_text = match.group()
            try:
                return json.loads(matched_text)
            except json.JSONDecodeError:
                # ── SRAO REPAIR LAYER: Handle literal unescaped characters in source code string payloads ──
                try:
                    # 1. Isolate the modernized code string value directly between property boundary anchors
                    code_match = re.search(r'"modernised_code"\s*:\s*"(.*?)"\s*,\s*"explanation"', matched_text, re.DOTALL)
                    if code_match:
                        raw_captured_code = code_match.group(1)
                        
                        # 2. Repair common string literal escaping defects
                        # Replaces literal raw newlines with escaped sequences so json.loads stays happy
                        repaired_text = matched_text.replace(raw_captured_code, raw_captured_code.replace("\n", "\\n"))
                        return json.loads(repaired_text)
                except Exception:
                    pass
                
                # Ultimate manual extraction fallback route if standard parsing fails completely
                logger.error("Failed to parse fallback text group string into standard JSON structural format. Initiating key extraction fallback.")
                try:
                    # Manually split out the code content to bypass JSON serialization problems
                    code_block = matched_text.split('"modernised_code":')[1].split('"explanation":')[0].strip().strip(',').strip('"')
                    # Fix escaped newlines and double quote characters
                    final_code = code_block.replace('\\n', '\n').replace('\\"', '"')
                    return {
                        "modernised_code": final_code,
                        "explanation": "Extracted via backup string token isolation mechanics.",
                        "breaking_change": False,
                        "imports_added": []
                    }
                except Exception:
                    logger.error("❌ Ultimate parsing fallback failed.")
                    
    return None


def _extract_version_number(target_java: str) -> str:
    """Extract the numeric version from a string like 'Java 21' or 'Java 17/21'."""
    match = re.search(r"(\d+)", target_java)
    return match.group(1) if match else "21"
