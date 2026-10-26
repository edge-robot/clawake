"""Goal dispatch journal volume of the Onshape MCP service (#40)."""

import builtins
import io
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from clawake import cli
from clawake.cli import app
from clawake.services import onshape_mcp
from clawake.services.onshape_mcp import (
    DISPATCH_JOURNAL_CONTAINER_PATH,
    McpProbes,
    OnshapeMcpSpec,
    check_journal_config,
    plan_onshape_mcp,
    render_onshape_mcp,
    validate_runtime_paths,
)
from clawake.services.systemd import CommandResult

FIXTURES = Path(__file__).parent / "fixtures" / "onshape-mcp-journal"
CONTAINER = "clawonshape-mcp.container"
JOURNAL_LINE = f"Volume=/srv/example-mcp/journal:{DISPATCH_JOURNAL_CONTAINER_PATH}:rw\n"
IMAGE = {
    "repository": "localhost/onshape-mcp",
    "tag": "pinned",
    "digest": "sha256:" + "a" * 64,
}
GRANT = 'goal_grant_file = "/run/onshape-credentials/goal-grant.json"\n'
JOURNAL = f'dispatch_journal_dir = "{DISPATCH_JOURNAL_CONTAINER_PATH}"\n'
# Fake values of other keys; error messages must never echo them.
OTHER = (
    'mutation_approval_file = "/run/onshape-credentials/fake-approval-marker.json"\n'
    'public_url = "http://fake-url-marker:8080/"\n'
)
runner = CliRunner()


def fixed_spec(journal: bool) -> OnshapeMcpSpec:
    data = {
        "image": IMAGE,
        "credential_directory": "/srv/example-mcp/credential",
        "config_file": "/srv/example-mcp/config.toml",
        "quadlet_root": "/srv/example-quadlet",
    }
    if journal:
        data["dispatch_journal_directory"] = "/srv/example-mcp/journal"
    return OnshapeMcpSpec.model_validate(data)


def spec_data(tmp_path: Path, journal: bool = True) -> dict:
    data = {
        "image": dict(IMAGE),
        "credential_directory": str(tmp_path / "credential"),
        "config_file": str(tmp_path / "config.toml"),
        "quadlet_root": str(tmp_path / "quadlet"),
    }
    if journal:
        data["dispatch_journal_directory"] = str(tmp_path / "journal")
    return data


def config_text(*keys: str) -> str:
    return "[external_token]\n" + OTHER + "".join(keys)


# A1 / A2: rendering


def test_quadlet_without_journal_is_byte_identical_to_the_previous_render():
    # The golden was rendered by the base commit (85079c8) with the same spec.
    rendered = render_onshape_mcp(fixed_spec(journal=False))[CONTAINER]
    assert rendered == (FIXTURES / "without-journal.container").read_text()
    assert DISPATCH_JOURNAL_CONTAINER_PATH not in rendered


def test_quadlet_with_journal_adds_exactly_one_rw_volume_line():
    without = render_onshape_mcp(fixed_spec(journal=False))
    with_journal = render_onshape_mcp(fixed_spec(journal=True))
    rendered = with_journal[CONTAINER]
    assert rendered == (FIXTURES / "with-journal.container").read_text()
    journal_lines = [
        line for line in rendered.splitlines() if DISPATCH_JOURNAL_CONTAINER_PATH in line
    ]
    assert journal_lines == [JOURNAL_LINE.rstrip("\n")]
    # The new line follows the existing volumes; nothing else changes.
    assert rendered.replace(JOURNAL_LINE, "") == without[CONTAINER]
    lines = rendered.splitlines()
    index = lines.index(JOURNAL_LINE.rstrip("\n"))
    assert lines[index - 1].startswith("Volume=") and lines[index - 1].endswith(":ro")
    assert (
        with_journal[f"{fixed_spec(True).network}.network"]
        == without[f"{fixed_spec(False).network}.network"]
    )


