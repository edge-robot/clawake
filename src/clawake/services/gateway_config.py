from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path

from clawake.config import InstanceSpec

_LOOPBACK_ADDRESSES = {"127.0.0.1", "::1", "localhost"}
_MANAGED_WORKSPACE = "/workspace"
_PLUGIN_RUNTIME_STATE = "clawake-managed-plugin-runtime.json"


def _load_config(config_path: Path) -> dict[str, object]:
    if not config_path.exists():
        return {}
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid OpenClaw config '{config_path}': {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"OpenClaw config '{config_path}' must contain a JSON object")
    return payload


def _write_config(config_path: Path, payload: dict[str, object]) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = config_path.with_suffix(".json.clawake-tmp")
    temporary_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary_path, 0o600)
    temporary_path.replace(config_path)


def _format_origin_host(address: str) -> str:
    if ":" in address and not address.startswith("["):
        return f"[{address}]"
    return address


def local_control_ui_origins(instance: InstanceSpec) -> list[str]:
    """Return browser origins Clawake can derive from the gateway port mapping."""
    origins: list[str] = []
    gateway_port = instance.gateway_runtime.gateway_container_port
    for port in instance.ports:
        if port.protocol != "tcp" or port.container_port != gateway_port:
            continue
        host = _format_origin_host(port.bind_address)
        origins.append(f"http://{host}:{port.host_port}")
        if port.bind_address in _LOOPBACK_ADDRESSES:
            origins.append(f"http://localhost:{port.host_port}")
    return list(dict.fromkeys(origins))


def ensure_workspace_config(instance: InstanceSpec) -> tuple[Path, bool]:
    """Make the workspace mounted by Clawake OpenClaw's default agent workspace."""
    config_path = Path(instance.workspace_path).expanduser() / ".openclaw" / "openclaw.json"
    payload = _load_config(config_path)

    agents = payload.setdefault("agents", {})
    if not isinstance(agents, dict):
        raise ValueError(f"OpenClaw config '{config_path}': agents must be an object")
    defaults = agents.setdefault("defaults", {})
    if not isinstance(defaults, dict):
        raise ValueError(f"OpenClaw config '{config_path}': agents.defaults must be an object")

    if defaults.get("workspace") == _MANAGED_WORKSPACE:
        return config_path, False

    defaults["workspace"] = _MANAGED_WORKSPACE
    _write_config(config_path, payload)
    return config_path, True


def ensure_control_ui_config(instance: InstanceSpec) -> tuple[Path, bool]:
    """Merge inferred local Control UI settings into OpenClaw's persisted config."""
    config_path = Path(instance.workspace_path).expanduser() / ".openclaw" / "openclaw.json"
    origins = local_control_ui_origins(instance)
    if not instance.gateway_runtime.enabled or not origins:
        return config_path, False

    payload = _load_config(config_path)

    gateway = payload.setdefault("gateway", {})
    if not isinstance(gateway, dict):
        raise ValueError(f"OpenClaw config '{config_path}': gateway must be an object")
    control_ui = gateway.setdefault("controlUi", {})
    if not isinstance(control_ui, dict):
        raise ValueError(f"OpenClaw config '{config_path}': gateway.controlUi must be an object")

    existing_origins = control_ui.get("allowedOrigins", [])
    if not isinstance(existing_origins, list) or not all(
        isinstance(origin, str) for origin in existing_origins
    ):
        raise ValueError(
            f"OpenClaw config '{config_path}': gateway.controlUi.allowedOrigins "
            "must be a string array"
        )

    changed = False
    merged_origins = list(dict.fromkeys([*existing_origins, *origins]))
    if merged_origins != existing_origins:
        control_ui["allowedOrigins"] = merged_origins
        changed = True

    gateway_bindings = [
        port.bind_address
        for port in instance.ports
        if port.protocol == "tcp"
        and port.container_port == instance.gateway_runtime.gateway_container_port
    ]
    local_http_only = bool(gateway_bindings) and all(
        address in _LOOPBACK_ADDRESSES for address in gateway_bindings
    )
    if local_http_only and "allowInsecureAuth" not in control_ui:
        # OpenClaw otherwise rejects token authentication from its HTTP Control UI.
        # This is only enabled when the published gateway is loopback-only.
        control_ui["allowInsecureAuth"] = True
        changed = True

    if not changed:
        return config_path, False

    _write_config(config_path, payload)
    return config_path, True


