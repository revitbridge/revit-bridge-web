"""Skill directory (user + read-only built-ins) and admin-gated logs."""
from __future__ import annotations

from backend.api import chat as chat_module

ADMIN = {"X-Admin-Token": "hunter2"}


def test_the_wheel_puts_the_protocol_and_revit_core_in_the_prompt(make_client, env, tmp_path):
    """Without SKILLS_DIR the wheel's skills are listed, but only the revit-bridge protocol
    and revit-core are enabled; revit-addin-dev, references and lessons stay off. The
    system prompt is the package's host instructions, those two SKILL bodies and one
    line that sends the model to get_guidance for the lessons."""
    from revit_bridge import host_instructions
    from revit_bridge.guidance import CATEGORIES

    client = make_client()
    listing = client.get("/api/skills").json()["skills"]
    assert all(s["readonly"] and s["source"] == "builtin" for s in listing)
    on = [s["id"] for s in listing if s["enabled"]]
    assert on == ["builtin:revit-bridge/SKILL", "builtin:revit-core/SKILL"]
    assert "builtin:revit-addin-dev/SKILL" in [s["id"] for s in listing]
    assert any(s["id"].startswith("builtin:revit-bridge/references/") for s in listing)
    lessons = [s for s in listing if s["id"].startswith("builtin:revit-core/lessons/")]
    assert lessons and not any(s["enabled"] for s in lessons)

    protocol = client.get("/api/skills/builtin:revit-bridge/SKILL").json()
    core = client.get("/api/skills/builtin:revit-core/SKILL").json()
    addin = client.get("/api/skills/builtin:revit-addin-dev/SKILL").json()
    assert protocol["content"].startswith("# revit-bridge") and protocol["enabled"] is True
    assert core["content"].startswith("# revit-core") and core["enabled"] is True
    assert addin["enabled"] is False
    # Read-only: the store refuses to edit or delete anything from the wheel.
    from backend.skill_store import get_skill_store
    assert get_skill_store().update("builtin:revit-bridge/SKILL", content="x") is None
    assert get_skill_store().delete("builtin:revit-bridge/SKILL") is False

    prompt = chat_module.build_system_prompt()
    assert prompt == (host_instructions().strip() + "\n\n## Skills\n\n" + protocol["content"]
                      + "\n\n---\n\n" + core["content"] + "\n\n## Guidance\n\n" + chat_module.GUIDANCE_POINTER)
    assert addin["content"] not in prompt
    for lesson in lessons:                                   # titles in the index, bodies not
        assert get_skill_store().get(lesson["id"])["content"] not in prompt
    pointer = chat_module.GUIDANCE_POINTER
    assert "\n" not in pointer and "get_guidance" in pointer
    assert all(category in pointer for category in CATEGORIES)
    assert len(prompt.encode("utf-8")) < 25_000

    # With no built-in skills at all: the host instructions and the pointer.
    empty = tmp_path / "no-skills"
    empty.mkdir()
    env.setenv("SKILLS_DIR", str(empty))
    make_client()
    assert chat_module.build_system_prompt() == (host_instructions().strip() + "\n\n## Guidance\n\n"
                                                 + chat_module.GUIDANCE_POINTER)
    assert chat_module.build_system_prompt(bridge=False) == chat_module.BASE_PROMPT


def test_builtin_enabled_default_depends_on_the_source(tmp_path, monkeypatch):
    """Wheel skills: revit-bridge and revit-core SKILL.md on, every other file off,
    unless the file says otherwise. A mounted SKILLS_DIR: on unless the file says
    ``enabled: false``."""
    from backend import skill_store

    wheel = tmp_path / "wheel-skills"
    files = {
        "extra/SKILL.md": "---\nname: extra\nenabled: true\n---\n\nX on.\n",
        "revit-addin-dev/SKILL.md": "---\nname: revit-addin-dev\n---\n\nAdd-in off.\n",
        "revit-bridge/SKILL.md": "---\nname: revit-bridge\n---\n\nB default.\n",
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
        ("builtin:revit-bridge/SKILL", True), ("builtin:revit-bridge/references/on", True),
        ("builtin:revit-bridge/references/r", False),
        ("builtin:revit-core/SKILL", True), ("builtin:revit-core/lessons/l", False),
        ("builtin:revit-views/SKILL", False),
    ]
    assert store.get("builtin:revit-bridge/references/r")["enabled"] is False
    assert store.active_prompt() == "X on.\n\n---\n\nB default.\n\n---\n\nB ref on.\n\n---\n\nC default."

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
    # No ADMIN_PASSWORD configured: mutations are unavailable, not open.
    assert client.post("/api/skills", json={"name": "x", "content": "y"}).status_code == 503


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

    prompt = chat_module.build_system_prompt()
    assert "Always snapshot first." in prompt and "Walls are 200mm." in prompt and "Desks face north." in prompt

    assert client.patch(f"/api/skills/{skill_id}", json={"enabled": False}, headers=ADMIN).json()["enabled"] is False
    assert "Desks face north." not in chat_module.build_system_prompt()

    updated = client.put(f"/api/skills/{skill_id}", json={"description": "desk rule", "content": "Desks face south."}, headers=ADMIN)
    assert updated.json()["description"] == "desk rule" and updated.json()["content"] == "Desks face south."

    assert client.delete(f"/api/skills/{skill_id}", headers=ADMIN).json()["deleted"] == skill_id
    assert client.get(f"/api/skills/{skill_id}").status_code == 404


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
    assert make_client().get("/api/logs").status_code == 503
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
