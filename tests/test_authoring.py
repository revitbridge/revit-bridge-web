import json

import pytest
from revit_bridge.capabilities import ToolStore
from backend.log_store import get_log_store
from tests.conftest import FIXTURE_ADMIN_PASSWORD
from tests.fake_model import json_turn, text_turn
from tests.test_api_v1 import B
from tests.test_host_loop import HEADERS, model  # noqa: F401
from tests.test_workbench import addin, workbench_handler, CAPABILITIES, NOT_COMPILED  # noqa: F401

ADMIN = {**HEADERS, "X-Admin-Token": FIXTURE_ADMIN_PASSWORD}
PACK = {"schema_version": 1, "version": "1.0.0", "name": "sample_tool", "description": "Return a sample",
        "code_template": "return 1;", "parameters": []}


def test_only_admin_can_author_on_cloud(client, model):
    assert not client.get("/config.json").json()["features"]["authoring"]
    assert client.get("/config.json", headers=ADMIN).json()["features"]["authoring"]
    assert client.post(f"{B}/tools/draft", json={"description": "sample"}, headers=HEADERS).status_code == 403
    assert not model.requests


def test_invalid_model_settings_keep_the_server_reason(client, model):
    headers = {k: v for k, v in ADMIN.items() if k != "X-LLM-Key"}
    reply = client.post(f"{B}/tools/draft", headers=headers, json={"description": "Create a view"})
    assert reply.status_code == 400 and "X-LLM-Key" in reply.json()["message"]
    assert not model.requests


def test_draft_check_edit_trial_and_save(client, model, addin, monkeypatch):
    store = ToolStore()
    model.turns.extend([json_turn({**PACK, "code_template": ""}), json_turn(PACK)])
    result = client.post(f"{B}/tools/draft", headers=ADMIN, json={
        "description": "Return a sample", "based_on": "query_levels", "name": "new_sample"})
    assert result.status_code == 200, result.text
    draft = result.json()["pack"]
    assert result.json()["problems"] == [] and result.json()["repair_rounds"] == 1
    assert result.json()["compiled"] and draft["name"] == "new_sample" and store.load("new_sample") is None
    context = json.loads(model.requests[0]["messages"][-1]["content"])
    assert context.keys() == {"description", "reference", "revit_version"} and context["revit_version"] == "2026"
    assert context["reference"]["name"] == "query_levels" and "source_query" not in context["reference"]
    repair = json.loads(model.requests[1]["messages"][-1]["content"])
    assert repair == {"previous_draft": {**draft, "code_template": ""},
                      "problems": ["code_template: required, non-empty string"]}
    assert {req["method"] for req in addin.requests} == {"get_bridge_capabilities", "check_code_in_revit"}
    initial = list(store.user_dir.glob("*.yaml"))
    def no_scratch(*args, **kwargs):
        raise AssertionError("Draft must stay in memory")
    import tempfile
    monkeypatch.setattr(tempfile, "TemporaryDirectory", no_scratch)
    draft["code_template"] = "return broken;"
    addin.handler = workbench_handler(check=NOT_COMPILED)
    check = client.post(f"{B}/check", json={"pack": draft}, headers=ADMIN).json()
    assert check["compiled"] is False and check["diagnostics"]
    draft["code_template"] = "return 1;"
    addin.handler = workbench_handler()
    assert client.post(f"{B}/check", json={"pack": draft}, headers=ADMIN).json()["compiled"]
    trial = client.post(f"{B}/dry-run", json={"pack": draft, "values": {}}, headers=ADMIN).json()
    assert trial["success"] and trial["rolled_back"]
    evidence = client.get(f"{B}/evidence").text
    assert trial["evidence_id"] in evidence and 'return 1;' not in evidence
    assert list(store.user_dir.glob("*.yaml")) == initial
    payload = {"name": draft["name"], "code": draft["code_template"], "description": draft["description"],
               "parameters": draft["parameters"]}
    assert client.post(f"{B}/solidify", json={**payload, "name": "query_levels"}, headers=ADMIN).status_code == 409
    assert client.post(f"{B}/solidify", json=payload, headers=ADMIN).status_code == 200
    assert store.load("new_sample").code_template == "return 1;"


