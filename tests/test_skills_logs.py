"""Skill directory (user + read-only built-ins) and admin-gated logs."""
from __future__ import annotations

import os
import stat

import pytest

from backend.api import chat as chat_module

ADMIN = {"X-Admin-Token": "hunter2"}


def test_the_wheel_s_skills_are_listed_and_stay_out_of_the_prompt(make_client, env, tmp_path):
    """Without SKILLS_DIR the wheel's skills are listed, but only revit-core is enabled;
    the revit-bridge protocol (the MCP host's tool flow), revit-addin-dev, references and
    lessons stay off. None of them enters the system prompt any more: it is the package's
    host instructions (this host's protocol) and this host's paragraph - the model here
    writes no code, and the skills are guidance for writing it."""
    import shutil

    from revit_bridge import skills_dir

    client = make_client()
    listing = client.get("/api/skills").json()["skills"]
    assert all(s["readonly"] and s["source"] == "builtin" for s in listing)
    on = [s["id"] for s in listing if s["enabled"]]
    assert on == ["builtin:revit-core/SKILL"]
    assert "builtin:revit-bridge/SKILL" in [s["id"] for s in listing]
    assert "builtin:revit-addin-dev/SKILL" in [s["id"] for s in listing]
    assert any(s["id"].startswith("builtin:revit-bridge/references/") for s in listing)
    lessons = [s for s in listing if s["id"].startswith("builtin:revit-core/lessons/")]
    assert lessons and not any(s["enabled"] for s in lessons)

    protocol = client.get("/api/skills/builtin:revit-bridge/SKILL").json()
    core = client.get("/api/skills/builtin:revit-core/SKILL").json()
    addin = client.get("/api/skills/builtin:revit-addin-dev/SKILL").json()
    assert protocol["content"].startswith("# revit-bridge") and protocol["enabled"] is False
    assert core["content"].startswith("# revit-core") and core["enabled"] is True
    assert addin["enabled"] is False
    # Read-only: the store refuses to edit or delete anything from the wheel.
    from backend.skill_store import get_skill_store
    assert get_skill_store().update("builtin:revit-bridge/SKILL", content="x") is None
    assert get_skill_store().delete("builtin:revit-bridge/SKILL") is False

    expected = chat_module.inline_card_instructions(code=False, mentions=True).rstrip("\n") + "\n" + chat_module.WORKBENCH_LINE
    prompt = chat_module.build_system_prompt()
    assert prompt == expected
    for skill in (core, protocol, addin):
        assert skill["content"] not in prompt
    # 3,689 bytes with revit-bridge 0.9.0 (the package's text without code, with the
    # selections rule, and the host's workbench line); 4,288 with 0.8.2's full text and the
    # host's own paragraph, 6,007 with the revit-core skill and the get_guidance pointer it
    # carried before.
    assert len(prompt.encode("utf-8")) < 4_000
    assert chat_module.build_system_prompt(bridge=False) == chat_module.BASE_PROMPT

    # No built-in skills, every skill switched off, or a mounted SKILLS_DIR whose files are
    # on: the prompt is the same.
    empty = tmp_path / "no-skills"
    empty.mkdir()
    off = tmp_path / "all-off"
    off.mkdir()
    (off / "SKILL.md").write_text("---\nname: x\nenabled: false\n---\n\nOff.\n", encoding="utf-8")
    mounted = tmp_path / "mounted"
    shutil.copytree(skills_dir() / "revit-bridge", mounted / "revit-bridge")
    for directory in (empty, off, mounted):
        env.setenv("SKILLS_DIR", str(directory))
        make_client()
        assert chat_module.build_system_prompt() == expected, directory
    assert get_skill_store().get("builtin:revit-bridge/SKILL")["enabled"] is True   # on, and still not in it


