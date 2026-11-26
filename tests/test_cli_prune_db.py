from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from cost_aware_model_router.cli import build_parser, main
from cost_aware_model_router.gateway import ShadowComparisonRecord
from cost_aware_model_router.schemas import ModelTier, ProvenanceTag, TaskCategory
from cost_aware_model_router.store import SQLiteEconomicsStore
from cost_aware_model_router.telemetry import OTelSpanRecord
from cost_aware_model_router.thermostat import SpendLedgerEntry


def _parse_kv_output(stdout: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in stdout.strip().splitlines():
        key, value = line.split("=", 1)
        parsed[key] = value
    return parsed


def _make_ledger_entry(request_id: str, cost_usd: str) -> SpendLedgerEntry:
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


def test_build_parser_prune_db() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["prune-db", "--db-path", "/tmp/economics.db", "--max-age-hours", "24"]
    )
    assert args.command == "prune-db"
    assert args.db_path == Path("/tmp/economics.db")
    assert args.max_age_hours == 24.0

    default_args = parser.parse_args(["prune-db", "--db-path", "/tmp/economics.db"])
    assert default_args.max_age_hours == 168.0


def test_main_prune_db_removes_old_rows_and_preserves_recent_and_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "economics.db"
    base_epoch = 1_700_000_000

    with SQLiteEconomicsStore(db_path) as store:
        # Insert aged rows (2 hours old)
        monkeypatch.setattr(
            "cost_aware_model_router.store.time.time_ns",
            lambda: (base_epoch - 7200) * 1_000_000_000,
        )
        store.record_ledger_entry(_make_ledger_entry("req-old", "0.02000000"))
        store.record_shadow_comparison(_make_shadow_record("shadow-old-1"))
        store.record_shadow_comparison(_make_shadow_record("shadow-old-2"))
        store.record_span(_make_span("0000000000000001"))

        # Insert recent rows (5 minutes old)
        monkeypatch.setattr(
            "cost_aware_model_router.store.time.time_ns",
            lambda: (base_epoch - 300) * 1_000_000_000,
        )
        store.record_ledger_entry(_make_ledger_entry("req-new", "0.03000000"))
        store.record_shadow_comparison(_make_shadow_record("shadow-new-1"))
        store.record_span(_make_span("0000000000000002"))

    monkeypatch.setattr("cost_aware_model_router.store.time.time", lambda: float(base_epoch))

    rc = main(["prune-db", "--db-path", str(db_path), "--max-age-hours", "1"])
    assert rc == 0

    output = _parse_kv_output(capsys.readouterr().out)
    assert output == {
        "db_path": str(db_path),
        "max_age_hours": "1.0",
        "shadow_comparisons_deleted": "2",
        "otel_spans_deleted": "1",
    }

    with SQLiteEconomicsStore(db_path) as reopened:
        assert [rec.request_id for rec in reopened.load_shadow_comparisons()] == ["shadow-new-1"]
        assert [span.span_id for span in reopened.load_spans(limit=10)] == ["0000000000000002"]
        summary = reopened.summarize_economics()
        assert summary.total_requests == 2
        assert summary.total_cost_usd == Decimal("0.05000000")


@pytest.mark.parametrize("bad_hours", ["0", "-2.5", "nan", "inf"])
def test_main_prune_db_returns_1_on_invalid_max_age_hours(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    bad_hours: str,
) -> None:
    db_path = tmp_path / "economics.db"
    rc = main(["prune-db", "--db-path", str(db_path), "--max-age-hours", bad_hours])
    assert rc == 1
    assert "Database prune failed:" in capsys.readouterr().err
