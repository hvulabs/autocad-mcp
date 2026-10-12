"""Bulk drafting, file transfer and the LibreDWG route.

Three things a remote headless server needs to be usable for a whole sheet:

* ``entity_batch_create`` answers with handles only when asked
  (``fields=["handle"], compact=True``), so a thousand entities do not cost a
  thousand full records in the reply;
* ``file_upload`` / ``file_download`` / ``file_list`` move drawings in and out
  of the server's own disk, confined to ALLOWED_PATHS and drawing extensions;
* with LibreDWG present, the ezdxf engine opens and writes real DWG.
"""

from __future__ import annotations

import base64
import gzip
import os
import shutil
from json import dumps
from pathlib import Path

import pytest
import pytest_asyncio
from fastmcp import Client
from fastmcp.exceptions import ToolError

import config
import server
from backends import ezdxf_backend

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as connected:
        status = (await connected.call_tool("system_status", {})).structured_content or {}
        assert status.get("backend") == "ezdxf", "these tests must never touch live AutoCAD"
        await connected.call_tool("drawing_new", {})
        yield connected


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config.settings, "allowed_paths", [tmp_path.resolve()])
    return tmp_path.resolve()


def _lines(n):
    return [{"type": "line", "x1": i, "y1": 0, "x2": i, "y2": 100} for i in range(n)]


# ---------------------------------------------------------------------------
# entity_batch_create result shaping
# ---------------------------------------------------------------------------


async def test_batch_create_default_still_returns_full_records(client):
    result = (
        await client.call_tool("entity_batch_create", {"entities": _lines(3)})
    ).structured_content
    assert result["created"] == 3
    assert isinstance(result["entities"], list)
    assert {"handle", "type", "layer", "properties"} <= set(result["entities"][0])


async def test_batch_create_compact_handles_are_a_fraction_of_the_default(client):
    default = (
        await client.call_tool("entity_batch_create", {"entities": _lines(200)})
    ).structured_content
    compact = (
        await client.call_tool(
            "entity_batch_create",
            {"entities": _lines(200), "fields": ["handle"], "compact": True},
        )
    ).structured_content
    assert compact["created"] == 200
    assert compact["entities"]["fields"] == ["handle"]
    assert len(compact["entities"]["rows"]) == 200
    assert len(dumps(compact)) < len(dumps(default)) / 10


async def test_batch_create_reports_errors_alongside_a_compact_result(client):
    result = (
        await client.call_tool(
            "entity_batch_create",
            {"entities": [*_lines(2), {"type": "nonsense"}], "compact": True},
        )
    ).structured_content
    assert result["created"] == 2
    assert result["errors"][0]["index"] == 2


# ---------------------------------------------------------------------------
# File transfer
# ---------------------------------------------------------------------------


async def test_upload_then_open_then_download_round_trip(client, store, tmp_path):
    source = tmp_path / "seed.dxf"
    await client.call_tool("entity_batch_create", {"entities": _lines(5)})
    await client.call_tool("drawing_save_as", {"path": str(source)})
    raw = source.read_bytes()
    target = store / "uploaded.dxf"

    up = (
        await client.call_tool(
            "file_upload",
            {
                "path": str(target),
                "content_base64": base64.b64encode(gzip.compress(raw)).decode(),
                "gzipped": True,
            },
        )
    ).structured_content
    assert up["bytes"] == len(raw)
    assert target.read_bytes() == raw

    await client.call_tool("drawing_open", {"path": str(target)})
    stats = (await client.call_tool("analysis_entity_stats", {})).structured_content
    assert stats["total_entities"] == 5

    down = (await client.call_tool("file_download", {"path": str(target)})).structured_content
    assert gzip.decompress(base64.b64decode(down["content_base64"])) == raw
    assert down["sha256"] == up["sha256"]

    listing = (await client.call_tool("file_list", {"folder": str(store)})).structured_content
    assert "seed.dxf" in [f["name"] for f in listing["files"]]


