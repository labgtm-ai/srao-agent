import os
import sys
import re
import json
import logging
import subprocess
import tempfile
import difflib
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

# Force Google GenAI SDK to use Vertex AI endpoints
os.environ["GOOGLE_GENAI_USE_VERTEXAI"]   = "true"
os.environ["GOOGLE_GENAI_USE_ENTERPRISE"] = "true"

import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig
from google.adk.runners  import InMemoryRunner, RunConfig
from google.adk.sessions import InMemorySessionService
from google.genai.types  import Content, Part, ContextWindowCompressionConfig, SlidingWindow
from google.adk.agents import LlmAgent
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
logger = logging.getLogger("srao.pipeline")

# Configuration constants loaded directly from system parameters
PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "deutschebank-aipocs")
LOCATION   = os.environ.get("GCP_LOCATION",   "us-central1")
MODEL_NAME = "gemini-2.5-flash"
MAX_RETRIES = 3
BATCH_SIZE = int(os.environ.get("SRAO_BATCH_SIZE", "1"))
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_API_URL = "https://api.github.com"
APP_NAME, USER_ID, SESSION_ID = "srao-app", "developer", "srao-session-001"

vertexai.init(project=PROJECT_ID, location=LOCATION)

# ── GLOBAL ACCUMULATOR CACHE FOR PERSISTING DATA DELTAS ──
ACCUMULATED_CHANGES_CACHE = []

# Pattern match registry definitions
PATTERNS = [
    ("RAW_THREAD", "Raw Thread/Runnable usage — replace with Virtual Threads", "HIGH", r"\bnew\s+Thread\s*\(|implements\s+Runnable\b", "Java 21"),
    ("SYNCHRONIZED_BLOCK", "synchronized block/method — consider java.util.concurrent alternatives", "HIGH", r"\bsynchronized\s*[\(\{]", "Java 21"),
    ("BLOCKING_SLEEP", "Thread.sleep() — indicates blocking wait", "HIGH", r"Thread\.sleep\s*\(", "Java 8+"),
    ("COMPLETABLE_FUTURE_MISSING", "Blocking I/O in service layer — wrap with CompletableFuture", "HIGH", r"\.(get|getInputStream|readLine)\s*\(", "Java 8+"),
    ("POJO_CLASS", "Mutable POJO with getters/setters — candidate for Record class", "MEDIUM", r"private\s+\w+\s+\w+\s*;\s*\n.*public\s+\w+\s+get\w+\s*\(\s*\)", "Java 16+"),
    ("NULL_CHECK", "Explicit null check — replace with Optional<T>", "MEDIUM", r"if\s*\(\s*\w+\s*==\s*null\s*\)|if\s*\(\s*null\s*==\s*\w+\s*\)", "Java 8+"),
    ("RAW_TYPE", "Raw type usage (no generics) — add proper type parameters", "MEDIUM", r"\b(List|Map|Set|Collection|ArrayList|HashMap|HashSet)\s+\w+\s*=\s*new\s+\1\s*\(\)", "Java 8+"),
    ("INSTANCEOF_CAST", "instanceof + explicit cast — use pattern matching instanceof", "MEDIUM", r"instanceof\s+\w+\s*\)\s*\{\s*\n\s*\w+\s+\w+\s*=\s*\(\w+\)", "Java 16+"),
    ("FOR_LOOP", "Traditional for-loop — replace with Stream API", "LOW", r"\bfor\s*\(\s*(int\s+\w+\s*=\s*0|\w+\s+\w+\s*:\s*\w+)", "Java 8+"),
    ("STRING_CONCAT", "String concatenation in loop — use StringBuilder", "LOW", r'(\w+)\s*\+=\s*"', "Java 8+"),
    ("STRING_BUFFER", "StringBuffer — replace with StringBuilder", "LOW", r"\bnew\s+StringBuffer\s*\(", "Java 8+"),
    ("MULTILINE_STRING", "Multi-line String concatenation — use Text Blocks", "LOW", r'"[^"]*\\n[^"]*"\s*\+\s*"', "Java 15+"),
]
SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}

