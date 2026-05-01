# MCP Failure Analysis Server

An MCP (Model Context Protocol) server that analyzes WebdriverIO test failures using local LLMs via Ollama. Designed for integration with JavaScript/TypeScript test automation frameworks.

## Overview

This server provides intelligent failure analysis for WebdriverIO E2E tests by:
- Extracting relevant code context from spec files and their imports
- Parsing error messages and stack traces
- Using local LLMs (via Ollama) to provide detailed root cause analysis
- Maintaining session context across multiple failure analyses

## Features

- **Intelligent Code Analysis**: Automatically traverses import dependencies (up to 3 levels deep) to gather relevant context
- **Session Management**: Maintains context across multiple analyses with TTL-based expiration
- **Tree-sitter Parsing**: Efficient JavaScript/TypeScript import extraction
- **Local LLM Integration**: Works with Ollama for privacy-preserving analysis
- **MCP Protocol**: Standard MCP server compatible with any MCP client
- **WebdriverIO Optimized**: Tailored prompts for WebdriverIO test failures

## Installation

### Prerequisites

- Python 3.11+
- [UV](https://docs.astral.sh/uv/) for package management
- [Ollama](https://ollama.ai/) with a vision-capable model (e.g., `gemma4:e4b`)

### Setup

1. Clone the repository and navigate to the project:
```bash
cd failure_analysis_server
```

2. Sync dependencies with UV:
```bash
uv sync
```

3. Create your `.env` file:
```bash
cp .env.example .env
# Edit .env with your configuration
```

4. Ensure Ollama is running with your chosen model:
```bash
ollama pull gemma4:e4b
ollama serve
```

## Configuration

All configuration is done via environment variables in `.env`:

### WebdriverIO MCP Client
| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_CLIENT_URL` | `http://localhost:3000` | URL of WebdriverIO MCP client |
| `MCP_CLIENT_PORT` | `3000` | Port for WebdriverIO MCP client |

### Ollama LLM
| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_OLLAMA_HOST` | `http://localhost:11434` | Ollama server host |
| `MCP_OLLAMA_MODEL` | `gemma4:e4b` | Model to use for analysis (vision-capable recommended) |
| `MCP_OLLAMA_TEMPERATURE` | `0.1` | Temperature (0.0-1.0) |
| `MCP_OLLAMA_NUM_CTX` | `131072` | Context window size |
| `MCP_OLLAMA_TIMEOUT` | `300` | Request timeout in seconds |

### Analysis Settings
| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_MAX_IMPORT_DEPTH` | `3` | How many levels of imports to traverse |
| `MCP_MAX_FILE_SIZE_KB` | `500` | Max file size to analyze |
| `MCP_MAX_DOM_SIZE_KB` | `100` | Max DOM snapshot size after cleaning |
| `MCP_SESSION_TTL_MINUTES` | `60` | Session expiration time |
| `MCP_MAX_SESSION_HISTORY` | `10` | Failures to keep per session |

### Logging
| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_LOG_LEVEL` | `INFO` | Log level (DEBUG, INFO, WARNING, ERROR) |

## Usage

### Running the Server

#### Development Mode (with Inspector)
```bash
mcp dev mcp_server.py
```

#### Production Mode
```bash
uv run python mcp_server.py
```

Or install the entry point:
```bash
uv pip install -e .
uv run mcp-server
```

### MCP Tools

The server exposes these MCP tools:

#### `analyze_failure`
Analyzes a WebdriverIO test failure with optional visual and DOM context.

**Parameters:**
- `console_output` (string): Raw console output from test run (supports ndjson)
- `spec_file_path` (string): Absolute path to the failing spec file
- `session_id` (string, optional): Session ID for maintaining context
- `screenshot_base64` (string, optional): Base64-encoded screenshot of the page at failure time
- `dom_snapshot` (string, optional): Full DOM snapshot or accessibility tree of the page
- `screenshot_mime_type` (string, optional): MIME type of the screenshot (default `image/png`)

**Returns:** JSON with `success`, `analysis`, `session_id`, `model`, `files_analyzed`

#### `clear_session`
Clears a session and its history.

**Parameters:**
- `session_id` (string): Session ID to clear

### MCP Resources

#### `config://current`
Returns current server configuration.

#### `session://{session_id}/status`
Returns session status and failure history.

## Architecture

```
WebdriverIO Test Failure
    │
    ▼
┌─────────────────────┐
│  MCP Client (JS)    │
│  (WebdriverIO side) │
└──────────┬──────────┘
           │ stdio/MCP protocol
           ▼
┌─────────────────────┐
│  MCP Server (Python)│
│  - Parse stack trace│
│  - Extract imports  │
│  - Gather code      │
└──────────┬──────────┘
           │ HTTP
           ▼
┌─────────────────────┐
│  Ollama (LLM)       │
│  - Analyze failure  │
│  - Suggest fixes    │
└─────────────────────┘
```

## Code Analysis

The server uses tree-sitter for parsing JavaScript/TypeScript:

1. **Import Extraction**: Parses ES6 imports and CommonJS requires
2. **Import Resolution**: Resolves relative imports with extension inference
3. **Code Gathering**: BFS traversal up to configured depth
4. **File Filtering**: Size limits and duplicate prevention

## Session Management

Sessions maintain context across multiple analyses:

- **TTL-based expiration**: Sessions expire after inactivity
- **Failure history**: Previous failures inform context
- **Conversation history**: LLM conversation is maintained
- **Auto-cleanup**: Expired sessions are automatically removed

## Error Patterns

The server recognizes common WebdriverIO error patterns:

| Error Pattern | Description |
|---------------|-------------|
| Element Not Found | Selector doesn't match any element |
| Element Not Interactable | Element exists but can't be interacted with |
| Timeout | Operation exceeded timeout threshold |
| Stale Element Reference | DOM changed after element was located |
| Assertion Failed | Test assertion didn't match expected value |
| Selector Error | Invalid selector syntax |

## Development

### Project Structure
```
failure_analysis_server/
├── mcp_server.py          # Main MCP server
├── pyproject.toml         # Project dependencies
├── .env                   # Configuration
├── .env.example           # Configuration template
├── README.md              # This file
└── CLAUDE.md              # Claude context
```

### Running Tests
```bash
uv run pytest
```

### Adding New Tools

1. Define the tool function with `@mcp.tool()` decorator
2. Use Pydantic `Field` for parameter descriptions
3. Return JSON strings for structured responses
4. Add error handling with informative messages

### Modifying the System Prompt

The `SYSTEM_PROMPT` constant in `mcp_server.py` defines how the LLM analyzes failures. Modify this to change analysis behavior.

## Troubleshooting

### Ollama Connection Issues
```
Cannot connect to Ollama at http://localhost:11434
```
**Solution:** Ensure Ollama is running: `ollama serve`

### Model Not Found
```
Model gemma4:e4b not found
```
**Solution:** Pull the model: `ollama pull gemma4:e4b`

### Screenshot Not Analyzed
If the LLM does not reference the screenshot in its analysis:
- Verify the model supports vision (`ollama show gemma4:e4b` should list `vision` in capabilities)
- Text-only models (`qwen2.5-coder:7b`) cannot process images
- Ensure `screenshot_base64` is a valid base64 string

### Import Resolution Failures
If imports aren't being resolved:
1. Check `MCP_MAX_IMPORT_DEPTH` setting
2. Verify file paths are relative (not absolute)
3. Ensure imported files exist and match expected extensions

### Session Expiration
Sessions expire after `MCP_SESSION_TTL_MINUTES` of inactivity. Increase this value for long-running analysis sessions.

### Large DOM Snapshots
If DOM snapshots cause context window overflows:
- Reduce `MCP_MAX_DOM_SIZE_KB` (default 100KB)
- Strip scripts/styles client-side before sending
- Send an accessibility tree instead of full HTML

## Integration with WebdriverIO

The server is designed to work with a WebdriverIO MCP client. The client should:

1. Start the MCP server as a subprocess
2. Communicate via stdio using MCP protocol
3. Send `analyze_failure` tool calls with:
   - Console output (raw text or ndjson lines)
   - Screenshot (`browser.takeScreenshot()` returns base64 PNG)
   - DOM snapshot (`document.documentElement.outerHTML`)
   - Spec file path
4. Handle JSON responses and display analysis

Example client usage (JavaScript):
```javascript
// Connect to MCP server
const client = new MCPClient({
  command: 'uv',
  args: ['run', 'python', 'mcp_server.py']
});

// Analyze a failure
const result = await client.callTool('analyze_failure', {
  console_output: testOutput,
  spec_file_path: '/path/to/spec.ts',
  screenshot_base64: await browser.takeScreenshot(),
  dom_snapshot: await browser.execute(() => document.documentElement.outerHTML)
});
```

## License

MIT License - See LICENSE file for details.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Run tests
5. Submit a pull request

## Support

For issues and feature requests, please use the GitHub issue tracker.
