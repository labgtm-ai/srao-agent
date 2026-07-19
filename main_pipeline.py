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
import time

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
from tools.ast_analyzer import analyze_java_file

os.environ["GOOGLE_GENAI_USE_VERTEXAI"]   = "true"
os.environ["GOOGLE_GENAI_USE_ENTERPRISE"] = "true"
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
        res = subprocess.run(["git", "clone", "--branch", branch, repo_url, clone_dir], capture_output=True, text=True, timeout=120)
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

# def analyze_java_file(file_path: str) -> dict:
#     try: source = Path(file_path).read_text(encoding="utf-8", errors="replace")
#     except Exception as exc: return {"status": "error", "message": str(exc)}
#     findings = []
#     for pid, desc, sev, regex, target in PATTERNS:
#         try: matches = list(re.finditer(regex, source, re.MULTILINE))
#         except re.error: continue
#         if not matches: continue
#         line_numbers = [source[: m.start()].count("\n") + 1 for m in matches]
#         findings.append({"pattern_id": pid, "description": desc, "severity": sev, "line_numbers": line_numbers, "target_java": target, "snippet": source})
#     findings.sort(key=lambda f: SEVERITY_ORDER.get(f["severity"], 9))
#     return {"status": "success", "file": file_path, "findings": findings}

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

