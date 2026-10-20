"""`clawake rollout` (ADR-003 item 5, ADR-004): one preview, then a verified per-member rollout.

Every host interaction is a fake: systemd, the /readyz GET, `podman exec`, backups and the
clock. Plugin archives are synthetic tgz files built in `tmp_path`. Nothing touches Podman,
systemd, OpenClaw or the network.
"""

from __future__ import annotations

import io
import json
import os
import re
import tarfile
from pathlib import Path

import pytest
import yaml

from clawake.config import load_inventory
from clawake.services import rollout as rollout_service
from clawake.services.gateway_config import (
    ensure_plugin_runtime_config,
    read_installed_plugins,
    record_installed_plugin,
)
from clawake.services.plugins import artifact_digest
from clawake.services.systemd import CommandResult
from test_member_ops import FakeHost, _archive, _deploy, _instance, _invoke

PLUGIN = "example-cad"
PLAN_TIME = 1000.0  # FakeClock start; the read-only plan probes before any wait
VOLUME = re.compile(r"^Volume=(?P<source>[^:]+):/opt/clawake/plugins/(?P<id>[a-z0-9-]+)\.tgz:ro$")


# --- fixtures ---------------------------------------------------------------------------


def _version_of(path: Path) -> str:
    with tarfile.open(path, "r:gz") as archive:
        handle = archive.extractfile("package/package.json")
        assert handle is not None
        return json.loads(handle.read())["version"]


class RolloutHost(FakeHost):
    """FakeHost whose container mounts what the deployed Quadlet file says at (re)start.

    Records one ordered event log across backups, deploys, systemd, readiness, installs and
    pin commits, so tests can assert the rollout order.
    """

    def __init__(self, members: list[str], quadlet_root: Path) -> None:
        super().__init__(members)
        self.quadlet_root = quadlet_root
        self.events: list[tuple[str, str]] = []
        self._waiting: set[str] = set()

    def sync_mounts(self, name: str) -> None:
        quadlet = self.quadlet_root / f"{name}.container"
        for line in quadlet.read_text(encoding="utf-8").splitlines():
            match = VOLUME.match(line)
            if match:
                source = Path(match["source"])
                plugin_id = match["id"]
                digest = artifact_digest(source)
                self.mounted_digests[(name, plugin_id)] = digest.removeprefix("sha256:")
                self.archive_versions[(name, plugin_id)] = _version_of(source)

    def _act(self, op: str, name: str) -> CommandResult:
        self.events.append((op, name))
        result = super()._act(op, name)
        if result.return_code == 0:
            self.sync_mounts(name)
            self._waiting.add(name)
        return result

    def _record_reload(self) -> CommandResult:
        self.events.append(("daemon-reload", ""))
        return super()._record_reload()

    def http_get(self, url: str, timeout: float):
        outcome = super().http_get(url, timeout)
        name = self._member_for_port(url)
        if outcome.status == 200 and name in self._waiting:
            self._waiting.discard(name)
            self.events.append(("ready", name))
        return outcome

    def subprocess_run(self, command: list[str], **kwargs: object):
        result = super().subprocess_run(command, **kwargs)
        if command[3:6] == ["openclaw", "plugins", "install"]:
            name = command[2].removesuffix("-ctr")
            self.events.append(("install" if result.returncode == 0 else "install-failed", name))
        return result

    def patch_rollout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.patch(monkeypatch)
        real_backup = rollout_service.backup_instance
        real_apply = rollout_service.apply_artifacts
        real_commit = rollout_service.set_plugin_pins

        def backup(instance, output_dir, execute=False):
            self.events.append(("backup", instance.name))
            return real_backup(instance, output_dir, execute=execute)

        def apply(changes):
            for member in dict.fromkeys(change.member for change in changes):
                self.events.append(("deploy", member))
            return real_apply(changes)

        def commit(inventory_path, member, pins, **kwargs):
            self.events.append(("commit", member))
            return real_commit(inventory_path, member, pins, **kwargs)

        monkeypatch.setattr(rollout_service, "backup_instance", backup)
        monkeypatch.setattr(rollout_service, "apply_artifacts", apply)
        monkeypatch.setattr(rollout_service, "set_plugin_pins", commit)


