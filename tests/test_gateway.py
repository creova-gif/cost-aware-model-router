from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from cost_aware_model_router.adapters import (
    DeterministicFixtureAdapter,
    generate_bundled_workload,
    load_bundled_catalog,
)
from cost_aware_model_router.gateway import (
    ChatCompletionRequest,
    ChatMessage,
    OpenAIRouterGateway,
    create_asgi_or_httpx_app,
)
from cost_aware_model_router.policies import default_policy_suite
from cost_aware_model_router.schemas import (
    ModelTier,
    PolicyKind,
    PolicySpec,
    TaskCategory,
)
from cost_aware_model_router.telemetry import (
    InMemorySpanExporter,
    OTelSpanRecord,
    load_policies_from_json,
    split_workload_holdout,
)


def _policy_by_id(policy_id: str) -> PolicySpec:
    for p in default_policy_suite():
        if p.policy_id == policy_id:
            return p
    raise KeyError(policy_id)


def test_gateway_chat_completions_and_otel_span_export(tmp_path: Path) -> None:
    exporter = InMemorySpanExporter()
    gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("threshold-balanced"),
        span_exporter=exporter,
    )

    async def _run() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=gateway.as_httpx_transport(),
            base_url="http://router.local",
        ) as client:
            return await client.post(
                "/v1/chat/completions",
                headers={
                    "X-Org-Id": "org_acme",
                    "X-Agent-Id": "invoice_bot",
                },
                json={
                    "model": "auto",
                    "request_id": "req-001",
                    "category": "json_extraction",
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                "Extract customer_id, invoice_total, and currency "
                                "as JSON for ticket #1."
                            ),
                        }
                    ],
                },
            )

    resp = asyncio.run(_run())
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["router_metadata"]["org_id"] == "org_acme"
    assert body["router_metadata"]["agent_id"] == "invoice_bot"
    assert body["router_metadata"]["policy_id"] == "threshold-balanced"
    assert body["router_metadata"]["selected_tier"] == "compact"
    assert body["usage"]["prompt_tokens"] > 0
    assert body["usage"]["completion_tokens"] > 0
    assert Decimal(body["usage"]["cost_usd"]) > Decimal("0")

    spans = exporter.get_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "gen_ai.router.completion"
    assert len(span.trace_id) == 32
    assert len(span.span_id) == 16
    assert span.attributes["cost_router.org_id"] == "org_acme"
    assert span.attributes["cost_router.agent_id"] == "invoice_bot"
    assert span.attributes["cost_router.policy_id"] == "threshold-balanced"
    assert span.attributes["cost_router.selected_tier"] == "compact"
    assert span.attributes["cost_router.shadow_mode"] is False
    assert span.attributes["gen_ai.request.model"] == body["model"]

    jsonl_path = tmp_path / "spans.jsonl"
    exporter.export_jsonl(jsonl_path)
    lines = jsonl_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    reloaded = OTelSpanRecord.model_validate(json.loads(lines[0]))
    assert reloaded == span


def test_shadow_mode_records_candidate_without_mutating_primary_response(
    tmp_path: Path,
) -> None:
    shadow_log = tmp_path / "shadow.jsonl"
    gateway = OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=_policy_by_id("all-frontier"),
        shadow_policy=_policy_by_id("all-compact"),
        shadow_log_path=shadow_log,
    )

    transport = create_asgi_or_httpx_app(gateway)

    async def _run() -> tuple[httpx.Response, httpx.Response]:
        async with httpx.AsyncClient(transport=transport, base_url="http://router.local") as client:
            r1 = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "request_id": "req-005",
                    "category": TaskCategory.JSON_EXTRACTION.value,
                    "messages": [{"role": "user", "content": "Extract invoice #5 JSON."}],
                },
            )
            r2 = await client.get("/v1/finops/shadow-summary")
            return r1, r2

    resp, summary_resp = asyncio.run(_run())
    assert resp.status_code == 200
    payload = resp.json()
    # Production response MUST come from primary policy (all-frontier), unmutated by shadow policy
    assert payload["router_metadata"]["policy_id"] == "all-frontier"
    assert payload["router_metadata"]["selected_tier"] == ModelTier.FRONTIER.value
    assert payload["router_metadata"]["shadow_evaluated"] is True
    assert payload["router_metadata"]["shadow_policy_id"] == "all-compact"

    records = gateway.get_shadow_records()
    assert len(records) == 1
    rec = records[0]
    assert rec.primary_policy_id == "all-frontier"
    assert rec.primary_tier == ModelTier.FRONTIER
    assert rec.shadow_policy_id == "all-compact"
    assert rec.shadow_tier == ModelTier.COMPACT
    assert rec.would_downgrade is True
    assert rec.shadow_cost_delta_usd < Decimal("0")

    assert summary_resp.status_code == 200
    summary = summary_resp.json()
    assert summary["total_shadow_requests"] == 1
    assert summary["downgrade_count"] == 1
    assert Decimal(summary["shadow_cost_delta_usd"]) < Decimal("0")
    assert shadow_log.exists()
    assert len(shadow_log.read_text(encoding="utf-8").strip().splitlines()) == 1


def test_gateway_rejects_non_deployable_oracle_policy() -> None:
    gateway = OpenAIRouterGateway()
    req = ChatCompletionRequest(
        messages=[ChatMessage(role="user", content="Hello")],
        x_policy_id="ear-oracle-tau-0.02",
    )
    with pytest.raises(ValueError, match="deployable=False"):
        asyncio.run(gateway.handle_chat_completion(req))


def test_load_policies_from_json_and_holdout_split(tmp_path: Path) -> None:
    custom_policies = [
        PolicySpec(
            policy_id="custom-compact",
            display_name="Custom Compact",
            kind=PolicyKind.STATIC_TIER,
            target_tier=ModelTier.COMPACT,
        ),
        PolicySpec(
            policy_id="custom-threshold",
            display_name="Custom Threshold",
            kind=PolicyKind.COMPLEXITY_THRESHOLD,
            in_sample_fit=True,
            compact_max_score=0.28,
            mid_max_score=0.62,
        ),
    ]
    policy_file = tmp_path / "policies.json"
    policy_file.write_text(
        json.dumps([p.model_dump(mode="json") for p in custom_policies], indent=2),
        encoding="utf-8",
    )

    loaded = load_policies_from_json(policy_file)
    assert len(loaded) == 2
    assert loaded[0].policy_id == "custom-compact"
    assert loaded[1].in_sample_fit is True

    workload = generate_bundled_workload()
    train, holdout = split_workload_holdout(workload, holdout_fraction=0.30)
    assert len(train) + len(holdout) == len(workload)
    train_ids = {item.request_id for item in train}
    holdout_ids = {item.request_id for item in holdout}
    assert train_ids.isdisjoint(holdout_ids)
    # Deterministic across repeated calls
    train2, holdout2 = split_workload_holdout(workload, holdout_fraction=0.30)
    assert [i.request_id for i in train] == [i.request_id for i in train2]
    assert [i.request_id for i in holdout] == [i.request_id for i in holdout2]
