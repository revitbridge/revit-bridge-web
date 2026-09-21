"""The v1 contract under /api/v1/bridge: one test per endpoint, the package behind a fake add-in."""
from __future__ import annotations

from fastapi.testclient import TestClient

from revit_bridge import execution

from backend.api import bridge as bridge_module
from tests.fake_revit import FakeRevit, model_handler

B = "/api/v1/bridge"
WALL = {"level_name": "L1", "start_x": 0, "start_y": 0, "end_x": 5000, "end_y": 0, "height": 3000}


def spec_for(tool: str, **params) -> dict:
    """A run_tool TaskSpec whose every parameter is an answered question."""
    return {
        "task": f"run {tool}",
        "action": {"kind": "run_tool", "tool": tool},
        "parameters": [
            {"name": k, "value": v, "unit": "mm" if isinstance(v, (int, float)) else None,
             "source": "answer", "evidence": f"q_{k}"}
            for k, v in params.items()
        ],
    }


def code_spec(code: str, parameters: list | None = None) -> dict:
    return {"task": "run code", "action": {"kind": "execute_code", "code": code, "code_parameters": parameters},
            "parameters": []}


def confirm(client: TestClient, spec: dict) -> str:
    resp = client.post(f"{B}/spec/confirm", json={"spec": spec})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def app_client(env) -> TestClient:
    from backend.main import create_app
    return TestClient(create_app())


# -- snapshot / query --------------------------------------------------------------

def test_snapshot_is_the_package_snapshot(revit, client):
    snap = client.get(f"{B}/snapshot", params={"categories": ["OST_Walls,OST_Doors"]}).json()
    assert snap["schema_version"] == 1 and snap["document"]["title"] == "Project1"
    assert snap["units"]["length"] == "mm"
    assert [lv["name"] for lv in snap["levels"]] == ["L1", "L2"]
    assert snap["grids"] == {"count": 2, "names": ["A", "1"]}
    assert [t["category"] for t in snap["family_types"]] == ["OST_Walls", "OST_Doors"]
    assert len(snap["fingerprint"]) == 16 and snap["warnings"] == []

    assert [t["category"] for t in client.get(f"{B}/snapshot").json()["family_types"]] == [
        "OST_Walls", "OST_StructuralColumns", "OST_StructuralFraming", "OST_Floors", "OST_Doors", "OST_Windows"]
    assert client.get(f"{B}/snapshot", params={"categories": ""}).json()["family_types"] == []

    bad = client.get(f"{B}/snapshot", params={"categories": "Walls"})
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_category"


def test_snapshot_without_a_revit_is_503(client):
    resp = client.get(f"{B}/snapshot")
    assert resp.status_code == 503
    assert resp.json()["error"] == "revit_unreachable" and "127.0.0.1:1" in resp.json()["endpoint"]


def test_query_returns_the_package_answer_as_is(revit, client):
    resp = client.post(f"{B}/query", json={"kind": "levels"})
    assert resp.status_code == 200
    assert resp.json()["kind"] == "levels"
    assert [lv["name"] for lv in resp.json()["items"]] == ["L1", "L2"]

    unknown = client.post(f"{B}/query", json={"kind": "rooms"})
    assert unknown.status_code == 400
    assert unknown.json()["error"] == "unknown_kind" and "levels" in unknown.json()["kinds"]

    bad_args = client.post(f"{B}/query", json={"kind": "levels", "args": {"limit": 5}})
    assert bad_args.status_code == 400 and bad_args.json()["error"] == "invalid_args"

    assert client.post(f"{B}/query", json={"args": {}}).status_code == 422  # kind missing: invalid_args
    assert client.post(f"{B}/query", json={"args": {}}).json()["error"] == "invalid_args"


def test_query_without_a_revit_is_503(client):
    resp = client.post(f"{B}/query", json={"kind": "levels"})
    assert resp.status_code == 503
    assert resp.json()["error"] == "revit_unreachable" and resp.json()["kind"] == "levels"


# -- packs ---------------------------------------------------------------------------

