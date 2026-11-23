"""The workbench's backend: compile and dry run (no token), export and import of pack
files, built-in packs read-only, tool names checked before the file system, and the
user tool directory shared with the package's MCP server (read from disk on every call)."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import pytest
import yaml

from revit_bridge import checking
from revit_bridge import dry_run as package_dry_run
from revit_bridge.capabilities import ToolStore
from revit_bridge.paths import builtin_capabilities_dir, user_capabilities_dir
from revit_bridge.revit.client import RevitClient

from backend.api import bridge as bridge_module
from backend.api.model_tools import call_tool
from backend.session import Session
from tests.fake_revit import FakeRevit, model_handler
from tests.test_api_v1 import B, WALL, confirm, spec_for
from tests.test_deploy_mode import LOCAL

ADMIN = {"X-Admin-Token": "hunter2"}
CODE = "return new FilteredElementCollector(document).OfCategory(BuiltInCategory.OST_Walls).GetElementCount();"
BLOCKED_CODE = "System.IO.File.Delete(\"x\"); return 1;"

# What an add-in that compiles and dry-runs reports (get_bridge_capabilities).
CAPABILITIES = {
    "protocol": "1", "addin_version": "0.6.0",
    "methods": ["get_bridge_capabilities", "send_code_to_revit", "check_code_in_revit", "dry_run_code_in_revit"],
    "features": {"check_code": True, "dry_run": True},
    "revit": {"version_number": "2026", "version_name": "Autodesk Revit 2026"},
    "runtime": {"clr": "8.0"},
}
COMPILED = {"compiled": True, "diagnostics": [], "truncated": 0, "fingerprint": "fp-1"}
NOT_COMPILED = {"compiled": False, "truncated": 0, "fingerprint": "fp-2", "diagnostics": [{
    "id": "CS1061", "severity": "error", "line": 1, "column": 12, "in_wrapper": False, "symbol": "Lenght",
    "message": "'Wall' does not contain a definition for 'Lenght' and no accessible extension method 'Lenght' "
               "accepting a first argument of type 'Wall' could be found"}]}
DRY_RUN = {"success": True, "rolled_back": True, "result": json.dumps({"Changed": 3}), "error": None,
           "diagnostics": [], "failures": [], "fingerprint": "fp-3",
           "changes": {"added": {"count": 0}, "modified": {"count": 3, "ids": [11, 12, 13]}, "deleted": {"count": 0}}}


def workbench_handler(check=COMPILED, dry_run=DRY_RUN, capabilities=CAPABILITIES):
    """The small model of tests/fake_revit.py plus the capability reply and the two methods."""
    base = model_handler()

    def handler(request: dict) -> dict:
        method, rid = request.get("method"), request.get("id")
        if method == "get_bridge_capabilities":
            if capabilities is None:
                return FakeRevit.error(rid, -32601, "Method 'get_bridge_capabilities' not found")
            return FakeRevit.ok(rid, capabilities)
        if method == "check_code_in_revit":
            return FakeRevit.ok(rid, check)
        if method == "dry_run_code_in_revit":
            return FakeRevit.ok(rid, dry_run)
        return base(request)
    return handler


def counting_handler():
    """``workbench_handler`` whose wall count grows by one with every run of a pack's code, so
    a count_delta validator passes on each of several runs."""
    base = workbench_handler()
    walls = {"count": 1}

    def handler(request: dict) -> dict:
        if request.get("method") == "send_code_to_revit":
            code = request["params"]["code"]
            if "GetElementCount" in code and "Enum.Parse(typeof(BuiltInCategory)" in code:
                return FakeRevit.code_result(request.get("id"), walls["count"])
            reply = base(request)
            if json.loads(reply["result"]["result"] or "null") == {"Status": "Created", "ElementId": 4242}:
                walls["count"] += 1
            return reply
        return base(request)
    return handler


@pytest.fixture
def addin(env):
    """A fake add-in that compiles and dry-runs, reachable over TCP."""
    with FakeRevit(counting_handler()) as fake:
        env.setenv("REVIT_BRIDGE_PORT", str(fake.port))
        yield fake


def sent(fake: FakeRevit, method: str) -> list[dict]:
    return [r["params"] for r in fake.requests if r.get("method") == method]


def package_reply(coroutine_factory, port: int):
    """What the package function answers on its own client to the same add-in."""
    async def run():
        client = RevitClient(port=port)
        try:
            return await coroutine_factory(client)
        finally:
            await client.disconnect()
    return asyncio.run(run())


# -- POST /check -----------------------------------------------------------------------

def test_check_is_the_package_s_check_code(addin, client):
    resp = client.post(f"{B}/check", json={"code": CODE, "parameters": [3000]})
    assert resp.status_code == 200, resp.text
    reply = resp.json()
    assert reply["checked"] is True and reply["compiled"] is True and reply["fingerprint"] == "fp-1"
    assert sent(addin, "check_code_in_revit") == [{"code": CODE, "parameters": [3000]}]
    # The reply is the package's, as the MCP tool gives it (same library of lessons).
    expected = package_reply(lambda c: checking.check_code(c, CODE, [3000], library=bridge_module.guidance_library),
                             addin.port)
    assert reply == json.loads(json.dumps(expected))
    assert set(reply) == set(checking.reply())


def test_a_failed_compile_comes_back_with_hints_and_lessons(env, client):
    code = "Wall wall = null; var l = (wall.Location as LocationCurve).Curve.Lenght; return l;"
    with FakeRevit(workbench_handler(check=NOT_COMPILED)) as fake:
        env.setenv("REVIT_BRIDGE_PORT", str(fake.port))
        reply = client.post(f"{B}/check", json={"code": code}).json()
        expected = package_reply(lambda c: checking.check_code(c, code, None, library=bridge_module.guidance_library),
                                 fake.port)
    assert reply == json.loads(json.dumps(expected))
    assert reply["checked"] is True and reply["compiled"] is False
    assert [d["id"] for d in reply["diagnostics"]] == ["CS1061"]
    assert reply["hints"] and reply["hints"][0]["id"] == "CS1061"
    # The lessons for the error, chosen by the package as for the MCP tool.
    assert "revit-geometry/wall-length-from-location" in [item["id"] for item in reply["guidance"]]


def test_check_refuses_blocked_code_without_revit_and_reports_no_revit_as_503(client):
    blocked = client.post(f"{B}/check", json={"code": BLOCKED_CODE})
    assert blocked.status_code == 200
    assert blocked.json() == checking.blocked_reply(BLOCKED_CODE)       # nothing was needed from Revit
    assert blocked.json()["blocked"] is True and blocked.json()["checked"] is True
    unreachable = client.post(f"{B}/check", json={"code": CODE})
    assert unreachable.status_code == 503 and unreachable.json()["error"] == "revit_unreachable"
    assert client.post(f"{B}/check", json={"parameters": []}).json()["error"] == "invalid_args"


def test_an_add_in_that_cannot_compile_is_the_package_s_not_checked(env, client):
    with FakeRevit(workbench_handler(capabilities=None)) as fake:
        env.setenv("REVIT_BRIDGE_PORT", str(fake.port))
        reply = client.post(f"{B}/check", json={"code": CODE}).json()
    assert reply["checked"] is False and reply["capability"] == "unsupported"
    assert reply["reason"].startswith("method_not_found")


# -- POST /dry-run ---------------------------------------------------------------------

def test_dry_run_is_the_package_s_dry_run_code_with_no_token(addin, client, tmp_path):
    resp = client.post(f"{B}/dry-run", json={"code": CODE, "parameters": [500]})
    assert resp.status_code == 200, resp.text
    reply = resp.json()
    assert reply["success"] is True and reply["rolled_back"] is True
    assert reply["result"] == {"Changed": 3} and reply["changes"]["modified"]["count"] == 3
    assert reply["evidence_id"].startswith("ev_")
    # No confirmation, no token: the request to the add-in has only the code and parameters.
    assert sent(addin, "dry_run_code_in_revit") == [{"code": CODE, "parameters": [500]}]
    assert not sent(addin, "send_code_to_revit") or all("confirm" not in p for p in sent(addin, "send_code_to_revit"))
    # One ledger line, a dry run of this host and scope.
    records = client.get(f"{B}/evidence").json()
    assert [(r["action"], r["host"], r["scope"]) for r in records] == [("dry_run", "web", "local")]
    assert records[0]["result_summary"]["changes"] == {"added": 0, "modified": 3, "deleted": 0}
    keys = set(package_dry_run.reply())
    assert keys <= set(reply)


def test_the_dry_run_takes_no_files_from_the_browser(addin, client, tmp_path):
    """The package's include reads .cs files on the machine running it: not offered here.
    A body with any field the route does not take is refused before anything else."""
    secret = tmp_path / "Secret.cs"
    secret.write_text("public static class Secret { public const string Key = \"x\"; }", encoding="utf-8")
    for route in ("dry-run", "check"):
        resp = client.post(f"{B}/{route}", json={"code": CODE, "include": [str(secret)]})
        assert resp.status_code == 422 and resp.json()["error"] == "invalid_args", resp.text
        assert "include" in resp.json()["message"]
    assert sent(addin, "dry_run_code_in_revit") == [] and sent(addin, "check_code_in_revit") == []


def test_a_dry_run_that_was_not_rolled_back_is_the_package_s_defect_reply(env, client):
    with FakeRevit(workbench_handler(dry_run={**DRY_RUN, "rolled_back": False})) as fake:
        env.setenv("REVIT_BRIDGE_PORT", str(fake.port))
        reply = client.post(f"{B}/dry-run", json={"code": CODE}).json()
    assert reply["success"] is False and reply["error"] == "dry_run_not_rolled_back"
    assert reply["hint"] == package_dry_run.NOT_ROLLED_BACK_HINT


def test_dry_run_refuses_blocked_code_without_revit_and_reports_no_revit_as_503(client):
    blocked = client.post(f"{B}/dry-run", json={"code": BLOCKED_CODE})
    assert blocked.status_code == 200 and blocked.json() == package_dry_run.blocked_reply(BLOCKED_CODE)
    unreachable = client.post(f"{B}/dry-run", json={"code": CODE})
    assert unreachable.status_code == 503 and unreachable.json()["error"] == "revit_unreachable"
    assert client.get(f"{B}/evidence").json() == []                    # nothing reached an add-in


# -- a draft pack: the package fills the template ------------------------------------------

def draft_of(name: str) -> dict:
    """A built-in pack's file as a draft (what the workbench sends as ``pack``)."""
    return yaml.safe_load((builtin_capabilities_dir() / f"{name}.yaml").read_text(encoding="utf-8"))