def _team(tmp_path: Path, members: list[str], *, runtime_pins: bool = True) -> Path:
    """Inventory with plugin declarations only; the archive pins live in the overlay."""
    (tmp_path / "shared").mkdir(exist_ok=True)
    instances = []
    for index, name in enumerate(members):
        team = tmp_path / name / "team.yml"
        team.parent.mkdir(parents=True, exist_ok=True)
        team.write_text("team: sample\n", encoding="utf-8")
        (tmp_path / name / "workspace").mkdir(exist_ok=True)
        (tmp_path / name / "workspace" / "notes.md").write_text("synthetic\n", encoding="utf-8")
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
                "plugins": [{"id": PLUGIN}],
            }
        )
    data: dict = {
        "version": 1,
        "cluster": {"name": "c", "mode": "single_host", "primary_host": "h"},
        "hosts": [{"name": "h", "quadlet_root": str(tmp_path / "quadlets")}],
        "instances": instances,
    }
    if runtime_pins:
        data["runtime_pins"] = "pins/pins.json"
    path = tmp_path / "inventory.yml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _write_pins(cfg: Path, pins: dict[str, Path]) -> Path:
    directory = cfg.parent / "pins"
    directory.mkdir(mode=0o700, exist_ok=True)
    overlay = {
        "version": 1,
        "cluster": "c",
        "members": {
            name: {
                "plugins": {PLUGIN: {"artifact_path": str(path), "sha256": artifact_digest(path)}}
            }
            for name, path in pins.items()
        },
    }
    path = directory / "pins.json"
    path.write_text(json.dumps(overlay, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


class Team:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, members: list[str]):
        self.root = tmp_path
        self.old = _archive(tmp_path, PLUGIN, "1.2.3")
        self.new = _archive(tmp_path, PLUGIN, "1.2.4")
        self.cfg = _team(tmp_path, members)
        self.pins = _write_pins(self.cfg, dict.fromkeys(members, self.old))
        _deploy(self.cfg)
        self.host = RolloutHost(members, tmp_path / "quadlets")
        for name in members:
            instance = _instance(self.cfg, name)
            record_installed_plugin(instance, PLUGIN, artifact_digest(self.old), "1.2.3")
            ensure_plugin_runtime_config(instance)
            self.host.installed[name][PLUGIN] = "1.2.3"
            self.host.sync_mounts(name)
        self.host.patch_rollout(monkeypatch)
        monkeypatch.chdir(tmp_path)

    def rollout(self, *args: str):
        return _invoke("rollout", "-c", str(self.cfg), *args)

    def plugin_arg(self, path: Path | None = None) -> list[str]:
        return ["--plugin", f"{PLUGIN}={path or self.new}"]

    def snapshot(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in sorted(self.root.rglob("*"))
            if path.is_file()
        }

    def pins_backups(self) -> list[Path]:
        directory = self.pins.parent / "pins-backups"
        return sorted(directory.iterdir()) if directory.exists() else []

    def overlay_pin(self, member: str) -> dict:
        return json.loads(self.pins.read_text())["members"][member]["plugins"][PLUGIN]


@pytest.fixture
def team(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Team:
    return Team(tmp_path, monkeypatch, ["alpha", "beta"])


def _summary(output: str) -> dict[str, list[str]]:
    lines = output.splitlines()
    start = lines.index("Rollout summary:") + 2
    rows = {}
    for line in lines[start:]:
        if not line.startswith("  "):
            break
        cells = line.split()
        rows[cells[0]] = cells
    return rows


def _header(output: str) -> list[str]:
    lines = output.splitlines()
    return lines[lines.index("Rollout summary:") + 1].split()


# --- preview ----------------------------------------------------------------------------


def test_preview_shows_old_to_new_and_mutates_nothing(team: Team) -> None:
    before = team.snapshot()

    result = team.rollout("-m", "alpha", *team.plugin_arg())

    assert result.exit_code == 0, result.output
    out = result.output
    assert "Member alpha:" in out
    assert "Member beta:" not in out
    assert "  readiness now: ready" in out
    assert "  setup: restart (alpha.container)" in out
    old_prefix = artifact_digest(team.old)[:19]
    new_prefix = artifact_digest(team.new)[:19]
    assert f"  plugin {PLUGIN}: 1.2.3 ({old_prefix}) -> 1.2.4 ({new_prefix})" in out
    assert "    action: install (digest_changed, version_mismatch)" in out
    assert "  grants: managed plugin config and agent grants unchanged" in out
    assert "  backup: .backups/alpha-<UTC timestamp>.tar.gz" in out
    assert "Overlay backup: .backups/pins-<UTC timestamp>.json" in out
    assert out.rstrip().endswith(
        "Members to restart: alpha\nDRY RUN rollout complete. Re-run with --execute."
    )
    # Nothing written: no files, no overlay, no ledger, no backups; no systemd or installs.
    assert team.snapshot() == before
    assert not (team.root / ".backups").exists()
    assert team.host.calls == []
    assert team.host.installs() == []
    assert team.host.events == []


def test_preview_is_deterministic(team: Team) -> None:
    first = team.rollout(*team.plugin_arg())
    second = team.rollout(*team.plugin_arg())

    assert first.exit_code == 0, first.output
    assert first.output == second.output


# --- execute: happy path ----------------------------------------------------------------


def test_execute_single_member_order_and_other_member_untouched(team: Team) -> None:
    beta_quadlet = (team.root / "quadlets" / "beta.container").read_bytes()
    beta_ledger = read_installed_plugins(_instance(team.cfg, "beta"))

    result = team.rollout("-m", "alpha", *team.plugin_arg(), "--execute")

    assert result.exit_code == 0, result.output
    assert team.host.events == [
        ("backup", "alpha"),
        ("deploy", "alpha"),
        ("daemon-reload", ""),
        ("restart", "alpha"),
        ("ready", "alpha"),
        ("install", "alpha"),
        ("restart", "alpha"),
        ("ready", "alpha"),
        ("commit", "alpha"),
    ]
    # Verified: the probe after the last restart saw the new version before the commit.
    assert team.host.installed["alpha"][PLUGIN] == "1.2.4"
    assert team.overlay_pin("alpha") == {
        "artifact_path": str(team.new),
        "sha256": artifact_digest(team.new),
    }
    assert read_installed_plugins(_instance(team.cfg, "alpha"))[PLUGIN].version == "1.2.4"
    # beta: no systemd call, no probe, Quadlet, ledger and pin unchanged.
    assert all(name != "beta" for _op, name in team.host.calls)
    assert all(name != "beta" for name, _ in team.host.http)
    assert (team.root / "quadlets" / "beta.container").read_bytes() == beta_quadlet
    assert read_installed_plugins(_instance(team.cfg, "beta")) == beta_ledger
    assert team.overlay_pin("beta")["sha256"] == artifact_digest(team.old)
    assert _header(result.output) == [
        "member",
        "setup",
        "plugin",
        "action",
        "version",
        "digest",
        "result",
        "readiness",
        "ready_s",
        "pins",
    ]
    row = _summary(result.output)["alpha"]
    assert row[:7] == ["alpha", "restart", PLUGIN, "install", "1.2.3->1.2.4", row[5], "ok"]
    assert row[5] == artifact_digest(team.new)[:19]
    assert row[7:] == ["ready", "46", "yes"]
    assert "Rollout complete." in result.output


def test_execute_two_members_runs_one_member_after_the_other(team: Team) -> None:
    result = team.rollout(*team.plugin_arg(), "--execute")

    assert result.exit_code == 0, result.output
    names = [name for op, name in team.host.events if op != "daemon-reload"]
    first_beta = names.index("beta")
    assert set(names[:first_beta]) == {"alpha"}
    # Backups for all members come first; then alpha completes (incl. commit) before beta.
    assert team.host.events[:2] == [("backup", "alpha"), ("backup", "beta")]
    alpha_commit = team.host.events.index(("commit", "alpha"))
    beta_deploy = team.host.events.index(("deploy", "beta"))
    assert alpha_commit < beta_deploy
    assert team.host.events[-1] == ("commit", "beta")
    for name in ("alpha", "beta"):
        assert team.overlay_pin(name)["sha256"] == artifact_digest(team.new)
        assert _summary(result.output)[name][-1] == "yes"


# --- execute: stop at the first failure -------------------------------------------------


def test_readiness_failure_after_restart_stops_without_commit(team: Team) -> None:
    team.host.crash_after_act.add("alpha")
    overlay_before = team.pins.read_bytes()
    beta_quadlet = (team.root / "quadlets" / "beta.container").read_bytes()

    result = team.rollout(*team.plugin_arg(), "--execute")

    assert result.exit_code == 1, result.output
    assert ("commit", "alpha") not in team.host.events
    assert team.pins.read_bytes() == overlay_before
    assert team.pins_backups() == []
    # beta: backed up up front, but never deployed, restarted or probed after the plan.
    assert [event for event in team.host.events if event[1] == "beta"] == [("backup", "beta")]
    assert all(at == PLAN_TIME for name, at in team.host.http if name == "beta")
    assert (team.root / "quadlets" / "beta.container").read_bytes() == beta_quadlet
    assert team.host.installs() == []
    out = result.output
    assert "The deployed state of alpha now differs from the committed runtime pins." in out
    assert re.search(r"Backups: .*\.backups/alpha-\d{8}T\d{6}\.\d{6}Z\.tar\.gz", out)
    assert f"clawake rollout -c {team.cfg} -m alpha --execute" in out
    rows = _summary(out)
    assert rows["alpha"][6:8] == ["failed", "failed"]
    assert rows["alpha"][-1] == "no"
    assert rows["beta"][6] == "not_run"
    assert rows["beta"][-1] == "no"


def test_install_failure_stops_without_commit_or_ledger_entry(team: Team) -> None:
    team.host.fail_install.add("alpha")
    overlay_before = team.pins.read_bytes()

    result = team.rollout(*team.plugin_arg(), "--execute")

    assert result.exit_code == 1, result.output
    assert ("install-failed", "alpha") in team.host.events
    assert ("commit", "alpha") not in team.host.events
    assert team.pins.read_bytes() == overlay_before
    ledger = read_installed_plugins(_instance(team.cfg, "alpha"))
    assert ledger[PLUGIN].sha256 == artifact_digest(team.old)
    assert [event for event in team.host.events if event[1] == "beta"] == [("backup", "beta")]
    assert _summary(result.output)["alpha"][3] == "failed"


def test_backup_failure_aborts_before_any_deploy(team: Team, monkeypatch) -> None:
    def broken_backup(instance, output_dir, execute=False):
        team.host.events.append(("backup", instance.name))
        raise OSError("No space left on device")

    monkeypatch.setattr(rollout_service, "backup_instance", broken_backup)
    before = team.snapshot()

    result = team.rollout(*team.plugin_arg(), "--execute")

    assert result.exit_code == 1, result.output
    assert team.host.events == [("backup", "alpha")]
    assert team.host.calls == []
    assert "Backup failed for alpha" in result.output
    assert team.snapshot() == before
    rows = _summary(result.output)
    assert rows["alpha"][6] == "not_run"
    assert rows["beta"][6] == "not_run"


# --- usage errors: exit 2 before anything else ------------------------------------------


def _bad_archive(tmp_path: Path) -> Path:
    path = tmp_path / "artifacts" / "no-version.tgz"
    payload = json.dumps({"name": PLUGIN}).encode()
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo("package/package.json")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return path


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("undeclared", "is not declared on any selected member"),
        ("missing_file", "does not exist"),
        ("unreadable_version", "cannot read the version"),
        ("malformed", "expects <id>=<path-to-tgz>"),
        ("unknown_member", "Unknown member 'gamma'"),
    ],
)
def test_invalid_requests_exit_2_before_touching_the_host(
    team: Team, case: str, message: str
) -> None:
    args = {
        "undeclared": ["--plugin", f"other-plugin={team.new}"],
        "missing_file": ["--plugin", f"{PLUGIN}={team.root / 'missing.tgz'}"],
        "unreadable_version": ["--plugin", f"{PLUGIN}={_bad_archive(team.root)}"],
        "malformed": ["--plugin", str(team.new)],
        "unknown_member": ["-m", "gamma", *team.plugin_arg()],
    }[case]
    before = team.snapshot()

    result = team.rollout(*args, "--execute")

    assert result.exit_code == 2, result.output
    assert message in result.output
    assert team.host.events == []
    assert team.host.http == []
    assert team.snapshot() == before


