import ast
import datetime
import json
import operator
import re
import time
import uuid
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable, Awaitable, Union

from app.core.config import settings
from app.core.logging import logger
from app.core.security import resolve_and_validate_path, validate_file_size, SecurityError, PathTraversalError
from app.core.command_runner import run_command_safely
from app.database.connection import db
from app.schemas.tools import ToolDefinition, ToolExecutionResponse
from app.services.web_search_service import web_search_service
from app.services.lenovo_kb_service import lenovo_kb_service

# Safe AST evaluator for math
SAFE_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

def safe_eval_math(node):
    if isinstance(node, ast.Constant):  # number
        if isinstance(node.value, (int, float)):
            if abs(node.value) > 1e100:
                raise ValueError("Number exceeds safety limit (1e100)")
            return node.value
        raise ValueError("Only numeric constants allowed")
    elif isinstance(node, ast.BinOp):
        left = safe_eval_math(node.left)
        right = safe_eval_math(node.right)
        op_type = type(node.op)
        if op_type is ast.Pow:
            # Prevent exponential blowup and integer string conversion freeze
            if abs(right) > 1000 or (isinstance(left, (int, float)) and abs(left) > 1e15 and right > 2):
                raise ValueError("Exponent or base too large for safe evaluation (max exponent: 1000)")
        elif op_type in (ast.Div, ast.FloorDiv, ast.Mod):
            if right == 0:
                raise ZeroDivisionError("Division by zero")
        if op_type in SAFE_OPERATORS:
            res = SAFE_OPERATORS[op_type](left, right)
            if isinstance(res, (int, float)) and abs(res) > 1e100:
                raise ValueError("Intermediate result exceeds calculation bounds")
            return res
        raise ValueError(f"Unsupported operator: {op_type}")
    elif isinstance(node, ast.UnaryOp):
        operand = safe_eval_math(node.operand)
        op_type = type(node.op)
        if op_type in SAFE_OPERATORS:
            return SAFE_OPERATORS[op_type](operand)
        raise ValueError(f"Unsupported unary operator: {op_type}")
    else:
        raise ValueError(f"Unsupported AST node: {type(node)}")

