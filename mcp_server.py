"""MCP Failure Analysis Server - Analyzes WebdriverIO test failures using local LLMs.

This server follows the MCP protocol with stdio transport for integration with WebdriverIO.
Run with: python mcp_server.py
Or with UV: uv run mcp_server.py
Or with Inspector: npx @modelcontextprotocol/inspector uv run mcp_server.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import tree_sitter_javascript as ts_js
import tree_sitter_typescript as ts_ts
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from ollama import AsyncClient
from pydantic import Field
from tree_sitter import Language, Parser, Query, Tree

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=getattr(logging, os.getenv("MCP_LOG_LEVEL", "INFO").upper()),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Initialize FastMCP server
mcp = FastMCP("failure-analysis", log_level="ERROR")

# Tree-sitter language instances
JS_LANGUAGE = Language(ts_js.language())
TYPESCRIPT_LANGUAGE = Language(ts_ts.language_typescript())
TSX_LANGUAGE = Language(ts_ts.language_tsx())

# Tree-sitter queries for import extraction
ES6_IMPORT_QUERY = """
(import_statement
  source: (string
    (string_fragment) @source))
"""

COMMONJS_REQUIRE_QUERY = """
(call_expression
  function: (identifier) @require
  arguments: (arguments
    (string
      (string_fragment) @source)))
  (#eq? @require "require")
"""


# =============================================================================
# Configuration
# =============================================================================

class Config:
    """Server configuration from environment variables."""

    def __init__(self):
        """Initialize configuration from environment variables."""
        # WebdriverIO MCP Client Configuration
        self.mcp_client_url: str = os.getenv("MCP_CLIENT_URL", "http://localhost:3000")
        self.mcp_client_port: int = int(os.getenv("MCP_CLIENT_PORT", "3000"))

        # Ollama Configuration
        self.ollama_host: str = os.getenv("MCP_OLLAMA_HOST", "http://localhost:11434")
        self.ollama_model: str = os.getenv("MCP_OLLAMA_MODEL", "qwen2.5-coder:7b")
        self.ollama_temperature: float = float(os.getenv("MCP_OLLAMA_TEMPERATURE", "0.1"))
        self.ollama_num_ctx: int = int(os.getenv("MCP_OLLAMA_NUM_CTX", "32768"))
        self.ollama_timeout: int = int(os.getenv("MCP_OLLAMA_TIMEOUT", "300"))

        # Analysis Configuration
        self.max_import_depth: int = int(os.getenv("MCP_MAX_IMPORT_DEPTH", "3"))
        self.max_file_size_kb: int = int(os.getenv("MCP_MAX_FILE_SIZE_KB", "500"))
        self.session_ttl_minutes: int = int(os.getenv("MCP_SESSION_TTL_MINUTES", "60"))
        self.max_session_history: int = int(os.getenv("MCP_MAX_SESSION_HISTORY", "10"))

        # Validate configuration
        if not self.ollama_host.startswith(("http://", "https://")):
            self.ollama_host = f"http://{self.ollama_host}"
        self.ollama_host = self.ollama_host.rstrip("/")

        # Validate MCP client URL
        if not self.mcp_client_url.startswith(("http://", "https://")):
            self.mcp_client_url = f"http://{self.mcp_client_url}"
        self.mcp_client_url = self.mcp_client_url.rstrip("/")


# Global config instance
_config: Config | None = None


def get_config() -> Config:
    """Get or create global config instance."""
    global _config
    if _config is None:
        _config = Config()
    return _config


# =============================================================================
# Session Management
# =============================================================================

class FailureContext:
    """Context for a single failure analysis."""

    def __init__(
        self,
        timestamp: float,
        spec_path: Path,
        error_summary: str,
        analysis_result: str,
        files_analyzed: list[str] | None = None,
    ):
        self.timestamp = timestamp
        self.spec_path = spec_path
        self.error_summary = error_summary
        self.analysis_result = analysis_result
        self.files_analyzed = files_analyzed or []


class Session:
    """Session containing failure history and context."""

    def __init__(self, session_id: str, created_at: float, last_accessed: float):
        self.session_id = session_id
        self.created_at = created_at
        self.last_accessed = last_accessed
        self.failures: list[FailureContext] = []
        self.total_analyses: int = 0
        self.conversation_history: list[dict] = []

    def add_failure(self, context: FailureContext) -> None:
        """Add a failure to the session history."""
        config = get_config()
        self.failures.append(context)
        self.total_analyses += 1
        self.last_accessed = time.time()

        # Keep only recent failures
        if len(self.failures) > config.max_session_history:
            self.failures = self.failures[-config.max_session_history :]

    def add_to_conversation(self, role: str, content: str) -> None:
        """Add message to conversation history."""
        self.conversation_history.append({"role": role, "content": content})
        # Keep conversation manageable
        if len(self.conversation_history) > 20:
            self.conversation_history = self.conversation_history[-20:]

    def get_context_summary(self) -> str:
        """Generate a summary of session context for LLM."""
        if not self.failures:
            return "No previous failures in this session."

        lines = [f"Session History ({len(self.failures)} previous failures):"]
        for i, failure in enumerate(self.failures[-5:], 1):
            lines.append(f"\n{i}. Spec: {failure.spec_path}")
            lines.append(f"   Error: {failure.error_summary[:200]}...")
        return "\n".join(lines)

    def is_expired(self) -> bool:
        """Check if session has expired."""
        config = get_config()
        return (time.time() - self.last_accessed) > (config.session_ttl_minutes * 60)


class SessionManager:
    """Manages active sessions with TTL-based expiration."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._cleanup_counter: int = 0

    def get_or_create(self, session_id: str | None) -> tuple[str, Session]:
        """Get existing session or create new one."""
        self._cleanup_counter += 1

        # Cleanup expired sessions every 100 accesses
        if self._cleanup_counter >= 100:
            self._cleanup_expired()
            self._cleanup_counter = 0

        sid = session_id or str(uuid.uuid4())

        if sid not in self._sessions:
            now = time.time()
            self._sessions[sid] = Session(
                session_id=sid,
                created_at=now,
                last_accessed=now,
            )

        session = self._sessions[sid]
        session.last_accessed = time.time()
        return sid, session

    def clear(self, session_id: str) -> bool:
        """Clear a specific session. Returns True if existed."""
        if session_id in self._sessions:
            del self._sessions[session_id]
            return True
        return False

    def _cleanup_expired(self) -> None:
        """Remove expired sessions."""
        expired = [
            sid for sid, session in self._sessions.items() if session.is_expired()
        ]
        for sid in expired:
            del self._sessions[sid]
            logger.debug(f"Cleaned up expired session: {sid}")


# Global session manager
_session_manager: SessionManager | None = None


def get_session_manager() -> SessionManager:
    """Get or create global session manager."""
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager()
    return _session_manager


# =============================================================================
# Code Analysis
# =============================================================================

class ImportExtractor:
    """Extracts imports from JavaScript/TypeScript files using tree-sitter."""

    def __init__(self) -> None:
        self._parsers: dict[str, Parser] = {}

    def _get_parser(self, file_path: Path) -> Parser:
        """Get or create parser for file type."""
        ext = file_path.suffix

        if ext not in self._parsers:
            if ext == ".ts":
                parser = Parser(TYPESCRIPT_LANGUAGE)
            elif ext == ".tsx":
                parser = Parser(TSX_LANGUAGE)
            else:
                parser = Parser(JS_LANGUAGE)
            self._parsers[ext] = parser

        return self._parsers[ext]

    def extract_imports(self, file_path: Path) -> list[Path]:
        """Extract and resolve all imports from a file."""
        if not file_path.exists():
            logger.warning(f"File not found: {file_path}")
            return []

        try:
            content = file_path.read_text(encoding="utf-8")
        except (IOError, UnicodeDecodeError) as e:
            logger.error(f"Error reading {file_path}: {e}")
            return []

        parser = self._get_parser(file_path)
        tree = parser.parse(content.encode())
        base_dir = file_path.parent

        imports: list[Path] = []
        es6_imports = self._parse_es6_imports(tree, content.encode(), base_dir, file_path)
        cjs_imports = self._parse_commonjs_requires(tree, content.encode(), base_dir, file_path)

        logger.debug(f"ES6 imports found: {len(es6_imports)}")
        logger.debug(f"CommonJS requires found: {len(cjs_imports)}")

        imports.extend(es6_imports)
        imports.extend(cjs_imports)

        return imports

    def _parse_es6_imports(self, tree: Tree, content: bytes, base_dir: Path, file_path: Path) -> list[Path]:
        """Parse ES6 import statements using regex."""
        imports = []

        try:
            content_str = content.decode('utf-8')
            # Match various import patterns
            patterns = [
                r'import\s+(?:{[^}]+}|\*\s+as\s+\w+|\w+)\s+from\s+["\']([^"\']+)["\']',
                r'import\s+["\']([^"\']+)["\']',  # Side-effect imports
            ]
            for pattern in patterns:
                for match in re.finditer(pattern, content_str):
                    source = match.group(1)
                    resolved = self._resolve_import(source, base_dir)
                    if resolved and resolved not in imports:
                        imports.append(resolved)

            logger.debug(f"ES6 imports found in {file_path.name}: {len(imports)}")

        except Exception as e:
            logger.warning(f"Error parsing ES6 imports: {e}")

        return imports

    def _parse_commonjs_requires(self, tree: Tree, content: bytes, base_dir: Path, file_path: Path) -> list[Path]:
        """Parse CommonJS require statements using regex."""
        imports = []

        try:
            content_str = content.decode('utf-8')
            pattern = r'require\s*\(\s*["\']([^"\']+)["\']\s*\)'
            for match in re.finditer(pattern, content_str):
                source = match.group(1)
                resolved = self._resolve_import(source, base_dir)
                if resolved and resolved not in imports:
                    imports.append(resolved)

            logger.debug(f"CommonJS requires found in {file_path.name}: {len(imports)}")

        except Exception as e:
            logger.warning(f"Error parsing CommonJS requires: {e}")

        return imports

    def _resolve_import(self, source: str, base_dir: Path) -> Path | None:
        """Resolve import source to absolute path."""
        # Skip external packages (no ./ or ../)
        if not source.startswith((".", "/")):
            return None

        # Handle absolute imports (from project root)
        if source.startswith("/"):
            return None

        # Resolve relative imports
        target = base_dir / source

        # Try extensions
        extensions = [".ts", ".tsx", ".js", ".jsx"]

        # Try as-is first
        if target.exists() and target.is_file():
            return target.resolve()

        # Try with extensions
        for ext in extensions:
            candidate = Path(str(target) + ext)
            if candidate.exists():
                return candidate.resolve()

        # Try index files
        for ext in extensions:
            candidate = target / f"index{ext}"
            if candidate.exists():
                return candidate.resolve()

        logger.debug(f"Could not resolve import: {source} from {base_dir}")
        return None


class CodeAnalyzer:
    """Analyzes code files and extracts relevant context."""

    def __init__(self) -> None:
        self._extractor = ImportExtractor()

    def analyze_failure(
        self,
        spec_path: Path,
        error_stack: list[str],
        max_depth: int = 1,
    ) -> dict[str, str]:
        """Analyze a failure and extract relevant code."""
        config = get_config()

        files_to_analyze: set[Path] = {spec_path.resolve()}
        analyzed: set[Path] = set()
        result: dict[str, str] = {}
        max_size = config.max_file_size_kb * 1024

        logger.info(f"Starting analysis of {spec_path} with max_depth={max_depth}")

        # Parse stack trace for additional files
        for line in error_stack:
            file_path = self._extract_path_from_stack(line)
            if file_path and file_path.exists():
                logger.debug(f"Adding file from stack trace: {file_path}")
                files_to_analyze.add(file_path)

        # BFS through imports
        current_depth = 0
        while files_to_analyze and current_depth <= max_depth:
            next_level: set[Path] = set()
            logger.info(f"Depth {current_depth}: Analyzing {len(files_to_analyze)} file(s)")

            for file_path in files_to_analyze:
                if file_path in analyzed:
                    logger.debug(f"Skipping already analyzed: {file_path}")
                    continue

                analyzed.add(file_path)

                # Check file size
                try:
                    if file_path.stat().st_size > max_size:
                        logger.warning(f"Skipping large file: {file_path}")
                        continue
                except OSError:
                    logger.debug(f"Cannot stat file: {file_path}")
                    continue

                try:
                    content = file_path.read_text(encoding="utf-8")
                    result[str(file_path)] = content
                    logger.info(f"Added file to analysis: {file_path}")

                    # Extract imports for next level
                    if current_depth < max_depth:
                        imports = self._extractor.extract_imports(file_path)
                        if imports:
                            logger.info(f"  Found {len(imports)} imports in {file_path.name}: {[i.name for i in imports]}")
                        else:
                            logger.info(f"  No imports found in {file_path.name}")
                        for imp in imports:
                            if imp not in analyzed:
                                next_level.add(imp)

                except (IOError, UnicodeDecodeError) as e:
                    logger.error(f"Error reading {file_path}: {e}")

            files_to_analyze = next_level
            current_depth += 1

        logger.info(f"Analysis complete. Total files: {len(result)}")
        return result

    def _extract_path_from_stack(self, line: str) -> Path | None:
        """Extract file path from a stack trace line."""
        # Match patterns like "at /path/to/file.ts:10:5" or "file.ts:10"
        patterns = [
            r"at\s+(.+?:\d+:\d+)",
            r"Error in\s+(.+?:\d+)",
            r"\s*([^:\s]+\.(ts|tsx|js|jsx)):(\d+)",
        ]

        for pattern in patterns:
            match = re.search(pattern, line)
            if match:
                path_str = match.group(1)
                # Remove line numbers
                path_str = re.sub(r":\d+:\d+$", "", path_str)
                path_str = re.sub(r":\d+$", "", path_str)
                path = Path(path_str)
                if path.exists():
                    return path

        return None


def _extract_error_details(console_output: str) -> dict[str, Any]:
    """Extract error details from console output.

    Returns a dictionary with:
    - primary_error: The main error message
    - error_type: Categorized error type
    - test_name: Test case name if found
    - stack_trace: List of stack frames
    - error_lines: All lines containing error information
    """
    lines = console_output.split("\n")

    result = {
        "primary_error": "",
        "error_type": "",
        "test_name": "",
        "stack_trace": [],
        "error_lines": [],
    }

    # Extract test name
    for line in lines:
        test_match = re.search(r"TEST FAILED:\s*(.+?)(?:\n|$)", line)
        if test_match:
            result["test_name"] = test_match.group(1).strip()
            break

    # Find primary error message
    for line in lines:
        # Look for "Error Message:" pattern first (most reliable)
        msg_match = re.search(r"Error Message:\s*(.+)$", line)
        if msg_match:
            result["primary_error"] = msg_match.group(1).strip()
            result["error_lines"].append(line)
            break

    # If no explicit error message found, look for direct errors
    if not result["primary_error"]:
        for line in lines:
            if "Error:" in line and "at " not in line:
                error_match = re.search(r"Error:\s*(.+?)$", line)
                if error_match:
                    result["primary_error"] = error_match.group(1).strip()
                    result["error_lines"].append(line)
                    break

    # Extract stack trace (lines with "at " that reference project files)
    for line in lines:
        if "at " in line:
            # Only include lines that reference actual files (not node_modules)
            if "file:///" in line or ".js:" in line or ".ts:" in line:
                # Skip node_modules
                if "node_modules" not in line:
                    result["stack_trace"].append(line.strip())
                    result["error_lines"].append(line)

    # Determine error type
    primary = result["primary_error"].lower()
    if "element" in primary and "not found" in primary:
        result["error_type"] = "Element Not Found"
    elif "element" in primary and "interactable" in primary:
        result["error_type"] = "Element Not Interactable"
    elif "timeout" in primary:
        result["error_type"] = "Timeout"
    elif "stale" in primary:
        result["error_type"] = "Stale Element Reference"
    elif "assertion" in primary or "expect" in primary:
        result["error_type"] = "Assertion Failed"
    elif "selector" in primary:
        result["error_type"] = "Selector Error"
    else:
        result["error_type"] = "WebdriverIO Error"

    return result


# =============================================================================
# LLM Integration
# =============================================================================

SYSTEM_PROMPT = """You are an expert test automation engineer specializing in WebdriverIO and TypeScript/JavaScript. Your task is to analyze test failures and provide clear, actionable explanations.
BEFORE YOU ANALYZE ANYTHING — READ THIS:
When an assertion fails with mismatched values (Expected X, Received Y), the root cause
is ALMOST NEVER wrong test data. Ask yourself: what code runs BETWEEN the test data and
the assertion? That code is where the bug lives. You MUST read every method in the page
objects that touches the input fields before drawing any conclusion.

MANDATORY PRE-ANALYSIS CHECKLIST — complete this mentally before writing anything:
1. Find the method that writes data to the form fields (addValue, setValue, type, etc.)
2. Read that method's FULL implementation in the page object file
3. Check for: string concatenation (+), hardcoded strings, variable substitution errors
4. Only if steps 1-3 find nothing wrong, then look at test data or selectors

IF YOU FIND a concatenation like `addValue(value + "1")` or `setValue(text + "test")`:
- That IS the bug. Full stop.
- The fix is to remove the concatenation from the PAGE OBJECT method.
- You are FORBIDDEN from suggesting fixture/test data changes when this exists.


CRITICAL: Focus on the ERROR DETAILS section. The "Console Output" and "Error Details" sections contain the actual failure information. The "Relevant Code" section is context - DO NOT just describe it.

IMPORTANT: Multiple code files are provided including the spec file AND imported page objects/components (up to 3-4 levels deep). You MUST examine ALL files to find the root cause:
- The spec file shows WHAT failed
- The page object files show HOW elements are accessed
- Component files may contain the actual bug

Your job is to:
1. READ the Error Details first - identify the specific error message and type
2. TRACE the error through the stack trace to find the problematic code
3. EXAMINE ALL provided code files (spec + imports) - look for mismatches between selectors and actual usage
4. ANALYZE why that code caused the error
5. PROPOSE a specific fix - point to exact file and line

Common WebdriverIO failure patterns:
- "element wasn't found" -> Wrong selector in page object, element not rendered yet, or in iframe
- "element not interactable" -> Element covered by another element or not visible
- "stale element reference" -> DOM changed after element was located
- "timeout" -> Element took too long to appear or action never completed
- "can't call X on element" -> Element doesn't support that method or wrong element type
- "Expected X but got Y" -> Assertion failed, check actual vs expected values

When analyzing:
- Check if selectors in page objects match the actual DOM
- Look for missing awaits in async operations
- Verify test data matches expected format
- Check for timing issues or race conditions
- LOOK FOR THESE SPECIFIC BUGS in data entry methods:
  * String concatenation: `addValue(value+"1")`, `type(text+"test")` - these modify the intended value!
  * Hardcoded values in switch/case statements
  * Wrong variable passed: passing `lastName` instead of `firstName`
  * Extra whitespace or unexpected characters being added

Output Format:
## Failure Summary
One sentence describing what failed (e.g., "Test failed because selector '#foo' in CheckoutPage.js:23 was not found")

## Root Cause Analysis
- **Error Type:** (e.g., Element not found, Timeout, Assertion failed)
- **Location:** (specific file and line number from stack trace)
- **Cause:** (why this happened - wrong selector, timing issue, missing await, etc.)

CRITICAL INSTRUCTION: You MUST investigate the ACTUAL IMPLEMENTATION of methods that handle data, not just the test data values.
- Look at methods that SET data (addValue, setValue, type, etc.) in page objects
- Check for hardcoded values, string concatenations, or transformations in the framework code
- Verify the data flow: test data → method call → element interaction
- The bug is OFTEN in the framework code (page object methods), not the test data

MANDATORY: Before suggesting to change test expectations or test data:
1. EXAMINE the implementation of ALL methods that interact with the failing element
2. CHECK for any hardcoded strings, concatenations (+"1", +"test"), or transformations
3. VERIFY the actual values being sent to the application match what the test intends
4. Only after confirming the framework code is correct, then consider test data issues

DO NOT simply say "change the expected value" - find the ROOT CAUSE in the code.

## Suggested Fix
STRICT RULE: If you found ANY string concatenation, hardcoded value, or 
transformation in a framework method (page object, util, component), you MUST 
fix THAT code. You are FORBIDDEN from suggesting test data changes when a 
framework bug exists.

The fix must be in the framework file, not the test or fixture file.
- File: (the page object or util file containing the bug)
- Line: (exact line number)
- Current: (the buggy line as it appears in the code)
- Fixed: (remove the concatenation/transformation)

Only in case the framework file does not have bugs need fixing, focus on the spec file or test data. 

## Prevention Tips
How to avoid similar issues in the future

## Confidence
high|medium|low - based on clarity of error message and code context

## Files Analyzed
List which files were examined and what you found in each
"""


class OllamaClient:
    """Async client for Ollama LLM interactions."""

    def __init__(self) -> None:
        config = get_config()
        self.host = config.ollama_host
        self.model = config.ollama_model
        self.temperature = config.ollama_temperature
        self.num_ctx = config.ollama_num_ctx
        self.timeout = config.ollama_timeout
        self._client: AsyncClient | None = None

    async def _get_client(self) -> AsyncClient:
        """Get or create async client."""
        if self._client is None:
            self._client = AsyncClient(host=self.host)
        return self._client

    async def check_connection(self) -> bool:
        """Check if Ollama is accessible and model is available."""
        try:
            client = await self._get_client()
            models = await client.list()
            model_names = [m.get("name", m.get("model")) for m in models.get("models", [])]

            if self.model not in model_names:
                logger.warning(f"Model {self.model} not found. Available: {model_names}")
                return False

            return True

        except Exception as e:
            logger.error(f"Ollama connection check failed: {e}")
            return False

    async def analyze(
        self,
        prompt: str,
        session_history: list[dict],
    ) -> dict[str, Any]:
        """Analyze failure using Ollama."""
        client = await self._get_client()

        # Build messages
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(session_history)
        messages.append({"role": "user", "content": prompt})

        # Check token estimate (rough)
        estimated_tokens = len(SYSTEM_PROMPT) + len(prompt) + sum(
            len(m.get("content", "")) for m in session_history
        )
        estimated_tokens //= 4

        if estimated_tokens > self.num_ctx * 0.8:
            logger.warning(
                f"Prompt may exceed context window: ~{estimated_tokens} tokens vs {self.num_ctx} limit"
            )

        try:
            response = await asyncio.wait_for(
                client.chat(
                    model=self.model,
                    messages=messages,
                    options={
                        "temperature": self.temperature,
                        "num_ctx": self.num_ctx,
                    },
                ),
                timeout=self.timeout,
            )

            return {
                "content": response["message"]["content"],
                "model": self.model,
                "done": response.get("done", False),
            }

        except asyncio.TimeoutError:
            raise RuntimeError(f"Request timed out after {self.timeout}s")
        except Exception as e:
            raise RuntimeError(f"Ollama error: {e}")


# =============================================================================
# MCP Tools and Resources
# =============================================================================

@mcp.tool()
async def analyze_failure(
    console_output: str = Field(description="Raw console output from WebdriverIO test run"),
    spec_file_path: str = Field(description="Absolute or relative path to the spec file that failed"),
    session_id: Optional[str] = Field(default=None, description="Optional session ID for maintaining context across analyses"),
) -> str:
    """Analyze a WebdriverIO test failure and provide explanation with fix suggestions.

    This tool analyzes the console output from a WebdriverIO test run, extracts the relevant
    code files (including imported page objects), and uses a local LLM to provide
    detailed analysis and fix suggestions.

    The analysis includes:
    - Root cause identification from error and stack trace
    - Code context from the spec file and its imports
    - Session-aware analysis (previous failures provide context)
    - Markdown-formatted output with code examples

    Returns a JSON string with the analysis results.
    """
    # Get or create session
    session_id, session = get_session_manager().get_or_create(session_id)

    try:
        # Validate spec file
        spec_path = Path(spec_file_path).resolve()
        if not spec_path.exists():
            return json.dumps({
                "success": False,
                "error": f"Spec file not found: {spec_file_path}",
                "session_id": session_id,
            })

        if spec_path.suffix not in (".ts", ".tsx", ".js", ".jsx"):
            return json.dumps({
                "success": False,
                "error": f"Invalid spec file type: {spec_path.suffix}",
                "session_id": session_id,
            })

        # Parse console output to extract error details
        error_info = _extract_error_details(console_output)
        error_lines = error_info["error_lines"]
        error_summary = error_info["primary_error"] if error_info["primary_error"] else "Unknown error"

        # Analyze code
        config = get_config()
        analyzer = CodeAnalyzer()
        code_context = analyzer.analyze_failure(
            spec_path=spec_path,
            error_stack=error_lines,
            max_depth=config.max_import_depth,
        )

        # Build prompt - put ERROR DETAILS first and prominently
        prompt_lines = ["# WebdriverIO Test Failure Analysis\n"]

        # CRITICAL: Error details section - this is what the LLM should focus on
        prompt_lines.append("=" * 60)
        prompt_lines.append("ERROR DETAILS (FOCUS HERE)")
        prompt_lines.append("=" * 60)
        prompt_lines.append(f"\n**Primary Error:** {error_info['primary_error']}")
        if error_info['error_type']:
            prompt_lines.append(f"**Error Type:** {error_info['error_type']}")
        if error_info['test_name']:
            prompt_lines.append(f"**Test Case:** {error_info['test_name']}")
        prompt_lines.append(f"**Spec File:** {spec_path}")
        if error_info['stack_trace']:
            prompt_lines.append("\n**Stack Trace (most relevant frames):**")
            for frame in error_info['stack_trace'][:5]:  # Top 5 frames
                prompt_lines.append(f"  - {frame}")
        prompt_lines.append("\n" + "=" * 60 + "\n")

        # Add session context
        session_context = session.get_context_summary()
        if session_context:
            prompt_lines.append("## Session Context")
            prompt_lines.append(session_context)
            prompt_lines.append("")

        # Add relevant code files (prioritize files mentioned in stack trace)
        prompt_lines.append("## Relevant Code Files")
        # Sort code_context to put stack trace files first
        sorted_files = sorted(
            code_context.items(),
            key=lambda x: (0 if any(f in x[0] for f in error_info['stack_trace']) else 1, x[0])
        )
        for file_path, content in sorted_files:
            prompt_lines.append(f"\n### {file_path}")
            prompt_lines.append("```typescript")
            # Add line numbers
            numbered = "\n".join(
                f"{i+1:4d} | {line}" for i, line in enumerate(content.split("\n"))
            )
            prompt_lines.append(numbered)
            prompt_lines.append("```")

        # Raw console output at the end for reference
        prompt_lines.append("\n## Full Console Output (for reference)")
        prompt_lines.append("```")
        # Truncate if extremely long
        max_output = 5000
        output = console_output[:max_output]
        if len(console_output) > max_output:
            output += f"\n... (truncated, {len(console_output) - max_output} chars remaining)"
        prompt_lines.append(output)
        prompt_lines.append("```")

        prompt = "\n".join(prompt_lines)

        # Check Ollama connection
        llm_client = OllamaClient()
        if not await llm_client.check_connection():
            return json.dumps({
                "success": False,
                "error": (
                    f"Cannot connect to Ollama at {config.ollama_host} "
                    f"or model {config.ollama_model} not found. "
                    "Please ensure Ollama is running and the model is pulled."
                ),
                "session_id": session_id,
            })

        # Get analysis from LLM
        response = await llm_client.analyze(
            prompt=prompt,
            session_history=session.conversation_history,
        )

        # Update session with this failure
        session.add_failure(FailureContext(
            timestamp=time.time(),
            spec_path=spec_path,
            error_summary=error_summary[:500],
            analysis_result=response["content"],
            files_analyzed=list(code_context.keys()),
        ))

        # Add to conversation history
        session.add_to_conversation("user", prompt[:1000])  # Truncate for history
        session.add_to_conversation("assistant", response["content"][:1000])

        return json.dumps({
            "success": True,
            "analysis": response["content"],
            "session_id": session_id,
            "model": response["model"],
            "files_analyzed": list(code_context.keys()),
        }, indent=2)

    except Exception as e:
        logger.exception("Unexpected error during analysis")
        return json.dumps({
            "success": False,
            "error": f"Internal error: {str(e)}",
            "session_id": session_id,
        })


@mcp.tool()
def clear_session(
    session_id: str = Field(description="Session ID to clear"),
) -> str:
    """Clear a session and its conversation history.

    This removes all stored failure context and conversation history
    for the specified session. Use this when you want to start fresh
    analysis without previous context.

    Returns a JSON confirmation.
    """
    existed = get_session_manager().clear(session_id)

    return json.dumps({
        "success": True,
        "message": (
            f"Session {session_id} cleared."
            if existed else
            f"Session {session_id} not found (may have expired)."
        ),
        "session_id": session_id,
    })


@mcp.resource("session://{session_id}/status")
def get_session_status(session_id: str) -> str:
    """Get current session status and history summary.

    Returns markdown formatted information about the session including:
    - Creation time
    - Number of analyses performed
    - Recent failure summaries
    """
    _, session = get_session_manager().get_or_create(session_id)

    lines = [f"# Session Status: {session_id}\n"]
    lines.append(f"- **Created**: {time.ctime(session.created_at)}")
    lines.append(f"- **Last Accessed**: {time.ctime(session.last_accessed)}")
    lines.append(f"- **Total Analyses**: {session.total_analyses}")
    lines.append(f"- **History Size**: {len(session.failures)}")

    if session.failures:
        lines.append("\n## Recent Failures\n")
        for i, failure in enumerate(session.failures[-5:], 1):
            lines.append(f"{i}. `{failure.spec_path.name}` - {failure.error_summary[:60]}...")

    return "\n".join(lines)


@mcp.resource("config://current")
def get_current_config() -> str:
    """Get current server configuration (sensitive values masked).

    Returns markdown formatted configuration showing:
    - MCP Client settings (URL, port)
    - Ollama settings (host, model, temperature, context window)
    - Analysis settings (import depth, file size limits)
    - Session settings (TTL, max history)
    """
    config = get_config()

    lines = ["# MCP Failure Analysis Server Configuration\n"]
    lines.append("## MCP Client Settings")
    lines.append(f"- **Client URL**: {config.mcp_client_url}")
    lines.append(f"- **Client Port**: {config.mcp_client_port}")
    lines.append("\n## Ollama Settings")
    lines.append(f"- **Host**: {config.ollama_host}")
    lines.append(f"- **Model**: {config.ollama_model}")
    lines.append(f"- **Temperature**: {config.ollama_temperature}")
    lines.append(f"- **Context Window**: {config.ollama_num_ctx}")
    lines.append(f"- **Timeout**: {config.ollama_timeout}s")
    lines.append(f"\n## Analysis Settings")
    lines.append(f"- **Max Import Depth**: {config.max_import_depth}")
    lines.append(f"- **Max File Size**: {config.max_file_size_kb}KB")
    lines.append(f"\n## Session Settings")
    lines.append(f"- **Session TTL**: {config.session_ttl_minutes}min")
    lines.append(f"- **Max History**: {config.max_session_history}")

    return "\n".join(lines)


# =============================================================================
# Entry Point
# =============================================================================

def main() -> None:
    """Run the MCP server with stdio transport."""
    config = get_config()
    logger.info("Starting MCP Failure Analysis Server")
    logger.info(f"MCP Client URL: {config.mcp_client_url}:{config.mcp_client_port}")
    logger.info(f"Ollama host: {config.ollama_host}")
    logger.info(f"Model: {config.ollama_model}")
    logger.info(f"Log level: {logging.getLevelName(logger.level)}")

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
