from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import cost_aware_model_router.cli as cli_module
from cost_aware_model_router.cli import _build_gateway_for_serve, build_parser, main
from cost_aware_model_router.gateway import OpenAIRouterGateway
from cost_aware_model_router.governance import TenantAuthRegistry, TenantRole


class _FakeClock:
    def __init__(self, initial: float = 100.0) -> None:
        self.now = initial

    def __call__(self) -> float:
        return self.now


def test_registry_to_json_from_json_round_trip_preserves_auth_rbac_and_digests() -> None:
    clock = _FakeClock()
    registry = TenantAuthRegistry(clock=clock)
    client_token, client_rec = registry.issue_key(
        "org_alpha",
        TenantRole.ROUTER_CLIENT,
        rate_limit_rps=10.0,
        burst_capacity=2,
    )
    admin_token, admin_rec = registry.issue_key(
        "org_beta",
        TenantRole.ADMIN,
        rate_limit_rps=50.0,
        burst_capacity=100,
    )
    disabled_token, disabled_rec = registry.issue_key("org_gamma", TenantRole.AUDITOR)
    registry.disable_key(disabled_rec.key_id)

    records = registry.list_records()
    assert [r.key_id for r in records] == sorted(r.key_id for r in records)

    raw_json = registry.to_json()
    assert client_token not in raw_json
    assert admin_token not in raw_json
    assert disabled_token not in raw_json
    assert client_token.split(".", 1)[1] not in raw_json
    assert admin_token.split(".", 1)[1] not in raw_json
    assert client_rec.key_sha256 in raw_json
    assert admin_rec.key_sha256 in raw_json

    restored = TenantAuthRegistry.from_json(raw_json, clock=clock)
    assert restored.list_records() == records

    client_ok = restored.authenticate_and_authorize(
        f"Bearer {client_token}",
        required_role=TenantRole.ROUTER_CLIENT,
        claimed_org_id="org_alpha",
    )
    assert client_ok.status_code == 200
    assert client_ok.authorized is True

    client_forbidden = restored.authenticate_and_authorize(
        f"Bearer {client_token}",
        required_role=TenantRole.ADMIN,
    )
    assert client_forbidden.status_code == 403
    assert client_forbidden.authorized is False

    admin_ok = restored.authenticate_and_authorize(
        f"Bearer {admin_token}",
        required_role=TenantRole.AUDITOR,
        claimed_org_id="org_beta",
    )
    assert admin_ok.status_code == 200
    assert admin_ok.authorized is True

    disabled_res = restored.authenticate_and_authorize(
        f"Bearer {disabled_token}",
        required_role=TenantRole.AUDITOR,
    )
    assert disabled_res.status_code == 403
    assert disabled_res.authenticated is True
    assert disabled_res.authorized is False


def test_registry_from_json_rejects_duplicate_key_id_and_invalid_shape() -> None:
    registry = TenantAuthRegistry()
    _, rec = registry.issue_key("org_dup", TenantRole.ROUTER_CLIENT)
    payload = json.loads(registry.to_json())
    payload["records"].append(payload["records"][0])

    with pytest.raises(ValueError, match=f"Duplicate key_id in auth config: {rec.key_id}"):
        TenantAuthRegistry.from_json(json.dumps(payload))

    with pytest.raises(ValueError, match="expected an object with a 'records' list"):
        TenantAuthRegistry.from_json('{"records": "not-a-list"}')


def test_build_parser_issue_key_and_serve_auth_config_defaults() -> None:
    parser = build_parser()
    issue_args = parser.parse_args(["issue-key", "--org-id", "org_one", "--out", "auth.json"])
    assert issue_args.command == "issue-key"
    assert issue_args.org_id == "org_one"
    assert issue_args.role == TenantRole.ROUTER_CLIENT.value
    assert issue_args.rate_limit_rps == pytest.approx(20.0)
    assert issue_args.burst_capacity == 40
    assert issue_args.out == Path("auth.json")

    serve_args = parser.parse_args(["serve", "--auth-config", "auth.json"])
    assert serve_args.command == "serve"
    assert serve_args.auth_config == Path("auth.json")


def _parse_kv_output(stdout: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in stdout.strip().splitlines():
        key, value = line.split("=", 1)
        parsed[key] = value
    return parsed


def test_cli_issue_key_creates_and_appends_and_serve_authenticates(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth_path = tmp_path / "nested" / "auth-registry.json"

    rc1 = main(
        [
            "issue-key",
            "--org-id",
            "org_acme",
            "--role",
            "router_client",
            "--rate-limit-rps",
            "15.5",
            "--burst-capacity",
            "30",
            "--out",
            str(auth_path),
        ]
    )
    assert rc1 == 0
    assert auth_path.is_file()
    out1 = _parse_kv_output(capsys.readouterr().out)
    token1 = out1["token"]
    assert out1["org_id"] == "org_acme"
    assert out1["role"] == "router_client"
    assert out1["out"] == str(auth_path)
    assert token1.startswith(f"{out1['key_prefix']}.")

    rc2 = main(
        [
            "issue-key",
            "--org-id",
            "org_ops",
            "--role",
            "admin",
            "--out",
            str(auth_path),
        ]
    )
    assert rc2 == 0
    out2 = _parse_kv_output(capsys.readouterr().out)
    token2 = out2["token"]
    assert out2["org_id"] == "org_ops"
    assert out2["role"] == "admin"

    persisted_text = auth_path.read_text(encoding="utf-8")
    assert token1 not in persisted_text
    assert token2 not in persisted_text
    assert token1.split(".", 1)[1] not in persisted_text
    assert token2.split(".", 1)[1] not in persisted_text

    persisted_json = json.loads(persisted_text)
    assert len(persisted_json["records"]) == 2
    digests = {rec["key_sha256"] for rec in persisted_json["records"]}
    assert digests == {
        hashlib.sha256(token1.encode("utf-8")).hexdigest(),
        hashlib.sha256(token2.encode("utf-8")).hexdigest(),
    }

    loaded_registry = TenantAuthRegistry.from_json(persisted_text)
    gateway = _build_gateway_for_serve(
        policy_id="threshold-balanced",
        auth_registry=loaded_registry,
    )
    assert gateway.auth_registry is loaded_registry
    assert (
        gateway.auth_registry.authenticate_and_authorize(
            f"Bearer {token1}",
            required_role=TenantRole.ROUTER_CLIENT,
            claimed_org_id="org_acme",
        ).status_code
        == 200
    )

    captured_gateways: list[tuple[OpenAIRouterGateway, str, int]] = []
    fake_server = MagicMock()
    fake_server.server_address = ("127.0.0.1", 0)

    def _fake_create_server(gw: OpenAIRouterGateway, *, host: str, port: int) -> MagicMock:
        captured_gateways.append((gw, host, port))
        return fake_server

    monkeypatch.setattr(cli_module, "create_router_http_server", _fake_create_server)
    rc_serve = main(["serve", "--auth-config", str(auth_path), "--port", "0"])
    assert rc_serve == 0
    assert len(captured_gateways) == 1
    served_gateway = captured_gateways[0][0]
    assert served_gateway.auth_registry is not None
    assert (
        served_gateway.auth_registry.authenticate_and_authorize(
            f"Bearer {token2}",
            required_role=TenantRole.ADMIN,
            claimed_org_id="org_ops",
        ).status_code
        == 200
    )