def ensure_plugin_runtime_config(instance: InstanceSpec) -> tuple[Path, bool]:
    """Atomically merge managed plugin settings and per-agent optional-tool grants."""
    config_path = Path(instance.workspace_path).expanduser() / ".openclaw" / "openclaw.json"
    state_path = config_path.with_name(_PLUGIN_RUNTIME_STATE)
    payload = _load_config(config_path)
    original_payload = deepcopy(payload)
    state = _load_config(state_path)
    changed = False

    plugins = payload.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        raise ValueError(f"OpenClaw config '{config_path}': plugins must be an object")
    entries = plugins.setdefault("entries", {})
    if not isinstance(entries, dict):
        raise ValueError(f"OpenClaw config '{config_path}': plugins.entries must be an object")

    for plugin in instance.plugins:
        entry = entries.setdefault(plugin.id, {})
        if not isinstance(entry, dict):
            raise ValueError(
                f"OpenClaw config '{config_path}': plugin entry '{plugin.id}' must be an object"
            )
        if entry.get("enabled") is not plugin.enabled:
            entry["enabled"] = plugin.enabled
            changed = True
        if plugin.config and entry.get("config") != plugin.config:
            entry["config"] = plugin.config
            changed = True

    if any(plugin.custom_ui for plugin in instance.plugins):
        gateway = payload.setdefault("gateway", {})
        if not isinstance(gateway, dict):
            raise ValueError(f"OpenClaw config '{config_path}': gateway must be an object")
        control_ui = gateway.setdefault("controlUi", {})
        if not isinstance(control_ui, dict):
            raise ValueError(
                f"OpenClaw config '{config_path}': gateway.controlUi must be an object"
            )
        experimental = control_ui.setdefault("experimental", {})
        if not isinstance(experimental, dict):
            raise ValueError(
                f"OpenClaw config '{config_path}': gateway.controlUi.experimental must be an object"
            )
        if experimental.get("customPlugins") is not True:
            experimental["customPlugins"] = True
            changed = True

    previous_grants = state.get("agentToolAllow", {})
    if not isinstance(previous_grants, dict):
        raise ValueError(f"Clawake state '{state_path}': agentToolAllow must be an object")

    agents = payload.setdefault("agents", {})
    if not isinstance(agents, dict):
        raise ValueError(f"OpenClaw config '{config_path}': agents must be an object")
    agent_entries = agents.setdefault("entries", {})
    if not isinstance(agent_entries, dict):
        raise ValueError(f"OpenClaw config '{config_path}': agents.entries must be an object")
    for agent_id, grant in previous_grants.items():
        if not isinstance(agent_id, str) or not isinstance(grant, dict):
            raise ValueError(f"Clawake state '{state_path}': invalid agent tool grant")
        key = grant.get("key")
        old_tools = grant.get("tools")
        if (
            key not in {"allow", "alsoAllow"}
            or not isinstance(old_tools, list)
            or not all(isinstance(item, str) for item in old_tools)
        ):
            raise ValueError(f"Clawake state '{state_path}': invalid grant for agent '{agent_id}'")
        agent = agent_entries.get(agent_id)
        if not isinstance(agent, dict):
            continue
        tools = agent.get("tools")
        if not isinstance(tools, dict):
            continue
        existing = tools.get(key)
        if not isinstance(existing, list) or not all(isinstance(item, str) for item in existing):
            continue
        filtered = [item for item in existing if item not in old_tools]
        if filtered != existing:
            tools[key] = filtered
            changed = True

    next_grants: dict[str, object] = {}
    for agent_id, managed_tools in instance.agent_tool_allow.items():
        agent = agent_entries.setdefault(agent_id, {})
        if not isinstance(agent, dict):
            raise ValueError(
                f"OpenClaw config '{config_path}': agent entry '{agent_id}' must be an object"
            )
        tools = agent.setdefault("tools", {})
        if not isinstance(tools, dict):
            raise ValueError(
                f"OpenClaw config '{config_path}': tools for agent '{agent_id}' must be an object"
            )
        key = "allow" if "allow" in tools else "alsoAllow"
        existing = tools.get(key, [])
        if not isinstance(existing, list) or not all(isinstance(item, str) for item in existing):
            raise ValueError(
                f"OpenClaw config '{config_path}': agents.entries.{agent_id}.tools.{key} "
                "must be a string array"
            )
        merged = list(dict.fromkeys([*existing, *managed_tools]))
        if merged != existing:
            tools[key] = merged
            changed = True

        next_grants[agent_id] = {"key": key, "tools": managed_tools}

    changed = payload != original_payload
    if changed:
        _write_config(config_path, payload)
    next_state: dict[str, object] = {"version": 1, "agentToolAllow": next_grants}
    if state != next_state:
        _write_config(state_path, next_state)
    return config_path, changed
