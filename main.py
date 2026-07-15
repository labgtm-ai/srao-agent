"""
main.py  —  SRAO Agent  v5  ·  google-adk 2.4.0
=================================================

ROOT CAUSE OF "(Stage 2 completed — agent produced no text summary)"
─────────────────────────────────────────────────────────────────────
When Stage 2 was given all 33 findings at once, gemini-2.5-flash tried
to generate 33 function_call objects in a single response (took 29s).
The resulting JSON was too large / malformed:
  finish_reason = MALFORMED_FUNCTION_CALL  →  content = None
ADK sets content=None on that event.  Our loop:
  "if not event.content: continue" → skipped it → all_text stayed empty.

THE FIX — batched Stage 2
──────────────────────────
Instead of sending all 33 findings to the agent at once, Stage 2 now
sends files in BATCHES of BATCH_SIZE (default 4) per agent turn.
Each batch is a separate runner.run() call with a fresh compact prompt.
4 files per turn = at most ~12 tool calls (retrieve+modernise+validate per file).
That fits comfortably in one LLM response.

Results are accumulated across all batches into one final report.

ALSO FIXED:
  - Verbose event logging: finish_reason and error_code are now logged
    so failures surface immediately rather than silently returning empty.
  - Each batch turn logs clearly (Batch 1/N, files x-y of total).
"""

import os
os.environ["GOOGLE_GENAI_USE_VERTEXAI"]   = "true"
os.environ["GOOGLE_GENAI_USE_ENTERPRISE"] = "true"

import asyncio
import logging
import sys
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
    try:
        import google.auth
        _, p = google.auth.default()
        if p: os.environ["GOOGLE_CLOUD_PROJECT"] = p
    except Exception: pass

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
    return RunConfig(
        max_llm_calls = 500,
        context_window_compression = ContextWindowCompressionConfig(
            trigger_tokens = 32000,
            sliding_window = SlidingWindow(target_tokens=16000),
        ),
    )


def build_runner() -> InMemoryRunner:
    runner = InMemoryRunner(agent=srao_agent, app_name=APP_NAME)
    asyncio.run(runner.session_service.create_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID,
    ))
    return runner


def run_agent_turn(runner: InMemoryRunner, user_text: str,
                   label: str = "") -> str:
    """
    Send one message, collect text from ALL events.
    Now also logs finish_reason and error_code so failures are visible.
    """
    message     = Content(role="user", parts=[Part(text=user_text)])
    all_text    : list[str] = []
    tool_counts : dict[str, int] = {}
    tag = f"[{label}] " if label else ""

    for event in runner.run(
        user_id=USER_ID, session_id=SESSION_ID,
        new_message=message, run_config=make_run_config(),
    ):
        # ── Log finish_reason / error so failures surface ─────────────────
        if event.finish_reason and str(event.finish_reason) not in ("FinishReason.STOP",""):
            logger.warning("%sfinish_reason=%s error_code=%s error_msg=%s",
                           tag, event.finish_reason,
                           event.error_code, event.error_message)
        if event.error_message:
            logger.error("%sAgent error: %s", tag, event.error_message)

        if not event.content or not event.content.parts:
            continue

        for part in event.content.parts:
            if part.text:
                all_text.append(part.text)
            elif hasattr(part, "function_call") and part.function_call:
                n = part.function_call.name
                tool_counts[n] = tool_counts.get(n, 0) + 1
                logger.info("%s[→ tool] %-32s #%d", tag, n, tool_counts[n])
            elif hasattr(part, "function_response") and part.function_response:
                logger.info("%s[← tool] %s", tag, part.function_response.name)

    text = "".join(all_text).strip()
    if text:
        logger.info("%sTEXT RECEIVED (%d chars)", tag, len(text))
    else:
        logger.warning("%sNO TEXT in response (tool_counts=%s)", tag, tool_counts)
    return text


# ── Stage 1: pure Python, zero LLM ───────────────────────────────────────────

def stage1_scan_and_analyse(repo_url: str, branch: str) -> dict:
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