def test_journal_plan_is_idempotent(tmp_path):
    value = OnshapeMcpSpec.model_validate(spec_data(tmp_path))
    for name, content in render_onshape_mcp(value).items():
        target = Path(value.quadlet_root) / name
        target.parent.mkdir(exist_ok=True)
        target.write_text(content)
    assert plan_onshape_mcp(value) == []


# Disjointness


@pytest.mark.parametrize(
    "journal",
    [
        "credential",
        "credential/journal",
        "config.toml",
        "quadlet",
        "quadlet/journal",
        "",
    ],
)
def test_journal_directory_must_be_disjoint_from_other_mcp_paths(tmp_path, journal):
    data = spec_data(tmp_path)
    data["dispatch_journal_directory"] = str(tmp_path / journal)
    with pytest.raises(ValidationError, match="must be disjoint"):
        OnshapeMcpSpec.model_validate(data)


def test_journal_directory_must_be_absolute(tmp_path):
    data = spec_data(tmp_path)
    data["dispatch_journal_directory"] = "relative/journal"
    with pytest.raises(ValidationError):
        OnshapeMcpSpec.model_validate(data)


# Consistency rules (pure)


def test_consistent_configurations_pass(tmp_path):
    declared = OnshapeMcpSpec.model_validate(spec_data(tmp_path))
    undeclared = OnshapeMcpSpec.model_validate(spec_data(tmp_path, journal=False))
    check_journal_config(declared, config_text(GRANT, JOURNAL))
    check_journal_config(declared, config_text(JOURNAL))
    check_journal_config(undeclared, config_text())
    check_journal_config(undeclared, "")
    # Keys outside [external_token] are not the server's goal keys (§12.1).
    check_journal_config(undeclared, GRANT + JOURNAL + config_text())


def test_goal_grant_without_declared_journal_fails(tmp_path):
    value = OnshapeMcpSpec.model_validate(spec_data(tmp_path, journal=False))
    with pytest.raises(ValueError, match="goal_grant_file") as error:
        check_journal_config(value, config_text(GRANT))
    assert "dispatch_journal_directory" in str(error.value)


def test_declared_journal_without_dispatch_journal_dir_fails(tmp_path):
    value = OnshapeMcpSpec.model_validate(spec_data(tmp_path))
    with pytest.raises(ValueError, match="dispatch_journal_dir is not"):
        check_journal_config(value, config_text(GRANT))
    with pytest.raises(ValueError, match="dispatch_journal_dir is not"):
        check_journal_config(value, config_text())


def test_declared_journal_with_other_dispatch_journal_dir_fails(tmp_path):
    value = OnshapeMcpSpec.model_validate(spec_data(tmp_path))
    wrong = 'dispatch_journal_dir = "/fake-wrong-journal-marker"\n'
    with pytest.raises(ValueError, match="dispatch_journal_dir is not") as error:
        check_journal_config(value, config_text(GRANT, wrong))
    assert "fake-wrong-journal-marker" not in str(error.value)


def test_dispatch_journal_dir_without_declared_journal_fails(tmp_path):
    # The server would point at a path on the read-only root file system.
    value = OnshapeMcpSpec.model_validate(spec_data(tmp_path, journal=False))
    with pytest.raises(ValueError, match="dispatch_journal_dir"):
        check_journal_config(value, config_text(JOURNAL))


@pytest.mark.parametrize(
    ("journal", "keys"),
    [
        (False, (GRANT,)),
        (True, (GRANT,)),
        (True, (GRANT, 'dispatch_journal_dir = "/fake-wrong-journal-marker"\n')),
        (False, (JOURNAL,)),
    ],
)
def test_error_messages_do_not_leak_config_values(tmp_path, journal, keys):
    value = OnshapeMcpSpec.model_validate(spec_data(tmp_path, journal=journal))
    with pytest.raises(ValueError) as error:
        check_journal_config(value, config_text(*keys))
    message = str(error.value)
    for marker in ["fake-approval-marker", "fake-url-marker", "goal-grant.json", "fake-wrong"]:
        assert marker not in message


