from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from cost_aware_model_router.gateway import ShadowComparisonRecord
from cost_aware_model_router.schemas import ModelTier, ProvenanceTag, TaskCategory
from cost_aware_model_router.store import SQLiteEconomicsStore
from cost_aware_model_router.telemetry import OTelSpanRecord
from cost_aware_model_router.thermostat import SpendLedgerEntry


def _make_ledger_entry(request_id: str, cost_usd: str = "0.01000000") -> SpendLedgerEntry:
    return SpendLedgerEntry(
        request_id=request_id,
        org_id="org_alpha",
        agent_id="agent_1",
        category=TaskCategory.CODE_SQL,
        tier=ModelTier.MID,
        provenance=ProvenanceTag.MEASURED,
        passed=True,
        cost_usd=Decimal(cost_usd),
        input_tokens=100,
        output_tokens=50,
        cached=False,
        otel_attributes={"env": "test"},
    )


def _make_shadow_record(request_id: str) -> ShadowComparisonRecord:
    return ShadowComparisonRecord(
        request_id=request_id,
        org_id="org_alpha",
        agent_id="agent_1",
        primary_policy_id="static_frontier",
        primary_tier=ModelTier.FRONTIER,
        primary_cost_usd=Decimal("0.01500000"),
        primary_latency_ms=250.0,
        shadow_policy_id="complexity_threshold_v1",
        shadow_tier=ModelTier.COMPACT,
        shadow_cost_usd=Decimal("0.00250000"),
        shadow_cost_delta_usd=Decimal("-0.01250000"),
        would_downgrade=True,
        explanation="Shadow policy selected compact tier.",
    )


def _make_span(span_id: str) -> OTelSpanRecord:
    return OTelSpanRecord(
        trace_id="0123456789abcdef0123456789abcdef",
        span_id=span_id,
        name="gen_ai.router.completion",
        start_time_unix_nano=1_700_000_000_000_000_000,
        end_time_unix_nano=1_700_000_000_100_000_000,
        attributes={"cost_router.org_id": "org_alpha"},
    )


def test_prune_older_than_deletes_old_events_and_preserves_spend_totals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "retention.db"
    base_epoch = 1_700_000_000

    with SQLiteEconomicsStore(db_path) as store:
        # Old records at t = base_epoch - 200s
        monkeypatch.setattr(
            "cost_aware_model_router.store.time.time_ns",
            lambda: (base_epoch - 200) * 1_000_000_000,
        )
        store.record_ledger_entry(_make_ledger_entry("req-old", cost_usd="0.02000000"))
        store.record_shadow_comparison(_make_shadow_record("shadow-old-1"))
        store.record_shadow_comparison(_make_shadow_record("shadow-old-2"))
        store.record_span(_make_span("0000000000000001"))

        # Recent records at t = base_epoch - 20s
        monkeypatch.setattr(
            "cost_aware_model_router.store.time.time_ns",
            lambda: (base_epoch - 20) * 1_000_000_000,
        )
        store.record_ledger_entry(_make_ledger_entry("req-new", cost_usd="0.03000000"))
        store.record_shadow_comparison(_make_shadow_record("shadow-new-1"))
        store.record_span(_make_span("0000000000000002"))
        store.record_span(_make_span("0000000000000003"))

        deleted = store.prune_older_than(60.0, now_epoch=float(base_epoch))
        assert deleted == {"shadow_comparisons": 2, "otel_spans": 1}

        remaining_shadows = store.load_shadow_comparisons()
        assert [rec.request_id for rec in remaining_shadows] == ["shadow-new-1"]

        remaining_spans = store.load_spans(limit=10)
        assert [span.span_id for span in remaining_spans] == [
            "0000000000000002",
            "0000000000000003",
        ]

        summary = store.summarize_economics()
        assert summary.total_requests == 2
        assert summary.total_cost_usd == Decimal("0.05000000")
        assert summary.spend_by_org == {"org_alpha": Decimal("0.05000000")}

    # Verify deletions were committed to disk
    with SQLiteEconomicsStore(db_path) as reopened:
        assert [rec.request_id for rec in reopened.load_shadow_comparisons()] == ["shadow-new-1"]
        assert [s.span_id for s in reopened.load_spans(limit=10)] == [
            "0000000000000002",
            "0000000000000003",
        ]
        assert reopened.summarize_economics().total_cost_usd == Decimal("0.05000000")


def test_prune_older_than_strict_less_than_cutoff(monkeypatch: pytest.MonkeyPatch) -> None:
    now_epoch = 1_700_000_100
    max_age = 100.0
    cutoff_ns = int((now_epoch - max_age) * 1_000_000_000)

    with SQLiteEconomicsStore(":memory:") as store:
        monkeypatch.setattr("cost_aware_model_router.store.time.time_ns", lambda: cutoff_ns)
        store.record_shadow_comparison(_make_shadow_record("at-cutoff"))
        store.record_span(_make_span("00000000000000aa"))

        monkeypatch.setattr("cost_aware_model_router.store.time.time_ns", lambda: cutoff_ns - 1)
        store.record_shadow_comparison(_make_shadow_record("before-cutoff"))
        store.record_span(_make_span("00000000000000bb"))

        deleted = store.prune_older_than(max_age, now_epoch=float(now_epoch))
        assert deleted == {"shadow_comparisons": 1, "otel_spans": 1}
        assert [r.request_id for r in store.load_shadow_comparisons()] == ["at-cutoff"]
        assert [s.span_id for s in store.load_spans(limit=10)] == ["00000000000000aa"]


@pytest.mark.parametrize("bad_age", [0.0, -1.0, -0.001, float("nan"), float("inf"), float("-inf")])
def test_prune_older_than_rejects_invalid_max_age(bad_age: float) -> None:
    with SQLiteEconomicsStore(":memory:") as store, pytest.raises(
        ValueError, match="max_age_seconds"
    ):
        store.prune_older_than(bad_age)


@pytest.mark.parametrize("bad_now", [float("nan"), float("inf"), float("-inf")])
def test_prune_older_than_rejects_non_finite_now_epoch(bad_now: float) -> None:
    with SQLiteEconomicsStore(":memory:") as store, pytest.raises(
        ValueError, match="now_epoch"
    ):
        store.prune_older_than(10.0, now_epoch=bad_now)


def test_prune_older_than_raises_when_closed() -> None:
    store = SQLiteEconomicsStore(":memory:")
    store.close()
    with pytest.raises(RuntimeError, match="SQLiteEconomicsStore is closed"):
        store.prune_older_than(60.0)
