"""Pure planning of the OpenClaw fields owned by Clawake.

The ownership ledger contains generated values (including secret references), never
copies of unmanaged runtime configuration. Operator-owned fields remain untouched.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection
from copy import deepcopy
from pathlib import Path

from clawake.config import InstanceSpec, Inventory
from clawake.services.gateway_config import configure_control_ui, configure_workspace

STATE_FILE = "clawake-managed-runtime.json"
_MISSING = object()

# ADR-001 (3) / ADR-005 (5): named one-time adoptions of not-yet-owned fields.
DISCORD_FIELD = "channels/discord"
DISCORD_ADOPTION = "discord-channel"
ADOPTIONS = {DISCORD_ADOPTION: DISCORD_FIELD}
_SNOWFLAKE_SEGMENT = re.compile(r"[0-9]{17,20}")


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
    mcp = (
        {
            "mcp/servers/onshape": {
                "transport": "streamable-http",
                "url": "http://clawonshape-mcp:8080/mcp",
                "enabled": instance.onshape_mcp_enabled,
                "requestTimeoutMs": 30000,
                "connectionTimeoutMs": 5000,
                "supportsParallelToolCalls": False,
                **(
                    {
                        "toolFilter": {
                            "include": [
                                "onshape_auth_status",
                                "onshape_api_explain",
                                "onshape_api_schema",
                                "onshape_api_call",
                            ]
                        }
                    }
                    if instance.onshape_mcp_enabled
                    else {}
                ),
            }
        }
        if instance.onshape_mcp
        else {}
    )
    if settings is None:
        return mcp
    desired = {f"agents/entries/{settings.lead_agent_id}/workspace": "/workspace"}
    desired.update(mcp)
    for name, server in settings.mcp_servers.items():
        path = f"mcp/servers/{name}"
        # InstanceSpec validation rejects this; never let one MCP model override the other.
        if path in desired:
            raise ValueError(f"MCP server '{name}' is configured by onshape_mcp and mcp_servers")
        desired[path] = server.runtime_config()
    if settings.subagents:
        spec = settings.subagents
        desired.update(
            {
                "agents/defaults/subagents/maxSpawnDepth": spec.max_spawn_depth,
                "agents/defaults/subagents/maxChildrenPerAgent": spec.max_children_per_agent,
                "agents/defaults/subagents/maxConcurrent": spec.max_concurrent,
                "agents/defaults/subagents/runTimeoutSeconds": spec.run_timeout_seconds,
                "tools/sessions/visibility": "tree",
            }
        )
    if settings.discord is not None:
        # ADR-005 item 4: one owned field holding the whole object.
        # Architecture-Ref: clawake/docs/architecture.md#shared-networks-and-leadership-teams
        # The doc says unmanaged channels survive reconciliation; ADR-005 makes a declared
        # channels.discord Clawake-owned (taken over once via setup --adopt discord-channel).
        desired[DISCORD_FIELD] = settings.discord.runtime_config()
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
        elif path == DISCORD_FIELD or path.startswith(DISCORD_FIELD + "/"):
            kind = "added" if left is _MISSING else "removed" if right is _MISSING else "changed"
            result.append(f"{_redacted(path)} ({kind})")
        else:
            result.append(path)
    return list(dict.fromkeys(result))


def _redacted(path: str) -> str:
    """Key path under channels/discord with every Discord ID replaced by ``<id>``."""
    return "/".join(
        "<id>" if _SNOWFLAKE_SEGMENT.fullmatch(part) else part for part in path.split("/")
    )


def _unowned_discord_conflict(payload: dict, old_fields: dict, desired: dict) -> object:
    """Return the live unowned ``channels.discord`` that differs from the rendering, or _MISSING."""
    if DISCORD_FIELD not in desired or DISCORD_FIELD in old_fields:
        return _MISSING
    channels = payload.get("channels")
    existing = channels.get("discord", _MISSING) if isinstance(channels, dict) else _MISSING
    if existing is _MISSING or existing == desired[DISCORD_FIELD]:
        return _MISSING
    return existing


def _read_runtime(instance: InstanceSpec) -> tuple[Path, dict, str | None, Path, dict, str | None]:
    root = instance.runtime_state_path
    config_path = root / "openclaw.json"
    state_path = root / STATE_FILE
    payload, previous = _read_object(config_path)
    state, previous_state = _read_object(state_path)
    old_fields = state.get("fields", {})
    if not isinstance(old_fields, dict) or state.get("version", 1) != 1:
        raise ValueError(f"Invalid managed runtime ledger: {state_path}")
    return config_path, payload, previous, state_path, state, previous_state


def pending_adoptions(inventory: Inventory, instance: InstanceSpec) -> tuple[str, ...]:
    """Named adoptions a setup of this member needs; read-only, never reads token values."""
    _config, payload, _previous, _state_path, state, _ = _read_runtime(instance)
    desired = desired_fields(inventory, instance)
    if _unowned_discord_conflict(payload, state.get("fields", {}), desired) is _MISSING:
        return ()
    return (DISCORD_ADOPTION,)


def _check_discord_adoption(
    instance: InstanceSpec, existing: object, config_path: Path, adopt: Collection[str]
) -> None:
    if DISCORD_ADOPTION not in adopt:
        raise ValueError(
            f"Refusing to overwrite unmanaged OpenClaw field: {DISCORD_FIELD} (named adoption "
            f"'{DISCORD_ADOPTION}': review the preview, then run setup with "
            f"--adopt {DISCORD_ADOPTION})"
        )
    if not isinstance(existing, dict):
        raise ValueError(
            f"Cannot adopt {DISCORD_FIELD} of member {instance.name}: the live value in "
            f"{config_path} is not an object"
        )
    # Only the type is inspected; the value is never read into a message or the ledger.
    if isinstance(existing.get("token"), str):
        spec = instance.openclaw.discord if instance.openclaw else None
        if spec is not None and spec.token_env is not None:
            reference = json.dumps({"source": "env", "provider": "default", "id": spec.token_env})
            action = (
                f"Move it into the member env file as {spec.token_env}, replace "
                f"channels.discord.token with {reference}"
            )
        else:
            action = (
                "Discord is disabled for this member: remove channels.discord.token (or "
                "replace it with an env SecretRef)"
            )
        raise ValueError(
            f"Cannot adopt {DISCORD_FIELD} of member {instance.name}: the live token in "
            f"{config_path} is a plaintext string. {action}, then re-run setup --adopt "
            f"{DISCORD_ADOPTION}. Clawake never reads, moves or deletes token values."
        )


def plan_runtime_files(
    inventory: Inventory,
    instance: InstanceSpec,
    *,
    adopt: Collection[str] = (),
) -> list[tuple[Path, str, str | None, tuple[str, ...]]]:
    """Return planned files and changed keys without writes or secret expansion.

    ``adopt`` names the one-time adoptions the operator requested (``setup --adopt``).
    """
    config_path, payload, previous, state_path, state, previous_state = _read_runtime(instance)
    original = deepcopy(payload)
    old_fields = state.get("fields", {})
    desired = desired_fields(inventory, instance)
    conflict = _unowned_discord_conflict(payload, old_fields, desired)
    if conflict is not _MISSING:
        _check_discord_adoption(instance, conflict, config_path, adopt)
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
        # Drift guard: a field Clawake already owns must still hold the value it wrote.
        if path in old_fields and existing is not _MISSING and existing != old_fields[path]:
            raise ValueError(f"Managed field changed outside Clawake: {path}")
        # One-time adoption of a not-yet-managed, disabled hosted Onshape connector.
        adopting_disabled_hosted_onshape = (
            instance.onshape_mcp
            and path == "mcp/servers/onshape"
            and isinstance(existing, dict)
            and existing.get("url") == "https://fs-mcp.labs.onshape.app/mcp"
            and existing.get("enabled") is False
        )
        # Named adoption discord-channel; its preconditions were checked above.
        adopting_discord = path == DISCORD_FIELD and conflict is not _MISSING
        if (
            path not in old_fields
            and existing is not _MISSING
            and existing != value
            and not adopting_disabled_hosted_onshape
            and not adopting_discord
        ):
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
        if any(
            isinstance(binding, dict) and binding.get("match", {}).get("channel") == "a2a"
            for binding in bindings
        ):
            raise ValueError("Refusing to replace unmanaged A2A routing bindings")
        next_bindings = [
            {
                "agentId": instance.openclaw.lead_agent_id,
                "match": {"channel": "a2a"},
            }
        ]
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
        result.append(
            (
                config_path,
                json.dumps(payload, indent=2) + "\n",
                previous,
                tuple(_changed_keys(original, payload)),
            )
        )
    next_state = {"version": 1, "fields": desired, "bindings": next_bindings}
    if (desired or state) and next_state != state:
        result.append(
            (
                state_path,
                json.dumps(next_state, indent=2) + "\n",
                previous_state,
                ("managed ownership ledger",),
            )
        )
    return result
