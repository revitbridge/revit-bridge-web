"""Skill directory (user + read-only built-ins) and admin-gated logs."""
from __future__ import annotations

from backend.api import chat as chat_module

ADMIN = {"X-Admin-Token": "hunter2"}


def test_skill_edits_require_the_admin_password(client):
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