def test_tools_listing_has_the_mcp_list_tools_shape(client):
    tools = client.get(f"{B}/tools").json()
    assert len(tools) == 8
    wall = next(t for t in tools if t["name"] == "create_wall")
    assert set(wall) == {"name", "description", "version", "parameters", "preconditions", "validator", "used"}
    assert wall["version"] == "1.0.0" and wall["validator"] == "count_delta" and wall["used"] == 0
    assert wall["preconditions"][0] == {"kind": "levels_min", "value": 1}
    level = next(p for p in wall["parameters"] if p["name"] == "level_name")
    assert level == {"name": "level_name", "type": "string", "source": "tool:levels",
                     "required": True, "choices_from": "levels"}
    height = next(p for p in wall["parameters"] if p["name"] == "height")
    assert height["unit"] == "mm" and height["default"] == 3000 and height["required"] is False


def test_tool_detail_update_and_delete_are_unchanged(client, tmp_path):
    detail = client.get(f"{B}/tools/create_wall").json()
    assert detail["display_name"] == "Create Wall" and detail["validator"]["kind"] == "count_delta"
    assert client.get(f"{B}/tools/nope").json() == {"error": "unknown_tool", "message": "Tool 'nope' not found", "tool": "nope"}
    assert client.get(f"{B}/tools/nope").status_code == 404

    updated = client.put(f"{B}/tools/query_levels", json={"description": "levels, sorted"})
    assert updated.status_code == 200 and updated.json()["description"] == "levels, sorted"
    assert (tmp_path / "data" / "capabilities" / "query_levels.yaml").is_file()
    invalid = client.put(f"{B}/tools/query_levels", json={"code_template": "var l = \"{level_name}\"; return 1;"})
    assert invalid.status_code == 422 and invalid.json()["error"] == "invalid_pack"
    assert invalid.json()["problems"] == ["code_template: placeholder {level_name} is not a declared parameter"]
    blocked = client.put(f"{B}/tools/query_levels", json={"code_template": "System.IO.File.Delete(\"x\"); return 1;"})
    assert blocked.status_code == 422 and blocked.json()["error"] == "blocked"

    assert client.delete(f"{B}/tools/query_levels").json() == {"status": "deleted", "name": "query_levels"}
    assert client.delete(f"{B}/tools/query_levels").status_code == 404
    assert len(client.get(f"{B}/tools").json()) == 7


def test_tool_choices_come_from_revit(revit, client):
    choices = client.get(f"{B}/tools/create_wall/choices").json()
    assert choices == {"level_name": [{"label": "L1 (0.0mm)", "value": "L1"}, {"label": "L2 (3500.0mm)", "value": "L2"}]}
    assert client.get(f"{B}/tools/query_levels/choices").json() == {}      # no dynamic parameters
    assert client.get(f"{B}/tools/nope/choices").status_code == 404


def test_missing_params_asks_with_options_from_the_snapshot(client):
    snapshot = {
        "taken_at": "2026-09-21T00:00:00Z", "duration_ms": 1,
        "document": {"title": "P", "revit_version": "2026", "is_workshared": False},
        "units": {"length": "mm", "raw": ""}, "active_view": None,
        "levels": [{"id": 1, "name": "L1", "elevation_mm": 0.0}, {"id": 2, "name": "L2", "elevation_mm": 3500.0}],
        "grids": {"count": 0, "names": []}, "family_types": [], "selection": [], "selection_count": 0,
        "links": [], "phases": [], "warnings": [], "fingerprint": "abcdef0123456789",
    }
    resp = client.post(f"{B}/tools/create_wall/missing-params",
                       json={"known": {"start_x": 0, "start_y": 0}, "snapshot": snapshot, "language": "en"})
    assert resp.status_code == 200
    questions = resp.json()
    assert [q["param"] for q in questions] == ["level_name", "end_x", "end_y"]   # height has a default
    assert questions[0]["id"] == "q_level_name" and questions[0]["allow_other"] is True
    assert [o["value"] for o in questions[0]["options"]] == ["L1", "L2"]
    assert questions[0]["options"][0]["source"] == "tool:levels"
    assert questions[1]["text"].startswith("Please provide end_x")

    # No snapshot given and no Revit: the questions still go out, without options.
    resp = client.post(f"{B}/tools/create_wall/missing-params", json={"known": {}})
    assert resp.status_code == 200 and resp.json()[0]["options"] == []

    bad = client.post(f"{B}/tools/create_wall/missing-params", json={"known": {}, "snapshot": {"levels": 3}})
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_snapshot"
    assert client.post(f"{B}/tools/nope/missing-params", json={"known": {}}).status_code == 404


