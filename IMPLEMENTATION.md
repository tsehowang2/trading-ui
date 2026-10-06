# Implementation milestones — profiles, backups and account ledger

## Automatic strategy account — runtime historical start and catch-up

Implemented independent **Strategy Paper** page and cash-constrained execution.
No broker is connected, and manual accounts/legacy holdings are never inherited.

1. Save the profile watchlist and risk/confidence/pyramid settings.
2. Open Strategy Paper; choose start date and initial cash at runtime, then Start.
   Weekend/holiday dates move to the next exchange session. Default date is the
   latest completed session. Old starts work only with available stock/benchmark
   history and sufficient warmup; missing required data blocks that session.
3. The page catches up automatically when opened, or on Run, in 10-session chunks.
   Each session commits its fills, positions, pending orders and equity together.
   Closing the page/retrying resumes the saved checkpoint, without duplicate fills.
   Pause/resume controls are available. No daily invocation is required to replay
   missed sessions; this is NOT real-time execution while the website is closed.
4. Settings/watchlist are **frozen at launch** for reproducibility. Editing the
   profile does not retroactively change an existing run. Explicit Reset replaces
   only this strategy paper account with new cash/start/settings. Download backup
   first; manual history and old portfolio holdings remain untouched.

### Allocation and execution

- Completed-close shared strategy decisions schedule following-session open orders.
  Exits execute first; new positions ranked by confidence, RS and ticker take
  priority over pyramids. Shared cash is consumed once, rather than treating every
  signal as separately affordable. Sell proceeds are used only after actual
  modeled sells fill. Pending sells do not release slots during close planning.
- Whole-share purchases enforce cash reserve, max positions, per-name exposure
  ceiling of equity/max_positions, per-position stop-risk budget and total stop
  risk ceiling min(20%, risk_per_trade_pct \* max_positions). Commission is $2 per
  fill; no margin/shorting. Open gaps resize/reject orders using actual open prices.
- Pyramids require configured confidence/profit/cushion, price advancement since
  last purchase, at most three additions per continuous position and one per session.
  They use remaining risk/exposure capacity, preserve the ratcheted stop and reject
  opening gaps that would average down. Some strong trends may allow no adds when
  the position already fills its exposure/risk allocation; this is intentional.
- Decisions, declined candidates, fills/rejections, equity, realized profit and
  drawdown are shown separately. Historical processing is labeled retrospective
  modeled sessions. Daily and chunked replay match on fixed provider data.
- Price model remains Yahoo split-adjusted **synthetic shares**, not original-share
  broker simulation. Modeled cash dividends use provider dividend amounts. A change
  to already processed provider history blocks continuation, requiring explicit
  reset/replay rather than mixing old prices with a revised split scale. Thus any
  chosen date is subject to provider coverage/revisions; arbitrary dates cannot be
  guaranteed. A modern frozen universe applied to old dates also has survivorship
  selection bias; this feature is not unbiased strategy certification.
- Existing profile advisory dashboard still uses legacy holdings estimates; the
  Strategy Paper page is the actual independent jointly constrained simulation.

### Portable strategy progress

Backup schema **v4** includes paper run configuration, positions/timers/peaks/stops,
checkpoint, pending and filled/rejected orders, input prefix hashes and daily equity
history plus account ledger. Full restore retains identity; import-as-new remaps
account/event/run identifiers and fill references. After restoring, Run resumes at
the saved checkpoint. V1/v2/v3 remain supported with absent-progress warnings; full
restore from older backup removes history it cannot contain. Raw provider price
cache is not exported; changed refetched processed history will block continuation.

Local tests use synthetic provider fixtures, temporary state and no Render/live
Yahoo connection. They cover stock selection, reserves/slots/risk, next-open fills,
gap resizing, stops/exits/pyramids, concurrent retries, daily/chunk equivalence,
missing data resume, frozen universe, history revisions and backup continuation.
Automatic daily real-time execution, full historical raw corporate-action modeling,
time-weighted returns and causal manual-vs-strategy attribution remain outside this
first simulator. Funding is fixed to launch cash; changing it requires explicit
reset, not editing an already processed strategy ledger.

Browser smoke testing with mocked prices verified a runtime 2024 historical start,
two modeled fills, automatic multi-chunk catch-up and preserved equity/history at
the first unavailable session. No provider/broker requests were made. The local
regression suite passed 102 tests after this implementation.

