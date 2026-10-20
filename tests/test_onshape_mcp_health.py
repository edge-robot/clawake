"""Onshape MCP health command, image preflight and post-apply health wait (#39)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from clawake import cli
from clawake.services import onshape_mcp
from clawake.services.onshape_mcp import (
    DEFAULT_HEALTH_COMMAND,
    McpProbes,
    OnshapeMcpSpec,
    render_onshape_mcp,
)
from clawake.services.systemd import CommandResult

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


# --- fake host ----------------------------------------------------------------------------------


class FakeMcpHost:
    """Answers the podman/systemctl probes of the MCP lifecycle; never runs anything."""

    def __init__(
        self,
        *,
        preflight: dict[str, int] | None = None,
        health: list[str] | None = None,
        restarts: list[str] | None = None,
    ) -> None:
        self.preflight = preflight or {}
        self.health = list(health or ["healthy"])
        self.restarts = list(restarts or ["0"])
        self.calls: list[list[str]] = []
        self.now = 0.0

    def run(self, argv: list[str], timeout: float) -> CommandResult:
        assert timeout > 0
        self.calls.append(list(argv))
        if argv[:2] == ["podman", "run"]:
            executable = argv[argv.index("--entrypoint") + 1]
            code = self.preflight.get(executable, 0)
            return CommandResult(argv, code, "", "")
        if argv[:2] == ["podman", "inspect"]:
            value = self.health.pop(0) if len(self.health) > 1 else self.health[0]
            if value == "<missing>":
                return CommandResult(argv, 125, "", "Error: no such container")
            return CommandResult(argv, 0, value + "\n", "")
        if argv[:3] == ["systemctl", "--user", "show"]:
            value = self.restarts.pop(0) if len(self.restarts) > 1 else self.restarts[0]
            return CommandResult(argv, 0, value + "\n", "")
        raise AssertionError(f"unexpected command {argv}")

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds > 0
        self.now += seconds

    def probes(self) -> McpProbes:
        return McpProbes(run=self.run, clock=self.clock, sleep=self.sleep)

    def podman_runs(self) -> list[list[str]]:
        return [call for call in self.calls if call[:2] == ["podman", "run"]]


class FakeSystemd:
    def __init__(self, status_code: int = 0) -> None:
        self.calls: list[tuple[str, str, bool]] = []
        self.status_code = status_code

    def _result(self, verb: str, name: str, execute: bool, code: int = 0) -> CommandResult:
        self.calls.append((verb, name, execute))
        return CommandResult(["systemctl", "--user", verb, f"{name}.service"], code, "", "")

    def daemon_reload(self, execute: bool = False) -> CommandResult:
        self.calls.append(("daemon-reload", "", execute))
        return CommandResult(["systemctl", "--user", "daemon-reload"], 0, "", "")

    def restart(self, name: str, execute: bool = False) -> CommandResult:
        return self._result("restart", name, execute)

    def stop(self, name: str, execute: bool = False) -> CommandResult:
        return self._result("stop", name, execute)

    def status(self, name: str, execute: bool = False) -> CommandResult:
        return self._result("status", name, execute, self.status_code if execute else 0)


@pytest.fixture
def service_file(tmp_path: Path):
    def write(**health: object) -> Path:
        credential = tmp_path / "credential"
        credential.mkdir(mode=0o700, exist_ok=True)
        config = tmp_path / "config.toml"
        config.write_text("non-secret config\n", encoding="utf-8")
        config.chmod(0o600)
        updates: dict[str, object] = {
            "credential_directory": str(credential),
            "config_file": str(config),
            "quadlet_root": str(tmp_path / "quadlet"),
        }
        if health:
            updates["health"] = health
        path = tmp_path / "service.json"
        path.write_text(json.dumps(_service_json(**updates)), encoding="utf-8")
        return path

    return write


def _invoke(
    monkeypatch: pytest.MonkeyPatch,
    host: FakeMcpHost,
    systemd: FakeSystemd,
    *args: str,
):
    monkeypatch.setattr(onshape_mcp, "default_mcp_probes", host.probes)
    monkeypatch.setattr(cli, "SystemdService", lambda: systemd)
    return CliRunner().invoke(cli.app, ["onshape-mcp", *args])


def _quadlet_files(tmp_path: Path) -> list[str]:
    root = tmp_path / "quadlet"
    return sorted(path.name for path in root.iterdir()) if root.exists() else []


# --- preflight ----------------------------------------------------------------------------------


def test_plan_preflight_is_the_only_podman_call_and_is_read_only(
    tmp_path: Path, service_file, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = FakeMcpHost()
    systemd = FakeSystemd()
    path = service_file(command=BINARY_COMMAND)

    result = _invoke(monkeypatch, host, systemd, "-c", str(path), "--action", "plan")

    assert result.exit_code == 0, result.output
    assert host.calls == [
        [
            "podman",
            "run",
            "--rm",
            "--pull=never",
            "--network=none",
            "--entrypoint",
            "/usr/local/bin/onshape-mcp",
            IMAGE_REF,
            "--help",
        ]
    ]
    assert systemd.calls == []
    assert _quadlet_files(tmp_path) == []


def test_default_shell_form_preflights_the_shell_and_curl(
    tmp_path: Path, service_file, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = FakeMcpHost()
    path = service_file()

    result = _invoke(monkeypatch, host, FakeSystemd(), "-c", str(path), "--action", "plan")

    assert result.exit_code == 0, result.output
    assert [call[call.index("--entrypoint") + 1] for call in host.podman_runs()] == [
        "/bin/sh",
        "curl",
    ]


@pytest.mark.parametrize("code", [126, 127])
@pytest.mark.parametrize("action", ["plan", "apply"])
def test_missing_health_executable_fails_closed_before_any_write(
    tmp_path: Path,
    service_file,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    code: int,
) -> None:
    host = FakeMcpHost(preflight={"curl": code})
    systemd = FakeSystemd()
    path = service_file()

    result = _invoke(monkeypatch, host, systemd, "-c", str(path), "--action", action, "--execute")

    assert result.exit_code == 1, result.output
    assert result.output.strip().splitlines() == [
        f"Onshape MCP image {IMAGE_REF} cannot run health command curl: "
        f"executable missing or not runnable (podman exit {code})"
    ]
    assert systemd.calls == []
    assert _quadlet_files(tmp_path) == []


def test_preflight_podman_error_fails_closed(
    tmp_path: Path, service_file, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = FakeMcpHost(preflight={"/usr/local/bin/onshape-mcp": 125})
    systemd = FakeSystemd()
    path = service_file(command=BINARY_COMMAND)

    result = _invoke(monkeypatch, host, systemd, "-c", str(path), "--action", "apply", "--execute")

    assert result.exit_code == 1, result.output
    assert result.output.strip() == (
        f"Onshape MCP preflight could not run image {IMAGE_REF} for health command "
        "/usr/local/bin/onshape-mcp: podman exit 125 (the image must be in local storage)"
    )
    assert systemd.calls == []
    assert _quadlet_files(tmp_path) == []


def test_help_exit_code_of_the_command_itself_counts_as_present(
    tmp_path: Path, service_file, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = FakeMcpHost(preflight={"/usr/local/bin/onshape-mcp": 2})
    path = service_file(command=BINARY_COMMAND)

    result = _invoke(monkeypatch, host, FakeSystemd(), "-c", str(path), "--action", "plan")

    assert result.exit_code == 0, result.output
