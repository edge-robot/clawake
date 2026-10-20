import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from clawake.config import BackupPolicy, DashboardMeta, ImageSpec, InstanceSpec
from clawake.services.backup import backup_instance, prune_backups


def _instance_with_paths(workspace: Path, team_definition_path: Path) -> InstanceSpec:
    return InstanceSpec(
        name="one",
        host="h",
        role="developer",
        workspace_path=str(workspace),
        team_definition_path=str(team_definition_path),
        quadlet_path="one.container",
        container_name="one",
        image=ImageSpec(repository="ghcr.io/openclaw/openclaw", tag="2026.6.5"),
        backup_policy=BackupPolicy(enabled=True, pre_mutation=True),
        dashboard=DashboardMeta(friendly_name="One"),
    )


def test_backup_instance_skips_unreadable_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    team_definition = tmp_path / "role"
    output_dir = tmp_path / "backups"

    workspace.mkdir(parents=True, exist_ok=True)
    team_definition.mkdir(parents=True, exist_ok=True)

    readable = team_definition / "readable.txt"
    readable.write_text("ok\n", encoding="utf-8")

    unreadable = team_definition / "openclaw.json"
    unreadable.write_text("{}\n", encoding="utf-8")
    unreadable.chmod(0)

    instance = _instance_with_paths(workspace, team_definition)

    try:
        archive = backup_instance(instance, output_dir=output_dir, execute=True)
    finally:
        unreadable.chmod(0o600)

    assert archive.exists()

    with tarfile.open(archive, "r:gz") as tar:
        members = [member.name for member in tar.getmembers()]

    assert any(name.endswith("/readable.txt") for name in members)
    assert not any(name.endswith("/openclaw.json") for name in members)


