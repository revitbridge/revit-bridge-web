"""Changing capability packs is admin only: PUT/DELETE /tools/{name} and POST /solidify.

A pack's code later runs in a designer's Revit once they confirm its card, so
creating, editing or deleting one takes the admin password - the same
X-Admin-Token check as the skill writes, /api/logs and the device list.
Reading, choosing, asking, confirming and running stay open as before.
"""
from __future__ import annotations

import pytest

from tests.test_api_v1 import B, WALL, confirm, spec_for

ADMIN = {"X-Admin-Token": "hunter2"}
SOLIDIFY = {"name": "count_walls", "description": "How many walls", "parameters": [],
            "code": "return new FilteredElementCollector(document).OfCategory(BuiltInCategory.OST_Walls).GetElementCount();"}


def writes():
    """The three pack-changing calls, each with a body that would succeed."""
    return [
        ("put", f"{B}/tools/query_levels", {"description": "changed"}),
        ("delete", f"{B}/tools/create_beam", None),
        ("post", f"{B}/solidify", SOLIDIFY),
    ]


def call(client, method: str, path: str, body, headers=None):
    kwargs = {"headers": headers or {}}
    if body is not None:
        kwargs["json"] = body
    return getattr(client, method)(path, **kwargs)


def untouched(client, tmp_path):
    names = {t["name"] for t in client.get(f"{B}/tools").json()}
    assert "create_beam" in names and "count_walls" not in names and len(names) == 8
    assert client.get(f"{B}/tools/query_levels").json()["description"] != "changed"
    assert not list((tmp_path / "data" / "capabilities").glob("*.yaml"))


def test_without_admin_password_packs_cannot_be_changed(client, tmp_path):
    for method, path, body in writes():
        for headers in ({}, ADMIN):
            resp = call(client, method, path, body, headers)
            assert resp.status_code == 503, (method, path, resp.text)
            assert resp.json() == {"error": "admin_disabled",
                                   "message": "Set ADMIN_PASSWORD to change capability packs"}
    untouched(client, tmp_path)


@pytest.fixture
def admin_client(make_client, env):
    env.setenv("ADMIN_PASSWORD", "hunter2")
    return make_client()


def test_without_the_right_token_packs_cannot_be_changed(admin_client, device_pair, tmp_path):
    client = admin_client
    for method, path, body in writes():
        for headers in ({}, {"X-Admin-Token": ""}, {"X-Admin-Token": "hunter3"}, {"X-Admin-Token": "HUNTER2"},
                        device_pair):            # a device's own key is no admin password
            resp = call(client, method, path, body, headers)
            assert resp.status_code == 403, (method, path, headers, resp.text)
            assert resp.json() == {"error": "admin_required", "message": "Wrong or missing X-Admin-Token"}
        # The check comes before the body is looked at: no hint about what would be valid.
        bad = call(client, method, path, {"parameters": "not a list", "name": 1} if body is not None else None)
        assert bad.status_code == 403
    untouched(client, tmp_path)


def test_with_the_admin_password_packs_change(admin_client, tmp_path):
    client = admin_client
    updated = client.put(f"{B}/tools/query_levels", json={"description": "changed"}, headers=ADMIN)
    assert updated.status_code == 200 and updated.json()["description"] == "changed"
    assert client.delete(f"{B}/tools/create_beam", headers=ADMIN).json() == {"status": "deleted", "name": "create_beam"}
    solid = client.post(f"{B}/solidify", json=SOLIDIFY, headers=ADMIN)
    assert solid.status_code == 200 and solid.json()["name"] == "count_walls"
    names = {t["name"] for t in client.get(f"{B}/tools").json()}
    assert "create_beam" not in names and "count_walls" in names
    # The route's own refusals still come after the admin check.
    blocked = client.put(f"{B}/tools/query_levels", json={"code_template": "System.IO.File.Delete(\"x\"); return 1;"},
                         headers=ADMIN)
    assert blocked.status_code == 400 and blocked.json()["error"] == "blocked"
    assert client.delete(f"{B}/tools/nope", headers=ADMIN).status_code == 404


def test_anyone_still_lists_reads_asks_and_runs_packs(revit, admin_client):
    client = admin_client
    assert len(client.get(f"{B}/tools").json()) == 8
    assert client.get(f"{B}/tools/create_wall").json()["name"] == "create_wall"
    assert client.get(f"{B}/tools/create_wall/choices").status_code == 200
    asked = client.post(f"{B}/tools/create_wall/missing-params", json={"known": {}})
    assert asked.status_code == 200 and asked.json()
    token = confirm(client, spec_for("create_wall", **WALL))
    run = client.post(f"{B}/tools/create_wall/run", json={"params": WALL, "token": token})
    assert run.status_code == 200 and run.json()["success"] is True


def test_the_contract_says_so(client):
    spec = client.get("/openapi.json").json()
    for path, method in ((f"{B}/tools/{{name}}", "put"), (f"{B}/tools/{{name}}", "delete"), (f"{B}/solidify", "post")):
        op = spec["paths"][path][method]
        assert {"403", "503"} <= set(op["responses"]), (path, method)
        assert "admin_required" in op["responses"]["403"]["description"]
        assert "admin_disabled" in op["responses"]["503"]["description"]
        assert any(p["name"] == "x-admin-token" and p["in"] == "header" for p in op["parameters"]), (path, method)
    # Reading a pack is not an admin route.
    assert not any(p["name"] == "x-admin-token" for p in spec["paths"][f"{B}/tools/{{name}}"]["get"]["parameters"])


def test_the_model_has_no_door_to_the_packs(client, tmp_path, monkeypatch):
    """The chat's tools only read packs: a model that names a writing tool gets unknown_tool."""
    import httpx

    from backend import llm as llm_module
    from backend.api.model_tools import TOOL_NAMES
    from tests.fake_model import FakeModel, text_turn, tool_turn

    assert TOOL_NAMES == {"get_project_snapshot", "query", "list_tools", "get_tool_choices", "missing_params",
                          "reconcile", "get_guidance", "propose_spec"}
    fake = FakeModel([tool_turn(("solidify", SOLIDIFY), ("delete_tool", {"name": "create_beam"})), text_turn("no")])
    real_client = httpx.AsyncClient
    monkeypatch.setattr(llm_module.httpx, "AsyncClient",
                        lambda *a, **k: real_client(*a, **{**k, "transport": fake.transport}))
    resp = client.post("/api/chat", json={"message": "save it as a pack"},
                       headers={"X-LLM-Base-Url": "https://model.example/v1", "X-LLM-Model": "m", "X-LLM-Key": "k"})
    assert resp.status_code == 200
    assert fake.tool_results("solidify")[0]["error"] == "unknown_tool"
    assert fake.tool_results("delete_tool")[0]["error"] == "unknown_tool"
    untouched(client, tmp_path)


@pytest.fixture
def device_pair(admin_client):
    paired = admin_client.post(f"{B}/devices/pair", json={}).json()
    return {"X-Device-Id": paired["device_id"], "X-Device-Key": paired["browser_key"]}
