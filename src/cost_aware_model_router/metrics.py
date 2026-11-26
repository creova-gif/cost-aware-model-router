from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation

from cost_aware_model_router.schemas import format_usd_str
from cost_aware_model_router.telemetry import OTelAttributeValue, OTelSpanRecord
from cost_aware_model_router.thermostat import SpendSummary


def escape_prometheus_label_value(value: str) -> str:
    """Escape backslash, double-quote, and newline characters for Prometheus label values."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_labels(labels: dict[str, str]) -> str:
    parts = [f'{k}="{escape_prometheus_label_value(v)}"' for k, v in labels.items()]
    return "{" + ",".join(parts) + "}"


def _attr_str(val: OTelAttributeValue | None, default: str = "unknown") -> str:
    if val is None:
        return default
    if isinstance(val, bool):
        return "true" if val else "false"
    return str(val)


def _attr_decimal(val: OTelAttributeValue | None) -> Decimal:
    if val is None or isinstance(val, bool):
        return Decimal("0")
    try:
        return Decimal(str(val))
    except InvalidOperation:
        return Decimal("0")


_CIRCUIT_STATE_VALUES: dict[str, int] = {
    "closed": 0,
    "half_open": 1,
    "open": 2,
}


def render_prometheus_metrics(
    spans: list[OTelSpanRecord],
    summary: SpendSummary | None = None,
    circuits: dict[str, str] | None = None,
) -> str:
    """Render router OTel spans, optional thermostat summary, and circuit states in Prometheus 0.0.4 text format."""
    req_counts: dict[tuple[str, str, str, str], int] = defaultdict(int)
    spend_totals: dict[tuple[str, str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    latency_sums: dict[tuple[str, str], float] = defaultdict(float)
    latency_counts: dict[tuple[str, str], int] = defaultdict(int)

    for span in spans:
        attrs = span.attributes
        org_id = _attr_str(attrs.get("cost_router.org_id"))
        tier = _attr_str(attrs.get("cost_router.selected_tier"))
        provenance = _attr_str(attrs.get("cost_router.cost_provenance"))
        shadow_mode = _attr_str(attrs.get("cost_router.shadow_mode"), default="false")
        cost_usd = _attr_decimal(attrs.get("cost_router.cost_usd"))
        latency_ms = max(0.0, (span.end_time_unix_nano - span.start_time_unix_nano) / 1_000_000.0)

        req_counts[(org_id, tier, provenance, shadow_mode)] += 1
        spend_totals[(org_id, tier, provenance)] += cost_usd
        latency_sums[(org_id, tier)] += latency_ms
        latency_counts[(org_id, tier)] += 1

    lines: list[str] = []

    lines.append("# HELP cost_router_requests_total Total number of routed model requests.")
    lines.append("# TYPE cost_router_requests_total counter")
    for (org_id, tier, provenance, shadow_mode), count in sorted(req_counts.items()):
        lbl = _format_labels(
            {
                "org_id": org_id,
                "tier": tier,
                "provenance": provenance,
                "shadow_mode": shadow_mode,
            }
        )
        lines.append(f"cost_router_requests_total{lbl} {count}")

    lines.append("# HELP cost_router_spend_usd_total Total accumulated LLM spend in USD.")
    lines.append("# TYPE cost_router_spend_usd_total counter")
    for (org_id, tier, provenance), total_usd in sorted(spend_totals.items()):
        lbl = _format_labels(
            {
                "org_id": org_id,
                "tier": tier,
                "provenance": provenance,
            }
        )
        lines.append(f"cost_router_spend_usd_total{lbl} {format_usd_str(total_usd)}")

    lines.append("# HELP cost_router_latency_ms Request latency in milliseconds.")
    lines.append("# TYPE cost_router_latency_ms summary")
    for org_id, tier in sorted(latency_counts.keys()):
        lbl = _format_labels({"org_id": org_id, "tier": tier})
        lines.append(f"cost_router_latency_ms_sum{lbl} {latency_sums[(org_id, tier)]:.6f}")
        lines.append(f"cost_router_latency_ms_count{lbl} {latency_counts[(org_id, tier)]}")

    if summary is not None:
        lines.append("# HELP cost_router_cache_hits_total Total semantic prompt cache hits.")
        lines.append("# TYPE cost_router_cache_hits_total counter")
        lines.append(f"cost_router_cache_hits_total {summary.cache_hits}")

        if summary.cost_per_successful_outcome_usd is not None:
            lines.append(
                "# HELP cost_router_goodput_usd Cost per successful outcome (goodput) in USD."
            )
            lines.append("# TYPE cost_router_goodput_usd gauge")
            lines.append(
                f"cost_router_goodput_usd {format_usd_str(summary.cost_per_successful_outcome_usd)}"
            )

    if circuits:
        lines.append(
            "# HELP cost_router_circuit_state "
            "Provider circuit breaker state (0=closed, 1=half_open, 2=open)."
        )
        lines.append("# TYPE cost_router_circuit_state gauge")
        for tier, state in sorted(circuits.items()):
            lbl = _format_labels({"tier": tier})
            val = _CIRCUIT_STATE_VALUES.get(state, -1)
            lines.append(f"cost_router_circuit_state{lbl} {val}")

    return "\n".join(lines) + "\n"
