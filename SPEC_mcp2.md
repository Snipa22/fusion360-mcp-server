# Spec: Migrate server.py from mcp 1.x decorator API to mcp 2.0.0

## Goal

Update `src/fusion360_mcp/server.py` and `pyproject.toml` to work with `mcp>=2.0.0`.
Also update `uv.lock` via `uv lock`. Do not touch any other files unless tests require it.

## What broke

`mcp 2.0.0` removed `Server.list_tools()`, `Server.call_tool()`, `Server.list_resources()`,
`Server.read_resource()`, `Server.list_resource_templates()`, `Server.list_prompts()`, and
`Server.get_prompt()` decorators from `mcp.server.lowlevel.Server`.

The new API is `mcp.server.MCPServer` (imported from `mcp.server`) which takes tool/resource/
prompt registrations via `add_request_handler` or by passing them at construction time.

## What the current server.py does

`server.py` uses `mcp.server.lowlevel.Server` with 6 decorator-registered handlers:
1. `@app.list_tools()` → returns `get_tool_list()`
2. `@app.call_tool()` → dispatches to `_send()`, formats result with `_format_result()`
3. `@app.list_resources()` → returns 3 hardcoded resources
4. `@app.read_resource()` → switches on URI, calls `_send()` for fusion360://status etc.
5. `@app.list_resource_templates()` → returns 1 template
6. `@app.list_prompts()` / `@app.get_prompt()` → hardcoded prompts

At the bottom it runs via `mcp.server.stdio.stdio_server` context manager.

## How to migrate

### 1. Check what mcp 2.0.0 actually provides

Run this to understand the new API before writing any code:

```bash
uvx --with "mcp==2.0.0" python3 -c "
from mcp.server import MCPServer
from mcp.server.lowlevel import Server
import inspect
# Check what request methods are available on MCPServer
print('MCPServer methods:', [m for m in dir(MCPServer) if not m.startswith('_')])
# Check add_request_handler signature
print()
print(inspect.getsource(Server.add_request_handler))
"
```

Also check what request type strings to use:
```bash
uvx --with "mcp==2.0.0" python3 -c "
import mcp.types as types
# The method strings for handlers
print([x for x in dir(types) if 'Request' in x or 'List' in x])
"
```

Also check if stdio_server still exists:
```bash
uvx --with "mcp==2.0.0" python3 -c "
from mcp.server.stdio import stdio_server
print('stdio_server still exists')
" 2>&1
```

### 2. Rewrite server.py

Based on what you find above, rewrite `src/fusion360_mcp/server.py` to use the mcp 2.0.0 API.

The key logic that must be preserved exactly:
- `_send()` function — routes to mock or real TCP connection
- `_format_result()` function — formats tool results as TextContent/ImageContent
- `main()` click command with `--mode`, `--host`, `--port` options
- The stdio transport (however it's done in 2.0.0)
- Error handling: connection failures return `isError=True` with the current message text

The decorator registrations just need to be rewritten to whatever the 2.0.0 equivalent is.
The BEHAVIOR must be identical — same tools, same resources, same prompts.

If `mcp.server.MCPServer` is easier than `mcp.server.lowlevel.Server` + `add_request_handler`,
use it. If the `lowlevel.Server` still works with `add_request_handler` for all 6 handler types,
use that. Use whichever is cleaner. Confirm by actually running the code, not just reading docs.

### 3. Update pyproject.toml

Change:
```
"mcp>=1.0.0,<2.0.0"
```
to:
```
"mcp>=2.0.0"
```

### 4. Update uv.lock

```bash
cd /home/impala/services/yshtola/fusion360-mcp-server
uv lock
```

### 5. Run tests

```bash
uv run pytest tests/ -x -q 2>&1 | tail -20
```

Fix any failures. The tests use mocks and don't require Fusion running.
If tests import from mcp and break, fix those imports too.

### 6. Smoke test

```bash
uv run python3 -c "
import asyncio
from fusion360_mcp.server import main
print('import OK')
" 2>&1
```

Should import without error.

Also verify the server starts and lists tools in mock mode:
```bash
echo '{"jsonrpc":"2.0","method":"initialize","id":1,"params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1"}}}' | \
  timeout 3 uv run fusion360-mcp-server --mode mock 2>/dev/null | head -5 || true
```

## Commit

Single commit: `feat(server): migrate to mcp 2.0.0 API`

## Do NOT push

Yshtola will review the diff and push.
