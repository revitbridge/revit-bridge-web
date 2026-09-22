"""Host surface: config.json, capability packs through the package, execution guards."""
from __future__ import annotations

import json

from backend.config import WebSettings

# Packs shipped read-only inside the revit-bridge 0.2 wheel.
BUILTIN_PACKS = 8


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


def test_builtin_packs_are_read_from_the_wheel_not_copied(client, env, tmp_path):
    tools = client.get("/api/v1/bridge/tools").json()
    names = sorted(t["name"] for t in tools)
    assert len(names) == BUILTIN_PACKS and len(set(names)) == BUILTIN_PACKS
    assert "create_wall" in names and "query_levels" in names

    # The package reads its built-in packs in place; the user directory under
    # the data root holds only what this host writes (nothing yet).
    user_dir = tmp_path / "data" / "capabilities"
    assert not list(user_dir.glob("*.yaml"))

    detail = client.get("/api/v1/bridge/tools/create_wall").json()
    assert detail["code_template"].strip()
    assert [p["name"] for p in detail["parameters"] if p.get("choices_from")] == ["level_name"]

    assert client.get("/api/v1/bridge/tools/nope").status_code == 404


def test_pack_edits_land_in_the_user_directory(client, tmp_path):
    resp = client.put("/api/v1/bridge/tools/query_levels", json={"description": "levels, sorted"})
    assert resp.status_code == 200
    assert resp.json()["description"] == "levels, sorted"
    assert resp.json()["revit_synced"] is False  # no Revit in tests
    user_dir = tmp_path / "data" / "capabilities"
    assert (user_dir / "query_levels.yaml").is_file()  # the built-in copy is untouched
    assert client.get("/api/v1/bridge/tools/query_levels").json()["description"] == "levels, sorted"

    blocked = client.put("/api/v1/bridge/tools/query_levels",
                         json={"code_template": "System.IO.File.Delete(\"x\"); return 1;"})
    assert blocked.status_code == 400 and blocked.json()["error"] == "blocked"

    # Deleting removes the user copy and hides the built-in one behind a marker.
    assert client.delete("/api/v1/bridge/tools/query_levels").json()["status"] == "deleted"
    assert not (user_dir / "query_levels.yaml").exists()
    assert (user_dir / "query_levels.disabled").is_file()
    assert len(client.get("/api/v1/bridge/tools").json()) == BUILTIN_PACKS - 1
    assert client.get("/api/v1/bridge/tools/query_levels").status_code == 404


def test_health_routes_without_revit(client):
    health = client.get("/api/v1/bridge/revit-health").json()
    assert health["revit_connected"] is False
    assert health["mode"] == "waiting_for_revit"
    assert health["ws_slots"]["connected"] == 0

    service = client.get("/api/v1/bridge/service-health").json()
    assert service["status"] == "ok" and service["connected_slots"] == 0
    assert client.get("/api/v1/bridge/slots").json()["max_slots"] == 5


def test_openapi_lists_the_v1_contract(client):
    spec = client.get("/openapi.json").json()
    paths = set(spec["paths"])
    bridge = {p for p in paths if p.startswith("/api/v1/bridge/")}
    assert bridge == {
        "/api/v1/bridge/snapshot", "/api/v1/bridge/query",
        "/api/v1/bridge/tools", "/api/v1/bridge/tools/{name}", "/api/v1/bridge/tools/{name}/choices",
        "/api/v1/bridge/tools/{name}/missing-params", "/api/v1/bridge/tools/{name}/run",
        "/api/v1/bridge/spec/reconcile", "/api/v1/bridge/spec/confirm",
        "/api/v1/bridge/execute", "/api/v1/bridge/solidify",
        "/api/v1/bridge/evidence", "/api/v1/bridge/evidence/{evidence_id}/validate",
        "/api/v1/bridge/trigger-selection",
        "/api/v1/bridge/revit-health", "/api/v1/bridge/service-health", "/api/v1/bridge/slots",
    }
    assert {"/health", "/config.json", "/api/chat", "/api/skills", "/api/skills/import",
            "/api/logs", "/api/logs/stats", "/api/logs/verify"} <= paths
    # Nothing from the retired RAG / orchestration surface survived.
    assert not [p for p in paths if any(k in p for k in ("generate", "orchestrate", "classify", "match-tool", "search", "t2r"))]
    assert json.dumps(spec)  # serialisable, exported to docs/api-v1.json


def test_openapi_describes_the_error_contract(client):
    """Bridge routes declare their {error, message?} statuses; other routes keep FastAPI's shape."""
    spec = client.get("/openapi.json").json()
    error_ref = {"$ref": "#/components/schemas/ErrorBody"}

    query = spec["paths"]["/api/v1/bridge/query"]["post"]["responses"]
    assert set(query) == {"200", "400", "403", "422", "500", "503"}
    for status in ("400", "422", "503"):
        assert query[status]["content"]["application/json"]["schema"] == error_ref
    assert "HTTPValidationError" not in json.dumps(query)

    run = spec["paths"]["/api/v1/bridge/tools/{name}/run"]["post"]["responses"]
    assert set(run) == {"200", "400", "403", "404", "422", "503"}
    # Every bridge route with a body or parameters declares its own 422; none falls back.
    for path, item in spec["paths"].items():
        if path.startswith("/api/v1/bridge/"):
            for op in item.values():
                assert "HTTPValidationError" not in json.dumps(op["responses"]), path

    skill = spec["paths"]["/api/skills/{skill_id}"]["put"]["responses"]
    assert skill["422"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/HTTPValidationError"}
    body = spec["components"]["schemas"]["ErrorBody"]
    assert body["required"] == ["error"] and body.get("additionalProperties", True) is not False

    # The handler is scoped the same way: a bad body outside the bridge keeps {detail}.
    outside = client.post("/api/skills", json={"content": 1})
    assert outside.status_code in (403, 422, 503)
    if outside.status_code == 422:
        assert "detail" in outside.json() and "error" not in outside.json()
    inside = client.post("/api/v1/bridge/query", json={"args": {}})
    assert inside.status_code == 422 and inside.json()["error"] == "invalid_args"