## Milestone 3 — completed sessions and causal daily signals

Implemented and tested locally with mocked providers; no Render/database or live
Yahoo connectivity testing was performed.

- NYSE calendar handles holidays, DST and early closes. A daily session becomes
  usable 30 minutes after its scheduled close. In-progress candles are excluded.
- Per-symbol locked JSON/PostgreSQL caches reuse completed bars, backfill missing
  history and fetch new sessions incrementally. Adding one ticker reuses cached
  tickers and shared SPY/VIX/VIX3M. Missing dates are explicit, not fabricated;
  same-session failed/missing requests retry on a new session or explicit force.
  Historical requests do not erase newer cached bars.
- Provider adjustment policy is explicit: Yahoo `auto_adjust=False` OHLC is
  provider split-adjusted, not dividend-adjusted. It is **not guaranteed original
  historical execution prices**. Legacy CSV caches are not trusted under this new
  policy. Newly observed splits require historical refresh to avoid mixed scales.
- Shared strategy.py drives active live snapshots and backtests. Version
  `daily-close-term-structure-v3` uses actual contemporaneous `^VIX / ^VIX3M`,
  exact-date SPY/VIX inputs with no forward/backward filling, past-only rolling
  windows, own-date SMA comparisons and finite-window ADX.
- ADX now uses finite rolling DM/DX smoothing rather than a moving EWM seed. This
  is a deliberate strategy change to make finite warmup stable. The score weights
  and confidence threshold remain, but old performance numbers are not comparable.
  The prior VIX proxy and new term-structure thresholds require empirical validation;
  **no profitability or parameter calibration claim is made**.
- Required current inputs missing: live result is unavailable, not a stale prior
  signal or made-up VIX value. A historical run with no usable warmup/benchmarks is
  rejected. Missing stock sessions reject replay rather than infer a later fill.
- Close-only exits ratchet a continuous position stop upward. Intraday low breaches
  alone do not exit. Pending decisions fill at the next stock-session open, with
  fills processed before that day's close equity/decision. Same-bar future-open
  accounting is removed. Cooldown/crash-recovery transitions live in the shared
  state evaluator. Last-close orders remain pending; no artificial terminal SELL.
  Optional terminal liquidation is separately labeled and reflected in equity.
- Backtests use the selected profile's confidence, expose decision session and
  execution date separately, and report pending orders. Cash dividends supplied
  by the provider are credited to modeled holdings. Splits use Yahoo's consistently
  split-adjusted price scale, **not** manual-ledger execution prices; this remains
  a synthetic adjusted-share replay, not an original-share broker simulation.
- Live finite indicator frames cache by input revision/version. Normalized cached
  precision is identical on first computation and reuse. Entry freshness means
  all gates transitioned from known false to true—not days above SMA200. Missing
  prior conditions do not prove a fresh transition. Trend age never asserts a
  recorded past alert. Benchmark helpers no longer backward-fill future prices.
- Portfolio Refresh appends immutable **eligibility observations** with session,
  observation time, strategy/config/input hashes and indicator evidence. Repeated
  identical refresh is idempotent; data/config revisions append another record.
  Accounts & Trades displays observations. These are not account-state orders,
  fills or claims that you actually received an alert on a past session.
- Backup v3 includes observed indicator evidence alongside profiles/accounts/events;
  v1/v2 remain importable with missing-history warnings. Full restores of older
  versions remove evidence absent from that backup. Raw provider bar history is a
  separate discardable cache and is **not** included: observations can be reviewed,
  but a v3 backup alone cannot reproduce the entire historical price path.
- Migration 3 adds market_symbol_cache and profile_signal_history. Runtime market
  cache is durable in configured PostgreSQL, isolated atomic files locally.

### Verification and remaining boundaries

Tests prove prefix/future-perturbation invariance on fixed inputs, finite-indicator
warmup stability, next-open chronology, stop ratchet, close-only exits, timer-state
parity, missing-current-term input rejection, wrapper/config parity, holiday/DST
and forming-candle behavior, download reuse, indicator-cache reuse, immutable
observation revisions and v1/v2/v3 backup compatibility. No provider coverage or
real-market performance validation was performed. `^VIX3M` availability remains a
provider requirement; missing data is not silently replaced with the old ratio.

