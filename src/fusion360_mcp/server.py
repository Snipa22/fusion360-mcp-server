"""
Fusion360 MCP Server — stdio transport.

Bridges Claude Code ↔ Fusion 360 add-in via TCP socket on localhost.
Supports ``--mode mock`` for testing without Fusion running.
"""

import json
import logging
import os
import re

import anyio
import click
import mcp.types as types
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict

from .connection import get_connection, reset_connection
from .mock import mock_command
from .tools import get_tool_by_name, get_tool_list


class _PassthroughArgModel(BaseModel):
    """Pydantic arg-model that accepts any kwargs and passes them through unchanged.

    The default MCPServer.add_tool() infers a schema from the handler's Python
    signature.  Our handlers use ``arguments: dict`` (a single opaque blob) so MCP
    would advertise ``{"arguments": {...}}`` to the LLM — forcing the LLM to nest all
    real params one level deeper.  We want the LLM to call e.g.
    ``extrude(height=5.0)`` directly, not ``extrude(arguments={"height": 5.0})``.

    The fix: after ``add_tool`` we (a) replace ``tool.parameters`` with the rich
    per-tool schema from tools.py so the LLM sees the correct fields, and (b) replace
    ``tool.fn_metadata.arg_model`` with this class so Pydantic accepts any kwargs and
    wraps them back into the ``arguments`` dict that the real handler expects.
    """

    model_config = ConfigDict(extra="allow")

    def model_dump_one_level(self) -> dict:  # called by FuncMetadata
        # Collect declared fields + extras → wrap as {'arguments': {...}}
        data: dict = {}
        for k in self.__class__.model_fields:
            data[k] = getattr(self, k)
        if self.__pydantic_extra__:
            data.update(self.__pydantic_extra__)
        return {"arguments": data}

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("fusion360_mcp.server")


def _send(
    mode: str,
    command_type: str,
    params: dict | None = None,
    *,
    host: str = "localhost",
    port: int = 9876,
) -> dict:
    """Route a command through either the real TCP connection or mock."""
    if mode == "mock":
        return mock_command(command_type, params)
    conn = get_connection(host=host, port=port)
    return conn.send_command(command_type, params)


# Fields surfaced at the top of the formatted text block.  Everything else
# in the result dict is rendered below as ``key: value`` pairs.
_SPECIAL_KEYS = {
    "ok",
    "error_kind",
    "error_message",
    "hints",
    "traceback",
    "deltas",
    "image_base64",
}


def _format_deltas(deltas: dict) -> list[str]:
    """Render the deltas sub-dict as indented bullet lines."""
    lines = ["  deltas:"]
    bc_before = deltas.get("body_count_before")
    bc_after = deltas.get("body_count_after")
    bc_delta = deltas.get("body_count_delta")
    if bc_before is not None or bc_after is not None:
        lines.append(f"    body_count: {bc_before} → {bc_after} (Δ{bc_delta:+d})")
    mg_before = deltas.get("mass_g_before")
    mg_after = deltas.get("mass_g_after")
    mg_delta = deltas.get("mass_g_delta")
    if mg_delta is not None:
        lines.append(
            f"    mass_g:     {mg_before:.3f} → {mg_after:.3f} (Δ{mg_delta:+.3f})"
        )
    if deltas.get("bbox_before") is not None:
        lines.append(f"    bbox_before: {deltas['bbox_before']}")
    if deltas.get("bbox_after") is not None:
        lines.append(f"    bbox_after:  {deltas['bbox_after']}")
    return lines


