"""
main.py  —  SRAO Agent  v5  ·  google-adk 2.4.0
=================================================
Optimized runner script ensuring strict batch isolation, robust 
ADK event listening patterns, and automated remote GitHub PR creation.
"""

import os
# Force Google GenAI SDK to use Vertex AI endpoints
os.environ["GOOGLE_GENAI_USE_VERTEXAI"]   = "true"
os.environ["GOOGLE_GENAI_USE_ENTERPRISE"] = "true"

import asyncio
import logging
import sys
import json
from pathlib import Path
from dotenv import load_dotenv

# Load variables from .env if present
load_dotenv()

# Fallback authentication handling for Google Cloud
if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
    try:
        import google.auth
        _, p = google.auth.default()
        if p: os.environ["GOOGLE_CLOUD_PROJECT"] = p
    except Exception:
        pass

if not os.environ.get("GCP_PROJECT_ID"):
    os.environ["GCP_PROJECT_ID"] = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
if not os.environ.get("GOOGLE_CLOUD_LOCATION"):
    os.environ["GOOGLE_CLOUD_LOCATION"] = os.environ.get("GCP_LOCATION", "us-central1")

from google.adk.runners  import InMemoryRunner, RunConfig
from google.adk.sessions import InMemorySessionService
from google.genai.types  import (Content, Part,
                                  ContextWindowCompressionConfig, SlidingWindow)

from agents.srao_agent  import srao_agent
from tools.repo_scanner import scan_repository, list_java_files
from tools.ast_analyzer import analyze_java_file, classify_severity
from tools.pr_creator import create_pull_request

# Configure systemic console logging formats
logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("srao.main")

MODE       = os.environ.get("MODE",          "interactive")
PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "")
LOCATION   = os.environ.get("GCP_LOCATION",  "us-central1")
APP_NAME, USER_ID, SESSION_ID = "srao-app", "developer", "srao-session-001"

# Number of files per agent batch turn — keeps response size manageable
BATCH_SIZE = int(os.environ.get("SRAO_BATCH_SIZE", "1"))

if not PROJECT_ID:
    logger.error(
        "\n  GCP_PROJECT_ID not set.\n"
        "  Fix: export GCP_PROJECT_ID=$(gcloud config get-value project)\n"
    )
    sys.exit(1)


def _warn_github():
    """Validates presence of repository target credentials."""
    missing = [v for v in ["GITHUB_TOKEN","GITHUB_OWNER","GITHUB_REPO"]
               if not os.environ.get(v)]
    if missing:
        logger.warning(
            "\n  ⚠  GitHub PR creation will be SKIPPED — missing:\n"
            + "\n".join(f"     export {v}=your_value" for v in missing)
            + "\n  Modernised files saved automatically to /tmp/srao_output/\n"
        )
    return not bool(missing)


logger.info("Project=%s  Location=%s  Mode=%s  Backend=VertexAI  BatchSize=%d",
            PROJECT_ID, LOCATION, MODE, BATCH_SIZE)
_warn_github()


def make_run_config() -> RunConfig:
    """Builds token compression bounds for the underlying ADK Session window."""
    return RunConfig(
        max_llm_calls = 500,
        context_window_compression = ContextWindowCompressionConfig(
            trigger_tokens = 32000,
            sliding_window = SlidingWindow(target_tokens=16000),
        ),
    )


def build_runner() -> InMemoryRunner:
    """Prepares and instantiates an ADK runtime container session."""
    runner = InMemoryRunner(agent=srao_agent, app_name=APP_NAME)
    asyncio.run(runner.session_service.create_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID,
    ))
    return runner