def test_backup_instance_dry_run_does_not_create_archive(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    team_definition = tmp_path / "role"
    output_dir = tmp_path / "backups"

    workspace.mkdir(parents=True, exist_ok=True)
    team_definition.mkdir(parents=True, exist_ok=True)

    instance = _instance_with_paths(workspace, team_definition)
    archive = backup_instance(instance, output_dir=output_dir, execute=False)

    assert not archive.exists()
    assert archive.parent == output_dir


def test_backup_instance_skips_legacy_workspace_runtime_tree(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    team_definition = tmp_path / "role"
    output_dir = tmp_path / "backups"

    workspace.mkdir(parents=True, exist_ok=True)
    team_definition.mkdir(parents=True, exist_ok=True)

    legacy_runtime = workspace / ".clawake" / "state"
    legacy_runtime.mkdir(parents=True, exist_ok=True)
    (legacy_runtime / "openclaw.json").write_text('{"legacy":true}\n', encoding="utf-8")
    (workspace / "README.txt").write_text("workspace data\n", encoding="utf-8")

    instance = _instance_with_paths(workspace, team_definition)
    archive = backup_instance(instance, output_dir=output_dir, execute=True)

    with tarfile.open(archive, "r:gz") as tar:
        members = [member.name for member in tar.getmembers()]

    assert any(name.endswith("/README.txt") for name in members)
    assert not any("/.clawake/" in name for name in members)


def test_prune_backups_retains_newest_matching_files(tmp_path: Path) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    paths = [backups / f"one-2026102{number}T093015.000001Z.tar.gz" for number in range(3)]
    for path in paths:
        path.write_text(path.name, encoding="utf-8")

    removed = prune_backups(backups, "one", retention=2, suffix=".tar.gz")

    assert removed == [paths[0]]
    assert not paths[0].exists()
    assert paths[1].exists()
    assert paths[2].exists()


# --- #29: runtime archives never overwrite each other -----------------------------------------


def _fixed_clock(*moments: datetime):
    remaining = list(moments)

    def now() -> datetime:
        return remaining.pop(0)

    return now


_SAME_SECOND = (
    datetime(2026, 10, 20, 9, 30, 15, 1, tzinfo=UTC),
    datetime(2026, 10, 20, 9, 30, 15, 2, tzinfo=UTC),
)


def _small_instance(tmp_path: Path) -> InstanceSpec:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.txt").write_text("workspace data\n", encoding="utf-8")
    return _instance_with_paths(workspace, tmp_path / "team")


def test_two_runtime_backups_in_the_same_second_produce_two_archives(tmp_path: Path) -> None:
    instance = _small_instance(tmp_path)
    output = tmp_path / "backups"
    clock = _fixed_clock(*_SAME_SECOND)

    first = backup_instance(instance, output, execute=True, now=clock)
    second = backup_instance(instance, output, execute=True, now=clock)

    assert first.name == "one-20261020T093015.000001Z.tar.gz"
    assert second.name == "one-20261020T093015.000002Z.tar.gz"
    assert sorted(path.name for path in output.iterdir()) == [first.name, second.name]


def test_runtime_backup_fails_closed_when_the_archive_exists(tmp_path: Path) -> None:
    instance = _small_instance(tmp_path)
    output = tmp_path / "backups"
    output.mkdir()
    existing = output / "one-20261020T093015.000001Z.tar.gz"
    existing.write_bytes(b"earlier archive")

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        backup_instance(instance, output, execute=True, now=_fixed_clock(_SAME_SECOND[0]))

    assert existing.read_bytes() == b"earlier archive"


# --- #28: pruning selects exactly one member's or config's backups ----------------------------


def _touch(directory: Path, *names: str) -> list[Path]:
    paths = [directory / name for name in names]
    for path in paths:
        path.write_text(path.name, encoding="utf-8")
    return paths


def _names(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir())


def test_pruning_member_a_keeps_every_backup_of_member_a_b(tmp_path: Path) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    a_b = _touch(
        backups,
        "a-b-20261018T080000Z.tar.gz",
        "a-b-20261019T080000.000001Z.tar.gz",
        "a-b-20261020T080000.000001Z.tar.gz",
    )
    _touch(backups, "a-20261018T090000Z.tar.gz", "a-20261020T090000.000001Z.tar.gz")

    removed = prune_backups(backups, "a", retention=1, suffix=".tar.gz")

    assert [path.name for path in removed] == ["a-20261018T090000Z.tar.gz"]
    assert all(path.exists() for path in a_b)
    assert "a-20261020T090000.000001Z.tar.gz" in _names(backups)


def test_pruning_handles_second_and_sub_second_names_by_their_timestamp(tmp_path: Path) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    _touch(
        backups,
        "one-20261018T101439Z.tar.gz",  # old second-resolution name, oldest
        "one-20261019T101439.342141Z.tar.gz",
        "one-20261020T101439Z.tar.gz",  # old name, but the newest backup
        "one-20261019T101439.342142Z.tar.gz",
    )

    removed = prune_backups(backups, "one", retention=2, suffix=".tar.gz")

    assert sorted(path.name for path in removed) == [
        "one-20261018T101439Z.tar.gz",
        "one-20261019T101439.342141Z.tar.gz",
    ]
    assert _names(backups) == [
        "one-20261019T101439.342142Z.tar.gz",
        "one-20261020T101439Z.tar.gz",
    ]


def test_pruning_never_deletes_files_that_do_not_parse(tmp_path: Path) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    unparseable = _touch(
        backups,
        "one-latest.tar.gz",
        "one-20261020T101439Z.tar.gz.partial",
        "one-20261020T101439.342Z.tar.gz",
        "one-20261020T101439Z-1.tar.gz",
        "one-20261320T101439Z.tar.gz",  # month 13
        "one-20261019T101439Z.tgz",
        "notes-one-20261019T101439Z.tar.gz",
    )
    _touch(backups, "one-20261019T101439Z.tar.gz", "one-20261020T101439Z.tar.gz")
    (backups / "one-20261018T101439Z.tar.gz").symlink_to(backups / "one-latest.tar.gz")

    removed = prune_backups(backups, "one", retention=1, suffix=".tar.gz")

    assert [path.name for path in removed] == ["one-20261019T101439Z.tar.gz"]
    assert all(path.exists() for path in unparseable)
    assert (backups / "one-20261018T101439Z.tar.gz").is_symlink()


def test_pruning_a_config_keeps_member_archives_and_other_configs(tmp_path: Path) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    kept = _touch(
        backups,
        "team-20261018T101439Z.tar.gz",  # member named "team"
        "team-b-20261018T101439Z.json",  # inventory team-b.json
        "team-20261018T101439Z.yml",  # same stem, other suffix
    )
    _touch(backups, "team-20261018T101439Z.json", "team-20261019T101439.000001Z.json")

    removed = prune_backups(backups, "team", retention=1, suffix=".json")

    assert [path.name for path in removed] == ["team-20261018T101439Z.json"]
    assert all(path.exists() for path in kept)


def test_pruning_a_missing_directory_removes_nothing(tmp_path: Path) -> None:
    assert prune_backups(tmp_path / "absent", "one", retention=1, suffix=".tar.gz") == []
