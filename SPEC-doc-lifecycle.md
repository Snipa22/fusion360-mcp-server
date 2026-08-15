# Spec: open_document, close_document, delete_document

## Context

`list_hub_files` now returns the full Pinchy project index with name + id.
These three tools complete the document lifecycle: open from hub, close,
and delete from hub.

---

## Tool 1: `open_document`

### Handler: `command_handler.py`

```python
def open_document(
    self,
    name: str,
    project_name: str = "Pinchy",
) -> dict:
```

**Logic:**
1. Load cache via `hub_cache._load_cache()`.
2. Look up `name` in `cache["projects"][project_name]["files"]` to get the `id`
   (a `urn:adsk.wipprod:...` string).
3. If not found in cache, do a live crawl via `_crawl_project_main_thread` first,
   then retry the lookup. If still not found, raise `RuntimeError` with available names.
4. Resolve the `DataFile` object:
   ```python
   hub = self.app.data.activeHub
   data_object = self.app.data.findObjectById(file_id)
   data_file = adsk.core.DataFile.cast(data_object)
   ```
5. Open: `doc = self.app.documents.open(data_file)` — this makes it visible and active.
6. Return `{"opened": True, "name": doc.name, "project": project_name}`.

**Error cases:**
- File not found in hub: raise with list of available names.
- `app.documents.open` returns None: raise `RuntimeError("Failed to open document — Fusion returned null")`.

### Schema: `tools.py`

```python
{
    "name": "open_document",
    "title": "Open Document",
    "description": (
        "Open a saved document from the hub by name. Looks the file up in the "
        "local cache (project_name defaults to 'Pinchy') and opens it in Fusion. "
        "The opened document becomes the active document."
    ),
    "inputSchema": {
        "type": "object",
        "required": ["name"],
        "properties": {
            "name": {"type": "string", "description": "Exact document name as shown in list_hub_files"},
            "project_name": {
                "type": "string",
                "default": "Pinchy",
                "description": "Hub project containing the document",
            },
        },
    },
},
```

Add `"open_document"` to `_IDEMPOTENT` (opening an already-open doc is safe).

---

## Tool 2: `close_document`

### Handler

```python
def close_document(
    self,
    name: str,
    save: bool = False,
) -> dict:
```

**Logic:**
1. Iterate `self.app.documents` to find a document whose `.name == name`.
2. If not found: raise `RuntimeError(f"Document '{name}' is not open. Open documents: [...]")`.
3. Call `doc.close(save)` — `save=True` saves before closing, `save=False` discards unsaved changes.
4. Return `{"closed": True, "name": name, "saved": save}`.

**Note:** If `save=True` and the document was never saved (unsaved new doc), `doc.close(True)`
may raise — catch and re-raise with a clear message: "Document has never been saved; use save_as first then close."

### Schema

```python
{
    "name": "close_document",
    "title": "Close Document",
    "description": (
        "Close a currently open document by name. "
        "Pass save=true to save before closing; save=false (default) discards unsaved changes."
    ),
    "inputSchema": {
        "type": "object",
        "required": ["name"],
        "properties": {
            "name": {"type": "string", "description": "Name of the open document to close"},
            "save": {
                "type": "boolean",
                "default": False,
                "description": "If true, save the document before closing",
            },
        },
    },
},
```

---

## Tool 3: `delete_document`

### Handler

```python
def delete_document(
    self,
    name: str,
    project_name: str = "Pinchy",
) -> dict:
```

**Logic:**
1. Load cache, look up `id` for `name` in `project_name` (same cache lookup as `open_document`).
2. If not in cache, live crawl + retry. If still not found, raise with available names.
3. Resolve `DataFile` via `app.data.findObjectById(file_id)`.
4. Safety check: `data_file.isInUse` — if True, raise `RuntimeError(f"Cannot delete '{name}': document is currently open. Close it first.")`.
5. Call `data_file.deleteMe()`.
6. Evict from cache: remove the entry from `cache["projects"][project_name]["files"]`
   where `entry["name"] == name`, then `hub_cache._save_cache(cache)`.
7. Return `{"deleted": True, "name": name, "project": project_name}`.

### Schema

```python
{
    "name": "delete_document",
    "title": "Delete Document",
    "description": (
        "Permanently delete a document from the hub. "
        "The document must not be currently open (close it first). "
        "This is irreversible — the file is deleted from Autodesk's cloud."
    ),
    "inputSchema": {
        "type": "object",
        "required": ["name"],
        "properties": {
            "name": {"type": "string", "description": "Exact document name to delete"},
            "project_name": {
                "type": "string",
                "default": "Pinchy",
                "description": "Hub project containing the document",
            },
        },
    },
},
```

Add `"delete_document"` to `_DESTRUCTIVE`.

---

## Helper: `_find_data_file(app, file_id)`

Factor out the `findObjectById` + cast pattern used by both `open_document`
and `delete_document`:

```python
def _find_data_file(app, file_id: str):
    """Resolve a hub file id to a DataFile object."""
    obj = app.data.findObjectById(file_id)
    if obj is None:
        raise RuntimeError(f"Hub file id '{file_id}' not found (may have been deleted from hub)")
    return adsk.core.DataFile.cast(obj)
```

---

## Dispatch table

Add to `_COMMANDS`:
```python
"open_document": self.open_document,
"close_document": self.close_document,
"delete_document": self.delete_document,
```

---

## Mock handlers (`mock.py`)

Add mock entries following the existing pattern:
- `open_document` → `{"ok": True, "opened": True, "name": params.get("name", "MockDoc"), "project": params.get("project_name", "Pinchy")}`
- `close_document` → `{"ok": True, "closed": True, "name": params.get("name", "MockDoc"), "saved": params.get("save", False)}`
- `delete_document` → `{"ok": True, "deleted": True, "name": params.get("name", "MockDoc"), "project": params.get("project_name", "Pinchy")}`

---

## Tests (`tests/test_doc_lifecycle.py` — new file)

Use the same mock pattern as existing tests. Since handlers require `adsk`,
test the pure logic only:

1. `test_open_document_schema_present` — tool appears in get_tool_list()
2. `test_close_document_schema_present` — tool appears in get_tool_list()
3. `test_delete_document_schema_present` — tool appears in get_tool_list()
4. `test_open_document_mock` — mock returns opened=True with correct name
5. `test_close_document_mock` — mock returns closed=True
6. `test_delete_document_mock` — mock returns deleted=True
7. `test_open_document_required_name` — schema has "name" in required
8. `test_delete_document_in_destructive` — "delete_document" in _DESTRUCTIVE set

---

## Files to modify

- `addon/server/command_handler.py` — add `_find_data_file`, 3 new handlers, dispatch entries
- `src/fusion360_mcp/tools.py` — add 3 schemas, update annotation sets
- `src/fusion360_mcp/mock.py` — add 3 mock handlers
- `tests/test_doc_lifecycle.py` — new file, 8 tests

## Branch

`snipa/new-features`. Keep all 319 existing tests passing.
Commit and push to `origin snipa/new-features` when done.
