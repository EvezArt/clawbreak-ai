"""ClawBreak - Tool execution engine."""
import subprocess
import shlex
import json
import os
import time
import httpx
from pathlib import Path

class ToolEngine:
    def __init__(self, config, memory):
        self.config = config
        self.memory = memory
        self.tools = {
            "shell": self._tool_shell,
            "search": self._tool_search,
            "memory": self._tool_memory,
            "file": self._tool_file,
            "sysinfo": self._tool_sysinfo,
        }

    def get_tool_descriptions(self):
        """Returns tool descriptions for the system prompt."""
        descs = []
        for name, fn in self.tools.items():
            descs.append(f"- {name}: {fn.__doc__.strip()}")
        return "\n".join(descs)

    def execute(self, tool_name, args):
        """Execute a tool and return the result."""
        if tool_name not in self.tools:
            return {"error": f"Unknown tool: {tool_name}. Available: {list(self.tools.keys())}"}

        try:
            result = self.tools[tool_name](**args if isinstance(args, dict) else {"input": str(args)})
            return {"result": result}
        except Exception as e:
            return {"error": str(e)}

    def _tool_shell(self, command="", timeout=None):
        """Run a restricted, read-only command. Arbitrary shell execution is disabled."""
        timeout = min(int(timeout or self.config.get("tools", "shell_timeout") or 30), 30)
        if not isinstance(command, str) or not command.strip():
            return "Error: command required"
        if any(op in command for op in (";", "|", "&", ">", "<", "`", "$", "\n", "\r")):
            return "Blocked: shell operators and expansions are not allowed"
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            return f"Blocked: invalid command syntax: {exc}"
        if not argv:
            return "Error: command required"
        if argv[0] == "pwd":
            if len(argv) != 1:
                return "Blocked: pwd takes no arguments"
        elif argv[0] == "ls":
            root = Path(os.environ.get("CLAWBREAK_WORKSPACE", os.getcwd())).expanduser().resolve()
            for arg in argv[1:]:
                if arg.startswith("-"):
                    if arg not in {"-l", "-a", "-la", "-al", "--all"}:
                        return "Blocked: unsupported ls option"
                else:
                    target = Path(arg).expanduser()
                    if not target.is_absolute():
                        target = root / target
                    target = target.resolve()
                    if target != root and root not in target.parents:
                        return "Blocked: ls path is outside the workspace"
        elif argv[0] == "git" and len(argv) >= 2 and argv[1] in {"status", "diff"}:
            if any(arg.startswith("-") and arg not in {"--short", "--porcelain", "--stat", "--name-only", "--check", "--cached"} for arg in argv[2:]):
                return "Blocked: unsupported git option"
            if any(not arg.startswith("-") for arg in argv[2:]):
                return "Blocked: git path/ref arguments are not allowed"
        else:
            return "Blocked: allowed commands are pwd, ls, git status, git diff"
        try:
            result = subprocess.run(argv, shell=False, capture_output=True, text=True, timeout=timeout, cwd=str(Path(os.environ.get("CLAWBREAK_WORKSPACE", os.getcwd())).expanduser().resolve()))
            output = result.stdout
            if result.stderr:
                output += f"\nSTDERR: {result.stderr}"
            if result.returncode != 0:
                output += f"\nExit code: {result.returncode}"
            return output[:10000]
        except subprocess.TimeoutExpired:
            return f"Command timed out after {timeout}s"
        except Exception as exc:
            return f"Error: {exc}"

    def _tool_search(self, query="", categories="general", engines="", limit=5):
        """Search the web via SearXNG. Args: query (str), categories (str), limit (int)"""
        searx_url = self.config.get("tools", "searxng_url")
        params = {"q": query, "format": "json", "categories": categories}
        if engines:
            params["engines"] = engines

        try:
            resp = httpx.get(f"{searx_url}/search", params=params, timeout=10)
            data = resp.json()
            results = data.get("results", [])[:limit]
            if not results:
                return f"No results for: {query}"
            output = []
            for r in results:
                output.append(f"## {r.get('title','')}\n{r.get('url','')}\n{r.get('content','')[:200]}\n")
            return "\n".join(output)
        except Exception as e:
            return f"Search error: {e}"

    def _tool_memory(self, action="recall", key="", value="", query="", category="general"):
        """Store or recall facts. Args: action (store|recall|list|delete), key, value, query, category"""
        if action == "store":
            if not key or not value:
                return "Error: key and value required for store"
            self.memory.store_fact(key, value, category)
            return f"Stored: {key} = {value[:100]}"
        elif action == "recall":
            if not query:
                return "Error: query required for recall"
            facts = self.memory.recall_facts(query)
            if not facts:
                return f"No facts matching: {query}"
            return "\n".join(f"- {f['key']}: {f['value'][:100]} [{f['category']}]" for f in facts)
        elif action == "list":
            facts = self.memory.list_facts(category if category != "general" else None)
            if not facts:
                return "No facts stored"
            return "\n".join(f"- {f['key']}: {f['value'][:80]} [{f['category']}] accessed {f['access_count']}x" for f in facts)
        elif action == "delete":
            if not key:
                return "Error: key required for delete"
            self.memory.delete_fact(key)
            return f"Deleted: {key}"
        else:
            return f"Unknown action: {action}. Use: store, recall, list, delete"

    def _workspace_root(self):
        return Path(os.environ.get("CLAWBREAK_WORKSPACE", os.getcwd())).expanduser().resolve()

    def _resolve_workspace_path(self, raw_path="", allow_root=False):
        root = self._workspace_root()
        candidate = Path(os.path.expanduser(raw_path))
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve()
        if candidate == root and allow_root:
            return candidate
        if candidate == root or root not in candidate.parents:
            raise PermissionError("Path is outside the configured workspace")
        return candidate

    def _tool_file(self, action="read", path="", content=""):
        """Read or write files only inside CLAWBREAK_WORKSPACE (defaults to current directory)."""
        if action == "read":
            if not path:
                return "Error: path required"
            try:
                return self._resolve_workspace_path(path).read_text()[:10000]
            except Exception as e:
                return f"Read error: {e}"
        elif action == "write":
            if not path or not isinstance(content, str):
                return "Error: path and text content required"
            try:
                p = self._resolve_workspace_path(path)
                p.parent.mkdir(parents=True, exist_ok=True)
                p = self._resolve_workspace_path(str(p))
                p.write_text(content)
                return f"Written {len(content)} chars to {p}"
            except Exception as e:
                return f"Write error: {e}"
        elif action == "list":
            try:
                target = self._resolve_workspace_path(path, allow_root=not bool(path)) if path else self._workspace_root()
                if target.is_dir():
                    items = list(target.iterdir())[:50]
                    return "\\n".join(f"{'DIR' if i.is_dir() else 'FILE'} {i.name}" for i in items)
                return f"Not a directory: {path}"
            except Exception as e:
                return f"List error: {e}"
        return f"Unknown action: {action}"

    def _tool_sysinfo(self):
        """Get system information. No args needed."""
        import platform
        try:
            mem_info = subprocess.run(["free", "-h"], capture_output=True, text=True, timeout=5).stdout
        except:
            mem_info = "unavailable"
        try:
            disk_info = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=5).stdout
        except:
            disk_info = "unavailable"

        return f"""System: {platform.system()} {platform.release()}
Machine: {platform.machine()}
Python: {platform.python_version()}
CPU count: {os.cpu_count()}
PID: {os.getpid()}
Uptime: {subprocess.run(['uptime', '-p'], capture_output=True, text=True, timeout=5).stdout.strip()}

Memory:
{mem_info}

Disk:
{disk_info}"""

    def parse_tool_calls(self, text):
        """Parse tool calls from LLM response text. Supports [tool:name(args)] syntax."""
        import re
        calls = []
        pattern = r'\[tool:(\w+)\((.*?)\)\]'
        for match in re.finditer(pattern, text):
            tool_name = match.group(1)
            args_str = match.group(2)
            try:
                # Try parsing as JSON
                args = json.loads(args_str)
            except:
                # Fall back to simple key=value parsing
                args = {"input": args_str}
            calls.append({"name": tool_name, "args": args})
        return calls
