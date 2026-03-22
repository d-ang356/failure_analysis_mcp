# CLAUDE.md - Failure Analysis Server

## Project Context

This is an **MCP (Model Context Protocol) server** built in Python that analyzes WebdriverIO test failures using local LLMs via Ollama. The server provides intelligent failure analysis by extracting code context and using LLMs to identify root causes and suggest fixes.

**Key Integration Point**: This server connects to a **WebdriverIO JavaScript MCP client** on the frontend side. The configuration for this connection is managed via environment variables in `.env`.

## Architecture

### Core Components

```
mcp_server.py
├── Config                    # Environment-based configuration
├── SessionManager           # TTL-based session management
├── ImportExtractor          # Tree-sitter based JS/TS import extraction
├── CodeAnalyzer             # BFS traversal of code dependencies
├── OllamaClient            # Async LLM client
└── MCP Tools/Resources     # analyze_failure, clear_session, etc.
```

### Data Flow

1. **WebdriverIO Test** fails and captures console output
2. **MCP Client (JS)** sends `analyze_failure` call via stdio
3. **MCP Server** parses error details from console output
4. **ImportExtractor** traverses imports (3 levels deep by default)
5. **CodeAnalyzer** gathers relevant code files
6. **OllamaClient** sends prompt to local LLM
7. **Analysis** is returned to client with fix suggestions

## Key Configuration

The `.env` file controls:

```bash
# WebdriverIO client connection
MCP_CLIENT_URL=http://localhost:3000
MCP_CLIENT_PORT=3000

# LLM configuration
MCP_OLLAMA_HOST=http://localhost:11434
MCP_OLLAMA_MODEL=qwen2.5-coder:7b

# Analysis depth
MCP_MAX_IMPORT_DEPTH=3
```

## MCP Protocol

### Tools

**`analyze_failure`** (primary tool)
- Input: `console_output`, `spec_file_path`, `session_id` (optional)
- Output: JSON with `analysis`, `session_id`, `files_analyzed`
- Behavior: Extracts code, sends to LLM, maintains session context

**`clear_session`**
- Input: `session_id`
- Output: JSON confirmation
- Behavior: Removes session from memory

### Resources

**`config://current`**
- Returns: Markdown formatted server configuration
- Use for: Debugging configuration issues

**`session://{session_id}/status`**
- Returns: Session metadata and failure history
- Use for: Checking analysis context

## Code Analysis Strategy

### Import Extraction (ImportExtractor)

Uses **tree-sitter** for robust parsing with regex fallback:

```python
# ES6 imports
import { foo } from './bar'
import * as utils from '../utils'

# CommonJS requires
const baz = require('./baz')
```

Resolution priority:
1. Exact path match
2. Path + extension (.ts, .tsx, .js, .jsx)
3. Directory + index file

### BFS Traversal (CodeAnalyzer)

Starting from spec file:
- Depth 0: Spec file itself
- Depth 1: Direct imports (page objects)
- Depth 2: Page object imports (components)
- Depth 3: Component imports (utils)

Files are filtered by:
- Size (MCP_MAX_FILE_SIZE_KB)
- Existence check
- Duplicate prevention

### Error Extraction

Stack trace parsing recognizes patterns:
```
at FunctionName (/path/to/file.ts:10:5)
Error in /path/to/file.ts:10
file.ts:10:5
```

Error types are categorized:
- Element Not Found
- Element Not Interactable
- Timeout
- Stale Element Reference
- Assertion Failed
- Selector Error

## LLM Integration

### System Prompt Strategy

The `SYSTEM_PROMPT` is designed to make the LLM:
1. Focus on ERROR DETAILS first (not just code context)
2. TRACE through stack traces to root cause
3. EXAMINE ALL provided files (spec + imports)
4. FIND bugs in framework code (not just test data)
5. PROPOSE specific fixes with file/line numbers

### Common Bug Patterns Emphasized

The prompt specifically instructs the LLM to look for:
- String concatenation bugs: `addValue(value+"1")`
- Hardcoded values in switch/case
- Wrong variable passing: `lastName` instead of `firstName`
- Missing awaits in async operations
- Selector mismatches

### Token Management

Estimated tokens = (prompt length + history) / 4

