from __future__ import annotations

import json
from pathlib import Path

import pytest

from cost_aware_model_router.cli import build_parser, main
from cost_aware_model_router.governance import TenantAuthRegistry, TenantRole


def _parse_kv_output(stdout: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in stdout.strip().splitlines():
        key, value = line.split("=", 1)
        parsed[key] = value
    return parsed


def test_registry_rotate_key_disables_old_and_preserves_metadata() -> None:
    registry = TenantAuthRegistry()
    old_token, old_rec = registry.issue_key(
        "org_acme",
        TenantRole.ADMIN,
        rate_limit_rps=12.5,
        burst_capacity=25,
    )

    new_token, new_rec = registry.rotate_key(old_rec.key_id)

    assert new_rec.key_id != old_rec.key_id
    assert new_token != old_token
    assert new_token.startswith(f"crk_{new_rec.key_id}.")
    assert new_rec.key_prefix == f"crk_{new_rec.key_id}"
    assert new_rec.org_id == "org_acme"
    assert new_rec.role is TenantRole.ADMIN
    assert new_rec.rate_limit_rps == pytest.approx(12.5)
    assert new_rec.burst_capacity == 25
    assert new_rec.enabled is True

    records_by_id = {rec.key_id: rec for rec in registry.list_records()}
    assert records_by_id[old_rec.key_id].enabled is False
    assert records_by_id[new_rec.key_id].enabled is True

    old_decision = registry.authenticate_and_authorize(
        f"Bearer {old_token}",
        required_role=TenantRole.ADMIN,
        claimed_org_id="org_acme",
    )
    assert old_decision.authenticated is True
    assert old_decision.authorized is False
    assert old_decision.status_code == 403
    assert old_decision.reason == "API key is disabled"

    new_decision = registry.authenticate_and_authorize(
        f"Bearer {new_token}",
        required_role=TenantRole.ADMIN,
        claimed_org_id="org_acme",
    )
    assert new_decision.authenticated is True
    assert new_decision.authorized is True
    assert new_decision.status_code == 200
    assert new_decision.org_id == "org_acme"
    assert new_decision.role is TenantRole.ADMIN

    with pytest.raises(KeyError, match="Unknown key_id: missing_key"):
        registry.rotate_key("missing_key")


def test_build_parser_rotate_key_and_list_keys() -> None:
    parser = build_parser()

    rotate_args = parser.parse_args(
        ["rotate-key", "--key-id", "key_abc", "--auth-config", "/tmp/auth.json"]
    )
    assert rotate_args.command == "rotate-key"
    assert rotate_args.key_id == "key_abc"
    assert rotate_args.auth_config == Path("/tmp/auth.json")

    list_args = parser.parse_args(["list-keys", "--auth-config", "/tmp/auth.json"])
    assert list_args.command == "list-keys"
    assert list_args.auth_config == Path("/tmp/auth.json")


def test_cli_rotate_key_and_list_keys_end_to_end(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    auth_path = tmp_path / "auth-registry.json"

    rc_issue = main(
        [
            "issue-key",
            "--org-id",
            "org_acme",
            "--role",
            "router_client",
            "--rate-limit-rps",
            "15.0",
            "--burst-capacity",
            "30",
            "--out",
            str(auth_path),
        ]
    )
    assert rc_issue == 0
    issue_out = _parse_kv_output(capsys.readouterr().out)
    old_key_id = issue_out["key_id"]
    old_prefix = issue_out["key_prefix"]
    old_token = issue_out["token"]

    rc_rotate = main(
        [
            "rotate-key",
            "--key-id",
            old_key_id,
            "--auth-config",
            str(auth_path),
        ]
    )
    assert rc_rotate == 0
    rotate_out = _parse_kv_output(capsys.readouterr().out)
    new_key_id = rotate_out["key_id"]
    new_prefix = rotate_out["key_prefix"]
    new_token = rotate_out["token"]

    assert rotate_out["old_key_id"] == old_key_id
    assert new_key_id != old_key_id
    assert rotate_out["org_id"] == "org_acme"
    assert rotate_out["role"] == "router_client"
    assert rotate_out["auth_config"] == str(auth_path)
    assert new_token.startswith(f"{new_prefix}.")

    persisted_text = auth_path.read_text(encoding="utf-8")
    payload = json.loads(persisted_text)
    assert len(payload["records"]) == 2
    by_id = {rec["key_id"]: rec for rec in payload["records"]}
    assert by_id[old_key_id]["enabled"] is False
    assert by_id[new_key_id]["enabled"] is True

    reloaded = TenantAuthRegistry.from_json(persisted_text)
    assert (
        reloaded.authenticate_and_authorize(
            f"Bearer {old_token}",
            required_role=TenantRole.ROUTER_CLIENT,
            claimed_org_id="org_acme",
        ).status_code
        == 403
    )
    assert (
        reloaded.authenticate_and_authorize(
            f"Bearer {new_token}",
            required_role=TenantRole.ROUTER_CLIENT,
            claimed_org_id="org_acme",
        ).status_code
        == 200
    )

    rc_list = main(["list-keys", "--auth-config", str(auth_path)])
    assert rc_list == 0
    list_stdout = capsys.readouterr().out
    lines = list_stdout.strip().splitlines()
    assert len(lines) == 2

    assert old_key_id in list_stdout
    assert old_prefix in list_stdout
    assert new_key_id in list_stdout
    assert new_prefix in list_stdout
    assert "enabled=false" in list_stdout
    assert "enabled=true" in list_stdout
    assert "rate_limit_rps=15.0" in list_stdout
    assert "burst_capacity=30" in list_stdout

    assert old_token not in list_stdout
    assert new_token not in list_stdout
    assert old_token.split(".", 1)[1] not in list_stdout
    assert new_token.split(".", 1)[1] not in list_stdout
    assert by_id[old_key_id]["key_sha256"] not in list_stdout
    assert by_id[new_key_id]["key_sha256"] not in list_stdout
    assert "key_sha256" not in list_stdout


def test_cli_rotate_key_and_list_keys_error_paths(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing_path = tmp_path / "missing.json"

    assert (
        main(["rotate-key", "--key-id", "key_missing", "--auth-config", str(missing_path)])
        == 1
    )
    assert "Key rotation failed:" in capsys.readouterr().err

    assert main(["list-keys", "--auth-config", str(missing_path)]) == 1
    assert "Listing keys failed:" in capsys.readouterr().err

    auth_path = tmp_path / "auth-registry.json"
    assert (
        main(["issue-key", "--org-id", "org_acme", "--out", str(auth_path)]) == 0
    )
    capsys.readouterr()

    assert (
        main(["rotate-key", "--key-id", "unknown_id", "--auth-config", str(auth_path)])
        == 1
    )
    assert "Unknown key_id: unknown_id" in capsys.readouterr().err