HEIGHT = {"element_id": 4242, "height": 3500}


def drafts_left() -> list[str]:
    import tempfile
    return sorted(p.name for p in Path(tempfile.gettempdir()).glob("rbw-draft-*"))


def test_a_draft_is_filled_by_the_package_then_checked_and_dry_run(addin, client, tmp_path):
    before = drafts_left()
    expected, problems = ToolStore().render("modify_wall_height", HEIGHT)   # what the saved pack would send
    assert problems == [] and "{height}" not in expected
    pack = {**draft_of("modify_wall_height"), "name": "raise_wall"}
    checked = client.post(f"{B}/check", json={"pack": pack, "values": HEIGHT})
    assert checked.status_code == 200 and checked.json()["compiled"] is True, checked.text
    dry = client.post(f"{B}/dry-run", json={"pack": pack, "values": HEIGHT})
    assert dry.status_code == 200 and dry.json()["success"] is True, dry.text
    assert sent(addin, "check_code_in_revit") == [{"code": expected, "parameters": []}]
    assert sent(addin, "dry_run_code_in_revit") == [{"code": expected, "parameters": []}]
    # Nothing written where packs live; the temporary store is gone.
    assert not (tmp_path / "data" / "capabilities").exists() or not list((tmp_path / "data" / "capabilities").iterdir())
    assert drafts_left() == before