Warning if > 80% of context window.
Timeout: MCP_OLLAMA_TIMEOUT seconds.

## Session Management

Sessions provide **context across multiple analyses**:

```python
Session:
- session_id: UUID
- created_at: timestamp
- last_accessed: timestamp (for TTL)
- failures: List[FailureContext] (last 10)
- conversation_history: List[dict] (last 20 messages)
```

TTL cleanup happens every 100 session accesses.

## Development Patterns

### Adding a New Tool

```python
@mcp.tool()
async def my_tool(
    param: str = Field(description="Parameter description"),
) -> str:
    """Tool description for MCP clients."""
    try:
        # Implementation
        return json.dumps({"success": True, "result": ...})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})
```

### Adding a Resource

```python
@mcp.resource("resource://{id}/type")
def my_resource(id: str) -> str:
    """Resource description."""
    return "markdown or json content"
```

### Configuration Access

Always use `get_config()` singleton:

```python
config = get_config()
host = config.ollama_host
```

## Testing

### Manual Testing with MCP Inspector

```bash
mcp dev mcp_server.py
```

This launches the MCP Inspector UI for interactive testing.

### Testing Ollama Connection

```python
from mcp_server import OllamaClient
client = OllamaClient()
await client.check_connection()  # Returns True/False
```

### Testing Import Extraction

```python
from mcp_server import ImportExtractor
extractor = ImportExtractor()
imports = extractor.extract_imports(Path("./test.ts"))
```

## Common Issues

### "Model not found"
- Ensure model is pulled: `ollama pull qwen2.5-coder:7b`
- Check `MCP_OLLAMA_MODEL` matches available models

### "Cannot connect to Ollama"
- Verify Ollama is running: `ollama serve`
- Check `MCP_OLLAMA_HOST` URL format (http://)

### "No imports found"
- Check file extensions (.ts, .tsx, .js, .jsx)
- Verify imports are relative (./ or ../)
- Increase `MCP_MAX_IMPORT_DEPTH`

### Session lost
- Sessions expire after `MCP_SESSION_TTL_MINUTES`
- Check session hasn't been cleared
- Reuse `session_id` from previous response

## Dependencies

Core:
- `mcp[cli]` - MCP protocol and CLI tools
- `ollama` - Async Ollama client
- `pydantic` - Data validation
- `python-dotenv` - Environment loading

Parsing:
- `tree-sitter` - Core parser
- `tree-sitter-javascript` - JS grammar
- `tree-sitter-typescript` - TS/TSX grammar

Dev:
- `pytest` - Testing
- `pytest-asyncio` - Async test support

## Performance Considerations

### Import Traversal
- Limit depth to prevent exponential growth
- Size filtering prevents large files
- Duplicate detection prevents cycles

### LLM Calls
- Timeout prevents hanging
- Token estimation prevents context overflow
- Session history truncation keeps context manageable

### Session Cleanup
- Expired sessions auto-remove
- Cleanup every 100 accesses (not every call)
- Memory usage scales with active sessions

## Security Notes

- All file access is read-only
- No external network calls except to Ollama
- Session IDs are UUIDs (unguessable)
- No sensitive data in logs (paths are logged, content is not)

## Future Enhancements

Potential improvements:
- Add caching for analyzed files
- Support for additional test frameworks (Playwright, Cypress)
- Vector DB for similar failure lookup
- Multi-model support (fallback if primary fails)
- Batch analysis optimization for CI/CD

## Claude-Specific Context

When working on this project:

1. **Test changes** with `mcp dev mcp_server.py` when possible
2. **Respect .env** - don't hardcode configuration
3. **Keep imports minimal** - tree-sitter is already heavy
4. **Error handling** - return JSON with success: false on errors
5. **Type hints** - use Python 3.11+ features (|, etc.)
6. **Async** - the server is fully async, use await for Ollama calls
7. **Logging** - use logger, not print statements
8. **Session safety** - always validate session_id exists before access

## References

- MCP Protocol: https://modelcontextprotocol.io/
- Ollama Python: https://github.com/ollama/ollama-python
- Tree-sitter: https://tree-sitter.github.io/tree-sitter/
- WebdriverIO: https://webdriver.io/
- UV: https://docs.astral.sh/uv/
