import json

from revit_bridge.capabilities import ToolStore
from tests.conftest import FIXTURE_ADMIN_PASSWORD
from tests.fake_model import json_turn
from tests.test_api_v1 import B
from tests.test_host_loop import HEADERS, model  # noqa: F401
from tests.test_workbench import addin, workbench_handler, NOT_COMPILED  # noqa: F401

ADMIN = {**HEADERS, "X-Admin-Token": FIXTURE_ADMIN_PASSWORD}
PACK = {"schema_version": 1, "version": "1.0.0", "name": "sample_tool", "description": "Return a sample",
        "code_template": "return 1;", "parameters": []}


def test_only_admin_can_author_on_cloud(client, model):
    assert not client.get("/config.json").json()["features"]["authoring"]
    assert client.get("/config.json", headers=ADMIN).json()["features"]["authoring"]
    assert client.post(f"{B}/tools/draft", json={"description": "sample"}, headers=HEADERS).status_code == 403
    assert not model.requests


def test_draft_check_edit_trial_and_save(client, model, addin, monkeypatch):
    store = ToolStore()
    model.turns.append(json_turn({**PACK, "code_template": ""}))
    result = client.post(f"{B}/tools/draft", headers=ADMIN, json={
        "description": "Return a sample", "based_on": "query_levels", "name": "new_sample"})
    assert result.status_code == 200, result.text
    draft = result.json()["pack"]
    assert result.json()["problems"] and draft["name"] == "new_sample" and store.load("new_sample") is None
    context = json.loads(model.requests[0]["messages"][-1]["content"])
    assert context.keys() == {"description", "reference", "revit_version"} and context["revit_version"] == "2026"
    assert context["reference"]["name"] == "query_levels" and "source_query" not in context["reference"]
    assert all(req["method"] == "get_bridge_capabilities" for req in addin.requests)
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
