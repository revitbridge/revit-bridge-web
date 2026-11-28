"""Host surface: config.json, capability packs through the package, execution guards."""
from __future__ import annotations

import json

from backend.config import WebSettings
from tests.conftest import builtin_pack_names


def test_health_and_config_json(make_client, env):
    env.setenv("PUBLIC_WS_BASE", "wss://relay.example/api/v1/bridge/ws/")
    env.setenv("ADMIN_PASSWORD", "s3cret")
    env.setenv("MAX_DEVICES", "3")
    client = make_client()

    assert client.get("/health").json() == {"status": "ok", "version": "0.1.0"}

    resp = client.get("/config.json")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache"
    assert resp.json() == {
        "apiBase": "",  # served by the backend, the SPA is same-origin by construction
        "wsBase": "wss://relay.example/api/v1/bridge/ws",
        "features": {
            "mode": "cloud",
            "byoModel": True,
            "serverModel": False,
            "admin": True,
            "maxDevices": 3,
            # the suite's chat is the one loop (LLM_SINGLE_MODEL=1, tests/conftest.py)
            "roles": False,
            # a cloud host's visitor picks no mode (an admin may: tests/test_chat_modes.py)
            "runModes": ["confirm"],
            "autoRun": {"max_steps": 8, "excluded_tools": ["delete_elements", "delete_selected", "delete_elements_by_category"]},
            "authoring": False,
            "chatModes": ["auto"],
            "chatModeDefault": "single",
            # kept until the connect page of 7.3 reads maxDevices
            "slotTokenRequired": False,
            "maxSlots": 3,
            # each role's model name: LLM_MODEL (unset here) without a models file
            "models": {"analyst": None, "planner": None, "supervisor": None, "optimizer": None},
            "modelLibrary": [],
            "modelSelection": {"analyst": None, "planner": None, "supervisor": None, "optimizer": None},
            "modelsFile": False,
        },
    }


def test_settings_defaults_come_from_env_only():
    # A cloud host needs ADMIN_PASSWORD (DEPLOY_MODE); the local defaults are in test_deploy_mode.py.
    s = WebSettings.from_env({"DEPLOY_MODE": "cloud", "ADMIN_PASSWORD": "pw"})
    assert (s.host, s.port, s.max_devices, s.cors_origins) == ("0.0.0.0", 7860, 20, ())
    assert s.config_json() == {"apiBase": "", "wsBase": "", "features": {
        "mode": "cloud", "byoModel": True, "serverModel": False, "admin": True, "maxDevices": 20,
        "roles": True, "chatModes": ["auto"], "chatModeDefault": "roles", "slotTokenRequired": False,
        "runModes": ["confirm"],
        "autoRun": {"max_steps": 8, "excluded_tools": ["delete_elements", "delete_selected", "delete_elements_by_category"]},
        "authoring": False,
        "maxSlots": 20}}
    # The analyst is off by default (planner + supervisor).
    assert s.roles_analyst is False and s.chat_mode_default == "roles"
    s = WebSettings.from_env({"DEPLOY_MODE": "cloud", "ADMIN_PASSWORD": "pw",     # CORS_ORIGINS: cloud only
                              "LLM_BASE_URL": "https://m.example/v1", "LLM_MODEL": "m", "LLM_API_KEY": "k",
                              "CORS_ORIGINS": "https://a, https://b"})
    assert s.server_model_configured and s.cors_origins == ("https://a", "https://b")
    # Without its URL the server's key could only go to a visitor's endpoint: not a server model.
    assert not WebSettings.from_env({"LLM_MODEL": "m", "LLM_API_KEY": "k"}).server_model_configured


