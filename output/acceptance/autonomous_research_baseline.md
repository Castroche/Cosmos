# COSMOS Autonomous Research Baseline

- Captured: 2026-08-22 (Asia/Shanghai)
- Workspace: `F:\Cosmos`
- Scope: pre-upgrade read-only architecture and regression baseline
- Database: `data/cosmos.db`, 3,514,572,800 bytes, SQLite WAL, `PRAGMA quick_check = ok`

## Regression baseline

| Check | Result |
|---|---|
| Backend tests | PASS — 569 passed, 1 dependency deprecation warning, 96.86s |
| Python lint | PASS — ruff reported no violations |
| Frontend typecheck | PASS |
| Frontend lint | PASS |
| Frontend tests | PASS — 3 passed |
| Frontend production build | PASS — 1,597 modules; main JS 563.09 kB (chunk-size warning) |

The only observed warnings are the Starlette/FastAPI `httpx` test-client deprecation and the
frontend bundle-size advisory. Neither changes the baseline result.

## Architecture map

```text
Exchange/archives -> immutable ResearchDataset + feature sidecars
                  -> StrategySpec / strategy library / AI proposal
                  -> exact event Backtest
                  -> Research Campaign v17
                     -> cumulative lineage and trial-family accounting
                     -> strategy-level BH + DSR + PBO
                     -> one-time sealed blind evaluation
                     -> CampaignExecutionCertificate
                  -> plan lifecycle -> risk -> order ledger -> execution

Market events -> Agent coordinator -> bounded tool registry
              -> regime analysis + fixed-family adaptive candidates
              -> Campaign/screen/plan/backtest/execute tools
```

## Dependency graph

- `strategy_spec` defines the three closed legacy families and is consumed by the library,
  backtest, lifecycle, API and Agent adaptation paths.
- `backtest` is the authoritative deterministic execution-semantics engine and feeds Campaign,
  candidate lifecycle and plan validation.
- `research_campaign` depends on backtest and statistical validation; API orchestration supplies
  immutable datasets, trial-family state, blind-set bindings and cell-cache persistence.
- `storage` owns SQLite initialization, additive compatibility alterations, Campaign/trial-family
  ledgers, blind consumption, candidates, certificates and Agent state.
- `agent/coordinator` and the tool registries orchestrate bounded research and trading actions;
  risk and execution remain separate deterministic boundaries.

## Existing reusable controls

- Campaign engine v17 with cell-level content cache and replay.
- Permanent cumulative evidence accounting at research-lineage and broad market-type trial-family
  levels, including retry idempotency and blind-set sealing after use.
- One-time chronological holdout binding and certificate fingerprints.
- DSR, one-sided Sharpe evidence, batch BH adjustment and PBO with confidence bounds.
- Deterministic event backtest with fees, spread/slippage, latency and conservative same-bar order
  semantics.
- Existing risk controls, order idempotency ledger, recovery/reconciliation and execution fault
  tests.
- Runtime Agent tool capability registry and LLM gateway with structured schemas.

## Confirmed upgrade gaps

1. Campaign admission still uses median win rate, pooled Wilson win-rate lower bound and a
   one-sided 50% win-rate null as hard gates; this can reject positive-expectancy asymmetric
   strategies.
2. The current trial tables aggregate fingerprints/counts but do not persist a complete
   append-only per-empirical-trial record or online alpha-wealth transitions.
3. `ResearchCampaignReport` carries detailed `blind_candidate` evidence inside the main report;
   the Agent-facing contract needs a separate redacted DTO and certification boundary.
4. Dataset facts primarily use one `timestamp`; full event/source/available/ingestion/revision
   semantics are not represented consistently.
5. StrategySpec is a safe but closed three-family schema. There is no finite typed general DSL,
   canonical strategy IR or dual compiler backend.
6. AI proposal exists as an API capability but is not an autonomous, capability-scoped Research
   Agent tool chain. The live Agent mainly selects or mutates bounded existing families.
7. Current cache keys cover final Campaign cells, not feature cubes, signals, cost scenarios,
   bootstrap references or behavior fingerprints.
8. Execution certificates do not model generator envelopes, short-lived fast eligibility,
   append-only revocation or effective forward-evidence certification.
9. Storage initialization uses embedded `CREATE TABLE` and opportunistic `ALTER TABLE`; there is
   no explicit ordered `schema_migrations` ledger.
10. The workspace had no Git history before this upgrade. Phase 0 establishes a secret-safe
    baseline repository; runtime build identity will bind Git SHA, source-tree hash and dirty state.

## Current decision

The existing acceptance remains `HOLD`: engineering infrastructure is healthy, but there is no
qualified cross-market Champion and no stable profitability evidence. This baseline authorizes
architecture work only; it is not evidence of Alpha.