def test_builtin_enabled_default_depends_on_the_source(tmp_path, monkeypatch):
    """Wheel skills: revit-core SKILL.md on, every other file off (revit-bridge's
    SKILL.md too), unless the file says otherwise. A mounted SKILLS_DIR: on unless the
    file says ``enabled: false``."""
    from backend import skill_store

    wheel = tmp_path / "wheel-skills"
    files = {
        "extra/SKILL.md": "---\nname: extra\nenabled: true\n---\n\nX on.\n",
        "revit-addin-dev/SKILL.md": "---\nname: revit-addin-dev\n---\n\nAdd-in off.\n",
        "revit-bridge/SKILL.md": "---\nname: revit-bridge\n---\n\nB off.\n",
        "revit-bridge/references/on.md": "---\nenabled: true\n---\n\nB ref on.\n",
        "revit-bridge/references/r.md": "# R\n\nReference.\n",
        "revit-core/SKILL.md": "---\nname: revit-core\n---\n\nC default.\n",
        "revit-core/lessons/l.md": "---\nid: revit-core/l\n---\n\nLesson.\n",
        "revit-views/SKILL.md": "---\nname: revit-views\nenabled: false\n---\n\nV off.\n",
    }
    for rel, text in files.items():
        (wheel / rel).parent.mkdir(parents=True, exist_ok=True)
        (wheel / rel).write_text(text, encoding="utf-8")
    monkeypatch.setattr(skill_store, "skills_dir", lambda: wheel)

    store = skill_store.SkillStore(tmp_path / "user")
    assert [(s["id"], s["enabled"]) for s in store.list_all()] == [
        ("builtin:extra/SKILL", True), ("builtin:revit-addin-dev/SKILL", False),
        ("builtin:revit-bridge/SKILL", False), ("builtin:revit-bridge/references/on", True),
        ("builtin:revit-bridge/references/r", False),
        ("builtin:revit-core/SKILL", True), ("builtin:revit-core/lessons/l", False),
        ("builtin:revit-views/SKILL", False),
    ]
    assert store.get("builtin:revit-bridge/references/r")["enabled"] is False
    assert store.active_prompt() == "X on.\n\n---\n\nB ref on.\n\n---\n\nC default."
    # Its own front matter still turns it on, in its old place.
    (wheel / "revit-bridge" / "SKILL.md").write_text("---\nname: revit-bridge\nenabled: true\n---\n\nB on.\n",
                                                     encoding="utf-8")
    assert store.get("builtin:revit-bridge/SKILL")["enabled"] is True
    assert store.active_prompt() == "X on.\n\n---\n\nB on.\n\n---\n\nB ref on.\n\n---\n\nC default."

    mounted = tmp_path / "mounted"
    mounted.mkdir()
    (mounted / "SKILL.md").write_text("---\nname: m\n---\n\nM default.\n", encoding="utf-8")
    (mounted / "off.md").write_text("---\nname: off\nenabled: false\n---\n\nOff.\n", encoding="utf-8")
    store = skill_store.SkillStore(tmp_path / "user2", mounted)
    assert [(s["id"], s["enabled"]) for s in store.list_all()] == [("builtin:SKILL", True), ("builtin:off", False)]
    assert store.active_prompt() == "M default."


def test_skill_edits_require_the_admin_password(make_client, env, tmp_path):
    empty = tmp_path / "no-skills"
    empty.mkdir()
    env.setenv("SKILLS_DIR", str(empty))  # override the wheel's skills with nothing
    client = make_client()
    assert client.get("/api/skills").json() == {"skills": []}
    # No X-Admin-Token: mutations are refused, not open (a cloud host always has ADMIN_PASSWORD).
    assert client.post("/api/skills", json={"name": "x", "content": "y"}).status_code == 403


def test_skill_crud_and_prompt_injection(make_client, env, tmp_path):
    env.setenv("ADMIN_PASSWORD", "hunter2")
    builtin = tmp_path / "plugin-skills"
    (builtin / "standards").mkdir(parents=True)
    (builtin / "SKILL.md").write_text("---\nname: revit-bridge\ndescription: protocol\n---\n\nAlways snapshot first.\n", encoding="utf-8")
    (builtin / "standards" / "walls.md").write_text("# Wall standard\n\nWalls are 200mm.\n", encoding="utf-8")
    env.setenv("SKILLS_DIR", str(builtin))
    client = make_client()

    listing = client.get("/api/skills").json()["skills"]
    assert [(s["id"], s["readonly"], s["layer"]) for s in listing] == [
        ("builtin:SKILL", True, ""), ("builtin:standards/walls", True, "standards"),
    ]
    assert listing[1]["name"] == "Wall standard" and listing[1]["description"] == "Walls are 200mm"

    detail = client.get("/api/skills/builtin:standards/walls").json()
    assert detail["content"].startswith("# Wall standard")
    assert client.get("/api/skills/builtin:../../etc/passwd").status_code in (400, 404)
    from backend.skill_store import get_skill_store
    assert get_skill_store().get("builtin:../plugin-skills/SKILL") is None
    assert get_skill_store().get("builtin:standards/../SKILL") is None
    assert client.delete("/api/skills/builtin:SKILL", headers=ADMIN).status_code == 404

    assert client.post("/api/skills", json={"name": "Office layout", "content": "Desks face north."}).status_code == 403
    created = client.post("/api/skills", json={"name": "Office layout", "content": "Desks face north."}, headers=ADMIN)
    assert created.status_code == 200
    skill_id = created.json()["id"]
    assert skill_id == "office-layout"
    assert (tmp_path / "data" / "skills" / "office-layout.md").is_file()

    # Enabled or not, a skill's text never reaches the chat's prompt (prompt injection by
    # a skill file is off the table along with the skills themselves).
    prompt = chat_module.build_system_prompt()
    assert not any(text in prompt for text in ("Always snapshot first.", "Walls are 200mm.", "Desks face north."))
    assert get_skill_store().get(skill_id)["enabled"] is True

    assert client.patch(f"/api/skills/{skill_id}", json={"enabled": False}, headers=ADMIN).json()["enabled"] is False
    assert chat_module.build_system_prompt() == prompt

    updated = client.put(f"/api/skills/{skill_id}", json={"description": "desk rule", "content": "Desks face south."}, headers=ADMIN)
    assert updated.json()["description"] == "desk rule" and updated.json()["content"] == "Desks face south."

    assert client.delete(f"/api/skills/{skill_id}", headers=ADMIN).json()["deleted"] == skill_id
    assert client.get(f"/api/skills/{skill_id}").status_code == 404


