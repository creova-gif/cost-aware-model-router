from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from cost_aware_model_router.cli import build_parser, main
from cost_aware_model_router.schemas import ModelTier, ProvenanceTag, TaskCategory
from cost_aware_model_router.store import SQLiteEconomicsStore
from cost_aware_model_router.thermostat import SpendLedgerEntry


def _make_ledger_entry(request_id: str, cost_usd: str, *, passed: bool = True) -> SpendLedgerEntry:
    return SpendLedgerEntry(
        request_id=request_id,
        org_id="org_backup",
        agent_id="agent_1",
        category=TaskCategory.CODE_SQL,
        tier=ModelTier.MID,
        provenance=ProvenanceTag.MEASURED,
        passed=passed,
        cost_usd=Decimal(cost_usd),
        input_tokens=120,
        output_tokens=60,
        cached=False,
        otel_attributes={"env": "test"},
    )


def _parse_kv_output(stdout: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in stdout.strip().splitlines():
        key, value = line.split("=", 1)
        parsed[key] = value
    return parsed


def test_backup_to_preserves_economics_and_creates_parent_dirs(tmp_path: Path) -> None:
    db_path = tmp_path / "source.db"
    backup_path = tmp_path / "nested" / "backups" / "economics-backup.db"

    with SQLiteEconomicsStore(db_path) as store:
        store.record_ledger_entry(_make_ledger_entry("req-1", "0.01250000", passed=True))
        store.record_ledger_entry(_make_ledger_entry("req-2", "0.00750000", passed=False))
        expected_summary = store.summarize_economics()

        returned_path = store.backup_to(backup_path)
        assert returned_path == backup_path
        assert backup_path.is_file()
        assert backup_path.stat().st_size > 0

    with SQLiteEconomicsStore(backup_path) as restored:
        assert restored.is_healthy()
        restored_summary = restored.summarize_economics()
        assert restored_summary == expected_summary
        assert restored_summary.total_requests == 2
        assert restored_summary.passed_requests == 1
        assert restored_summary.failed_requests == 1
        assert restored_summary.total_cost_usd == Decimal("0.02000000")


def test_backup_to_raises_runtime_error_when_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "closed.db"
    store = SQLiteEconomicsStore(db_path)
    store.close()

    with pytest.raises(RuntimeError, match="closed"):
        store.backup_to(tmp_path / "should-not-exist.db")


def test_build_parser_backup_db() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["backup-db", "--db-path", "/tmp/source.db", "--out", "/tmp/backup.db"]
    )
    assert args.command == "backup-db"
    assert args.db_path == Path("/tmp/source.db")
    assert args.out == Path("/tmp/backup.db")


def test_cli_backup_db_writes_readable_backup(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "live.db"
    out_path = tmp_path / "snapshots" / "live-backup.db"

    with SQLiteEconomicsStore(db_path) as store:
        store.record_ledger_entry(_make_ledger_entry("req-cli-1", "0.03000000"))

    rc = main(["backup-db", "--db-path", str(db_path), "--out", str(out_path)])
    assert rc == 0

    output = _parse_kv_output(capsys.readouterr().out)
    assert output["db_path"] == str(db_path)
    assert output["backup_path"] == str(out_path)
    assert int(output["bytes"]) == out_path.stat().st_size
    assert int(output["bytes"]) > 0

    with SQLiteEconomicsStore(out_path) as backed_up:
        summary = backed_up.summarize_economics()
        assert summary.total_requests == 1
        assert summary.total_cost_usd == Decimal("0.03000000")


def test_cli_backup_db_returns_1_when_out_is_directory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "live.db"
    out_dir = tmp_path / "existing-dir"
    out_dir.mkdir()

    rc = main(["backup-db", "--db-path", str(db_path), "--out", str(out_dir)])
    assert rc == 1
    assert "Database backup failed:" in capsys.readouterr().err
