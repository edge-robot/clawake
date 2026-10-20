"""Onshape MCP health command, image preflight and post-apply health wait (#39)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from clawake.services.onshape_mcp import (
    DEFAULT_HEALTH_COMMAND,
    OnshapeMcpSpec,
    render_onshape_mcp,
)

FIXTURES = Path(__file__).parent / "fixtures" / "onshape-mcp"
DIGEST = "sha256:" + "a" * 64
IMAGE_REF = f"localhost/onshape-mcp:pinned@{DIGEST}"
BINARY_COMMAND = ["/usr/local/bin/onshape-mcp", "healthcheck"]


def _service_json(**updates: object) -> dict[str, object]:
    """The key set of a deployed service.json, with synthetic values."""
    data: dict[str, object] = {
        "name": "clawonshape-mcp",
        "network": "clawonshape-mcp-private",
        "image": {"repository": "localhost/onshape-mcp", "tag": "pinned", "digest": DIGEST},
        "credential_directory": "/srv/example-mcp/credential",
        "config_file": "/srv/example-mcp/config.toml",
        "quadlet_root": "/srv/example-quadlet",
        "client_container": "engineer",
    }
    data.update(updates)
    return data


def _health_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("Health")]


# --- spec and rendering -------------------------------------------------------------------------


def test_service_json_without_health_renders_todays_quadlet_byte_for_byte() -> None:
    spec = OnshapeMcpSpec.model_validate(_service_json())

    rendered = render_onshape_mcp(spec)

    assert sorted(rendered) == sorted(path.name for path in FIXTURES.iterdir())
    for name, text in rendered.items():
        assert text == (FIXTURES / name).read_text(encoding="utf-8"), name


def test_default_health_block_reproduces_the_legacy_health_lines() -> None:
    spec = OnshapeMcpSpec.model_validate(_service_json())

    assert spec.health.command == DEFAULT_HEALTH_COMMAND
    assert _health_lines(render_onshape_mcp(spec)["clawonshape-mcp.container"]) == [
        "HealthCmd=curl --fail --silent http://127.0.0.1:8080/health",
        "HealthInterval=30s",
        "HealthTimeout=5s",
        "HealthRetries=3",
        "HealthOnFailure=kill",
    ]


def test_binary_health_command_renders_exec_form_without_shell() -> None:
    spec = OnshapeMcpSpec.model_validate(_service_json(health={"command": BINARY_COMMAND}))

    assert _health_lines(render_onshape_mcp(spec)["clawonshape-mcp.container"]) == [
        'HealthCmd=["CMD","/usr/local/bin/onshape-mcp","healthcheck"]',
        "HealthInterval=30s",
        "HealthTimeout=5s",
        "HealthRetries=3",
        "HealthOnFailure=kill",
    ]


def test_custom_health_timing_and_quoting() -> None:
    health = {
        "command": ["probe", "--url", "http://127.0.0.1:8080/health?x=1&y=2", 'a "b" c\\d'],
        "interval_s": 10,
        "timeout_s": 2,
        "retries": 5,
        "start_period_s": 20,
    }
    spec = OnshapeMcpSpec.model_validate(_service_json(health=health))

    lines = _health_lines(render_onshape_mcp(spec)["clawonshape-mcp.container"])

    assert lines == [
        'HealthCmd=["CMD","probe","--url","http://127.0.0.1:8080/health?x=1&y=2",'
        '"a \\"b\\" c\\\\d"]',
        "HealthInterval=10s",
        "HealthTimeout=2s",
        "HealthRetries=5",
        "HealthStartPeriod=20s",
        "HealthOnFailure=kill",
    ]
    value = lines[0].removeprefix("HealthCmd=")
    assert json.loads(value) == ["CMD", *health["command"]]


@pytest.mark.parametrize(
    "command",
    [
        [],
        [""],
        ["/usr/local/bin/onshape-mcp", ""],
        ["/usr/local/bin/onshape-mcp", "health\ncheck"],
        ["/usr/local/bin/onshape-mcp\n"],
        ["/usr/local/bin/onshape-mcp", "%n"],
        ["/usr/local/bin/%h/onshape-mcp"],
        ["/usr/local/bin/onshape-mcp", "${HOME}"],
        ["/usr/local/bin/onshape-mcp", "tab\there"],
        ["relative/onshape-mcp", "healthcheck"],
        ["./onshape-mcp"],
        ["-x"],
        ["onshape mcp"],
        ["x"] * 17,
    ],
)
def test_unsafe_health_commands_are_rejected(command: list[str]) -> None:
    with pytest.raises(ValidationError):
        OnshapeMcpSpec.model_validate(_service_json(health={"command": command}))


@pytest.mark.parametrize(
    "field,value",
    [("interval_s", 0), ("timeout_s", 0), ("retries", 0), ("start_period_s", -1), ("extra", 1)],
)
def test_health_timing_bounds_and_unknown_keys(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        OnshapeMcpSpec.model_validate(
            _service_json(health={"command": BINARY_COMMAND, field: value})
        )
