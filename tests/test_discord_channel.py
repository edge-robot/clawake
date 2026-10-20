"""Discord channel provisioning from the inventory (ADR-005 items 3-5, #26, #21).

Every ID is a synthetic snowflake; every token is an obvious canary. Systemd, readiness,
backups and the clock are fakes; nothing touches Podman, systemd, OpenClaw or the network.
"""

from __future__ import annotations

import copy
import json
import os
import tarfile
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from clawake import cli
from clawake.config import DiscordSpec, load_inventory
from clawake.services.deployment import plan_deployment
from clawake.services.env_keys import env_file_keys
from clawake.services.runtime_config import (
    STATE_FILE,
    desired_fields,
    pending_adoptions,
    plan_runtime_files,
)
from clawake.services.runtime_pins import FILE_MODE
from test_member_ops import FakeHost, _invoke

GOLDEN = Path(__file__).parent / "fixtures" / "discord-channel" / "golden"
TEMPLATES = Path(__file__).resolve().parents[1] / "templates"

APPLICATION = "100000000000000010"
GUILD = "100000000000000001"
LEAD_CHANNEL = "100000000000000002"
APPROVER_A = "100000000000000003"
APPROVER_B = "100000000000000004"
ROLE = "100000000000000005"
ENGINEER_CHANNEL = "100000000000000006"
SITE_IDS = (APPLICATION, GUILD, LEAD_CHANNEL, APPROVER_A, APPROVER_B, ROLE, ENGINEER_CHANNEL)
CANARY = "canary-discord-token-must-never-appear-0000"
GATEWAY_CANARY = "canary-gateway-token-must-never-appear-0000"
SECRET_REF = {"source": "env", "provider": "default", "id": "DISCORD_BOT_TOKEN"}

LEAD_DISCORD = {
    "enabled": True,
    "token_env": "DISCORD_BOT_TOKEN",
    "application_id": "${site:discord.application_id}",
    "dm_policy": "allowlist",
    "allow_from": "${site:discord.approvers}",
    "group_policy": "allowlist",
    "guilds": [
        {
            "id": "${site:discord.guild}",
            "require_mention": True,
            "roles": ["${site:discord.operator_role}"],
            "channels": [
                {
                    "id": "${site:discord.lead_channel}",
                    "require_mention": True,
                    "users": "${site:discord.approvers}",
                }
            ],
        }
    ],
}
SITE = {
    "version": 1,
    "cluster": "example-cad",
    "values": {
        "discord.application_id": APPLICATION,
        "discord.guild": GUILD,
        "discord.lead_channel": LEAD_CHANNEL,
        "discord.approvers": [APPROVER_A, APPROVER_B],
        "discord.operator_role": ROLE,
    },
}


def _hand_written(token: object, *, enabled: bool = True, channel: str = LEAD_CHANNEL) -> dict:
    """The shape of a hand-written block on a deployed member (synthetic values)."""
    return {
        "enabled": enabled,
        "token": copy.deepcopy(token),
        "groupPolicy": "allowlist",
        "guilds": {GUILD: {"channels": {channel: {"enabled": True}}}},
    }


def _member(root: Path, name: str, index: int, discord: dict | None, env: str) -> dict:
    env_file = root / name / "member.env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(env, encoding="utf-8")
    team = root / name / "team.yml"
    team.write_text("team: sample\n", encoding="utf-8")
    (root / name / "workspace").mkdir(exist_ok=True)
    openclaw: dict = {"lead_agent_id": name}
    if discord is not None:
        openclaw["discord"] = discord
    return {
        "name": name,
        "host": "h",
        "role": "product_owner" if index == 0 else "developer",
        "workspace_path": str(root / name / "workspace"),
        "state_path": str(root / name / "state"),
        "team_definition_path": str(team),
        "quadlet_path": f"{name}.container",
        "container_name": f"{name}-ctr",
        "image": {"repository": "ghcr.io/openclaw/openclaw", "tag": "2026.9.9"},
        "env_files": [str(env_file)],
        "ports": [
            {
                "bind_address": "127.0.0.1",
                "host_port": 18789 + index * 10,
                "container_port": 18789,
                "protocol": "tcp",
            }
        ],
        "gateway_runtime": {"enabled": True, "bind": "loopback"},
        "dashboard": {"friendly_name": name},
        "openclaw": openclaw,
    }