def test_a_draft_the_package_cannot_fill_is_422_with_its_messages(addin, client):
    pack = draft_of("modify_wall_height")
    missing = client.post(f"{B}/check", json={"pack": pack, "values": {"element_id": 1}})
    assert missing.status_code == 422 and missing.json()["error"] == "render_failed"
    assert missing.json()["errors"] == ["Missing required parameter: height"]
    wrong = client.post(f"{B}/dry-run", json={"pack": pack, "values": {"element_id": 1, "height": "tall"}})
    assert wrong.json()["errors"] == ["Parameter 'height' expects double, got 'tall'"]
    leftover = {**pack, "code_template": pack["code_template"] + "\n// {undeclared}"}
    invalid = client.post(f"{B}/check", json={"pack": leftover, "values": HEIGHT})
    assert invalid.status_code == 422 and invalid.json()["error"] == "invalid_pack"
    assert invalid.json()["problems"] == ["code_template: placeholder {undeclared} is not a declared parameter"]
    named = client.post(f"{B}/check", json={"pack": {**pack, "name": "con"}, "values": HEIGHT})
    assert named.status_code == 422 and named.json()["error"] == "invalid_tool_name"
    assert sent(addin, "check_code_in_revit") == [] and sent(addin, "dry_run_code_in_revit") == []


def test_code_and_pack_go_alone(client):
    pack = draft_of("query_levels")
    for body in ({"code": CODE, "pack": pack}, {"pack": pack, "parameters": [1]}, {"code": CODE, "values": {}},
                 {}, {"values": {"a": 1}}):
        for route in ("check", "dry-run"):
            resp = client.post(f"{B}/{route}", json=body)
            assert resp.status_code == 422 and resp.json()["error"] == "invalid_args", (route, body, resp.text)


def test_a_draft_s_code_still_meets_the_sandbox(client):
    pack = {**draft_of("query_levels"), "code_template": BLOCKED_CODE}
    assert client.post(f"{B}/dry-run", json={"pack": pack, "values": {}}).json()["error"] == "blocked"
    assert client.post(f"{B}/check", json={"pack": pack, "values": {}}).json()["blocked"] is True


def test_a_cloud_host_rate_limits_check_and_dry_run_with_the_confirm_limiter(make_client, env):
    env.setenv("CHAT_RATE_LIMIT", "3")
    client = make_client()
    body = {"code": BLOCKED_CODE}                    # answered without Revit
    assert client.post(f"{B}/check", json=body).status_code == 200
    assert client.post(f"{B}/dry-run", json=body).status_code == 200
    assert client.post(f"{B}/spec/confirm", json={"spec": {"task": "x"}}).status_code == 422   # counted too
    limited = client.post(f"{B}/check", json=body)
    assert limited.status_code == 429 and limited.json()["error"] == "rate_limited"
    assert client.post(f"{B}/spec/confirm", json={"spec": {"task": "x"}}).status_code == 429


def test_a_local_host_does_not_rate_limit_them(make_client, local_env):
    local_env.setenv("CHAT_RATE_LIMIT", "1")
    client = make_client(**LOCAL)
    for _ in range(4):
        assert client.post(f"{B}/check", json={"code": BLOCKED_CODE}).status_code == 200
        assert client.post(f"{B}/dry-run", json={"code": BLOCKED_CODE}).status_code == 200