**Legacy portfolio limitation:** old holdings still contain no entry dates or
continuous-position/cooldown history. Their inferred peaks/stops and pyramid
suggestions remain estimates, prominently labeled in both dashboards. Full
position-state parity requires identical input/config/state; a legacy dashboard
recommendation is not guaranteed to equal a single-symbol replay trade.

**Next milestone:** ledger-aware, jointly cash/slot/exposure-constrained portfolio
planning, then persistent paper execution. Current advisory feasibility still
checks candidates independently and must not be treated as an affordable order
list. Auto paper trading, daily account valuations/returns, observation-to-action
statistics and point-in-time corporate-action/raw-execution replay remain pending.

Provider historical corrections can change reconstructed backtests. Previously
recorded observations remain preserved; prefix tests do not make Yahoo history
immutable or prove a strategy will work in real trading.

## Milestone 2 — accounts and dated trade history

Implemented locally; no Render connection or deployment testing was performed.

- **Accounts & Trades** in the navigation creates one independent manual and one
  paper USD account per profile. New accounts start empty with zero cash; no
  funding or legacy trades are fabricated.
- Record actual deposits, withdrawals, buys, sells, total execution fees, cash
  dividends and split ratios. Local browser times are stored as UTC timestamps;
  occurrence time and entry time are separate. Same-timestamp events use entry
  sequence as a tie-breaker. Historical batches validate together and commit once.
- Whole-share buys only; fractional sells support split-created residuals. A split
  changes shares, never total cost basis. Splits/dividends are entered explicitly;
  this milestone does not automatically download corporate actions.
- Cash and positions are derived from recorded history. Decimal FIFO includes buy
  fees in lot cost and deducts sell fees from proceeds; partial sales consume oldest
  lots. Dollar net profit excludes deposits/withdrawals and includes dividends.
- Per-stock results show realized profit, fees, dividends and trade classifications.
  Realized profit attribution uses the sold FIFO lots' **buy** classification;
  exits retain their own tag. Tags are user annotations, not evidence of an actual
  historical strategy signal or causal strategy outperformance.
- Each event needs a UUID idempotency key. Identical retries do not duplicate
  events. Negative cash at any historical point, oversells, future executions and
  invalid dates/numbers are rejected before saving.
- Corrections append a VOID record and retain the original. If voiding alone would
  invalidate later trades, submit a batch with VOID and valid replacement events.
  Paper accounts permit funding and funding corrections only; users cannot invent
  simulator executions. Automatic paper trading is still the later milestone.
- Legacy holdings and the existing signal dashboard are intentionally unchanged.
  The account page compares shares and average cost so complete entered history
  can be reconciled before any future switch to ledger-managed portfolio signals.
- Optional user-supplied quotes value open positions; quotes are not persisted or
  independently verified. Missing quotes leave equity/unrealized/net profit
  explicitly unpriced. Daily equity history, automatic market quotes, drawdown and
  time-weighted returns need the next market-data/strategy milestone.
- PostgreSQL migration 2 adds per-profile JSONB account state with cascade deletion;
  account events and profile updates share the repository lock and transaction.
  Local mode upgrades its atomic state document to version 2 without changing
  original legacy files.
- Backup schema **v2** includes accounts and complete event history, numeric values
  as decimal strings, and correction references. Imports validate full ledger
  replay before changing anything. Import-as-new remaps account/event UUIDs and
  correction references; full restore preserves source profile/account/event IDs.
  Version 1 remains importable but contains **no account history**. Full restoring
  v1 therefore removes current account history; import-as-new is safer.

### Local verification

Local pytest tests cover funding, FIFO partial exits, fees, splits/dividends,
subsecond chronological ordering, account isolation, idempotency, historical batch
corrections, rollback, malformed values and v1/v2 backups. External PostgreSQL
tests are excluded for this milestone as requested. Browser smoke tests use
temporary data and real CSRF: deposit, buy, quote valuation and partial sell gave
cash $918 and realized P&L $18.50; paper trade choices were disabled.

### Entry workflow

1. Choose profile, create/select its manual account.
2. Enter actual initial funding and subsequent deposits/withdrawals at their dates.
3. Enter dated buys, sells, fees, dividends and splits; use batches when correcting
   historical records together. Exact timestamp ties follow entered sequence.
4. Compare ledger shares/cost with Legacy holdings reconciliation. Differences may
   be missing history or fee-inclusive FIFO cost versus old average cost.
5. Supply optional quotes for a current dollar valuation, then export v2 from
   Settings to preserve every account event outside the application.