# -- specs ---------------------------------------------------------------------------

def test_reconcile_checks_the_draft_against_the_snapshot(revit, client):
    resp = client.post(f"{B}/spec/reconcile", json={"spec": spec_for("create_wall", **{**WALL, "level_name": "L9"})})
    assert resp.status_code == 200
    out = resp.json()
    assert out["ready"] is False
    assert [(c["param"], c["kind"], c["available"]) for c in out["conflicts"]] == [("level_name", "not_found", ["L1", "L2"])]

    ok = client.post(f"{B}/spec/reconcile", json={"spec": spec_for("create_wall", **WALL)}).json()
    assert ok["conflicts"] == [] and ok["questions"] == []

    bad = client.post(f"{B}/spec/reconcile", json={"spec": {"task": "x"}})
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_spec"


def test_reconcile_without_snapshot_or_revit_is_503(client):
    resp = client.post(f"{B}/spec/reconcile", json={"spec": spec_for("create_wall", **WALL)})
    assert resp.status_code == 503 and resp.json()["error"] == "revit_unreachable"


def test_confirm_issues_a_host_ui_token(client):
    resp = client.post(f"{B}/spec/confirm", json={"spec": spec_for("create_wall", **WALL)})
    assert resp.status_code == 200
    out = resp.json()
    assert set(out) == {"token", "spec_hash", "expires_at", "card"}
    assert out["card"].startswith("Task: run create_wall\nTool: create_wall (run_tool)\nParameters:")
    assert out["card"].endswith("Confirm? (yes / change something)")
    conf = bridge_module.get_gate().peek(out["token"])
    assert conf.channel == "host_ui" and conf.confirmed_by == "designer" and conf.spec_hash == out["spec_hash"]

    missing = client.post(f"{B}/spec/confirm", json={"spec": spec_for("create_wall", **{k: v for k, v in WALL.items() if k != "level_name"})})
    assert missing.status_code == 422
    assert missing.json()["error"] == "invalid_spec"
    assert [(e["code"], e["param"]) for e in missing.json()["errors"]] == [("missing_param", "level_name")]

    unparsable = client.post(f"{B}/spec/confirm", json={"spec": {"task": "x"}})
    assert unparsable.status_code == 422 and unparsable.json()["errors"][0]["code"] == "invalid_spec"


# -- execution -----------------------------------------------------------------------

def test_run_tool_needs_a_token_then_runs_validates_and_records(revit, client, tmp_path):
    no_token = client.post(f"{B}/tools/create_wall/run", json={"params": WALL})
    assert no_token.status_code == 400
    assert no_token.json()["error"] == "confirmation_required" and no_token.json()["success"] is False
    assert "confirm_spec" in no_token.json()["hint"]
    assert revit.requests == []                                        # nothing reached Revit

    token = confirm(client, spec_for("create_wall", **WALL))
    resp = client.post(f"{B}/tools/create_wall/run", json={"params": WALL, "token": token})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["success"] is True and out["error"] is None and out["tool"] == "create_wall"
    assert out["result"] == {"Status": "Created", "ElementId": 4242}
    assert out["validation"]["validator"] == "count_delta" and out["validation"]["passed"] is True
    assert out["evidence_id"].startswith("ev_") and out["preconditions_failed"] == [] and "hint" not in out
    assert "refusal" not in out

    record = bridge_module.get_ledger().get(out["evidence_id"])
    assert record["host"] == "web" and record["channel"] == "host_ui" and record["confirmed_by"] == "designer"
    assert record["action"] == "run_tool" and record["tool"] == "create_wall" and record["success"] is True
    assert record["token_prefix"] == token[:6]
    assert (tmp_path / "data" / "evidence").is_dir()
    assert next(t for t in client.get(f"{B}/tools").json() if t["name"] == "create_wall")["used"] == 1

    # A token is redeemed once.
    again = client.post(f"{B}/tools/create_wall/run", json={"params": WALL, "token": token}).json()
    assert again == {"success": False, "error": "confirmation_invalid", "reason": "used", "message": again["message"]}


