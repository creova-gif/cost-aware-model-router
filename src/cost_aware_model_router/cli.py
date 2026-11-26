from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

from cost_aware_model_router.adapters import (
    AnthropicAdapter,
    DeterministicFixtureAdapter,
    OpenAICompatibleAdapter,
    ProviderAdapter,
    generate_bundled_workload,
    load_bundled_catalog,
)
from cost_aware_model_router.gateway import (
    ChatCompletionRequest,
    ChatMessage,
    OpenAIRouterGateway,
    ThermostatBlockedError,
)
from cost_aware_model_router.judge import (
    HoldoutGeneralizationReport,
    OutOfSamplePolicyAuditor,
)
from cost_aware_model_router.policies import (
    build_evaluation_matrix,
    default_policy_suite,
    evaluate_policy_suite,
)
from cost_aware_model_router.receipt import (
    build_savings_receipt,
    render_receipt_markdown,
    verify_receipt_file,
)
from cost_aware_model_router.report import render_pareto_explorer_html
from cost_aware_model_router.schemas import (
    ModelTier,
    PolicySpec,
    PricingCatalog,
    SavingsReceipt,
    TaskCategory,
    WorkloadItem,
    format_usd_str,
)
from cost_aware_model_router.server import create_router_http_server
from cost_aware_model_router.telemetry import (
    InMemorySpanExporter,
    load_policies_from_json,
    split_workload_holdout,
)
from cost_aware_model_router.thermostat import (
    AgentBudgetThermostat,
    BudgetPolicy,
    SpendSummary,
)


def load_workload_jsonl(path: Path) -> list[WorkloadItem]:
    items: list[WorkloadItem] = []
    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            data = json.loads(stripped)
            items.append(WorkloadItem.model_validate(data))
        except Exception as exc:
            raise ValueError(f"Invalid workload item at {path}:{line_no}: {exc}") from exc
    if not items:
        raise ValueError(f"Workload file {path} contains 0 items")
    return items


