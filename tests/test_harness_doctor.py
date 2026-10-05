"""Exercise diagnosis without touching the real host or credentials."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / ".codex/skills/clawake-harness/scripts/doctor.py"
spec = importlib.util.spec_from_file_location("harness_doctor", SCRIPT)
doctor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doctor)


@pytest.mark.parametrize("name", ["podman", "uv", "gh"])
def test_missing_command(monkeypatch, capsys, name):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    check = doctor.Doctor()
    assert not check.command(name)
    assert f"BLOCKED {name}" in capsys.readouterr().out


def test_manager_unavailable(monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("private host error")

    monkeypatch.setattr(doctor.subprocess, "run", unavailable)
    assert doctor.Doctor().run(["systemctl", "--user", "list-units"]) == (1, "")


@pytest.mark.parametrize(
    "name,source,expected",
    [
        ("clawcad-mechanical-engineer", "", "UNKNOWN"),
        ("database", "", "UNRELATED"),
        ("clawcad-lead", "/tmp/old-clawcad/workspace", "UNKNOWN"),
        ("clawcad-lead", "/tmp/current/workspace", "CURRENT_WORKSPACE"),
    ],
)
def test_ownership_requires_evidence(name, source, expected):
    assert doctor.classify(name, source, Path("/tmp/current")) == expected


def test_env_missing(tmp_path):
    assert doctor.env_presence(tmp_path / "missing", ["TOKEN"]) == "NOT_CONFIGURED"


@pytest.mark.parametrize(
    "content,mode,expected",
    [
        ("TOKEN=\n", 0o600, "NOT_CONFIGURED"),
        ("TOKEN=''\n", 0o600, "NOT_CONFIGURED"),
        ("OTHER=fake\n", 0o600, "NOT_CONFIGURED"),
        ("TOKEN=fake-test-only\n", 0o600, "PASS"),
        ("TOKEN=fake-test-only\n", 0o644, "BLOCKED"),
    ],
)
def test_token_presence_never_prints_values(tmp_path, capsys, content, mode, expected):
    path = tmp_path / "test.env"
    path.write_text(content)
    path.chmod(mode)
    assert doctor.env_presence(path, ["TOKEN"]) == expected
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "collision,valid,preview_ok",
    [
        (False, True, True),
        (True, True, True),
        (False, False, False),
    ],
)
def test_inventory_and_preview_without_host_mutations(tmp_path, collision, valid, preview_ok):
    from clawake.config import load_inventory
    from clawake.services.deployment import plan_deployment

    root = Path(__file__).parents[1]
    data = json.loads(
        json.dumps(__import__("yaml").safe_load((root / "robotics-team/team.yml").read_text()))
    )
    data["hosts"][0]["quadlet_root"] = str(tmp_path / "quadlets")
    for index, member in enumerate(data["instances"]):
        member["workspace_path"] = str(tmp_path / f"workspace{index}")
        member["team_definition_path"] = str(tmp_path / f"role{index}")
    if collision:
        data["instances"][1]["ports"] = data["instances"][0]["ports"]
    if not valid:
        data["instances"][0]["host"] = "absent"
    config = tmp_path / "team.yml"
    config.write_text(json.dumps(data))
    if collision or not valid:
        with pytest.raises(ValueError):
            load_inventory(config)
    else:
        inventory = load_inventory(config)
        plan = plan_deployment(inventory, inventory.instances, root / "templates")
        assert plan and preview_ok
    assert not (tmp_path / "quadlets").exists()
    assert not (tmp_path / "workspace0").exists()


def test_network_wait_opt_out_is_scoped_and_generator_accepts_it(tmp_path):
    import os
    import subprocess

    import yaml

    from clawake.config import load_inventory
    from clawake.services.render import render_inventory

    root = Path(__file__).parents[1]
    data = yaml.safe_load((root / "robotics-team/team.yml").read_text())
    for member in data["instances"]:
        member["quadlet_default_dependencies"] = False
    for network in data["networks"]:
        network["quadlet_default_dependencies"] = False
    path = tmp_path / "inventory.yml"
    path.write_text(yaml.safe_dump(data))
    inventory = load_inventory(path)
    output = tmp_path / "rendered"
    render_inventory(inventory, output, root / "templates")
    generator = Path("/usr/lib/podman/quadlet")
    if not generator.exists():
        pytest.skip("Quadlet unavailable")
    result = subprocess.run(
        [str(generator), "-user", "-dryrun"],
        env={**os.environ, "QUADLET_UNIT_DIRS": str(output)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "unsupported key" not in result.stderr.lower()
    assert "podman-user-wait-network-online.service" not in result.stdout


@pytest.mark.parametrize(
    "unit,ports",
    [
        ("clawcad-mechanical-engineer.service loaded active running", ""),
        ("database.service loaded active running", "127.0.0.1:19389 "),
    ],
)
def test_old_runtime_and_port_collision_are_reported(monkeypatch, tmp_path, capsys, unit, ports):
    import sys

    def run(self, args, cwd=None):
        if args[0] == "git":
            return 0, f"git@github.com:edge-robot/{Path(cwd).name}.git"
        if args[0] == "systemctl":
            return 0, unit
        if args[0] == "ss":
            return 0, ports or "127.0.0.1:631 "
        return 1, ""

    monkeypatch.setattr(doctor.Doctor, "run", run)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(sys, "argv", ["doctor", "--inventory", str(tmp_path / "absent")])
    doctor.main()
    output = capsys.readouterr().out
    assert "RESULT BLOCKED" in output
    if "clawcad" in unit:
        assert "UNKNOWN unit clawcad-mechanical-engineer.service" in output
    else:
        assert "port 19389: UNKNOWN owner" in output


@pytest.mark.parametrize(
    "mode,success",
    [
        ("complete", True),
        ("changed_id", False),
        ("failed", False),
        ("wrong_marker", False),
    ],
)
def test_smoke_requires_original_final_completion(mode, success):
    import shutil
    import subprocess

    if not shutil.which("node"):
        pytest.skip("Node unavailable for isolated protocol-client test")
    client = SCRIPT.with_name("a2a-smoke.mjs").read_text()
    fixture = """
process.env.A2A_LEAD_TO_MECHANICAL = 'fake-test-only';
let request;
globalThis.setTimeout = fn => { fn(); return 0; };
globalThis.fetch = async (url, options) => {
  const body = JSON.parse(options.body);
  if (body.method === 'SendMessage') {
    request = body.params.message.messageId;
    if (!body.params.message.parts[0].text.includes('Do not call any tools')) throw Error('unsafe');
    return {ok:true,json:async()=>({result:{id:'original',status:{state:'TASK_STATE_WORKING'}}})};
  }
  if (body.method !== 'GetTask' || body.params.id !== 'original') throw Error('wrong polling');
  return {ok:true,json:async()=>({result:{
    id:MODE==='changed_id'?'other':'original',
    status:{state:MODE==='failed'?'TASK_STATE_FAILED':'TASK_STATE_COMPLETED'},
    artifacts:[{parts:[{text:`task_id=${MODE==='wrong_marker'?'bad':request} HARNESS_OK`}]}]
  }})};
};
""".replace("MODE", json.dumps(mode))
    result = subprocess.run(
        ["node", "--input-type=module"],
        input=fixture + client,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert (result.returncode == 0) == success
    if success:
        assert json.loads(result.stdout)["task_id"] == "original"
    assert "fake-test-only" not in result.stdout + result.stderr