def test_run_tool_refuses_a_tampered_projection_and_keeps_the_token(revit, client):
    token = confirm(client, spec_for("create_wall", **WALL))
    resp = client.post(f"{B}/tools/create_wall/run", json={"params": {**WALL, "height": 4000}, "token": token})
    assert resp.status_code == 200
    assert resp.json() == {"success": False, "error": "confirmation_invalid", "reason": "mismatch",
                           "message": "the execution does not match the confirmed spec"}
    other = client.post(f"{B}/tools/query_levels/run", json={"params": {}, "token": token}).json()
    assert other["reason"] == "mismatch"
    assert revit.requests == []
    assert bridge_module.get_gate().peek(token).used_at is None
    unknown = client.post(f"{B}/tools/create_wall/run", json={"params": WALL, "token": "nope"}).json()
    assert unknown["error"] == "confirmation_invalid" and unknown["reason"] == "unknown"


def test_run_tool_passes_validation_failed_through(env, tmp_path):
    with FakeRevit(model_handler(walls_before=1, walls_after=1)) as fake:   # nothing was created
        env.setenv("REVIT_BRIDGE_PORT", str(fake.port))
        with app_client(env) as client:
            token = confirm(client, spec_for("create_wall", **WALL))
            resp = client.post(f"{B}/tools/create_wall/run", json={"params": WALL, "token": token})
    assert resp.status_code == 200
    out = resp.json()
    assert out["success"] is False and out["error"] == "validation_failed"
    assert out["result"] == {"Status": "Created", "ElementId": 4242}
    assert out["validation"]["passed"] is False
    assert out["validation"]["checks"][0]["detail"] == "OST_Walls: before 1, after 1, delta 0, expected 1"
    record = bridge_module.get_ledger().get(out["evidence_id"])
    assert record["host"] == "web" and record["error"] == "validation_failed"


def test_run_tool_without_a_revit_is_503_and_keeps_the_token(client):
    token = confirm(client, spec_for("query_levels"))
    resp = client.post(f"{B}/tools/query_levels/run", json={"params": {}, "token": token})
    assert resp.status_code == 503 and resp.json()["error"] == "revit_unreachable"
    assert bridge_module.get_gate().peek(token).used_at is None
    assert client.get(f"{B}/evidence").json() == []


def test_execute_runs_confirmed_code_through_the_package(revit, client, monkeypatch):
    assert client.post(f"{B}/execute", json={"code": "return 1;"}).status_code == 400
    assert client.post(f"{B}/execute", json={"code": "return 1;"}).json()["error"] == "confirmation_required"

    token = confirm(client, code_spec("return 1;"))
    resp = client.post(f"{B}/execute", json={"code": "return 1;", "token": token})
    assert resp.status_code == 200
    out = resp.json()
    assert out["success"] is True and out["result"] == {"Status": "Created", "ElementId": 4242}
    assert out["tool"] is None and out["validation"] is None and out["evidence_id"].startswith("ev_")
    record = bridge_module.get_ledger().get(out["evidence_id"])
    assert record["host"] == "web" and record["action"] == "execute_code" and record["code_head"] == "return 1;"

    # The sandbox lives in the package: a blocked snippet never gets a token...
    evil = "System.Diagnostics.Process.Start(\"cmd\");"
    refused = client.post(f"{B}/spec/confirm", json={"spec": code_spec(evil)})
    assert refused.status_code == 422 and refused.json()["errors"][0]["code"] == "blocked_code"
    # ...and a review that fails at run time is a refusal (200, success false), not a 4xx.
    token = confirm(client, code_spec("return 2;"))
    monkeypatch.setattr(execution.sandbox, "review", lambda code: (False, ["Blocked pattern: test"]))
    blocked = client.post(f"{B}/execute", json={"code": "return 2;", "token": token})
    assert blocked.status_code == 200
    assert blocked.json() == {"success": False, "error": "blocked", "warnings": ["Blocked pattern: test"]}
    assert bridge_module.get_gate().peek(token).used_at is None


