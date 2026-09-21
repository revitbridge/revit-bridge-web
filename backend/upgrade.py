"""One-time repairs of the data volume after a host upgrade.

0.1 copied the package's built-in packs into ``DATA_DIR/capabilities`` on
first start. Since 0.2 that directory is the package's *user* pack directory,
where a file wins over the wheel's pack of the same name: the eleven 0.1
copies (v0 layout, no ``schema_version``) would shadow the rewritten
built-ins and keep three packs the package dropped. At startup every v0 file
in the user directory is moved to ``<user dir>/legacy-0.1/``. Nothing is
deleted and nothing is rewritten; a second start finds nothing to move.
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

_log = logging.getLogger("backend.upgrade")

LEGACY_DIR = "legacy-0.1"


def quarantine_legacy_packs(user_dir: Path) -> list[Path]:
    """Move every ``*.yaml`` without ``schema_version`` out of ``user_dir``.

    Returns the new paths. Files the package would not read anyway
    (``_``-prefixed, unparsable) stay where they are.
    """
    user_dir = Path(user_dir)
    if not user_dir.is_dir():
        return []
    moved: list[Path] = []
    for path in sorted(user_dir.glob("*.yaml")):
        if not path.is_file() or path.name.startswith("_") or not _is_v0(path):
            continue
        legacy = user_dir / LEGACY_DIR
        legacy.mkdir(parents=True, exist_ok=True)
        target = _free_name(legacy, path.name)
        path.replace(target)
        _log.warning("capability pack %s is a 0.1 copy without schema_version; moved to %s "
                     "(the package's own pack of that name is used instead)", path.name, target)
        moved.append(target)
    return moved


def _is_v0(path: Path) -> bool:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return False
    return isinstance(data, dict) and "schema_version" not in data


def _free_name(directory: Path, name: str) -> Path:
    """``name``, or ``<stem>-2.yaml``, ``-3``... when an earlier move left one there."""
    target = directory / name
    n = 2
    while target.exists():
        target = directory / f"{Path(name).stem}-{n}{Path(name).suffix}"
        n += 1
    return target
