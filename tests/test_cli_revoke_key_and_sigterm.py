from __future__ import annotations

import json
import os
import signal
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import cost_aware_model_router.cli as cli_module
from cost_aware_model_router.cli import build_parser, main
from cost_aware_model_router.gateway import OpenAIRouterGateway
from cost_aware_model_router.governance import TenantAuthRegistry, TenantRole


def _parse_kv_output(stdout: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in stdout.strip().splitlines():
        key, value = line.split("=", 1)
        parsed[key] = value
    return parsed


def test_build_parser_revoke_key() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["revoke-key", "--key-id", "key_123", "--auth-config", "/tmp/auth.json"]
    )
    assert args.command == "revoke-key"
    assert args.key_id == "key_123"
    assert args.auth_config == Path("/tmp/auth.json")


def test_cli_issue_key_then_revoke_key_disables_key_in_json_and_rejects_token(
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
            "--out",
            str(auth_path),
        ]
    )
    assert rc_issue == 0
    issue_out = _parse_kv_output(capsys.readouterr().out)
    key_id = issue_out["key_id"]
    raw_token = issue_out["token"]

    rc_revoke = main(
        [
            "revoke-key",
            "--key-id",
            key_id,
            "--auth-config",
            str(auth_path),
        ]
    )
    assert rc_revoke == 0
    revoke_out = _parse_kv_output(capsys.readouterr().out)
    assert revoke_out == {
        "key_id": key_id,
        "org_id": "org_acme",
        "enabled": "false",
        "auth_config": str(auth_path),
    }

    persisted_text = auth_path.read_text(encoding="utf-8")
    payload = json.loads(persisted_text)
    assert len(payload["records"]) == 1
    assert payload["records"][0]["key_id"] == key_id
    assert payload["records"][0]["enabled"] is False

    reloaded = TenantAuthRegistry.from_json(persisted_text)
    decision = reloaded.authenticate_and_authorize(
        f"Bearer {raw_token}",
        required_role=TenantRole.ROUTER_CLIENT,
        claimed_org_id="org_acme",
    )
    assert decision.authorized is False
    assert decision.status_code == 403
    assert decision.reason == "API key is disabled"


def test_cli_revoke_key_returns_1_on_missing_file_and_unknown_key_id(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing_path = tmp_path / "does-not-exist.json"
    rc_missing = main(
        [
            "revoke-key",
            "--key-id",
            "key_missing",
            "--auth-config",
            str(missing_path),
        ]
    )
    assert rc_missing == 1
    err_missing = capsys.readouterr().err
    assert "Key revocation failed:" in err_missing

    auth_path = tmp_path / "auth-registry.json"
    rc_issue = main(
        [
            "issue-key",
            "--org-id",
            "org_acme",
            "--out",
            str(auth_path),
        ]
    )
    assert rc_issue == 0
    capsys.readouterr()

    rc_unknown = main(
        [
            "revoke-key",
            "--key-id",
            "key_does_not_exist",
            "--auth-config",
            str(auth_path),
        ]
    )
    assert rc_unknown == 1
    err_unknown = capsys.readouterr().err
    assert "Unknown key_id: key_does_not_exist" in err_unknown


def test_serve_sigterm_triggers_shutdown_and_restores_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "economics.sqlite3"
    shutdown_called = threading.Event()
    fake_server = MagicMock()
    fake_server.server_address = ("127.0.0.1", 0)

    def _fake_shutdown() -> None:
        shutdown_called.set()

    def _fake_serve_forever() -> None:
        os.kill(os.getpid(), signal.SIGTERM)
        assert shutdown_called.wait(timeout=2.0)

    fake_server.shutdown.side_effect = _fake_shutdown
    fake_server.serve_forever.side_effect = _fake_serve_forever

    def _fake_create_server(_gw: OpenAIRouterGateway, *, host: str, port: int) -> MagicMock:
        return fake_server

    monkeypatch.setattr(cli_module, "create_router_http_server", _fake_create_server)

    prior_handler = signal.getsignal(signal.SIGTERM)
    rc = main(["serve", "--db-path", str(db_path), "--port", "0"])

    assert rc == 0
    assert shutdown_called.is_set()
    fake_server.shutdown.assert_called_once()
    fake_server.server_close.assert_called_once()
    assert signal.getsignal(signal.SIGTERM) is prior_handler
