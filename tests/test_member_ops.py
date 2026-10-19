"""Targeted restarts, readiness-gated plugin sync and the upgrade start window (ADR-003 P2).

Every host interaction is a fake: systemd, the /readyz GET, `podman exec` and the clock.
Nothing touches Podman, systemd, OpenClaw or the network.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from typer.testing import CliRunner

from clawake.cli import app
from clawake.config import load_inventory
from clawake.services.deployment import apply_artifacts, plan_deployment
from clawake.services.gateway_config import (
    ensure_plugin_runtime_config,
    read_installed_plugins,
    record_installed_plugin,
)
from clawake.services.plugins import artifact_digest
from clawake.services.readiness import HttpOutcome, ReadinessProbes
from clawake.services.systemd import CommandResult

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
ACTIVE = "Active: active (running) since Mon 2026-10-19 10:00:00 UTC; 5min ago"
INACTIVE = "Active: inactive (dead)"
FAILED = "Active: failed (Result: exit-code)"
START_DELAY = 46.0


# --- fixtures ---------------------------------------------------------------------------


def _archive(tmp_path: Path, plugin_id: str, version: str) -> Path:
    path = tmp_path / "artifacts" / f"{plugin_id}-{version}.tgz"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"name": plugin_id, "version": version}).encode()
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo("package/package.json")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return path


def _pin(tmp_path: Path, version: str, plugin_id: str = "example-cad") -> dict:
    path = _archive(tmp_path, plugin_id, version)
    return {"id": plugin_id, "artifact_path": str(path), "sha256": artifact_digest(path)}


def _config(tmp_path: Path, members: list[dict]) -> Path:
    (tmp_path / "shared").mkdir(exist_ok=True)
    instances = []
    for index, extra in enumerate(members):
        extra = dict(extra)
        name = extra.pop("name")
        team = tmp_path / name / "team.yml"
        team.parent.mkdir(parents=True, exist_ok=True)
        team.write_text("team: sample\n", encoding="utf-8")
        env_file = tmp_path / name / "member.env"
        env_file.write_text("OPENCLAW_GATEWAY_TOKEN=fake-token-for-tests\n", encoding="utf-8")
        instances.append(
            {
                "name": name,
                "host": "h",
                "role": "developer",
                "workspace_path": str(tmp_path / name / "workspace"),
                "state_path": str(tmp_path / name / "state"),
                "team_definition_path": str(team),
                "quadlet_path": f"{name}.container",
                "container_name": f"{name}-ctr",
                "image": {"repository": "ghcr.io/openclaw/openclaw", "tag": "2026.9.9"},
                "env_files": [str(env_file)],
                "ports": [
                    {
                        "bind_address": "127.0.0.1",
                        "host_port": 18789 + index * 10,
                        "container_port": 18789,
                        "protocol": "tcp",
                    }
                ],
                "gateway_runtime": {"enabled": True, "bind": "loopback"},
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


def _deploy(cfg: Path) -> None:
    """Install the current rendering, as a previous setup --execute would have."""
    inventory = load_inventory(cfg)
    for instance in inventory.instances:
        instance.runtime_state_path.mkdir(parents=True, exist_ok=True)
    apply_artifacts(plan_deployment(inventory, inventory.instances, TEMPLATES))


def _set_image_tag(cfg: Path, member: str, tag: str) -> None:
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    for instance in data["instances"]:
        if instance["name"] == member:
            instance["image"]["tag"] = tag
    cfg.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _instance(cfg: Path, name: str):
    return next(item for item in load_inventory(cfg).instances if item.name == name)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds > 0
        self.now += seconds


class FakeHost:
    """systemd + gateway + container: units, /readyz timing, plugin list, podman exec."""

    def __init__(self, members: list[str]) -> None:
        self.clock = FakeClock()
        self.units = dict.fromkeys(members, "active")
        self.ready_at = dict.fromkeys(members, 0.0)
        self.never_ready: set[str] = set()
        self.crash_after_act: set[str] = set()
        self.fail_ops: set[tuple[str, str]] = set()
        self.installed: dict[str, dict[str, str]] = {name: {} for name in members}
        self.archive_versions: dict[tuple[str, str], str] = {}
        self.mounted_digests: dict[tuple[str, str], str] = {}
        self.fail_install: set[str] = set()
        self.keep_old_version: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self.http: list[tuple[str, float]] = []
        self.execs: list[tuple[str, list[str], float]] = []

    # systemd ------------------------------------------------------------------------
    def _member_for_port(self, url: str) -> str:
        port = int(url.split(":")[2].split("/")[0])
        return list(self.units)[(port - 18789) // 10]

    def _act(self, op: str, name: str) -> CommandResult:
        self.calls.append((op, name))
        command = ["systemctl", "--user", op, f"{name}.service"]
        if (op, name) in self.fail_ops:
            return CommandResult(command, 1, "", f"Job for {name}.service failed")
        self.units[name] = "failed" if name in self.crash_after_act else "active"
        self.ready_at[name] = self.clock.now + START_DELAY
        return CommandResult(command, 0, "", "")

    def service(self) -> SimpleNamespace:
        host = self

        def status(name: str, execute: bool = False) -> CommandResult:
            state = host.units[name]
            text = {"active": ACTIVE, "inactive": INACTIVE, "failed": FAILED}[state]
            return CommandResult(["systemctl"], 0 if state == "active" else 3, text, "")

        def ok(*args: object, **kwargs: object) -> CommandResult:
            return CommandResult(["systemctl"], 0, "", "")

        return SimpleNamespace(
            status=status,
            restart=lambda name, execute=False: host._act("restart", name),
            start=lambda name, execute=False: host._act("start", name),
            stop=lambda name, execute=False: host._act("stop", name),
            daemon_reload=lambda execute=False: host._record_reload(),
            disable=ok,
            remove_quadlet=ok,
            logs=ok,
            unit_name=lambda name: f"{name}.service",
        )

    def _record_reload(self) -> CommandResult:
        self.calls.append(("daemon-reload", ""))
        return CommandResult(["systemctl", "--user", "daemon-reload"], 0, "", "")

    # readiness seams ----------------------------------------------------------------
    def http_get(self, url: str, timeout: float) -> HttpOutcome:
        name = self._member_for_port(url)
        self.http.append((name, self.clock.now))
        if name in self.never_ready or self.clock.now < self.ready_at[name]:
            return HttpOutcome(error="refused")
        return HttpOutcome(status=200)

    def run(self, argv: list[str], timeout: float) -> CommandResult:
        name = argv[2].removesuffix("-ctr")
        self.execs.append((name, argv, self.clock.now))
        if argv[-3:-1] == ["plugins", "list"]:
            plugins = [
                {"id": pid, "version": version, "enabled": True, "status": "loaded"}
                for pid, version in self.installed[name].items()
            ]
            return CommandResult(argv, 0, json.dumps({"plugins": plugins}), "")
        raise AssertionError(f"unexpected probe {argv}")

    def probes(self, service: object = None) -> ReadinessProbes:
        svc = self.service()
        return ReadinessProbes(
            unit_status=lambda name: svc.status(name, execute=True),
            run=self.run,
            http_get=self.http_get,
            clock=self.clock,
            sleep=self.clock.sleep,
        )

    # podman exec for installs (clawake.services.plugins.subprocess.run) --------------
    def subprocess_run(self, command: list[str], **_kwargs: object) -> SimpleNamespace:
        name = command[2].removesuffix("-ctr")
        self.execs.append((name, command, self.clock.now))
        if command[3] == "sha256sum":
            plugin_id = Path(command[4]).stem
            digest = self.mounted_digests[(name, plugin_id)]
            return SimpleNamespace(returncode=0, stdout=f"{digest}  {command[4]}\n", stderr="")
        if command[3:6] == ["openclaw", "plugins", "install"]:
            plugin_id = Path(command[-1]).stem
            if name in self.fail_install:
                return SimpleNamespace(returncode=1, stdout="", stderr="ECONNREFUSED")
            if name not in self.keep_old_version:
                self.installed[name][plugin_id] = self.archive_versions[(name, plugin_id)]
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected command {command}")

    def mount(self, member: str, pin: dict, version: str) -> None:
        """The archive bind-mounted into `member`, as setup --execute would render it."""
        self.mounted_digests[(member, pin["id"])] = pin["sha256"].removeprefix("sha256:")
        self.archive_versions[(member, pin["id"])] = version

    def installs(self) -> list[str]:
        return [name for name, argv, _ in self.execs if "install" in argv]

    def patch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from clawake import cli

        service = self.service()
        monkeypatch.setattr(cli, "SystemdService", lambda: service)
        monkeypatch.setattr(cli, "default_probes", self.probes)
        monkeypatch.setattr("clawake.services.plugins.subprocess.run", self.subprocess_run)


def _invoke(*args: str):
    return CliRunner().invoke(app, list(args))


def _setup_rows(output: str) -> dict[str, list[str]]:
    lines = output.splitlines()
    start = lines.index("Setup summary:") + 2
    rows = {}
    for line in lines[start:]:
        if not line.startswith("  "):
            break
        cells = line.split()
        rows[cells[0]] = cells
    return rows


def _sync_rows(output: str) -> dict[str, list[str]]:
    lines = output.splitlines()
    start = lines.index("Plugin sync summary:") + 2
    rows = {}
    for line in lines[start:]:
        if not line.startswith("  "):
            break
        cells = line.split()
        rows[cells[0]] = cells
    return rows


# --- setup ------------------------------------------------------------------------------


@pytest.fixture
def two_members(tmp_path: Path) -> tuple[Path, FakeHost]:
    cfg = _config(tmp_path, [{"name": "alpha"}, {"name": "beta"}])
    _deploy(cfg)
    return cfg, FakeHost(["alpha", "beta"])


def test_setup_restarts_only_the_changed_member(two_members, monkeypatch) -> None:
    cfg, host = two_members
    _set_image_tag(cfg, "alpha", "2026.9.10")
    host.patch(monkeypatch)

    result = _invoke("setup", "-c", str(cfg), "--execute")

    assert result.exit_code == 0, result.output
    assert host.calls == [("daemon-reload", ""), ("restart", "alpha")]
    # beta is unchanged and active: untouched and never waited on.
    assert {name for name, _ in host.http} == {"alpha"}
    rows = _setup_rows(result.output)
    assert rows["alpha"][:5] == ["alpha", "restart", "ok", "ready", "46"]
    assert rows["beta"][:5] == ["beta", "none", "ok", "-", "-"]


def test_setup_starts_unchanged_inactive_member(two_members, monkeypatch) -> None:
    cfg, host = two_members
    _set_image_tag(cfg, "alpha", "2026.9.10")
    host.units["beta"] = "inactive"
    host.patch(monkeypatch)

    result = _invoke("setup", "-c", str(cfg), "--execute")

    assert result.exit_code == 0, result.output
    assert host.calls == [("daemon-reload", ""), ("restart", "alpha"), ("start", "beta")]
    assert " - beta: start (unchanged, unit_inactive)" in result.output
    assert _setup_rows(result.output)["beta"][:4] == ["beta", "start", "ok", "ready"]


def test_setup_unchanged_active_team_is_a_no_op(two_members, monkeypatch) -> None:
    cfg, host = two_members
    host.patch(monkeypatch)

    result = _invoke("setup", "-c", str(cfg), "--execute")

    assert result.exit_code == 0, result.output
    assert host.calls == [("daemon-reload", "")]
    assert host.http == []


def test_setup_restarts_member_on_runtime_config_only_change(two_members, monkeypatch) -> None:
    cfg, host = two_members
    (_instance(cfg, "alpha").runtime_state_path / "openclaw.json").unlink()
    changes = plan_deployment(load_inventory(cfg), [_instance(cfg, "alpha")], TEMPLATES)
    assert changes and {change.kind for change in changes} == {"runtime"}
    host.patch(monkeypatch)

    preview = _invoke("setup", "-c", str(cfg))
    result = _invoke("setup", "-c", str(cfg), "--execute")

    assert " - alpha: restart (runtime_config)" in preview.output
    assert result.exit_code == 0, result.output
    assert host.calls == [("daemon-reload", ""), ("restart", "alpha")]


def test_setup_stops_after_first_member_fails_readiness(two_members, monkeypatch) -> None:
    cfg, host = two_members
    _set_image_tag(cfg, "alpha", "2026.9.10")
    host.units["beta"] = "inactive"
    host.crash_after_act.add("alpha")
    host.patch(monkeypatch)

    result = _invoke("setup", "-c", str(cfg), "--execute")

    assert result.exit_code == 1, result.output
    assert host.calls == [("daemon-reload", ""), ("restart", "alpha")]
    assert {name for name, _ in host.http} <= {"alpha"}
    rows = _setup_rows(result.output)
    assert rows["alpha"][:4] == ["alpha", "restart", "failed", "failed"]
    assert rows["beta"][:3] == ["beta", "start", "not_run"]
    assert "Setup stopped at alpha" in result.output


def test_setup_stops_after_first_member_never_gets_ready(two_members, monkeypatch) -> None:
    cfg, host = two_members
    data = yaml.safe_load(cfg.read_text())
    data["instances"][0]["readiness"] = {"start_window_seconds": 30}
    data["instances"][0]["image"]["tag"] = "2026.9.10"
    cfg.write_text(yaml.safe_dump(data, sort_keys=False))
    host.units["beta"] = "inactive"
    host.never_ready.add("alpha")
    host.patch(monkeypatch)

    result = _invoke("setup", "-c", str(cfg), "--execute")

    assert result.exit_code == 1, result.output
    assert ("start", "beta") not in host.calls
    assert host.clock.now - 1000.0 <= 30.0 + 1e-9
    assert "start_window_exceeded" in _setup_rows(result.output)["alpha"][-1]


def test_setup_stops_after_first_restart_command_fails(two_members, monkeypatch) -> None:
    cfg, host = two_members
    _set_image_tag(cfg, "alpha", "2026.9.10")
    _set_image_tag(cfg, "beta", "2026.9.10")
    host.fail_ops.add(("restart", "alpha"))
    host.patch(monkeypatch)

    result = _invoke("setup", "-c", str(cfg), "--execute")

    assert result.exit_code == 1
    assert host.calls == [("daemon-reload", ""), ("restart", "alpha")]
    assert _setup_rows(result.output)["beta"][:3] == ["beta", "restart", "not_run"]


def test_setup_preview_prints_actions_and_mutates_nothing(two_members, monkeypatch) -> None:
    cfg, host = two_members
    _set_image_tag(cfg, "alpha", "2026.9.10")
    host.units["beta"] = "inactive"
    host.patch(monkeypatch)
    quadlets = Path(load_inventory(cfg).hosts[0].quadlet_root)
    before = {path: path.read_bytes() for path in quadlets.iterdir()}

    result = _invoke("setup", "-c", str(cfg))

    assert result.exit_code == 0, result.output
    assert " - alpha: restart (alpha.container)" in result.output
    assert " - beta: start (unchanged, unit_inactive)" in result.output
    assert host.calls == []
    assert host.http == []
    assert {path: path.read_bytes() for path in quadlets.iterdir()} == before


# --- sync-plugins -----------------------------------------------------------------------


@pytest.fixture
def plugin_team(tmp_path: Path):
    """alpha moves to a new pin (1.2.4); beta stays at the recorded pin (1.2.3)."""
    old = _pin(tmp_path, "1.2.3")
    new = _pin(tmp_path, "1.2.4")
    cfg = _config(
        tmp_path, [{"name": "alpha", "plugins": [new]}, {"name": "beta", "plugins": [old]}]
    )
    host = FakeHost(["alpha", "beta"])
    for name in ("alpha", "beta"):
        instance = _instance(cfg, name)
        instance.runtime_state_path.mkdir(parents=True)
        record_installed_plugin(instance, "example-cad", old["sha256"], "1.2.3")
        ensure_plugin_runtime_config(instance)
        host.installed[name]["example-cad"] = "1.2.3"
    host.mount("alpha", new, "1.2.4")
    host.mount("beta", old, "1.2.3")
    return cfg, host, old, new


def test_sync_skips_identical_pin_without_install_or_restart(plugin_team, monkeypatch) -> None:
    cfg, host, _old, _new = plugin_team
    host.patch(monkeypatch)

    result = _invoke("sync-plugins", "-c", str(cfg), "-m", "beta", "--execute")

    assert result.exit_code == 0, result.output
    assert host.installs() == []
    assert host.calls == []
    assert _sync_rows(result.output)["beta"][:4] == ["beta", "skip", "example-cad", "1.2.3"]


def test_sync_new_digest_installs_restarts_waits_and_verifies(plugin_team, monkeypatch) -> None:
    cfg, host, _old, new = plugin_team
    host.patch(monkeypatch)

    result = _invoke("sync-plugins", "-c", str(cfg), "-m", "alpha", "--execute")

    assert result.exit_code == 0, result.output
    assert host.installs() == ["alpha"]
    assert host.calls == [("restart", "alpha")]
    ledger = read_installed_plugins(_instance(cfg, "alpha"))
    assert ledger["example-cad"].sha256 == new["sha256"]
    assert ledger["example-cad"].version == "1.2.4"
    row = _sync_rows(result.output)["alpha"]
    assert row == ["alpha", "install", "example-cad", "1.2.4", new["sha256"][:19], "ready", "46"]


def test_sync_waits_for_readiness_before_installing(plugin_team, monkeypatch) -> None:
    cfg, host, _old, _new = plugin_team
    host.ready_at["alpha"] = host.clock.now + 30.0
    host.patch(monkeypatch)

    result = _invoke("sync-plugins", "-c", str(cfg), "-m", "alpha", "--execute")

    assert result.exit_code == 0, result.output
    install_times = [at for _, argv, at in host.execs if "install" in argv]
    assert install_times and min(install_times) >= 1030.0


def test_sync_never_installs_on_member_that_does_not_get_ready(plugin_team, monkeypatch) -> None:
    cfg, host, _old, _new = plugin_team
    data = yaml.safe_load(cfg.read_text())
    data["instances"][0]["readiness"] = {"start_window_seconds": 20}
    cfg.write_text(yaml.safe_dump(data, sort_keys=False))
    host.never_ready.add("alpha")
    host.patch(monkeypatch)

    result = _invoke("sync-plugins", "-c", str(cfg), "--execute")

    assert result.exit_code == 1
    assert host.installs() == []
    assert host.calls == []
    # beta: the read-only preview probe only, before alpha's wait began.
    assert [argv[-3:-1] for name, argv, _t in host.execs if name == "beta"] == [["plugins", "list"]]
    assert all(at == 1000.0 for _name, _argv, at in host.execs)
    assert _sync_rows(result.output)["beta"][1] == "not_run"


def test_sync_fails_fast_on_stopped_member(plugin_team, monkeypatch) -> None:
    cfg, host, _old, _new = plugin_team
    host.units["alpha"] = "inactive"
    host.patch(monkeypatch)

    result = _invoke("sync-plugins", "-c", str(cfg), "-m", "alpha", "--execute")

    assert result.exit_code == 1
    assert host.installs() == []
    assert host.clock.now == 1000.0
    assert "run setup --execute first" in result.output


def test_sync_first_member_failure_leaves_second_untouched(plugin_team, monkeypatch) -> None:
    cfg, host, old, _new = plugin_team
    # beta also needs an install (ledger missing), so only stop-on-failure keeps it untouched.
    beta = _instance(cfg, "beta")
    (beta.runtime_state_path / "clawake-managed-plugin-runtime.json").unlink()
    host.fail_install.add("alpha")
    host.patch(monkeypatch)
    ledger_before = read_installed_plugins(_instance(cfg, "alpha"))

    result = _invoke("sync-plugins", "-c", str(cfg), "--execute")

    assert result.exit_code == 1
    failed_at = next(i for i, (_n, argv, _t) in enumerate(host.execs) if "install" in argv)
    # beta only saw the read-only preview probe before execution began; nothing after.
    assert [name for name, _argv, _t in host.execs[failed_at:]] == ["alpha"]
    assert all(
        argv[-3:-1] == ["plugins", "list"] for name, argv, _t in host.execs if name == "beta"
    )
    assert host.calls == []
    assert read_installed_plugins(_instance(cfg, "alpha")) == ledger_before
    rows = _sync_rows(result.output)
    assert rows["alpha"][1] == "failed"
    assert rows["beta"][1] == "not_run"


def test_sync_post_restart_verification_failure_stops(plugin_team, monkeypatch) -> None:
    cfg, host, _old, _new = plugin_team
    host.keep_old_version.add("alpha")
    host.patch(monkeypatch)

    result = _invoke("sync-plugins", "-c", str(cfg), "--execute")

    assert result.exit_code == 1
    assert host.calls == [("restart", "alpha")]
    assert "example-cad:version_mismatch" in result.output
    assert _sync_rows(result.output)["beta"][1] == "not_run"


def test_sync_preview_shows_actions_and_writes_no_ledger(plugin_team, monkeypatch) -> None:
    cfg, host, _old, _new = plugin_team
    host.patch(monkeypatch)
    ledgers = {
        name: (_instance(cfg, name).runtime_state_path / "clawake-managed-plugin-runtime.json")
        for name in ("alpha", "beta")
    }
    before = {name: path.read_bytes() for name, path in ledgers.items()}

    result = _invoke("sync-plugins", "-c", str(cfg))

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert "   action: install (digest_changed, version_mismatch)" in lines
    assert "   action: skip" in lines
    assert "Members to restart: alpha" in lines
    assert {name: path.read_bytes() for name, path in ledgers.items()} == before
    assert host.installs() == []
    assert host.calls == []
    assert all(argv[-3:-1] == ["plugins", "list"] for _n, argv, _ in host.execs)


def test_sync_preview_marks_not_ready_member_unverified(plugin_team, monkeypatch) -> None:
    cfg, host, _old, _new = plugin_team
    host.ready_at["beta"] = host.clock.now + 30.0
    host.patch(monkeypatch)

    result = _invoke("sync-plugins", "-c", str(cfg), "-m", "beta")

    assert "   action: install (unverified) (member_not_ready)" in result.output
    assert "Members to restart: beta (if install)" in result.output
    assert host.clock.now == 1000.0


def test_sync_member_without_ledger_installs_once_then_skips(plugin_team, monkeypatch) -> None:
    cfg, host, old, _new = plugin_team
    beta = _instance(cfg, "beta")
    (beta.runtime_state_path / "clawake-managed-plugin-runtime.json").unlink()
    host.patch(monkeypatch)

    preview = _invoke("sync-plugins", "-c", str(cfg), "-m", "beta")
    first = _invoke("sync-plugins", "-c", str(cfg), "-m", "beta", "--execute")
    second = _invoke("sync-plugins", "-c", str(cfg), "-m", "beta", "--execute")

    assert "   action: install (ledger_missing)" in preview.output
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert host.installs() == ["beta"]
    assert host.calls == [("restart", "beta")]


def test_ledger_survives_grant_reconciliation_and_drops_undeclared(tmp_path: Path) -> None:
    pin = _pin(tmp_path, "1.2.3")
    cfg = _config(tmp_path, [{"name": "alpha", "plugins": [pin]}])
    instance = _instance(cfg, "alpha")
    record_installed_plugin(instance, "example-cad", pin["sha256"], "1.2.3")
    record_installed_plugin(instance, "retired-plugin", pin["sha256"], "0.1.0")

    ensure_plugin_runtime_config(instance)

    assert set(read_installed_plugins(instance)) == {"example-cad"}
    state = json.loads(
        (instance.runtime_state_path / "clawake-managed-plugin-runtime.json").read_text()
    )
    assert state == {
        "version": 1,
        "agentToolAllow": {},
        "globalToolAllow": [],
        "installedPlugins": {"example-cad": {"sha256": pin["sha256"], "version": "1.2.3"}},
    }


def test_ledger_rejects_malformed_entries(tmp_path: Path) -> None:
    cfg = _config(tmp_path, [{"name": "alpha"}])
    instance = _instance(cfg, "alpha")
    path = instance.runtime_state_path / "clawake-managed-plugin-runtime.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "installedPlugins": {"x": {"sha256": "md5:1"}}}))
    with pytest.raises(ValueError, match="installedPlugins"):
        read_installed_plugins(instance)
    with pytest.raises(ValueError, match="non-sha256"):
        record_installed_plugin(instance, "x", "sha512-AAAA", None)


# --- upgrade ----------------------------------------------------------------------------


def _upgrade_patches(monkeypatch, host: FakeHost) -> None:
    from clawake import cli

    host.patch(monkeypatch)
    monkeypatch.setattr(cli, "check_image_availability", lambda image: None)
    monkeypatch.setattr(
        cli,
        "run_doctor",
        lambda instance, image: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        cli,
        "verify_runtime",
        lambda instance, image: SimpleNamespace(
            healthy=True, version="OpenClaw 2026.9.10", image_name="verified", error=""
        ),
    )


def _upgrade(cfg: Path) -> object:
    return _invoke(
        "upgrade",
        "-c",
        str(cfg),
        "-m",
        "alpha",
        "--to",
        "2026.9.10",
        "--digest",
        "sha256:" + "b" * 64,
        "--execute",
    )


def test_upgrade_waits_for_the_start_window(tmp_path: Path, monkeypatch) -> None:
    cfg = _config(tmp_path, [{"name": "alpha", "backup_policy": {"enabled": False}}])
    host = FakeHost(["alpha"])
    _upgrade_patches(monkeypatch, host)
    original_act = host._act

    def slow_start(op: str, name: str) -> CommandResult:
        result = original_act(op, name)
        host.ready_at[name] = host.clock.now + 90.0  # beyond the old ~60 s retry budget
        return result

    host._act = slow_start
    monkeypatch.chdir(tmp_path)

    result = _upgrade(cfg)

    assert result.exit_code == 0, result.output
    assert host.calls == [("stop", "alpha"), ("daemon-reload", ""), ("restart", "alpha")]
    assert "Health check: readiness=ready after 90s" in result.output


def test_upgrade_failed_readiness_takes_the_rollback_path(tmp_path: Path, monkeypatch) -> None:
    cfg = _config(
        tmp_path,
        [
            {
                "name": "alpha",
                "backup_policy": {"enabled": False},
                "readiness": {"start_window_seconds": 30},
            }
        ],
    )
    host = FakeHost(["alpha"])
    host.never_ready.add("alpha")
    _upgrade_patches(monkeypatch, host)
    monkeypatch.chdir(tmp_path)

    result = _upgrade(cfg)

    assert result.exit_code == 1
    assert host.calls == [
        ("stop", "alpha"),
        ("daemon-reload", ""),
        ("restart", "alpha"),
        ("stop", "alpha"),
    ]
    assert "readiness failed" in result.output
    assert "start_window_exceeded" in result.output
    assert host.clock.now - 1000.0 <= 30.0 + 1e-9


def test_upgrade_degraded_warns_and_completes(tmp_path: Path, monkeypatch) -> None:
    pin = _pin(tmp_path, "1.2.3")
    cfg = _config(
        tmp_path, [{"name": "alpha", "backup_policy": {"enabled": False}, "plugins": [pin]}]
    )
    host = FakeHost(["alpha"])
    host.installed["alpha"]["example-cad"] = "1.2.2"
    _upgrade_patches(monkeypatch, host)
    monkeypatch.chdir(tmp_path)

    result = _upgrade(cfg)

    assert result.exit_code == 0, result.output
    assert "WARNING: alpha is degraded: plugin_version_mismatch" in result.output


def test_sync_reinstalls_rebuilt_archive_with_same_version(plugin_team, monkeypatch) -> None:
    cfg, host, _old, new = plugin_team
    # The running version already equals the new pin; only the ledger digest differs.
    host.installed["alpha"]["example-cad"] = "1.2.4"
    host.patch(monkeypatch)

    preview = _invoke("sync-plugins", "-c", str(cfg), "-m", "alpha")
    result = _invoke("sync-plugins", "-c", str(cfg), "-m", "alpha", "--execute")

    assert "   action: install (digest_changed)" in preview.output
    assert result.exit_code == 0, result.output
    assert host.installs() == ["alpha"]
    assert read_installed_plugins(_instance(cfg, "alpha"))["example-cad"].sha256 == new["sha256"]


def test_sync_reinstalls_when_ledger_matches_but_runtime_differs(plugin_team, monkeypatch) -> None:
    cfg, host, _old, _new = plugin_team
    host.installed["beta"]["example-cad"] = "1.2.2"  # changed outside Clawake
    host.patch(monkeypatch)

    result = _invoke("sync-plugins", "-c", str(cfg), "-m", "beta", "--execute")

    assert result.exit_code == 0, result.output
    assert host.installs() == ["beta"]
    assert host.calls == [("restart", "beta")]
