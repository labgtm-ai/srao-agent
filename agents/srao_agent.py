"""
AI-Powered Service Refactoring and Optimization (SRAO) Agent
============================================================
Main orchestrator agent built on Google Vertex AI Agent Development Kit (ADK).
Modernises Java codebases from Java 8 → Java 17/21 using:
  - AST-based pattern detection
  - RAG-backed knowledge retrieval (Java 17/21 docs)
  - Gemini LLM for code generation
  - GitHub PR auto-creation
"""

from google.adk.agents import Agent
from google.adk.tools import FunctionTool

from tools.repo_scanner      import scan_repository, list_java_files
from tools.ast_analyzer      import analyze_java_file, classify_severity
from tools.rag_retriever     import retrieve_java_docs
from tools.code_modernizer   import modernize_code_snippet
from tools.pr_creator        import create_pull_request, validate_diff
from tools.report_generator  import generate_report

# ── Agent system prompt ───────────────────────────────────────────────────────

SYSTEM_PROMPT = """
You are SRAO (Service Refactoring and Optimization Agent), an expert Java modernization
AI agent running on Google Cloud Vertex AI.

Your mission is to:
1. SCAN a Java Git repository and identify all .java source files
2. ANALYSE each file for legacy patterns using AST analysis:
   - Raw for-loops that can become Stream API
   - Thread/Runnable/synchronized blocks → Virtual Threads (Java 21)
   - POJO/Lombok classes → Record classes (Java 16+)
   - Blocking operations → CompletableFuture / reactive (Java 8+)
   - Explicit null checks → Optional<T> (Java 8+)
   - StringBuffer / concatenation → Text Blocks (Java 15+)
   - Raw types / unchecked casts → Generics + Pattern Matching (Java 16+)
3. RETRIEVE relevant Java 17/21 documentation and examples via RAG
4. MODERNISE each identified pattern into idiomatic Java 17/21 code
5. VALIDATE the refactored code (compile check, lint)
6. DELIVER the changes as a GitHub Pull Request with explanations

Severity Classification:
  - HIGH   : Threading, blocking I/O, critical performance patterns
  - MEDIUM : Data modelling, null safety, type safety
  - LOW    : Code verbosity, string operations, minor style

Always explain WHY each change improves the code.
Process files in order of severity: HIGH → MEDIUM → LOW.
If CI validation fails, retry with an improved fix (max 3 iterations).
"""

# ── Tool registration ─────────────────────────────────────────────────────────

tools = [
    FunctionTool(scan_repository),
    FunctionTool(list_java_files),
    FunctionTool(analyze_java_file),
    FunctionTool(classify_severity),
    FunctionTool(retrieve_java_docs),
    FunctionTool(modernize_code_snippet),
    FunctionTool(validate_diff),
    FunctionTool(create_pull_request),
    FunctionTool(generate_report),
]

# ── Agent definition ──────────────────────────────────────────────────────────

srao_agent = Agent(
    model="gemini-2.0-flash-001",          # Vertex AI Gemini model
    name="srao_agent",
    description="Java codebase modernization agent — scans, analyses, refactors and delivers PRs",
    instruction=SYSTEM_PROMPT,
    tools=tools,
)
