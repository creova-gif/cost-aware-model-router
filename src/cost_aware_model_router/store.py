from __future__ import annotations

import json
import sqlite3
import threading
import time
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import assert_never

from cost_aware_model_router.gateway import ShadowComparisonRecord
from cost_aware_model_router.schemas import (
    TIER_ORDER,
    ModelTier,
    ProvenanceTag,
    TaskCategory,
    format_usd_str,
    quantize_usd,
)
from cost_aware_model_router.telemetry import OTelSpanRecord
from cost_aware_model_router.thermostat import SpendLedgerEntry, SpendSummary

_ZERO_USD = Decimal("0.00000000")

_CREATE_SPEND_LEDGER_TABLE = """
CREATE TABLE IF NOT EXISTS spend_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    org_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    category TEXT NOT NULL,
    tier TEXT NOT NULL,
    provenance TEXT NOT NULL,
    passed INTEGER NOT NULL,
    cost_usd TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cached INTEGER NOT NULL,
    otel_attributes_json TEXT NOT NULL,
    created_at_unix_ns INTEGER NOT NULL
)
"""

_CREATE_SPEND_LEDGER_ORG_INDEX = """
CREATE INDEX IF NOT EXISTS idx_spend_ledger_org_id ON spend_ledger (org_id)
"""

_CREATE_SHADOW_COMPARISONS_TABLE = """
CREATE TABLE IF NOT EXISTS shadow_comparisons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    org_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    primary_policy_id TEXT NOT NULL,
    primary_tier TEXT NOT NULL,
    primary_cost_usd TEXT NOT NULL,
    primary_latency_ms REAL NOT NULL,
    shadow_policy_id TEXT NOT NULL,
    shadow_tier TEXT NOT NULL,
    shadow_cost_usd TEXT NOT NULL,
    shadow_cost_delta_usd TEXT NOT NULL,
    would_downgrade INTEGER NOT NULL,
    explanation TEXT NOT NULL,
    created_at_unix_ns INTEGER NOT NULL
)
"""

_CREATE_SHADOW_COMPARISONS_ORG_INDEX = """
CREATE INDEX IF NOT EXISTS idx_shadow_comparisons_org_id ON shadow_comparisons (org_id)
"""

_CREATE_OTEL_SPANS_TABLE = """
CREATE TABLE IF NOT EXISTS otel_spans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    name TEXT NOT NULL,
    start_time_unix_nano INTEGER NOT NULL,
    end_time_unix_nano INTEGER NOT NULL,
    attributes_json TEXT NOT NULL,
    org_id TEXT,
    created_at_unix_ns INTEGER NOT NULL
)
"""

_CREATE_OTEL_SPANS_ORG_INDEX = """
CREATE INDEX IF NOT EXISTS idx_otel_spans_org_id ON otel_spans (org_id)
"""

_INSERT_LEDGER_SQL = """
INSERT INTO spend_ledger (
    request_id,
    org_id,
    agent_id,
    category,
    tier,
    provenance,
    passed,
    cost_usd,
    input_tokens,
    output_tokens,
    cached,
    otel_attributes_json,
    created_at_unix_ns
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_SELECT_ALL_LEDGER_SQL = """
SELECT
    request_id,
    org_id,
    agent_id,
    category,
    tier,
    provenance,
    passed,
    cost_usd,
    input_tokens,
    output_tokens,
    cached,
    otel_attributes_json
FROM spend_ledger
ORDER BY id ASC
"""

_SELECT_ORG_LEDGER_SQL = """
SELECT
    request_id,
    org_id,
    agent_id,
    category,
    tier,
    provenance,
    passed,
    cost_usd,
    input_tokens,
    output_tokens,
    cached,
    otel_attributes_json
