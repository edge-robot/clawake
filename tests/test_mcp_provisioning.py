import json
from pathlib import Path

import pytest

from clawake.config import Inventory, McpServerSpec, load_inventory
from clawake.services.runtime_config import plan_runtime_files


@pytest.fixture
def team(tmp_path):
    data = load_inventory(Path("robotics-team/team.yml")).model_dump()
    for member in data["instances"]:
        member["workspace_path"] = str(tmp_path / member["name"])
    return Inventory.model_validate(data)


def server(**updates):
    return McpServerSpec.model_validate(
        dict(url="https://mcp.example.test/mcp", tools=["inspect"], oauth_scope="read", **updates)
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("url", "http://mcp.example.test/mcp"),
        ("url", "https://secret@mcp.example.test/mcp"),
        ("url", "https://mcp.example.test/mcp?token=secret"),
        ("tools", ["*"]),
        ("tools", ["inspect", "inspect"]),
        ("tools", []),
        ("oauth_scope", "read\nwrite"),
    ],
)
def test_unsafe_spec_rejected(field, value):
    data = dict(url="https://mcp.example.test/mcp", tools=["inspect"], oauth_scope="read")
    data[field] = value
    with pytest.raises(ValueError):
        McpServerSpec.model_validate(data)


def apply(plan):
    for path, content, _, _ in plan:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def test_provision_no_credentials_no_grants_no_connection_and_safe_removal(team):
    member = team.instances[1]
    member.openclaw.mcp_servers["cad"] = server()
    path = Path(member.workspace_path) / ".openclaw/openclaw.json"
    path.parent.mkdir(parents=True)
    unmanaged = {"url": "https://other.example.test/mcp"}
    path.write_text(
        json.dumps(
            {"mcp": {"servers": {"other": unmanaged}}, "tools": {"allow": ["message"]}}
        )
    )
    apply(plan_runtime_files(team, member))
    result = json.loads(path.read_text())
    assert result["mcp"]["servers"]["cad"] == server().runtime_config()
    assert result["mcp"]["servers"]["cad"]["enabled"] is False
    assert result["tools"]["allow"] == ["message"]
    assert plan_runtime_files(team, member) == []
    member.openclaw.mcp_servers.clear()
    apply(plan_runtime_files(team, member))
    assert json.loads(path.read_text())["mcp"]["servers"] == {"other": unmanaged}


def test_unmanaged_and_externally_modified_server_are_not_overwritten(team):
    member = team.instances[1]
    member.openclaw.mcp_servers["cad"] = server()
    path = Path(member.workspace_path) / ".openclaw/openclaw.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"mcp": {"servers": {"cad": {"enabled": True}}}}))
    with pytest.raises(ValueError, match="unmanaged"):
        plan_runtime_files(team, member)
    path.write_text("{}")
    apply(plan_runtime_files(team, member))
    payload = json.loads(path.read_text())
    payload["mcp"]["servers"]["cad"]["enabled"] = True
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="changed outside"):
        plan_runtime_files(team, member)
