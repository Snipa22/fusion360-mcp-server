"""
Local write-through cache for Fusion 360 hub file listings.

``list_hub_files`` needs to answer instantly, but the Fusion cloud API
(``DataFolder.dataFiles``) is a network call that can take several seconds —
too slow for a 30s-timeout tool call, and never safe to run on a socket
thread. Instead we maintain a small JSON index on disk (``project_index.json``)
that's updated at well-defined moments (background crawl at startup,
``save_as``, ``save_document``) and served straight from disk on read.

This module holds the cache I/O + pure filtering/sorting logic and has
**no dependency on the ``adsk`` package**, so it can be imported and unit
tested without a running Fusion instance — unlike ``command_handler.py``,
which needs the real Fusion API to actually crawl a hub.
"""

from __future__ import annotations

import json
import os

# Resolves to <repo>/project_index.json regardless of CWD — this file lives
# at addon/server/hub_cache.py, so two levels up is the repo root.
_CACHE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "project_index.json"
)


def _empty_cache() -> dict:
    return {"hub": None, "last_updated": None, "projects": {}}


def _load_cache(path: str | None = None) -> dict:
    """Load cache from disk; return empty structure on miss or parse error."""
    path = path or _CACHE_PATH
    if not os.path.exists(path):
        return _empty_cache()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return _empty_cache()
    if not isinstance(data, dict):
        return _empty_cache()
    data.setdefault("hub", None)
    data.setdefault("last_updated", None)
    data.setdefault("projects", {})
    return data


def _save_cache(cache: dict, path: str | None = None) -> None:
    """Atomic write: write to a ``.tmp`` sibling then ``os.replace()`` into place."""
    path = path or _CACHE_PATH
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)
    os.replace(tmp_path, path)


def _upsert_file_entry(cache: dict, project_name: str, file_info: dict) -> None:
    """Insert or update (matched by ``name``) a file entry in *project_name*."""
    projects = cache.setdefault("projects", {})
    project = projects.setdefault(project_name, {"files": [], "last_crawled": None})
    files = project.setdefault("files", [])
    name = file_info.get("name")
    for existing in files:
        if existing.get("name") == name:
            existing.update(file_info)
            return
    files.append(dict(file_info))


def _collect_files(
    cache: dict, project_name: str | None = "Pinchy", search: str | None = None
) -> list[dict]:
    """Filter + sort files out of an already-loaded *cache*. Pure, no I/O."""
    projects = cache.get("projects", {})

    if project_name is None or project_name == "all":
        files = []
        for pname, pdata in projects.items():
            for entry in pdata.get("files", []):
                merged = dict(entry)
                merged["project"] = pname
                files.append(merged)
    else:
        pdata = projects.get(project_name, {})
        files = [dict(entry) for entry in pdata.get("files", [])]

    if search:
        needle = search.lower()
        files = [
            f
            for f in files
            if needle in (f.get("name") or "").lower()
            or needle in (f.get("description") or "").lower()
        ]

    files.sort(key=lambda f: f.get("last_modified") or "", reverse=True)
    return files


def _build_list_response(
    project_name: str = "Pinchy",
    search: str | None = None,
    path: str | None = None,
) -> dict:
    """Full ``list_hub_files`` response, given cache-file *path* (defaults to
    ``_CACHE_PATH``). Shared by the real handler and by tests."""
    path = path or _CACHE_PATH
    if not os.path.exists(path):
        return {
            "files": [],
            "project": project_name,
            "cached": False,
            "hint": "Cache not yet populated — background crawl running at startup",
        }
    cache = _load_cache(path)
    files = _collect_files(cache, project_name=project_name, search=search)
    return {
        "files": files,
        "project": project_name,
        "cached": True,
        "last_updated": cache.get("last_updated"),
    }