def _format_result(
    name: str,
    result: dict | object,
) -> list[types.ContentBlock]:
    """Render an addon/mock result into MCP content blocks.

    * Error responses (``ok: False``) → raises ToolError with formatted message.
    * ``render_view`` success → text metadata + ImageContent for the PNG.
    * Everything else → text block listing result fields (+ deltas if any).
    """
    # Non-dict fallback (shouldn't happen, but stay robust).
    if not isinstance(result, dict):
        return [types.TextContent(type="text", text=f"**{name}** OK\n  {result}")]

    # Error path (application-level failure classified by the addon).
    if result.get("ok") is False:
        lines = [
            f"**{name}** ERROR ({result.get('error_kind', 'UNKNOWN')})",
            f"  {result.get('error_message', '(no message)')}",
        ]
        hints = result.get("hints") or []
        if hints:
            lines.append("  hints:")
            lines.extend(f"    - {h}" for h in hints)
        tb = result.get("traceback")
        if tb:
            lines.append("")
            lines.append("traceback:")
            lines.append(tb)
        raise ToolError("\n".join(lines))

    # Success path.
    lines = [f"**{name}** OK"]
    for k, v in result.items():
        if k in _SPECIAL_KEYS:
            continue
        lines.append(f"  {k}: {v}")
    deltas = result.get("deltas")
    if isinstance(deltas, dict):
        lines.extend(_format_deltas(deltas))

    content: list[types.ContentBlock] = [
        types.TextContent(type="text", text="\n".join(lines)),
    ]

    # render_view: attach the PNG as an image block so vision models can see it.
    image_b64 = result.get("image_base64")
    if isinstance(image_b64, str) and image_b64:
        img_format = result.get("image_format", "png")
        content.append(
            types.ImageContent(
                type="image",
                data=image_b64,
                mime_type=f"image/{img_format}",
            )
        )
    return content


@click.command()
@click.option(
    "--mode",
    type=click.Choice(["socket", "mock"]),
    default="socket",
    help="'socket' connects to Fusion, 'mock' returns test data",
)
@click.option("--host", type=str,
              default=lambda: os.environ.get("FUSION_MCP_HOST", "localhost"),
              help=("Host where the Fusion 360 add-in listens "
                    "(env: FUSION_MCP_HOST). Use the Windows LAN IP when "
                    "the MCP server runs on a different machine."))
