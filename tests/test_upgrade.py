"""A data volume written by the 0.1 host: the old pack copies are parked aside at startup."""
from __future__ import annotations

from revit_bridge.capabilities import ToolStore

from backend.upgrade import LEGACY_DIR, quarantine_legacy_packs

# What 0.1 seeded into DATA_DIR/capabilities: the v0 layout, no schema_version.
V0_CREATE_WALL = """\
name: create_wall
display_name: Create Wall
description: 0.1 copy
code_template: 'return "v0";'
parameters:
- name: level_name
  type: string
  description: Level
  required: true
  choices_from: levels
tags:
- wall
created_at: '2026-09-18T00:00:00'
source_query: ''
"""


def _seed(tmp_path):
    user_dir = tmp_path / "data" / "capabilities"
    user_dir.mkdir(parents=True)
    (user_dir / "create_wall.yaml").write_text(V0_CREATE_WALL, encoding="utf-8")
    # A pack solidified by 0.2 (v1 layout) must stay exactly where it is.
    ToolStore(user_dir).solidify(
        name="count_walls",
        code="return new FilteredElementCollector(document).OfCategory(BuiltInCategory.OST_Walls).GetElementCount();",
        description="How many walls",
    )
    return user_dir


def test_legacy_pack_copies_are_moved_aside_at_startup(make_client, tmp_path, caplog):
    user_dir = _seed(tmp_path)
    v1_before = (user_dir / "count_walls.yaml").read_bytes()

    with caplog.at_level("WARNING", logger="backend.upgrade"):
        client = make_client()

    lines = [r.getMessage() for r in caplog.records if r.name == "backend.upgrade"]
    assert len(lines) == 1 and "create_wall.yaml" in lines[0] and LEGACY_DIR in lines[0]
    assert not (user_dir / "create_wall.yaml").exists()
    assert (user_dir / LEGACY_DIR / "create_wall.yaml").read_text(encoding="utf-8") == V0_CREATE_WALL
    assert (user_dir / "count_walls.yaml").read_bytes() == v1_before

    tools = {t["name"]: t for t in client.get("/api/v1/bridge/tools").json()}
    assert len(tools) == 9  # the eight wheel packs plus the v1 user pack
    assert tools["create_wall"]["version"] == "1.0.0"  # the wheel's pack, not the 0.1 copy
    assert tools["count_walls"]["description"] == "How many walls"
    assert client.get("/api/v1/bridge/tools/create_wall").json()["code_template"] != 'return "v0";'

    # A second start finds nothing to move: same files, same content, untouched.
    legacy = user_dir / LEGACY_DIR
    snapshot = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in legacy.iterdir()}
    assert quarantine_legacy_packs(user_dir) == []
    make_client()
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in legacy.iterdir()} == snapshot
    assert sorted(p.name for p in user_dir.glob("*.yaml")) == ["count_walls.yaml"]


def test_quarantine_leaves_what_the_package_would_not_read(tmp_path):
    user_dir = tmp_path / "caps"
    user_dir.mkdir()
    (user_dir / "_draft.yaml").write_text("name: draft\n", encoding="utf-8")
    (user_dir / "broken.yaml").write_text("name: [unclosed\n", encoding="utf-8")
    (user_dir / "old.yaml").write_text("name: old\ncode_template: return 1;\n", encoding="utf-8")
    (user_dir / LEGACY_DIR).mkdir()
    (user_dir / LEGACY_DIR / "old.yaml").write_text("earlier move\n", encoding="utf-8")

    moved = quarantine_legacy_packs(user_dir)

    assert moved == [user_dir / LEGACY_DIR / "old-2.yaml"]  # never overwrites an earlier move
    assert (user_dir / LEGACY_DIR / "old.yaml").read_text(encoding="utf-8") == "earlier move\n"
    assert (user_dir / "_draft.yaml").exists() and (user_dir / "broken.yaml").exists()
    assert quarantine_legacy_packs(tmp_path / "missing") == []