async def run_pipeline(
    *,
    workload_name: str,
    workload: list[WorkloadItem],
    catalog: PricingCatalog,
    adapter: ProviderAdapter,
    out_dir: Path,
    quality_floor: float = 0.92,
    policies: list[PolicySpec] | None = None,
) -> SavingsReceipt:
    traces = await adapter.execute_workload(workload, catalog)
    (
        eval_matrix,
        matrix_map,
        complete_ids,
        excluded_ids,
        unknown_burned,
    ) = build_evaluation_matrix(workload, traces)
    active_policies = policies if policies is not None else default_policy_suite()
    policy_results, frontier_ids = evaluate_policy_suite(
        workload,
        active_policies,
        matrix_map,
        complete_ids,
        excluded_ids,
        unknown_burned,
    )
    receipt = build_savings_receipt(
        workload_name=workload_name,
        workload=workload,
        catalog=catalog,
        evaluation_matrix=eval_matrix,
        policy_results=policy_results,
        pareto_optimal_ids=frontier_ids,
        complete_case_count=len(complete_ids),
        excluded_unknown_count=len(excluded_ids),
        default_quality_floor=quality_floor,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    md_text = render_receipt_markdown(receipt)
    html_text = render_pareto_explorer_html(receipt, md_text)
    json_text = json.dumps(json.loads(receipt.model_dump_json()), indent=2, sort_keys=True)

    (out_dir / "savings-receipt.json").write_text(json_text + "\n", encoding="utf-8")
    (out_dir / "savings-receipt.md").write_text(md_text, encoding="utf-8")
    (out_dir / "pareto-explorer.html").write_text(html_text, encoding="utf-8")
    return receipt


def _print_receipt_summary(receipt: SavingsReceipt, out_dir: Path) -> None:
    by_id = {r.policy_id: r for r in receipt.policy_results}
    base = by_id[receipt.baseline_policy_id]
    rec = by_id[receipt.recommended_policy_id]
    print(
        f"Workload: {receipt.workload_name} ({receipt.complete_case_requests} complete, {receipt.excluded_unknown_requests} excluded unknown)"
    )
    print(
        f"Baseline ({base.policy_id}): ${format_usd_str(base.total_cost_usd)} | pass={base.pass_rate * 100:.1f}%"
    )
    print(
        f"Recommended ({rec.policy_id}): ${format_usd_str(rec.total_cost_usd)} "
        f"({rec.cost_savings_vs_baseline_pct:+.1f}% savings) | pass={rec.pass_rate * 100:.1f}%"
    )
    print(f"2D Pareto Frontier Policies: {', '.join(receipt.pareto_optimal_policy_ids)}")
    print(f"Integrity SHA-256: {receipt.integrity.digest}")
    print(f"Artifacts written to: {out_dir}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cost-router",
        description="Cost-Aware Model Router — 3-Tier Pareto Optimizer & Auditable Savings Receipt CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    demo_p = sub.add_parser(
        "demo", help="Run the zero-API-key reproducible 60-prompt 3-tier Pareto demo"
    )
    demo_p.add_argument(
        "--out",
        type=Path,
        default=Path("./artifacts/demo"),
        help="Output directory for pareto-explorer.html and savings-receipt.{json,md}",
    )
    demo_p.add_argument(
        "--quality-floor",
        type=float,
        default=0.92,
        help="Minimum pass rate floor q_min in [0.0, 1.0] (default: 0.92)",
    )

    replay_p = sub.add_parser(
        "replay", help="Replay a JSONL workload against the 3-tier model catalog"
    )
    replay_p.add_argument(
        "--workload", type=Path, required=True, help="Path to workload JSONL file"
    )
    replay_p.add_argument(
        "--catalog", type=Path, default=None, help="Optional custom catalog JSON file"
    )
    replay_p.add_argument(
        "--policy-file", type=Path, default=None, help="Optional custom policy suite JSON file"
    )
    replay_p.add_argument(
        "--holdout-fraction",
        type=float,
        default=0.0,
        help="Optional deterministic holdout fraction in (0.0, 1.0) for out-of-sample evaluation",
    )
    replay_p.add_argument(
        "--out", type=Path, required=True, help="Output directory for generated artifacts"
    )
    replay_p.add_argument(
        "--quality-floor", type=float, default=0.92, help="Minimum pass rate floor q_min"
    )
    replay_p.add_argument(
        "--live", action="store_true", help="Execute against live LLM HTTP endpoint"
    )
    replay_p.add_argument(
        "--provider",
        choices=["openai", "anthropic"],
        default="openai",
        help="Live LLM provider adapter to use with --live (default: openai)",
    )
    replay_p.add_argument(
        "--base-url",
        type=str,
        default="https://api.openai.com/v1",
        help="Provider API base URL",
    )
    replay_p.add_argument(
        "--api-key-env", type=str, default="OPENAI_API_KEY", help="Env var holding API key"
    )
    replay_p.add_argument(
        "--concurrency", type=int, default=8, help="Max concurrent live requests (default: 8)"
    )
    replay_p.add_argument(
        "--max-cost-usd", type=str, default="5.00000000", help="Live spend circuit breaker in USD"
    )

    verify_p = sub.add_parser(
        "verify-receipt", help="Verify canonical SHA-256 integrity and invariants of a receipt"
    )
    verify_p.add_argument("receipt_path", type=Path, help="Path to savings-receipt.json")

    sim_p = sub.add_parser(
        "simulate-thermostat",
        help="Run a multi-agent gateway + thermostat + OTel simulation and write FinOps artifacts",
    )
    sim_p.add_argument(
        "--out",
        type=Path,
        default=Path("./artifacts/thermostat-sim"),
        help="Output directory for thermostat-economics.json, shadow-summary.json, and otel-spans.jsonl",
    )

    holdout_p = sub.add_parser(
        "audit-holdout",
        help="Fit threshold policy on train split and audit out-of-sample generalization on holdout split",
    )
    holdout_p.add_argument(
        "--out",
        type=Path,
        default=Path("./artifacts/holdout-audit"),
        help="Output directory for holdout-generalization.json",
    )
    holdout_p.add_argument(
        "--holdout-fraction",
        type=float,
        default=0.30,
        help="Deterministic holdout fraction in (0.0, 1.0) (default: 0.30)",
    )
    holdout_p.add_argument(
        "--quality-floor",
        type=float,
        default=0.90,
        help="Minimum pass rate floor q_min in [0.0, 1.0] (default: 0.90)",
    )

    serve_p = sub.add_parser(
        "serve",
        help="Start a local OpenAI-compatible HTTP router server",
    )
    serve_p.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Bind host address (default: 127.0.0.1)",
    )
    serve_p.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Bind TCP port (default: 8080)",
    )
    serve_p.add_argument(
        "--policy",
        type=str,
        default="threshold-balanced",
        help="Primary routing policy ID (default: threshold-balanced)",
    )
    serve_p.add_argument(
        "--shadow-policy",
        type=str,
        default=None,
        help="Optional shadow candidate policy ID (e.g. threshold-aggressive)",
    )
    serve_p.add_argument(
        "--hourly-budget-usd",
        type=Decimal,
        default=None,
        help="Optional hourly USD budget to enable AgentBudgetThermostat",
    )

    return parser