def run_agent_turn(runner: InMemoryRunner, user_text: str, label: str = "") -> list[dict]:
    """
    Send one message, collect text, and capture all successful tool outputs safely.
    Handles ADK structure variations to prevent dropping modernized code data.
    """
    message = Content(role="user", parts=[Part(text=user_text)])
    batch_changes = []
    tag = f"[{label}] " if label else ""

    for event in runner.run(
        user_id=USER_ID, session_id=SESSION_ID, 
        new_message=message, run_config=RunConfig(max_llm_calls=500)
    ):
        if not event.content or not event.content.parts: 
            continue
            
        for part in event.content.parts:
            # Check if this part contains a response back from our modernization tool
            if hasattr(part, "function_response") and part.function_response:
                if part.function_response.name == "agent_modernize_code_snippet":
                    try:
                        # Extract the raw payload data from the ADK wrapper object
                        resp_obj = part.function_response.response
                        
                        # Fallback 1: If ADK wraps it as a dictionary containing a 'text' or 'content' string
                        if isinstance(resp_obj, dict):
                            if "text" in resp_obj:
                                r_data = json.loads(resp_obj["text"])
                            elif "content" in resp_obj:
                                r_data = json.loads(resp_obj["content"])
                            # Fallback 2: The object is already a pre-parsed target data dictionary
                            elif "modernised_code" in resp_obj:
                                r_data = resp_obj
                            else:
                                r_data = resp_obj
                        else:
                            # Fallback 3: Try reading the attribute directly if it's a structural class object
                            raw_str = getattr(resp_obj, "text", getattr(resp_obj, "content", "{}"))
                            r_data = json.loads(raw_str)

                        # Validate and commit to our tracking batch array if structural keys clear
                        if isinstance(r_data, dict) and "modernised_code" in r_data:
                            logger.info("%sSuccessfully captured modernized code adjustments payload.", tag)
                            batch_changes.append(r_data)
                            
                    except Exception as parse_err:
                        logger.warning("%sFailed parsing tool tracking payload item: %s", tag, parse_err)
                        pass
                        
    return batch_changes


def stage1_scan_and_analyse(repo_url: str, branch: str) -> dict:
    """Locates and indexes legacy code structural blocks across the filesystem."""
    logger.info("=== STAGE 1: Scanning repository (no LLM) ===")

    scan = scan_repository(repo_url, branch)
    if scan.get("status") != "success":
        return {"error": scan.get("message", "Scan failed")}

    repo_path  = scan["repo_path"]
    java_files = scan["java_files"]
    logger.info("Cloned: %d Java files found", len(java_files))

    all_findings = []
    for rel_path in java_files:
        full_path = str(Path(repo_path) / rel_path)
        result    = analyze_java_file(full_path)
        if result.get("status") == "success" and result.get("findings"):
            for f in result["findings"]:
                f["file"] = rel_path
            all_findings.extend(result["findings"])
            logger.info("  %-60s findings=%d", rel_path, len(result["findings"]))

    classified = classify_severity(all_findings)
    high   = classified.get("high_priority_files",   [])
    medium = classified.get("medium_priority_files", [])
    low    = classified.get("low_priority_files",    [])

    findings_by_file: dict[str, list] = {}
    for f in all_findings:
        findings_by_file.setdefault(f.get("file","unknown"), []).append({
            "pattern_id":  f.get("pattern_id"),
            "severity":    f.get("severity"),
            "description": f.get("description"),
            "target_java": f.get("target_java"),
            "lines":       f.get("line_numbers",[])[:3],
        })

    ordered_files = high + medium + low
    logger.info("=== STAGE 1 complete: %d findings in %d files ===",
                len(all_findings), len(findings_by_file))

    return {
        "repo_path":        repo_path,
        "java_files":       java_files,
        "all_findings":     all_findings,
        "findings_by_file": findings_by_file,
        "ordered_files":    ordered_files,
        "high":  high, "medium": medium, "low": low,
    }


def _build_batch_message(batch_files: list[str], findings_by_file: dict) -> str:
    """Builds a highly structural batch instruction to prevent ADK response blowing up."""
    batch_data = {}
    for f in batch_files:
        batch_data[f] = findings_by_file.get(f, [])
        
    prompt = (
        f"Execute Stage 2 modernization updates on the following targeted batch components:\n"
        f"{json.dumps(batch_data, indent=2)}\n\n"
        f"Instructions:\n"
        f"1. Sequentially call 'agent_modernize_code_snippet' for each target file path.\n"
        f"2. Do not combine files together into single concurrent execution contexts.\n"
        f"3. Provide a clear text summary indicating modification statuses once all files are processed."
    )
    return prompt