def test_skill_ids_are_bounded(make_client, env, tmp_path):
    """A very long name is cut to a file name every file system takes; an id that is not
    on disk is 404 however long, never an OSError (500)."""
    from backend.skill_store import MAX_ID_BYTES

    empty = tmp_path / "no-skills"
    empty.mkdir()
    env.setenv("SKILLS_DIR", str(empty))
    env.setenv("ADMIN_PASSWORD", "hunter2")
    client = make_client()

    created = client.post("/api/skills", json={"name": "x" * 300, "content": "Long."}, headers=ADMIN)
    assert created.status_code == 200, created.text
    skill_id = created.json()["id"]
    assert skill_id == "x" * MAX_ID_BYTES
    again = client.post("/api/skills", json={"name": "x" * 300, "content": "Again."}, headers=ADMIN).json()["id"]
    assert again.endswith("-2") and len(again.encode()) <= MAX_ID_BYTES
    wide = client.post("/api/skills", json={"name": "楼层" * 100, "content": "Wide."}, headers=ADMIN).json()["id"]
    assert len(wide.encode()) <= MAX_ID_BYTES and wide.startswith("楼层")
    assert client.get(f"/api/skills/{skill_id}").json()["content"] == "Long."

    for long_id in ("y" * 300, "z" * (MAX_ID_BYTES + 1)):
        assert client.get(f"/api/skills/{long_id}").status_code == 404
        assert client.put(f"/api/skills/{long_id}", json={"content": "x"}, headers=ADMIN).status_code == 404
        assert client.patch(f"/api/skills/{long_id}", json={"enabled": False}, headers=ADMIN).status_code == 404
        assert client.delete(f"/api/skills/{long_id}", headers=ADMIN).status_code == 404


def test_a_long_id_already_on_disk_can_still_be_opened_changed_and_deleted(make_client, env, tmp_path):
    """The id bound applies to ids being created; a file main could write (a 150-character
    name) is listed, so it must also open, change, toggle and delete."""
    empty = tmp_path / "no-skills"
    empty.mkdir()
    env.setenv("SKILLS_DIR", str(empty))
    env.setenv("ADMIN_PASSWORD", "hunter2")
    client = make_client()
    assert client.get("/api/skills").json() == {"skills": []}          # the store makes its directory
    long_id = "a" * 150
    (tmp_path / "data" / "skills" / f"{long_id}.md").write_text(
        "---\nname: long\nenabled: true\n---\n\nOld rule.\n", encoding="utf-8")

    assert [s["id"] for s in client.get("/api/skills").json()["skills"]] == [long_id]
    assert client.get(f"/api/skills/{long_id}").json()["content"] == "Old rule."
    assert client.put(f"/api/skills/{long_id}", json={"content": "New rule."}, headers=ADMIN).json()["content"] == "New rule."
    assert client.patch(f"/api/skills/{long_id}", json={"enabled": False}, headers=ADMIN).json()["enabled"] is False
    assert client.delete(f"/api/skills/{long_id}", headers=ADMIN).json()["deleted"] == long_id
    assert client.get("/api/skills").json() == {"skills": []}


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_a_saved_skill_keeps_the_mode_a_plain_write_gives(tmp_path):
    """The temporary file of the atomic write must not leave every skill owner-only (0600):
    an operator editing ./data as another user reads them like any file the host writes."""
    from backend import skill_store

    empty = tmp_path / "no-skills"
    empty.mkdir()
    user = tmp_path / "user"
    old = os.umask(0o022)
    try:
        store = skill_store.SkillStore(user, empty)
        store.save(None, {"name": "office"}, "Desks face north.")
        plain = user / "plain.txt"
        plain.write_text("x", encoding="utf-8")
        expected = stat.S_IMODE(plain.stat().st_mode)
        assert stat.S_IMODE((user / "office.md").stat().st_mode) == expected == 0o644
        store.update("office", content="Desks face south.")          # replaced, same mode
        assert stat.S_IMODE((user / "office.md").stat().st_mode) == expected
    finally:
        os.umask(old)