async def _run_thermostat_simulation(out_dir: Path) -> SpendSummary:
    policies_by_id = {p.policy_id: p for p in default_policy_suite()}
    exporter = InMemorySpanExporter()
    thermostat = AgentBudgetThermostat(
        policies=[
            BudgetPolicy(
                org_id="org_demo",
                agent_id="reasoning_agent",
                hourly_budget_usd=Decimal("0.03000000"),
                soft_downgrade_ratio=0.45,
                hard_cap_ratio=1.00,
                max_loop_repeats=3,
                min_quality_tier=ModelTier.COMPACT,
            ),
            BudgetPolicy(
                org_id="org_demo",
                agent_id="floor_protected_agent",
                hourly_budget_usd=Decimal("0.03000000"),
                soft_downgrade_ratio=0.50,
                hard_cap_ratio=1.00,
                min_quality_tier=ModelTier.FRONTIER,
            ),
        ]
    )
    gateway = OpenAIRouterGateway(
        primary_policy=policies_by_id["all-frontier"],
        shadow_policy=policies_by_id["threshold-aggressive"],
        span_exporter=exporter,
        thermostat=thermostat,
    )

    sim_requests = [
        ChatCompletionRequest(
            request_id="sim-001",
            category=TaskCategory.REASONING,
            messages=[ChatMessage(role="user", content="Prove quorum invariant step-by-step #1")],
            x_org_id="org_demo",
            x_agent_id="reasoning_agent",
        ),
        # Repeat same prompt -> semantic cache hit ($0)
        ChatCompletionRequest(
            request_id="sim-002",
            category=TaskCategory.REASONING,
            messages=[ChatMessage(role="user", content="Prove quorum invariant step-by-step #1")],
            x_org_id="org_demo",
            x_agent_id="reasoning_agent",
        ),
        # New prompt with utilization >= 50% -> soft step-down to mid
        ChatCompletionRequest(
            request_id="sim-003",
            category=TaskCategory.REASONING,
            messages=[ChatMessage(role="user", content="Prove consensus safety invariant #2")],
            x_org_id="org_demo",
            x_agent_id="reasoning_agent",
        ),
        # Runaway loop repeat -> blocked with ThermostatBlockedError
        ChatCompletionRequest(
            request_id="sim-004",
            category=TaskCategory.REASONING,
            messages=[ChatMessage(role="user", content="Prove quorum invariant step-by-step #1")],
            x_org_id="org_demo",
            x_agent_id="reasoning_agent",
        ),
        # Quality-floor protected agent: first request succeeds, second hits soft cap -> escalated
        ChatCompletionRequest(
            request_id="sim-005",
            category=TaskCategory.JSON_EXTRACTION,
            messages=[ChatMessage(role="user", content="Extract invoice_total for ticket #55")],
            x_org_id="org_demo",
            x_agent_id="floor_protected_agent",
        ),
        ChatCompletionRequest(
            request_id="sim-006",
            category=TaskCategory.JSON_EXTRACTION,
            messages=[ChatMessage(role="user", content="Extract invoice_total for ticket #56")],
            x_org_id="org_demo",
            x_agent_id="floor_protected_agent",
        ),
    ]

    blocked_events: list[dict[str, str]] = []
    for req in sim_requests:
        try:
            await gateway.handle_chat_completion(req)
        except ThermostatBlockedError as exc:
            blocked_events.append(
                {
                    "request_id": exc.decision.request_id,
                    "agent_id": exc.decision.agent_id,
                    "action": exc.decision.action.value,
                    "explanation": exc.decision.explanation,
                }
            )

    out_dir.mkdir(parents=True, exist_ok=True)
    summary = thermostat.summarize_economics()
    econ_payload = summary.model_dump(mode="json")
    econ_payload["blocked_events"] = blocked_events
    (out_dir / "thermostat-economics.json").write_text(
        json.dumps(econ_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out_dir / "shadow-summary.json").write_text(
        json.dumps(gateway.build_shadow_summary(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    exporter.export_jsonl(out_dir / "otel-spans.jsonl")
    return summary


async def _run_holdout_audit(
    out_dir: Path,
    holdout_fraction: float,
    quality_floor: float,
) -> HoldoutGeneralizationReport:
    workload = generate_bundled_workload()
    catalog = load_bundled_catalog()
    adapter = DeterministicFixtureAdapter()
    auditor = OutOfSamplePolicyAuditor(holdout_fraction=holdout_fraction)
    traces = await adapter.execute_workload(workload, catalog)
    report = auditor.audit(workload, traces, quality_floor=quality_floor)

    out_dir.mkdir(parents=True, exist_ok=True)
    json_text = json.dumps(json.loads(report.model_dump_json()), indent=2, sort_keys=True)
    (out_dir / "holdout-generalization.json").write_text(json_text + "\n", encoding="utf-8")
    return report


def _print_holdout_summary(report: HoldoutGeneralizationReport, out_dir: Path) -> None:
    print(
        f"Holdout Audit: train={report.train_requests} | holdout={report.holdout_requests} | "
        f"thresholds=(compact<{report.selected_compact_max:.2f}, mid<{report.selected_mid_max:.2f})"
    )
    print(
        f"Pass Rate: train={report.in_sample_pass_rate * 100:.1f}% | "
        f"holdout={report.out_of_sample_pass_rate * 100:.1f}% "
        f"(delta={report.generalization_pass_delta * 100:+.1f}%)"
    )
    print(
        f"Savings vs Baseline: train={report.in_sample_savings_pct:+.1f}% | "
        f"holdout={report.out_of_sample_savings_pct:+.1f}%"
    )
    print(
        f"holds_quality_floor_out_of_sample={report.holds_quality_floor_out_of_sample} "
        f"(floor={report.quality_floor * 100:.1f}%)"
    )
    print(f"Artifacts written to: {out_dir}")


def _build_gateway_for_serve(
    *,
    policy_id: str,
    shadow_policy_id: str | None = None,
    hourly_budget_usd: Decimal | None = None,
) -> OpenAIRouterGateway:
    suite = default_policy_suite()
    policies_by_id = {p.policy_id: p for p in suite}

    primary_policy = policies_by_id.get(policy_id)
    if primary_policy is None:
        raise ValueError(f"Unknown primary policy {policy_id!r}")
    if not primary_policy.deployable:
        raise ValueError(f"Primary policy {policy_id!r} is marked deployable=False")

    shadow_policy: PolicySpec | None = None
    if shadow_policy_id is not None:
        shadow_policy = policies_by_id.get(shadow_policy_id)
        if shadow_policy is None:
            raise ValueError(f"Unknown shadow policy {shadow_policy_id!r}")
        if not shadow_policy.deployable:
            raise ValueError(f"Shadow policy {shadow_policy_id!r} is marked deployable=False")

    thermostat: AgentBudgetThermostat | None = None
    if hourly_budget_usd is not None:
        thermostat = AgentBudgetThermostat(
            policies=[
                BudgetPolicy(
                    org_id="org_demo",
                    agent_id=None,
                    hourly_budget_usd=hourly_budget_usd,
                )
            ]
        )

    return OpenAIRouterGateway(
        adapter=DeterministicFixtureAdapter(),
        catalog=load_bundled_catalog(),
        primary_policy=primary_policy,
        shadow_policy=shadow_policy,
        policies=suite,
        thermostat=thermostat,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "demo":
        workload = generate_bundled_workload()
        catalog = load_bundled_catalog()
        adapter: ProviderAdapter = DeterministicFixtureAdapter()
        receipt = asyncio.run(
            run_pipeline(
                workload_name="support-and-code-v1.jsonl",
                workload=workload,
                catalog=catalog,
                adapter=adapter,
                out_dir=args.out,
                quality_floor=args.quality_floor,
            )
        )
        _print_receipt_summary(receipt, args.out)
        return 0

    if args.command == "replay":
        try:
            workload = load_workload_jsonl(args.workload)
            if args.holdout_fraction > 0.0:
                _, workload = split_workload_holdout(workload, args.holdout_fraction)
            custom_policies = (
                load_policies_from_json(args.policy_file) if args.policy_file is not None else None
            )
            catalog = (
                PricingCatalog.model_validate(json.loads(args.catalog.read_text(encoding="utf-8")))
                if args.catalog is not None
                else load_bundled_catalog()
            )
            replay_adapter: ProviderAdapter
            if args.live:
                if args.provider == "anthropic":
                    api_key_env = (
                        "ANTHROPIC_API_KEY"
                        if args.api_key_env == "OPENAI_API_KEY"
                        else args.api_key_env
                    )
                    base_url = (
                        "https://api.anthropic.com"
                        if args.base_url == "https://api.openai.com/v1"
                        else args.base_url
                    )
                    api_key = os.environ.get(api_key_env)
                    replay_adapter = AnthropicAdapter(
                        base_url=base_url,
                        api_key=api_key,
                        max_concurrency=args.concurrency,
                        max_cost_usd=Decimal(args.max_cost_usd),
                    )
                else:
                    api_key = os.environ.get(args.api_key_env)
                    replay_adapter = OpenAICompatibleAdapter(
                        base_url=args.base_url,
                        api_key=api_key,
                        max_concurrency=args.concurrency,
                        max_cost_usd=Decimal(args.max_cost_usd),
                    )
            else:
                replay_adapter = DeterministicFixtureAdapter()

            receipt = asyncio.run(
                run_pipeline(
                    workload_name=args.workload.name,
                    workload=workload,
                    catalog=catalog,
                    adapter=replay_adapter,
                    out_dir=args.out,
                    quality_floor=args.quality_floor,
                    policies=custom_policies,
                )
            )
        except Exception as exc:
            print(f"Replay failed: {exc}", file=sys.stderr)
            return 1
        _print_receipt_summary(receipt, args.out)
        return 0

    if args.command == "verify-receipt":
        ok, msg = verify_receipt_file(args.receipt_path)
        if ok:
            print(f"VERIFIED: {msg}")
            return 0
        print(f"INVALID: {msg}", file=sys.stderr)
        return 1

    if args.command == "simulate-thermostat":
        summary = asyncio.run(_run_thermostat_simulation(args.out))
        print(
            f"Simulated {summary.total_requests} requests ({summary.cache_hits} cache hits) | "
            f"Total spend: ${format_usd_str(summary.total_cost_usd)}"
        )
        print(f"Artifacts written to: {args.out}")
        return 0

    if args.command == "audit-holdout":
        try:
            report = asyncio.run(
                _run_holdout_audit(
                    out_dir=args.out,
                    holdout_fraction=args.holdout_fraction,
                    quality_floor=args.quality_floor,
                )
            )
        except Exception as exc:
            print(f"Holdout audit failed: {exc}", file=sys.stderr)
            return 1
        _print_holdout_summary(report, args.out)
        return 0

    if args.command == "serve":
        try:
            gateway = _build_gateway_for_serve(
                policy_id=args.policy,
                shadow_policy_id=args.shadow_policy,
                hourly_budget_usd=args.hourly_budget_usd,
            )
            server = create_router_http_server(gateway, host=args.host, port=args.port)
        except Exception as exc:
            print(f"Failed to start server: {exc}", file=sys.stderr)
            return 1
        raw_host, bound_port = server.server_address[:2]
        bound_host = raw_host.decode("utf-8") if isinstance(raw_host, bytes) else raw_host
        print(f"cost-router listening on http://{bound_host}:{bound_port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
