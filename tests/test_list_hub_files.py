"""Tests for the `list_hub_files` write-through project index cache.

The actual `CommandHandler.list_hub_files` method lives in
`addon/server/command_handler.py`, which imports the real `adsk` package
and can't be instantiated outside Fusion. All of its behavior — reading
the cache file, filtering, sorting, upserting — is implemented as pure,
adsk-free functions in `addon/server/hub_cache.py` (which
`command_handler.py` imports and delegates to), so we exercise that
module directly here.
"""

from __future__ import annotations

import json

from addon.server import hub_cache


def _write_cache(path, cache: dict) -> None:
    path.write_text(json.dumps(cache), encoding="utf-8")


def test_list_hub_files_empty_cache(tmp_path):
    """Missing cache file → empty list + cached: False, with a helpful hint."""
    missing_path = tmp_path / "project_index.json"
    assert not missing_path.exists()

    result = hub_cache._build_list_response(
        project_name="Pinchy", path=str(missing_path)
    )

    assert result["files"] == []
    assert result["project"] == "Pinchy"
    assert result["cached"] is False
    assert "hint" in result
    assert "not yet populated" in result["hint"]


def test_list_hub_files_returns_pinchy_files(tmp_path):
    """A populated cache should return the matching project's files as-is."""
    cache_path = tmp_path / "project_index.json"
    _write_cache(
        cache_path,
        {
            "hub": "Snowdozer Racing",
            "last_updated": "2026-08-15T14:30:00",
            "projects": {
                "Pinchy": {
                    "files": [
                        {
                            "name": "Coupon Holder v3",
                            "id": "abc-123",
                            "version": 3,
                            "last_modified": "2026-08-10T09:12:00",
                            "description": "PLA test print revision",
                        }
                    ],
                    "last_crawled": "2026-08-15T14:30:00",
                },
                "OtherProject": {
                    "files": [
                        {
                            "name": "Unrelated Part",
                            "id": "zzz-999",
                            "version": 1,
                            "last_modified": "2026-08-01T00:00:00",
                            "description": "",
                        }
                    ],
                    "last_crawled": "2026-08-15T14:30:00",
                },
            },
        },
    )

    result = hub_cache._build_list_response(
        project_name="Pinchy", path=str(cache_path)
    )

    assert result["cached"] is True
    assert result["project"] == "Pinchy"
    assert result["last_updated"] == "2026-08-15T14:30:00"
    assert len(result["files"]) == 1
    assert result["files"][0]["name"] == "Coupon Holder v3"
    assert result["files"][0]["id"] == "abc-123"


def test_list_hub_files_search(tmp_path):
    """search= should case-insensitively filter on name and description."""
    cache_path = tmp_path / "project_index.json"
    _write_cache(
        cache_path,
        {
            "hub": "Snowdozer Racing",
            "last_updated": "2026-08-15T14:30:00",
            "projects": {
                "Pinchy": {
                    "files": [
                        {
                            "name": "Coupon Holder v3",
                            "id": "abc-123",
                            "version": 3,
                            "last_modified": "2026-08-10T09:12:00",
                            "description": "PLA test print revision",
                        },
                        {
                            "name": "Bracket v1",
                            "id": "def-456",
                            "version": 1,
                            "last_modified": "2026-08-09T00:00:00",
                            "description": "Mounting bracket",
                        },
                    ],
                    "last_crawled": "2026-08-15T14:30:00",
                }
            },
        },
    )

    result = hub_cache._build_list_response(
        project_name="Pinchy", search="coupon", path=str(cache_path)
    )

    assert len(result["files"]) == 1
    assert result["files"][0]["name"] == "Coupon Holder v3"

    # description match, case-insensitive
    result2 = hub_cache._build_list_response(
        project_name="Pinchy", search="BRACKET", path=str(cache_path)
    )
    assert len(result2["files"]) == 1
    assert result2["files"][0]["name"] == "Bracket v1"


def test_list_hub_files_all_projects(tmp_path):
    """project_name='all' should merge files from every project, tagged."""
    cache_path = tmp_path / "project_index.json"
    _write_cache(
        cache_path,
        {
            "hub": "Snowdozer Racing",
            "last_updated": "2026-08-15T14:30:00",
            "projects": {
                "Pinchy": {
                    "files": [
                        {
                            "name": "Coupon Holder v3",
                            "id": "abc-123",
                            "version": 3,
                            "last_modified": "2026-08-10T09:12:00",
                            "description": "PLA test print revision",
                        }
                    ],
                    "last_crawled": "2026-08-15T14:30:00",
                },
                "OtherProject": {
                    "files": [
                        {
                            "name": "Unrelated Part",
                            "id": "zzz-999",
                            "version": 1,
                            "last_modified": "2026-08-12T00:00:00",
                            "description": "",
                        }
                    ],
                    "last_crawled": "2026-08-15T14:30:00",
                },
            },
        },
    )

    result = hub_cache._build_list_response(project_name="all", path=str(cache_path))

    assert result["cached"] is True
    names = {f["name"] for f in result["files"]}
    assert names == {"Coupon Holder v3", "Unrelated Part"}
    projects = {f["project"] for f in result["files"]}
    assert projects == {"Pinchy", "OtherProject"}
    # sorted by last_modified descending
    assert result["files"][0]["name"] == "Unrelated Part"


def test_upsert_file_entry_insert():
    """Upserting a new name should append a new entry."""
    cache = {"hub": None, "last_updated": None, "projects": {}}

    hub_cache._upsert_file_entry(
        cache,
        "Pinchy",
        {
            "name": "New Part",
            "id": "new-1",
            "version": 1,
            "last_modified": "2026-08-15T10:00:00",
            "description": "first save",
        },
    )

    files = cache["projects"]["Pinchy"]["files"]
    assert len(files) == 1
    assert files[0]["name"] == "New Part"
    assert files[0]["version"] == 1


def test_upsert_file_entry_update():
    """Upserting an existing name should update it in place, not duplicate."""
    cache = {
        "hub": None,
        "last_updated": None,
        "projects": {
            "Pinchy": {
                "files": [
                    {
                        "name": "Coupon Holder v3",
                        "id": "abc-123",
                        "version": 3,
                        "last_modified": "2026-08-10T09:12:00",
                        "description": "PLA test print revision",
                    }
                ],
                "last_crawled": "2026-08-15T14:30:00",
            }
        },
    }

    hub_cache._upsert_file_entry(
        cache,
        "Pinchy",
        {
            "name": "Coupon Holder v3",
            "id": "abc-123",
            "version": 4,
            "last_modified": "2026-08-15T18:00:00",
            "description": "another revision",
        },
    )

    files = cache["projects"]["Pinchy"]["files"]
    assert len(files) == 1, "should update in place, not append a duplicate"
    assert files[0]["version"] == 4
    assert files[0]["last_modified"] == "2026-08-15T18:00:00"
    assert files[0]["description"] == "another revision"
