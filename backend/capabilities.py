"""Where this host keeps capability packs.

The packs shipped inside the ``revit-bridge`` wheel live in site-packages,
which the unprivileged container user cannot write. The host therefore
works on a copy under ``DATA_DIR/capabilities`` (seeded from the built-in
packs the first time) so that solidify / edit / delete persist in the data
volume. Setting ``REVIT_BRIDGE_CAPABILITIES_DIR`` bypasses the copy and
uses that directory as-is, exactly like the MCP server would.
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from revit_bridge.capabilities import ENV_CAPABILITIES_DIR, ToolStore, default_capabilities_dir

_log = logging.getLogger("backend.capabilities")

_store: ToolStore | None = None


def seed_capabilities(target: Path, source: Path | None = None) -> int:
    """Copy the built-in packs into ``target`` when it holds no pack yet."""
    source = source or default_capabilities_dir()
    target.mkdir(parents=True, exist_ok=True)
    if any(target.glob("*.yaml")):
        return 0
    copied = 0
    for pack in sorted(source.glob("*.yaml")):
        if pack.name.startswith("_"):
            continue
        shutil.copyfile(pack, target / pack.name)
        copied += 1
    _log.info("seeded %d capability pack(s) from %s into %s", copied, source, target)
    return copied


def get_tool_store() -> ToolStore:
    global _store
    if _store is None:
        if os.environ.get(ENV_CAPABILITIES_DIR, "").strip():
            _store = ToolStore()
        else:
            from backend.config import get_settings
            target = get_settings().capabilities_dir
            seed_capabilities(target)
            _store = ToolStore(target)
    return _store


def reset_tool_store() -> None:
    global _store
    _store = None