class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, ToolDefinition] = {}
        self._handlers: Dict[str, Callable[[Dict[str, Any]], Awaitable[Any]]] = {}
        self._active_workspace: Optional[Path] = None
        self._register_default_tools()

    def set_active_workspace(self, workspace_path: Optional[Union[str, Path]] = None):
        """Sets the active workspace root for sandboxed tool execution."""
        self._active_workspace = Path(workspace_path).resolve() if workspace_path else None

    def get_workspace_root(self) -> Path:
        """Returns the effective workspace root (user-specific or global)."""
        return self._active_workspace or settings.workspace_root

    def register_tool(
        self,
        definition: ToolDefinition,
        handler: Callable[[Dict[str, Any]], Awaitable[Any]]
    ):
        self._tools[definition.name] = definition
        self._handlers[definition.name] = handler

    def get_all_definitions(self) -> List[ToolDefinition]:
        return list(self._tools.values())

    def get_ollama_tools_schema(self) -> List[Dict[str, Any]]:
        schemas = []
        for tool in self._tools.values():
            schemas.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters
                }
            })
        return schemas

    async def execute_tool(
        self,
        name: str,
        arguments: Dict[str, Any],
        conversation_id: Optional[str] = None,
        approved: bool = False
    ) -> ToolExecutionResponse:
        start_time = time.time()
        tool_id = str(uuid.uuid4())

        if name not in self._tools or name not in self._handlers:
            err = f"Tool '{name}' is not registered."
            await self._log_execution(tool_id, conversation_id, name, arguments, None, "error", err, 0)
            return ToolExecutionResponse(
                tool_name=name,
                status="error",
                error=err
            )

        tool_def = self._tools[name]

        # Permission check
        if tool_def.requires_approval and not approved:
            return ToolExecutionResponse(
                tool_name=name,
                status="requires_approval",
                requires_approval=True,
                confirmation_message=f"Action '{name}' requires confirmation before modifying files or running commands."
            )

        # Check external tool permissions
        if tool_def.is_external and not settings.enable_web_search:
            err = "External network access is disabled in settings for privacy."
            await self._log_execution(tool_id, conversation_id, name, arguments, None, "blocked", err, 0)
            return ToolExecutionResponse(
                tool_name=name,
                status="blocked",
                error=err
            )

        try:
            handler = self._handlers[name]
            result = await handler(arguments)
            duration_ms = int((time.time() - start_time) * 1000)
            await self._log_execution(tool_id, conversation_id, name, arguments, result, "success", None, duration_ms)
            return ToolExecutionResponse(
                tool_name=name,
                status="success",
                result=result,
                execution_time_ms=duration_ms
            )
        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            err_msg = str(e)
            logger.exception(f"Error executing tool '{name}': {err_msg}")
            await self._log_execution(tool_id, conversation_id, name, arguments, None, "error", err_msg, duration_ms)
            return ToolExecutionResponse(
                tool_name=name,
                status="error",
                error=err_msg,
                execution_time_ms=duration_ms
            )

    async def _log_execution(
        self,
        log_id: str,
        conversation_id: Optional[str],
        tool_name: str,
        args: Dict[str, Any],
        result: Any,
        status: str,
        error: Optional[str],
        duration_ms: int
    ):
        try:
            await db.execute(
                """
                INSERT INTO tools_log (id, conversation_id, tool_name, arguments_json, result_json, status, error, execution_time_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    log_id,
                    conversation_id,
                    tool_name,
                    json.dumps(args, default=str),
                    json.dumps(result, default=str) if result is not None else None,
                    status,
                    error,
                    duration_ms
                )
            )
        except Exception as e:
            logger.error(f"Failed to log tool execution: {e}")

    def _register_default_tools(self):
        # 1. Calculator
        self.register_tool(
            ToolDefinition(
                name="calculator",
                description="Performs safe mathematical calculations (+, -, *, /, //, %, **).",
                parameters={
                    "type": "object",
                    "properties": {
                        "expression": {"type": "string", "description": "Mathematical expression (e.g., '14 * 12 + 5')"}
                    },
                    "required": ["expression"]
                },
                category="general"
            ),
            self._handle_calculator
        )

        # 2. Date and Time
        self.register_tool(
            ToolDefinition(
                name="get_current_datetime",
                description="Returns current local date, time, and timezone information.",
                parameters={
                    "type": "object",
                    "properties": {
                        "timezone": {"type": "string", "description": "Optional timezone name"}
                    }
                },
                category="general"
            ),
            self._handle_datetime
        )

        # 3. Read File
        self.register_tool(
            ToolDefinition(
                name="read_workspace_file",
                description="Reads the text content of a file within the authorized workspace.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative file path inside the workspace"}
                    },
                    "required": ["path"]
                },
                category="filesystem"
            ),
            self._handle_read_file
        )

        # 4. Search Files
        self.register_tool(
            ToolDefinition(
                name="search_workspace_files",
                description="Searches for files matching a pattern or containing specific text within the workspace.",
                parameters={
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Filename pattern to match (e.g. '*.py' or '*test*')"},
                        "content_search": {"type": "string", "description": "Optional text to search inside file contents"}
                    }
                },
                category="filesystem"
            ),
            self._handle_search_files
        )

        # 5. Create / Write File
        self.register_tool(
            ToolDefinition(
                name="write_workspace_file",
                description="Creates or overwrites a text file within the authorized workspace. Requires user confirmation if overwriting existing files.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative file path inside the workspace"},
                        "content": {"type": "string", "description": "Text content to write"}
                    },
                    "required": ["path", "content"]
                },
                requires_approval=True,
                category="filesystem"
            ),
            self._handle_write_file
        )

        # 6. JSON Validator & Formatter
        self.register_tool(
            ToolDefinition(
                name="validate_and_format_json",
                description="Validates a JSON string, reports syntax errors, and returns pretty-formatted JSON.",
                parameters={
                    "type": "object",
                    "properties": {
                        "json_string": {"type": "string", "description": "JSON string to validate and format"}
                    },
                    "required": ["json_string"]
                },
                category="general"
            ),
            self._handle_validate_json
        )

        # 7. Document Summarizer / Text Extractor
        self.register_tool(
            ToolDefinition(
                name="extract_document_text",
                description="Extracts clean text and line count statistics from an approved text or markdown file in the workspace.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative path to the document file"}
                    },
                    "required": ["path"]
                },
                category="filesystem"
            ),
            self._handle_extract_document
        )

        # 8. Run Project Tests
        self.register_tool(
            ToolDefinition(
                name="run_project_tests",
                description="Runs approved test suite (e.g. 'pytest' or 'npm test') inside the authorized workspace with strict sandboxing and timeouts.",
                parameters={
                    "type": "object",
                    "properties": {
                        "test_framework": {"type": "string", "description": "Test command to execute: 'pytest', 'python -m unittest', 'npm test'"}
                    },
                    "required": ["test_framework"]
                },
                requires_approval=True,
                category="project"
            ),
            self._handle_run_tests
        )

        # 9. Git Status and Diff
        self.register_tool(
            ToolDefinition(
                name="inspect_git_status",
                description="Inspects Git repository status, changed files, and unified diff within the workspace.",
                parameters={
                    "type": "object",
                    "properties": {}
                },
                category="git"
            ),
            self._handle_git_status
        )

        # 10. Web Search
        self.register_tool(
            ToolDefinition(
                name="web_search",
                description="Searches the live web and Wikipedia to retrieve real-time facts, current news, latest documentation, or recent data. Use this when answering questions about events, modern library versions, or information not present in the local training data.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "The search query keywords"},
                        "max_results": {"type": "integer", "description": "Number of results to retrieve (default 5, max 10)"}
                    },
                    "required": ["query"]
                },
                category="web",
                is_external=True
            ),
            self._handle_web_search
        )

        # 11. Fetch Web Page Content
        self.register_tool(
            ToolDefinition(
                name="fetch_web_page",
                description="Fetches and extracts clean readable text content from a web URL for deep analysis or citation.",
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Full HTTP or HTTPS web page URL to fetch"}
                    },
                    "required": ["url"]
                },
                category="web",
                is_external=True
            ),
            self._handle_fetch_web_page
        )

        # 12. Lenovo Tips & Resources Knowledge Base Search
        self.register_tool(
            ToolDefinition(
                name="search_lenovo_kb",
                description=(
                    "Searches the internal Lenovo 'Tips & Resources' knowledge base "
                    "(AP Known Issues, Known Tips, LEAP Tips, Thursday Tech Tips, and Part Removal Videos) "
                    "by model name, 4-character Machine Type (MT), FRU/PD/APTT code, or hardware/driver symptom. "
                    "ONLY call this tool when the user explicitly asks for known tips, known issues, KB records, "
                    "or part removal videos—do NOT call it for general tasks such as drafting emails or writing."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Model name, MT code, part/FRU, or symptom keywords"},
                        "limit": {"type": "integer", "description": "Maximum number of KB matches to return (default 6, max 15)"},
                        "confirmed_only": {"type": "boolean", "description": "Set true to only match Confirmed QI issues"},
                    },
                    "required": ["query"],
                },
                category="knowledge_base",
                is_external=False,
            ),
            self._handle_search_lenovo_kb,
        )

    # Handlers
    async def _handle_calculator(self, args: Dict[str, Any]) -> Dict[str, Any]:
        expr = args.get("expression", "")
        parsed = ast.parse(expr, mode='eval')
        val = safe_eval_math(parsed.body)
        return {"expression": expr, "result": val}

    async def _handle_datetime(self, args: Dict[str, Any]) -> Dict[str, Any]:
        now = datetime.datetime.now()
        return {
            "iso": now.isoformat(),
            "formatted": now.strftime("%A, %B %d, %Y %I:%M:%S %p"),
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S")
        }

    async def _handle_read_file(self, args: Dict[str, Any]) -> Dict[str, Any]:
        rel_path = args.get("path", "")
        root = self.get_workspace_root()
        resolved = resolve_and_validate_path(rel_path, workspace_root=root)
        validate_file_size(resolved)
        content = resolved.read_text(encoding="utf-8", errors="replace")
        return {
            "path": str(resolved.relative_to(root)),
            "content": content,
            "size_bytes": len(content.encode("utf-8")),
            "lines": len(content.splitlines())
        }

    async def _handle_search_files(self, args: Dict[str, Any]) -> Dict[str, Any]:
        pattern = args.get("pattern", "*")
        content_search = args.get("content_search")
        root = self.get_workspace_root()
        matches = []

        for p in root.rglob(pattern):
            if p.is_file():
                rel = str(p.relative_to(root))
                # Skip hidden/git folders
                if any(part.startswith(".") for part in p.parts):
                    continue
                if content_search:
                    try:
                        text = p.read_text(encoding="utf-8", errors="ignore")
                        if content_search.lower() in text.lower():
                            matches.append({"path": rel, "matched_content": True})
                    except Exception:
                        pass
                else:
                    matches.append({"path": rel, "size_bytes": p.stat().st_size})

        return {"matches": matches[:100], "total_matches": len(matches)}

    async def _handle_write_file(self, args: Dict[str, Any]) -> Dict[str, Any]:
        rel_path = args.get("path", "")
        content = args.get("content", "")
        root = self.get_workspace_root()
        resolved = resolve_and_validate_path(rel_path, workspace_root=root, allow_creation=True)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return {
            "path": str(resolved.relative_to(root)),
            "bytes_written": len(content.encode("utf-8")),
            "status": "written"
        }

    async def _handle_validate_json(self, args: Dict[str, Any]) -> Dict[str, Any]:
        raw = args.get("json_string", "")
        try:
            parsed = json.loads(raw)
            formatted = json.dumps(parsed, indent=2)
            return {"valid": True, "formatted": formatted, "type": type(parsed).__name__}
        except json.JSONDecodeError as err:
            return {"valid": False, "error": f"Line {err.lineno}, Col {err.colno}: {err.msg}"}

    async def _handle_extract_document(self, args: Dict[str, Any]) -> Dict[str, Any]:
        rel_path = args.get("path", "")
        root = self.get_workspace_root()
        resolved = resolve_and_validate_path(rel_path, workspace_root=root)
        validate_file_size(resolved)
        text = resolved.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        preview = lines[:20]
        return {
            "path": str(resolved.relative_to(root)),
            "total_lines": len(lines),
            "total_words": len(text.split()),
            "preview_lines": preview
        }

    async def _handle_run_tests(self, args: Dict[str, Any]) -> Dict[str, Any]:
        framework = args.get("test_framework", "pytest").strip()
        allowed = ["pytest", "python -m unittest", "npm test"]
        if framework not in allowed:
            raise ValueError(f"Command '{framework}' is not an approved test framework ({', '.join(allowed)}).")
        root = self.get_workspace_root()
        cmd_result = await run_command_safely(framework, cwd=root, timeout=60, approved=True)
        return cmd_result.to_dict()

    async def _handle_git_status(self, args: Dict[str, Any]) -> Dict[str, Any]:
        root = self.get_workspace_root()
        git_dir = root / ".git"
        if not git_dir.exists():
            return {"is_git_repo": False, "message": "Authorized workspace is not a Git repository."}
        
        status_res = await run_command_safely("git status --porcelain", cwd=root, timeout=15)
        diff_res = await run_command_safely("git diff", cwd=root, timeout=15)
        branch_res = await run_command_safely("git rev-parse --abbrev-ref HEAD", cwd=root, timeout=15)

        return {
            "is_git_repo": True,
            "branch": branch_res.stdout.strip() if branch_res.exit_code == 0 else "unknown",
            "modified": [line.strip() for line in status_res.stdout.splitlines() if line.strip()],
            "diff": diff_res.stdout[:5000] # return first 5000 chars of diff
        }

    async def _handle_web_search(self, args: Dict[str, Any]) -> Dict[str, Any]:
        query = str(args.get("query", "")).strip()
        max_results = int(args.get("max_results", 5))
        max_results = min(max(max_results, 1), 10)
        results = await web_search_service.search(query, max_results=max_results)
        return {
            "query": query,
            "total_results": len(results),
            "results": results,
            "formatted_context": web_search_service.format_search_context(query, results)
        }

    async def _handle_fetch_web_page(self, args: Dict[str, Any]) -> Dict[str, Any]:
        url = str(args.get("url", "")).strip()
        return await web_search_service.fetch_page(url)

    async def _handle_search_lenovo_kb(self, args: Dict[str, Any]) -> Dict[str, Any]:
        query = str(args.get("query", "")).strip()
        limit = min(max(int(args.get("limit", 6)), 1), 15)
        confirmed_only = bool(args.get("confirmed_only", False))
        matches = lenovo_kb_service.search(query=query, limit=limit, confirmed_only=confirmed_only)
        return {
            "query": query,
            "total_matches": len(matches),
            "results": matches,
            "formatted_context": lenovo_kb_service.format_context_for_llm(query, matches, max_items=limit),
        }

tool_registry = ToolRegistry()
