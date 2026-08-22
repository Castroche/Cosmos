# COSMOS Autonomous Research Final Upgrade — Final Acceptance

- Completed: 2026-08-22 (Asia/Shanghai)
- Workspace: `F:\Cosmos`
- Baseline commit: `68468e86a0cf647e388cad8dec3c4405bd2fc8c5`
- Baseline tag: `cosmos-autonomous-research-baseline`
- Campaign engine: v18
- Final database: `data/cosmos.db`, SQLite WAL, `PRAGMA quick_check = ok`
- Decision: engineering acceptance PASS; profitability remains unproven and requires independent
  forward evidence.

## Before / after architecture

Before:

```text
Market data -> fixed StrategySpec families -> exact backtest -> Campaign v17
            -> batch statistics -> one-time blind set -> legacy certificate
            -> risk -> execution
```

After:

```text
Versioned point-in-time MarketSnapshot
  -> ResearchHypothesisV2 (persisted first)
  -> certified generator or finite JSON DSL v2
  -> parser -> typed DAG -> semantic validator -> CanonicalStrategyIR
  -> static/differential/recursive/warmup/availability/immutability gates
  -> AST -> FeatureGraph -> pre-return behavior novelty
  -> permanent empirical Trial + global/family LORD++ alpha reservation
  -> NumPy fast intent backend -> purged OOF
  -> Canonical IR exact event replay
       (latency, fee, spread, slippage, partial fill, stop, lifecycle)
  -> deterministic 500-block bootstrap + DSR/PBO/FDR/cost stress
  -> Portfolio Gate Lite -> latest market/spread/liquidity Risk Gate
  -> EXECUTION_ELIGIBLE (certified provenance, clean build, one-bar TTL, low risk)
     or SHADOW_ONLY / REJECTED / UNTESTABLE
  -> effective forward evidence + 5000-block Slow Gate
  -> immutable FullStrategyCertificate + append-only revocation
```

The LLM boundary ends at structured Research tools. Trial accounting, compilation, statistics,
certificates, risk and execution are deterministic services.

## Phase results

### Phase 0 — baseline and build identity

- Secret-safe `.gitignore` excludes databases, WAL/SHM, market data, caches, logs, `.env`, keys,
  credentials and build output while retaining acceptance reports.
- Local Git baseline was committed and tagged before core changes.
- `RuntimeBuildIdentity` binds full Git SHA, tracked source-tree SHA-256, dirty state and capture
  time. Dirty builds may research and Shadow but cannot issue Generator Envelope, fast
  eligibility or Full Strategy certificates.
- Pre-migration consistent backup:
  `data/backups/cosmos-pre-autonomous-v2-20260822.db` (3,514,572,800 bytes, quick check OK).
- Baseline report: `output/acceptance/autonomous_research_baseline.md`.

### Phase 1 — research integrity and Campaign v18

- Removed win-rate 50%, Wilson lower bound, payoff and PF from universal qualification. They remain
  descriptive/ranking evidence.
- Campaign qualification now uses net/cost-adjusted expectancy, Expected R from pre-committed
  entry risk, deterministic moving-block lower bounds, DSR, PBO, purged OOF, cost stress and
  multiple-testing controls.
- Missing risk units, features, PIT semantics or sample size produce `UNTESTABLE`, not a false
  negative strategy result.
- Fast Expected R uses 500 deterministic blocks; Slow Certification uses 5000. Seeds derive from
  the complete artifact fingerprint.
- Append-only `PermanentTrialLedger` reserves empirical attempts before a cache, return, PnL,
  Sharpe or expectancy is read. Retry keys are idempotent; new windows remain new trials; crashes
  and timeouts do not refund budget.
- Persistent global and family LORD++ power-law streams record ordered allocation/result events.
  Parallel allocations cannot consume unconfirmed same-batch discoveries. Invalid restored state
  fails closed to validation error/conservative spending.
- Agent-facing blind results are restricted to `PASS`, `FAIL`, `SHADOW_REQUIRED`, coarse reason
  codes and sealed evidence references. Blind IDs, market splits and performance metrics are
  removed from Agent DTOs.
- OHLCV and derivative fact tables now carry event, available, source, ingestion and revision
  metadata. Joins use availability time; legacy OHLCV is adapted only through explicit closed-bar
  semantics.