def test_npm_plugin_cannot_be_rolled_out_from_an_archive(tmp_path: Path, monkeypatch) -> None:
    cfg = _team(tmp_path, ["alpha"])
    data = yaml.safe_load(cfg.read_text())
    data["instances"][0]["plugins"] = [
        {"id": PLUGIN, "npm_spec": "example-cad@1.2.3", "integrity": "sha512-AAAA"}
    ]
    cfg.write_text(yaml.safe_dump(data, sort_keys=False))
    (tmp_path / "pins").mkdir(mode=0o700)
    (tmp_path / "pins" / "pins.json").write_text('{"version": 1, "cluster": "c", "members": {}}')
    (tmp_path / "pins" / "pins.json").chmod(0o600)
    archive = _archive(tmp_path, PLUGIN, "1.2.4")
    host = FakeHost(["alpha"])
    host.patch(monkeypatch)

    result = _invoke("rollout", "-c", str(cfg), "--plugin", f"{PLUGIN}={archive}")

    assert result.exit_code == 2, result.output
    assert "npm plugin" in result.output
    assert host.http == []


def test_plugin_requires_runtime_pins(tmp_path: Path, monkeypatch) -> None:
    cfg = _team(tmp_path, ["alpha"], runtime_pins=False)
    data = yaml.safe_load(cfg.read_text())
    old = _archive(tmp_path, PLUGIN, "1.2.3")
    data["instances"][0]["plugins"] = [
        {"id": PLUGIN, "artifact_path": str(old), "sha256": artifact_digest(old)}
    ]
    cfg.write_text(yaml.safe_dump(data, sort_keys=False))
    new = _archive(tmp_path, PLUGIN, "1.2.4")
    host = FakeHost(["alpha"])
    host.patch(monkeypatch)
    before = cfg.read_bytes()

    result = _invoke("rollout", "-c", str(cfg), "--plugin", f"{PLUGIN}={new}", "--execute")

    assert result.exit_code == 2, result.output
    assert "clawake pins init" in result.output
    assert host.http == []
    assert host.calls == []
    assert cfg.read_bytes() == before


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read mode-000 files")
def test_unreadable_archive_exits_2(team: Team) -> None:
    team.new.chmod(0o000)
    try:
        result = team.rollout(*team.plugin_arg())
    finally:
        team.new.chmod(0o644)

    assert result.exit_code == 2, result.output
    assert "is not readable" in result.output