def test_repairs_compile_errors_on_rendered_sample_and_records_calls(client, model, addin):
    diagnostic = NOT_COMPILED["diagnostics"][0]
    # Never copy arbitrary add-in fields, environment or RPC replies into a repair.
    base = workbench_handler(capabilities={**CAPABILITIES, "document": "PRIVATE_DOCUMENT"})
    broken = workbench_handler(check={**NOT_COMPILED, "errorMessage": "PRIVATE_ADDIN_CONTEXT",
                                      "diagnostics": [{**diagnostic, "extra": "PRIVATE_DIAG"}]})
    addin.handler = lambda req: broken(req) if req.get("params", {}).get("code", "").startswith("doc.") else base(req)
    p = {"name": "view_name", "type": "string", "required": True, "source": "designer"}
    bad = {**PACK, "parameters": [p], "code_template": 'doc.Title; return "{view_name}";'}
    good = {**bad, "code_template": 'return new { Title = document.Title, Name = "{view_name}" };'}
    usage = {"prompt_tokens": 12, "completion_tokens": 3}
    model.turns.extend([text_turn("{", usage=usage), json_turn(bad, usage=usage), json_turn(good, usage=usage)])
    reply = client.post(f"{B}/tools/draft", headers=ADMIN, json={"description": "创建三维视图"}).json()
    assert reply["reference"] == "create_level" and reply["repair_rounds"] == 2
    assert reply["compiled"] and reply["pack"] == good and not reply["problems"]
    assert len(model.requests) == 3
    first, malformed, compiler = [json.loads(r["messages"][1]["content"]) for r in model.requests]
    assert set(first) == {"description", "reference", "revit_version"}
    assert first["reference"]["name"] == "create_level" and first["revit_version"] == "2026"
    assert malformed.keys() == compiler.keys() == {"previous_draft", "problems"}
    assert malformed["previous_draft"] == "{" and "Invalid JSON" in malformed["problems"][0]
    assert compiler["previous_draft"] == bad
    assert compiler["problems"][0]["message"] == diagnostic["message"] and compiler["problems"][0]["line"] == 1
    assert {r["model"] for r in model.requests} == {"fake-1"}
    assert all(len(r["messages"]) == 2 and "PRIVATE_" not in json.dumps(r) for r in model.requests)
    assert {r["method"] for r in addin.requests} == {"get_bridge_capabilities", "check_code_in_revit"}
    assert [r["params"]["code"] for r in addin.requests if r["method"] == "check_code_in_revit"] == [
        pack["code_template"].replace("{view_name}", "Sample") for pack in (bad, good)]
    run, = get_log_store().runs()
    rows = get_log_store().calls_of_session(run["run_label"])
    assert [(r["round"], r["mode"], r["input"], r["output"]) for r in rows] == [
        (n, "authoring", 12, 3) for n in (1, 2, 3)]


@pytest.mark.parametrize("valid_json", [True, False])
def test_returns_remaining_problems_after_two_repairs(client, model, addin, valid_json):
    model.turns.extend([json_turn({**PACK, "parameters": "bad"}) if valid_json else text_turn("[]") for _ in range(3)])
    reply = client.post(f"{B}/tools/draft", headers=ADMIN, json={"description": "List levels"})
    assert reply.status_code == (200 if valid_json else 502)
    assert len(model.requests) == 3 and reply.json()["repair_rounds"] == 2
    assert reply.json()["problems"]
    assert json.loads(model.requests[0]["messages"][1]["content"])["reference"]["name"] == "query_levels"
    assert all(r["method"] == "get_bridge_capabilities" for r in addin.requests)


def test_final_compile_error_locates_template_after_substitution(client, model, addin):
    code = 'var name = "{view_name}"; return doc.Title;'
    param = {"name": "view_name", "type": "string", "source": "default", "required": False, "default": "3D"}
    pack = {**PACK, "parameters": [param], "code_template": code}
    column = code.replace("{view_name}", "3D").index("doc") + 1
    addin.handler = workbench_handler(check={**NOT_COMPILED, "diagnostics": [
        {**NOT_COMPILED["diagnostics"][0], "id": "CS0103", "column": column, "message": "Unknown name 'doc'"}]})
    model.turns.extend([json_turn(pack) for _ in range(3)])
    reply = client.post(f"{B}/tools/draft", headers=ADMIN, json={"description": "Create view"}).json()
    assert reply["compiled"] is False and reply["repair_rounds"] == 2
    for data in (reply, client.post(f"{B}/check", headers=ADMIN, json={"pack": pack}).json()):
        diag, = data["diagnostics"]
        assert diag["column"] == column and diag["template_column"] == code.index("doc") + 1


def test_keeps_last_editable_pack_when_repairs_are_unusable(client, model, addin):
    editable = {**PACK, "code_template": ""}
    model.turns.extend([json_turn(editable), text_turn('{"parameters":NaN}'), text_turn("bad JSON")])
    reply = client.post(f"{B}/tools/draft", headers=ADMIN, json={"description": "Read levels"}).json()
    assert reply["repair_rounds"] == 2 and reply["pack"] == editable
    assert any(p.startswith("code_template:") for p in reply["problems"])
    assert any(p.startswith("Invalid JSON:") for p in reply["problems"])


def test_disconnected_draft_still_validates_and_renders_booleans(client, model, monkeypatch):
    from backend.api import bridge
    from backend.api.errors import ApiError

    async def offline():
        raise ApiError(503, "revit_unreachable", "PRIVATE_DEVICE")

    monkeypatch.setattr(bridge, "get_revit_client", offline)
    pack = {**PACK, "code_template": 'return new { Flag = bool.Parse("{flag}") };',
            "parameters": [{"name": "flag", "type": "bool", "required": True, "source": "designer"}]}
    model.turns.append(json_turn(pack))
    reply = client.post(f"{B}/tools/draft", headers=ADMIN, json={"description": "Read flag"}).json()
    assert reply["compiled"] is None and reply["repair_rounds"] == 0 and not reply["problems"]
    assert "PRIVATE_DEVICE" not in json.dumps(model.requests)
    assert bridge.render_draft(reply["pack"], {"flag": True}) == 'return new { Flag = bool.Parse("True") };'