def test_choices_come_from_the_package_s_get_tool_choices(revit, client):
    levels = client.post(f"{B}/choices", json={"choices_from": "levels"})
    assert levels.status_code == 200
    assert levels.json() == client.get(f"{B}/tools/create_wall/choices").json()["level_name"]
    assert levels.json() == [{"label": "L1 (0.0mm)", "value": "L1"}, {"label": "L2 (3500.0mm)", "value": "L2"}]
    types = client.post(f"{B}/choices", json={"choices_from": "family_types:OST_Walls"}).json()
    assert types == [{"label": "OST_Walls-TypeA", "value": "OST_Walls-TypeA"}]
    for bad in ("rooms", "family_types:Walls", "elements:OST_Walls;x", "levels\n", ""):
        resp = client.post(f"{B}/choices", json={"choices_from": bad})
        assert resp.status_code == 422 and resp.json()["error"] == "invalid_args", bad
    assert client.post(f"{B}/choices", json={"choices_from": "levels", "x": 1}).status_code == 422


def test_choices_without_revit_is_503(client):
    resp = client.post(f"{B}/choices", json={"choices_from": "levels"})
    assert resp.status_code == 503 and resp.json()["error"] == "revit_unreachable"


# -- the lessons check_code adds: the layers the MCP server loads ---------------------------

LESSON_CODE = "var id = new ElementId(1); return id.IntegerValue;"


@pytest.fixture
def fresh_guidance():
    bridge_module._load_guidance.cache_clear()
    yield
    bridge_module._load_guidance.cache_clear()


def test_the_company_project_and_personal_layers_come_from_the_environment(env, client, tmp_path, fresh_guidance):
    """Personal = <REVIT_BRIDGE_DATA_DIR>/guidance; a bad project layer is left out."""
    import shutil

    from revit_bridge.paths import guidance_dir

    personal = tmp_path / "data" / "guidance" / "revit-core" / "lessons"
    personal.mkdir(parents=True)
    shutil.copyfile(guidance_dir() / "revit-core" / "lessons" / "elementid-value.md", personal / "elementid-value.md")
    project = tmp_path / "project-guidance"
    project.mkdir()
    (project / "guidance.yaml").write_text("schema_version: 1\nalways: [no-such-category]\n", encoding="utf-8")
    env.setenv("REVIT_BRIDGE_GUIDANCE_PROJECT", str(project))
    with FakeRevit(workbench_handler()) as fake:
        env.setenv("REVIT_BRIDGE_PORT", str(fake.port))
        reply = client.post(f"{B}/check", json={"code": LESSON_CODE}).json()
    assert reply["compiled"] is True and not [w for w in reply["warnings"] if w.startswith("guidance_unavailable")]
    layers = {item["id"]: item["layer"] for item in reply["guidance"]}
    assert layers.get("revit-core/elementid-value") == "personal", reply["guidance"]
    library = bridge_module.guidance_library()
    assert any(w.startswith("layer_invalid: project") for w in library.warnings), library.warnings


def test_an_invalid_framework_layer_is_a_result_not_a_crash(env, client, monkeypatch, tmp_path, fresh_guidance):
    (tmp_path / "empty").mkdir()
    monkeypatch.setattr(bridge_module, "guidance_dir", lambda: tmp_path / "empty")
    with FakeRevit(workbench_handler()) as fake:
        env.setenv("REVIT_BRIDGE_PORT", str(fake.port))
        resp = client.post(f"{B}/check", json={"code": LESSON_CODE})
    assert resp.status_code == 200
    reply = resp.json()
    assert reply["compiled"] is True and reply["guidance"] == []
    assert any(w.startswith("guidance_unavailable") for w in reply["warnings"]), reply["warnings"]


# -- export and import -------------------------------------------------------------------

def yaml_request(client, text: str, *, overwrite: bool | None = None, headers=None):
    path = f"{B}/tools/import" + ("" if overwrite is None else f"?overwrite={str(overwrite).lower()}")
    return client.post(path, content=text.encode("utf-8"), headers={"Content-Type": "text/yaml", **(headers or {})})


def renamed(text: str, old: str, new: str) -> str:
    assert f"name: {old}\n" in text
    return text.replace(f"name: {old}\n", f"name: {new}\n", 1)


def test_export_gives_the_file_as_stored(client):
    resp = client.get(f"{B}/tools/create_wall/export")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/yaml")
    assert resp.text == (builtin_capabilities_dir() / "create_wall.yaml").read_text(encoding="utf-8")
    assert client.get(f"{B}/tools/nope/export").status_code == 404
    assert client.get(f"{B}/tools/con/export").json()["error"] == "invalid_tool_name"