### Phase 2 — finite Strategy DSL v2

- JSON only: no Python source, `eval`, `exec`, import, subprocess, filesystem, raw network or raw
  database capability.
- Processing is fixed: JSON parser -> Pydantic typed model -> DAG/type/complexity validator ->
  immutable Canonical IR.
- Graph cycles, negative lag, missing or unbounded windows, implicit incompatible types, excess
  nodes/lookback and side-effect-like parameters are rejected before returns.
- Existing trend, mean-reversion and breakout StrategySpecs remain unchanged and compile through
  legacy certified-generator adapters; their historical fingerprints remain stable.

Operator registry includes version, input/output type, lookback, warmup, PIT policy,
available-time policy, revision policy, determinism and fast/exact support. The required v2 surface
is present:

- Time series: `lag`, `diff`, `pct_change` (plus compatible difference/return aliases).
- Rolling: `rolling_mean`, `rolling_std`, `rolling_min`, `rolling_max`, `rolling_quantile`.
- Normalization: `zscore`, `rank`, `percentile`.
- Statistical: `corr`, `beta`, `residual`.
- Cross-sectional: `cs_rank`, `demean`, with causal time-by-asset panels and explicit target asset.
- Crypto: `funding`, `open_interest`, `basis`, `spread`, `orderflow_imbalance`,
  `trade_imbalance`.
- Logic: `AND`, `OR`, `NOT`, `GT`, `LT`, `CROSS_ABOVE`, `CROSS_BELOW`.
- Risk: `time_stop`, `price_stop`, `volatility_target`, `max_position`, `max_holding`.

### Phase 3 — Canonical IR and dual backend

- Canonical IR uses stable field ordering, normalized Decimal strings, evaluation clock, warmup,
  feature/operator versions and risk intent; SHA-256 identity is immutable.
- NumPy fast backend evaluates continuous arrays and pre-materialized feature graphs.
- Exact adapter consumes the same IR intent. Its event replay applies next-bar latency, adverse
  spread/slippage, fees, volume participation/partial fills, fixed entry risk stops and ordered
  exits. Fill/PnL differences are allowed; feature, signal, entry/exit/position intent and
  invalidation must match point by point.
- Numba/Rust was not introduced because measured NumPy performance is far inside the SLO.

### Phase 4 — 300-second Fast Gate

- `DeadlineContext` starts from `MarketSnapshotReady`, exposes elapsed/remaining time, stage
  deadlines and cancellation, and reserves the final 15 seconds for persistence.
- The committed result and final Trial result are written in one SQLite `BEGIN IMMEDIATE`
  transaction. Deadline failure commits `UNTESTABLE(deadline_exceeded)` and cannot add risk.
- Persistent event/family budgets enforce 12 semantic proposals, 3 empirical trials, 2 exact
  finalists and 1 evolution round across restart and retries.
- Caches: `RollingFeatureCube`, `SignalCache`, `CostScenarioCache`,
  `BootstrapReferenceCache`, `BehaviorFingerprintIndex`; namespace keys derive from the full
  `ArtifactFingerprintV2`.
- Artifact identity binds dataset/schema/availability, feature AST, IR, operator versions, market
  scope, PIT semantics, compiler and both engine versions, cost/slippage parameters,
  purge/embargo, validation config, Git SHA, source-tree hash and dirty state.
- Market-data arrival freshness has a distinct SLA field and reason code; it is not hidden inside
  the 300-second research timing.

Benchmark artifact: `output/acceptance/autonomous_fast_gate_benchmark.json`.

| Scenario | p50 | p90 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|
| Snapshot ready, warm process/features | 0.137286s | 0.141734s | 0.142028s | 0.142292s | 0.142358s |
| Cold feature load + gate diagnostic | 0.183748s | 0.210883s | 0.214396s | 0.217501s | 0.218277s |

The benchmark ran 12 real local cases: BTC/ETH/SOL, 1h/4h, two DSL variants and up to 2000 bars
per case. All 12 were honestly rejected by evidence/novelty gates; no performance or acceptance
threshold was weakened to manufacture a trading PASS. Warm p95 is below 240 seconds and every
case is below the 300-second hard deadline.

### Phase 5 — hypothesis and Agent boundary

