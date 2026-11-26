from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest

from cost_aware_model_router.cli import (
    _build_gateway_for_serve,
    _load_budget_policies,
    build_parser,
)
from cost_aware_model_router.gateway import (
    ChatCompletionRequest,
    ChatMessage,
    ThermostatBlockedError,
)
from cost_aware_model_router.thermostat import BudgetPolicy


def test_serve_parser_access_log_and_budget_config_flags(tmp_path: Path) -> None:
    budget_file = tmp_path / "budgets.json"
    parser = build_parser()

    default_args = parser.parse_args(["serve"])
    assert default_args.access_log is False
    assert default_args.budget_config is None

    args = parser.parse_args(
        [
            "serve",
            "--access-log",
            "--budget-config",
            str(budget_file),
        ]
    )
    assert args.command == "serve"
    assert args.access_log is True
    assert args.budget_config == budget_file


def test_load_budget_policies_list_and_object_formats(tmp_path: Path) -> None:
    payload = [
        {"org_id": "org_a", "hourly_budget_usd": "1.50"},
        {"org_id": "org_b", "agent_id": "agent_1", "hourly_budget_usd": "5.00"},
    ]

    list_path = tmp_path / "list.json"
    list_path.write_text(json.dumps(payload), encoding="utf-8")
    loaded_list = _load_budget_policies(list_path)
    assert len(loaded_list) == 2
    assert loaded_list[0].org_id == "org_a"
    assert loaded_list[0].hourly_budget_usd == Decimal("1.50000000")
    assert loaded_list[1].org_id == "org_b"
    assert loaded_list[1].agent_id == "agent_1"

    obj_path = tmp_path / "obj.json"
    obj_path.write_text(json.dumps({"policies": payload}), encoding="utf-8")
    loaded_obj = _load_budget_policies(obj_path)
    assert loaded_obj == loaded_list


@pytest.mark.parametrize(
    "raw_content",
    [
        "not-valid-json",
        "[]",
        '{"policies": []}',
        '{"other": []}',
        '{"policies": "not-a-list"}',
        "123",
        json.dumps([{"org_id": "org_a", "hourly_budget_usd": "-1.00"}]),
        json.dumps([{"org_id": "", "hourly_budget_usd": "1.00"}]),
    ],
)
def test_load_budget_policies_rejects_invalid_or_empty(
    tmp_path: Path, raw_content: str
) -> None:
    bad_path = tmp_path / "bad.json"
    bad_path.write_text(raw_content, encoding="utf-8")

    with pytest.raises(ValueError):
        _load_budget_policies(bad_path)


def test_build_gateway_for_serve_enforces_per_tenant_budgets() -> None:
    policies = [
        BudgetPolicy(
            org_id="org_a",
            hourly_budget_usd=Decimal("0.000001"),
            enable_semantic_cache=False,
        ),
        BudgetPolicy(
            org_id="org_b",
            hourly_budget_usd=Decimal("10.00"),
            enable_semantic_cache=False,
        ),
    ]
    gateway = _build_gateway_for_serve(
        policy_id="threshold-balanced",
        budget_policies=policies,
    )

    req_a1 = ChatCompletionRequest(
        model="auto",
        messages=[ChatMessage(role="user", content="Explain circuit breakers in distributed systems.")],
        x_org_id="org_a",
    )
    req_a2 = ChatCompletionRequest(
        model="auto",
        messages=[ChatMessage(role="user", content="Explain Raft consensus in distributed systems.")],
        x_org_id="org_a",
    )
    req_b = ChatCompletionRequest(
        model="auto",
        messages=[ChatMessage(role="user", content="Explain circuit breakers in distributed systems.")],
        x_org_id="org_b",
    )

    async def _run() -> None:
        # First request for org_a spends more than 0.000001 USD; second request is blocked.
        await gateway.handle_chat_completion(req_a1)
        with pytest.raises(ThermostatBlockedError) as exc_info:
            await gateway.handle_chat_completion(req_a2)
        assert exc_info.value.decision.org_id == "org_a"

        # org_b has its own 10.00 USD budget and remains unaffected.
        resp_b = await gateway.handle_chat_completion(req_b)
        assert resp_b.choices

    asyncio.run(_run())