def test_a_built_in_pack_copied_under_another_name_round_trips(addin, make_client, env, tmp_path):
    env.setenv("ADMIN_PASSWORD", "hunter2")
    client = make_client()
    exported = client.get(f"{B}/tools/create_wall/export").text
    # Imported as it is, it would overwrite the built-in one: refused, overwrite or not.
    for overwrite in (None, True):
        refused = yaml_request(client, exported, overwrite=overwrite, headers=ADMIN)
        assert refused.status_code == 409 and refused.json()["error"] == "builtin_read_only"
    mine = renamed(exported, "create_wall", "my_wall")
    imported = yaml_request(client, mine, headers=ADMIN)
    assert imported.status_code == 200, imported.text
    assert imported.json() == {"status": "imported", "name": "my_wall", "version": "1.0.0", "replaced": False,
                               "revit_synced": False}
    stored = tmp_path / "data" / "capabilities" / "my_wall.yaml"
    assert stored.read_text(encoding="utf-8") == mine                   # the file as sent
    assert client.get(f"{B}/tools/my_wall/export").text == mine
    listed = {t["name"]: t for t in client.get(f"{B}/tools").json()}
    assert (listed["my_wall"]["builtin"], listed["my_wall"]["origin"]) == (False, "user")
    builtin, copy = client.get(f"{B}/tools/create_wall").json(), client.get(f"{B}/tools/my_wall").json()
    assert {**builtin, "name": "my_wall"} == copy

    # The copy behaves as the original: the same code reaches Revit, the same validator passes.
    runs = {}
    for name in ("create_wall", "my_wall"):
        token = confirm(client, spec_for(name, **WALL))
        runs[name] = client.post(f"{B}/tools/{name}/run", json={"params": WALL, "token": token}).json()
    for run in runs.values():
        assert run["success"] is True and run["validation"]["passed"] is True, run
    assert runs["my_wall"]["validation"]["validator"] == runs["create_wall"]["validation"]["validator"]
    assert [c["passed"] for c in runs["my_wall"]["validation"]["checks"]] == \
        [c["passed"] for c in runs["create_wall"]["validation"]["checks"]]
    pack_runs = [p["code"] for p in sent(addin, "send_code_to_revit") if "Wall.Create" in p["code"]]
    assert len(pack_runs) == 2 and pack_runs[0] == pack_runs[1]

    # Exported, deleted, imported again: the same pack.
    again = client.get(f"{B}/tools/my_wall/export").text
    assert client.delete(f"{B}/tools/my_wall", headers=ADMIN).json()["status"] == "deleted"
    assert yaml_request(client, again, headers=ADMIN).status_code == 200
    assert client.get(f"{B}/tools/my_wall").json() == copy


def test_import_does_not_replace_a_user_pack_unless_asked(client_local):
    client, user_dir = client_local
    first = "schema_version: 1\nname: count_walls\nversion: 1.0.0\ndescription: one\ncode_template: return 1;\n"
    second = first.replace("description: one", "description: two")
    assert yaml_request(client, first).status_code == 200
    taken = yaml_request(client, second)
    assert taken.status_code == 409 and taken.json()["error"] == "pack_exists"
    assert "overwrite=true" in taken.json()["message"]
    assert yaml_request(client, second, overwrite=False).status_code == 409
    assert (user_dir / "count_walls.yaml").read_text(encoding="utf-8") == first
    replaced = yaml_request(client, second, overwrite=True)
    assert replaced.status_code == 200 and replaced.json()["replaced"] is True
    assert client.get(f"{B}/tools/count_walls").json()["description"] == "two"


@pytest.mark.parametrize("text, problem", [
    ("name: [unclosed", "not a YAML pack file"),
    ("- just\n- a list\n", "pack must be a mapping"),
    ("name: old_style\ncode_template: return 1;\n", "schema_version: expected 1"),
    ("schema_version: 1\nname: x\nversion: 1.0.0\ncode_template: 'return \"{level}\";'\n",
     "placeholder {level} is not a declared parameter"),
    ("schema_version: 1\nname: x\nversion: 1.0.0\ncode_template: return 1;\nsecret: 1\n", "unknown field 'secret'"),
])
def test_import_validates_the_file_as_the_package_validates_a_pack(client_local, text, problem):
    client, user_dir = client_local
    resp = yaml_request(client, text)
    assert resp.status_code == 422 and resp.json()["error"] == "invalid_pack", resp.text
    assert any(problem in p for p in resp.json()["problems"]), resp.json()
    assert not list(user_dir.glob("*.yaml"))


def test_import_refuses_bad_names_blocked_code_and_oversized_files(client_local):
    client, user_dir = client_local
    pack = "schema_version: 1\nname: {name}\nversion: 1.0.0\ncode_template: {code}\n"
    for name in ("con", "Nul", "lpt1", "a-b", "1abc"):
        resp = yaml_request(client, pack.format(name=name, code="return 1;"))
        assert resp.status_code == 422 and resp.json()["error"] == "invalid_tool_name", name
    blocked = yaml_request(client, pack.format(name="evil", code="'System.IO.File.Delete(\"x\"); return 1;'"))
    assert blocked.status_code == 400 and blocked.json()["error"] == "blocked"
    big = yaml_request(client, pack.format(name="big", code="return 1;") + "#" * bridge_module.MAX_IMPORT_BYTES)
    assert big.status_code == 422 and big.json()["error"] == "invalid_args"
    not_utf8 = client.post(f"{B}/tools/import", content=b"name: \xff\xfe", headers={"Content-Type": "text/yaml"})
    assert not_utf8.status_code == 422 and not_utf8.json()["problems"] == ["the file is not UTF-8 text"]
    assert not list(user_dir.glob("*.yaml")) and not list(user_dir.glob(".*"))


PACK_HEAD = "schema_version: 1\nname: {name}\nversion: 1.0.0\ncode_template: return 1;\n"


