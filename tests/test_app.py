"""Host surface: config.json, capability packs through the package, execution guards."""
from __future__ import annotations

import json

from backend.config import WebSettings

BUILTIN_PACKS = 11


def test_health_and_config_json(make_client, env):
    env.setenv("PUBLIC_WS_BASE", "wss://relay.example/api/v1/bridge/ws/")
    env.setenv("ADMIN_PASSWORD", "s3cret")
    env.setenv("MAX_SLOTS", "3")
    client = make_client()

    assert client.get("/health").json() == {"status": "ok", "version": "0.1.0"}

    resp = client.get("/config.json")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache"
    assert resp.json() == {
        "apiBase": "",  # served by the backend, the SPA is same-origin by construction
        "wsBase": "wss://relay.example/api/v1/bridge/ws",
        "features": {
            "byoModel": True,
            "serverModel": False,
            "admin": True,
            "slotTokenRequired": False,
            "maxSlots": 3,
        },
    }


def test_settings_defaults_come_from_env_only():
    s = WebSettings.from_env({})
    assert (s.host, s.port, s.max_slots, s.cors_origins) == ("0.0.0.0", 7860, 5, ())
    assert s.config_json() == {"apiBase": "", "wsBase": "", "features": {
        "byoModel": True, "serverModel": False, "admin": False, "slotTokenRequired": False, "maxSlots": 5}}
    s = WebSettings.from_env({"LLM_MODEL": "m", "LLM_API_KEY": "k", "CORS_ORIGINS": "https://a, https://b"})
    assert s.server_model_configured and s.cors_origins == ("https://a", "https://b")


def test_capability_packs_are_seeded_into_data_dir_and_listed(client, env, tmp_path):
    tools = client.get("/api/v1/bridge/tools").json()
    names = sorted(t["name"] for t in tools)
    assert len(names) == BUILTIN_PACKS and len(set(names)) == BUILTIN_PACKS
    assert "create_wall" in names and "query_levels" in names

    seeded = tmp_path / "data" / "capabilities"
    assert sorted(p.stem for p in seeded.glob("*.yaml")) == names

    detail = client.get("/api/v1/bridge/tools/create_wall").json()
    assert detail["code_template"].strip()
    assert [p["name"] for p in detail["parameters"] if p.get("choices_from")] == ["level_name"]

    assert client.get("/api/v1/bridge/tools/nope").status_code == 404


def test_pack_edits_persist_in_the_host_copy(client):
    resp = client.put("/api/v1/bridge/tools/query_levels", json={"description": "levels, sorted"})
    assert resp.status_code == 200
    assert resp.json()["description"] == "levels, sorted"
    assert resp.json()["revit_synced"] is False  # no Revit in tests

    blocked = client.put("/api/v1/bridge/tools/query_levels",
                         json={"code_template": "System.IO.File.Delete(\"x\"); return 1;"})
    assert blocked.status_code == 422

    assert client.delete("/api/v1/bridge/tools/query_levels").json()["status"] == "deleted"
    assert len(client.get("/api/v1/bridge/tools").json()) == BUILTIN_PACKS - 1


def test_execute_is_reviewed_then_needs_a_revit(client):
    blocked = client.post("/api/v1/bridge/execute", json={"code": "System.Diagnostics.Process.Start(\"cmd\");"})
    assert blocked.status_code == 400
    assert blocked.json()["detail"]["error"] == "blocked"

    unreachable = client.post("/api/v1/bridge/execute", json={"code": "return 1;"})
    assert unreachable.status_code == 502
    assert "127.0.0.1:1" in unreachable.json()["detail"]


def test_run_tool_validates_parameters_before_touching_revit(client):
    resp = client.post("/api/v1/bridge/tools/create_wall/run", json={"params": {}})
    assert resp.status_code == 422
    assert "level_name" in resp.json()["detail"]

    resp = client.post("/api/v1/bridge/tools/query_levels/run", json={"params": {}})
    assert resp.status_code == 502  # valid, reviewed, no Revit listening


def test_solidify_reviews_code_and_writes_a_pack(client, tmp_path):
    resp = client.post("/api/v1/bridge/solidify", json={
        "name": "count_walls",
        "code": "return new FilteredElementCollector(document).OfCategory(BuiltInCategory.OST_Walls).GetElementCount();",
        "description": "How many walls",
        "tags": ["query"],
    })
    assert resp.status_code == 200
    assert resp.json() == {"status": "solidified", "name": "count_walls",
                           "display_name": "Count Walls", "revit_synced": False}
    assert (tmp_path / "data" / "capabilities" / "count_walls.yaml").is_file()

    blocked = client.post("/api/v1/bridge/solidify", json={"name": "evil", "code": "System.IO.File.Delete(\"x\");"})
    assert blocked.status_code == 400


def test_health_routes_without_revit(client):
    health = client.get("/api/v1/bridge/revit-health").json()
    assert health["revit_connected"] is False
    assert health["mode"] == "waiting_for_revit"
    assert health["ws_slots"]["connected"] == 0

    service = client.get("/api/v1/bridge/service-health").json()
    assert service["status"] == "ok" and service["connected_slots"] == 0
    assert client.get("/api/v1/bridge/slots").json()["max_slots"] == 5

    units = client.get("/api/v1/bridge/project-units").json()
    assert "error" in units and units["current_setting"] == "mm"


def test_openapi_lists_the_contract(client):
    spec = client.get("/openapi.json").json()
    paths = set(spec["paths"])
    expected = {
        "/health", "/config.json", "/api/chat", "/api/skills", "/api/skills/import",
        "/api/logs", "/api/logs/stats", "/api/logs/verify",
        "/api/v1/bridge/tools", "/api/v1/bridge/tools/{name}", "/api/v1/bridge/tools/{name}/run",
        "/api/v1/bridge/tools/{name}/choices", "/api/v1/bridge/execute", "/api/v1/bridge/solidify",
        "/api/v1/bridge/query-revit", "/api/v1/bridge/trigger-selection",
        "/api/v1/bridge/revit-health", "/api/v1/bridge/service-health", "/api/v1/bridge/slots",
        "/api/v1/bridge/unit", "/api/v1/bridge/project-units",
    }
    assert expected <= paths, expected - paths
    # Nothing from the retired RAG / orchestration surface survived.
    assert not [p for p in paths if any(k in p for k in ("generate", "orchestrate", "classify", "match-tool", "search", "t2r"))]
    assert json.dumps(spec)  # serialisable, exported to docs/api-v0.json
