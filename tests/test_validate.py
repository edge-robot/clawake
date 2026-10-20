"""`validate` on runtime planning refusals (edge-robot/clawake#32, ADR-005 item 5).

`validate` is read-only: a pending named adoption is a notice, every other planning
refusal is a clean `Error: <message>` with exit 1, and no traceback is ever printed.
All IDs and tokens are synthetic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_discord_channel import (
    CANARY,
    SECRET_REF,
    _assert_no_private_values,
    _hand_written,
    _live_config,
    _snapshot,
    _team,
    _write_live,
)
from test_member_ops import FakeHost, _invoke


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeHost:
    fake = FakeHost(["lead", "engineer"])
    fake.patch(monkeypatch)
    monkeypatch.chdir(tmp_path)
    return fake


PENDING_NOTICE = (
    "pending adoption: lead discord-channel (review with: clawake setup -c {cfg} "
    "--adopt discord-channel)"
)


def _assert_clean(result) -> None:
    """No traceback: the command ended through typer.Exit, not an uncaught exception."""
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(
        result.exception
    )
    assert "Traceback" not in result.output


def test_validate_pending_adoption_is_a_notice(tmp_path: Path) -> None:
    cfg = _team(tmp_path)
    _write_live(tmp_path, "lead", {"channels": {"discord": _hand_written(SECRET_REF)}})

    result = _invoke("validate", "-c", str(cfg))

    assert result.exit_code == 0, (result.output, result.exception)
    _assert_clean(result)
    assert result.output.splitlines() == [
        "Valid inventory: example-cad; 2 member(s), 10 artifact change(s).",
        f"site values: {tmp_path / 'private' / 'site.json'} (5 values, 6 references)",
        PENDING_NOTICE.format(cfg=cfg),
    ]
    _assert_no_private_values(result.output)


def test_validate_pending_adoption_with_string_token_is_an_error(tmp_path: Path) -> None:
    cfg = _team(tmp_path)
    config = _write_live(tmp_path, "lead", {"channels": {"discord": _hand_written(CANARY)}})

    result = _invoke("validate", "-c", str(cfg))

    assert result.exit_code == 1, (result.output, result.exception)
    _assert_clean(result)
    text = " ".join(result.output.split())
    assert text.startswith(
        f"Error: Cannot adopt channels/discord of member lead: the live token in {config} is a "
        "plaintext string."
    )
    assert "Valid inventory" not in result.output
    _assert_no_private_values(result.output)


def test_validate_drift_after_adoption_is_an_error(tmp_path: Path, host: FakeHost) -> None:
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

    result = _invoke("validate", "-c", str(cfg))

    assert result.exit_code == 1, (result.output, result.exception)
    _assert_clean(result)
    assert result.output.startswith(
        "Error: Managed field changed outside Clawake: channels/discord"
    )
    _assert_no_private_values(result.output)


def test_validate_without_discord_output_is_unchanged(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with_site = tmp_path / "site"
    with_site.mkdir()
    site_path = with_site / "private" / "site.json"

    plain_result = _invoke("validate", "-c", str(_team(plain, lead=None, site=None)))
    site_result = _invoke("validate", "-c", str(_team(with_site, lead=None)))

    # Expected bytes captured from the base commit 3ec4c60.
    assert plain_result.exit_code == 0
    assert plain_result.output == (
        "Valid inventory: example-cad; 2 member(s), 10 artifact change(s).\n"
    )
    assert site_result.exit_code == 0
    assert site_result.output == (
        "Valid inventory: example-cad; 2 member(s), 10 artifact change(s).\n"
        f"site values: {site_path} (5 values, 0 references)\n"
        f"WARNING: site values file {site_path}: unused values: discord.application_id, "
        "discord.approvers, discord.guild, discord.lead_channel, discord.operator_role\n"
    )


def test_validate_writes_nothing(tmp_path: Path) -> None:
    cfg = _team(tmp_path)
    for token, code in ((SECRET_REF, 0), (CANARY, 1)):
        _write_live(tmp_path, "lead", {"channels": {"discord": _hand_written(token)}})
        before = _snapshot(tmp_path)

        result = _invoke("validate", "-c", str(cfg))

        assert result.exit_code == code, (result.output, result.exception)
        assert _snapshot(tmp_path) == before
    assert not (tmp_path / ".backups").exists()
    assert not (tmp_path / "quadlets").exists()