async def test_upload_refuses_to_overwrite_unless_asked(client, store):
    target = store / "a.dxf"
    target.write_text("old")
    body = {"path": str(target), "content_base64": base64.b64encode(b"new").decode()}
    with pytest.raises(ToolError, match="overwrite"):
        await client.call_tool("file_upload", body)
    await client.call_tool("file_upload", {**body, "overwrite": True})
    assert target.read_bytes() == b"new"


async def test_transfer_is_disabled_without_allowed_paths(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config.settings, "allowed_paths", [])
    (tmp_path / "x.dxf").write_text("0\nEOF\n")
    with pytest.raises(ToolError, match="ALLOWED_PATHS"):
        await client.call_tool("file_download", {"path": str(tmp_path / "x.dxf")})


async def test_transfer_refuses_non_drawing_files(client, store):
    (store / "secret.env").write_text("TOKEN=1")
    with pytest.raises(ToolError, match="not a drawing file"):
        await client.call_tool("file_download", {"path": str(store / "secret.env")})


async def test_transfer_refuses_paths_outside_allowed_paths(client, store, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "x.dxf"
    outside.write_text("0\nEOF\n")
    with pytest.raises(ToolError):
        await client.call_tool("file_download", {"path": str(outside)})


async def test_transfer_respects_the_size_cap(client, store, monkeypatch):
    monkeypatch.setenv("MAX_TRANSFER_BYTES", "10")
    (store / "big.dxf").write_bytes(b"x" * 11)
    with pytest.raises(ToolError, match="MAX_TRANSFER_BYTES"):
        await client.call_tool("file_download", {"path": str(store / "big.dxf")})


# ---------------------------------------------------------------------------
# LibreDWG
# ---------------------------------------------------------------------------


def _libredwg_dir() -> str | None:
    pinned = os.environ.get("LIBREDWG_BIN")
    if pinned and (Path(pinned) / "dxf2dwg").is_file():
        return pinned
    found = shutil.which("dxf2dwg")
    return str(Path(found).parent) if found and shutil.which("dwg2dxf") else None


async def test_without_a_converter_dwg_is_still_refused(client, tmp_path, monkeypatch):
    monkeypatch.setenv("LIBREDWG_BIN", str(tmp_path))  # an empty folder: no tools
    monkeypatch.setattr(ezdxf_backend, "_odafc_available", lambda: False)
    with pytest.raises(ToolError):
        await client.call_tool("drawing_save_as", {"path": str(tmp_path / "x.dwg")})
    with pytest.raises(ToolError):
        await client.call_tool("drawing_export_dwg", {"path": str(tmp_path / "x.dwg")})


@pytest.mark.skipif(_libredwg_dir() is None, reason="LibreDWG (dxf2dwg/dwg2dxf) not installed")
async def test_libredwg_writes_a_dwg_that_reopens_with_the_same_entities(
    client, tmp_path, monkeypatch
):
    monkeypatch.setenv("LIBREDWG_BIN", _libredwg_dir())
    monkeypatch.setattr(ezdxf_backend, "_odafc_available", lambda: False)
    await client.call_tool("entity_batch_create", {"entities": _lines(7)})
    out = tmp_path / "made.dwg"

    written = (await client.call_tool("drawing_export_dwg", {"path": str(out)})).structured_content
    assert written["writer"] == "libredwg"
    assert written["version"] == "R2000"
    assert out.read_bytes()[:4] == b"AC10", "a DWG header, not DXF text"

    caps = (await client.call_tool("system_about", {})).structured_content["capabilities"]
    assert caps["dwg"]["supported"] and caps["dwg_write"]["supported"]

    await client.call_tool("drawing_open", {"path": str(out)})
    stats = (await client.call_tool("analysis_entity_stats", {})).structured_content
    assert stats["by_type"].get("LINE") == 7
