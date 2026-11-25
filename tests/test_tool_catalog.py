import pytest
from tests.fake_revit import FakeRevit


def test_offline_catalog_keeps_metadata_and_validator_contract(client):
    tools = client.get("/api/v1/bridge/tools").json()
    wall = next(tool for tool in tools if tool["name"] == "create_wall")
    detail = client.get("/api/v1/bridge/tools/create_wall").json()
    assert wall["display_name"] == detail["display_name"]
    assert wall["not_for"] == detail["not_for"] and wall["applies_when"] == detail["applies_when"]
    assert isinstance(wall["revit_versions"], list) and wall["builtin"] and wall["origin"] == "builtin"
    assert wall["validator"] == detail["validator"]["kind"]


@pytest.mark.parametrize("policy,expected", [({"allowed": False}, False), ({"allowed": True}, True), ({}, None)])
def test_policy_comes_from_selected_addin(client, revit, policy, expected):
    original = revit.handler
    def handle(request):
        if request["method"] == "get_bridge_capabilities":
            return FakeRevit.ok(request["id"], {"protocol": "1", "user_tools": policy})
        return original(request)
    revit.handler = handle
    health = client.get("/api/v1/bridge/revit-health").json()
    assert health["revit_connected"] and health["user_tools_allowed"] is expected
