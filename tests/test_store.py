from __future__ import annotations

import sqlite3
import threading
from decimal import Decimal
from pathlib import Path

import pytest

from cost_aware_model_router.gateway import ShadowComparisonRecord
from cost_aware_model_router.schemas import ModelTier, ProvenanceTag, TaskCategory
from cost_aware_model_router.store import SQLiteEconomicsStore
from cost_aware_model_router.telemetry import OTelSpanRecord
from cost_aware_model_router.thermostat import SpendLedgerEntry


def _make_ledger_entry(
    *,
    request_id: str,
    org_id: str = "org_alpha",
    agent_id: str = "agent_1",
    category: TaskCategory = TaskCategory.CODE_SQL,
    tier: ModelTier = ModelTier.MID,
    provenance: ProvenanceTag = ProvenanceTag.MEASURED,
    passed: bool = True,
    cost_usd: Decimal = Decimal("0.00123456"),
    input_tokens: int = 120,
    output_tokens: int = 45,
    cached: bool = False,
    otel_attributes: dict[str, str] | None = None,
) -> SpendLedgerEntry:
    return SpendLedgerEntry(
        request_id=request_id,
        org_id=org_id,
        agent_id=agent_id,
        category=category,
        tier=tier,
        provenance=provenance,
        passed=passed,
        cost_usd=cost_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached=cached,
        otel_attributes=otel_attributes if otel_attributes is not None else {"env": "test"},
    )


def _make_shadow_record(
    *,
    request_id: str,
    org_id: str = "org_alpha",
    agent_id: str = "agent_1",
    primary_cost: Decimal = Decimal("0.01500000"),
    shadow_cost: Decimal = Decimal("0.00250000"),
    would_downgrade: bool = True,
) -> ShadowComparisonRecord:
    return ShadowComparisonRecord(
        request_id=request_id,
        org_id=org_id,
        agent_id=agent_id,
        primary_policy_id="static_frontier",
        primary_tier=ModelTier.FRONTIER,
        primary_cost_usd=primary_cost,
        primary_latency_ms=420.5,
        shadow_policy_id="complexity_threshold_v1",
        shadow_tier=ModelTier.COMPACT if would_downgrade else ModelTier.FRONTIER,
        shadow_cost_usd=shadow_cost,
        shadow_cost_delta_usd=shadow_cost - primary_cost,
        would_downgrade=would_downgrade,
        explanation="Shadow policy selected compact tier.",
    )


