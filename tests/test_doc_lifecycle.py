"""Tests for open_document, close_document, delete_document tools."""

from __future__ import annotations

from src.fusion360_mcp.mock import mock_command
from src.fusion360_mcp.tools import TOOLS, _DESTRUCTIVE, get_tool_list


def _tool_names() -> set[str]:
    return {t["name"] for t in TOOLS}


def test_open_document_schema_present():
    assert "open_document" in _tool_names()


def test_close_document_schema_present():
    assert "close_document" in _tool_names()


def test_delete_document_schema_present():
    assert "delete_document" in _tool_names()


def test_open_document_required_name():
    t = next(t for t in TOOLS if t["name"] == "open_document")
    assert "name" in t["inputSchema"]["required"]


def test_close_document_required_name():
    t = next(t for t in TOOLS if t["name"] == "close_document")
    assert "name" in t["inputSchema"]["required"]


def test_delete_document_required_name():
    t = next(t for t in TOOLS if t["name"] == "delete_document")
    assert "name" in t["inputSchema"]["required"]


def test_delete_document_in_destructive():
    assert "delete_document" in _DESTRUCTIVE


def test_open_document_mock():
    result = mock_command("open_document", {"name": "MyDesign", "project_name": "Pinchy"})
    assert result["opened"] is True
    assert result["name"] == "MyDesign"
    assert result["project"] == "Pinchy"


def test_close_document_mock_no_save():
    result = mock_command("close_document", {"name": "MyDesign"})
    assert result["closed"] is True
    assert result["saved"] is False


def test_close_document_mock_with_save():
    result = mock_command("close_document", {"name": "MyDesign", "save": True})
    assert result["closed"] is True
    assert result["saved"] is True


def test_delete_document_mock():
    result = mock_command("delete_document", {"name": "OldDesign", "project_name": "Pinchy"})
    assert result["deleted"] is True
    assert result["name"] == "OldDesign"
    assert result["project"] == "Pinchy"


def test_open_document_in_tool_list():
    names = {t.name for t in get_tool_list()}
    assert "open_document" in names
    assert "close_document" in names
    assert "delete_document" in names
