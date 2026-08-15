# Spec: `list_hub_files` tool — write-through project index

## Goal

Add a `list_hub_files` tool that returns saved files from the Fusion 360 hub
without making live cloud API calls (which time out at 30s).

The solution is a **write-through local cache**: a JSON file on disk that is
updated at the right moments and served instantly on demand.

---

## Architecture

### Cache file

```
C:\fusion360-mcp-src\project_index.json
```

Format:
```json
{
  "hub": "Snowdozer Racing",
  "last_updated": "2026-08-15T14:30:00",
  "projects": {
    "Pinchy": {
      "files": [
        {
          "name": "Coupon Holder v3",
          "id": "<dataFile.id>",
          "version": 3,
          "last_modified": "2026-08-10T09:12:00",
          "description": "PLA test print revision"
        }
      ],
      "last_crawled": "2026-08-15T14:30:00"
    }
  }
}
```

### Cache update triggers

1. **After `save_as`** — append the new file entry to the appropriate project in the cache
2. **After `save_document`** — update `last_modified` + increment version for the active doc's entry
3. **Background crawl at add-in startup** — crawl all projects in a background thread, update the full cache. Must NOT block the main thread. Use `threading.Thread(daemon=True)`. Log start/completion to `adsk.core.Application.get().log()`. Cap per-project timeout at 20s via `threading.Event`. Default project to crawl first: `"Pinchy"` (crawl it before others).

### Cache read

`list_hub_files` reads the JSON file directly — no cloud call, returns immediately.

---

## New tool: `list_hub_files`

### Handler method (addon/server/command_handler.py)

```python
def list_hub_files(
    self,
    project_name: str = "Pinchy",
    search: str = None,
) -> dict:
```

- Reads `project_index.json` from `C:\fusion360-mcp-src\project_index.json`
  (same dir as the add-in source, `os.path.dirname(__file__)` two levels up, or
  hardcode relative to `__file__` → `../../project_index.json`)
- If the file doesn't exist, return `{"files": [], "project": project_name, "cached": False, "hint": "Cache not yet populated — background crawl running at startup"}`
- If `project_name` is `None` or `"all"`, return files from all projects, each with a `project` field
- If `search` is provided, case-insensitive substring match on `name` and `description`
- Sort by `last_modified` descending
- Return: `{"files": [...], "project": project_name, "cached": True, "last_updated": "..."}`

### Schema (src/fusion360_mcp/tools.py)

Add to `TOOLS` list in the document-management section:

```python
{
    "name": "list_hub_files",
    "title": "List Hub Files",
    "description": (
        "List saved files in a Fusion 360 hub project from a local cache. "
        "Fast — no cloud API call. Cache is populated at add-in startup and "
        "updated after every save_as/save_document call. "
        "Default project is 'Pinchy'. Pass search= to filter by name."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "project_name": {
                "type": "string",
                "default": "Pinchy",
                "description": "Project name to list (default: 'Pinchy'). Pass 'all' for all projects.",
            },
            "search": {
                "type": "string",
                "description": "Optional case-insensitive substring filter on file name/description",
            },
        },
    },
},
```

Add `"list_hub_files"` to `_READ_ONLY` set.

---

## Cache management helpers (addon/server/command_handler.py)

Add a `_CacheManager` class (or module-level functions) responsible for:

```python
_CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "project_index.json")

def _load_cache() -> dict:
    """Load cache from disk; return empty structure on miss."""

def _save_cache(cache: dict) -> None:
    """Atomic write: write to .tmp then rename."""

def _upsert_file_entry(cache: dict, project_name: str, file_info: dict) -> None:
    """Insert or update a file entry by name in the given project."""

def _crawl_project(app, project_name: str, cache: dict, timeout_s: float = 20.0) -> int:
    """Crawl one project's rootFolder.dataFiles, update cache in-place. Returns file count."""

def _background_crawl(app) -> None:
    """Background thread: crawl 'Pinchy' first, then remaining projects."""
```

**Atomic write** is important — use write-to-`.tmp`-then-`os.replace()` to avoid
partial reads from the MCP server.

---

## Hook save_as and save_document

In `save_as`, after `doc.saveAs(...)` succeeds, call `_upsert_file_entry` with the
new file's name and description, then `_save_cache`.

In `save_document`, after `doc.save(...)` succeeds, update the `last_modified` timestamp
and bump version of the matching entry (match by `doc.name`), then `_save_cache`.

---

## Background crawl at startup

In `CommandHandler.__init__` (or wherever the add-in initializes the handler), start
the background thread:

```python
import threading
t = threading.Thread(target=_background_crawl, args=(self.app,), daemon=True)
t.start()
```

The daemon flag ensures it doesn't block Fusion shutdown.

---

## Dispatch table

Add to `execute_command`'s `_COMMANDS` dict:
```python
"list_hub_files": self.list_hub_files,
```

---

## Tests

Add to `tests/` (mock-based, consistent with existing test patterns):

1. `test_list_hub_files_empty_cache` — returns empty list + `cached: False` when file missing
2. `test_list_hub_files_returns_pinchy_files` — mock cache JSON on disk, verify filtered return
3. `test_list_hub_files_search` — verify `search="coupon"` filters correctly
4. `test_list_hub_files_all_projects` — verify `project_name="all"` merges all projects
5. `test_upsert_file_entry_insert` — new entry added
6. `test_upsert_file_entry_update` — existing entry updated (last_modified bumped)

---

## Constraints / pitfalls

- The `dataFiles` property on a Fusion `DataFolder` makes a network call — always in background thread, never in main thread during a tool call.
- `os.replace()` is atomic on Windows — use it for cache writes.
- Don't use `time.sleep()` for the per-project timeout; use `threading.Event.wait(timeout)` with the crawl running in a sub-thread.
- `_CACHE_PATH` should resolve relative to `__file__` so it works regardless of CWD.
- File `id` comes from `dataFile.id` (a string GUID) — include it for future `open_document` use.
- Version: use `dataFile.versionNumber` if available, else omit.
- The `last_modified` field: `dataFile.dateModified` returns a Python `datetime` — call `.isoformat()`.
- Keep existing 312 tests passing. New tests should follow the same mock pattern as `tests/test_commands.py`.

---

## Files to modify

- `addon/server/command_handler.py` — add cache helpers, `list_hub_files`, background crawl init, save_as/save_document hooks
- `src/fusion360_mcp/tools.py` — add `list_hub_files` tool schema + add to `_READ_ONLY`
- `tests/test_commands.py` (or new `tests/test_list_hub_files.py`) — 6 new tests

## Branch

Work on `snipa/new-features`. Do NOT touch `snipa/fixes-and-lan`.
Push to `origin snipa/new-features` when done (no force push needed, this branch diverges from main).