FALLBACK_KB = {
    "FOR_LOOP": "List<String> upper = names.stream().filter(name -> name.startsWith(\"A\")).map(String::toUpperCase).collect(Collectors.toList());",
    "RAW_THREAD": "try (var executor = Executors.newVirtualThreadPerTaskExecutor()) { executor.submit(() -> doWork()); }",
    "POJO_CLASS": "public record User(String name, int age) {}",
    "NULL_CHECK": "String result = Optional.ofNullable(value).map(String::toUpperCase).orElse(null);",
    "STRING_BUFFER": "StringBuilder sb = new StringBuilder(); items.forEach(sb::append);",
    "MULTILINE_STRING": "String json = \"\"\"\n{\n  \"name\": \"Alice\"\n}\n\"\"\";",
    "INSTANCEOF_CAST": "if (obj instanceof String s) { System.out.println(s.length()); }",
    "SYNCHRONIZED_BLOCK": "private final AtomicInteger count = new AtomicInteger(0); public void increment() { count.incrementAndGet(); }"
}

def scan_repository(repo_url: str, branch: str = "main", target_dir: Optional[str] = None) -> dict:
    if os.path.exists(repo_url) and os.path.isdir(repo_url):
        return {"status": "success", "repo_path": str(Path(repo_url).resolve()), "java_files": _find_java_files(repo_url)}
    clone_dir = target_dir or tempfile.mkdtemp(prefix="srao_repo_")
    try:
        res = subprocess.run(["git", "clone", "--depth", "1", "--branch", branch, repo_url, clone_dir], capture_output=True, text=True, timeout=120)
        if res.returncode != 0: return {"status": "error", "message": f"git clone failed: {res.stderr.strip()}"}
        return {"status": "success", "repo_path": clone_dir, "java_files": _find_java_files(clone_dir)}
    except Exception as e: return {"status": "error", "message": str(e)}

def _find_java_files(root: str) -> list[str]:
    root_path = Path(root)
    java_files = []
    for path in root_path.rglob("*.java"):
        rel = path.relative_to(root_path).as_posix()
        if "src/test" in rel or "target/" in rel or "build/" in rel: continue
        java_files.append(rel)
    java_files.sort(key=lambda p: (1 if "src/test" in p else 0, p))
    return java_files

def analyze_java_file(file_path: str) -> dict:
    try: source = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except Exception as exc: return {"status": "error", "message": str(exc)}
    findings = []
    for pid, desc, sev, regex, target in PATTERNS:
        try: matches = list(re.finditer(regex, source, re.MULTILINE))
        except re.error: continue
        if not matches: continue
        line_numbers = [source[: m.start()].count("\n") + 1 for m in matches]
        findings.append({"pattern_id": pid, "description": desc, "severity": sev, "line_numbers": line_numbers, "target_java": target, "snippet": source})
    findings.sort(key=lambda f: SEVERITY_ORDER.get(f["severity"], 9))
    return {"status": "success", "file": file_path, "findings": findings}

def classify_severity(findings: list) -> dict:
    file_severity = {}
    for f in findings: file_severity.setdefault(f.get("file", "unknown"), set()).add(f.get("severity", "LOW"))
    high   = [f for f, sevs in file_severity.items() if "HIGH"   in sevs]
    medium = [f for f, sevs in file_severity.items() if "MEDIUM" in sevs and "HIGH" not in sevs]
    low    = [f for f, sevs in file_severity.items() if sevs == {"LOW"}]
    return {"high_priority_files": sorted(high), "medium_priority_files": sorted(medium), "low_priority_files": sorted(low), "recommended_order": sorted(high) + sorted(medium) + sorted(low)}

