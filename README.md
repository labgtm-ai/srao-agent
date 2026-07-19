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