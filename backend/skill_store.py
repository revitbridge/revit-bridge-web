"""Skills are Markdown files (YAML front matter + protocol text).

Two directories feed the store:

- the user directory ``DATA_DIR/skills`` - editable through the API,
  one ``<id>.md`` per skill;
- a read-only built-in directory, scanned recursively and exposed with ids
  ``builtin:<relative/path>``: by default the plugin skills shipped inside
  the ``revit-bridge`` wheel (``revit_bridge.skills_dir()``, so
  ``builtin:revit-bridge/SKILL`` and its ``references/``); ``SKILLS_DIR``
  replaces that directory with a mounted checkout.

Every enabled skill is concatenated into the chat system prompt. Of the
wheel's skills only each ``SKILL.md`` is enabled by default (the chat has the
tools it describes since the host loop); the ``references/`` are material
the skill reads on demand, not prompt text, so they stay off unless a file's
own ``enabled: true`` front matter says otherwise. A mounted ``SKILLS_DIR``
is enabled by default throughout: mounting it is the operator's choice and
its files are theirs to edit.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from revit_bridge import skills_dir

_log = logging.getLogger("backend.skills")

_FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_ID_RE = re.compile(r"[\w\-]+")
BUILTIN_PREFIX = "builtin:"


def parse_skill_file(text: str) -> dict[str, Any]:
    """Split a skill file into ``{"meta": {...}, "content": str}``."""
    match = _FRONT_MATTER.match(text)
    if match:
        try:
            meta = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        content = text[match.end():]
    else:
        meta, content = {}, text
    return {"meta": meta, "content": content.strip()}


def build_skill_file(meta: dict, content: str) -> str:
    front = yaml.dump(meta, allow_unicode=True, default_flow_style=False, sort_keys=False).strip()
    return f"---\n{front}\n---\n\n{content}\n"


def _first_title_and_paragraph(text: str) -> tuple[str, str]:
    title = desc = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not title and stripped.startswith("# "):
            title = stripped[2:].strip()
        elif title and not desc and stripped and not stripped.startswith("#"):
            desc = stripped.rstrip(".").strip()
            break
    return title, desc


class SkillStore:
    def __init__(self, user_dir: Path, builtin_dir: Path | None = None):
        self._dir = Path(user_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        # No override: the skills shipped inside the revit-bridge wheel, of which
        # only SKILL.md files enter the prompt unless a file says ``enabled: true``.
        self._builtin = Path(builtin_dir) if builtin_dir else skills_dir()
        self._builtin_enabled_by_default = builtin_dir is not None

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def slug(name: str) -> str:
        s = re.sub(r"[^\w\-]", "-", name.lower().strip())
        return re.sub(r"-+", "-", s).strip("-") or "unnamed"

    def _file_for(self, skill_id: str) -> Path:
        if not _ID_RE.fullmatch(skill_id):
            raise ValueError(f"Invalid skill id: {skill_id}")
        return self._dir / f"{skill_id}.md"

    def _summary(self, skill_id: str, path: Path, parsed: dict, *, readonly: bool, layer: str = "") -> dict:
        meta = parsed["meta"]
        title, desc = _first_title_and_paragraph(parsed["content"])
        enabled_default = True
        if readonly:
            enabled_default = self._builtin_enabled_by_default or path.name == "SKILL.md"
        return {
            "id": skill_id,
            "name": str(meta.get("name") or title or path.stem),
            "description": str(meta.get("description") or desc or ""),
            "version": str(meta.get("version", "1.0")),
            "author": str(meta.get("author", "")),
            "enabled": bool(meta.get("enabled", enabled_default)),
            "source": "builtin" if readonly else "custom",
            "layer": layer,
            "readonly": readonly,
            "file_size": path.stat().st_size,
        }

    # -- read ------------------------------------------------------------------

    def list_all(self) -> list[dict]:
        skills = self._list_builtin()
        for path in sorted(self._dir.glob("*.md")):
            try:
                parsed = parse_skill_file(path.read_text(encoding="utf-8"))
                skills.append(self._summary(path.stem, path, parsed, readonly=False))
            except Exception as exc:  # noqa: BLE001 - one bad file must not hide the rest
                _log.warning("skipping skill %s: %s", path.name, exc)
        return skills

    @staticmethod
    def _builtin_order(rel: Path) -> tuple:
        """Sort key: a directory's own files before its subdirectories, ``SKILL.md``
        first among them, then by name - the same on every platform (Path
        ordering is case-insensitive on Windows and would put ``references/``
        before ``SKILL.md`` there and after it on Linux)."""
        parents = tuple(part.lower() for part in rel.parts[:-1])
        return (parents, 0 if rel.name == "SKILL.md" else 1, rel.name.lower())

    def _list_builtin(self) -> list[dict]:
        if not self._builtin or not self._builtin.is_dir():
            return []
        found = []
        root = self._builtin.resolve()
        paths = sorted(root.rglob("*.md"), key=lambda p: self._builtin_order(p.relative_to(root)))
        for path in paths:
            rel = path.relative_to(root).with_suffix("").as_posix()
            layer = rel.split("/")[0] if "/" in rel else ""
            try:
                parsed = parse_skill_file(path.read_text(encoding="utf-8"))
                found.append(self._summary(f"{BUILTIN_PREFIX}{rel}", path, parsed, readonly=True, layer=layer))
            except Exception as exc:  # noqa: BLE001
                _log.warning("skipping built-in skill %s: %s", rel, exc)
        return found

    def _builtin_path(self, skill_id: str) -> Path | None:
        if not self._builtin or not skill_id.startswith(BUILTIN_PREFIX):
            return None
        rel = skill_id[len(BUILTIN_PREFIX):]
        if not rel or ".." in rel.split("/") or rel.startswith(("/", "\\")):
            return None
        root = self._builtin.resolve()
        path = (root / (rel + ".md")).resolve()
        if path.is_file() and path.is_relative_to(root):
            return path
        return None

    def get(self, skill_id: str) -> dict | None:
        if skill_id.startswith(BUILTIN_PREFIX):
            path = self._builtin_path(skill_id)
            if not path:
                return None
            text = path.read_text(encoding="utf-8")
            parsed = parse_skill_file(text)
            rel = skill_id[len(BUILTIN_PREFIX):]
            layer = rel.split("/")[0] if "/" in rel else ""
            return {**self._summary(skill_id, path, parsed, readonly=True, layer=layer),
                    "content": parsed["content"], "raw": text}
        try:
            path = self._file_for(skill_id)
        except ValueError:
            return None
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8")
        parsed = parse_skill_file(text)
        return {**self._summary(skill_id, path, parsed, readonly=False),
                "content": parsed["content"], "raw": text}

    # -- write -----------------------------------------------------------------

    def save(self, skill_id: str | None, meta: dict, content: str) -> dict:
        """Create (``skill_id=None``) or overwrite a custom skill."""
        overwrite = bool(meta.pop("_overwrite", False))
        if not skill_id:
            skill_id = self.slug(str(meta.get("name") or "unnamed"))
        path = self._file_for(skill_id)
        if path.is_file() and not overwrite:
            n = 2
            while self._file_for(f"{skill_id}-{n}").is_file():
                n += 1
            skill_id = f"{skill_id}-{n}"
            path = self._file_for(skill_id)
        meta.setdefault("name", skill_id)
        meta.setdefault("enabled", True)
        meta.setdefault("version", "1.0")
        path.write_text(build_skill_file(meta, content), encoding="utf-8")
        _log.info("skill saved: %s (%d bytes)", skill_id, path.stat().st_size)
        return self.get(skill_id)  # type: ignore[return-value]

    def update(self, skill_id: str, meta: dict | None = None, content: str | None = None) -> dict | None:
        if skill_id.startswith(BUILTIN_PREFIX) or not self.get(skill_id):
            return None
        parsed = parse_skill_file(self._file_for(skill_id).read_text(encoding="utf-8"))
        if meta:
            parsed["meta"].update(meta)
        if content is not None:
            parsed["content"] = content
        parsed["meta"]["_overwrite"] = True
        return self.save(skill_id, parsed["meta"], parsed["content"])

    def toggle(self, skill_id: str, enabled: bool) -> dict | None:
        return self.update(skill_id, meta={"enabled": enabled})

    def delete(self, skill_id: str) -> bool:
        if skill_id.startswith(BUILTIN_PREFIX):
            return False
        try:
            path = self._file_for(skill_id)
        except ValueError:
            return False
        if path.is_file():
            path.unlink()
            _log.info("skill deleted: %s", skill_id)
            return True
        return False

    def import_from_text(self, raw_text: str, source: str = "") -> dict:
        parsed = parse_skill_file(raw_text)
        meta = parsed["meta"]
        if source:
            meta["source_url"] = source
        return self.save(self.slug(str(meta.get("name") or "imported")), meta, parsed["content"])

    # -- prompt ----------------------------------------------------------------

    def active_prompt(self) -> str:
        """Every enabled skill, built-ins first, joined for the system prompt."""
        parts: list[str] = []
        for summary in self.list_all():
            if not summary["enabled"]:
                continue
            skill = self.get(summary["id"])
            if skill and skill["content"]:
                parts.append(skill["content"])
        return "\n\n---\n\n".join(parts)


_store: SkillStore | None = None


def get_skill_store() -> SkillStore:
    global _store
    if _store is None:
        from backend.config import get_settings
        settings = get_settings()
        _store = SkillStore(settings.user_skills_dir, settings.skills_dir)
    return _store


def reset_skill_store() -> None:
    global _store
    _store = None
