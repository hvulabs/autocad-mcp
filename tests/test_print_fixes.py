"""Regressions found drawing a real A3 1:50 bedroom plan through the server.

* a vertical dimension (rotated MTEXT) made the DWG write fail;
* Vietnamese text made it fail with a UnicodeDecodeError from dxf2dwg's output;
* with LibreDWG 0.13.3 the DWG "saved" but lost names, dimensions and
  model space, so every DWG write is now read back and checked;
* PDF text came out as empty boxes on a machine with no fonts;
* after a failed item, compact batch rows no longer lined up with the input.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
import pytest_asyncio
from fastmcp import Client

import server
from backends import ezdxf_backend

pytestmark = pytest.mark.asyncio


def _libredwg_dir() -> str | None:
    pinned = os.environ.get("LIBREDWG_BIN")
    if pinned and (Path(pinned) / "dxf2dwg").is_file():
        return pinned
    found = shutil.which("dxf2dwg")
    return str(Path(found).parent) if found and shutil.which("dwg2dxf") else None


#: Read at import, before conftest pins LIBREDWG_BIN away for every test.
_LIBREDWG = _libredwg_dir()


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as connected:
        status = (await connected.call_tool("system_status", {})).structured_content or {}
        assert status.get("backend") == "ezdxf", "these tests must never touch live AutoCAD"
        await connected.call_tool("drawing_new", {})
        yield connected


async def _room_with_vertical_dim_and_vietnamese(client):
    await client.call_tool(
        "cad_batch",
        {
            "steps": [
                {
                    "tool": "entity_create_polyline",
                    "args": {
                        "points": [[0, 0], [4000, 0], [4000, 3500], [0, 3500]],
                        "closed": True,
                    },
                },
                {"tool": "entity_create_text", "args": {"text": "PHÒNG NGỦ", "x": 1500, "y": 1700, "height": 150}},
                {"tool": "entity_create_text", "args": {"text": "Bậu cửa +900", "x": 200, "y": 3200, "height": 100}},
                {
                    "tool": "dimension_linear",
                    "args": {"x1": 0, "y1": 0, "x2": 4000, "y2": 0, "dim_x": 2000, "dim_y": -500},
                },
                {
                    "tool": "dimension_linear",
                    "args": {
                        "x1": 0, "y1": 0, "x2": 0, "y2": 3500,
                        "dim_x": -500, "dim_y": 1750, "rotation": 90,
                    },
                },
            ]
        },
    )


@pytest.mark.skipif(_LIBREDWG is None, reason="LibreDWG (dxf2dwg/dwg2dxf) not installed")
async def test_dwg_keeps_vertical_dims_vietnamese_text_and_names(client, tmp_path, monkeypatch):
    monkeypatch.setenv("LIBREDWG_BIN", _LIBREDWG)
    monkeypatch.setattr(ezdxf_backend, "_odafc_available", lambda: False)
    await client.call_tool("layer_create", {"name": "FURNITURE"})
    await _room_with_vertical_dim_and_vietnamese(client)
    out = tmp_path / "room.dwg"

    written = (await client.call_tool("drawing_save_as", {"path": str(out)})).structured_content
    assert written["format"] == "dwg"

    doc = ezdxf_backend._read_dwg(str(out))
    kinds = sorted(e.dxftype() for e in doc.modelspace())
    assert kinds.count("DIMENSION") == 2
    assert kinds.count("TEXT") == 2
    assert "FURNITURE" in [layer.dxf.name for layer in doc.layers], "names must not be cut"
    texts = {e.dxf.text for e in doc.modelspace().query("TEXT")}
    assert any("PH" in t and ("Ủ" in t or "U+1EE6" in t.upper()) for t in texts)


@pytest.mark.skipif(_LIBREDWG is None, reason="LibreDWG (dxf2dwg/dwg2dxf) not installed")
async def test_dwg_write_leaves_the_open_drawing_untouched(client, tmp_path, monkeypatch):
    monkeypatch.setenv("LIBREDWG_BIN", _LIBREDWG)
    monkeypatch.setattr(ezdxf_backend, "_odafc_available", lambda: False)
    await _room_with_vertical_dim_and_vietnamese(client)
    await client.call_tool("drawing_export_dwg", {"path": str(tmp_path / "a.dwg")})
    texts = (
        await client.call_tool("analysis_select_by_type", {"entity_type": "TEXT"})
    ).structured_content
    assert "PHÒNG NGỦ" in str(texts), "the live drawing keeps its Unicode text"


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_incomplete_dwg_is_reported_not_passed_off_as_saved(tmp_path, monkeypatch):
    import ezdxf

    doc = ezdxf.new()
    for i in range(5):
        doc.modelspace().add_line((i, 0), (i, 10))
    out = tmp_path / "x.dwg"
    out.write_bytes(b"AC1015")
    monkeypatch.setattr(ezdxf_backend, "_libredwg_tool", lambda name: "/bin/true")
    lossy = ezdxf.new()
    lossy.modelspace().add_line((0, 0), (1, 1))
    for i in range(4):  # same len() as the original, but dead: the recover case
        lossy.modelspace().add_line((i, 0), (i, 1)).destroy()
    monkeypatch.setattr(ezdxf_backend, "_read_dwg", lambda path: lossy)
    with pytest.raises(RuntimeError, match="incomplete"):
        ezdxf_backend._verify_dwg(doc, out)


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_non_utf8_converter_output_does_not_crash(monkeypatch, tmp_path):
    script = tmp_path / "dxf2dwg"
    script.write_text("#!/bin/sh\nprintf 'Warning: \\273 text\\n' >&2\nexit 0\n")
    script.chmod(0o755)
    monkeypatch.setenv("LIBREDWG_BIN", str(tmp_path))
    proc = ezdxf_backend._run_libredwg("dxf2dwg", [])
    assert "Warning" in proc.stderr


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_mtext_rotation_becomes_a_direction():
    import ezdxf

    doc = ezdxf.new()
    mtext = doc.blocks.new("*D9").add_mtext("3500", dxfattribs={"rotation": 90})
    assert ezdxf_backend._mtext_rotation_to_direction(doc) == 1
    assert not mtext.dxf.hasattr("rotation")
    x, y, _ = mtext.dxf.text_direction
    assert abs(x) < 1e-9 and abs(y - 1) < 1e-9


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_render_fonts_fall_back_to_matplotlib_when_the_system_has_none(monkeypatch):
    from ezdxf.fonts import fonts

    saved = fonts.font_manager.dumps()
    try:
        fonts.font_manager.clear()
        fonts.font_manager._fallback_font_name = ""
        assert ezdxf_backend._ensure_render_fonts() == "matplotlib"
        font = fonts.make_font("arial.ttf", 2.5)
        assert not isinstance(font, fonts.MonospaceFont), "text would print as boxes"
    finally:
        fonts.font_manager.loads(saved)
        fonts.font_manager._fallback_font_name = ""


async def test_sheet_pdf_is_paper_white_and_reports_its_font(client, tmp_path, store_root):
    await client.call_tool("entity_create_text", {"text": "PHÒNG NGỦ", "x": 0, "y": 0, "height": 5})
    await client.call_tool("layout_create", {"name": "A3"})
    await client.call_tool(
        "page_setup_apply", {"layout": "A3", "paper": "ISO_A3", "orientation": "landscape"}
    )
    out = store_root / "sheet.pdf"
    result = (
        await client.call_tool("drawing_export_pdf", {"path": str(out), "layout": "A3"})
    ).structured_content
    assert result["fonts"] in {"system", "matplotlib"}
    assert out.read_bytes()[:4] == b"%PDF"
    cfg = ezdxf_backend._print_config(type("L", (), {"dxf_layout": None})())
    from ezdxf.addons.drawing.config import BackgroundPolicy

    assert cfg.background_policy == BackgroundPolicy.WHITE


async def test_batch_rows_name_their_input_after_a_failure(client):
    result = (
        await client.call_tool(
            "entity_batch_create",
            {
                "entities": [
                    {"type": "line", "x1": 0, "y1": 0, "x2": 1, "y2": 0},
                    {"type": "hatch", "points": [[0, 0], [1, 0], [1, 1]]},
                    {"type": "text", "text": "A", "x": 0, "y": 0, "height": 1},
                ],
                "fields": ["handle"],
                "compact": True,
            },
        )
    ).structured_content
    assert result["created"] == 2
    assert result["errors"][0]["index"] == 1
    assert result["row_index"] == [0, 2]


async def test_batch_without_failures_has_no_row_index(client):
    result = (
        await client.call_tool(
            "entity_batch_create",
            {"entities": [{"type": "line", "x1": 0, "y1": 0, "x2": 1, "y2": 0}], "compact": True},
        )
    ).structured_content
    assert "row_index" not in result


@pytest.fixture
def store_root(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config.settings, "allowed_paths", [tmp_path.resolve()])
    return tmp_path.resolve()


async def test_title_block_on_a_layout_starts_at_the_paper_corner(client):
    await client.call_tool("layout_create", {"name": "A3"})
    await client.call_tool(
        "page_setup_apply", {"layout": "A3", "paper": "ISO_A3", "orientation": "landscape"}
    )
    rows = (await client.call_tool("page_setup_list", {})).structured_content
    result = (
        await client.call_tool(
            "titleblock_apply_iso_a3", {"title": "T", "drawing_no": "1", "layout": "A3"}
        )
    ).structured_content
    lo = result["bbox"]["min"]
    assert lo[0] < 0 and lo[1] < 0, rows  # moved off the printable-area origin
    assert abs(result["bbox"]["max"][0] - lo[0] - 420) < 1e-6


async def test_title_block_keeps_an_explicit_origin(client):
    await client.call_tool("layout_create", {"name": "A3"})
    result = (
        await client.call_tool(
            "titleblock_apply_iso_a3",
            {"title": "T", "drawing_no": "1", "layout": "A3", "origin_x": 0, "origin_y": 0},
        )
    ).structured_content
    assert result["bbox"]["min"] == [0, 0]
