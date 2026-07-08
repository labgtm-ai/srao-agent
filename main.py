"""
main.py
────────
Entry point for the SRAO agent.

Supports three run modes:
  1. Cloud Run HTTP server (production)  — MODE=server
  2. Google Cloud Console interactive    — MODE=interactive
  3. Single-repo batch job               — MODE=batch

Usage:
  # Interactive (Cloud Console / local)
  python main.py

  # Cloud Run server
  MODE=server python main.py

  # Batch modernisation
  MODE=batch REPO_URL=https://github.com/org/repo.git python main.py
"""

import json
import logging
import os
import sys


from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai.types import Content, Part

from agents.srao_agent  import srao_agent

from dotenv import load_dotenv
load_dotenv()

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("srao.main")

# ── Configuration ─────────────────────────────────────────────────────────────
MODE       = os.environ.get("MODE",       "interactive")
PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "your-project-id")
LOCATION   = os.environ.get("GCP_LOCATION",   "us-central1")
APP_ID     = "srao-app"
SESSION_ID = "srao-session-001"


def build_runner() -> Runner:
    """Instantiate the ADK Runner with in-memory session service."""
    session_service = InMemorySessionService()
    return Runner(
        agent=srao_agent,
        app_name=APP_ID,
        session_service=session_service,
        auto_create_session=True
    )


# ── Mode: Interactive (Cloud Console / terminal) ──────────────────────────────

def run_interactive():
    """REPL loop — paste a repo URL and let the agent modernise it."""
    runner = build_runner()
    logger.info("SRAO Agent ready. Type 'quit' to exit.\n")

    print("=" * 60)
    print("  SRAO — AI-Powered Java Modernisation Agent")
    print("  Running on Vertex AI / Google Cloud")
    print("=" * 60)

    while True:
        try:
            user_input = input("\n> Enter repo URL (or instruction): ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
            break

        if user_input.lower() in {"quit", "exit", "q"}:
            break
        if not user_input:
            continue

        # If it looks like a repo URL, wrap it in a task message
        if user_input.startswith("http") or user_input.startswith("git@"):
            message = (
                f"Please modernise the Java codebase in this repository: {user_input}\n"
                "Scan all Java files, detect legacy patterns, generate modernised code, "
                "and create a GitHub Pull Request with the changes."
            )
        else:
            message = user_input

        content = Content(role="user", parts=[Part.from_text(text=message)])

        print("\n[SRAO Agent thinking...]\n")
        final_response = None
        for event in runner.run(
            user_id="developer",
            session_id=SESSION_ID,
            new_message=content,
        ):
            if event.is_final_response():
                final_response = event

        if final_response and final_response.content:
            for part in final_response.content.parts:
                if part.text:
                    print(f"[Agent]: {part.text}")


# ── Mode: Batch ───────────────────────────────────────────────────────────────

def run_batch():
    """Non-interactive single-repo modernisation batch run."""
    repo_url = os.environ.get("REPO_URL", "")
    if not repo_url:
        logger.error("REPO_URL environment variable not set.")
        sys.exit(1)

    branch      = os.environ.get("REPO_BRANCH", "main")
    repo_owner  = os.environ.get("GITHUB_OWNER", "")
    repo_name   = os.environ.get("GITHUB_REPO",  "")

    message = (
        f"Modernise the Java codebase at: {repo_url} (branch: {branch})\n"
        f"GitHub repo: {repo_owner}/{repo_name}\n"
        "Scan all Java files, detect legacy patterns, generate modernised code "
        "using Java 17/21 idioms, validate changes, and create a Pull Request."
    )

    runner  = build_runner()
    content = Content(role="user", parts=[Part.from_text(text=message)])

    logger.info("Starting batch modernisation of %s", repo_url)
    final_response = None
    for event in runner.run(
        user_id="developer",
        session_id=SESSION_ID,
        new_message=content,
    ):
        if event.is_final_response():
            final_response = event

    if final_response and final_response.content:
        for part in final_response.content.parts:
            if part.text:
                print(part.text)


# ── Mode: Cloud Run HTTP server ───────────────────────────────────────────────

def run_server():
    """HTTP server mode for Cloud Run deployment."""
    from flask import Flask, request, jsonify

    app    = Flask(__name__)
    runner = build_runner()

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok", "agent": "srao", "version": "1.0"})

    @app.route("/modernise", methods=["POST"])
    def modernise():
        data     = request.get_json(force=True)
        repo_url = data.get("repo_url", "")
        branch   = data.get("branch", "main")
        owner    = data.get("github_owner", "")
        repo     = data.get("github_repo", "")

        if not repo_url:
            return jsonify({"error": "repo_url is required"}), 400

        message = (
            f"Modernise the Java codebase at: {repo_url} (branch: {branch})\n"
            f"GitHub repo: {owner}/{repo}\n"
            "Scan, analyse, modernise, and create a Pull Request."
        )
        content = Content(role="user", parts=[Part.from_text(text=message)])

        final_text = ""
        for event in runner.run(
            user_id="developer",
            session_id=SESSION_ID,
            new_message=content,
        ):
            if event.is_final_response() and event.content:
                for part in event.content.parts:
                    if part.text:
                        final_text += part.text

        return jsonify({"status": "success", "result": final_text})

    port = int(os.environ.get("PORT", 8080))
    logger.info("SRAO Cloud Run server starting on port %d", port)
    app.run(host="0.0.0.0", port=port)


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if MODE == "server":
        run_server()
    elif MODE == "batch":
        run_batch()
    else:
        run_interactive()
