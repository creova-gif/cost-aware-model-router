# ADR-017: SQLite Online Backup and Disaster Recovery
- **Status:** Accepted
- **Owner:** Justin Mafie

## Context
Following `ADR-015`, `SQLiteEconomicsStore` (`store.py`) persists `spend_ledger`, `shadow_comparisons`, and `otel_spans` in SQLite `WAL` mode and truncates WAL frames on `close()`. However, production deployments running `cost-router serve` continuously cannot stop gateway traffic or close the live store simply to take a durable point-in-time backup:

- **Unsafe File-Level Copies in WAL Mode:** Copying the `.db` file directly with `cp`, `shutil.copy2`, or container volume snapshots while `SQLiteEconomicsStore` is actively serving writes risks capturing an inconsistent split between the main database file and uncheckpointed `-wal` / `-shm` frames, potentially corrupting the backup.
- **No Operator Backup CLI:** Operators lacked a built-in CLI subcommand (`cost-router backup-db`) to produce a consistent, self-contained SQLite backup file for disaster recovery, scheduled snapshots, or off-host archival without interrupting live traffic.

## Decision
1. **Lock-Safe Online Backup Method (`store.py`):**
   - Add `SQLiteEconomicsStore.backup_to(dest_path)` to perform a consistent online backup of the open SQLite database to `dest_path`.
   - Acquire `self._lock` and validate `self._ensure_open()` before opening the destination SQLite connection and invoking Python's `sqlite3.Connection.backup()` API, which copies database pages transactionally and produces a self-contained, consistent SQLite snapshot even while the source store is operating in `WAL` mode.
2. **Operator CLI Subcommand (`cli.py`):**
   - Expose `cost-router backup-db` in `cli.py` so operators and scheduled cron/Kubernetes jobs can trigger `SQLiteEconomicsStore.backup_to(dest_path)` from the command line against a live or idle economics database.

## Alternatives Considered
- **Raw Filesystem Copy (`shutil.copy2` / `cp`) of `.db` + `-wal` + `-shm` (Rejected):** Copying multiple WAL-mode files non-atomically while concurrent writes occur can yield a torn or unrecoverable snapshot.
- **Stopping `cost-router serve` for Cold Backups (Rejected):** Taking the gateway offline for routine backups violates availability expectations for production FinOps control-plane deployments.
- **Running `PRAGMA wal_checkpoint(TRUNCATE)` Followed by `cp` (Rejected):** Even after a checkpoint, new writes arriving between the checkpoint and the file copy can dirty the `-wal` file again; `sqlite3.Connection.backup()` handles page-level consistency natively under SQLite's backup API.

## Consequences & Failure Modes
- **Positive:**
  - Operators and automation can take consistent, self-contained SQLite backups via `SQLiteEconomicsStore.backup_to(dest_path)` and `cost-router backup-db` without stopping `cost-router serve`.
  - Backups produced by `sqlite3.Connection.backup()` include committed WAL state in a single portable SQLite file suitable for disaster recovery and off-host retention.
- **Failure Modes & Mitigations:**
  - **Write Serialization During Backup:** Acquiring `self._lock` in `SQLiteEconomicsStore.backup_to(dest_path)` serializes in-process writes for the duration of the page copy; large databases should be pruned periodically with `cost-router prune-db` (`ADR-011`) to keep backup duration bounded.
  - **Destination Path / Disk Space Errors:** If `dest_path` is unwritable, on a read-only filesystem, or runs out of disk space, `sqlite3.Connection.backup()` raises `sqlite3.Error` / `OSError` without mutating the live source database.
  - **Backup of a Closed Store:** Calling `backup_to(dest_path)` on a closed `SQLiteEconomicsStore` fails fast via `self._ensure_open()`.
