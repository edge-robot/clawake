"""Read-only harness checks; output never includes command stderr or secret values."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path


class Doctor:
    def __init__(self):
        self.blocked = False
        self.runtime_blocked = False

    def report(self, state, label):
        print(f"{state} {label}")
        self.blocked |= state == "BLOCKED"

    def run(self, args, cwd=None):
        try:
            result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=30)
            return result.returncode, result.stdout
        except (OSError, subprocess.TimeoutExpired):
            return 1, ""

    def command(self, name, required=True):
        found = bool(shutil.which(name))
        self.report("PASS" if found else "BLOCKED" if required else "WARNING", name)
        return found


def classify(name, source, workspace):
    """Names alone cannot establish old/current ownership."""
    if source and Path(source).resolve().is_relative_to(workspace.resolve()):
        return "CURRENT_WORKSPACE"
    if re.search(r"clawcad|clawake|openclaw|robotics", name, re.I):
        return "UNKNOWN"
    return "UNRELATED"


def env_presence(path, required):
    # Values remain local and are never returned, logged or interpolated.
    if not path.is_file():
        return "NOT_CONFIGURED"
    if path.stat().st_mode & 0o077:
        return "BLOCKED"
    keys = set()
    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeError):
        return "BLOCKED"
    for line in lines:
        match = re.fullmatch(r"\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*", line)
        if match and match[2].strip("\"'") and not match[2].startswith("#"):
            keys.add(match[1])
    return "PASS" if set(required) <= keys else "NOT_CONFIGURED"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--clawcad", type=Path)
    parser.add_argument("--inventory", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[4]
    cad = args.clawcad or root.parent / "clawcad"
    d = Doctor()
    release = Path("/etc/os-release").read_text()
    d.report(
        "PASS" if re.search(r"^ID=[\"\']?omarchy[\"\']?$", release, re.M) else "WARNING",
        "Omarchy detected",
    )
    d.report("PASS", f"architecture {platform.machine()}; kernel {platform.release()}")
    d.command("pacman")
    d.report("PASS" if shutil.which("yay") or shutil.which("paru") else "WARNING", "AUR helper")
    d.report(
        "PASS" if tuple(map(int, platform.python_version_tuple()[:2])) >= (3, 11) else "BLOCKED",
        f"Python {platform.python_version()}",
    )
    uv = d.command("uv")
    if d.command("gh"):
        code, _ = d.run(["gh", "auth", "status"])
        d.report("PASS" if code == 0 else "NOT_CONFIGURED", "GitHub authentication")
    for label, path in (("Clawake", root), ("ClawCAD", cad)):
        code, remote = d.run(["git", "remote", "get-url", "origin"], path)
        d.report(
            "PASS" if code == 0 and f"edge-robot/{label.lower()}" in remote else "BLOCKED",
            f"{label} repository",
        )
    inventory = args.inventory or cad / "state/demo/team.yml"
    expected_artifacts = {}
    expected_members = {}
    if uv and (root / ".venv").is_dir() and inventory.is_file():
        probe = (
            "import json,sys; from pathlib import Path; "
            "from clawake.config import load_inventory; "
            "from clawake.services.render import render_instance_assets,render_shared_network; "
            "i=load_inventory(sys.argv[1]); "
            "a={n.quadlet_path:render_shared_network(n) for n in i.networks}; "
            "[a.update(render_instance_assets(m,Path('templates'))) for m in i.instances]; "
            "print(json.dumps({'artifacts':a,'members':{m.container_name:"
            "{'workspace':m.workspace_path,'ports':[p.host_port for p in m.ports]} "
            "for m in i.instances}}))"
        )
        rc, raw = d.run(
            ["uv", "run", "--no-sync", "--offline", "python", "-c", probe, str(inventory)], root
        )
        try:
            desired = json.loads(raw) if rc == 0 else {}
            expected_artifacts = desired.get("artifacts", {})
            expected_members = desired.get("members", {})
        except ValueError:
            pass
    quadlets = Path.home() / ".config/containers/systemd"
    current_artifacts = set()
    for name, content in expected_artifacts.items():
        path = quadlets / name
        if path.is_file() and path.read_text() == content:
            current_artifacts.add(name)
    current_units = set()
    for name in current_artifacts:
        suffix = (
            "-network.service"
            if name.endswith(".network")
            else "-volume.service"
            if name.endswith(".volume")
            else ".service"
        )
        current_units.add(name.rsplit(".", 1)[0] + suffix)
    code, units = d.run(["systemctl", "--user", "list-units", "--all", "--no-pager", "--plain"])
    d.report("PASS" if code == 0 else "BLOCKED", "systemd user manager")
    d.runtime_blocked |= code != 0
    for line in units.splitlines():
        if re.search(r"clawcad|clawake|openclaw|robotics", line, re.I):
            name = line.split()[0]
            current = name in current_units
            d.report(
                "PASS" if current else "WARNING",
                f"{'CURRENT_WORKSPACE' if current else 'UNKNOWN'} unit {name}",
            )
            d.runtime_blocked |= not current
    quadlets = Path.home() / ".config/containers/systemd"
    for path in sorted(quadlets.glob("**/*")):
        if path.is_file():
            state = "CURRENT_WORKSPACE" if path.name in current_artifacts else "UNKNOWN"
            d.report(
                "PASS" if state == "CURRENT_WORKSPACE" else "WARNING",
                f"{state} Quadlet {path.name}",
            )
            d.runtime_blocked |= state != "CURRENT_WORKSPACE"
    generator = any(
        Path(p).exists()
        for p in (
            "/usr/lib/systemd/user-generators/podman-user-generator",
            "/usr/lib/podman/quadlet",
        )
    )
    d.report(
        "PASS" if generator else "BLOCKED", "Quadlet generator available (functional test separate)"
    )
    current_containers = set()
    current_images = set()
    if d.command("podman"):
        code, output = d.run(["podman", "info", "--format", "json"])
        try:
            info = json.loads(output)
            ready = code == 0 and info["host"]["security"]["rootless"] is True
            storage = bool(info.get("store", {}).get("graphRoot"))
            networking = bool(info.get("host", {}).get("networkBackend"))
        except (ValueError, KeyError, TypeError):
            ready = storage = networking = False
        d.report("PASS" if ready else "BLOCKED", "rootless Podman")
        d.report("PASS" if storage else "BLOCKED", "Podman storage")
        d.report("PASS" if networking else "BLOCKED", "Podman networking")
        for kind, command in (
            ("container", ["ps", "-a"]),
            ("image", ["images"]),
            ("network", ["network", "ls"]),
            ("volume", ["volume", "ls"]),
        ):
            code, output = d.run(["podman", *command, "--format", "json"])
            try:
                resources = json.loads(output) if code == 0 else None
            except ValueError:
                resources = None
            if resources is None:
                d.report("BLOCKED", f"Podman {kind} inspection")
            else:
                for resource in resources:
                    names = resource.get("Names", resource.get("names", []))
                    name = (
                        names[0]
                        if isinstance(names, list) and names
                        else names
                        if isinstance(names, str)
                        else resource.get("name", resource.get("Name", "unnamed"))
                    )
                    current = False
                    unrelated = kind == "network" and name == "podman"
                    if kind == "container" and name in expected_members:
                        rc, mounts = d.run(
                            ["podman", "inspect", "--format", "{{json .Mounts}}", name]
                        )
                        try:
                            current = rc == 0 and any(
                                m.get("Source") == expected_members[name]["workspace"]
                                and m.get("Destination") == "/workspace"
                                for m in json.loads(mounts)
                            )
                        except (ValueError, TypeError):
                            pass
                        if current:
                            current_containers.add(name)
                            rc, image_id = d.run(
                                ["podman", "inspect", "--format", "{{.Image}}", name]
                            )
                            if rc == 0:
                                current_images.add(image_id.strip())
                    elif kind == "network":
                        current = name + ".network" in current_artifacts
                    elif kind == "image":
                        image_id = resource.get("Id", resource.get("ID", ""))
                        current = image_id in current_images
                    elif kind == "volume":
                        current = name + ".volume" in current_artifacts
                    state = (
                        "CURRENT_WORKSPACE" if current else "UNRELATED" if unrelated else "UNKNOWN"
                    )
                    d.report(
                        "PASS" if current or unrelated else "WARNING", f"{state} {kind} {name}"
                    )
                    d.runtime_blocked |= not current and not unrelated
    else:
        d.runtime_blocked = True
    code, listeners = d.run(["ss", "-H", "-ltn"])
    if code == 0 and not listeners.strip():
        # An empty result in a desktop session can indicate a sandbox netlink denial.
        code = 1
    d.report("PASS" if code == 0 else "BLOCKED", "TCP port inspection")
    for port in (19389, 19489):
        occupied = bool(re.search(rf":{port}\s", listeners))
        owned = False
        for name in current_containers:
            if port in expected_members[name]["ports"]:
                rc, bindings = d.run(["podman", "port", name])
                owned |= rc == 0 and f"127.0.0.1:{port}" in bindings
        label = "CURRENT_WORKSPACE" if owned else "UNKNOWN owner" if occupied else "free"
        d.report(
            "PASS" if owned or not occupied and code == 0 else "WARNING" if occupied else "BLOCKED",
            f"port {port}: {label}",
        )
        d.runtime_blocked |= occupied and not owned or code != 0
    inventory = args.inventory or cad / "state/demo/team.yml"
    if not inventory.is_file():
        d.report("NOT_CONFIGURED", "ClawCAD inventory")
        d.runtime_blocked = True
    elif uv and (root / ".venv").is_dir():
        # Load via the repository's own model, suppress values and validation errors.
        code, _ = d.run(
            ["uv", "run", "--no-sync", "--offline", "clawake", "validate", "-c", str(inventory)],
            root,
        )
        d.report("PASS" if code == 0 else "BLOCKED", "CLI / inventory validation")
        try:
            # Existing demo preparation emits JSON (valid YAML).
            data = json.loads(inventory.read_text())
            instances = data["instances"]
            names = {i["name"] for i in instances}
            expected = {"clawcad-robotics-lead", "clawcad-mechanical-engineer"}
            d.report(
                "PASS" if names == expected and len(instances) == 2 else "BLOCKED",
                "two-agent topology",
            )
            for instance in instances:
                peers = instance.get("openclaw", {}).get("a2a", {}).get("peers", {})
                required = ["OPENCLAW_GATEWAY_TOKEN"]
                for peer in peers.values():
                    required += [peer["inbound_token_env"], peer["outbound_token_env"]]
                paths = [
                    Path(os.path.expandvars(p)).expanduser() for p in instance.get("env_files", [])
                ]
                state = env_presence(paths[0], required) if len(paths) == 1 else "NOT_CONFIGURED"
                d.report(state, f"{instance['name']} required env/token presence")
                d.runtime_blocked |= state != "PASS"
            by_name = {i["name"]: i for i in instances}
            topology_ok = True
            for instance in instances:
                settings = instance.get("openclaw", {}).get("a2a", {})
                peers = settings.get("peers", {})
                topology_ok &= settings.get("enabled", False) and len(peers) == 1
                for peer in peers.values():
                    target = by_name.get(peer["instance"], {})
                    reverse = next(
                        (
                            p
                            for p in target.get("openclaw", {})
                            .get("a2a", {})
                            .get("peers", {})
                            .values()
                            if p["instance"] == instance["name"]
                        ),
                        {},
                    )
                    topology_ok &= (
                        reverse.get("inbound_token_env") == peer["outbound_token_env"]
                        and reverse.get("outbound_token_env") == peer["inbound_token_env"]
                        and bool(
                            set(instance.get("networks", [])) & set(target.get("networks", []))
                        )
                    )
                config = Path(instance["workspace_path"]) / ".openclaw/openclaw.json"
                payload = json.loads(config.read_text())
                tools = payload.get("tools", {})
                allow = set(tools.get("allow", []))
                is_lead = instance["name"] == "clawcad-robotics-lead"
                approved = (
                    {"message", "clawcad_delegate_render"}
                    if is_lead
                    else {"clawcad_render_current"}
                )
                policy_ok = bool(allow) and allow <= approved
                entries = payload.get("agents", {}).get("entries", {})
                policy_ok &= set(entries) == {instance["name"]}
                for entry in entries.values():
                    grants = entry.get("tools", {})
                    policy_ok &= (
                        set(grants.get("allow", []) + grants.get("alsoAllow", [])) <= approved
                    )
                d.report(
                    "PASS" if policy_ok else "BLOCKED",
                    f"{instance['name']} configured tool policy (effective tools separate)",
                )
                provider_files = list(
                    config.parent.glob("agents/*/agent/auth-profiles.json")
                ) + list(config.parent.glob("agents/*/agent/auth.json"))
                model = payload.get("agents", {}).get("defaults", {}).get("model")
                d.report(
                    "PASS" if provider_files else "WARNING" if model else "NOT_CONFIGURED",
                    f"{instance['name']} model config present; auth checked by runtime smoke"
                    if model
                    else f"{instance['name']} model not configured; use clawake onboard",
                )
            d.report("PASS" if topology_ok else "BLOCKED", "reciprocal authenticated A2A topology")
        except (ValueError, KeyError, OSError, TypeError):
            d.report(
                "WARNING",
                "inventory semantic diagnosis unavailable; inspect validated YAML privately",
            )
            d.runtime_blocked = True
    else:
        d.report("NOT_CONFIGURED", "Clawake development environment; run make install-dev")
        d.runtime_blocked = True
    d.report(
        "NOT_CONFIGURED", "tests, preview and final A2A completion: separate evidence required"
    )
    result = (
        "BLOCKED" if d.blocked else "NOT_CONFIGURED" if d.runtime_blocked else "READY FOR PREVIEW"
    )
    print(f"RESULT {result}; deployment and smoke not certified by doctor")
    return 1 if d.blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
