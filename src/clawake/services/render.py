from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from clawake.config import InstanceSpec, Inventory, NetworkSpec, ServiceSpec
from clawake.services.runtime_upgrade import browser_cache_path, is_browser_image


def image_ref(instance: InstanceSpec | ServiceSpec) -> str:
    base = f"{instance.image.repository}:{instance.image.tag}"
    if instance.image.digest:
        return f"{base}@{instance.image.digest}"
    return base


def _environment(template_root: Path) -> Environment:
    return Environment(
        loader=FileSystemLoader(str(template_root)),
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_instance(instance: InstanceSpec, template_root: Path) -> str:
    return render_instance_assets(instance, template_root=template_root)[instance.quadlet_path]


def render_instance_assets(instance: InstanceSpec, template_root: Path) -> dict[str, str]:
    env = _environment(template_root)
    context = {
        "instance": instance,
        "image_ref": image_ref(instance),
        "browser_image": is_browser_image(instance.image),
        "browser_cache_path": browser_cache_path(instance),
    }
    templates = {
        instance.quadlet_path: "quadlet/openclaw.container.j2",
        instance.runtime_volume_quadlet_path: "quadlet/openclaw.volume.j2",
    }
    if not instance.networks:
        templates[instance.network_quadlet_path] = "quadlet/openclaw.network.j2"
    rendered: dict[str, str] = {}
    for target_path, template_name in templates.items():
        template = env.get_template(template_name)
        rendered[target_path] = template.render(**context).strip() + "\n"
    return rendered


def render_shared_network(network: NetworkSpec) -> str:
    quadlet = (
        "" if network.quadlet_default_dependencies else "[Quadlet]\nDefaultDependencies=false\n\n"
    )
    return (
        quadlet + f"[Unit]\nDescription=Clawake shared network {network.name}\n\n"
        f"[Network]\nNetworkName={network.name}\nNetworkDeleteOnStop=true\n"
    )


def render_inventory(inventory: Inventory, output_dir: Path, template_root: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rendered_paths: list[Path] = []
    for network in inventory.networks:
        target = output_dir / network.quadlet_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_shared_network(network), encoding="utf-8")
        rendered_paths.append(target)
    for instance in [*inventory.instances, *inventory.services]:
        assets = (
            render_instance_assets(instance, template_root=template_root)
            if isinstance(instance, InstanceSpec)
            else render_service_assets(instance)
        )
        for relative_path, content in assets.items():
            target = output_dir / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            rendered_paths.append(target)
    return rendered_paths


def quadlet_word(value: str) -> str:
    """systemd word quoting, preserving literal arguments without shell expansion."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def render_service_assets(service: ServiceSpec) -> dict[str, str]:
    lines = [
        "[Unit]",
        f"Description=Clawake service {service.name}",
        "After=network-online.target",
        "Wants=network-online.target",
    ]
    for dependency in sorted(service.depends_on):
        lines += [f"After={dependency}.service", f"Requires={dependency}.service"]
    lines += [
        "",
        "[Container]",
        f"ContainerName={service.name}",
        f"Image={image_ref(service)}",
        "UserNS=keep-id",
        f"NetworkAlias={service.name}",
        f"Label=clawake.service={service.name}",
    ]
    for network in sorted(service.networks):
        lines.append(f"Network={network}.network")
    for path in service.env_files:
        lines.append(f"EnvironmentFile={path}")
    for mount in service.mounts:
        lines.append(f"Volume={mount.source}:{mount.target}" + (":ro" if mount.read_only else ""))
    for volume in service.volumes:
        lines.append(
            f"Volume={service.volume_name(volume.name)}.volume:{volume.target}"
            + (":ro" if volume.read_only else "")
        )
    for secret in service.secrets:
        lines.append(
            f"Secret={secret.name},type=mount,target={secret.target or secret.name},mode=0400"
        )
    for port in service.ports:
        address = f"[{port.bind_address}]" if ":" in port.bind_address else port.bind_address
        lines.append(
            f"PublishPort={address}:{port.host_port}:{port.container_port}/{port.protocol}"
        )
    if service.command:
        lines.append("Exec=" + " ".join(quadlet_word(arg) for arg in service.command))
    if service.health:
        health = service.health
        # curl must be supplied by the service image. Never disable TLS verification.
        lines += [
            f"HealthCmd=curl --fail --silent --show-error --max-time {health.timeout_seconds} "
            f"{health.scheme}://{service.name}:{health.port}{health.path}",
            f"HealthInterval={health.interval_seconds}s",
            f"HealthTimeout={health.timeout_seconds}s",
            f"HealthRetries={health.retries}",
        ]
    lines += [
        "",
        "[Service]",
        f"Restart={service.restart_policy}",
        "TimeoutStartSec=120",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    assets = {service.quadlet_path: "\n".join(lines)}
    for volume in service.volumes:
        name = service.volume_name(volume.name)
        assets[f"{name}.volume"] = f"[Volume]\nVolumeName={name}\n"
    return assets