def test_invalid_toml_fails_without_quoting_the_file(tmp_path):
    value = OnshapeMcpSpec.model_validate(spec_data(tmp_path))
    with pytest.raises(ValueError, match="not valid TOML") as error:
        check_journal_config(value, '[external_token]\npublic_url = "fake-url-marker\n')
    assert "fake-url-marker" not in str(error.value)


# A4: private permissions of the journal directory


def private_runtime(tmp_path: Path) -> OnshapeMcpSpec:
    value = OnshapeMcpSpec.model_validate(spec_data(tmp_path))
    Path(value.credential_directory).mkdir(mode=0o700)
    Path(value.config_file).write_text(config_text(GRANT, JOURNAL))
    Path(value.config_file).chmod(0o600)
    Path(value.dispatch_journal_directory).mkdir(mode=0o700)
    return value


def test_private_journal_directory_passes(tmp_path):
    validate_runtime_paths(private_runtime(tmp_path))


def test_missing_journal_directory_fails(tmp_path):
    value = private_runtime(tmp_path)
    Path(value.dispatch_journal_directory).rmdir()
    with pytest.raises(FileNotFoundError):
        validate_runtime_paths(value)


@pytest.mark.parametrize("mode", [0o750, 0o705, 0o770, 0o701, 0o710])
def test_journal_directory_with_group_or_other_bits_fails(tmp_path, mode):
    value = private_runtime(tmp_path)
    Path(value.dispatch_journal_directory).chmod(mode)
    with pytest.raises(ValueError, match="private owner permissions"):
        validate_runtime_paths(value)


def test_journal_file_instead_of_directory_fails(tmp_path):
    value = private_runtime(tmp_path)
    journal = Path(value.dispatch_journal_directory)
    journal.rmdir()
    journal.write_text("")
    journal.chmod(0o600)
    with pytest.raises(ValueError, match="private owner permissions"):
        validate_runtime_paths(value)


def test_journal_symlink_fails(tmp_path):
    value = private_runtime(tmp_path)
    journal = Path(value.dispatch_journal_directory)
    journal.rmdir()
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    journal.symlink_to(target)
    with pytest.raises(ValueError, match="private owner permissions"):
        validate_runtime_paths(value)


def test_journal_directory_owned_by_another_user_fails(tmp_path, monkeypatch):
    import os

    value = private_runtime(tmp_path)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(tmp_path).st_uid + 1)
    with pytest.raises(ValueError, match="private owner permissions"):
        validate_runtime_paths(value)


# A3 / A4 / A5: CLI plan and apply


class FakeSystemd:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def daemon_reload(self, execute: bool = False) -> CommandResult:
        assert execute
        self.calls.append("daemon-reload")
        return CommandResult(["systemctl", "--user", "daemon-reload"], 0, "", "")

    def restart(self, name: str, execute: bool = False) -> CommandResult:
        assert execute
        self.calls.append(f"restart {name}")
        return CommandResult(["systemctl", "--user", "restart", name], 0, "", "")


def healthy_mcp_run(argv: list[str], timeout: float) -> CommandResult:
    """#39 seam: the health preflight passes, the container is healthy, no restarts."""
    assert timeout > 0
    if argv[:2] == ["podman", "run"]:
        return CommandResult(argv, 0, "", "")
    if argv[:2] == ["podman", "inspect"]:
        return CommandResult(argv, 0, "healthy\n", "")
    if argv[:3] == ["systemctl", "--user", "show"]:
        return CommandResult(argv, 0, "0\n", "")
    raise AssertionError(f"unexpected command {argv}")


@pytest.fixture
def systemd(monkeypatch) -> FakeSystemd:
    fake = FakeSystemd()
    monkeypatch.setattr(cli, "SystemdService", lambda: fake)
    # #39's health preflight runs during plan; never reach real podman/systemctl.
    monkeypatch.setattr(onshape_mcp, "default_mcp_probes", lambda: McpProbes(run=healthy_mcp_run))
    return fake


