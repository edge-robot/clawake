"""Member readiness model (ADR-003 P1). Every probe is a fake; nothing touches the host."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from clawake.config import InstanceSpec, Inventory
from clawake.services.readiness import (
    HttpOutcome,
    MemberReadiness,
    ReadinessProbes,
    pinned_plugin_version,
    probe_member,
    wait_members_ready,
)
from clawake.services.systemd import CommandResult

FAKE_SHA = "sha256:" + "a" * 64
ACTIVE = "Active: active (running) since Mon 2026-10-19 10:00:00 UTC; 5min ago"


def _archive(tmp_path: Path, plugin_id: str, version: str) -> Path:
    path = tmp_path / f"{plugin_id}-{version}.tgz"
    payload = json.dumps({"name": plugin_id, "version": version}).encode()
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo("package/package.json")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return path


def _instance(
    tmp_path: Path,
    *,
    plugins: list[dict] | None = None,
    readiness: dict | None = None,
    name: str = "one",
    port: int = 18789,
) -> InstanceSpec:
    member: dict = {
        "name": name,
        "host": "h",
        "role": "developer",
        "workspace_path": str(tmp_path / name / "workspace"),
        "team_definition_path": str(tmp_path / "team.yml"),
        "quadlet_path": f"{name}.container",
        "container_name": f"{name}-ctr",
        "image": {"repository": "ghcr.io/openclaw/openclaw", "tag": "2026.9.9"},
        "ports": [
            {
                "bind_address": "127.0.0.1",
                "host_port": port,
                "container_port": 18789,
                "protocol": "tcp",
            }
        ],
        "dashboard": {"friendly_name": name},
    }
    if plugins is not None:
        member["plugins"] = plugins
    if readiness is not None:
        member["readiness"] = readiness
    inventory = Inventory.model_validate(
        {
            "version": 1,
            "cluster": {"name": "c", "mode": "single_host", "primary_host": "h"},
            "hosts": [{"name": "h", "quadlet_root": str(tmp_path / "quadlets")}],
            "instances": [member],
        }
    )
    return inventory.instances[0]


def _pinned(tmp_path: Path, plugin_id: str = "example-cad", version: str = "1.2.3") -> dict:
    return {
        "id": plugin_id,
        "artifact_path": str(_archive(tmp_path, plugin_id, version)),
        "sha256": FAKE_SHA,
    }


def _plugins_json(*entries: dict) -> str:
    return json.dumps({"plugins": list(entries), "diagnostics": []})


def _plugin_entry(**overrides: object) -> dict:
    entry = {
        "id": "example-cad",
        "version": "1.2.3",
        "enabled": True,
        "status": "loaded",
        "origin": "installed",
    }
    entry.update(overrides)
    return entry


def _channels_json(*accounts: dict) -> str:
    return json.dumps({"channelAccounts": {"discord": list(accounts)}})


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds > 0
        self.sleeps.append(seconds)
        self.now += seconds


class FakeProbes:
    """Scripted probe answers; every call is recorded for argv assertions."""

    def __init__(
        self,
        *,
        unit: CommandResult | list[CommandResult] | None = None,
        readyz: HttpOutcome | list[HttpOutcome] | None = None,
        plugins: CommandResult | None = None,
        channels: CommandResult | None = None,
        clock: FakeClock | None = None,
        probe_cost: float = 0.0,
    ) -> None:
        self.unit = unit if unit is not None else _unit(0, ACTIVE)
        self.readyz = readyz if readyz is not None else HttpOutcome(status=200)
        self.plugins = plugins if plugins is not None else _ok(_plugins_json(_plugin_entry()))
        self.channels = channels if channels is not None else _ok(_channels_json())
        self.clock = clock or FakeClock()
        self.probe_cost = probe_cost
        self.commands: list[list[str]] = []
        self.urls: list[tuple[str, float]] = []
        self.timeouts: list[float] = []

    @staticmethod
    def _next(value):
        if isinstance(value, list):
            return value.pop(0) if len(value) > 1 else value[0]
        return value

    def unit_status(self, name: str) -> CommandResult:
        return self._next(self.unit)

    def run(self, argv: list[str], timeout: float) -> CommandResult:
        self.commands.append(argv)
        self.timeouts.append(timeout)
        self.clock.now += min(self.probe_cost, timeout)
        if argv[-3:-1] == ["plugins", "list"]:
            return self.plugins
        if argv[-3:-1] == ["channels", "status"]:
            return self.channels
        raise AssertionError(f"unexpected command {argv}")

    def http_get(self, url: str, timeout: float) -> HttpOutcome:
        self.urls.append((url, timeout))
        self.timeouts.append(timeout)
        self.clock.now += min(self.probe_cost, timeout)
        return self._next(self.readyz)

    def bundle(self) -> ReadinessProbes:
        return ReadinessProbes(
            unit_status=self.unit_status,
            run=self.run,
            http_get=self.http_get,
            clock=self.clock,
            sleep=self.clock.sleep,
        )


def _unit(rc: int, stdout: str) -> CommandResult:
    return CommandResult(["systemctl", "--user", "status", "one.service"], rc, stdout, "")


def _ok(stdout: str) -> CommandResult:
    return CommandResult(["podman"], 0, stdout, "")


def _codes(result: MemberReadiness) -> list[str]:
    return [reason.code for reason in result.reasons]


# --- stopped ---------------------------------------------------------------------------


def test_stopped_when_unit_inactive(tmp_path: Path) -> None:
    probes = FakeProbes(unit=_unit(3, "Active: inactive (dead)"))
    result = probe_member(_instance(tmp_path), probes.bundle())
    assert result.state == "stopped"
    assert _codes(result) == ["unit_inactive"]
    assert probes.urls == []
    assert probes.commands == []


def test_stopped_when_unit_not_found(tmp_path: Path) -> None:
    probes = FakeProbes(unit=_unit(4, ""))
    result = probe_member(_instance(tmp_path), probes.bundle())
    assert result.state == "stopped"
    assert _codes(result) == ["unit_not_found"]


# --- failed ----------------------------------------------------------------------------


def test_failed_when_unit_failed(tmp_path: Path) -> None:
    probes = FakeProbes(unit=_unit(3, "Active: failed (Result: exit-code)"))
    result = probe_member(_instance(tmp_path), probes.bundle())
    assert result.state == "failed"
    assert _codes(result) == ["unit_failed"]
    assert probes.urls == []


def test_failed_when_unit_waits_for_auto_restart(tmp_path: Path) -> None:
    probes = FakeProbes(unit=_unit(3, "Active: activating (auto-restart) (Result: exit-code)"))
    result = probe_member(_instance(tmp_path), probes.bundle())
    assert result.state == "failed"
    assert _codes(result) == ["unit_auto_restart"]


def test_failed_when_unit_state_unknown(tmp_path: Path) -> None:
    probes = FakeProbes(unit=_unit(1, "garbled"))
    result = probe_member(_instance(tmp_path), probes.bundle())
    assert result.state == "failed"
    assert _codes(result) == ["unit_state_unknown"]


# --- starting --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        (HttpOutcome(status=503), "readyz_not_ready"),
        (HttpOutcome(error="refused"), "readyz_unreachable"),
        (HttpOutcome(error="timeout"), "readyz_timeout"),
        (HttpOutcome(error="error"), "readyz_unreachable"),
        (HttpOutcome(status=204), "readyz_not_ready"),
    ],
)
def test_starting_when_readyz_not_200(tmp_path: Path, outcome: HttpOutcome, code: str) -> None:
    probes = FakeProbes(readyz=outcome)
    result = probe_member(_instance(tmp_path, plugins=[_pinned(tmp_path)]), probes.bundle())
    assert result.state == "starting"
    assert _codes(result) == [code]
    assert probes.commands == []


def test_starting_when_unit_is_activating(tmp_path: Path) -> None:
    probes = FakeProbes(unit=_unit(3, "Active: activating (start) since today"))
    result = probe_member(_instance(tmp_path), probes.bundle())
    assert result.state == "starting"
    assert _codes(result) == ["unit_activating"]
    assert probes.urls == []


def test_readyz_probe_uses_published_host_port_and_bounded_timeout(tmp_path: Path) -> None:
    probes = FakeProbes()
    probe_member(_instance(tmp_path, port=28789), probes.bundle())
    assert probes.urls == [("http://127.0.0.1:28789/readyz", 5)]


# --- ready -----------------------------------------------------------------------------


def test_ready_without_plugins_or_channels(tmp_path: Path) -> None:
    probes = FakeProbes()
    result = probe_member(_instance(tmp_path), probes.bundle())
    assert result.state == "ready"
    assert result.reasons == ()
    assert probes.commands == []


def test_ready_when_pinned_plugin_matches(tmp_path: Path) -> None:
    probes = FakeProbes()
    result = probe_member(_instance(tmp_path, plugins=[_pinned(tmp_path)]), probes.bundle())
    assert result.state == "ready"
    assert probes.commands == [
        ["podman", "exec", "one-ctr", "openclaw", "plugins", "list", "--json"]
    ]
    assert [plugin.to_json() for plugin in result.plugins] == [
        {
            "id": "example-cad",
            "pinned_version": "1.2.3",
            "installed_version": "1.2.3",
            "status": "loaded",
            "enabled": True,
        }
    ]


def test_ready_when_npm_plugin_matches_spec_version(tmp_path: Path) -> None:
    npm = {"id": "example-cad", "npm_spec": "@example/cad@1.2.3", "integrity": "sha512-AAAA"}
    probes = FakeProbes()
    result = probe_member(_instance(tmp_path, plugins=[npm]), probes.bundle())
    assert result.state == "ready"
    assert result.plugins[0].pinned_version == "1.2.3"


def test_ready_when_channels_enabled_and_running(tmp_path: Path) -> None:
    probes = FakeProbes(
        channels=_ok(
            _channels_json({"accountId": "default", "running": True, "lifecycle": "running"})
        )
    )
    instance = _instance(tmp_path, readiness={"channels": True})
    result = probe_member(instance, probes.bundle())
    assert result.state == "ready"
    assert probes.commands == [
        ["podman", "exec", "one-ctr", "openclaw", "channels", "status", "--json"]
    ]


# --- degraded: plugins -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("plugins_stdout", "code"),
    [
        (_plugins_json(_plugin_entry(version="1.2.2")), "plugin_version_mismatch"),
        (_plugins_json(_plugin_entry(enabled=False)), "plugin_disabled"),
        (_plugins_json(_plugin_entry(status="error")), "plugin_not_loaded"),
        (_plugins_json(_plugin_entry(id="other-plugin")), "plugin_missing"),
        (_plugins_json(), "plugin_missing"),
        ("not json", "plugins_unparseable"),
        (json.dumps({"items": []}), "plugins_unparseable"),
        (json.dumps({"plugins": [{"id": "example-cad", "version": 3}]}), "plugins_unparseable"),
        (json.dumps([]), "plugins_unparseable"),
    ],
)
def test_degraded_when_plugin_does_not_match_pin(
    tmp_path: Path, plugins_stdout: str, code: str
) -> None:
    probes = FakeProbes(plugins=_ok(plugins_stdout))
    result = probe_member(_instance(tmp_path, plugins=[_pinned(tmp_path)]), probes.bundle())
    assert result.state == "degraded"
    assert code in _codes(result)


def test_degraded_when_plugins_list_command_fails(tmp_path: Path) -> None:
    probes = FakeProbes(plugins=CommandResult(["podman"], 125, "", "Error: secret-ish detail"))
    result = probe_member(_instance(tmp_path, plugins=[_pinned(tmp_path)]), probes.bundle())
    assert result.state == "degraded"
    assert _codes(result) == ["plugins_list_failed"]
    assert "secret-ish" not in result.reasons[0].detail


def test_degraded_when_pinned_archive_unreadable(tmp_path: Path) -> None:
    pinned = _pinned(tmp_path)
    Path(pinned["artifact_path"]).write_bytes(b"not a tarball")
    probes = FakeProbes()
    result = probe_member(_instance(tmp_path, plugins=[pinned]), probes.bundle())
    assert result.state == "degraded"
    assert "plugin_pin_unreadable" in _codes(result)


def test_pinned_plugin_version_reads_package_json(tmp_path: Path) -> None:
    instance = _instance(tmp_path, plugins=[_pinned(tmp_path, version="0.4.1")])
    assert pinned_plugin_version(instance.plugins[0]) == "0.4.1"


# --- degraded: channels ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("channels_stdout", "code"),
    [
        (_channels_json({"accountId": "default", "running": False}), "channel_not_running"),
        (
            _channels_json({"accountId": "default", "running": False, "lifecycle": "blocked"}),
            "channel_not_running",
        ),
        (
            _channels_json({"accountId": "default", "running": True, "lifecycle": "blocked"}),
            "channel_lifecycle",
        ),
        (json.dumps({"status": "starting", "startupPhase": "plugins"}), "channels_starting"),
        ("{", "channels_unparseable"),
        (json.dumps({"unexpected": True}), "channels_unparseable"),
        (_channels_json({"accountId": "default"}), "channels_unparseable"),
        (_channels_json(), "channels_none_reported"),
    ],
)
def test_degraded_when_channel_is_not_running(
    tmp_path: Path, channels_stdout: str, code: str
) -> None:
    probes = FakeProbes(channels=_ok(channels_stdout))
    result = probe_member(_instance(tmp_path, readiness={"channels": True}), probes.bundle())
    assert result.state == "degraded"
    assert code in _codes(result)


LIVE_CHANNELS = Path(__file__).parent / "fixtures" / "channels-status-live-shape.json"


def _live_channels(**overrides: object) -> str:
    """The recorded 2026.9.9 shape (synthetic IDs) with one discord account overridden."""
    payload = json.loads(LIVE_CHANNELS.read_text(encoding="utf-8"))
    payload["channelAccounts"]["discord"][0].update(overrides)
    return json.dumps(payload)


def test_channel_live_shape_with_lifecycle_ready_is_up(tmp_path: Path) -> None:
    probes = FakeProbes(channels=_ok(_live_channels()))
    result = probe_member(_instance(tmp_path, readiness={"channels": True}), probes.bundle())
    assert result.state == "ready"
    assert result.reasons == ()


def test_channel_live_shape_connected_false_is_degraded(tmp_path: Path) -> None:
    probes = FakeProbes(channels=_ok(_live_channels(connected=False)))
    result = probe_member(_instance(tmp_path, readiness={"channels": True}), probes.bundle())
    assert result.state == "degraded"
    assert _codes(result) == ["channel_not_connected"]


@pytest.mark.parametrize("lifecycle", ["warming", "Ready", ""])
def test_channel_live_shape_unknown_lifecycle_is_degraded(tmp_path: Path, lifecycle: str) -> None:
    probes = FakeProbes(channels=_ok(_live_channels(lifecycle=lifecycle)))
    result = probe_member(_instance(tmp_path, readiness={"channels": True}), probes.bundle())
    assert result.state == "degraded"
    assert _codes(result) == ["channel_lifecycle"]


def test_channel_running_without_lifecycle_is_degraded(tmp_path: Path) -> None:
    stdout = _channels_json({"accountId": "default", "running": True})
    probes = FakeProbes(channels=_ok(stdout))
    result = probe_member(_instance(tmp_path, readiness={"channels": True}), probes.bundle())
    assert _codes(result) == ["channel_lifecycle"]


def test_channel_ready_lifecycle_but_not_running_is_degraded(tmp_path: Path) -> None:
    probes = FakeProbes(channels=_ok(_live_channels(running=False)))
    result = probe_member(_instance(tmp_path, readiness={"channels": True}), probes.bundle())
    assert _codes(result) == ["channel_not_running"]


def test_channel_lifecycle_blocked_reports_both_running_and_lifecycle(tmp_path: Path) -> None:
    stdout = _channels_json({"accountId": "default", "running": False, "lifecycle": "blocked"})
    probes = FakeProbes(channels=_ok(stdout))
    result = probe_member(_instance(tmp_path, readiness={"channels": True}), probes.bundle())
    assert _codes(result) == ["channel_not_running", "channel_lifecycle"]


def test_channel_checking_is_off_by_default_and_never_probes(tmp_path: Path) -> None:
    probes = FakeProbes(channels=_ok(_channels_json({"accountId": "default", "running": False})))
    instance = _instance(tmp_path)
    assert instance.readiness.channels is False
    assert instance.readiness.start_window_seconds == 180
    result = probe_member(instance, probes.bundle())
    assert result.state == "ready"
    assert not any("channels" in argv for argv in probes.commands)


def test_channel_probe_never_uses_probe_flag(tmp_path: Path) -> None:
    probes = FakeProbes()
    probe_member(_instance(tmp_path, readiness={"channels": True}), probes.bundle())
    assert probes.commands
    assert all("--probe" not in argv for argv in probes.commands)


def test_subprocess_probes_have_bounded_timeouts(tmp_path: Path) -> None:
    probes = FakeProbes()
    instance = _instance(tmp_path, plugins=[_pinned(tmp_path)], readiness={"channels": True})
    probe_member(instance, probes.bundle())
    assert len(probes.timeouts) == 3
    assert all(0 < timeout <= 10 for timeout in probes.timeouts)


# --- secrets ---------------------------------------------------------------------------


def test_reasons_never_contain_secret_looking_values(tmp_path: Path) -> None:
    fake_token = "FAKE-TOKEN-0123456789abcdef0123456789abcdef"
    plugins_stdout = _plugins_json(
        _plugin_entry(version=f"Bearer {fake_token}", status=f"token={fake_token}")
    )
    channels_stdout = _channels_json(
        {
            "accountId": f"http://user:{fake_token}@example.invalid",
            "running": False,
            "lifecycle": f"blocked {fake_token}",
            "lastError": f"401 token {fake_token}",
        }
    )
    probes = FakeProbes(plugins=_ok(plugins_stdout), channels=_ok(channels_stdout))
    instance = _instance(tmp_path, plugins=[_pinned(tmp_path)], readiness={"channels": True})
    result = probe_member(instance, probes.bundle())
    dumped = json.dumps(result.to_json())
    assert result.state == "degraded"
    assert fake_token not in dumped
    assert "example.invalid" not in dumped
    assert "Bearer" not in dumped


# --- bounded wait ----------------------------------------------------------------------


def test_wait_reaches_ready(tmp_path: Path) -> None:
    clock = FakeClock()
    probes = FakeProbes(
        readyz=[HttpOutcome(error="refused"), HttpOutcome(status=503), HttpOutcome(status=200)],
        clock=clock,
    )
    [result] = wait_members_ready([_instance(tmp_path)], probes=probes.bundle())
    assert result.state == "ready"
    assert len(probes.urls) == 3
    assert clock.now - 1000.0 < 180


def test_wait_times_out_to_failed_and_never_exceeds_window(tmp_path: Path) -> None:
    clock = FakeClock()
    probes = FakeProbes(readyz=HttpOutcome(error="timeout"), clock=clock, probe_cost=7.0)
    instance = _instance(tmp_path, readiness={"start_window_seconds": 30})
    [result] = wait_members_ready([instance], probes=probes.bundle())
    assert result.state == "failed"
    assert _codes(result) == ["readyz_timeout", "start_window_exceeded"]
    assert clock.now <= 1000.0 + 30
    assert all(timeout <= 10 for timeout in probes.timeouts)


def test_wait_timeout_override_bounds_the_window(tmp_path: Path) -> None:
    clock = FakeClock()
    probes = FakeProbes(readyz=HttpOutcome(status=503), clock=clock)
    [result] = wait_members_ready([_instance(tmp_path)], timeout=5, probes=probes.bundle())
    assert result.state == "failed"
    assert clock.now <= 1005.0
    assert sum(clock.sleeps) <= 5


def test_wait_accepts_degraded_as_settled(tmp_path: Path) -> None:
    clock = FakeClock()
    probes = FakeProbes(plugins=_ok(_plugins_json()), clock=clock)
    instance = _instance(tmp_path, plugins=[_pinned(tmp_path)])
    [result] = wait_members_ready([instance], probes=probes.bundle())
    assert result.state == "degraded"
    assert clock.sleeps == []


def test_wait_keeps_stopped_member_stopped_after_window(tmp_path: Path) -> None:
    clock = FakeClock()
    probes = FakeProbes(unit=_unit(3, "Active: inactive (dead)"), clock=clock)
    instance = _instance(tmp_path, readiness={"start_window_seconds": 10})
    [result] = wait_members_ready([instance], probes=probes.bundle())
    assert result.state == "stopped"
    assert _codes(result) == ["unit_inactive", "start_window_exceeded"]
    assert clock.now <= 1010.0


def test_wait_rejects_non_positive_timeout(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        wait_members_ready([_instance(tmp_path)], timeout=0, probes=FakeProbes().bundle())


def test_readiness_window_is_validated(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _instance(tmp_path, readiness={"start_window_seconds": 0})
    with pytest.raises(ValueError):
        _instance(tmp_path, readiness={"unknown": True})


def test_health_default_path_is_readyz(tmp_path: Path) -> None:
    assert _instance(tmp_path).health.path == "/readyz"


# --- status CLI ------------------------------------------------------------------------

LEGACY_STATUS_KEYS = {
    "instance",
    "role",
    "state",
    "return_code",
    "unit",
    "stdout",
    "stderr",
    "diagnostics",
}


def _cli_config(tmp_path: Path, members: list[dict]) -> Path:
    import yaml

    (tmp_path / "team.yml").write_text("team: sample\n", encoding="utf-8")
    instances = []
    for index, extra in enumerate(members):
        name = extra.pop("name")
        instances.append(
            {
                "name": name,
                "host": "h",
                "role": "developer",
                "workspace_path": str(tmp_path / name / "workspace"),
                "state_path": str(tmp_path / name / "state"),
                "team_definition_path": str(tmp_path / name / "team.yml"),
                "quadlet_path": f"{name}.container",
                "container_name": f"{name}-ctr",
                "image": {"repository": "ghcr.io/openclaw/openclaw", "tag": "2026.9.9"},
                "ports": [
                    {
                        "bind_address": "127.0.0.1",
                        "host_port": 18789 + index * 10,
                        "container_port": 18789,
                        "protocol": "tcp",
                    }
                ],
                "dashboard": {"friendly_name": name},
                **extra,
            }
        )
    data = {
        "version": 1,
        "cluster": {"name": "c", "mode": "single_host", "primary_host": "h"},
        "hosts": [{"name": "h", "quadlet_root": str(tmp_path / "quadlets")}],
        "instances": instances,
    }
    path = tmp_path / "inventory.yml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


class _CliSystemd:
    """Fake SystemdService: per-member status, no journal content, no mutation methods."""

    units: dict[str, CommandResult] = {}

    def status(self, instance_name: str, execute: bool = False) -> CommandResult:
        return self.units.get(instance_name, _unit(0, ACTIVE))

    def logs(self, instance_name: str, lines: int = 100, execute: bool = False) -> CommandResult:
        return CommandResult(["journalctl"], 0, "", "")

    def unit_name(self, instance_name: str) -> str:
        return f"{instance_name}.service"


def _patch_cli(monkeypatch, fake: FakeProbes, units: dict[str, CommandResult] | None = None):
    from clawake import cli

    class Systemd(_CliSystemd):
        pass

    Systemd.units = units or {}

    def probes(service: object) -> ReadinessProbes:
        bundle = fake.bundle()
        return ReadinessProbes(
            unit_status=lambda name: service.status(name, execute=True),
            run=bundle.run,
            http_get=bundle.http_get,
            clock=bundle.clock,
            sleep=bundle.sleep,
        )

    monkeypatch.setattr(cli, "SystemdService", Systemd)
    monkeypatch.setattr(cli, "default_probes", probes)


def _invoke(*args: str):
    from typer.testing import CliRunner

    from clawake.cli import app

    return CliRunner().invoke(app, ["status", *args])


def test_status_json_keeps_legacy_keys_and_adds_readiness(tmp_path: Path, monkeypatch) -> None:
    cfg = _cli_config(tmp_path, [{"name": "one", "plugins": [_pinned(tmp_path)]}])
    _patch_cli(monkeypatch, FakeProbes())

    result = _invoke("--config", str(cfg), "--format", "json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) == {"cluster", "mode", "instances"}
    row = payload["instances"][0]
    assert LEGACY_STATUS_KEYS | {"readiness", "reasons", "plugins"} == set(row)
    assert row["state"] == "healthy"
    assert row["readiness"] == "ready"
    assert row["reasons"] == []
    assert row["plugins"] == [
        {
            "id": "example-cad",
            "pinned_version": "1.2.3",
            "installed_version": "1.2.3",
            "status": "loaded",
            "enabled": True,
        }
    ]


def test_status_json_starting_keeps_deprecated_state_healthy(tmp_path: Path, monkeypatch) -> None:
    cfg = _cli_config(tmp_path, [{"name": "one"}])
    _patch_cli(monkeypatch, FakeProbes(readyz=HttpOutcome(error="refused")))

    result = _invoke("--config", str(cfg), "--format", "json")

    assert result.exit_code == 0
    row = json.loads(result.output)["instances"][0]
    assert row["state"] == "healthy"
    assert row["readiness"] == "starting"
    assert row["reasons"] == [
        {"code": "readyz_unreachable", "detail": "GET /readyz: connection refused"}
    ]


def test_status_text_shows_readiness_reasons_and_degraded_marker(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = _cli_config(tmp_path, [{"name": "one", "plugins": [_pinned(tmp_path)]}])
    fake = FakeProbes(plugins=_ok(_plugins_json(_plugin_entry(version="1.2.2"))))
    _patch_cli(monkeypatch, fake)

    result = _invoke("--config", str(cfg))

    assert result.exit_code == 0
    assert "- one [developer] state=healthy rc=0 readiness=degraded" in result.output
    assert "WARNING: degraded" in result.output
    assert (
        "  reason: plugin_version_mismatch: example-cad: installed 1.2.2, pinned 1.2.3"
        in result.output
    )
    assert "plugin: example-cad pinned=1.2.3 installed=1.2.2 status=loaded enabled=yes" in (
        result.output
    )


@pytest.mark.parametrize(
    ("unit", "readyz", "expected_exit", "readiness"),
    [
        (_unit(0, ACTIVE), HttpOutcome(status=200), 0, "ready"),
        (_unit(0, ACTIVE), HttpOutcome(status=503), 0, "starting"),
        (_unit(3, "Active: inactive (dead)"), HttpOutcome(status=200), 1, "stopped"),
        (_unit(3, "Active: failed (Result: signal)"), HttpOutcome(status=200), 1, "failed"),
    ],
)
def test_status_exit_codes(tmp_path: Path, monkeypatch, unit, readyz, expected_exit, readiness):
    cfg = _cli_config(tmp_path, [{"name": "one"}])
    _patch_cli(monkeypatch, FakeProbes(readyz=readyz), units={"one": unit})

    result = _invoke("--config", str(cfg), "--format", "json")

    assert result.exit_code == expected_exit
    assert json.loads(result.output)["instances"][0]["readiness"] == readiness


def test_status_exit_non_zero_if_any_member_stopped(tmp_path: Path, monkeypatch) -> None:
    cfg = _cli_config(tmp_path, [{"name": "one"}, {"name": "two"}])
    units = {"two": _unit(3, "Active: inactive (dead)")}
    _patch_cli(monkeypatch, FakeProbes(), units=units)

    result = _invoke("--config", str(cfg), "--format", "json")

    assert result.exit_code == 1
    states = [row["readiness"] for row in json.loads(result.output)["instances"]]
    assert states == ["ready", "stopped"]


def test_status_wait_reaches_ready_and_exits_zero(tmp_path: Path, monkeypatch) -> None:
    cfg = _cli_config(tmp_path, [{"name": "one"}])
    clock = FakeClock()
    fake = FakeProbes(readyz=[HttpOutcome(error="refused"), HttpOutcome(status=200)], clock=clock)
    _patch_cli(monkeypatch, fake)

    result = _invoke("--config", str(cfg), "--wait", "--format", "json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["instances"][0]["readiness"] == "ready"
    assert clock.sleeps == [2.0]


def test_status_wait_times_out_to_failed_within_override(tmp_path: Path, monkeypatch) -> None:
    cfg = _cli_config(tmp_path, [{"name": "one"}])
    clock = FakeClock()
    _patch_cli(monkeypatch, FakeProbes(readyz=HttpOutcome(status=503), clock=clock))

    result = _invoke("--config", str(cfg), "--wait", "--timeout", "7")

    assert result.exit_code == 1
    assert "readiness=failed" in result.output
    assert "reason: start_window_exceeded: not ready within 7s" in result.output
    assert clock.now <= 1007.0


def test_status_wait_degraded_exits_zero(tmp_path: Path, monkeypatch) -> None:
    cfg = _cli_config(tmp_path, [{"name": "one", "plugins": [_pinned(tmp_path)]}])
    _patch_cli(monkeypatch, FakeProbes(plugins=_ok(_plugins_json())))

    result = _invoke("--config", str(cfg), "--wait")

    assert result.exit_code == 0
    assert "readiness=degraded" in result.output
    assert "reason: plugin_missing: example-cad: not installed" in result.output


def test_status_without_wait_never_sleeps(tmp_path: Path, monkeypatch) -> None:
    cfg = _cli_config(tmp_path, [{"name": "one"}])
    clock = FakeClock()
    _patch_cli(monkeypatch, FakeProbes(readyz=HttpOutcome(status=503), clock=clock))

    result = _invoke("--config", str(cfg))

    assert result.exit_code == 0
    assert clock.sleeps == []


def test_status_timeout_requires_wait(tmp_path: Path, monkeypatch) -> None:
    cfg = _cli_config(tmp_path, [{"name": "one"}])
    _patch_cli(monkeypatch, FakeProbes())

    result = _invoke("--config", str(cfg), "--timeout", "5")

    assert result.exit_code == 2


def test_status_cli_channel_probe_argv_is_read_only(tmp_path: Path, monkeypatch) -> None:
    cfg = _cli_config(
        tmp_path,
        [{"name": "one", "plugins": [_pinned(tmp_path)], "readiness": {"channels": True}}],
    )
    fake = FakeProbes(
        channels=_ok(
            _channels_json({"accountId": "default", "running": True, "lifecycle": "running"})
        )
    )
    _patch_cli(monkeypatch, fake)

    result = _invoke("--config", str(cfg))

    assert result.exit_code == 0
    assert fake.commands == [
        ["podman", "exec", "one-ctr", "openclaw", "plugins", "list", "--json"],
        ["podman", "exec", "one-ctr", "openclaw", "channels", "status", "--json"],
    ]
    assert fake.urls == [("http://127.0.0.1:18789/readyz", 5)]


def test_v2_status_rejects_wait(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "services-v2.yml"

    result = _invoke("--config", str(fixture), "--wait")

    assert result.exit_code == 2
    assert "only supported for v1" in result.output
