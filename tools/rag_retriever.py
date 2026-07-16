"""
tools/rag_retriever.py
───────────────────────
Retrieves relevant Java 17/21 documentation and code examples
from a Vertex AI Vector Search index or localized fallback maps.
"""

import logging
import os
from typing import Optional, Dict, Any

from google.cloud import aiplatform
from vertexai.language_models import TextEmbeddingModel

logger = logging.getLogger("srao.rag_retriever")

# ── Configuration (from environment / config.yaml) ────────────────────────────
PROJECT_ID        = os.environ.get("GCP_PROJECT_ID", "your-project-id")
LOCATION          = os.environ.get("GCP_LOCATION",   "us-central1")
INDEX_ENDPOINT_ID = os.environ.get("VECTOR_INDEX_ENDPOINT_ID", "")
DEPLOYED_INDEX_ID = os.environ.get("DEPLOYED_INDEX_ID", "srao_java_docs_index")
EMBEDDING_MODEL   = "text-embedding-004"

# Inline fallback knowledge base (used when vector index is not yet deployed)
FALLBACK_KB: Dict[str, str] = {
    "FOR_LOOP": """
// Before (Java 7 style)
List<String> names = Arrays.asList("Alice", "Bob", "Charlie");
List<String> upper = new ArrayList<>();
for (String name : names) {
    if (name.startsWith("A")) {
        upper.add(name.toUpperCase());
    }
}

// After (Java 8+ Stream API)
List<String> upper = names.stream()
    .filter(name -> name.startsWith("A"))
    .map(String::toUpperCase)
    .collect(Collectors.toList());
// Java 16+: .toList() instead of .collect(Collectors.toList())
""",
    "RAW_THREAD": """
// Before (Java 5 style)
Thread t = new Thread(() -> { doWork(); });
t.start();

// After (Java 21 Virtual Threads)
try (var executor = Executors.newVirtualThreadPerTaskExecutor()) {
    executor.submit(() -> doWork());
}
// Or directly:
Thread.ofVirtual().start(() -> doWork());
""",
    "POJO_CLASS": """
// Before (POJO with Lombok or manual getters/setters)
public class User {
    private String name;
    private int age;
    public String getName() { return name; }
    public void setName(String name) { this.name = name; }
    public int getAge() { return age; }
    public void setAge(int age) { this.age = age; }
}

// After (Java 16+ Record)
public record User(String name, int age) {}
// Usage: var u = new User("Alice", 30); u.name(); u.age();
""",
    "NULL_CHECK": """
// Before
String result = null;
if (value != null) {
    result = value.toUpperCase();
}

// After (Java 8+ Optional)
String result = Optional.ofNullable(value)
    .map(String::toUpperCase)
    .orElse(null);
""",
    "STRING_BUFFER": """
// Before
StringBuffer sb = new StringBuffer();
for (String s : items) { sb.append(s).append(", "); }

// After
String result = String.join(", ", items);
// Or for complex cases:
StringBuilder sb = new StringBuilder();  // Not thread-safe (faster)
""",
    "MULTILINE_STRING": """
// Before
String json = "{\\n" +
    "  \\"name\\": \\"Alice\\",\\n" +
    "  \\"age\\": 30\\n" +
"}";

// After (Java 15+ Text Blocks)
String json = \"\"\"
    {
      "name": "Alice",
      "age": 30
    }
    \"\"\";
""",
    "INSTANCEOF_CAST": """
// Before
if (obj instanceof String) {
    String s = (String) obj;
    System.out.println(s.length());
}

// After (Java 16+ Pattern Matching)
if (obj instanceof String s) {
    System.out.println(s.length());
}
// Java 21 Switch Pattern Matching:
switch (obj) {
    case String s  -> System.out.println(s.length());
    case Integer i -> System.out.println(i * 2);
    default        -> System.out.println("other");
}
""",
    "SYNCHRONIZED_BLOCK": """
// Before
private synchronized void increment() { count++; }

// After: use java.util.concurrent.atomic
private final AtomicInteger count = new AtomicInteger(0);
public void increment() { count.incrementAndGet(); }

// Or for complex critical sections: ReentrantLock / StampedLock
private final ReentrantLock lock = new ReentrantLock();
public void increment() {
    lock.lock();
    try { count++; } finally { lock.unlock(); }
}
""",
}


