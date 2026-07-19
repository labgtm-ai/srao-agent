## SRAO End-to-End Modernization Workflow

```text
                 User Inputs
                      │
                      ▼
        Repository URL + Target Java Version
                      │
                      ▼
            Stage 1 - Repository Scan
                      │
        Clone Repository / Local Repository
                      │
                      ▼
        Discover All Java Source Files
                      │
                      ▼
          AST Analysis - Legacy Pattern Detection
                      │
                      ▼
      Prioritize Files by Severity - HIGH → LOW
                      │
                      ▼
      Stage 2 - AI Modernization Pipeline
                      │
      Update pom.xml to Target Java Version
                      │
                      ▼
      Baseline Maven Compilation Validation
                      │
                      ▼
             For Each Java File
                      │
            ├── Retrieve detected patterns
            ├── Retrieve RAG migration recipe
            ├── Invoke Gemini
            ├── Modernize complete source file
            ├── Run incremental Maven compile
            └── Retain only successful changes
                      │
                      ▼
          Test Compatibility Validation
                      │
            ├── Run Maven testCompile
            ├── Detect incompatible test classes
            ├── AI modernizes affected tests
            └── Re-run testCompile
                      │
                      ▼
          Global Project Validation
                      │
            ├── Run global Maven build
            ├── Validate Spring Boot startup
            ├── Run PMD or Checkstyle
            └── Generate validation summary
                      │
                      ▼
                Git Operations
                      │
            ├── Create feature branch
            ├── Commit validated changes
            ├── Push feature branch
            └── Create GitHub Pull Request
                      │
                      ▼
         JSON and Markdown Report Generation
```

---

## AI-Assisted CI Validation Pipeline

```text
Repository Scan
        │
        ▼
AST Analysis
        │
        ▼
RAG Retrieval
        │
        ▼
AI Refactoring
        │
        ▼
Incremental Compilation
        │
        ▼
Test Compatibility Validation
        │
        ▼
Global Maven Build
        │
        ▼
Spring Boot Startup Validation
        │
        ▼
PMD / Checkstyle
        │
        ▼
Git Feature Branch
        │
        ▼
GitHub Pull Request
        │
        ▼
JSON and Markdown Reports
```

The SRAO Agent performs an AI-assisted CI validation workflow before creating a pull request. It analyzes, modernizes, compiles, validates, and statically analyzes the application. Deployment is not currently performed by the agent.

----------------------------------------------------------------------------
Developer Notes:

## Demo Repositories

| Repository | Description | Approximate Execution Time |
|------------|-------------|---------------------------:|
| **Enterprise Legacy App** | Large enterprise Spring Boot application with multiple modules and legacy Java patterns | **20–25 minutes** |
| **Order Processor Service** | Medium-sized Spring Boot microservice used to demonstrate AI-assisted modernization | **~10 minutes** |
| **Order Service Legacy Demo** | Lightweight demo project with a limited number of Java files and common legacy patterns | **~5 minutes** |

> **Note:** The execution time of the demo project can be further reduced by temporarily disabling a few low-priority AST detection patterns in `tools/ast_analyzer.py` during demonstrations.

---

## Running the SRAO Agent

### Prerequisites

1. Configure the GitHub Personal Access Token in `main_pipeline.py` under the `if __name__ == "__main__":` section.
2. Ensure the required Python dependencies are installed.
3. Authenticate to Google Cloud and Vertex AI.

---

### Clean Previous Workspace

```bash
rm -rf /tmp/srao_repo_*
find . -type d -name "__pycache__" -exec rm -rf {} +
```

---

### Execute the Agent

```bash
python main_pipeline.py
```

The agent will automatically perform the following steps:

1. Clone the target repository.
2. Analyze legacy Java patterns using the AST Analyzer.
3. Retrieve modernization guidance using RAG.
4. Modernize the code using Gemini.
5. Perform incremental Maven compilation.
6. Validate test compatibility.
7. Execute the global Maven build.
8. Validate Spring Boot application startup.
9. Run PMD / Checkstyle (if configured).
10. Create a GitHub feature branch and Pull Request.
11. Generate JSON and Markdown modernization reports.