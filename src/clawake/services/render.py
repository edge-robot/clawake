from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from clawake.config import InstanceSpec, Inventory, NetworkSpec
from clawake.services.runtime_upgrade import browser_cache_path, is_browser_image


def image_ref(instance: InstanceSpec) -> str:
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
    return (
        f"[Unit]\nDescription=Clawake shared network {network.name}\n\n"
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
    for instance in inventory.instances:
        assets = render_instance_assets(instance, template_root=template_root)
        for relative_path, content in assets.items():
            target = output_dir / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            rendered_paths.append(target)
    return rendered_paths