## Milestone 1 — existing profile and backup foundation

## Implemented

- Validated profile/settings and holdings writes, atomic single-holding updates,
  failure propagation, unknown-profile rejection, and preserved zero settings.
- PostgreSQL remains authoritative when `DATABASE_URL` is configured. A database
  failure returns an error, not empty holdings or a separate local account.
- Local development migrates legacy profile/holdings JSON into one atomic,
  cross-process-locked document. Original files are preserved for reconciliation.
- Version 1 downloadable JSON backup includes every saved profile setting,
  watchlist, current holding and selected default-profile reference. It excludes
  credentials, price caches and analysis caches. Numeric values use strings in
  the backup; imported values retain current application's numeric precision.
- Backup validation/checksum, preview, new-profile import with ID remapping and
  unique names, and explicitly confirmed full replacement. Imports commit once.
  A replacement requires downloading a current all-profiles backup first.
- Profile-isolated analysis caches with settings/holdings fingerprints. Saved
  pyramid percentages and profile confidence are passed into dashboard analysis.
- Refresh now uses POST; login and same-origin CSRF protection cover mutations.

## Render deployment — required before deploying this update

1. Keep the existing PostgreSQL `DATABASE_URL` and existing Gunicorn start command.
2. Run configure_security.py locally using the editor's Python run action. Enter
   the password directly into the terminal, not chat. Copy the printed
   `APP_PASSWORD_HASH` and `FLASK_SECRET_KEY` into Render's Environment settings.
   Keep the password and values private; never commit them. Reuse the secret key
   across workers/redeploys so browser sessions stay consistent.
3. Set those variables **before deploying** this update. Render/database-enabled
   deployments intentionally return 503 until both are configured. Local-only
   development without authentication is restricted to loopback access.
4. Install runtime dependencies from requirements.txt (includes filelock).
   Existing Procfile / `gunicorn app:app` remains valid. Use HTTPS on Render.
5. Startup adds schema_migrations and profile_analysis_cache without deleting
   legacy tables. Migration 1 is serialized and transactional. Existing legacy
   initialization precedes it; broader migration cleanup remains planned.
6. Sign in, open Settings, and export all profiles. Keep the downloaded file on
   your computer or another durable storage service **outside Render**.
7. Do not treat any file under Render's app filesystem as a durable backup.

## Moving to a new Render database

Before expiry, export all profiles and verify the downloaded file exists. Create
the replacement PostgreSQL instance, update `DATABASE_URL`, and redeploy with the
same security variables. In Settings choose the saved backup, inspect the preview,
and import as new profiles. This preserves any existing profiles on the new DB.
Use the explicit replace-all action only if a full replacement is intended; it
downloads the current data before requesting the confirmation phrase. Select a
restored profile using **Use**, then refresh analysis.

Preview tokens expire after 30 minutes and become invalid after use. If profiles
change after preview, repeat the preview. Version 1 rejects unsupported formats,
unknown fields and newer versions rather than silently discarding future data.
SHA-256 detects file corruption; it is not a digital signature or encryption.
Backups contain personal holdings and should be stored privately.

## Verification and limitations

Offline pytest/Flask tests cover CRUD validation, local migration/concurrency,
auth/CSRF, cache isolation and backup roundtrip/rollback. Tests use temporary data
and never connect to the configured Render database or download market prices.
Development test dependencies are in requirements-dev.txt.

Browser smoke testing used temporary local accounts, with real CSRF protection:
backup selection/preview, import-as-new, preservation of existing profiles, and
saving a zero cash-reserve setting succeeded. No Render data was accessed.

The local Docker engine was unavailable, so real PostgreSQL roundtrip/migration
testing is not yet verified. Optional integration tests require a disposable
database explicitly authorized by TEST_DATABASE_URL; never use production there.

Milestone 1 originally used UTC run-date freshness; milestone 3 above replaces it
with completed exchange-session/version checks. Full ledger-aware portfolio
allocation and account-execution parity are still pending.

The trade/funding ledger is implemented in milestone 2; the shared causal engine,
term-structure inputs and observed eligibility are implemented in milestone 3.
Ledger-aware allocation and paper trading remain pending. Version 1 is a profile
backup; version 2 adds account history and version 3 adds observed evidence. None
is a complete PostgreSQL dump or a raw price-history reproduction package.
Existing legacy holdings remain cost basis snapshots; no historical executions
are invented.
