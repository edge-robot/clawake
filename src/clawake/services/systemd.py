from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass


@dataclass
class CommandResult:
    command: list[str]
    return_code: int
    stdout: str
    stderr: str


_UNIT_SAFE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.@-]+$")


class SystemdService:
    @staticmethod
    def unit_name(instance_name: str) -> str:
        if not _UNIT_SAFE_NAME_PATTERN.fullmatch(instance_name):
            raise ValueError("Instance name contains unsupported characters")
        return f"{instance_name}.service"

    def _run(self, command: list[str], execute: bool) -> CommandResult:
        if not execute:
            return CommandResult(command=command, return_code=0, stdout="DRY_RUN", stderr="")
        process = subprocess.run(command, check=False, capture_output=True, text=True)
        return CommandResult(
            command=command,
            return_code=process.returncode,
            stdout=process.stdout.strip(),
            stderr=process.stderr.strip(),
        )

    def daemon_reload(self, execute: bool = False) -> CommandResult:
        return self._run(["systemctl", "--user", "daemon-reload"], execute)

    def stop(self, instance_name: str, execute: bool = False) -> CommandResult:
        return self._run(["systemctl", "--user", "stop", self.unit_name(instance_name)], execute)

    def disable(self, instance_name: str, execute: bool = False) -> CommandResult:
        return self._run(["systemctl", "--user", "disable", self.unit_name(instance_name)], execute)

    def start(self, instance_name: str, execute: bool = False) -> CommandResult:
        return self._run(["systemctl", "--user", "start", self.unit_name(instance_name)], execute)

    def restart(self, instance_name: str, execute: bool = False) -> CommandResult:
        return self._run(["systemctl", "--user", "restart", self.unit_name(instance_name)], execute)

    def status(self, instance_name: str, execute: bool = False) -> CommandResult:
        return self._run(["systemctl", "--user", "status", self.unit_name(instance_name)], execute)

    def logs(self, instance_name: str, lines: int = 100, execute: bool = False) -> CommandResult:
        return self._run(
            ["journalctl", "--user-unit", self.unit_name(instance_name), "-n", str(lines)],
            execute,
        )

    def remove_quadlet(self, quadlet_path: str, execute: bool = False) -> CommandResult:
        return self._run(["rm", "-f", quadlet_path], execute)

    def remove_container(self, container_name: str, execute: bool = False) -> CommandResult:
        return self._run(["podman", "rm", "-f", container_name], execute)

    def remove_image(self, image_ref: str, execute: bool = False) -> CommandResult:
        return self._run(["podman", "image", "rm", image_ref], execute)