def test_solidify_takes_v1_parameters_and_a_validator(client, tmp_path):
    resp = client.post(f"{B}/solidify", json={
        "name": "count_walls",
        "code": "return new FilteredElementCollector(document).OfCategory(BuiltInCategory.OST_Walls).GetElementCount();",
        "description": "How many walls",
        "parameters": [],
        "validator": {"kind": "count_delta", "category": "OST_Walls", "expected": 0},
        "source_query": "how many walls",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "solidified", "name": "count_walls", "display_name": "Count Walls",
                           "version": "1.0.0", "revit_synced": False}
    assert (tmp_path / "data" / "capabilities" / "count_walls.yaml").is_file()
    listed = next(t for t in client.get(f"{B}/tools").json() if t["name"] == "count_walls")
    assert listed["validator"] == "count_delta"

    invalid = client.post(f"{B}/solidify", json={"name": "interpolated", "code": "var l = \"{level_name}\"; return 1;"})
    assert invalid.status_code == 422 and invalid.json()["error"] == "invalid_pack"
    assert invalid.json()["problems"] == ["code_template: placeholder {level_name} is not a declared parameter"]

    blocked = client.post(f"{B}/solidify", json={"name": "evil", "code": "System.IO.File.Delete(\"x\");"})
    assert blocked.status_code == 400 and blocked.json()["error"] == "blocked"


# -- evidence ------------------------------------------------------------------------

def test_evidence_lists_the_ledger_newest_first(revit, client):
    assert client.get(f"{B}/evidence").json() == []
    first = client.post(f"{B}/tools/query_levels/run",
                        json={"params": {}, "token": confirm(client, spec_for("query_levels"))}).json()
    second = client.post(f"{B}/execute",
                         json={"code": "return 1;", "token": confirm(client, code_spec("return 1;"))}).json()
    assert first["success"] and second["success"]

    records = client.get(f"{B}/evidence").json()
    assert [r["id"] for r in records] == [second["evidence_id"], first["evidence_id"]]
    assert all(r["host"] == "web" for r in records)
    assert [r["id"] for r in client.get(f"{B}/evidence", params={"tool": "query_levels"}).json()] == [first["evidence_id"]]
    assert len(client.get(f"{B}/evidence", params={"limit": 1}).json()) == 1
    assert client.get(f"{B}/evidence", params={"limit": "many"}).status_code == 422


def test_validate_reruns_the_recorded_assertion(revit, client):
    run = client.post(f"{B}/tools/create_wall/run",
                      json={"params": WALL, "token": confirm(client, spec_for("create_wall", **WALL))}).json()
    assert run["success"] is True
    resp = client.post(f"{B}/evidence/{run['evidence_id']}/validate")
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["evidence_id"] == run["evidence_id"] and report["tool"] == "create_wall"
    assert report["validator"] == "count_delta" and report["passed"] is True

    assert client.post(f"{B}/evidence/ev_nope/validate").status_code == 404
    assert client.post(f"{B}/evidence/ev_nope/validate").json()["error"] == "unknown_evidence"
    code_run = client.post(f"{B}/execute",
                           json={"code": "return 1;", "token": confirm(client, code_spec("return 1;"))}).json()
    no_validator = client.post(f"{B}/evidence/{code_run['evidence_id']}/validate")
    assert no_validator.status_code == 400 and no_validator.json()["error"] == "no_validator"


# -- selection, retired routes -------------------------------------------------------

def test_trigger_selection_returns_the_picked_element(env):
    def handler(request):
        if request.get("method") == "send_code_to_revit" and "PickObject" in request["params"]["code"]:
            return FakeRevit.code_result(request["id"], {"Id": 7, "Name": "Basic Wall", "Category": "Walls"})
        return FakeRevit.default_handler(request)

    with FakeRevit(handler) as fake:
        env.setenv("REVIT_BRIDGE_PORT", str(fake.port))
        with app_client(env) as client:
            resp = client.post(f"{B}/trigger-selection")
    assert resp.status_code == 200
    assert resp.json() == {"elements": [{"Id": 7, "Name": "Basic Wall", "Category": "Walls"}]}


def test_retired_routes_are_gone(client):
    paths = set(client.get("/openapi.json").json()["paths"])
    assert not {f"{B}/unit", f"{B}/project-units", f"{B}/query-revit"} & paths
    # An unknown GET is a 404, or the SPA's index.html when a frontend build is present.
    gone = client.get(f"{B}/project-units")
    assert gone.status_code == 404 or "html" in gone.headers["content-type"]
    assert client.post(f"{B}/unit", json={"unit": "mm"}).status_code in (404, 405)
    assert client.post(f"{B}/query-revit", json={"command": "get_levels"}).status_code in (404, 405)