def test_schema_wal_mode_and_org_indexes(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "economics.db"
    with SQLiteEconomicsStore(db_path) as store:
        store.record_ledger_entry(_make_ledger_entry(request_id="req-1"))

    conn = sqlite3.connect(str(db_path))
    try:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()
        assert journal_mode is not None
        assert str(journal_mode[0]).lower() == "wal"

        indexes = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        assert "idx_spend_ledger_org_id" in indexes
        assert "idx_shadow_comparisons_org_id" in indexes
        assert "idx_otel_spans_org_id" in indexes
    finally:
        conn.close()


def test_persistence_across_close_and_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "durable.db"
    ledger_entry = _make_ledger_entry(
        request_id="req-persist-1",
        cost_usd=Decimal("0.00000007"),
        otel_attributes={"trace_id": "abc123", "region": "us-east-1"},
    )
    shadow_rec = _make_shadow_record(request_id="req-persist-1")
    span_rec = OTelSpanRecord.create_router_span(
        system="openai",
        request_model="gpt-4o-mini",
        input_tokens=100,
        output_tokens=25,
        org_id="org_alpha",
        agent_id="agent_1",
        policy_id="complexity_threshold_v1",
        selected_tier=ModelTier.COMPACT,
        cost_usd=Decimal("0.00000007"),
        cost_provenance=ProvenanceTag.MEASURED,
        shadow_mode=False,
        trace_id="a" * 32,
        span_id="b" * 16,
    )

    store1 = SQLiteEconomicsStore(db_path)
    store1.record_ledger_entry(ledger_entry)
    store1.record_shadow_comparison(shadow_rec)
    store1.record_span(span_rec)
    store1.close()
    # Second close must be idempotent
    store1.close()

    with SQLiteEconomicsStore(db_path) as store2:
        loaded_ledger = store2.load_ledger_entries()
        loaded_shadows = store2.load_shadow_comparisons()
        loaded_spans = store2.load_spans()

        assert loaded_ledger == [ledger_entry]
        assert loaded_shadows == [shadow_rec]
        assert loaded_spans == [span_rec]


def test_org_id_tenant_scoped_queries_and_injection_safety(tmp_path: Path) -> None:
    db_path = tmp_path / "tenants.db"
    alpha_1 = _make_ledger_entry(request_id="a-1", org_id="org_alpha", agent_id="bot_a")
    beta_1 = _make_ledger_entry(request_id="b-1", org_id="org_beta", agent_id="bot_b")
    alpha_2 = _make_ledger_entry(request_id="a-2", org_id="org_alpha", agent_id="bot_a")

    shadow_alpha = _make_shadow_record(request_id="a-1", org_id="org_alpha")
    shadow_beta = _make_shadow_record(request_id="b-1", org_id="org_beta", would_downgrade=False)

    with SQLiteEconomicsStore(db_path) as store:
        store.record_ledger_entry(alpha_1)
        store.record_ledger_entry(beta_1)
        store.record_ledger_entry(alpha_2)
        store.record_shadow_comparison(shadow_alpha)
        store.record_shadow_comparison(shadow_beta)

        assert store.load_ledger_entries() == [alpha_1, beta_1, alpha_2]
        assert store.load_ledger_entries(org_id="org_alpha") == [alpha_1, alpha_2]
        assert store.load_ledger_entries(org_id="org_beta") == [beta_1]
        assert store.load_ledger_entries(org_id="' OR '1'='1") == []

        assert store.load_shadow_comparisons() == [shadow_alpha, shadow_beta]
        assert store.load_shadow_comparisons(org_id="org_alpha") == [shadow_alpha]
        assert store.load_shadow_comparisons(org_id="org_beta") == [shadow_beta]
        assert store.load_shadow_comparisons(org_id="' OR '1'='1") == []


def test_summarize_economics_eight_place_decimal_precision(tmp_path: Path) -> None:
    db_path = tmp_path / "summary.db"
    entries = [
        _make_ledger_entry(
            request_id="r1",
            org_id="org_alpha",
            agent_id="a1",
            category=TaskCategory.CODE_SQL,
            tier=ModelTier.MID,
            provenance=ProvenanceTag.MEASURED,
            passed=True,
            cost_usd=Decimal("0.00000001"),
        ),
        _make_ledger_entry(
            request_id="r2",
            org_id="org_alpha",
            agent_id="a2",
            category=TaskCategory.REASONING,
            tier=ModelTier.FRONTIER,
            provenance=ProvenanceTag.MODELED,
            passed=False,
            cost_usd=Decimal("0.00000002"),
        ),
        _make_ledger_entry(
            request_id="r3",
            org_id="org_alpha",
            agent_id="a1",
            category=TaskCategory.CLASSIFICATION,
            tier=ModelTier.COMPACT,
            provenance=ProvenanceTag.UNKNOWN,
            passed=True,
            cost_usd=Decimal("0.00000000"),
            cached=True,
        ),
        _make_ledger_entry(
            request_id="r4",
            org_id="org_beta",
            agent_id="b1",
            category=TaskCategory.SUMMARIZATION,
            tier=ModelTier.COMPACT,
            provenance=ProvenanceTag.UNKNOWN,
            passed=False,
            cost_usd=Decimal("0.10000005"),
        ),
    ]

    with SQLiteEconomicsStore(db_path) as store:
        empty_summary = store.summarize_economics()
        assert empty_summary.total_requests == 0
        assert empty_summary.total_cost_usd == Decimal("0.00000000")
        assert empty_summary.cost_per_request_usd == Decimal("0.00000000")
        assert empty_summary.cost_per_successful_outcome_usd is None

        for entry in entries:
            store.record_ledger_entry(entry)

        alpha_summary = store.summarize_economics(org_id="org_alpha")
        assert alpha_summary.total_requests == 3
        assert alpha_summary.passed_requests == 2
        assert alpha_summary.failed_requests == 1
        assert alpha_summary.cache_hits == 1
        assert alpha_summary.total_cost_usd == Decimal("0.00000003")
        assert alpha_summary.measured_cost_usd == Decimal("0.00000001")
        assert alpha_summary.modeled_cost_usd == Decimal("0.00000002")
        assert alpha_summary.unknown_cost_usd == Decimal("0.00000000")
        assert alpha_summary.cost_per_request_usd == Decimal("0.00000001")
        assert alpha_summary.cost_per_successful_outcome_usd == Decimal("0.00000002")
        assert alpha_summary.spend_by_org == {"org_alpha": Decimal("0.00000003")}
        assert alpha_summary.spend_by_agent == {
            "org_alpha/a1": Decimal("0.00000001"),
            "org_alpha/a2": Decimal("0.00000002"),
        }
        assert alpha_summary.spend_by_category[TaskCategory.CODE_SQL] == Decimal("0.00000001")
        assert alpha_summary.spend_by_category[TaskCategory.REASONING] == Decimal("0.00000002")
        assert alpha_summary.spend_by_tier[ModelTier.MID] == Decimal("0.00000001")
        assert alpha_summary.spend_by_tier[ModelTier.FRONTIER] == Decimal("0.00000002")

        beta_summary = store.summarize_economics(org_id="org_beta")
        assert beta_summary.total_requests == 1
        assert beta_summary.passed_requests == 0
        assert beta_summary.cost_per_successful_outcome_usd is None
        assert beta_summary.unknown_cost_usd == Decimal("0.10000005")

        global_summary = store.summarize_economics()
        assert global_summary.total_requests == 4
        assert global_summary.total_cost_usd == Decimal("0.10000008")
        assert global_summary.cost_per_request_usd == Decimal("0.02500002")
        assert global_summary.cost_per_successful_outcome_usd == Decimal("0.05000004")


def test_otel_span_round_trip_and_limits(tmp_path: Path) -> None:
    db_path = tmp_path / "spans.db"
    spans = [
        OTelSpanRecord.create_router_span(
            system="anthropic",
            request_model="claude-3-5-sonnet",
            input_tokens=50 + i,
            output_tokens=10 + i,
            org_id="org_alpha",
            agent_id="agent_1",
            policy_id="cascade_v1",
            selected_tier=ModelTier.MID,
            cost_usd=Decimal("0.00050000"),
            cost_provenance=ProvenanceTag.MEASURED,
            shadow_mode=(i % 2 == 0),
            start_time_unix_nano=1_700_000_000_000_000_000 + i,
            end_time_unix_nano=1_700_000_000_001_000_000 + i,
            trace_id=f"{i + 1:032x}",
            span_id=f"{i + 1:016x}",
            extra_attributes={"custom.score": 0.85, "custom.flag": True},
        )
        for i in range(5)
    ]

    with SQLiteEconomicsStore(db_path) as store:
        for span in spans:
            store.record_span(span)

        assert store.load_spans(limit=0) == []
        assert store.load_spans(limit=3) == spans[:3]
        assert store.load_spans(limit=100) == spans
        with pytest.raises(ValueError, match="non-negative"):
            store.load_spans(limit=-1)


def test_closed_store_raises_and_thread_safety(tmp_path: Path) -> None:
    db_path = tmp_path / "concurrent.db"
    with SQLiteEconomicsStore(db_path) as store:
        errors: list[BaseException] = []

        def worker(worker_idx: int) -> None:
            try:
                for item_idx in range(10):
                    store.record_ledger_entry(
                        _make_ledger_entry(
                            request_id=f"w{worker_idx}-i{item_idx}",
                            org_id=f"org_{worker_idx}",
                            cost_usd=Decimal("0.00001000"),
                        )
                    )
            except BaseException as exc:  # pragma: no cover - defensive test capture
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(idx,)) for idx in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert len(store.load_ledger_entries()) == 40

    with pytest.raises(RuntimeError, match="closed"):
        store.load_ledger_entries()
