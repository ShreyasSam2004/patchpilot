# Tools and MCP

## One interface

Everything the agent can do is a `Tool`:

```python
class Tool(ABC, Generic[ArgsT]):
    name: ClassVar[str]
    description: ClassVar[str]
    args_model: ClassVar[type[BaseModel]]   # doubles as the JSON schema
    origin: ClassVar[ToolOrigin]            # NATIVE | MCP
    timeout_s: ClassVar[float]
    mutating: ClassVar[bool]                # feeds the anti-loop detector

    async def execute(self, ctx: ToolContext, args: ArgsT) -> ToolResult: ...
```

`Tool.run()` wraps `execute()` and guarantees:

- **validation** — arguments are parsed by the Pydantic model; a bad call
  returns the schema in the error so the model can correct itself;
- **containment** — a raised `ToolError` becomes a failed `ToolResult`, and even
  an unexpected exception becomes `tool_exception` rather than killing the run;
- **timing** — every call is timed and the duration is recorded.

`ToolContext` carries exactly what a tool may touch: the workspace, the sandbox,
settings, the event emitter and shared run state. Nothing is reachable through
globals, so tools cannot acquire capabilities they were not given.

## The registry and the policy

```
                     Agent
                       │   sees only: name, description, JSON schema
                  ToolRegistry
                       │
                  ToolPolicy  ── deny beats allow; unlisted ⇒ refused
                  ╱          ╲
          NativeTool        McpTool ──► MCP client ──► server
```

`ToolRegistry.specs()` returns schemas for **allowed** tools only, so a denied
tool is not merely refused at call time — the model never learns it exists.
`ToolRegistry.inventory()` returns everything including denied tools, which is
what the Settings page renders.

### Who decides what

| Actor | Decides |
|---|---|
| MCP server | which tools *exist* |
| PatchPilot policy | which tools this agent is *allowed to call* |
| The model | which allowed tool to call *now* |

Discovery is not permission. A newly added MCP tool is registered, appears in
the inventory as denied, and cannot be reached until an operator adds it to the
allow-list.

### Configuring the policy

```bash
PATCHPILOT_TOOL_ALLOW=list_files,read_file,search_code,find_symbol,apply_patch,edit_file,write_file,git_diff,git_status,git_log,run_tests,run_command,record_hypothesis,finish
PATCHPILOT_TOOL_DENY=git_push,git_commit,delete_repository,host_shell,credential_access,network_fetch
```

Rules support `fnmatch` patterns (`git_*`), and deny always wins:

```bash
PATCHPILOT_TOOL_ALLOW=git_*
PATCHPILOT_TOOL_DENY=git_push,git_commit    # git_diff/status/log allowed, push/commit not
```

The default is **deny**. `ToolPolicy.permissive()` exists but is only ever
constructed by tests, never from configuration.

## Native tools

| Tool | Purpose | Mutating |
|---|---|---|
| `list_files` | Orient in the tree; skips `.git`, `node_modules`, `.venv`, `__pycache__` | |
| `read_file` | Read with line numbers; `start_line`/`end_line` for regions | |
| `search_code` | Literal or regex search, `file:line` results | |
| `find_symbol` | Where a symbol is *defined*, across 10 language patterns | |
| `apply_patch` | Apply a unified diff (preferred editor) | ✓ |
| `edit_file` | Exact unique-anchor string replacement | ✓ |
| `write_file` | Whole-file write (for genuinely new files) | ✓ |
| `git_diff` / `git_status` / `git_log` | Inspect the working tree and history | |
| `run_tests` | Run tests, parse the summary, extract the failure section | |
| `run_command` | Linter, type checker, build step, repro script | |
| `record_hypothesis` | Structured root-cause claim before editing | |
| `finish` | End the run — guarded against unearned success | |

`apply_patch` tries `git apply`, then `--3way`, then `-C1 --whitespace=fix`,
and turns the failure into actionable advice: a malformed `@@` count, a wrong
path, or stale context each produce a different hint.

`edit_file` exists because diff hunks with wrong line numbers are the single
most common way an LLM edit fails. A unique-anchor replacement sidesteps line
numbers without rewriting the file, and refuses ambiguous anchors rather than
guessing.

## Enabling MCP

```bash
pip install -e ".[mcp]"
PATCHPILOT_MCP_ENABLED=true
PATCHPILOT_MCP_CONFIG_PATH=./mcp.config.json
```

`mcp.config.json` uses the familiar `mcpServers` shape:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/repo"],
      "enabled": true
    }
  }
}
```

On each run PatchPilot connects over stdio, calls `list_tools`, and wraps every
discovered tool as an `McpTool`. Inspect the result without starting a run:

```bash
patchpilot tools list        # full registry with the policy verdict per tool
patchpilot tools mcp         # connect to each server and list what it offers
```

or `GET /api/mcp/probe` and `GET /api/mcp/tools`.

## MCP safety properties

- **Name collisions cannot shadow native tools.** An MCP `read_file` is
  re-registered as `mcp__<server>__read_file`; the native implementation keeps
  the plain name. It remains reachable, but only under a name that says where
  it came from — and only if policy allows it.
- **Server schemas are authoritative for their own tools.** PatchPilot builds a
  Pydantic model for validation but sends the server's own JSON schema to the
  model, and falls back to passing arguments through unvalidated rather than
  being stricter than the server.
- **Descriptions are untrusted input.** They are prefixed with the server name
  and truncated to 2000 characters.
- **Assumed mutating.** An unknown remote tool is treated as capable of changing
  the workspace, so the anti-loop detector treats repeats as real repeats.
- **A broken server degrades the tool set, never the run.** Connection failures
  are logged and skipped.
- **Environment values are never displayed.** The Settings page shows which
  environment *keys* a server receives; the values are how servers receive
  secrets and are not echoed anywhere.

## Adding a native tool

1. Subclass `Tool` with a Pydantic `args_model` and a real description — the
   description is the model's only documentation.
2. Add it to `build_native_tools()`.
3. Add its name to the default allow-list in `config.py`.

Step 3 is easy to forget, so a test enforces it:
`test_default_policy_permits_every_native_tool` fails if a shipped tool is
unreachable under the default policy.
