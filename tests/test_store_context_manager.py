from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cost_aware_model_router.schemas import ModelTier, ProvenanceTag, TaskCategory
from cost_aware_model_router.store import SQLiteEconomicsStore
from cost_aware_model_router.thermostat import SpendLedgerEntry


def _make_entry(request_id: str = "req-ctx-1", org_id: str = "org-ctx") -> SpendLedgerEntry:
    return SpendLedgerEntry(
        request_id=request_id,
        org_id=org_id,
        agent_id="agent-ctx",
        category=TaskCategory.CODE_SQL,
        tier=ModelTier.MID,
        provenance=ProvenanceTag.MEASURED,
        passed=True,
        cost_usd=Decimal("0.00250000"),
        input_tokens=100,
        output_tokens=50,
        cached=False,
        otel_attributes={"env": "test"},
    )


def test_context_manager_records_closes_and_persists_across_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "econ.db"
    entry = _make_entry()

    with SQLiteEconomicsStore(db_path) as store:
        assert store.is_healthy() is True
        store.record_ledger_entry(entry)

    assert store.is_healthy() is False

    with SQLiteEconomicsStore(db_path) as reopened:
        entries = reopened.load_ledger_entries(org_id="org-ctx")
        assert len(entries) == 1
        assert entries[0].request_id == "req-ctx-1"
        assert entries[0].cost_usd == Decimal("0.00250000")

    assert reopened.is_healthy() is False


def test_close_checkpoints_wal_and_reenter_raises_runtime_error(tmp_path: Path) -> None:
    db_path = tmp_path / "econ.db"
    wal_path = tmp_path / "econ.db-wal"

    store = SQLiteEconomicsStore(db_path)
    store.record_ledger_entry(_make_entry("req-wal-1"))
    store.close()

    assert store.is_healthy() is False
    # TRUNCATE checkpoint flushes WAL frames into the main DB and truncates/removes the WAL file.
    assert not wal_path.exists() or wal_path.stat().st_size == 0

    # Idempotent second close does not raise.
    store.close()

    with pytest.raises(RuntimeError, match="closed"), store:
        pass

    # Verify the checkpointed main database file contains the record on its own.
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT request_id FROM spend_ledger").fetchone()
        assert row == ("req-wal-1",)
    finally:
        conn.close()


def test_close_ignores_sqlite_checkpoint_error_and_still_closes(tmp_path: Path) -> None:
    db_path = tmp_path / "econ.db"
    store = SQLiteEconomicsStore(db_path)

    mock_conn = MagicMock(spec=sqlite3.Connection)
    mock_conn.execute.side_effect = sqlite3.OperationalError("database is locked")
    real_conn = store._conn
    store._conn = mock_conn
    try:
        store.close()
    finally:
        real_conn.close()

    mock_conn.execute.assert_called_once_with("PRAGMA wal_checkpoint(TRUNCATE)")
    mock_conn.close.assert_called_once_with()
    assert store.is_healthy() is False