def apply_patch(repo_root: str, relative_file_path: str, modernized_code: str) -> bool:
    try:
        full_path = Path(repo_root) / relative_file_path
        backup_path = full_path.with_suffix(".java.bak")
        if not backup_path.exists(): full_path.rename(backup_path)
        full_path.write_text(modernized_code, encoding="utf-8")
        return True
    except Exception: return False

def revert_file_changes(repo_root: str, relative_file_path: str) -> None:
    full_path = Path(repo_root) / relative_file_path
    backup_path = full_path.with_suffix(".java.bak")
    if backup_path.exists():
        if full_path.exists(): full_path.unlink()
        backup_path.rename(full_path)

def run_compile_validation(repo_root: str, relative_file_path: str) -> tuple[bool, str]:
    root_path = Path(repo_root)
    full_target_file = root_path / relative_file_path
    try:
        source_base = str(root_path)
        parts = Path(relative_file_path).parts
        if "src" in parts:
            src_idx = parts.index("src")
            if src_idx + 2 < len(parts) and parts[src_idx+1] == "main" and parts[src_idx+2] == "java":
                source_base = str(root_path / Path(*parts[:src_idx+3]))
            else:
                source_base = str(root_path / Path(*parts[:src_idx+1]))
        res = subprocess.run(
            ["javac", "-source", "17", "-target", "17", "-proc:none", "-Xlint:none", "-sourcepath", source_base, str(full_target_file)],
            capture_output=True, text=True, timeout=30
        )
        if res.returncode == 0:
            class_file = full_target_file.with_suffix(".class")
            if class_file.exists(): class_file.unlink()
            return True, ""
        error_output = res.stderr
        lines = error_output.splitlines()
        ignorable_errors = ["does not exist", "cannot find symbol", "package org.springframework", "package jakarta"]
        real_syntax_issues = []
        for line in lines:
            if "error:" in line and not any(msg in line for msg in ignorable_errors):
                real_syntax_issues.append(line)
        if not real_syntax_issues: return True, ""
        return False, "\n".join(real_syntax_issues)
    except Exception as e: return True, ""

def validate_diff(original_code: str, modernised_code: str, file_path: str = "") -> dict:
    """Validate that modernised code is structurally sound and represents a complete file."""
    if not original_code and file_path:
        try: original_code = Path(file_path).read_text(encoding="utf-8", errors="ignore")
        except Exception: pass
    stripped = modernised_code.strip()
    if not stripped: return {"status": "invalid", "issues": ["Empty"]}
    if any(sig in stripped.lower() for sig in ["i'm sorry", "i cannot", "traceback"]) and len(stripped) < 500: return {"status": "invalid", "issues": ["Error response"]}
    if not any(sig in stripped for sig in ["{", ";", "class", "public"]): return {"status": "invalid", "issues": ["Not java"]}
    if "class " not in stripped and "interface " not in stripped and "record " not in stripped: return {"status": "invalid", "issues": ["Partial output"]}
    diff = "".join(difflib.unified_diff(original_code.splitlines(keepends=True), modernised_code.splitlines(keepends=True)))
    return {"status": "valid", "diff": diff, "issues": []}