def test_a_skill_file_is_replaced_whole_or_not_at_all(tmp_path, monkeypatch):
    from backend import skill_store

    empty = tmp_path / "no-skills"
    empty.mkdir()
    user = tmp_path / "user"
    store = skill_store.SkillStore(user, empty)
    store.save(None, {"name": "office"}, "Desks face north.")

    # Text that cannot be written (a lone surrogate from a JSON escape) leaves the old file.
    with pytest.raises(UnicodeEncodeError):
        store.update("office", content="half \ud800 written")
    assert store.get("office")["content"] == "Desks face north."

    # A failure at the last step leaves the old file and no temporary file behind.
    def refuse(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(skill_store.os, "replace", refuse)
    with pytest.raises(OSError):
        store.update("office", content="Desks face south.")
    assert store.get("office")["content"] == "Desks face north."
    assert sorted(p.name for p in user.iterdir()) == ["office.md"]


def test_an_unreadable_skill_file_is_not_a_500(make_client, env, tmp_path):
    empty = tmp_path / "no-skills"
    empty.mkdir()
    env.setenv("SKILLS_DIR", str(empty))
    env.setenv("ADMIN_PASSWORD", "hunter2")
    client = make_client()
    assert client.get("/api/skills").json() == {"skills": []}          # the store makes its directory
    skills = tmp_path / "data" / "skills"
    (skills / "latin1.md").write_bytes("---\nname: caf\xe9\n---\n\nCaf\xe9.\n".encode("latin-1"))
    assert client.get("/api/skills").json() == {"skills": []}        # the list skips it
    assert client.get("/api/skills/latin1").status_code == 404           # and so does the detail
    assert client.put("/api/skills/latin1", json={"content": "x"}, headers=ADMIN).status_code == 404
    assert client.delete("/api/skills/latin1", headers=ADMIN).status_code == 200   # it can still go
    assert chat_module.build_system_prompt(bridge=True)                  # the prompt is unaffected


def test_skill_import_accepts_only_github(make_client, env):
    env.setenv("ADMIN_PASSWORD", "hunter2")
    client = make_client()
    resp = client.post("/api/skills/import", json={"url": "https://example.com/skill.md"}, headers=ADMIN)
    assert resp.status_code == 400
    from backend.api.skills import candidate_raw_urls
    assert candidate_raw_urls("https://github.com/o/r/blob/main/skills/r/SKILL.md") == [
        "https://raw.githubusercontent.com/o/r/main/skills/r/SKILL.md"]
    assert candidate_raw_urls("https://github.com/o/r")[0] == "https://raw.githubusercontent.com/o/r/main/skills/r/SKILL.md"


def test_logs_are_admin_only(make_client, env):
    assert make_client().get("/api/logs").status_code == 403      # the fixture's password, no token
    env.setenv("ADMIN_PASSWORD", "hunter2")
    client = make_client()
    assert client.get("/api/logs").status_code == 403
    assert client.get("/api/logs/verify", headers=ADMIN).json() == {"valid": True}
    assert client.get("/api/logs", headers=ADMIN).json()["total"] == 0
    assert client.get("/api/logs/stats", headers=ADMIN).json()["total"] == 0
    assert client.delete("/api/logs?before=2030-01-01", headers=ADMIN).json()["deleted"] == 0


def test_log_date_filters_include_the_end_day(make_client, env):
    from datetime import datetime, timedelta, timezone

    from backend.log_store import end_of_day, get_log_store

    env.setenv("ADMIN_PASSWORD", "hunter2")
    client = make_client()
    store = get_log_store()
    store.log(module="chat", user_input="today's question", assistant_output="answer")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")

    same_day = client.get("/api/logs", params={"start_date": today, "end_date": today}, headers=ADMIN).json()
    assert same_day["total"] == 1
    assert client.get("/api/logs", params={"end_date": yesterday}, headers=ADMIN).json()["total"] == 0
    assert client.get("/api/logs", params={"start_date": tomorrow}, headers=ADMIN).json()["total"] == 0
    assert client.get("/api/logs", params={"start_date": yesterday, "end_date": tomorrow}, headers=ADMIN).json()["total"] == 1

    assert end_of_day("2026-09-18") == "2026-09-19"
    assert end_of_day("2026-12-31") == "2027-01-01"
    assert end_of_day("2026-09-18 12:00:00") == "2026-09-18 12:00:00"  # explicit time: used as-is
    assert end_of_day("not-a-date") == "not-a-date"