def stage2_process_batches(stage1_data: dict, runner: InMemoryRunner):
    """
    Executes modernization batch runs sequentially and handles the final automated GitHub PR step.
    """
    ordered_files = stage1_data.get("ordered_files", [])
    findings_by_file = stage1_data.get("findings_by_file", {})
    
    if not ordered_files:
        logger.info("No legacy code pattern matches found. Skipping Stage 2 modernization execution.")
        return

    total_files = len(ordered_files)
    logger.info("=== STAGE 2: Modernizing codebases in chunks (Batch Size: %d) ===", BATCH_SIZE)

    all_accumulated_changes = []

    for i in range(0, total_files, BATCH_SIZE):
        batch_files = ordered_files[i : i + BATCH_SIZE]
        batch_num = (i // BATCH_SIZE) + 1
        total_batches = (total_files + BATCH_SIZE - 1) // BATCH_SIZE
        
        label = f"Batch {batch_num}/{total_batches}"
        logger.info("Processing %s: Target files %d to %d of %d", label, i + 1, min(i + BATCH_SIZE, total_files), total_files)
        
        # Build structured tracking prompt payloads
        batch_chunk_data = {f: findings_by_file.get(f, []) for f in batch_files}
        prompt = (
            f"Execute Stage 2 modernization updates on the following targeted batch components:\n"
            f"{json.dumps(batch_chunk_data, indent=2)}\n\n"
            f"Instructions:\n"
            f"1. Sequentially call 'agent_modernize_code_snippet' for each target file path.\n"
            f"2. Do not combine files together into single concurrent execution contexts.\n"
            f"3. Provide a clear text summary indicating modification statuses once all files are processed."
        )
        
        # Execute turning logic and capture tool payload parameters
        changes = run_agent_turn(runner, prompt, label=label)
        
        if changes:
            # Inject matching file metrics back into the payload structures
            for idx, c in enumerate(changes):
                if idx < len(batch_files):
                    c["file"] = batch_files[idx]
                    c["file_path"] = batch_files[idx]
            all_accumulated_changes.extend(changes)

    # ── Trigger Dynamic Git Branch Push and Live Pull Request Activation ──
    if all_accumulated_changes:
        logger.info("Batch modernization runs concluded. Initiating pull request operations...")
        
        # Inject the absolute sandbox path location pointer into every change element
        for change in all_accumulated_changes:
            change["repo_path"] = stage1_data["repo_path"]

        pr_result = create_pull_request(
            repo_owner=os.environ.get("GITHUB_OWNER", ""),
            repo_name=os.environ.get("GITHUB_REPO", ""),
            base_branch="main",
            changes=all_accumulated_changes
        )
        logger.info("Pull request automation output response status: %s", pr_result.get("message"))
    else:
        logger.warning("No functional code adjustments were registered during agent execution loops. Skipping PR submission.")


if __name__ == "__main__":
    # Check for core runtime terminal input parameters
    if len(sys.argv) < 2:
        logger.error("❌ ERROR: Missing target repository address configuration input parameter.")
        logger.error("Usage: python main_pipeline.py <repository_url_or_local_path> [branch_name]")
        sys.exit(1)
        
    target_repo = sys.argv[1]
    target_branch = sys.argv[2] if len(sys.argv) > 2 else "main"
    
    logger.info("Initializing multi-agent migration environment sequence loop workflow.")
    
    try:
        # 1. Execute static system analyzer
        analysis_results = stage1_scan_and_analyse(target_repo, target_branch)
        
        if not analysis_results or "error" in analysis_results:
            logger.error("❌ STAGE 1 SETUP FAILED: %s", analysis_results.get("error", "Unknown initialization failure"))
            sys.exit(1)
            
        # 2. Spin up ADK Agent interface
        agent_runner = InMemoryRunner(agent=srao_agent, app_name=APP_NAME)
        asyncio.run(agent_runner.session_service.create_session(
            app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID
        ))
        
        # 3. Stream tasks systematically to Gemini 2.5 Flash
        stage2_process_batches(analysis_results, agent_runner)
        logger.info("Multi-agent enterprise codebase translation process complete.")
        
    except Exception as e:
        logger.exception("❌ CRITICAL PIPELINE CRASH: An unhandled exception stopped execution.")
        sys.exit(1)