def write_service(tmp_path: Path, journal: bool = True) -> Path:
    path = tmp_path / "service.yml"
    path.write_text(yaml.safe_dump(spec_data(tmp_path, journal=journal)))
    return path


def invoke(service: Path, action: str, execute: bool = False):
    args = ["onshape-mcp", "--config", str(service), "--action", action]
    if execute:
        args.append("--execute")
    return runner.invoke(app, args)


def quadlet_files(tmp_path: Path) -> list[Path]:
    root = tmp_path / "quadlet"
    return sorted(root.iterdir()) if root.exists() else []


@pytest.mark.parametrize("execute", [False, True])
@pytest.mark.parametrize("action", ["plan", "apply"])
@pytest.mark.parametrize(
    ("journal", "keys"),
    [(False, (GRANT,)), (True, (GRANT,)), (False, (JOURNAL,))],
)
def test_inconsistent_config_fails_plan_and_apply_before_any_change(
    tmp_path, systemd, action, execute, journal, keys
):
    value = private_runtime(tmp_path)
    Path(value.config_file).write_text(config_text(*keys))
    service = write_service(tmp_path, journal=journal)
    result = invoke(service, action, execute=execute)
    assert result.exit_code == 2
    assert "dispatch_journal" in result.output
    assert "fake-approval-marker" not in result.output
    assert "fake-url-marker" not in result.output
    assert quadlet_files(tmp_path) == []
    assert systemd.calls == []


def test_unreadable_config_fails_the_preview(tmp_path, systemd):
    service = write_service(tmp_path, journal=False)
    result = invoke(service, "plan")
    assert result.exit_code == 2
    assert "config.toml is not readable" in result.output
    assert quadlet_files(tmp_path) == []


def test_consistent_preview_mutates_nothing(tmp_path, systemd):
    private_runtime(tmp_path)
    service = write_service(tmp_path)
    for action in ["plan", "apply"]:
        result = invoke(service, action)
        assert result.exit_code == 0, result.output
        assert f"clawonshape-mcp: {tmp_path / 'quadlet' / CONTAINER}" in result.output
    assert quadlet_files(tmp_path) == []
    assert systemd.calls == []


def test_apply_execute_checks_journal_permissions_before_writing(tmp_path, systemd):
    value = private_runtime(tmp_path)
    Path(value.dispatch_journal_directory).chmod(0o750)
    result = invoke(write_service(tmp_path), "apply", execute=True)
    assert result.exit_code != 0
    assert "private owner permissions" in str(result.exception)
    assert quadlet_files(tmp_path) == []
    assert systemd.calls == []


def test_apply_execute_reads_only_config_toml(tmp_path, systemd, monkeypatch):
    value = private_runtime(tmp_path)
    credential = Path(value.credential_directory)
    for name in ["goal-grant.json", "approved-update.json", "access-token.json"]:
        (credential / name).write_text("{}")
        (credential / name).chmod(0o600)
    (Path(value.dispatch_journal_directory) / "goal.jsonl").write_text("")
    opened: list[Path] = []
    original_path_open = Path.open
    original_open = builtins.open

    def path_open(self, *args, **kwargs):
        opened.append(Path(self).absolute())
        return original_path_open(self, *args, **kwargs)

    def recording_open(file, *args, **kwargs):
        if isinstance(file, (str, Path)):
            opened.append(Path(file).absolute())
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(Path, "open", path_open)
    monkeypatch.setattr(builtins, "open", recording_open)
    monkeypatch.setattr(io, "open", recording_open)
    result = invoke(write_service(tmp_path), "apply", execute=True)
    assert result.exit_code == 0, result.output
    assert systemd.calls == ["daemon-reload", "restart clawonshape-mcp"]
    assert Path(value.config_file) in opened
    for path in opened:
        assert not path.is_relative_to(credential)
        assert not path.is_relative_to(Path(value.dispatch_journal_directory))
    deployed = (tmp_path / "quadlet" / CONTAINER).read_text()
    assert f"{value.dispatch_journal_directory}:{DISPATCH_JOURNAL_CONTAINER_PATH}:rw" in deployed
