"""Tests for vault_save_video_frame: a transcriptor-mcp frame written straight to the vault."""

import asyncio
import base64
import json

import httpx
import pytest
from mcp.server.fastmcp import FastMCP

from obsidian_vault_mcp import video_frames
from obsidian_vault_mcp.video_frames import VideoFrameExtension, save_video_frame

_TRANSCRIPTOR = "http://transcriptor-mcp:4200/mcp"
_VIDEO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
_JPEG_BYTES = b"\xff\xd8\xff\xe0 fake-jpeg-bytes"
_JPEG_B64 = base64.b64encode(_JPEG_BYTES).decode()


def _sse(message: dict) -> httpx.Response:
    body = f"event: message\ndata: {json.dumps(message)}\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)


def _frame_reply(mime_type: str = "image/jpeg", data: str = _JPEG_B64) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [
                {"type": "text", "text": "Frame captured at 00:00:05.000"},
                {"type": "image", "data": data, "mimeType": mime_type},
            ],
            "structuredContent": {"timestamp": "00:00:05.000", "width": 1280, "mimeType": mime_type},
        },
    }


@pytest.fixture
def transcriptor(monkeypatch):
    """Route the tool's HTTP client to a handler; returns the list of requests it received."""
    state = {"reply": _sse(_frame_reply()), "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(json.loads(request.content))
        reply = state["reply"]
        if isinstance(reply, Exception):
            raise reply
        return reply

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        video_frames.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    return state


def _save(**kwargs) -> dict:
    return json.loads(asyncio.run(save_video_frame(_TRANSCRIPTOR, _VIDEO, **kwargs)))


def test_writes_frame_to_vault(vault_dir, transcriptor):
    result = _save(path="Cocktails/negroni.jpg", timecode="00:05")
    assert "error" not in result, result
    assert result["created"] is True
    assert result["timestamp"] == "00:00:05.000"
    assert (vault_dir / "Cocktails" / "negroni.jpg").read_bytes() == _JPEG_BYTES


def test_sends_only_the_arguments_given(vault_dir, transcriptor):
    _save(path="frame.png", seconds=12.5, width=640)
    call = transcriptor["requests"][0]
    assert call["method"] == "tools/call"
    assert call["params"]["name"] == "get_video_frame"
    assert call["params"]["arguments"] == {"url": _VIDEO, "format": "png", "seconds": 12.5, "width": 640}


def test_format_follows_the_extension(vault_dir, transcriptor):
    _save(path="frame.JPEG")
    assert transcriptor["requests"][0]["params"]["arguments"]["format"] == "jpeg"


def test_rejects_unsupported_extension_without_calling(vault_dir, transcriptor):
    result = _save(path="frame.webp")
    assert ".png, .jpg or .jpeg" in result["error"]
    assert transcriptor["requests"] == []


def test_accepts_plain_json_reply(vault_dir, transcriptor):
    transcriptor["reply"] = httpx.Response(200, json=_frame_reply())
    result = _save(path="frame.jpg")
    assert "error" not in result, result
    assert (vault_dir / "frame.jpg").read_bytes() == _JPEG_BYTES


def test_tool_error_is_passed_on(vault_dir, transcriptor):
    transcriptor["reply"] = _sse({
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": "Video unavailable"}], "isError": True},
    })
    result = _save(path="frame.jpg")
    assert result["error"] == "Video unavailable"
    assert not (vault_dir / "frame.jpg").exists()


def test_rpc_error_is_passed_on(vault_dir, transcriptor):
    transcriptor["reply"] = _sse({"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "Invalid params"}})
    result = _save(path="frame.jpg")
    assert "Invalid params" in result["error"]


def test_unreachable_transcriptor_is_an_error_not_a_crash(vault_dir, transcriptor):
    transcriptor["reply"] = httpx.ConnectError("connection refused")
    result = _save(path="frame.jpg")
    assert "connection refused" in result["error"]


def test_http_error_status_is_an_error(vault_dir, transcriptor):
    transcriptor["reply"] = httpx.Response(503, text="server busy")
    result = _save(path="frame.jpg")
    assert "503" in result["error"]


def test_overwrite_guard_still_applies(vault_dir, transcriptor):
    (vault_dir / "frame.jpg").write_bytes(b"existing")
    result = _save(path="frame.jpg")
    assert "already exists" in result["error"]
    assert (vault_dir / "frame.jpg").read_bytes() == b"existing"
    assert _save(path="frame.jpg", overwrite=True)["created"] is False


def test_path_traversal_is_rejected(vault_dir, transcriptor):
    result = _save(path="../escape.jpg")
    assert "error" in result
    assert not (vault_dir.parent / "escape.jpg").exists()


def test_mime_and_extension_mismatch_is_rejected(vault_dir, transcriptor):
    transcriptor["reply"] = _sse(_frame_reply(mime_type="image/png"))
    result = _save(path="frame.jpg")
    assert "is not allowed for media_type" in result["error"]


def _tool_names(mcp: FastMCP) -> set[str]:
    return {tool.name for tool in asyncio.run(mcp.list_tools())}


def test_extension_registers_tool_when_configured():
    mcp = FastMCP("test")
    VideoFrameExtension(_TRANSCRIPTOR).register_tools(mcp)
    assert "vault_save_video_frame" in _tool_names(mcp)


def test_extension_is_off_without_url(monkeypatch):
    monkeypatch.delenv("TRANSCRIPTOR_MCP_URL", raising=False)
    mcp = FastMCP("test")
    VideoFrameExtension().register_tools(mcp)
    assert "vault_save_video_frame" not in _tool_names(mcp)


def test_extension_reads_url_from_environment(monkeypatch):
    monkeypatch.setenv("TRANSCRIPTOR_MCP_URL", f"  {_TRANSCRIPTOR}  ")
    assert VideoFrameExtension().transcriptor_url == _TRANSCRIPTOR