@click.option(
    "--port", type=int,
    default=lambda: int(os.environ.get("FUSION_MCP_PORT", "9876")),
    help="TCP port the Fusion 360 add-in listens on (env: FUSION_MCP_PORT)",
)
def main(mode: str, host: str, port: int) -> int:
    """Fusion360 MCP Server — connects Claude to Fusion 360."""

    app = MCPServer("fusion360-mcp-server")

    # ── tools ────────────────────────────────────────────────────────

    # Helper to create tool handlers with proper closure
    def make_tool_handler(tool_name: str):
        async def tool_handler(
            arguments: dict,
        ) -> list[types.ContentBlock]:
            try:
                result = _send(mode, tool_name, arguments, host=host, port=port)
            except Exception as exc:
                reset_connection()
                raise ToolError(
                    f"Error ({tool_name}): {exc}\n\n"
                    "Make sure Fusion 360 is running and the "
                    "Fusion360MCP add-in is started."
                )

            return _format_result(tool_name, result)
        return tool_handler

    # Register all tools from the tool registry.
    #
    # After add_tool() we patch each registered Tool object so the LLM receives
    # the rich per-tool schema from tools.py instead of the generic
    # ``{"arguments": dict}`` schema that MCPServer would infer from our handler
    # signature.  See _PassthroughArgModel for the full explanation.
    for tool_def in get_tool_list():
        app.add_tool(
            make_tool_handler(tool_def.name),
            name=tool_def.name,
            description=tool_def.description,
            annotations=tool_def.annotations if hasattr(tool_def, "annotations") else None,
        )
        # Inject the correct per-tool input schema.
        registered = app._tool_manager.get_tool(tool_def.name)
        if registered is not None:
            registered.parameters = tool_def.input_schema
            registered.fn_metadata.arg_model = _PassthroughArgModel

    # ── resources ────────────────────────────────────────────────────

    @app.resource("fusion360://status")
    async def read_status() -> str:
        try:
            result = _send(mode, "ping", host=host, port=port)
            return json.dumps(
                {"connected": True, "ping": result},
                indent=2,
            )
        except Exception as exc:
            reset_connection()
            return json.dumps(
                {"connected": False, "error": str(exc)},
                indent=2,
            )

    @app.resource("fusion360://design")
    async def read_design() -> str:
        try:
            result = _send(mode, "get_scene_info", host=host, port=port)
            return json.dumps(result, indent=2)
        except Exception as exc:
            reset_connection()
            return json.dumps({"error": str(exc)}, indent=2)

    @app.resource("fusion360://parameters")
    async def read_parameters() -> str:
        try:
            result = _send(
                mode,
                "get_parameters",
                host=host,
                port=port,
            )
            return json.dumps(result, indent=2)
        except Exception as exc:
            reset_connection()
            return json.dumps({"error": str(exc)}, indent=2)

    @app.resource("fusion360://body/{name}")
    async def read_body(name: str) -> str:
        try:
            result = _send(
                mode,
                "get_object_info",
                {"name": name},
                host=host,
                port=port,
            )
            return json.dumps(result, indent=2)
        except Exception as exc:
            reset_connection()
            return json.dumps({"error": str(exc)}, indent=2)

    @app.resource("fusion360://component/{name}")
    async def read_component(name: str) -> str:
        try:
            result = _send(
                mode,
                "get_object_info",
                {"name": name},
                host=host,
                port=port,
            )
            return json.dumps(result, indent=2)
        except Exception as exc:
            reset_connection()
            return json.dumps({"error": str(exc)}, indent=2)

    # ── prompts ───────────────────────────────────────────────────────

    @app.prompt(
        name="create-box",
        description="Guide for creating a parametric box in Fusion 360",
    )
    async def prompt_create_box(
        length: str = "10",
        width: str = "5",
        height: str = "3",
    ) -> list[types.PromptMessage]:
        text = (
            f"Create a parametric box in Fusion 360:\n"
            f"1. create_sketch on xy plane\n"
            f"2. draw_rectangle width={width} height={length}\n"
            f"3. extrude height={height}\n"
            f"4. get_scene_info to verify"
        )
        return [
            types.PromptMessage(
                role="user",
                content=types.TextContent(
                    type="text",
                    text=text,
                ),
            ),
        ]

    @app.prompt(
        name="model-threaded-bolt",
        description="Step-by-step guide for modeling a threaded bolt in Fusion 360",
    )
    async def prompt_threaded_bolt(
        designation: str = "M10x1.5",
    ) -> list[types.PromptMessage]:
        text = (
            f"Model a threaded bolt ({designation}):\n"
            f"1. create_sketch on xy plane\n"
            f"2. draw_circle for bolt shaft\n"
            f"3. extrude to bolt length\n"
            f"4. create_thread designation={designation}\n"
            f"5. Create hex head sketch + extrude\n"
            f"6. chamfer head edges"
        )
        return [
            types.PromptMessage(
                role="user",
                content=types.TextContent(
                    type="text",
                    text=text,
                ),
            ),
        ]

    @app.prompt(
        name="sheet-metal-enclosure",
        description="Guide for creating a sheet metal enclosure",
    )
    async def prompt_sheet_metal(
        length: str = "20",
        width: str = "10",
        height: str = "5",
    ) -> list[types.PromptMessage]:
        text = (
            f"Create a sheet metal enclosure "
            f"({length}x{width}x{height} cm):\n"
            f"1. create_sketch on xy plane\n"
            f"2. draw_rectangle {width}x{length}\n"
            f"3. extrude to sheet thickness\n"
            f"4. create_flange on each edge\n"
            f"5. flat_pattern to verify unfold"
        )
        return [
            types.PromptMessage(
                role="user",
                content=types.TextContent(
                    type="text",
                    text=text,
                ),
            ),
        ]

    # ── run ──────────────────────────────────────────────────────────

    async def arun():
        await app.run_stdio_async()

    anyio.run(arun)
    return 0
