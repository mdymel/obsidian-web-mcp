"""Save a frame of a video straight into the vault, by way of a transcriptor-mcp server.

transcriptor-mcp's get_video_frame hands the frame to the model as an image block, which the
model can look at but cannot copy back out as base64 for vault_write_binary. This extension
adds vault_save_video_frame, which asks transcriptor-mcp for the frame itself, server to
server, and writes the bytes it gets back -- the image never passes through the conversation.

Off unless TRANSCRIPTOR_MCP_URL is set to the server's Streamable HTTP endpoint, e.g.
http://transcriptor-mcp:4200/mcp; without it the tool is not advertised at all.
transcriptor-mcp checks no credentials, so that URL should be one only this server can reach.

Run with the `vault-mcp-frames` entry point, which is the stock server plus this extension.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import PurePosixPath

import httpx

from .extensions import Extension
from .serialization import dumps
from .server import serve
from .tools.write import vault_write_binary

logger = logging.getLogger(__name__)

# A frame is one yt-dlp + ffmpeg run on the transcriptor side, queue wait included; its own
# budget is 60 s by default. Leave room above that so its error message reaches us instead
# of a bare timeout.
_REQUEST_TIMEOUT_SECONDS = 120.0

_FORMAT_BY_EXTENSION = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg"}


def _frame_arguments(
    url: str,
    image_format: str,
    timecode: str | None,
    seconds: float | None,
    width: int | None,
    quality: int | None,
) -> dict:
    """Build get_video_frame's arguments, leaving out what the caller did not set."""
    arguments = {
        "url": url,
        "format": image_format,
        "timecode": timecode,
        "seconds": seconds,
        "width": width,
        "quality": quality,
    }
    return {key: value for key, value in arguments.items() if value is not None}


def _parse_rpc_response(content_type: str, body: str) -> dict:
    """Return the JSON-RPC message from a Streamable HTTP reply, plain JSON or SSE."""
    if "text/event-stream" not in content_type:
        return json.loads(body)
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        message = json.loads(line[len("data:"):])
        if "result" in message or "error" in message:
            return message
    raise ValueError("transcriptor-mcp sent an event stream with no result in it")


def _extract_frame(message: dict) -> tuple[str, str, dict]:
    """Pull (base64 data, mime type, structured metadata) out of a tools/call reply."""
    if "error" in message:
        raise ValueError(f"transcriptor-mcp error: {message['error'].get('message', message['error'])}")
    result = message.get("result") or {}
    content = result.get("content") or []
    if result.get("isError"):
        text = " ".join(item.get("text", "") for item in content if item.get("type") == "text")
        raise ValueError(text.strip() or "transcriptor-mcp could not capture the frame")
    for item in content:
        if item.get("type") == "image" and item.get("data"):
            return item["data"], item.get("mimeType", ""), result.get("structuredContent") or {}
    raise ValueError("transcriptor-mcp returned no image")


async def save_video_frame(
    transcriptor_url: str,
    url: str,
    path: str,
    timecode: str | None = None,
    seconds: float | None = None,
    width: int | None = None,
    quality: int | None = None,
    overwrite: bool = False,
    create_dirs: bool = True,
) -> str:
    """Capture one frame through transcriptor-mcp and write it to `path` in the vault."""
    image_format = _FORMAT_BY_EXTENSION.get(PurePosixPath(path).suffix.lower())
    if image_format is None:
        return dumps({"error": "path must end in .png, .jpg or .jpeg", "path": path})

    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "get_video_frame",
            "arguments": _frame_arguments(url, image_format, timecode, seconds, width, quality),
        },
    }
    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.post(
                transcriptor_url,
                json=request,
                headers={"Accept": "application/json, text/event-stream"},
            )
        response.raise_for_status()
        message = _parse_rpc_response(response.headers.get("content-type", ""), response.text)
        data, media_type, metadata = _extract_frame(message)
    except (httpx.HTTPError, ValueError) as e:
        logger.warning(f"vault_save_video_frame failed for {url}: {e}")
        return dumps({"error": str(e), "path": path, "url": url})

    written = json.loads(vault_write_binary(path, data, media_type, overwrite, create_dirs))
    if "error" not in written:
        written["url"] = url
        for key in ("timestamp", "width"):
            if key in metadata:
                written[key] = metadata[key]
    return dumps(written)


class VideoFrameExtension(Extension):
    """Adds vault_save_video_frame when TRANSCRIPTOR_MCP_URL is set."""

    def __init__(self, transcriptor_url: str | None = None):
        self.transcriptor_url = (
            transcriptor_url if transcriptor_url is not None else os.environ.get("TRANSCRIPTOR_MCP_URL", "")
        ).strip()

    def register_tools(self, mcp) -> None:
        if not self.transcriptor_url:
            logger.info("TRANSCRIPTOR_MCP_URL is not set -- vault_save_video_frame is disabled")
            return
        transcriptor_url = self.transcriptor_url

        @mcp.tool(
            name="vault_save_video_frame",
            description=(
                "Capture one frame of a video (YouTube, Instagram, TikTok, Vimeo and other platforms "
                "yt-dlp supports) and save it as an image in the vault. Give timecode (\"01:23\", "
                "\"00:01:23.500\") or seconds; the format follows the path's extension (.png, .jpg, "
                ".jpeg). Optional width (max 1920, never upscales) and jpeg quality (2 best to 31). "
                "Use this instead of get_video_frame + vault_write_binary: the image is fetched and "
                "written server-side, so it never has to pass through the conversation."
            ),
            annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
        )
        async def vault_save_video_frame(
            url: str,
            path: str,
            timecode: str | None = None,
            seconds: float | None = None,
            width: int | None = None,
            quality: int | None = None,
            overwrite: bool = False,
            create_dirs: bool = True,
        ) -> str:
            """Save a video frame to the vault."""
            return await save_video_frame(
                transcriptor_url, url, path, timecode, seconds, width, quality, overwrite, create_dirs
            )

        logger.info(f"vault_save_video_frame enabled via {transcriptor_url}")


def main() -> None:
    """Console-script entry point: the stock server plus vault_save_video_frame."""
    serve([VideoFrameExtension()])
