from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from cost_aware_model_router.adapters import DeterministicFixtureAdapter, load_bundled_catalog
from cost_aware_model_router.cli import _build_gateway_for_serve, build_parser, main
from cost_aware_model_router.gateway import (
    ChatCompletionRequest,
    ChatMessage,
    OpenAIRouterGateway,
)
from cost_aware_model_router.policies import default_policy_suite
from cost_aware_model_router.schemas import ModelTier, PolicySpec, TaskCategory
from cost_aware_model_router.store import SQLiteEconomicsStore
from cost_aware_model_router.thermostat import AgentBudgetThermostat, BudgetPolicy


def _policy_by_id(policy_id: str) -> PolicySpec:
    for policy in default_policy_suite():
        if policy.policy_id == policy_id:
            return policy
    raise KeyError(policy_id)


def test_gateway_thermostat_and_sqlite_store_persistence_and_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "gateway-economics.db"
    thermostat = AgentBudgetThermostat(
        policies=[
            BudgetPolicy(
                org_id="org_acme",
                agent_id="agent_sql",
                hourly_budget_usd=Decimal("0.01500000"),
                soft_downgrade_ratio=0.40,
                hard_cap_ratio=1.00,
                max_loop_repeats=3,
                min_quality_tier=ModelTier.COMPACT,
            )
        ]
    )

    with SQLiteEconomicsStore(db_path) as store:
        gateway = OpenAIRouterGateway(
            adapter=DeterministicFixtureAdapter(),
            catalog=load_bundled_catalog(),
            primary_policy=_policy_by_id("all-frontier"),
            shadow_policy=_policy_by_id("threshold-aggressive"),
            thermostat=thermostat,
            store=store,
        )

        # 1. Normal execution -> populates ledger, shadow comparison, span, and semantic cache
        resp_1 = asyncio.run(
            gateway.handle_chat_completion(
                ChatCompletionRequest(
                    request_id="req-001",
                    category=TaskCategory.CODE_SQL,
                    messages=[
                        ChatMessage(
                            role="user",
                            content="SELECT id, total FROM invoices WHERE status = 'paid';",
                        )
                    ],
                    x_org_id="org_acme",
                    x_agent_id="agent_sql",
                )
            )
        )
        assert resp_1.router_metadata.selected_tier == ModelTier.FRONTIER
        assert resp_1.router_metadata.shadow_evaluated is True

        # 2. Repeat prompt -> CACHE_HIT ($0), persists span and cached ledger entry
        resp_2 = asyncio.run(
            gateway.handle_chat_completion(
                ChatCompletionRequest(
                    request_id="req-002",
                    category=TaskCategory.CODE_SQL,
                    messages=[
                        ChatMessage(
                            role="user",
                            content="SELECT id, total FROM invoices WHERE status = 'paid';",
                        )
                    ],
                    x_org_id="org_acme",
                    x_agent_id="agent_sql",
                )
            )
        )
        assert resp_2.router_metadata.routed_model == "semantic-cache"
        assert resp_2.router_metadata.thermostat_action == "cache_hit"

        # 3. New prompt after soft_downgrade_ratio exceeded -> STEP_DOWN_TIER
        resp_3 = asyncio.run(
            gateway.handle_chat_completion(
                ChatCompletionRequest(
                    request_id="req-003",
                    category=TaskCategory.JSON_EXTRACTION,
                    messages=[
                        ChatMessage(
                            role="user",
                            content="Extract customer_id and amount from receipt #42.",
                        )
                    ],
                    x_org_id="org_acme",
                    x_agent_id="agent_sql",
                )
            )
        )
        assert resp_3.router_metadata.thermostat_action == "step_down_tier"
        assert resp_3.router_metadata.selected_tier == ModelTier.MID

        expected_summary = thermostat.summarize_economics(org_id="org_acme")
        expected_gateway_summary = gateway.summarize_economics(org_id="org_acme")
        assert expected_gateway_summary == expected_summary.model_dump(mode="json")

        expected_shadows = gateway.get_shadow_records()
        expected_spans = gateway.span_exporter.get_spans()
        assert len(expected_shadows) == 2
        assert len(expected_spans) == 3

    # Reopen a fresh SQLiteEconomicsStore on the exact same .db path
    with SQLiteEconomicsStore(db_path) as reopened:
        reopened_summary = reopened.summarize_economics(org_id="org_acme")
        assert reopened_summary == expected_summary
        assert reopened_summary.total_requests == 3
        assert reopened_summary.cache_hits == 1

        reopened_shadows = reopened.load_shadow_comparisons(org_id="org_acme")
        assert reopened_shadows == expected_shadows

        reopened_spans = reopened.load_spans(limit=10)
        assert reopened_spans == expected_spans


def test_gateway_with_store_and_no_thermostat_persists_and_summarizes(tmp_path: Path) -> None:
    db_path = tmp_path / "store-only.db"

    with SQLiteEconomicsStore(db_path) as store:
        gateway = OpenAIRouterGateway(
            adapter=DeterministicFixtureAdapter(),
            catalog=load_bundled_catalog(),
            primary_policy=_policy_by_id("threshold-balanced"),
            shadow_policy=_policy_by_id("threshold-aggressive"),
            store=store,
        )

        asyncio.run(
            gateway.handle_chat_completion(
                ChatCompletionRequest(
                    request_id="req-101",
                    category=TaskCategory.CLASSIFICATION,
                    messages=[ChatMessage(role="user", content="Classify ticket urgency.")],
                    x_org_id="org_beta",
                    x_agent_id="agent_triage",
                )
            )
        )

        econ = gateway.summarize_economics(org_id="org_beta")
        assert econ["total_requests"] == 1
        assert "org_beta" in econ["spend_by_org"]

    with SQLiteEconomicsStore(db_path) as reopened:
        summary = reopened.summarize_economics(org_id="org_beta")
        assert summary.model_dump(mode="json") == econ
        assert len(reopened.load_shadow_comparisons(org_id="org_beta")) == 1
        assert len(reopened.load_spans(limit=10)) == 1


def test_gateway_summarize_economics_requires_thermostat_or_store() -> None:
    gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("threshold-balanced"),
    )
    with pytest.raises(ValueError, match="Thermostat or SQLiteEconomicsStore"):
        gateway.summarize_economics()


def test_cli_serve_db_path_argument_and_wiring(tmp_path: Path) -> None:
    db_path = tmp_path / "cli-serve.db"
    parser = build_parser()
    args = parser.parse_args(["serve", "--db-path", str(db_path)])
    assert args.command == "serve"
    assert args.db_path == db_path

    default_args = parser.parse_args(["serve"])
    assert default_args.db_path is None

    with SQLiteEconomicsStore(db_path) as store:
        gateway = _build_gateway_for_serve(
            policy_id="threshold-balanced",
            shadow_policy_id="threshold-aggressive",
            hourly_budget_usd=Decimal("5.00000000"),
            store=store,
        )
        asyncio.run(
            gateway.handle_chat_completion(
                ChatCompletionRequest(
                    request_id="req-201",
                    messages=[ChatMessage(role="user", content="Classify billing question.")],
                )
            )
        )

    with SQLiteEconomicsStore(db_path) as reopened:
        assert len(reopened.load_ledger_entries()) == 1
        assert len(reopened.load_shadow_comparisons()) == 1
        assert len(reopened.load_spans()) == 1

    # Invalid policy should fail cleanly and still close the opened store
    rc = main(["serve", "--policy", "does-not-exist", "--db-path", str(db_path)])
    assert rc == 1