FROM spend_ledger
WHERE org_id = ?
ORDER BY id ASC
"""

_INSERT_SHADOW_SQL = """
INSERT INTO shadow_comparisons (
    request_id,
    org_id,
    agent_id,
    primary_policy_id,
    primary_tier,
    primary_cost_usd,
    primary_latency_ms,
    shadow_policy_id,
    shadow_tier,
    shadow_cost_usd,
    shadow_cost_delta_usd,
    would_downgrade,
    explanation,
    created_at_unix_ns
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_SELECT_ALL_SHADOW_SQL = """
SELECT
    request_id,
    org_id,
    agent_id,
    primary_policy_id,
    primary_tier,
    primary_cost_usd,
    primary_latency_ms,
    shadow_policy_id,
    shadow_tier,
    shadow_cost_usd,
    shadow_cost_delta_usd,
    would_downgrade,
    explanation
FROM shadow_comparisons
ORDER BY id ASC
"""

_SELECT_ORG_SHADOW_SQL = """
SELECT
    request_id,
    org_id,
    agent_id,
    primary_policy_id,
    primary_tier,
    primary_cost_usd,
    primary_latency_ms,
    shadow_policy_id,
    shadow_tier,
    shadow_cost_usd,
    shadow_cost_delta_usd,
    would_downgrade,
    explanation
FROM shadow_comparisons
WHERE org_id = ?
ORDER BY id ASC
"""

_INSERT_SPAN_SQL = """
INSERT INTO otel_spans (
    trace_id,
    span_id,
    name,
    start_time_unix_nano,
    end_time_unix_nano,
    attributes_json,
    org_id,
    created_at_unix_ns
) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""

_SELECT_SPANS_SQL = """
SELECT
    trace_id,
    span_id,
    name,
    start_time_unix_nano,
    end_time_unix_nano,
    attributes_json
FROM otel_spans
ORDER BY id ASC
LIMIT ?
"""


class SQLiteEconomicsStore:
    """Thread-safe SQLite-backed durable store for spend ledger, shadow comparisons, and OTel spans."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._db_path = str(db_path)
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._closed = False
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            with self._conn:
                self._conn.execute(_CREATE_SPEND_LEDGER_TABLE)
                self._conn.execute(_CREATE_SPEND_LEDGER_ORG_INDEX)
                self._conn.execute(_CREATE_SHADOW_COMPARISONS_TABLE)
                self._conn.execute(_CREATE_SHADOW_COMPARISONS_ORG_INDEX)
                self._conn.execute(_CREATE_OTEL_SPANS_TABLE)
                self._conn.execute(_CREATE_OTEL_SPANS_ORG_INDEX)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("SQLiteEconomicsStore is closed")

    def record_ledger_entry(self, entry: SpendLedgerEntry) -> None:
        """Persist a SpendLedgerEntry row with 8-place USD string precision."""
        attrs_json = json.dumps(entry.otel_attributes, sort_keys=True, separators=(",", ":"))
        params = (
            entry.request_id,
            entry.org_id,
            entry.agent_id,
            entry.category.value,
            entry.tier.value,
            entry.provenance.value,
            1 if entry.passed else 0,
            format_usd_str(entry.cost_usd),
            entry.input_tokens,
            entry.output_tokens,
            1 if entry.cached else 0,
            attrs_json,
            time.time_ns(),
        )
        with self._lock:
            self._ensure_open()
            with self._conn:
                self._conn.execute(_INSERT_LEDGER_SQL, params)

    def load_ledger_entries(self, org_id: str | None = None) -> list[SpendLedgerEntry]:
        """Load persisted SpendLedgerEntry rows, optionally filtered by org_id."""
        with self._lock:
            self._ensure_open()
            if org_id is None:
                rows = self._conn.execute(_SELECT_ALL_LEDGER_SQL).fetchall()
            else:
                rows = self._conn.execute(_SELECT_ORG_LEDGER_SQL, (org_id,)).fetchall()

        return [
            SpendLedgerEntry(
                request_id=str(row["request_id"]),
                org_id=str(row["org_id"]),
                agent_id=str(row["agent_id"]),
                category=TaskCategory(str(row["category"])),
                tier=ModelTier(str(row["tier"])),
                provenance=ProvenanceTag(str(row["provenance"])),
                passed=bool(row["passed"]),
                cost_usd=Decimal(str(row["cost_usd"])),
                input_tokens=int(row["input_tokens"]),
                output_tokens=int(row["output_tokens"]),
                cached=bool(row["cached"]),
                otel_attributes=json.loads(str(row["otel_attributes_json"])),
            )
            for row in rows
        ]

    def record_shadow_comparison(self, rec: ShadowComparisonRecord) -> None:
        """Persist a ShadowComparisonRecord row."""
        params = (
            rec.request_id,
            rec.org_id,
            rec.agent_id,
            rec.primary_policy_id,
            rec.primary_tier.value,
            format_usd_str(rec.primary_cost_usd),
            rec.primary_latency_ms,
            rec.shadow_policy_id,
            rec.shadow_tier.value,
            format_usd_str(rec.shadow_cost_usd),
            format_usd_str(rec.shadow_cost_delta_usd),
            1 if rec.would_downgrade else 0,
            rec.explanation,
            time.time_ns(),
        )
        with self._lock:
            self._ensure_open()
            with self._conn:
                self._conn.execute(_INSERT_SHADOW_SQL, params)

    def load_shadow_comparisons(self, org_id: str | None = None) -> list[ShadowComparisonRecord]:
        """Load persisted ShadowComparisonRecord rows, optionally filtered by org_id."""
        with self._lock:
            self._ensure_open()
            if org_id is None:
                rows = self._conn.execute(_SELECT_ALL_SHADOW_SQL).fetchall()
            else:
                rows = self._conn.execute(_SELECT_ORG_SHADOW_SQL, (org_id,)).fetchall()

        return [
            ShadowComparisonRecord(
                request_id=str(row["request_id"]),
                org_id=str(row["org_id"]),
                agent_id=str(row["agent_id"]),
                primary_policy_id=str(row["primary_policy_id"]),
                primary_tier=ModelTier(str(row["primary_tier"])),
                primary_cost_usd=Decimal(str(row["primary_cost_usd"])),
                primary_latency_ms=float(row["primary_latency_ms"]),
                shadow_policy_id=str(row["shadow_policy_id"]),
                shadow_tier=ModelTier(str(row["shadow_tier"])),
                shadow_cost_usd=Decimal(str(row["shadow_cost_usd"])),
                shadow_cost_delta_usd=Decimal(str(row["shadow_cost_delta_usd"])),
                would_downgrade=bool(row["would_downgrade"]),
                explanation=str(row["explanation"]),
            )
            for row in rows
        ]

    def record_span(self, span: OTelSpanRecord) -> None:
        """Persist an OTelSpanRecord row."""
        attrs_json = json.dumps(span.attributes, sort_keys=True, separators=(",", ":"))
        raw_org = span.attributes.get("cost_router.org_id")
        org_id = raw_org if isinstance(raw_org, str) and raw_org else None
        params = (
            span.trace_id,
            span.span_id,
            span.name,
            span.start_time_unix_nano,
            span.end_time_unix_nano,
            attrs_json,
            org_id,
            time.time_ns(),
        )
        with self._lock:
            self._ensure_open()
            with self._conn:
                self._conn.execute(_INSERT_SPAN_SQL, params)

    def load_spans(self, limit: int = 100) -> list[OTelSpanRecord]:
        """Load persisted OTelSpanRecord rows up to limit."""
        if limit < 0:
            raise ValueError("limit must be non-negative")
        if limit == 0:
            return []

        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(_SELECT_SPANS_SQL, (limit,)).fetchall()

        return [
            OTelSpanRecord(
                trace_id=str(row["trace_id"]),
                span_id=str(row["span_id"]),
                name="gen_ai.router.completion",
                start_time_unix_nano=int(row["start_time_unix_nano"]),
                end_time_unix_nano=int(row["end_time_unix_nano"]),
                attributes=json.loads(str(row["attributes_json"])),
            )
            for row in rows
        ]

    def summarize_economics(self, org_id: str | None = None) -> SpendSummary:
        """Compute SpendSummary with 8-place Decimal precision from persisted ledger entries."""
        entries = self.load_ledger_entries(org_id=org_id)
        total_requests = len(entries)
        passed_requests = sum(1 for e in entries if e.passed)
        failed_requests = total_requests - passed_requests
        cache_hits = sum(1 for e in entries if e.cached)

        total_cost = _ZERO_USD
        measured_cost = _ZERO_USD
        modeled_cost = _ZERO_USD
        unknown_cost = _ZERO_USD

        spend_by_org: dict[str, Decimal] = {}
        spend_by_agent: dict[str, Decimal] = {}
        spend_by_category: dict[TaskCategory, Decimal] = {cat: _ZERO_USD for cat in TaskCategory}
        spend_by_tier: dict[ModelTier, Decimal] = {tier: _ZERO_USD for tier in TIER_ORDER}

        for entry in entries:
            cost = entry.cost_usd
            total_cost = quantize_usd(total_cost + cost)

            match entry.provenance:
                case ProvenanceTag.MEASURED:
                    measured_cost = quantize_usd(measured_cost + cost)
                case ProvenanceTag.MODELED:
                    modeled_cost = quantize_usd(modeled_cost + cost)
                case ProvenanceTag.UNKNOWN:
                    unknown_cost = quantize_usd(unknown_cost + cost)
                case _ as unreachable:
                    assert_never(unreachable)

            spend_by_org[entry.org_id] = quantize_usd(
                spend_by_org.get(entry.org_id, _ZERO_USD) + cost
            )
            agent_scope = f"{entry.org_id}/{entry.agent_id}"
            spend_by_agent[agent_scope] = quantize_usd(
                spend_by_agent.get(agent_scope, _ZERO_USD) + cost
            )
            spend_by_category[entry.category] = quantize_usd(
                spend_by_category[entry.category] + cost
            )
            spend_by_tier[entry.tier] = quantize_usd(spend_by_tier[entry.tier] + cost)

        cost_per_request = (
            quantize_usd(total_cost / Decimal(total_requests))
            if total_requests > 0
            else _ZERO_USD
        )
        cost_per_successful_outcome: Decimal | None = (
            quantize_usd(total_cost / Decimal(passed_requests)) if passed_requests > 0 else None
        )

        return SpendSummary(
            total_requests=total_requests,
            passed_requests=passed_requests,
            failed_requests=failed_requests,
            cache_hits=cache_hits,
            total_cost_usd=total_cost,
            measured_cost_usd=measured_cost,
            modeled_cost_usd=modeled_cost,
            unknown_cost_usd=unknown_cost,
            cost_per_request_usd=cost_per_request,
            cost_per_successful_outcome_usd=cost_per_successful_outcome,
            spend_by_org=spend_by_org,
            spend_by_agent=spend_by_agent,
            spend_by_category=spend_by_category,
            spend_by_tier=spend_by_tier,
        )

    def is_healthy(self) -> bool:
        """Return True if the store is open and responds to a lightweight SQLite query."""
        with self._lock:
            if self._closed:
                return False
            try:
                self._conn.execute("SELECT 1").fetchone()
                return True
            except sqlite3.Error:
                return False

    def close(self) -> None:
        """Close the underlying SQLite connection idempotently."""
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def __enter__(self) -> SQLiteEconomicsStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()