def _team(
    root: Path,
    *,
    lead: dict | None = LEAD_DISCORD,
    engineer: dict | None = None,
    lead_env: str | None = None,
    site: dict | None = SITE,
) -> Path:
    if engineer is None:
        engineer = {"enabled": False}
    if lead_env is None:
        lead_env = (
            f"# synthetic\nOPENCLAW_GATEWAY_TOKEN={GATEWAY_CANARY}\nDISCORD_BOT_TOKEN={CANARY}\n"
        )
    data: dict = {
        "version": 1,
        "cluster": {"name": "example-cad", "primary_host": "h"},
        "hosts": [{"name": "h", "quadlet_root": str(root / "quadlets")}],
        "instances": [
            _member(root, "lead", 0, copy.deepcopy(lead), lead_env),
            _member(root, "engineer", 1, engineer, f"OPENCLAW_GATEWAY_TOKEN={GATEWAY_CANARY}\n"),
        ],
    }
    if site is not None:
        data["site_values"] = "private/site.json"
        path = root / "private" / "site.json"
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.write_text(json.dumps(site, indent=2), encoding="utf-8")
        path.chmod(FILE_MODE)
    cfg = root / "inventory.yml"
    cfg.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return cfg


def _instance(cfg: Path, name: str):
    inventory = load_inventory(cfg)
    return inventory, next(item for item in inventory.instances if item.name == name)


def _live_config(root: Path, name: str) -> Path:
    return root / name / "state" / "openclaw.json"