def save_changes_locally(changes: List[Dict], output_dir: str = "/tmp/srao_output") -> dict:
    """Save all modernised files to disk when GitHub credentials or dependencies fail."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    for c in changes:
        rel = c.get("file_path") or c.get("file") or "unknown.java"
        code = c.get("modernised_code") or ""
        try:
            src_idx = next(i for i, p in enumerate(Path(rel).parts) if p == "src")
            clean = Path(*Path(rel).parts[src_idx:])
        except StopIteration: clean = Path(Path(rel).name)
        dest = out / clean
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(code, encoding="utf-8")
    return {"status": "success", "output_dir": str(out)}

def create_pull_request(repo_owner: str, repo_name: str, base_branch: str, changes: List[Dict]) -> dict:
    """
    Executes actual Git operations, applies file updates to disk,
    pushes a time-scoped feature branch upstream, and creates a GitHub Pull Request.
    """
    token = os.environ.get("GITHUB_TOKEN", GITHUB_TOKEN)
    raw_owner = repo_owner or os.environ.get("GITHUB_OWNER", "")
    raw_repo  = repo_name  or os.environ.get("GITHUB_REPO",  "")
    
    if not token or not raw_owner or not raw_repo or not changes: 
        return save_changes_locally(changes)
    
    # ── URL SANITIZATION ──
    def clean_slug(s: str) -> str:
        s = s.replace("https://", "").replace("http://", "").replace("github.com", "")
        s = s.strip("/").replace(".git", "")
        return s.split("/")[-1] if "/" in s else s

    owner = clean_slug(raw_owner)
    repo  = clean_slug(raw_repo)
    
    # ── REPOSITORY PATH DISCOVERY ──
    paths = sorted(list(Path("/tmp").glob("srao_repo_*")), key=os.path.getmtime)
    if not paths:
        return {"status": "error", "message": "No valid git repository path found in /tmp"}
    local_repo_path = str(paths[-1])
    
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    feature_branch = f"srao/modernized_code_{ts}"
    
    try:
        logger.info(f"Purging remote cache configurations in sandbox path: {local_repo_path}")
        
        # Configure local git actor identity
        subprocess.run(["git", "config", "user.name", "SRAO Agent"], cwd=local_repo_path, check=True)
        subprocess.run(["git", "config", "user.email", "srao@google.com"], cwd=local_repo_path, check=True)
        
        # ── APPLY CHANGES TO FILESYSTEM (FIXED BUG) ──
        for change in changes:
            file_path = change.get("file_path")
            new_content = change.get("content")  # Assuming your change dict provides the code asset
            if file_path and new_content:
                full_path = Path(local_repo_path) / file_path
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_text(new_content, encoding="utf-8")
        
        # ── REMOTE MANAGEMENT ──
        subprocess.run(["git", "remote", "remove", "origin"], cwd=local_repo_path, capture_output=True)
        
        authenticated_url = f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"
        logger.info(f"Registering pristine upstream remote origin path track: https://github.com/{owner}/{repo}.git")
        subprocess.run(["git", "remote", "add", "origin", authenticated_url], cwd=local_repo_path, check=True)
        
        # Checkout branch, stage files, and commit
        subprocess.run(["git", "checkout", "-b", feature_branch], cwd=local_repo_path, check=True)
        subprocess.run(["git", "add", "."], cwd=local_repo_path, check=True)
        subprocess.run(["git", "commit", "-m", "chore: modernized java assets via multi-agent pipeline"], cwd=local_repo_path, check=True)
        
        logger.info(f"Pushing time-scoped feature branch '{feature_branch}' upstream...")
        subprocess.run(["git", "push", "-u", "origin", feature_branch], cwd=local_repo_path, check=True)
        
    except subprocess.CalledProcessError as e:
        logger.error(f"Git execution failed: {e.stderr if hasattr(e, 'stderr') else str(e)}")
        return {"status": "error", "message": f"Git sub-process failure: {str(e)}"}
    except Exception as e: 
        logger.error(f"Git operation block failed: {str(e)}")
        return {"status": "error", "message": f"Git fail: {str(e)}"}
    
    # ── PULL REQUEST CREATION ──
    details = "\n".join([f"- **{c.get('file_path','unknown')}**: {c.get('explanation','Refactored.')}" for c in changes])
    payload = {
        "title": f"[SRAO] Java Modernization: {len(changes)} files refactored", 
        "body": f"### AI Modernization\n\n{details}", 
        "head": feature_branch, 
        "base": base_branch
    }
    
    res = requests.post(
        f"{GITHUB_API_URL}/repos/{owner}/{repo}/pulls", 
        json=payload, 
        headers={
            "Authorization": f"token {token}", 
            "Accept": "application/vnd.github.v3+json"
        }
    )
    
    if res.status_code in (200, 201): 
        return {"status": "success", "pr_url": res.json()["html_url"], "message": "PR created"}
    
    return {"status": "error", "message": res.text}


class CodeModernizer:
    def __init__(self):
        schema = {
            "type": "OBJECT",
            "properties": {
                "modernised_code": {"type": "STRING"},
                "explanation": {"type": "STRING"},
                "breaking_change": {"type": "BOOLEAN"},
                "imports_added": {"type": "ARRAY", "items": {"type": "STRING"}}
            },
            "required": ["modernised_code", "explanation", "breaking_change", "imports_added"]
        }
        self.model = GenerativeModel(MODEL_NAME, generation_config=GenerationConfig(temperature=0.1, response_mime_type="application/json", response_schema=schema))

    def run_modernize(self, ctx: dict) -> Optional[dict]:
        prompt = f"Modernize target {ctx['pattern_id']}\nDescription: {ctx['description']}\nCode:\n```java\n{ctx['legacy_code']}\n```\nRAG info:\n{ctx.get('rag_context','')}"
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                res = self.model.generate_content(prompt)
                parsed = json.loads(res.text.strip())
                apply_patch(ctx["repo_root"], ctx["file_path"], parsed["modernised_code"])
                ok, err = run_compile_validation(ctx["repo_root"], ctx["file_path"])
                if ok: return parsed
                prompt = f"Fix compilation error: {err}\nPrevious attempt:\n{parsed['modernised_code']}"
            except Exception: pass
        revert_file_changes(ctx["repo_root"], ctx["file_path"])
        return None

modernizer_engine = CodeModernizer()

def agent_retrieve_java_docs(pattern_id: str) -> str:
    return FALLBACK_KB.get(pattern_id, "No documentation found.")

def agent_modernize_code_snippet(file_path: str, pattern_id: str, description: str, target_java: str, rag_context: str, repo_root: str = "/tmp") -> dict:
    if repo_root == "/tmp":
        paths = sorted(list(Path("/tmp").glob("srao_repo_*")), key=os.path.getmtime)
        if paths: repo_root = str(paths[-1])
    full_path = str(Path(repo_root) / file_path)
    code = Path(full_path).read_text(encoding="utf-8", errors="ignore")
    res = modernizer_engine.run_modernize({"pattern_id": pattern_id, "description": description, "target_java": target_java, "legacy_code": code, "rag_context": rag_context, "repo_root": repo_root, "file_path": file_path})
    
    if res and "modernised_code" in res:
        record = {"file_path": file_path, "modernised_code": res["modernised_code"], "explanation": res.get("explanation", "Refactored.")}
        ACCUMULATED_CHANGES_CACHE.append(record)
        return res
    return {"modernised_code": code, "explanation": "Failed", "breaking_change": True, "imports_added": []}

def agent_validate_diff(file_path: str, modernised_code: str, repo_root: str = "/tmp") -> dict:
    if repo_root == "/tmp":
        paths = sorted(list(Path("/tmp").glob("srao_repo_*")), key=os.path.getmtime)
        if paths: repo_root = str(paths[-1])
    return validate_diff(original_code="", modernised_code=modernised_code, file_path=str(Path(repo_root) / file_path))

srao_agent = LlmAgent(
    model=MODEL_NAME, name="srao_agent", description="Modernization Agent",
    instruction="Iterate through findings sequentially. For each file finding, call agent_retrieve_java_docs, then agent_modernize_code_snippet, then agent_validate_diff.",
    tools=[agent_retrieve_java_docs, agent_modernize_code_snippet, agent_validate_diff]
)

def run_agent_turn(runner: InMemoryRunner, user_text: str, label: str = "") -> str:
    message = Content(role="user", parts=[Part(text=user_text)])
    all_text = []
    for event in runner.run(user_id=USER_ID, session_id=SESSION_ID, new_message=message, run_config=RunConfig(max_llm_calls=500)):
        if not event.content or not event.content.parts: continue
        for part in event.content.parts:
            if part.text: all_text.append(part.text)
    return "".join(all_text).strip()

def stage1_scan_and_analyse(repo_url: str, branch: str) -> dict:
    scan = scan_repository(repo_url, branch)
    if scan.get("status") != "success": return {"error": scan.get("message", "Scan failed")}
    repo_path, java_files = scan["repo_path"], scan["java_files"]
    all_findings = []
    for rel_path in java_files:
        res = analyze_java_file(str(Path(repo_path) / rel_path))
        if res.get("status") == "success" and res.get("findings"):
            for f in res["findings"]: f["file"] = rel_path
            all_findings.extend(res["findings"])
    classified = classify_severity(all_findings)
    findings_by_file = {}
    for f in all_findings: findings_by_file.setdefault(f.get("file","unknown"), []).append({"pattern_id": f.get("pattern_id"), "severity": f.get("severity"), "description": f.get("description"), "target_java": f.get("target_java")})
    return {"repo_path": repo_path, "findings_by_file": findings_by_file, "ordered_files": classified.get("recommended_order", [])}

def stage2_process_batches(stage1_data: dict, runner: InMemoryRunner):
    ordered_files = stage1_data.get("ordered_files", [])
    findings_by_file = stage1_data.get("findings_by_file", {})
    if not ordered_files: return
    
    global ACCUMULATED_CHANGES_CACHE
    ACCUMULATED_CHANGES_CACHE = []
    
    for i in range(0, len(ordered_files), BATCH_SIZE):
        batch_files = ordered_files[i : i + BATCH_SIZE]
        prompt = f"Process targeted batch component objects:\n{json.dumps({f: findings_by_file.get(f, []) for f in batch_files})}\nInstructions: Call tools in strict step sequence."
        run_agent_turn(runner, prompt, label=f"Batch {i+1}")

    if ACCUMULATED_CHANGES_CACHE:
        for c in ACCUMULATED_CHANGES_CACHE:
            if isinstance(c.get("file_path"), list) and len(c["file_path"]) > 0:
                c["file_path"] = c["file_path"][0]
                
        logger.info(f"Captured {len(ACCUMULATED_CHANGES_CACHE)} verified file additions. Triggering git pull request creation pipeline...")
        pr_result = create_pull_request(os.environ.get("GITHUB_OWNER",""), os.environ.get("GITHUB_REPO",""), "main", ACCUMULATED_CHANGES_CACHE)
        logger.info("PR Result URL details: %s", pr_result.get("pr_url", pr_result.get("message")))
    else:
        logger.warning("No functional code adjustments were registered during agent execution loops. Skipping PR submission.")

if __name__ == "__main__":
    os.environ["GITHUB_TOKEN"] = "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"
    os.environ["GITHUB_OWNER"] = "labgtm-ai"
    os.environ["GITHUB_REPO"]  = "java-legacy-enterprise-app"
    
    os.environ["GCP_PROJECT_ID"] = "deutschebank-aipocs"
    os.environ["GOOGLE_CLOUD_PROJECT"] = "deutschebank-aipocs"
    os.environ["GCP_LOCATION"] = "us-central1"
    os.environ["SRAO_BATCH_SIZE"] = "1"

    PROJECT_ID = "deutschebank-aipocs"
    LOCATION = "us-central1"
    GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]

    if len(sys.argv) < 2:
        target_repo = "https://github.com"
    else:
        target_repo = sys.argv[1]
        
    target_branch = sys.argv[2] if len(sys.argv) > 2 else "main"
    
    logger.info("Initializing multi-agent migration environment sequence loop workflow.")
    try:
        analysis_results = stage1_scan_and_analyse(target_repo, target_branch)
        if "error" in analysis_results: sys.exit(analysis_results["error"])
        runner = InMemoryRunner(agent=srao_agent, app_name=APP_NAME)
        asyncio.run(runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID))
        stage2_process_batches(analysis_results, runner)
    except Exception as e:
        logger.exception("❌ CRITICAL PIPELINE CRASH:")
        sys.exit(1)