@pytest.mark.parametrize("text", [
    PACK_HEAD.format(name="aliased") + "description: &d hello\nsource_query: *d\n",             # one alias
    PACK_HEAD.format(name="looped") + "not_for: &loop [*loop]\n",                                # refers to itself
    "a: &x [1, 2]\nb: [*x, *x, *x, *x]\n",
])
def test_import_refuses_yaml_aliases(client_local, text):
    client, user_dir = client_local
    resp = yaml_request(client, text)
    assert resp.status_code == 422 and resp.json()["error"] == "invalid_pack", resp.text
    assert "aliases" in resp.json()["problems"][0]
    assert not list(user_dir.glob("*.yaml"))


def test_import_reports_what_the_package_cannot_read_as_a_pack(client_local):
    client, user_dir = client_local
    deep = yaml_request(client, PACK_HEAD.format(name="deep") + "not_for: " + "[" * 3000 + "]" * 3000 + "\n")
    assert deep.status_code == 422 and deep.json()["error"] == "invalid_pack"
    assert "recursion" in deep.json()["problems"][0]
    # validate_pack sorts the keys it does not know: keys of two types make it raise TypeError.
    for extra in ("1: a\nextra: b\n", "parameters:\n- {name: a, 2: x, zz: y, source: designer, required: true}\n"):
        resp = yaml_request(client, PACK_HEAD.format(name="keys") + extra)
        assert resp.status_code == 422 and resp.json()["error"] == "invalid_pack", resp.text
        assert "every key must be text" in resp.json()["problems"][0]
    assert not list(user_dir.glob("*.yaml"))


def test_import_refuses_a_declared_size_over_the_limit_before_reading_the_body(client_local, monkeypatch):
    from starlette.requests import Request

    client, user_dir = client_local
    read: list[int] = []
    real_body = Request.body

    async def spy(self):
        read.append(1)
        return await real_body(self)

    monkeypatch.setattr(Request, "body", spy)
    big = PACK_HEAD.format(name="big") + "#" * bridge_module.MAX_IMPORT_BYTES
    resp = client.post(f"{B}/tools/import", content=big.encode(), headers={"Content-Type": "text/yaml"})
    assert resp.status_code == 422 and resp.json()["error"] == "invalid_args"
    assert read == []                                                   # refused on Content-Length
    assert yaml_request(client, PACK_HEAD.format(name="small")).status_code == 200 and read == [1]


@pytest.fixture
def client_local(make_client, local_env, tmp_path):
    """A local host (no password) and its user capabilities directory."""
    return make_client(**LOCAL), tmp_path / "data" / "capabilities"


# -- built-in packs are read-only ---------------------------------------------------------

def test_built_in_packs_cannot_be_changed_hidden_or_replaced(client_local):
    client, user_dir = client_local
    for method, kwargs in (("put", {"json": {"description": "mine now"}}), ("delete", {})):
        resp = getattr(client, method)(f"{B}/tools/create_wall", **kwargs)
        assert resp.status_code == 409 and resp.json()["error"] == "builtin_read_only", method
        assert "read-only" in resp.json()["message"] and resp.json()["tool"] == "create_wall"
    solid = client.post(f"{B}/solidify", json={"name": "create_wall", "code": "return 1;"})
    assert solid.status_code == 409 and solid.json()["error"] == "builtin_read_only"
    assert not user_dir.exists() or not list(user_dir.iterdir())        # no copy, no marker
    assert client.get(f"{B}/tools/create_wall").json()["description"] != "mine now"


def test_a_user_file_with_a_built_in_name_is_the_designer_s_own(client_local):
    """An older host's edit of a built-in left a user file of that name: it is listed as the
    designer's, can be edited and deleted, and deleting it brings the built-in back."""
    client, user_dir = client_local
    user_dir.mkdir(parents=True)
    original = (builtin_capabilities_dir() / "query_levels.yaml").read_text(encoding="utf-8")
    (user_dir / "query_levels.yaml").write_text(original.replace("description:", "description: (edited)", 1),
                                                encoding="utf-8")
    listed = {t["name"]: t for t in client.get(f"{B}/tools").json()}
    assert (listed["query_levels"]["builtin"], listed["query_levels"]["origin"]) == (False, "user")
    assert listed["query_levels"]["directory"] == str(user_dir)
    assert client.put(f"{B}/tools/query_levels", json={"description": "levels"}).status_code == 200
    assert client.delete(f"{B}/tools/query_levels").json() == {"status": "deleted", "name": "query_levels"}
    assert sorted(p.name for p in user_dir.iterdir() if p.name != "usage.json") == []   # no .disabled marker
    listed = {t["name"]: t for t in client.get(f"{B}/tools").json()}
    assert listed["query_levels"]["origin"] == "builtin" and listed["query_levels"]["builtin"] is True
    # With only the wheel's file left, the name is read-only again: no import, no saving.
    assert yaml_request(client, original).json()["error"] == "builtin_read_only"
    assert yaml_request(client, original, overwrite=True).json()["error"] == "builtin_read_only"
    assert client.post(f"{B}/solidify", json={"name": "query_levels", "code": "return 1;"}).status_code == 409


