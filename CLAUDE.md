# CLAUDE.md - Failure Analysis Server

## Project Context

This is an **MCP (Model Context Protocol) server** built in Python that analyzes WebdriverIO test failures using local LLMs via Ollama. The server provides intelligent failure analysis by extracting code context, analyzing screenshots, and cross-referencing DOM snapshots to identify root causes and suggest fixes.

**Key Integration Point**: This server connects to a **WebdriverIO JavaScript MCP client** on the frontend side. The configuration for this connection is managed via environment variables in `.env`.

**Vision Support**: The server supports vision-capable models (e.g., `gemma4:e4b`, `qwen3-vl:8b`) that can analyze screenshots of the page at failure time. The default model is `gemma4:e4b`.

## Architecture

### Core Components

```
mcp_server.py
├── Config                    # Environment-based configuration
├── SessionManager           # TTL-based session management
├── ImportExtractor          # Tree-sitter based JS/TS import extraction
├── CodeAnalyzer             # BFS traversal of code dependencies
├── clean_dom_snapshot       # DOM cleaning and truncation for LLM consumption
├── OllamaClient            # Async LLM client with vision support
└── MCP Tools/Resources     # analyze_failure, clear_session, etc.
```

### Data Flow

1. **WebdriverIO Test** fails and captures console output, screenshot, and DOM snapshot
2. **MCP Client (JS)** sends `analyze_failure` call via stdio (including base64 screenshot + DOM)
3. **MCP Server** parses error details from console output
4. **ImportExtractor** traverses imports (3 levels deep by default)
5. **CodeAnalyzer** gathers relevant code files
6. **OllamaClient** sends prompt + screenshot image to local LLM
7. **Analysis** is returned to client with fix suggestions, incorporating visual and DOM context

## Key Configuration

The `.env` file controls:

```bash
# WebdriverIO client connection
MCP_CLIENT_URL=http://localhost:3000
MCP_CLIENT_PORT=3000

# LLM configuration
MCP_OLLAMA_HOST=http://localhost:11434
MCP_OLLAMA_MODEL=gemma4:e4b
MCP_OLLAMA_TEMPERATURE=0.1
MCP_OLLAMA_NUM_CTX=131072
MCP_OLLAMA_TIMEOUT=300

# Analysis depth
MCP_MAX_IMPORT_DEPTH=3
MCP_MAX_DOM_SIZE_KB=100
```

## MCP Protocol

### Tools

**`analyze_failure`** (primary tool)
- Input: `console_output`, `spec_file_path`, `session_id` (optional), `screenshot_base64` (optional), `dom_snapshot` (optional), `screenshot_mime_type` (optional, default `image/png`)
- Output: JSON with `analysis`, `session_id`, `files_analyzed`
- Behavior: Extracts code, sends prompt + optional screenshot image to LLM, includes cleaned DOM in prompt text, maintains session context

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
- Size (`MCP_MAX_FILE_SIZE_KB`)
- Existence check
- Duplicate prevention

### DOM Snapshot Processing (`clean_dom_snapshot`)

When a `dom_snapshot` is provided by the client:
1. Removes `<script>`, `<style>`, and stylesheet `<link>` tags
2. Strips HTML comments
3. Removes `data-*` attributes except `data-testid` and `data-id`
4. Collapses excessive whitespace
5. Truncates to `MCP_MAX_DOM_SIZE_KB` if still too large

This keeps the DOM focused on structural debugging information while minimizing token count.

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
4. ANALYZE screenshots for visual discrepancies (if provided)
5. CROSS-REFERENCE DOM with selectors from code (if provided)
6. FIND bugs in framework code (not just test data)
7. PROPOSE specific fixes with file/line numbers

### Selector Mismatch Protocol

When the error type is "Element Not Found", "Timeout" waiting for an element, or any selector-related failure, the LLM follows a **mandatory 6-step protocol**:

1. **Identify** the failing selector from the page object code.
2. **Search** the DOM snapshot for that exact selector.
3. If not found, declare a **SELECTOR MISMATCH** — the framework selector is outdated.
4. **Find the correct replacement** in the DOM by searching for elements with the same purpose, similar `data-testid`, nearby `id`, same class, or same visual role.
5. **Confirm** with the screenshot (if provided) that the element exists visually.
6. **Propose the fix** with the exact file, line, current selector, and corrected selector.

The LLM is forbidden from suggesting test data changes, wait times, or retry logic when a selector mismatch is detected. The fix must be in the page object / framework file.

### Vision Model Support

The server uses `ollama.Message` and `ollama.Image` to pass base64-encoded screenshots to the LLM:

```python
from ollama import Message, Image
messages = [
    Message(role="system", content=SYSTEM_PROMPT),
    Message(role="user", content=prompt, images=[Image(value=screenshot_base64)]),
]
```

Only vision-capable models (e.g., `gemma4:e4b`, `qwen3-vl:8b`) can process images. Text-only models (e.g., `qwen2.5-coder:7b`) will ignore the image.

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
- Ensure model is pulled: `ollama pull gemma4:e4b`
- Check `MCP_OLLAMA_MODEL` matches available models
- For vision analysis, use a vision-capable model (`gemma4:e4b`, `qwen3-vl:8b`). Text-only models (`qwen2.5-coder:7b`) cannot analyze screenshots.

### "Cannot connect to Ollama"
- Verify Ollama is running: `ollama serve`
- Check `MCP_OLLAMA_HOST` URL format (http://)

### Screenshot not analyzed
- Verify the model supports vision: `ollama show gemma4:e4b` should list `vision` in capabilities
- Ensure `screenshot_base64` is a valid base64 string (not a file path or URL)
- Check server logs for `Including screenshot in analysis`

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
- **Client-side DOM compression** to reduce token usage further
- **Video/gif support** for animation-related failures

## Client-Side Requirements

The WebdriverIO MCP client is expected to provide the following when calling `analyze_failure`:

### Console Output Format
The client may send console output as raw text or **ndjson** (newline-delimited JSON) lines. The server parses both formats and extracts error messages, stack traces, and test names.

### Screenshot
- **Source**: `browser.takeScreenshot()` in WebdriverIO returns a base64 PNG by default.
- **Parameter**: `screenshot_base64` (string)
- **MIME type**: `screenshot_mime_type` (default `image/png`)
- **Size**: Keep screenshots under 2MB base64 (~1.5MB raw) to avoid timeouts.

### DOM Snapshot
- **Source**: `browser.execute(() => document.documentElement.outerHTML)`
- **Parameter**: `dom_snapshot` (string)
- **Cleaning**: The server cleans the DOM server-side, but the client can pre-strip scripts/styles to reduce payload size.
- **Size limit**: Server truncates to `MCP_MAX_DOM_SIZE_KB` (default 100KB).

### Example Client Call (JavaScript)
```javascript
const result = await client.callTool('analyze_failure', {
  console_output: testOutput,           // raw or ndjson
  spec_file_path: '/path/to/spec.ts',
  session_id: currentSessionId,
  screenshot_base64: await browser.takeScreenshot(),
  dom_snapshot: await browser.execute(() => document.documentElement.outerHTML),
  screenshot_mime_type: 'image/png'
});
```

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