def _build_batch_message(batch_files: list[str],
                          findings_by_file: dict,
                          repo_path: str,
                          repo_url: str,
                          gh_owner: str,
                          gh_repo: str,
                          branch: str,
                          batch_num: int,
                          total_batches: int,
                          is_last: bool,
                          has_github: bool) -> str:
    """Build a compact prompt for one batch of files."""

    lines = [
        f"Batch {batch_num} of {total_batches} — modernise these {len(batch_files)} file(s):",
        f"Repository: {repo_url}  (local path: {repo_path})",
        "",
    ]
    for fname in batch_files:

        full_path = str(Path(repo_path) / fname)

        lines.append(
        f"""
        FILE: {full_path}
        IMPORTANT:
        Use this file path when calling tools.
        Do NOT include full source code in tool arguments.
        The tool can load the file directly from disk.
        """
        )

        for fi in findings_by_file.get(fname, [])[:4]:
            lines.append(
                f"  FILE: {full_path}"
                f"\n    pattern={fi['pattern_id']}  severity={fi['severity']}"
                f"\n    desc={fi['description']}"
                f"\n    target={fi['target_java']}"
            )
    lines.append("")

    pr_line = (
        "After processing the files return only a summary "
        "of the modernization results."
    ) if is_last else (
        f"After processing all {len(batch_files)} files above, "
        "Do not call save_changes_locally."
    )

    
    lines += [
        "For EACH file:",
        "1. Call retrieve_java_docs.",
        "2. Call modernize_code_snippet.",
        "3. Call validate_diff.",
        "Use tool calls directly.",
        "Do NOT generate Python code.",
        "Do not generate print(default_api...) calls.",
        "Return only a summary of completed work."
        "Do NOT write print(default_api.modernize_code_snippet(...)).",
        "Do NOT write print(default_api.validate_diff(...)).",
        "Do NOT write print(default_api.save_changes_locally(...)).",
        "Only retry validation failures.",
        pr_line,
        "Return a concise text summary.",
        "Do NOT call generate_report.",
        "generate_report will be called once after all batches finish.",
    ]

    return "\n".join(lines)


# ── Stage 2: batched agent turns ─────────────────────────────────────────────

def stage2_modernise_batched(runner: InMemoryRunner,
                              stage1: dict,
                              repo_url: str,
                              branch: str,
                              gh_owner: str,
                              gh_repo: str) -> str:
    """
    Process files in batches of BATCH_SIZE.
    Each batch is one agent turn — at most ~12 tool calls per turn.
    Keeps the LLM response small to avoid MALFORMED_FUNCTION_CALL.
    """
    logger.info("=== STAGE 2: Batched agent modernisation (batch_size=%d) ===",
                BATCH_SIZE)

    ordered  = stage1["ordered_files"]
    fby_file = stage1["findings_by_file"]
    rpath    = stage1["repo_path"]
    has_gh   = all(os.environ.get(v) for v in
                   ["GITHUB_TOKEN","GITHUB_OWNER","GITHUB_REPO"])

    # Split into batches
    batches = [ordered[i:i+BATCH_SIZE]
               for i in range(0, len(ordered), BATCH_SIZE)]
    total   = len(batches)
    summaries: list[str] = []

    for idx, batch in enumerate(batches, start=1):
        is_last = (idx == total)
        logger.info("--- Batch %d/%d (%d files) ---", idx, total, len(batch))

        message = _build_batch_message(
            batch_files      = batch,
            findings_by_file = fby_file,
            repo_path        = rpath,
            repo_url         = repo_url,
            gh_owner         = gh_owner,
            gh_repo          = gh_repo,
            branch           = branch,
            batch_num        = idx,
            total_batches    = total,
            is_last          = is_last,
            has_github       = has_gh,
        )

        result = run_agent_turn(runner, message,
                                label=f"Batch {idx}/{total}")

        if result:
            summaries.append(f"### Batch {idx}/{total}\n{result}")
        else:
            summaries.append(
                f"### Batch {idx}/{total}\n"
                f"⚠ No text response for files: {', '.join(batch)}"
            )

    logger.info("--- Final summary skipped ---")

    return "\n\n".join(summaries) or "(No output generated)"


