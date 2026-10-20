import pytest

from clawake.services.systemd import SystemdService


def test_rejects_invalid_instance_name() -> None:
    service = SystemdService()
    with pytest.raises(ValueError):
        service.unit_name("bad;name")


def test_start_is_a_dry_run_unless_executed() -> None:
    result = SystemdService().start("one")
    assert result.command == ["systemctl", "--user", "start", "one.service"]
    assert result.stdout == "DRY_RUN"