def _write_live(root: Path, name: str, payload: dict) -> Path:
    path = _live_config(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _assert_no_private_values(text: str) -> None:
    for value in (*SITE_IDS, CANARY, GATEWAY_CANARY):
        assert value not in text


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeHost:
    fake = FakeHost(["lead", "engineer"])
    fake.patch(monkeypatch)
    monkeypatch.chdir(tmp_path)
    return fake


# --- model validation (ADR-005 item 3) --------------------------------------------------


def _spec(**changes: object) -> dict:
    base: dict = {
        "enabled": True,
        "token_env": "DISCORD_BOT_TOKEN",
        "guilds": [{"id": GUILD, "channels": [{"id": LEAD_CHANNEL, "users": [APPROVER_A]}]}],
    }
    base.update(changes)
    return base


def _guild(**changes: object) -> list[dict]:
    guild: dict = {"id": GUILD, "channels": [{"id": LEAD_CHANNEL, "users": [APPROVER_A]}]}
    guild.update(changes)
    return [guild]


INVALID = [
    ("dm_open", _spec(dm_policy="open"), "dm_policy"),
    ("group_open", _spec(group_policy="open"), "group_policy"),
    ("user_name", _spec(guilds=_guild(users=["alice"])), "numeric Discord snowflake"),
    ("user_tag", _spec(guilds=_guild(users=["alice#0001"])), "numeric Discord snowflake"),
    ("user_star", _spec(guilds=_guild(users=["*"])), "numeric Discord snowflake"),
    ("role_name", _spec(guilds=_guild(roles=["operators"])), "numeric Discord snowflake"),
    ("short_id", _spec(guilds=_guild(id="1234")), "numeric Discord snowflake"),
    ("long_id", _spec(guilds=_guild(id="1" * 21)), "numeric Discord snowflake"),
    ("int_id", _spec(guilds=_guild(id=100000000000000001)), "valid string"),
    (
        "star_channel",
        _spec(guilds=_guild(channels=[{"id": "*", "users": [APPROVER_A]}])),
        "snowflake",
    ),
    ("app_name", _spec(application_id="my-bot"), "numeric Discord snowflake"),
    ("dm_star", _spec(dm_policy="allowlist", allow_from=["*"]), "numeric Discord snowflake"),
    ("name_matching", _spec(dangerouslyAllowNameMatching=True), "Extra inputs"),
    ("name_matching_snake", _spec(dangerously_allow_name_matching=True), "Extra inputs"),
    ("guild_name_matching", _spec(guilds=_guild(dangerouslyAllowNameMatching=True)), "Extra"),
    ("allowlist_without_allow_from", _spec(dm_policy="allowlist"), "allow_from is required"),
    ("allowlist_empty", _spec(dm_policy="allowlist", allow_from=[]), "allow_from is required"),
    ("allow_from_disabled", _spec(allow_from=[APPROVER_A]), "only allowed with dm_policy"),
    (
        "allow_from_pairing",
        _spec(dm_policy="pairing", allow_from=[APPROVER_A]),
        "only allowed with dm_policy",
    ),
    (
        "channel_without_allowlist",
        _spec(guilds=_guild(channels=[{"id": LEAD_CHANNEL}])),
        "guilds[0].channels[0]: group_policy allowlist requires non-empty users or roles",
    ),
    (
        "channel_empty_allowlist",
        _spec(guilds=_guild(channels=[{"id": LEAD_CHANNEL, "users": [], "roles": []}])),
        "guilds[0].channels[0]: group_policy allowlist requires",
    ),
    (
        "guild_without_channels_or_allowlist",
        _spec(guilds=[{"id": GUILD}]),
        "guilds[0]: group_policy allowlist requires non-empty users or roles",
    ),
    ("disabled_with_token", {"enabled": False, "token_env": "DISCORD_BOT_TOKEN"}, "token_env"),
    ("disabled_with_policy", {"enabled": False, "dm_policy": "disabled"}, "dm_policy"),
    ("disabled_with_guilds", {"enabled": False, "guilds": []}, "allows no other key"),
    (
        "duplicate_guild",
        _spec(guilds=[*_guild(), *_guild()]),
        "duplicate guild id in discord.guilds[1]",
    ),
    (
        "duplicate_channel",
        _spec(
            guilds=_guild(
                channels=[
                    {"id": LEAD_CHANNEL, "users": [APPROVER_A]},
                    {"id": LEAD_CHANNEL, "users": [APPROVER_B]},
                ]
            )
        ),
        "duplicate channel id in discord.guilds[0].channels[1]",
    ),
    (
        "duplicate_user",
        _spec(guilds=_guild(users=[APPROVER_A, APPROVER_A])),
        "must not contain duplicate IDs",
    ),
    ("token_env_lowercase", _spec(token_env="discord_token"), "token_env"),
    ("token_env_dollar", _spec(token_env="${DISCORD_BOT_TOKEN}"), "token_env"),
    ("token_env_missing", {"enabled": True, "guilds": []}, "token_env is required"),
    # OpenClaw SecretRef id pattern ^[A-Z][A-Z0-9_]{0,127}$ (follow-up 2026-10-19).
    ("token_env_leading_underscore", _spec(token_env="_DISCORD_BOT_TOKEN"), "token_env"),
    ("token_env_leading_digit", _spec(token_env="1DISCORD"), "token_env"),
    ("token_env_129_chars", _spec(token_env="D" * 129), "token_env"),
    # configWrites is fixed to false and never configurable (ADR-005 amendment item 8).
    ("config_writes_snake", _spec(config_writes=True), "Extra inputs"),
    ("config_writes_camel", _spec(configWrites=True), "Extra inputs"),
    ("disabled_config_writes", {"enabled": False, "config_writes": False}, "Extra inputs"),
    ("token_literal", _spec(token="x"), "Extra inputs"),
    ("accounts", _spec(accounts={}), "Extra inputs"),
]


@pytest.mark.parametrize(("case", "data", "message"), INVALID, ids=[row[0] for row in INVALID])
def test_discord_model_rejects(case: str, data: dict, message: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        DiscordSpec.model_validate(data)

    text = str(excinfo.value)
    assert message in text
    for value in (GUILD, LEAD_CHANNEL, APPROVER_A, APPROVER_B, APPLICATION):
        assert value not in text


VALID = [
    ("disabled_only", {"enabled": False}),
    (
        "guild_roles_cover_channel",
        _spec(guilds=_guild(roles=[ROLE], channels=[{"id": LEAD_CHANNEL}])),
    ),
    ("guild_users_without_channels", _spec(guilds=[{"id": GUILD, "users": [APPROVER_A]}])),
    (
        "disabled_channel_needs_no_allowlist",
        _spec(
            guilds=_guild(
                channels=[
                    {"id": LEAD_CHANNEL, "users": [APPROVER_A]},
                    {"id": ENGINEER_CHANNEL, "enabled": False},
                ]
            )
        ),
    ),
    ("group_disabled", _spec(group_policy="disabled", guilds=[{"id": GUILD}])),
    ("dm_allowlist", _spec(dm_policy="allowlist", allow_from=[APPROVER_A])),
    ("dm_pairing", _spec(dm_policy="pairing")),
    ("no_guilds", _spec(guilds=[])),
    ("twenty_digits", _spec(application_id="1" * 20)),
    ("token_env_128_chars", _spec(token_env="D" * 128)),
    ("token_env_single_letter", _spec(token_env="D")),
]


@pytest.mark.parametrize(("case", "data"), VALID, ids=[row[0] for row in VALID])
def test_discord_model_accepts(case: str, data: dict) -> None:
    DiscordSpec.model_validate(data)


def test_discord_model_defaults() -> None:
    spec = DiscordSpec.model_validate(_spec())

    assert spec.dm_policy == "disabled"
    assert spec.group_policy == "allowlist"
    assert spec.guilds[0].require_mention is True
    assert spec.guilds[0].channels[0].require_mention is None
    assert spec.guilds[0].channels[0].enabled is True


def test_model_error_names_inventory_path_and_site_name(tmp_path: Path) -> None:
    site = copy.deepcopy(SITE)
    site["values"]["discord.approvers"] = ["alice", APPROVER_B]
    cfg = _team(tmp_path, site=site)

    with pytest.raises(ValueError) as excinfo:
        load_inventory(cfg)

    text = str(excinfo.value)
    assert "numeric Discord snowflake" in text
    assert "site value 'discord.approvers' at instances[lead].openclaw.discord.allow_from" in text
    assert "alice" not in text
    _assert_no_private_values(text)


def test_missing_allowlist_error_text(tmp_path: Path) -> None:
    lead = copy.deepcopy(LEAD_DISCORD)
    del lead["guilds"][0]["roles"]
    del lead["guilds"][0]["channels"][0]["users"]
    cfg = _team(tmp_path, lead=lead)

    with pytest.raises(ValueError) as excinfo:
        load_inventory(cfg)

    assert (
        "discord.guilds[0].channels[0]: group_policy allowlist requires non-empty users or "
        "roles on the channel or its guild (no channel open to the whole guild)"
    ) in str(excinfo.value)
    _assert_no_private_values(str(excinfo.value))


# --- rendering (ADR-005 item 4) ---------------------------------------------------------


def _golden(name: str, payload: object) -> None:
    text = json.dumps(payload, indent=2) + "\n"
    path = GOLDEN / name
    if os.environ.get("CLAWAKE_UPDATE_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert path.read_text(encoding="utf-8") == text


def test_rendered_discord_objects_match_golden(tmp_path: Path) -> None:
    cfg = _team(tmp_path)
    inventory = load_inventory(cfg)
    lead, engineer = inventory.instances

    _golden("lead.channels-discord.json", desired_fields(inventory, lead)["channels/discord"])
    _golden(
        "engineer.channels-discord.json", desired_fields(inventory, engineer)["channels/discord"]
    )
    assert desired_fields(inventory, engineer)["channels/discord"] == {"enabled": False}


def test_rendering_is_deterministic_and_uses_openclaw_names(tmp_path: Path) -> None:
    cfg = _team(tmp_path)
    first = desired_fields(*_instance(cfg, "lead"))["channels/discord"]
    second = desired_fields(*_instance(cfg, "lead"))["channels/discord"]

    assert json.dumps(first) == json.dumps(second)
    assert first["token"] == SECRET_REF
    assert list(first) == [
        "enabled",
        "token",
        "applicationId",
        "dmPolicy",
        "allowFrom",
        "groupPolicy",
        "configWrites",
        "guilds",
    ]
    guild = first["guilds"][GUILD]
    assert list(guild) == ["requireMention", "roles", "channels"]
    assert guild["channels"][LEAD_CHANNEL] == {
        "enabled": True,
        "requireMention": True,
        "users": [APPROVER_A, APPROVER_B],
    }


def test_rendering_omits_empty_lists_and_unset_optionals(tmp_path: Path) -> None:
    lead = {
        "enabled": True,
        "token_env": "DISCORD_BOT_TOKEN",
        "guilds": [
            {
                "id": GUILD,
                "users": [APPROVER_A],
                "roles": [],
                "channels": [{"id": LEAD_CHANNEL, "users": []}],
            }
        ],
    }
    cfg = _team(tmp_path, lead=lead, site=None)

    rendered = desired_fields(*_instance(cfg, "lead"))["channels/discord"]

    assert rendered == {
        "enabled": True,
        "token": SECRET_REF,
        "dmPolicy": "disabled",
        "groupPolicy": "allowlist",
        "configWrites": False,
        "guilds": {
            GUILD: {
                "requireMention": True,
                "users": [APPROVER_A],
                "channels": {LEAD_CHANNEL: {"enabled": True}},
            }
        },
    }


def test_enabled_render_always_disables_config_writes(tmp_path: Path) -> None:
    # ADR-005 amendment item 8: chat-initiated /config set|unset must stay off.
    minimal = DiscordSpec.model_validate(_spec(guilds=[]))
    cfg = _team(tmp_path)
    inventory = load_inventory(cfg)
    lead, engineer = inventory.instances

    assert minimal.runtime_config()["configWrites"] is False
    assert desired_fields(inventory, lead)["channels/discord"]["configWrites"] is False
    assert DiscordSpec.model_validate({"enabled": False}).runtime_config() == {"enabled": False}
    assert desired_fields(inventory, engineer)["channels/discord"] == {"enabled": False}


def test_member_without_discord_leaves_channels_discord_unowned(tmp_path: Path) -> None:
    cfg = _team(tmp_path, lead=None, engineer={"enabled": False})
    inventory, lead = _instance(cfg, "lead")
    hand_written = _hand_written(CANARY)
    _write_live(tmp_path, "lead", {"channels": {"discord": hand_written}})

    plan = plan_runtime_files(inventory, lead)

    assert "channels/discord" not in desired_fields(inventory, lead)
    config = next(content for path, content, _p, _k in plan if path.name == "openclaw.json")
    assert json.loads(config)["channels"]["discord"] == hand_written
    ledger = next(content for path, content, _p, _k in plan if path.name == STATE_FILE)
    assert "channels/discord" not in json.loads(ledger)["fields"]
    assert all(not key.startswith("channels/discord") for _p, _c, _o, keys in plan for key in keys)


def test_fresh_member_gets_discord_rendered_and_owned(tmp_path: Path) -> None:
    cfg = _team(tmp_path)
    inventory, lead = _instance(cfg, "lead")
    # An existing channels object, so the preview descends below channels/.
    _write_live(tmp_path, "lead", {"channels": {}})

    plan = {
        path.name: (content, keys)
        for path, content, _p, keys in plan_runtime_files(inventory, lead)
    }

    config, keys = plan["openclaw.json"]
    assert json.loads(config)["channels"]["discord"]["token"] == SECRET_REF
    assert "channels/discord (added)" in keys
    assert "channels/discord" in json.loads(plan[STATE_FILE][0])["fields"]
    _assert_no_private_values(" ".join(keys))


# --- token env check --------------------------------------------------------------------


def test_env_file_keys_returns_names_only(tmp_path: Path) -> None:
    path = tmp_path / "member.env"
    path.write_text(
        "# comment\n"
        f"OPENCLAW_GATEWAY_TOKEN={GATEWAY_CANARY}\n"
        f"  DISCORD_BOT_TOKEN={CANARY}\n"
        f"export EXPORTED={CANARY}\n"
        f"SPACED = {CANARY}\n"
        f"{CANARY}\n"
        "lower_case=1\n"
        "BAD-NAME=1\n"
        "\n",
        encoding="utf-8",
    )

    keys = env_file_keys(path)

    assert keys == frozenset({"OPENCLAW_GATEWAY_TOKEN", "DISCORD_BOT_TOKEN", "lower_case"})
    assert CANARY not in repr(keys)
    assert GATEWAY_CANARY not in repr(keys)


def test_env_file_keys_ignores_undecodable_values(tmp_path: Path) -> None:
    path = tmp_path / "member.env"
    path.write_bytes(b"DISCORD_BOT_TOKEN=\xff\xfe" + CANARY.encode() + b"\n")

    assert env_file_keys(path) == frozenset({"DISCORD_BOT_TOKEN"})


def test_setup_preview_fails_closed_without_token_env_key(tmp_path: Path, host: FakeHost) -> None:
    cfg = _team(tmp_path, lead_env=f"OPENCLAW_GATEWAY_TOKEN={GATEWAY_CANARY}\n")
    before = _snapshot(tmp_path)

    preview = _invoke("setup", "-c", str(cfg))
    execute = _invoke("setup", "-c", str(cfg), "--execute")

    for result in (preview, execute):
        assert result.exit_code == 2, result.output
        assert "discord.token_env DISCORD_BOT_TOKEN of member lead is not declared" in result.output
        _assert_no_private_values(result.output)
        assert result.exception is None or CANARY not in str(result.exception)
    assert _snapshot(tmp_path) == before
    assert host.calls == []


def test_token_env_check_names_missing_env_file(tmp_path: Path, host: FakeHost) -> None:
    cfg = _team(tmp_path)
    (tmp_path / "lead" / "member.env").unlink()

    result = _invoke("setup", "-c", str(cfg))

    assert result.exit_code == 2, result.output
    assert "not declared" in result.output
    assert "missing" in result.output


def test_token_canary_never_leaves_the_env_file(tmp_path: Path, host: FakeHost) -> None:
    cfg = _team(tmp_path)

    preview = _invoke("setup", "-c", str(cfg))
    execute = _invoke("setup", "-c", str(cfg), "--execute")

    assert preview.exit_code == 0, preview.output
    assert execute.exit_code == 0, execute.output
    for result in (preview, execute):
        assert CANARY not in result.output
        assert result.exception is None
    for relative, content in _snapshot(tmp_path).items():
        if relative == "lead/member.env":
            continue
        assert CANARY.encode() not in content, relative
    ledger = json.loads((tmp_path / "lead" / "state" / STATE_FILE).read_text())
    assert ledger["fields"]["channels/discord"]["token"] == SECRET_REF
    _assert_no_private_values(preview.output)


# --- adoption (ADR-005 item 5) ----------------------------------------------------------


def test_setup_without_flag_refuses_unowned_block(tmp_path: Path, host: FakeHost) -> None:
    cfg = _team(tmp_path)
    _write_live(tmp_path, "lead", {"channels": {"discord": _hand_written(SECRET_REF)}})
    before = _snapshot(tmp_path)

    result = _invoke("setup", "-c", str(cfg), "-m", "lead")

    assert result.exit_code == 1, result.output
    assert (
        "Refusing to overwrite unmanaged OpenClaw field: channels/discord (named adoption "
        "'discord-channel': review the preview, then run setup with --adopt discord-channel)"
    ) in result.output
    _assert_no_private_values(result.output)
    assert _snapshot(tmp_path) == before


def test_adoption_refuses_string_token_with_instructions(tmp_path: Path, host: FakeHost) -> None:
    cfg = _team(tmp_path)
    config = _write_live(tmp_path, "lead", {"channels": {"discord": _hand_written(CANARY)}})
    before = _snapshot(tmp_path)

    preview = _invoke("setup", "-c", str(cfg), "-m", "lead", "--adopt", "discord-channel")
    execute = _invoke(
        "setup", "-c", str(cfg), "-m", "lead", "--adopt", "discord-channel", "--execute"
    )

    for result in (preview, execute):
        assert result.exit_code == 1, result.output
        assert (
            f"Cannot adopt channels/discord of member lead: the live token in {config} is a "
            "plaintext string. Move it into the member env file as DISCORD_BOT_TOKEN, replace "
            'channels.discord.token with {"source": "env", "provider": "default", "id": '
            '"DISCORD_BOT_TOKEN"}, then re-run setup --adopt discord-channel. Clawake never '
            "reads, moves or deletes token values."
        ) in " ".join(result.output.split())
        _assert_no_private_values(result.output)
    assert _snapshot(tmp_path) == before
    assert not (tmp_path / ".backups").exists()


def test_disabled_member_string_token_instructions(tmp_path: Path, host: FakeHost) -> None:
    cfg = _team(tmp_path)
    _write_live(
        tmp_path,
        "engineer",
        {"channels": {"discord": _hand_written(CANARY, enabled=False, channel=ENGINEER_CHANNEL)}},
    )

    result = _invoke("setup", "-c", str(cfg), "-m", "engineer", "--adopt", "discord-channel")

    assert result.exit_code == 1, result.output
    text = " ".join(result.output.split())
    assert "the live token in" in text
    assert "Discord is disabled for this member: remove channels.discord.token" in text
    _assert_no_private_values(result.output)


def test_adoption_preview_redacts_ids_and_mutates_nothing(tmp_path: Path, host: FakeHost) -> None:
    cfg = _team(tmp_path)
    _write_live(tmp_path, "lead", {"channels": {"discord": _hand_written(SECRET_REF)}})
    before = _snapshot(tmp_path)

    result = _invoke("setup", "-c", str(cfg), "-m", "lead", "--adopt", "discord-channel")

    assert result.exit_code == 0, result.output
    out = result.output
    assert (
        "channels/discord/allowFrom (added), channels/discord/applicationId (added), "
        "channels/discord/configWrites (added), channels/discord/dmPolicy (added), "
        "channels/discord/guilds/<id>/channels/<id>/requireMention (added), "
        "channels/discord/guilds/<id>/channels/<id>/users (added), "
        "channels/discord/guilds/<id>/requireMention (added), "
        "channels/discord/guilds/<id>/roles (added)"
    ) in out
    assert (
        " - lead: adopt discord-channel (backup first: .backups/lead-<UTC timestamp>.tar.gz)"
    ) in out
    _assert_no_private_values(out)
    assert _snapshot(tmp_path) == before
    assert host.calls == []
    assert not (tmp_path / ".backups").exists()


def test_adoption_preview_lists_removed_and_changed_paths(tmp_path: Path, host: FakeHost) -> None:
    cfg = _team(tmp_path)
    live = _hand_written({**SECRET_REF, "id": "OLD_DISCORD_TOKEN"})
    live["guilds"][GUILD]["channels"][ENGINEER_CHANNEL] = {"enabled": True}
    live["guilds"][GUILD]["channels"][LEAD_CHANNEL]["allow"] = True
    _write_live(tmp_path, "lead", {"channels": {"discord": live}})

    result = _invoke("setup", "-c", str(cfg), "-m", "lead", "--adopt", "discord-channel")

    assert result.exit_code == 0, result.output
    assert "channels/discord/guilds/<id>/channels/<id> (removed)" in result.output
    assert "channels/discord/guilds/<id>/channels/<id>/allow (removed)" in result.output
    assert "channels/discord/token/id (changed)" in result.output
    _assert_no_private_values(result.output)


def test_adoption_execute_backs_up_first_then_replaces_and_owns(
    tmp_path: Path, host: FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _team(tmp_path)
    hand_written = _hand_written(SECRET_REF)
    config = _write_live(tmp_path, "lead", {"channels": {"discord": hand_written}, "keep": 1})
    events: list[tuple[str, object]] = []
    real_backup = cli.backup_instance
    real_apply = cli.apply_artifacts

    def backup(instance, output_dir, execute=False):
        events.append(("backup", json.loads(config.read_text())["channels"]["discord"]))
        return real_backup(instance, output_dir, execute=execute)

    def apply(changes):
        events.append(("apply", None))
        return real_apply(changes)

    monkeypatch.setattr(cli, "backup_instance", backup)
    monkeypatch.setattr(cli, "apply_artifacts", apply)

    result = _invoke(
        "setup", "-c", str(cfg), "-m", "lead", "--adopt", "discord-channel", "--execute"
    )

    assert result.exit_code == 0, result.output
    assert events == [("backup", hand_written), ("apply", None)]
    assert len(list((tmp_path / ".backups").glob("lead-*.tar.gz"))) == 1
    payload = json.loads(config.read_text())
    inventory, lead = _instance(cfg, "lead")
    expected = desired_fields(inventory, lead)["channels/discord"]
    assert payload["channels"]["discord"] == expected
    assert payload["keep"] == 1
    ledger = json.loads((tmp_path / "lead" / "state" / STATE_FILE).read_text())
    assert ledger["fields"]["channels/discord"] == expected
    assert pending_adoptions(inventory, lead) == ()
    _assert_no_private_values(result.output)

    # A second run with the flag is a no-op: nothing changed, no backup, no restart.
    calls = list(host.calls)
    snapshot = _snapshot(tmp_path)
    second = _invoke(
        "setup", "-c", str(cfg), "-m", "lead", "--adopt", "discord-channel", "--execute"
    )
    assert second.exit_code == 0, second.output
    assert "0/1 member(s) changed" in second.output
    assert "adopt discord-channel" not in second.output
    assert len(list((tmp_path / ".backups").glob("lead-*.tar.gz"))) == 1
    # setup always reloads systemd; nothing was restarted or started.
    assert host.calls[len(calls) :] == [("daemon-reload", "")]
    assert _snapshot(tmp_path) == snapshot


def test_backup_failure_stops_adoption_before_any_write(
    tmp_path: Path, host: FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _team(tmp_path)
    _write_live(tmp_path, "lead", {"channels": {"discord": _hand_written(SECRET_REF)}})
    before = _snapshot(tmp_path)

    def failing_backup(instance, output_dir, execute=False):
        raise OSError("disk full")

    monkeypatch.setattr(cli, "backup_instance", failing_backup)

    result = _invoke(
        "setup", "-c", str(cfg), "-m", "lead", "--adopt", "discord-channel", "--execute"
    )

    assert result.exit_code == 1, result.output
    assert "Backup before adopting discord-channel for lead failed: disk full" in result.output
    assert _snapshot(tmp_path) == before
    assert host.calls == []


def test_tar_error_in_adoption_backup_stops_cleanly_before_any_write(
    tmp_path: Path, host: FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #34: a TarError is a clean error like an OSError, not a traceback.
    cfg = _team(tmp_path)
    _write_live(tmp_path, "lead", {"channels": {"discord": _hand_written(SECRET_REF)}})
    before = _snapshot(tmp_path)

    def failing_backup(instance, output_dir, execute=False):
        raise tarfile.TarError("synthetic archive failure")

    monkeypatch.setattr(cli, "backup_instance", failing_backup)

    result = _invoke(
        "setup", "-c", str(cfg), "-m", "lead", "--adopt", "discord-channel", "--execute"
    )

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    assert "Traceback" not in result.output
    assert (
        "Backup before adopting discord-channel for lead failed: synthetic archive failure. "
        "Nothing was changed." in result.output
    )
    assert _snapshot(tmp_path) == before
    assert host.calls == []


def test_drift_after_adoption_trips_the_guard(tmp_path: Path, host: FakeHost) -> None:
    cfg = _team(tmp_path)
    _write_live(tmp_path, "lead", {"channels": {"discord": _hand_written(SECRET_REF)}})
    first = _invoke(
        "setup", "-c", str(cfg), "-m", "lead", "--adopt", "discord-channel", "--execute"
    )
    assert first.exit_code == 0, first.output
    config = _live_config(tmp_path, "lead")
    payload = json.loads(config.read_text())
    payload["channels"]["discord"]["groupPolicy"] = "open"
    config.write_text(json.dumps(payload), encoding="utf-8")

    for args in ([], ["--adopt", "discord-channel"]):
        result = _invoke("setup", "-c", str(cfg), "-m", "lead", *args)
        assert result.exit_code == 1, result.output
        assert "Managed field changed outside Clawake: channels/discord" in result.output


def test_unknown_adoption_name_is_rejected(tmp_path: Path, host: FakeHost) -> None:
    cfg = _team(tmp_path)

    result = _invoke("setup", "-c", str(cfg), "--adopt", "everything")

    assert result.exit_code == 2, result.output
    assert "Unknown adoption 'everything'" in result.output
    assert "discord-channel" in result.output


def test_rollout_refuses_pending_adoption(tmp_path: Path, host: FakeHost) -> None:
    cfg = _team(tmp_path)
    _write_live(tmp_path, "lead", {"channels": {"discord": _hand_written(SECRET_REF)}})
    before = _snapshot(tmp_path)

    preview = _invoke("rollout", "-c", str(cfg))
    execute = _invoke("rollout", "-c", str(cfg), "--execute")
    flag = _invoke("rollout", "-c", str(cfg), "--adopt", "discord-channel")

    for result in (preview, execute):
        assert result.exit_code == 1, result.output
        assert "Refusing to overwrite unmanaged OpenClaw field: channels/discord" in result.output
        assert "--adopt discord-channel" in result.output
        _assert_no_private_values(result.output)
    assert flag.exit_code == 2, flag.output
    assert _snapshot(tmp_path) == before
    assert host.calls == []


def test_pending_adoptions_is_read_only_and_names_the_rule(tmp_path: Path) -> None:
    cfg = _team(tmp_path)
    inventory, lead = _instance(cfg, "lead")
    assert pending_adoptions(inventory, lead) == ()
    _write_live(tmp_path, "lead", {"channels": {"discord": _hand_written(SECRET_REF)}})
    before = _snapshot(tmp_path)

    assert pending_adoptions(inventory, lead) == ("discord-channel",)
    assert _snapshot(tmp_path) == before


def test_equal_unowned_block_is_owned_without_flag(tmp_path: Path) -> None:
    cfg = _team(tmp_path)
    inventory, lead = _instance(cfg, "lead")
    rendered = desired_fields(inventory, lead)["channels/discord"]
    _write_live(tmp_path, "lead", {"channels": {"discord": rendered}})

    plan = plan_runtime_files(inventory, lead)

    assert pending_adoptions(inventory, lead) == ()
    ledger = next(content for path, content, _p, _k in plan if path.name == STATE_FILE)
    assert json.loads(ledger)["fields"]["channels/discord"] == rendered


# --- redaction --------------------------------------------------------------------------


def test_no_site_value_appears_in_any_preview(tmp_path: Path, host: FakeHost) -> None:
    cfg = _team(tmp_path)
    site_values = [
        item
        for value in SITE["values"].values()
        for item in (value if isinstance(value, list) else [value])
    ]
    outputs = [
        _invoke("setup", "-c", str(cfg)).output,
        _invoke("validate", "-c", str(cfg)).output,
        _invoke("rollout", "-c", str(cfg)).output,
    ]
    _write_live(tmp_path, "lead", {"channels": {"discord": _hand_written(SECRET_REF)}})
    outputs.append(_invoke("setup", "-c", str(cfg), "--adopt", "discord-channel").output)
    outputs.append(_invoke("setup", "-c", str(cfg)).output)

    assert "channels/discord/guilds/<id>/roles (added)" in outputs[3]
    assert "Refusing to overwrite unmanaged OpenClaw field: channels/discord" in outputs[4]
    for output in outputs:
        assert output
        for value in site_values:
            assert value not in output


def test_plan_keys_are_redacted_for_ids_outside_site_values(tmp_path: Path) -> None:
    lead = {
        "enabled": True,
        "token_env": "DISCORD_BOT_TOKEN",
        "guilds": [{"id": GUILD, "channels": [{"id": LEAD_CHANNEL, "users": [APPROVER_A]}]}],
    }
    cfg = _team(tmp_path, lead=lead, site=None)
    inventory, member = _instance(cfg, "lead")
    _write_live(tmp_path, "lead", {"channels": {"discord": _hand_written(SECRET_REF)}})

    changes = plan_deployment(inventory, [member], TEMPLATES, adopt=("discord-channel",))

    keys = [key for change in changes for key in change.changed_keys]
    assert "channels/discord/dmPolicy (added)" in keys
    assert "channels/discord/guilds/<id>/channels/<id>/users (added)" in keys
    _assert_no_private_values(" ".join(keys))


# --- unchanged behaviour ----------------------------------------------------------------

FOUNDATION = Path(__file__).parent / "fixtures" / "rollout-foundation" / "team.yml"


def _foundation_renders(cfg: Path, state_root: Path) -> dict[str, str]:
    from clawake.services.render import render_instance_assets

    inventory = load_inventory(cfg)
    artifacts: dict[str, str] = {}
    for instance in inventory.instances:
        for path, content in render_instance_assets(instance, TEMPLATES).items():
            artifacts[f"quadlet/{path}"] = content
        redirected = instance.model_copy(
            update={"state_path": str(state_root / instance.name / "state")}
        )
        artifacts[f"desired/{instance.name}"] = json.dumps(
            desired_fields(inventory, redirected), indent=2
        )
        for path, content, _previous, keys in plan_runtime_files(inventory, redirected):
            artifacts[f"runtime/{instance.name}/{path.name}"] = content
            artifacts[f"keys/{instance.name}/{path.name}"] = "\n".join(keys)
    return artifacts


def test_site_values_without_discord_render_byte_identical(tmp_path: Path) -> None:
    data = yaml.safe_load(FOUNDATION.read_text(encoding="utf-8"))
    data["site_values"] = "site.json"
    data["instances"][0]["dashboard"]["owner"] = "${site:owner}"
    with_site = tmp_path / "with-site" / "team.yml"
    with_site.parent.mkdir()
    with_site.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    site = with_site.parent / "site.json"
    site.write_text(
        json.dumps({"version": 1, "cluster": "example-cad", "values": {"owner": "example"}}),
        encoding="utf-8",
    )
    site.chmod(FILE_MODE)

    expected = _foundation_renders(FOUNDATION, tmp_path / "a")
    actual = _foundation_renders(with_site, tmp_path / "a")

    assert actual == expected
    assert all("discord" not in key for key in actual)