def run_pipeline(runner, repo_url, branch="main", gh_owner="", gh_repo="") -> str:
    stage1 = stage1_scan_and_analyse(repo_url, branch)
    if "error" in stage1:
        return f"Pipeline failed at scan: {stage1['error']}"
    if not stage1["all_findings"]:
        return (f"No legacy patterns found in {len(stage1['java_files'])} "
                "files — already modern!")

    n_batches = -(-len(stage1["ordered_files"]) // BATCH_SIZE)  # ceil division
    logger.info("Processing %d files in %d batches of %d",
                len(stage1["ordered_files"]), n_batches, BATCH_SIZE)

    result = stage2_modernise_batched(runner, stage1, repo_url, branch,
                                       gh_owner, gh_repo)
    return result or "(Stage 2 completed — agent produced no text summary)"


# ── INTERACTIVE ───────────────────────────────────────────────────────────────

def run_interactive():
    runner = build_runner()
    print()
    print("=" * 68)
    print("  SRAO — Java Modernisation Agent  v5  (ADK 2.4.0)")
    print(f"  Project : {PROJECT_ID}  |  Location : {LOCATION}")
    print(f"  Backend : Vertex AI  |  Batch size : {BATCH_SIZE} files/turn")
    print("  Type 'quit' to exit.")
    print("=" * 68)
    if not all(os.environ.get(v) for v in ["GITHUB_TOKEN","GITHUB_OWNER","GITHUB_REPO"]):
        print()
        print("  ℹ  No GitHub vars — files saved to /tmp/srao_output/")
        print("  ℹ  Set GITHUB_TOKEN / GITHUB_OWNER / GITHUB_REPO for auto-PR")
    print()
    print("Enter a GitHub repo URL to modernise it end-to-end.")
    print()

    while True:
        try:
            user_input = input("> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting."); break
        if not user_input: continue
        if user_input.lower() in {"quit","exit","q"}: break

        if user_input.startswith(("http://","https://","git@")):
            branch   = os.environ.get("REPO_BRANCH",  "main")
            gh_owner = os.environ.get("GITHUB_OWNER", "")
            gh_repo  = os.environ.get("GITHUB_REPO",  "")
            print("\n[Stage 1] Scanning & analysing (no LLM)...\n")
            response = run_pipeline(runner, user_input, branch, gh_owner, gh_repo)
        else:
            response = run_agent_turn(runner, user_input)

        print(f"\n{'='*68}\n[Agent Summary]:\n{'='*68}\n{response}\n{'='*68}\n")


def run_batch():
    repo_url = os.environ.get("REPO_URL","")
    if not repo_url: logger.error("REPO_URL not set"); sys.exit(1)
    runner = build_runner()
    print(run_pipeline(runner, repo_url,
                       branch  =os.environ.get("REPO_BRANCH","main"),
                       gh_owner=os.environ.get("GITHUB_OWNER",""),
                       gh_repo =os.environ.get("GITHUB_REPO","")))


def create_app():
    from flask import Flask, request, jsonify
    fapp, runner = Flask(__name__), build_runner()

    @fapp.route("/health")
    def health():
        return jsonify({"status":"ok","version":"5.0",
                        "project":PROJECT_ID,"batch_size":BATCH_SIZE})

    @fapp.route("/modernise", methods=["POST"])
    def modernise():
        d = request.get_json(force=True)
        if not d.get("repo_url"):
            return jsonify({"error":"repo_url required"}), 400
        result = run_pipeline(runner, d["repo_url"],
                              d.get("branch","main"),
                              d.get("github_owner",""),
                              d.get("github_repo",""))
        return jsonify({"status":"success","result":result})

    return fapp


app = create_app() if MODE == "server" else None

if __name__ == "__main__":
    if MODE == "server":
        create_app().run(host="0.0.0.0", port=int(os.environ.get("PORT",8080)))
    elif MODE == "batch":
        run_batch()
    else:
        run_interactive()