def test_a_user_file_with_a_built_in_name_is_replaced_like_any_user_pack(client_local):
    """Import and solidify follow the PUT / DELETE rule: the user file of a built-in's name
    is the designer's - an import replaces it only with overwrite=true, solidify replaces it."""
    client, user_dir = client_local
    user_dir.mkdir(parents=True)
    original = (builtin_capabilities_dir() / "query_levels.yaml").read_text(encoding="utf-8")
    (user_dir / "query_levels.yaml").write_text(original, encoding="utf-8")
    edited = original.replace("description:", "description: (mine)", 1)
    taken = yaml_request(client, edited)
    assert taken.status_code == 409 and taken.json()["error"] == "pack_exists"
    replaced = yaml_request(client, edited, overwrite=True)
    assert replaced.status_code == 200 and replaced.json()["replaced"] is True
    assert (user_dir / "query_levels.yaml").read_text(encoding="utf-8") == edited
    saved = client.post(f"{B}/solidify", json={"name": "query_levels", "code": "return 2;", "description": "saved"})
    assert saved.status_code == 200
    assert client.get(f"{B}/tools/query_levels").json()["code_template"] == "return 2;"
    assert client.get(f"{B}/tools/query_levels").json()["description"] == "saved"


# -- tool names ---------------------------------------------------------------------------

BAD_NAMES = ["con", "CON", "Con", "nul", "prn", "aux", "com1", "COM9", "com0", "lpt1", "LPT9", "com\u00b9",
             "lpt\u00b3", "con.txt", "a-b", "a.b", "1abc", "x" * 101, "conin$",
             "\u5899\u4f53", "\u0441reate_wall", "caf\u00e9", "x\uff11"]     # CJK, a Cyrillic c, Latin-1, fullwidth


@pytest.fixture
def no_store_calls(monkeypatch):
    """Fail the test if any ToolStore method that touches a pack file is called."""
    calls: list[tuple[str, object]] = []

    def spy(name):
        real = getattr(ToolStore, name)

        def wrapped(self, *args, **kwargs):
            calls.append((name, args[0] if args else kwargs.get("name")))
            return real(self, *args, **kwargs)
        monkeypatch.setattr(ToolStore, name, wrapped)

    for method in ("load", "path_of", "delete", "update", "solidify", "get_dynamic_params", "is_disabled", "enable"):
        spy(method)
    return calls


@pytest.mark.parametrize("name", BAD_NAMES)
def test_routes_refuse_a_bad_tool_name_before_the_store_is_asked(client_local, no_store_calls, name):
    client, user_dir = client_local
    path = f"{B}/tools/{quote(name, safe='')}"
    spec = spec_for(name, height=3000)
    for method, url, kwargs in (
        ("get", path, {}), ("put", path, {"json": {"description": "x"}}), ("delete", path, {}),
        ("get", f"{path}/choices", {}), ("post", f"{path}/missing-params", {"json": {"known": {}}}),
        ("post", f"{path}/run", {"json": {"params": {}, "token": "t"}}), ("get", f"{path}/export", {}),
        ("post", f"{B}/solidify", {"json": {"name": name, "code": "return 1;"}}),
        ("post", f"{B}/spec/confirm", {"json": {"spec": spec}}),
        ("post", f"{B}/spec/reconcile", {"json": {"spec": spec, "snapshot": None}}),
    ):
        resp = getattr(client, method)(url, **kwargs)
        assert resp.status_code == 422, (method, url, resp.status_code, resp.text)
        assert resp.json()["error"] == "invalid_tool_name", (method, url, resp.json())
    assert no_store_calls == []
    assert not user_dir.exists() or not list(user_dir.iterdir())


@pytest.mark.parametrize("name", ["con", "NUL", "com1", "lpt9", "a/../b", "", None, 7])
def test_the_model_s_tools_refuse_a_bad_tool_name_before_the_store_is_asked(env, no_store_calls, name):
    session = Session(session_id="s")

    def run(tool: str, args: dict):
        return asyncio.run(call_tool(tool, args, session))

    assert run("missing_params", {"tool": name}).result["error"] == "invalid_tool_name"
    assert run("get_tool_choices", {"name": name}).result["error"] == "invalid_tool_name"
    spec = {"task": "x", "action": {"kind": "run_tool", "tool": name}, "parameters": []}
    reconciled = run("reconcile", {"spec": spec, "snapshot": None}).result
    proposed = run("propose_spec", {"spec": spec})
    if isinstance(name, str) and name:
        assert reconciled["error"] == "invalid_tool_name"
        assert proposed.spec_event is None and proposed.result["accepted"] is False
        assert proposed.result["errors"][0]["code"] == "invalid_tool_name"
        assert session.spec is None
    assert no_store_calls == []


def test_good_names_still_reach_the_store(client):
    assert bridge_module.tool_name_problem("create_wall") is None
    assert bridge_module.tool_name_problem("_private2") is None
    assert bridge_module.tool_name_problem("console") is None        # a device name only when whole
    assert bridge_module.tool_name_problem("com10") is None
    assert bridge_module.tool_name_problem("x" * 100) is None
    assert bridge_module.tool_name_problem("con\n") is not None      # $ would match before a newline
    assert client.get(f"{B}/tools/console").status_code == 404       # a name the store was asked about
    # ASCII only: the schema's \\w would take these; the host does not.
    for name in ("\u5899\u4f53", "\u0441reate_wall", "create_wall\u0430"):
        assert "ASCII" in bridge_module.tool_name_problem(name), ascii(name)
        resp = client.get(f"{B}/tools/{quote(name, safe='')}")
        assert resp.status_code == 422 and resp.json()["error"] == "invalid_tool_name", ascii(name)


