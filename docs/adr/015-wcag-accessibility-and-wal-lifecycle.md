# ADR-015: WCAG 2.1 AA Accessibility, SQLite WAL Lifecycle, and Enterprise Governance
- **Status:** Accepted
- **Owner:** Justin Mafie

## Context
Following `ADR-014`, three operational, accessibility, and governance gaps remained before production sign-off:

1. **WCAG 2.1 AA Accessibility & CSS Selector Defect (`dashboard.py`, `report.py`):**
   - Both the live FinOps control-plane dashboard (`render_live_finops_dashboard_html` in `dashboard.py`) and the interactive Pareto Explorer (`render_pareto_explorer_html` in `report.py`) lacked semantic landmark elements (`<header>`, `<main id="main-content">`), keyboard skip navigation (`.skip-link`), high-contrast keyboard focus indicators (`:focus-visible`), `prefers-reduced-motion` media queries, `aria-live="polite"` status regions, and `scope="col"` attributes on table column headers.
   - In `report.py`, the filter tab buttons lacked `aria-pressed` state synchronization, the quality-floor range slider (`#quality-slider`) and Pareto SVG (`#pareto-svg`) lacked accessible labels, and a stray space in `. btn-active:hover` prevented hover styling from matching active filter buttons.
2. **SQLite WAL Checkpoint & Context Manager Re-Entry (`store.py`):**
   - `SQLiteEconomicsStore` operates in SQLite `WAL` mode, which can leave uncheckpointed `-wal` frames on disk after `close()`, and `__enter__` previously returned `self` without checking `self._closed`, allowing callers to re-enter an already-closed store and fail later on the first query.
3. **Enterprise Repository Governance & Performance Regression Guardrails:**
   - The repository lacked standard enterprise governance artifacts (`SECURITY.md`, `CODEOWNERS`, `.env.example`, and `.github/dependabot.yml`) as well as automated performance regression tests to catch latency or throughput regressions in routing, gateway, and store paths.

## Decision
1. **WCAG 2.1 AA Upgrades & CSS Fix (`dashboard.py`, `report.py`):**
   - Wrap the top header in `<header class="panel header-bar">` and primary content in `<main id="main-content">` with a `.skip-link` (`href="#main-content"`) at the top of `<body>`.
   - Add `:focus-visible` (`outline: 2px solid #2563eb; outline-offset: 2px;`) and `@media (prefers-reduced-motion: reduce)` rules to both HTML templates.
   - Add `aria-live="polite"` to `#refresh-status` and `#status-banner` (`role="status"`) in `dashboard.py`, and to `#infeasible-banner` in `report.py`.
   - Add `scope="col"` to all `<th>` header cells in `dashboard.py` and `report.py`.
   - In `report.py`, add `aria-label="Minimum quality floor"` to `#quality-slider`, `role="img" aria-label="2D Pareto Frontier Chart"` to `#pareto-svg`, `aria-pressed="true"|"false"` synchronized on `[data-filter]` button clicks, and fix `. btn-active:hover` to `.btn-active:hover`.
2. **`SQLiteEconomicsStore` Context Manager Guard & WAL Checkpoint (`store.py`):**
   - Update `SQLiteEconomicsStore.__enter__` to acquire `self._lock` and call `self._ensure_open()`, raising `RuntimeError` immediately if a closed store is re-entered.
   - Update `SQLiteEconomicsStore.close()` to execute `PRAGMA wal_checkpoint(TRUNCATE)` before `self._conn.close()`, logging any `sqlite3.Error` at `DEBUG` level so close remains idempotent and resilient.
3. **Enterprise Governance & Performance Regression Suite:**
   - Standardize repository governance with `SECURITY.md` (vulnerability reporting & response SLA), `CODEOWNERS` (review ownership), `.env.example` (documented environment variables with zero real secrets), and `.github/dependabot.yml` (automated dependency update PRs).
   - Establish a performance regression test suite covering critical routing, gateway, and persistence paths.

## Alternatives Considered
- **Deferring Accessibility to External CSS/JS Frameworks (Rejected):** Both `dashboard.py` and `report.py` are strictly zero-CDN, single-file HTML generators (`THREAT_MODEL.md`); all WCAG 2.1 AA semantics and styles must be self-contained.
- **Passive WAL Checkpointing Only (Rejected):** Leaving `-wal` truncation solely to SQLite's auto-checkpoint threshold risks leaving residual `-wal` files after graceful `SIGTERM` shutdown; `PRAGMA wal_checkpoint(TRUNCATE)` on `close()` guarantees clean single-file state on exit.
- **Allowing `__enter__` on a Closed Store (Rejected):** Failing fast in `__enter__` via `_ensure_open()` surfaces lifecycle bugs immediately rather than mid-block.

## Consequences & Failure Modes
- **Positive:**
  - Keyboard-only and screen-reader users can navigate `dashboard.py` and `report.py` with skip links, landmarks, focus rings, live announcements, and accurate button/table semantics, while `.btn-active:hover` renders as intended.
  - `SQLiteEconomicsStore` cleanly truncates WAL frames on `close()` and rejects context-manager re-entry after close.
  - Contributors and security researchers have clear disclosure, ownership, configuration, and automated dependency-hygiene workflows, backed by performance regression tests.
- **Failure Modes & Mitigations:**
  - **WAL Checkpoint Contention on Close:** If another reader holds a snapshot when `close()` runs `PRAGMA wal_checkpoint(TRUNCATE)`, SQLite may raise `sqlite3.OperationalError` (`database is locked`). `close()` catches `sqlite3.Error`, logs at `DEBUG`, and still closes the connection cleanly.
  - **Reduced-Motion Override:** `@media (prefers-reduced-motion: reduce)` disables transitions/animations in CSS; future interactive controls must avoid JS-driven animations that bypass CSS media queries.