def _spa(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>spa</title>", encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    return dist


def test_the_spa_fallback_never_answers_500_and_keeps_api_typos_out(env, tmp_path):
    from fastapi.testclient import TestClient

    from backend.main import create_app

    client = TestClient(create_app(frontend_dist=_spa(tmp_path)))
    # A NUL byte in the path is the SPA page, not a crash.
    nul = client.get("/%00")
    assert nul.status_code == 200 and "spa" in nul.text
    assert client.get("/a%00b/c").status_code == 200

    # An unknown address under /api is a JSON 404 in the bridge's error shape, not the page.
    for path in ("/api", "/api/", "/api/nope", "/api/v1/bridge/nope", "/api/v1/bridge/tool"):
        resp = client.get(path)
        assert resp.status_code == 404, path
        assert resp.headers["content-type"].startswith("application/json"), path
        assert resp.json()["error"] == "not_found", path

    # Nothing real is shadowed: routes, the runtime config, files of the build, the SPA itself.
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/config.json").json()["apiBase"] == ""
    assert "paths" in client.get("/openapi.json").json()
    assert {t["name"] for t in client.get("/api/v1/bridge/tools").json()} == builtin_pack_names()
    assert client.get("/api/skills").status_code == 200
    assert client.get("/favicon.svg").text == "<svg/>"
    assert client.get("/assets/app.js").text == "console.log(1)"
    for page in ("/", "/task", "/capabilities/packs", "/apiary"):
        resp = client.get(page)
        assert resp.status_code == 200 and "spa" in resp.text, page
    # Traversal stays inside the build.
    assert "spa" in client.get("/..%2f..%2fpyproject.toml").text


def test_cors_wildcard_never_allows_credentials(make_client, env):
    """Authentication is header-based; a wildcard must not turn into "any origin, with credentials"."""
    preflight = {"Origin": "https://evil.example", "Access-Control-Request-Method": "POST",
                 "Access-Control-Request-Headers": "x-device-key"}

    env.setenv("CORS_ORIGINS", "*")
    client = make_client()
    resp = client.options("/api/v1/bridge/query", headers=preflight)
    assert resp.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in resp.headers
    simple = client.get("/health", headers={"Origin": "https://evil.example", "Cookie": "a=b"})
    assert simple.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in simple.headers

    # An explicit list keeps credentials, for its own origins only.
    env.setenv("CORS_ORIGINS", "https://spa.example")
    client = make_client()
    ok = client.options("/api/v1/bridge/query", headers={**preflight, "Origin": "https://spa.example"})
    assert ok.headers["access-control-allow-origin"] == "https://spa.example"
    assert ok.headers["access-control-allow-credentials"] == "true"
    refused = client.options("/api/v1/bridge/query", headers=preflight)
    assert "access-control-allow-origin" not in refused.headers


def test_builtin_packs_are_read_from_the_wheel_not_copied(client, env, tmp_path):
    tools = client.get("/api/v1/bridge/tools").json()
    names = sorted(t["name"] for t in tools)
    assert names == sorted(builtin_pack_names())             # every wheel pack, each once, nothing else
    assert "create_wall" in names and "query_levels" in names

    # The package reads its built-in packs in place; the user directory under
    # the data root holds only what this host writes (nothing yet).
    user_dir = tmp_path / "data" / "capabilities"
    assert not list(user_dir.glob("*.yaml"))

    detail = client.get("/api/v1/bridge/tools/create_wall").json()
    assert detail["code_template"].strip()
    assert [p["name"] for p in detail["parameters"] if p.get("choices_from")] == ["level_name", "type_name", "top_level_name"]

    assert client.get("/api/v1/bridge/tools/nope").status_code == 404


def test_pack_edits_land_in_the_user_directory(make_client, env, tmp_path):
    # Editing and deleting packs take the admin password (tests/test_pack_admin.py).
    env.setenv("ADMIN_PASSWORD", "hunter2")
    client = make_client()
    admin = {"X-Admin-Token": "hunter2"}
    user_dir = tmp_path / "data" / "capabilities"
    saved = client.post("/api/v1/bridge/solidify", headers=admin, json={
        "name": "count_walls", "description": "How many walls", "parameters": [],
        "code": "return new FilteredElementCollector(document).OfCategory(BuiltInCategory.OST_Walls).GetElementCount();"})
    assert saved.status_code == 200 and (user_dir / "count_walls.yaml").is_file()
    resp = client.put("/api/v1/bridge/tools/count_walls", json={"description": "walls, counted"}, headers=admin)
    assert resp.status_code == 200
    assert resp.json()["description"] == "walls, counted"
    assert resp.json()["revit_synced"] is False  # no Revit in tests
    assert client.get("/api/v1/bridge/tools/count_walls").json()["description"] == "walls, counted"

    blocked = client.put("/api/v1/bridge/tools/count_walls",
                         json={"code_template": "System.IO.File.Delete(\"x\"); return 1;"}, headers=admin)
    assert blocked.status_code == 400 and blocked.json()["error"] == "blocked"

    # Deleting removes the user file; a built-in pack is neither copied nor hidden.
    assert client.delete("/api/v1/bridge/tools/count_walls", headers=admin).json()["status"] == "deleted"
    assert not (user_dir / "count_walls.yaml").exists()
    for method in ("put", "delete"):
        kwargs = {"json": {"description": "levels, sorted"}} if method == "put" else {}
        refused = getattr(client, method)("/api/v1/bridge/tools/query_levels", headers=admin, **kwargs)
        assert refused.status_code == 409 and refused.json()["error"] == "builtin_read_only"
    assert not list(user_dir.glob("query_levels*"))           # no copy, no .disabled marker
    assert sorted(t["name"] for t in client.get("/api/v1/bridge/tools").json()) == sorted(builtin_pack_names())


def test_health_routes_without_revit(client):
    health = client.get("/api/v1/bridge/revit-health").json()
    assert health["revit_connected"] is False
    assert health["mode"] == "waiting_for_revit"
    assert health["devices"] == {"max_devices": 20, "connected": 0}

    service = client.get("/api/v1/bridge/service-health").json()
    assert service["status"] == "ok" and service["connected_devices"] == 0
    assert client.get("/api/v1/bridge/slots").json() == {"max_devices": 20, "connected": 0}


def test_openapi_lists_the_v1_contract(client):
    spec = client.get("/openapi.json").json()
    paths = set(spec["paths"])
    bridge = {p for p in paths if p.startswith("/api/v1/bridge/")}
    assert bridge == {
        "/api/v1/bridge/snapshot", "/api/v1/bridge/query", "/api/v1/bridge/mentions",
        "/api/v1/bridge/mentions/preview", "/api/v1/bridge/sketch/capture", "/api/v1/bridge/sketch/resolve",
        "/api/v1/bridge/tools", "/api/v1/bridge/tools/{name}", "/api/v1/bridge/tools/{name}/choices",
        "/api/v1/bridge/tools/{name}/missing-params", "/api/v1/bridge/tools/{name}/run",
        "/api/v1/bridge/tools/{name}/export", "/api/v1/bridge/tools/import", "/api/v1/bridge/tools/draft",
        "/api/v1/bridge/spec/reconcile", "/api/v1/bridge/spec/confirm",
        "/api/v1/bridge/check", "/api/v1/bridge/dry-run", "/api/v1/bridge/choices",
        "/api/v1/bridge/execute", "/api/v1/bridge/solidify",
        "/api/v1/bridge/evidence", "/api/v1/bridge/evidence/{evidence_id}/validate",
        "/api/v1/bridge/remote-trials", "/api/v1/bridge/remote-trials/{evidence_id}",
        "/api/v1/bridge/trigger-selection", "/api/v1/bridge/selection-state",
        "/api/v1/bridge/watch-selection", "/api/v1/bridge/watch-selection/{session_id}/{watch_id}",
        "/api/v1/bridge/host-functions",
        "/api/v1/bridge/workflows", "/api/v1/bridge/workflows/{name}", "/api/v1/bridge/workflows/validate",
        "/api/v1/bridge/workflows/import", "/api/v1/bridge/workflows/{name}/export",
        "/api/v1/bridge/workflows/tools/{name}/result-paths", "/api/v1/bridge/workflows/optimize-text",
        "/api/v1/bridge/devices", "/api/v1/bridge/devices/{device_id}",
        "/api/v1/bridge/devices/pair", "/api/v1/bridge/devices/redeem",
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
