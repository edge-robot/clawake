"""Pure planning of the OpenClaw fields owned by Clawake.

The ownership ledger contains generated values (including secret references), never
copies of unmanaged runtime configuration. Operator-owned fields remain untouched.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from clawake.config import InstanceSpec, Inventory
from clawake.services.gateway_config import configure_control_ui, configure_workspace

STATE_FILE = "clawake-managed-runtime.json"
_MISSING = object()


def _parent(payload: dict, path: str, *, create: bool) -> tuple[dict, str] | None:
    parts = path.split("/")
    node = payload
    for key in parts[:-1]:
        if key not in node:
            if not create:
                return None
            node[key] = {}
        node = node[key]
        if not isinstance(node, dict):
            raise ValueError(f"OpenClaw configuration has an incompatible object at {path}")
    return node, parts[-1]


def desired_fields(inventory: Inventory, instance: InstanceSpec) -> dict:
    settings = instance.openclaw
    if settings is None:
        return {}
    desired = {f"agents/entries/{settings.lead_agent_id}/workspace": "/workspace"}
    if settings.subagents:
        spec = settings.subagents
        desired.update({
            "agents/defaults/subagents/maxSpawnDepth": spec.max_spawn_depth,
            "agents/defaults/subagents/maxChildrenPerAgent": spec.max_children_per_agent,
            "agents/defaults/subagents/maxConcurrent": spec.max_concurrent,
            "agents/defaults/subagents/runTimeoutSeconds": spec.run_timeout_seconds,
            "tools/sessions/visibility": "tree",
        })
    desired["channels/a2a/enabled"] = settings.a2a.enabled
    desired["plugins/entries/a2a/enabled"] = settings.a2a.enabled
    if settings.a2a.enabled:
        desired["channels/a2a/exposeAgents"] = [settings.lead_agent_id]
        targets = {member.name: member for member in inventory.instances}
        for name, peer in settings.a2a.peers.items():
            target = targets[peer.instance]
            port = target.gateway_runtime.gateway_container_port
            desired[f"channels/a2a/peers/{name}"] = {
                "token": "${" + peer.inbound_token_env + "}",
                "outboundToken": "${" + peer.outbound_token_env + "}",
                "url": f"http://{target.container_name}:{port}/a2a/v1",
            }
    return desired


def _read_object(path: Path) -> tuple[dict, str | None]:
    previous = path.read_text(encoding="utf-8") if path.exists() else None
    try:
        payload = json.loads(previous) if previous is not None else {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON configuration: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Expected configuration object: {path}")
    return payload, previous


def _changed_keys(before: dict, after: dict, prefix: str = "") -> list[str]:
    result = []
    for key in sorted(before.keys() | after.keys()):
        left, right = before.get(key, _MISSING), after.get(key, _MISSING)
        path = prefix + key
        if left == right:
            continue
        if isinstance(left, dict) and isinstance(right, dict):
            result.extend(_changed_keys(left, right, path + "/"))
        else:
            result.append(path)
    return result


def plan_runtime_files(
    inventory: Inventory, instance: InstanceSpec,
) -> list[tuple[Path, str, str | None, tuple[str, ...]]]:
    """Return planned files and changed keys without writes or secret expansion."""
    root = Path(instance.workspace_path).expanduser() / ".openclaw"
    config_path = root / "openclaw.json"
    state_path = root / STATE_FILE
    payload, previous = _read_object(config_path)
    original = deepcopy(payload)
    state, previous_state = _read_object(state_path)
    old_fields = state.get("fields", {})
    if not isinstance(old_fields, dict) or state.get("version", 1) != 1:
        raise ValueError(f"Invalid managed runtime ledger: {state_path}")
    desired = desired_fields(inventory, instance)
    for path, old_value in old_fields.items():
        if path in desired:
            continue
        parent = _parent(payload, path, create=False)
        if parent is not None:
            node, key = parent
            if key in node and node[key] != old_value:
                raise ValueError(f"Managed field changed outside Clawake: {path}")
            node.pop(key, None)
    for path, value in desired.items():
        parent = _parent(payload, path, create=True)
        assert parent is not None
        node, key = parent
        existing = node.get(key, _MISSING)
        if path not in old_fields and existing is not _MISSING and existing != value:
            raise ValueError(f"Refusing to overwrite unmanaged OpenClaw field: {path}")
        node[key] = value

    # Bind A2A ingress explicitly, even if operators add other local agents later.
    old_bindings = state.get("bindings", [])
    bindings = payload.get("bindings", [])
    if not isinstance(bindings, list) or not isinstance(old_bindings, list):
        raise ValueError("OpenClaw bindings must be an array")
    bindings = [binding for binding in bindings if binding not in old_bindings]
    next_bindings = []
    if instance.openclaw and instance.openclaw.a2a.enabled:
        if any(isinstance(binding, dict) and
               binding.get("match", {}).get("channel") == "a2a" for binding in bindings):
            raise ValueError("Refusing to replace unmanaged A2A routing bindings")
        next_bindings = [{
            "agentId": instance.openclaw.lead_agent_id,
            "match": {"channel": "a2a"},
        }]
    if next_bindings or old_bindings:
        payload["bindings"] = bindings + next_bindings

    configure_workspace(payload)
    configure_control_ui(instance, payload)
    if instance.openclaw and instance.openclaw.a2a.enabled:
        allowed = payload.get("plugins", {}).get("allow")
        if allowed is not None and (not isinstance(allowed, list) or "a2a" not in allowed):
            raise ValueError("Existing plugins.allow must include a2a before enabling A2A")
    result = []
    if payload != original or previous is None:
        result.append((config_path, json.dumps(payload, indent=2) + "\n", previous,
                       tuple(_changed_keys(original, payload))))
    next_state = {"version": 1, "fields": desired, "bindings": next_bindings}
    if (desired or state) and next_state != state:
        result.append((state_path, json.dumps(next_state, indent=2) + "\n", previous_state,
                       ("managed ownership ledger",)))
    return result