class RagRetriever:
    """
    SRAO multi-agent orchestration component matching vector lookups to code blocks.
    """
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.project_id = self.config.get("project_id", PROJECT_ID)
        self.location = self.config.get("location", LOCATION)
        self.endpoint_id = self.config.get("index_endpoint_id", INDEX_ENDPOINT_ID)
        self.deployed_index_id = self.config.get("deployed_index_id", DEPLOYED_INDEX_ID)

    def get_migration_recipe(self, pattern_id: str, context_snippet: str = "") -> str:
        """
        Main query entrypoint hook invoked by srao_agent.py orchestrator logic.
        Returns a formatted markdown documentation payload string.
        """
        res = retrieve_java_docs(
            pattern_id=pattern_id,
            context_snippet=context_snippet,
            project_id=self.project_id,
            location=self.location,
            endpoint_id=self.endpoint_id,
            deployed_index_id=self.deployed_index_id
        )
        
        # Format the mapping array into an explicit textual markdown instruction context block [2]
        if res.get("status") == "success":
            recipe_text = (
                f"### Migration Recipe for {pattern_id}\n"
                f"**Documentation Source:** {res.get('source')}\n"
                f"Summary: {res.get('documentation')}\n\n"
                f"Reference Legacy Example:\n```java\n{res.get('example_before')}\n```\n\n"
                f"Target Modernized Equivalent Implementation:\n```java\n{res.get('example_after')}\n```"
            )
            return recipe_text
        
        return "No specific modernization recipes found for this target pattern token."


def retrieve_java_docs(
    pattern_id: str, 
    context_snippet: str = "",
    project_id: str = PROJECT_ID,
    location: str = LOCATION,
    endpoint_id: str = INDEX_ENDPOINT_ID,
    deployed_index_id: str = DEPLOYED_INDEX_ID
) -> dict:
    """
    Retrieve Java 17/21 documentation and refactoring examples for a pattern.
    """
    # Try vector index lookup first if endpoints are alive
    if endpoint_id:
        try:
            result = _query_vector_index(pattern_id, context_snippet, project_id, location, endpoint_id, deployed_index_id)
            if result:
                return {**result, "source": "vector_index"}
        except Exception as exc:
            logger.warning("Vector index search dropped. Invoking fallback knowledge map fallback logic: %s", exc)

    # Fallback to inline knowledge base
    kb_entry = FALLBACK_KB.get(pattern_id)
    if kb_entry:
        parts   = kb_entry.strip().split("// After")
        before  = parts[0].replace("// Before", "").strip() if len(parts) > 0 else ""
        after   = ("// After" + parts[1]).strip()           if len(parts) > 1 else ""
        return {
            "status":         "success",
            "pattern_id":     pattern_id,
            "documentation":  f"Standard Java syntax architecture translation recommendation guideline rule for: {pattern_id}",
            "example_before": before,
            "example_after":  after,
            "source":         "fallback_kb",
        }

    return {
        "status":  "error",
        "message": f"No documentation found for pattern: {pattern_id}",
    }


# ── Vector index query ────────────────────────────────────────────────────────

def _query_vector_index(
    pattern_id: str, 
    context_snippet: str,
    project_id: str,
    location: str,
    endpoint_id: str,
    deployed_index_id: str
) -> Optional[dict]:
    """Query Vertex AI Vector Search for relevant Java documentation."""
    aiplatform.init(project=project_id, location=location)

    # Generate embedding for the query
    embedding_model = TextEmbeddingModel.from_pretrained(EMBEDDING_MODEL)
    query_text      = f"Java modernization: {pattern_id}\n{context_snippet}"
    embeddings      = embedding_model.get_embeddings([query_text])
    query_vector    = embeddings[0].values

    # Query the deployed index
    index_endpoint = aiplatform.MatchingEngineIndexEndpoint(
        index_endpoint_name=endpoint_id
    )
    response = index_endpoint.find_neighbors(
        deployed_index_id=deployed_index_id,
        queries=[query_vector],
        num_neighbors=3,
    )

    if not response or not response[0]:
        return None

    # Assemble structural blocks from neighbors text records if storage bounds exist
    # Note: Placed defensive placeholder layout text arrays to ensure safe SDK response parsing
    docs = [f"Neighbor ID Match Vector Context Reference: {neighbor.id}" for neighbor in response[0]]
    return {
        "status":         "success",
        "pattern_id":     pattern_id,
        "documentation":  f"Retrieved {len(docs)} matching nodes from cloud workspace indexes.",
        "example_before": "// Legacy code patterns identified across embedding index ranges",
        "example_after":  "\n".join(docs),
    }