def run_global_project_build(repo_root: str) -> tuple[bool, str]:
    """
    Executes a comprehensive project-wide Maven package build.
    Ensures all modernized source assets compile and link without errors.
    """
    logger.info("📦 STEP A: Launching Global Project-Wide Maven Build...")
    try:
        res = subprocess.run(
            ["mvn", "clean", "package", "-DskipTests=true"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=300  # 5-minute timeout window for full enterprise project compilation
        )
        if res.returncode == 0:
            logger.info("✅ Global Maven Build Successful! All packages created.")
            return True, "SUCCESS"
        
        logger.error("❌ Global Maven Build Failed.")
        build_output = (
            (res.stdout or "")
            + "\n"
            + (res.stderr or "")
        )

        return False, build_output
        #return False, res.stderr or res.stdout
    except Exception as e:
        return False, f"Maven global execution crash: {str(e)}"


def run_springboot_boot_check(repo_root: str) -> tuple[bool, str]:
    """
    Launches the Spring Boot application locally to verify it can initialize 
    and boot up successfully without contextual runtime crashes.
    Shuts down the application once the container context signals a clean startup.
    """
    logger.info("☕ STEP B: Launching Spring Boot Runtime Initialization Check...")
    try:
        # Start Spring Boot process using the Maven Spring Boot plugin
        # Forces 'spring-boot.run.fork=true' so we can terminate it programmatically
        proc = subprocess.Popen(
            ["mvn", "spring-boot:run", "-Dspring-boot.run.fork=true"],
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        
        # Read the console logs in real-time to monitor the boot-up status
        start_time = datetime.now()
        success_signals = ["Started ", "Application availability state LivenessState.CORRECT", "JVM running for"]
        failure_signals = ["APPLICATION FAILED TO START", "ExceptionInInitializerError", "ContextRefreshedEvent"]
        
        logger.info("Monitoring Spring Boot startup logs for operational readiness...")
        while True:
            # Prevent infinite hanging if the app stalls during boot
            if (datetime.now() - start_time).total_seconds() > 90:
                proc.terminate()
                return False, "Spring Boot initialization check timed out after 90 seconds."
                
            line = proc.stdout.readline()
            if not line:
                break
                
            # Print out matching startup indicators
            if "INFO" in line or "WARN" in line or "ERROR" in line:
                print(f"  [Spring Boot Log] {line.strip()}")
                
            if any(sig in line for sig in success_signals):
                logger.info("✅ Spring Boot Context initialized successfully! Application is live.")
                proc.terminate() # Safely shut down the running server app
                return True, "SUCCESS"
                
            if any(sig in line for sig in failure_signals):
                logger.error("❌ Spring Boot Application failed to boot down runtime lines.")
                proc.terminate()
                return False, f"Runtime Context Crash: {line.strip()}"
                
        return False, "Process terminated unexpectedly before boot completion."
    except Exception as e:
        return False, f"Spring Boot sub-process management handler errored: {str(e)}"


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
    
    logger.info(f"🚀 Initializing ADK session turn execution stream for {label}...")
    
    # Execute the agent stream loop
    for event in runner.run(
        user_id=USER_ID, 
        session_id=SESSION_ID, 
        new_message=message, 
        run_config=RunConfig(max_llm_calls=25)
    ):
        # 1. Fallback text extraction from standard content structures
        if hasattr(event, "content") and event.content and event.content.parts:
            for part in event.content.parts:
                if hasattr(part, "text") and part.text:
                    all_text.append(part.text)
                    
        # 2. ADK Specific: Extract intermediate thought or final step outputs
        if hasattr(event, "step_output") and event.step_output:
            if isinstance(event.step_output, str):
                all_text.append(event.step_output)
            elif hasattr(event.step_output, "text") and event.step_output.text:
                all_text.append(event.step_output.text)

        # 3. ADK Tool Execution Tracking: Print tool calls directly to console as visual anchors
        if hasattr(event, "tool_calls") and event.tool_calls:
            for call in event.tool_calls:
                logger.info(f"⚙️ [Tool Invocations] Agent triggered tool: {getattr(call, 'name', 'unknown')}")

    final_output = "".join(all_text).strip()
    
    # Safety Check: If the text stream is blank but files changed on disk anyway, 
    # we inject a fallback notice so the pipeline stays happy.
    if not final_output:
        return "[SRAO Diagnostics: Agent turn completed via silent internal tool orchestration pipelines.]"
        
    return final_output


def stage1_scan_and_analyse(repo_url: str, branch: str, target_version: int) -> dict:
    """
    Clones the repository and applies file-by-file pattern scanning, 
    filtering rules dynamically by the user's targeted Java version.
    """
    scan = scan_repository(repo_url, branch)
    if scan.get("status") != "success": 
        return {"error": scan.get("message", "Scan failed")}
        
    repo_path, java_files = scan["repo_path"], scan["java_files"]
    all_findings = []
    
    for rel_path in java_files:
        # Pass the target_version integer explicitly into the AST analyzer tool block
        res = analyze_java_file(str(Path(repo_path) / rel_path), target_version)
        if res.get("status") == "success" and res.get("findings"):
            for f in res["findings"]: 
                f["file"] = rel_path
            all_findings.extend(res["findings"])
            
    classified = classify_severity(all_findings)
    findings_by_file = {}
    
    for f in all_findings: 
        findings_by_file.setdefault(
            f.get("file", "unknown"),
            []
            ).append({
            "pattern_id": f.get("pattern_id"),
            "severity": f.get("severity"),
            "description": f.get("description"),
            "target_java": f.get("target_java"),
            "line_numbers": f.get("line_numbers", [])
        })
        
    return {
        "repo_path": repo_path, 
        "findings_by_file": findings_by_file, 
        "ordered_files": classified.get("recommended_order", []),
        "target_version": target_version
    }


def stage2_process_batches(
    stage1_data: dict,
    runner: InMemoryRunner
):
    """
    Modernizes production Java files sequentially.

    Production source changes are validated using Maven main-source
    compilation. After all production files are complete, affected test
    classes are updated separately to match the modernized production API.
    """

    from tools.code_modernizer import modernize_code_snippet
    from tools.rag_retriever import RagRetriever
    from tools.repo_scanner import (
        run_compile_validation,
        run_test_compile_validation,
        extract_failing_test_files,
        run_target_baseline_compile,
        prepare_maven_target_version,
        run_static_analysis_validation,
        clean_backup_files,
        revert_file_changes
    )
    from tools.pr_creator import create_pull_request

    ordered_files = stage1_data.get("ordered_files", [])
    findings_by_file = stage1_data.get("findings_by_file", {})
    repo_root = stage1_data["repo_path"]

    requested_target_version = int(
        stage1_data.get("target_version", 21)
    )
    target_java = f"Java {requested_target_version}"

    pipeline_start_time = time.time()

    validation_results = {
        "baseline_compile": False,
        "production_compile": False,
        "test_compile": False,
        "global_build": False,
        "spring_boot": False,
        "static_analysis": False,
        "static_analysis_tool": "NOT_RUN",
        "pr_created": False
    }

    if not ordered_files:
        logger.info(
            "No legacy code pattern matches found. "
            "Skipping Stage 2 modernization execution."
        )
        return

    total_files = len(ordered_files)

    logger.info(
        "=== STAGE 2: Modernizing code sequentially "
        "(Direct Service Architecture) ==="
    )

    global ACCUMULATED_CHANGES_CACHE
    ACCUMULATED_CHANGES_CACHE = []

    # ================================================================
    # STEP 1: Update Maven configuration once for the selected target
    # ================================================================

    pom_ok, pom_message, pom_change = prepare_maven_target_version(
        repo_root=repo_root,
        target_version=requested_target_version
    )

    if not pom_ok:
        logger.error(
            "Unable to prepare Maven project: %s",
            pom_message
        )
        return

    logger.info(pom_message)

    if pom_change:
        pom_change["repo_path"] = repo_root
        ACCUMULATED_CHANGES_CACHE.append(pom_change)

    # ================================================================
    # STEP 2: Validate baseline project before source modernization
    # ================================================================

    baseline_ok, baseline_log = run_target_baseline_compile(
        repo_root=repo_root
    )

    if not baseline_ok:
        logger.error(
            "Baseline project compilation failed after updating "
            "the Maven target version.\n%s",
            baseline_log[-6000:]
        )
        return
    
    retriever = RagRetriever()

    # ================================================================
    # STEP 3: Modernize production source files
    # ================================================================

    for i, target_file in enumerate(ordered_files):

        # Test classes are handled later in a dedicated compatibility pass.
        normalized_target_file = target_file.replace("\\", "/")

        if normalized_target_file.startswith("src/test/"):
            logger.info(
                "Skipping test source during production modernization: %s",
                target_file
            )
            continue

        file_findings = findings_by_file.get(target_file, [])

        if not file_findings:
            continue

        logger.info(
            "⏳ Processing target module [%d/%d]: %s "
            "(%d patterns found)",
            i + 1,
            total_files,
            target_file,
            len(file_findings)
        )

        full_path = Path(repo_root) / target_file

        baseline_code_content = (
            full_path.read_text(encoding="utf-8")
            if full_path.exists()
            else ""
        )

        file_originally_changed = False
        successfully_applied_findings = []

        # Process each legacy pattern independently.
        for p_idx, finding in enumerate(file_findings):

            pattern_id = finding.get(
                "pattern_id",
                "MODERNIZE"
            )

            description = finding.get(
                "description",
                "Refactor legacy architecture."
            )

            logger.info(
                "   ↳ [Pattern %d/%d] Executing modernization for: %s",
                p_idx + 1,
                len(file_findings),
                pattern_id
            )

            # Read the latest file content because earlier pattern passes
            # may already have updated this same class.
            original_code_content = (
                full_path.read_text(encoding="utf-8")
                if full_path.exists()
                else ""
            )

            rag_context = retriever.get_migration_recipe(
                pattern_id,
                original_code_content
            )

            result = modernize_code_snippet(
                file_path=str(full_path),
                description=description,
                target_java=target_java,
                legacy_code=original_code_content,
                pattern_id=pattern_id,
                rag_context=rag_context
            )

            if result.get("status") != "success":
                logger.error(
                    "     ❌ Modernizer failed for pattern %s: %s",
                    pattern_id,
                    result.get("message")
                )
                continue

            current_code_content = (
                full_path.read_text(encoding="utf-8")
                if full_path.exists()
                else ""
            )

            if current_code_content == original_code_content:
                logger.info(
                    "     ➖ Pattern %s produced no source change.",
                    pattern_id
                )
                continue

            # Compile only production source here.
            # Test compatibility is handled after all production classes.
            compiled_ok, compile_log = run_compile_validation(
                repo_root=repo_root,
                relative_file_path=target_file
            )

            if compiled_ok:
                logger.info(
                    "     ✅ Pattern %s applied and main source compiled.",
                    pattern_id
                )

                file_originally_changed = True
                successfully_applied_findings.append(finding)
                clean_backup_files(repo_root, target_file)

            else:
                logger.warning(
                    "     ⚠️ Pattern %s failed production compilation "
                    "for %s. Rolling back only this pattern change.\n%s",
                    pattern_id,
                    target_file,
                    compile_log[-6000:]
                )

                full_path.write_text(
                    original_code_content,
                    encoding="utf-8"
                )

        # ------------------------------------------------------------
        # Record the final validated state of this production file.
        # ------------------------------------------------------------
    
        if full_path.exists():
            final_file_content = full_path.read_text(
                encoding="utf-8"
            )

            if (
                file_originally_changed
                and final_file_content != baseline_code_content
            ):
                logger.info(
                    "✅ Production module modernization successful: %s",
                    target_file
                )

                pattern_ids = [
                    finding.get("pattern_id", "UNKNOWN")
                    for finding in successfully_applied_findings
                ]

                severity_summary = {
                    "HIGH": 0,
                    "MEDIUM": 0,
                    "LOW": 0
                }

                for finding in successfully_applied_findings:
                    severity = finding.get("severity", "LOW")
                    severity_summary[severity] = (
                        severity_summary.get(severity, 0) + 1
                    )

                ACCUMULATED_CHANGES_CACHE.append({
                    "file_path": target_file,
                    "file": target_file,
                    "modernised_code": final_file_content,
                    "repo_path": repo_root,
                    "target_version": requested_target_version,
                    "pattern_ids": pattern_ids,
                    "severity_summary": severity_summary,
                    "findings": successfully_applied_findings,
                    "explanation": (
                        f"Modernized {len(pattern_ids)} legacy pattern(s): "
                        f"{', '.join(pattern_ids)}."
                    )
                })

            else:
                logger.info(
                    "➖ Workspace unchanged for %s. "
                    "No production edits were retained.",
                    target_file
                )

    # ================================================================
    # STEP 4: Compile tests against modernized production classes
    # ================================================================

    logger.info(
        "=== TEST COMPATIBILITY VALIDATION ==="
    )

    test_compile_ok, test_compile_log = (
        run_test_compile_validation(repo_root)
    )

    if not test_compile_ok:
        logger.warning(
            "Test compilation failed after production modernization. "
            "Attempting to update affected test classes."
        )

        failing_test_files = extract_failing_test_files(
            repo_root=repo_root,
            compile_log=test_compile_log
        )

        if not failing_test_files:
            logger.error(
                "Test compilation failed, but no failing test files "
                "could be identified.\n%s",
                test_compile_log[-8000:]
            )
            return

        logger.info(
            "Failing test files detected: %s",
            ", ".join(failing_test_files)
        )

        # Build context only from production files that were actually
        # changed. This avoids sending the entire repository to Gemini.
        production_context_sections = []

        for change in ACCUMULATED_CHANGES_CACHE:
            production_file = str(
                change.get("file_path", "")
            ).replace("\\", "/")

            if not production_file.startswith("src/main/java/"):
                continue

            production_code = change.get(
                "modernised_code",
                ""
            )

            if not production_code:
                production_path = Path(repo_root) / production_file

                if production_path.exists():
                    production_code = production_path.read_text(
                        encoding="utf-8",
                        errors="replace"
                    )

            production_context_sections.append(
                f"FILE: {production_file}\n"
                f"```java\n"
                f"{production_code}\n"
                f"```"
            )

        production_context = "\n\n".join(
            production_context_sections
        )

        # ------------------------------------------------------------
        # Modernize each test class that failed compilation.
        # ------------------------------------------------------------

        for test_file in failing_test_files:

            test_path = Path(repo_root) / test_file

            if not test_path.exists():
                logger.error(
                    "Failing test file does not exist: %s",
                    test_file
                )
                return

            original_test_code = test_path.read_text(
                encoding="utf-8",
                errors="replace"
            )

            test_rag_context = (
                "The production source code has already been modernized.\n"
                "Update this test class to compile against the current "
                "production API while preserving the original test intent, "
                "assertions, and coverage.\n\n"
                "Do not modify production source code.\n"
                "Do not remove tests merely to make compilation pass.\n\n"
                "MAVEN TEST-COMPILATION ERRORS:\n"
                f"{test_compile_log[-8000:]}\n\n"
                "CURRENT MODERNIZED PRODUCTION SOURCE:\n"
                f"{production_context}"
            )

            logger.info(
                "Updating test compatibility for: %s",
                test_file
            )

            test_result = modernize_code_snippet(
                file_path=str(test_path),
                description=(
                    "Update this test class to use the current modernized "
                    "production API. Preserve its original assertions and "
                    "behavior. Do not change production source code."
                ),
                target_java=target_java,
                legacy_code=original_test_code,
                pattern_id="TEST_COMPATIBILITY",
                rag_context=test_rag_context
            )

            if test_result.get("status") != "success":
                logger.error(
                    "Unable to modernize test class %s: %s",
                    test_file,
                    test_result.get("message")
                )
                return

            modernized_test_code = test_path.read_text(
                encoding="utf-8",
                errors="replace"
            )

            if modernized_test_code == original_test_code:
                logger.error(
                    "Test modernization produced no change for %s.",
                    test_file
                )
                return

            ACCUMULATED_CHANGES_CACHE.append({
                "file_path": test_file,
                "file": test_file,
                "modernised_code": modernized_test_code,
                "repo_path": repo_root,
                "target_version": requested_target_version,
                "pattern_ids": ["TEST_COMPATIBILITY"],
                "severity_summary": {
                    "HIGH": 1,
                    "MEDIUM": 0,
                    "LOW": 0
                },
                "findings": [{
                    "pattern_id": "TEST_COMPATIBILITY",
                    "severity": "HIGH",
                    "description": (
                        "Test source was incompatible with the "
                        "modernized production API."
                    ),
                    "target_java": target_java,
                    "line_numbers": []
                }],
                "explanation": (
                    "Updated the test class to compile against the "
                    "modernized production API while preserving test intent."
                )
            })

        # Validate all updated test classes together.
        test_compile_ok, test_compile_log = (
            run_test_compile_validation(repo_root)
        )

        if not test_compile_ok:
            logger.warning(
                "Initial test modernization still has compilation errors. "
                "Running one corrective test pass.\n%s",
                test_compile_log[-6000:]
            )

            for test_file in failing_test_files:
                test_path = Path(repo_root) / test_file

                if not test_path.exists():
                    continue

                current_test_code = test_path.read_text(
                    encoding="utf-8",
                    errors="replace"
                )

                correction_context = (
                    "The previous test compatibility modernization did not compile.\n"
                    "Correct only the test class using the compiler error below.\n"
                    "Preserve all assertions and test intent.\n"
                    "Do not modify production source code.\n\n"
                    "COMPILER ERROR:\n"
                    f"{test_compile_log[-6000:]}\n\n"
                    "IMPORTANT TYPE RULE:\n"
                    "CompletableFuture completion callbacks receive Throwable, "
                    "not Exception. Use Throwable where required.\n\n"
                    "CURRENT MODERNIZED PRODUCTION SOURCE:\n"
                    f"{production_context}"
                )

                correction_result = modernize_code_snippet(
                    file_path=str(test_path),
                    description=(
                        "Correct this modernized test so it compiles against "
                        "the current production API. Fix the exact compiler "
                        "errors without removing tests or assertions."
                    ),
                    target_java=target_java,
                    legacy_code=current_test_code,
                    pattern_id="TEST_COMPATIBILITY_FIX",
                    rag_context=correction_context
                )

                if correction_result.get("status") != "success":
                    logger.error(
                        "Corrective test modernization failed for %s: %s",
                        test_file,
                        correction_result.get("message")
                    )
                    return

            test_compile_ok, test_compile_log = (
                run_test_compile_validation(repo_root)
            )

            if not test_compile_ok:
                logger.error(
                    "Test classes still fail after the corrective pass.\n%s",
                    test_compile_log[-8000:]
                )
                return

        logger.info(
            "✅ Test compatibility modernization completed successfully."
        )

    else:
        logger.info(
            "✅ Existing tests compile against the modernized production API."
        )

    # ================================================================
    # STEP 5: Comprehensive project validation gates
    # ================================================================

    if not ACCUMULATED_CHANGES_CACHE:
        logger.warning(
            "No functional code adjustments cleared validation. "
            "Skipping PR submission."
        )
        return

    logger.info(
        "Captured %d validated file change(s). "
        "Advancing to global project validation.",
        len(ACCUMULATED_CHANGES_CACHE)
    )

    build_ok, build_log = run_global_project_build(repo_root)

    if not build_ok:
        logger.error(
            "🛑 CRITICAL BUILD BLOCKER: "
            "Global Maven build failed.\n%s",
            build_log[-8000:]
        )
        return
        
    boot_ok, boot_log = run_springboot_boot_check(repo_root)

    if not boot_ok:
        logger.error(
            "🛑 CRITICAL RUNTIME BLOCKER: "
            "Application failed to boot cleanly.\n%s",
            boot_log
        )
        return

    # ================================================================
    # STATIC ANALYSIS VALIDATION GATE
    # ================================================================

    static_ok, static_log, static_tool = (
        run_static_analysis_validation(repo_root)
    )

    if not static_ok:
        logger.error(
            "🛑 STATIC ANALYSIS BLOCKER: %s validation failed.\n%s",
            static_tool,
            static_log[-8000:]
        )
        return

    if static_tool == "SKIPPED":
        logger.info(
            "ℹ️ Static analysis skipped because the project does not "
            "configure Checkstyle or PMD."
        )
    else:
        logger.info(
            "✅ Static analysis passed using %s.",
            static_tool
        )

    logger.info(
        "🎉 All validation gates passed. "
        "Pushing validated updates to GitHub."
    )

    # ================================================================
    # STEP 6: Create GitHub branch and Pull Request
    # ================================================================

    logger.info(
        "🎉 All validation gates passed. "
        "Pushing validated updates to GitHub."
    )

    pr_result = create_pull_request(
        repo_owner=os.environ.get("GITHUB_OWNER", ""),
        repo_name=os.environ.get("GITHUB_REPO", ""),
        base_branch="main",
        changes=ACCUMULATED_CHANGES_CACHE
    )

    if pr_result.get("status") == "success":
        logger.info(
            "✨ Modernization workflow successful. "
            "Pull Request: %s",
            pr_result.get("pr_url")
        )
    else:
        logger.error(
            "🛑 Modernization completed, but GitHub submission failed: %s",
            pr_result.get("message", "Unknown GitHub error")
        )

if __name__ == "__main__":
    import asyncio
    import sys

    # --- Keep Existing Environment Configurations Intact ---
    os.environ["GITHUB_TOKEN"] = ""
    os.environ["GITHUB_OWNER"] = "labgtm-ai"
    os.environ["GITHUB_REPO"]  = "java-legacy-enterprise-app"
    
    os.environ["GCP_PROJECT_ID"] = "deutschebank-aipocs"
    os.environ["GOOGLE_CLOUD_PROJECT"] = "deutschebank-aipocs"
    os.environ["GCP_LOCATION"] = "us-central1"
    os.environ["SRAO_BATCH_SIZE"] = "1"

    PROJECT_ID = "deutschebank-aipocs"
    LOCATION = "us-central1"
    GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]

    print("====================================================")
    print("      SRAO Java Modernization Agent Pipeline        ")
    print("====================================================\n")

    # --- Interactive Input Block ---
    # 1. Ask the user for the full Repo URL
    target_repo = input("Enter GitHub Repository URL or Local Directory Path: ").strip()
    if not target_repo:
        print("❌ Error: Repository URL or path cannot be empty.")
        sys.exit(1)

    # 2. Ask the user for the Branch Name (defaults to 'main')
    target_branch = input("Enter Target Branch Name [default: main]: ").strip()
    if not target_branch:
        target_branch = "main"

    # 3. New: Ask the user for the target Java specification version
    target_version_input = input("Enter Target Java Version (e.g., 8, 11, 14, 17, 21) [default: 21]: ").strip()
    if not target_version_input:
        target_java_version = 21
    else:
        try:
            target_java_version = int(target_version_input)
        except ValueError:
            print("⚠️ Invalid integer format. Defaulting baseline profile to Java 21.")
            target_java_version = 21

    # --- Automatically Extract Slug Context details from the input URL ---
        # --- Automatically Extract Slug Context details from the input URL ---
    # Extract GitHub owner and repository from the entered URL.
    if "github.com" in target_repo:

        clean_url = (
            target_repo
            .replace("https://", "")
            .replace("http://", "")
            .replace("git@github.com:", "")
            .rstrip("/")
        )

        if clean_url.endswith(".git"):
            clean_url = clean_url[:-4]

        # clean_url is now:
        # github.com/owner/repository
        # or owner/repository for SSH-style inputs
        if clean_url.startswith("github.com/"):
            clean_url = clean_url[len("github.com/"):]

        slug_parts = clean_url.split("/")

        if len(slug_parts) >= 2:
            os.environ["GITHUB_OWNER"] = slug_parts[0]
            os.environ["GITHUB_REPO"] = slug_parts[1]
        else:
            print(
                "❌ Unable to determine GitHub owner and repository "
                f"from: {target_repo}"
            )
            sys.exit(1)

    print("\n🚀 Initializing multi-agent migration environment sequence loop workflow.")
    print(f"   - Target Repo:      {target_repo}")
    print(f"   - Target Branch:    {target_branch}")
    print(f"   - Target Version:   Java {target_java_version}")
    print(f"   - Owner/Org:        {os.environ['GITHUB_OWNER']}")
    print(f"   - Repository:       {os.environ['GITHUB_REPO']}\n")

    # --- Core Business Logic Processing with Version Context ---
    logger.info("Initializing multi-agent migration environment sequence loop workflow.")
    try:
        # Added target_java_version parameter pass to stage1 analyzer processing hook
        analysis_results = stage1_scan_and_analyse(target_repo, target_branch, target_java_version)
        if "error" in analysis_results: 
            sys.exit(analysis_results["error"])
            
        runner = InMemoryRunner(agent=srao_agent, app_name=APP_NAME)
        asyncio.run(runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID))
        stage2_process_batches(analysis_results, runner)
    except Exception as e:
        logger.exception("❌ CRITICAL PIPELINE CRASH:")
        sys.exit(1)