def test_v2_inventory_is_rejected(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "services-v2.yml"
    if not fixture.exists():
        pytest.skip("no v2 fixture")
    result = _invoke("rollout", "-c", str(fixture))

    assert result.exit_code == 2, result.output
    assert "inventory version 1" in result.output


# --- idempotence ------------------------------------------------------------------------


def test_rollout_without_plugin_on_unchanged_team_is_a_no_op(team: Team) -> None:
    before = team.snapshot()

    preview = team.rollout()
    result = team.rollout("--execute")

    assert preview.exit_code == 0, preview.output
    assert "Members to restart: none" in preview.output
    assert result.exit_code == 0, result.output
    assert team.host.calls == []
    assert team.host.installs() == []
    assert not (team.root / ".backups").exists()
    assert team.snapshot() == before
    for name in ("alpha", "beta"):
        row = _summary(result.output)[name]
        assert row[1:4] == ["none", PLUGIN, "skip"]
        assert row[-1] == "-"


def test_second_run_with_same_plugin_is_idempotent(team: Team) -> None:
    first = team.rollout("-m", "alpha", *team.plugin_arg(), "--execute")
    assert first.exit_code == 0, first.output
    backups = team.pins_backups()
    overlay = team.pins.read_bytes()
    archives = sorted((team.root / ".backups").iterdir())
    calls = list(team.host.calls)

    preview = team.rollout("-m", "alpha", *team.plugin_arg())
    second = team.rollout("-m", "alpha", *team.plugin_arg(), "--execute")

    assert preview.exit_code == 0, preview.output
    assert "    action: skip" in preview.output
    assert "Members to restart: none" in preview.output
    assert second.exit_code == 0, second.output
    assert team.host.calls == calls
    assert team.pins.read_bytes() == overlay
    assert team.pins_backups() == backups
    assert sorted((team.root / ".backups").iterdir()) == archives
    row = _summary(second.output)["alpha"]
    assert row[1:5] == ["none", PLUGIN, "skip", "1.2.4"]
    assert row[-1] == "-"


def test_golden_rendering_unchanged_by_candidate_state(team: Team) -> None:
    """The candidate renders exactly like an inventory that carries the pin itself."""
    result = team.rollout("-m", "alpha", *team.plugin_arg(), "--execute")
    assert result.exit_code == 0, result.output

    quadlet = (team.root / "quadlets" / "alpha.container").read_text()
    assert f"Volume={team.new}:/opt/clawake/plugins/{PLUGIN}.tgz:ro" in quadlet
    reloaded = load_inventory(team.cfg)
    plugin = next(item for item in reloaded.instances if item.name == "alpha").plugins[0]
    assert plugin.artifact_path == str(team.new)