# -- the tool directory is shared and read from disk on every call ------------------------

def write_pack_in_another_process(env_vars: dict, name: str) -> None:
    """What the package's MCP server does on solidify_tool, in a process of its own."""
    script = ("import sys; from revit_bridge.capabilities import ToolStore; "
              "ToolStore().solidify(name=sys.argv[1], code='return 1;', description='from the MCP server')")
    subprocess.run([sys.executable, "-c", script, name], env=env_vars, check=True, timeout=60)


def child_env() -> dict:
    return {k: v for k, v in os.environ.items()}


def test_a_pack_another_process_saves_is_listed_at_once(client_local):
    client, user_dir = client_local
    before = {t["name"] for t in client.get(f"{B}/tools").json()}
    session = Session(session_id="s")
    assert "from_mcp" not in {t["name"] for t in asyncio.run(call_tool("list_tools", {}, session)).result}

    write_pack_in_another_process(child_env(), "from_mcp")
    assert (user_dir / "from_mcp.yaml").is_file()
    listed = {t["name"]: t for t in client.get(f"{B}/tools").json()}
    assert set(listed) == before | {"from_mcp"}
    assert listed["from_mcp"] == {**listed["from_mcp"], "builtin": False, "origin": "user", "directory": str(user_dir)}
    model_listing = {t["name"]: t for t in asyncio.run(call_tool("list_tools", {}, session)).result}
    assert model_listing["from_mcp"]["origin"] == "user" and "directory" not in model_listing["from_mcp"]
    assert client.get(f"{B}/tools/from_mcp").json()["description"] == "from the MCP server"

    # A pack the host saves is a file there that the other process reads.
    assert client.post(f"{B}/solidify", json={"name": "from_web", "code": "return 2;"}).status_code == 200
    assert (user_dir / "from_web.yaml").is_file()
    read_back = subprocess.run(
        [sys.executable, "-c", "from revit_bridge.capabilities import ToolStore; print(ToolStore().load('from_web').code_template)"],
        env=child_env(), check=True, timeout=60, capture_output=True, text=True)
    assert read_back.stdout.strip() == "return 2;"


def test_a_cloud_host_lists_no_directory(client):
    assert all("directory" not in t and t["origin"] == "builtin" for t in client.get(f"{B}/tools").json())


def test_a_local_host_lists_the_directory(client_local):
    local, _ = client_local
    assert {t["directory"] for t in local.get(f"{B}/tools").json()} == {str(builtin_capabilities_dir())}


def test_without_revit_bridge_data_dir_the_host_uses_the_package_s_own_directory(make_client, local_env, tmp_path,
                                                                                monkeypatch):
    """uv run revit-bridge-web sets nothing: the package's per-user data root, shared with
    its MCP server. A v0 pack there is another host's and stays where it is (the 0.1
    repair runs on this host's own DATA_DIR/capabilities only)."""
    from backend import main as main_module

    local_env.delenv("REVIT_BRIDGE_DATA_DIR", raising=False)
    local_env.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))      # never the real one
    local_env.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    shared = user_capabilities_dir()
    assert tmp_path in shared.parents and shared.parent.name == "revit-bridge"
    shared.mkdir(parents=True)
    (shared / "hand_written.yaml").write_text("name: hand_written\ncode_template: return 1;\n", encoding="utf-8")

    client = make_client(**LOCAL)
    assert ToolStore().user_dir == shared
    assert (shared / "hand_written.yaml").is_file() and not (shared / "legacy-0.1").exists()
    listed = {t["name"]: t for t in client.get(f"{B}/tools").json()}
    assert listed["hand_written"]["directory"] == str(shared)

    # The console entry does not set the package's variables either (it sets PORT: restored after).
    local_env.setenv("PORT", "7860")
    monkeypatch.setattr(main_module.uvicorn, "run", lambda *a, **k: None)
    main_module.main(["--port", "7861"])
    assert "REVIT_BRIDGE_DATA_DIR" not in os.environ and "REVIT_BRIDGE_CAPABILITIES_DIR" not in os.environ
    assert ToolStore().user_dir == shared


def test_the_0_1_repair_still_runs_on_the_host_s_own_volume(make_client, env, tmp_path):
    own = tmp_path / "data" / "capabilities"
    own.mkdir(parents=True)
    (own / "old_copy.yaml").write_text("name: old_copy\ncode_template: return 1;\n", encoding="utf-8")
    make_client()
    assert not (own / "old_copy.yaml").exists() and (own / "legacy-0.1" / "old_copy.yaml").is_file()


def test_the_suite_never_uses_the_real_data_root(env, tmp_path):
    """Every test above runs under conftest's env: the package's data root is the test's own."""
    assert Path(os.environ["REVIT_BRIDGE_DATA_DIR"]) == tmp_path / "data"
    assert tmp_path in user_capabilities_dir().parents