- `ResearchHypothesisV2` records observation, economic mechanism, causal mechanism, prediction,
  horizon, required features, regime, falsification/invalidation, source, parent and creation time.
  It is immutable and persisted before candidate compilation.
- Registered tools: `detect_research_opportunity`, `propose_hypothesis`,
  `propose_strategy_dsl`, `propose_mutation`, `review_candidate`, `summarize_failure`.
- Tools return bounded structured DTOs. They expose no database handle, Python executor,
  certificate signer, risk mutation or order entry.
- One-shot LLM generation has an eight-second budget and no same-event retry. Gateway timeout
  falls to certified deterministic generator mutation (`DEGRADED_AGENT`); generator failure falls
  to `SAFE_MODE` and existing valid certificates or HOLD.

### Phase 6 — novelty, lineage and evolution

- Persistent hierarchy and lineage bind event, family, structural candidate, parameter instance,
  execution variant, parents, operation, changed nodes and before/after fingerprints.
- De-duplication order is AST -> FeatureGraph -> pre-return Behavior. Behavior uses signals,
  positions, trade overlap, holding and turnover before PnL is read. PnL correlation remains an
  empirical, trial-counted statistic.
- Exactly nine bounded operations are exposed: ReplaceFeature, ReplaceTransform,
  AddRegimeFilter, RemoveRegimeFilter, ChangeExitRule, ChangeHoldingPeriod, CombineSignals,
  SimplifyStrategy and CrossStrategy.
- Symbol/exchange changes, minor parameters, Agent retry and process restart cannot reset the
  event/family budget.

### Phase 7 — envelope and certificate lifecycle

- Immutable `GeneratorEnvelopeCertificate` binds generator/policy versions, features/operators,
  AST/complexity/lookback, parameter and holding bounds, markets, execution/risk bounds,
  experiment budget, compiler/data/cost versions, expiry and revocation conditions.
- Provenance validation binds hypothesis hash, seed, generation fingerprint, envelope fingerprint
  and exact output IR hash.
- Fast eligibility requires certified provenance, in-envelope IR, Fast/Exact/Portfolio/Risk PASS,
  current regime/spread/liquidity PASS and clean build identity.
- Fast risk defaults to 5% of mature reference strategy risk, is capped at 10%, permits one active
  instance, prohibits scale-in/pyramiding/automatic leverage increase and expires after one target
  bar. Expiry blocks new risk but does not mechanically liquidate an existing managed position.
- Free LLM DSL and any envelope-external feature/operator/structure remain `SHADOW_ONLY` even with
  strong historical evidence.
- Certificate bodies never mutate. Compiler, feature, data source, cost/risk model, regime,
  spread, slippage and forward degradation append a revocation event.
- Legacy certificates retain existing behavior through compatibility paths and are not silently
  converted to v2 Full certificates.

### Phase 8 — Shadow, Slow Certification and portfolio

- Shadow advancement uses ESS, independent episodes/trades, regime/stress coverage, realized
  cost/slippage error, decay and calibration rather than elapsed wall time. ESS must be at least 30.
- Slow engine computes cross-market means, purged OOF, embargo, deterministic 5000-block Expected
  R, DSR proxy probability, PBO, FDR admission, cost stress and parameter stability, and requires
  sealed blind plus independent forward evidence references before signing.
- `FullStrategyCertificate` also requires clean build identity and is persisted/queryable through
  the v2 certificate store.
- Portfolio Gate Lite evaluates component expectation, marginal Sharpe, CVaR and MDD limits while
  binding signal/PnL/trade/exposure overlap evidence.

## Database migrations and reconciliation

`schema_migrations` is ordered and additive:

1. `legacy_schema_baseline`
2. `autonomous_research_integrity`
3. `point_in_time_fact_metadata`
4. `autonomous_research_v2_artifacts`
5. `autonomous_research_persistent_budgets`
6. `autonomous_research_novelty_index`

New durable areas include hypotheses, permanent trials/events, FDR streams/events, autonomous
runs/validation, budgets/events, artifact/cache index, AST/feature/behavior novelty, candidate
lineage, generator envelopes, certificates/revocations and Shadow evidence.

Migration safety:

- Store initialization creates a consistent quick-checked backup before a future schema upgrade.
- PIT changes are additive; old rows are not rewritten to invent publication times.
- Production post-migration quick check: `ok`.
- Pre/post row counts: 969 research datasets, 222 candidates, 129 Campaigns and 295 strategy plans.
- Dataset fingerprint reconciliation: 969/969 unchanged; zero mismatches.

## Public API

- `POST /api/research/autonomous-runs` — idempotent run creation and committed redacted result.
- `GET /api/research/autonomous-runs/{run_id}` — disposition, coarse codes, evidence references,
  timing and deadline status only.
- `GET /api/research/trials` — redacted permanent ledger query without return/blind metrics.
- `GET /api/research/certificates/{certificate_id}` — immutable body plus append-only revocation
  status.
- Existing StrategySpec, Campaign, Risk and Execution endpoints remain compatible.

## Acceptance A–E

- Acceptance A: integrated tests cover hypothesis-first persistence, semantic/integrity/novelty,
  permanent trial, Fast + exact replay, inside-envelope low-risk one-bar eligibility,
  outside-envelope Shadow and evidence-driven Full certificate creation.
- Acceptance B: injected LLM timeout proves deterministic-generator degradation and no trading
  side effect; dual failure produces SAFE/HOLD semantics.
- Acceptance C: tests cover trial increment/idempotency/restart, non-refundable limits, FDR
  restore, blind redaction, future-append signal immutability, AST/FeatureGraph/Behavior novelty and
  timeout fail-closed.
- Acceptance D: the BTC/ETH/SOL 1h/4h 12-case benchmark passes p95 and hard-deadline SLOs; cold and
  warm diagnostics are reported separately.
- Acceptance E: complete legacy regression, database reconciliation, frontend checks and build all
  pass.

## Final regression

| Check | Result |
|---|---|
| Backend | PASS — 602 tests |
| Ruff | PASS — backend and scripts |
| Frontend typecheck | PASS |
| Frontend lint | PASS |
| Frontend tests | PASS — 3 tests |
| Frontend build | PASS — 1,597 modules |
| SQLite quick check | PASS — `ok` |
| Legacy fingerprint/row reconciliation | PASS — zero dataset mismatches |

Non-blocking warnings remain unchanged: Starlette's TestClient/httpx deprecation and the 563.09 kB
frontend chunk advisory.

## Principal changed modules

- `build_identity.py`: runtime Git/source provenance.
- `research_integrity.py`: Expected R, permanent trials, blind DTO and online FDR.
- `strategy_dsl.py`: hypothesis/DSL/operator registry/Canonical IR and legacy adapters.
- `strategy_backends.py`, `strategy_integrity.py`: dual semantics and six integrity gates.
- `autonomous_research.py`: deadline, Fast/Slow gates, modes, portfolio and run result.
- `research_cache.py`: feature/signal/cost/bootstrap/behavior caches.
- `research_certificates.py`: artifacts, envelope, eligibility, Full/Shadow and revocation.
- `research_evolution.py`: hierarchy, budgets, tools and nine operations.
- `backtest.py`, `research_campaign.py`, `research.py`, `domain.py`: exact replay, Campaign v18,
  Expected R risk units and PIT facts.
- `storage.py`: migrations, atomic ledgers/budgets/runs, artifacts, lineage and certificates.
- `api.py`, `agent/reasoner.py`, `agent/coordinator.py`: redacted APIs and scoped Agent tools.
- `scripts/benchmark_autonomous_fast_gate.py`: reproducible Acceptance D benchmark.

## Known limits and remaining research risk

- The benchmark is local snapshot-ready performance, not venue/network/LLM market-data latency.
  External freshness is deliberately reported under its own SLA.
- Canonical v2 exact replay targets one traded asset per IR. Cross-sectional transforms accept a
  causal time-by-asset panel and select a target asset; multi-leg pair/carry fills continue through
  their existing separately certified execution paths.
- No production Full Strategy certificate or risk grant was issued by this upgrade. Real sealed
  blind and forward Shadow references must accumulate independently; the engine refuses to invent
  them.
- Strong engineering correctness cannot establish stable Alpha. Regime change, selection effects,
  costs, capacity and live execution remain empirical risks.

## Final determination

**COSMOS autonomous quantitative research architecture is engineering-complete; profitability
remains an empirical forward-evidence question.**
