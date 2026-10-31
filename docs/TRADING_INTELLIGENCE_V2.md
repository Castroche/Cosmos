# COSMOS Trading Intelligence v2

## Production contract

COSMOS production trading is event-driven. The only runtime modes are `SHADOW`,
`OKX_DEMO`, and `LIVE_AUTHORIZED`; the former scheduled Agent and automatic
`screen → backtest → execute` runtime is retired. Its mutation endpoints return
HTTP 404 because no compatibility routes are registered. Historical goals, runs,
strategies, orders, and positions remain
available as read-only audit evidence through
`GET /api/trading-intelligence/migration-summary` and the existing ledgers.

The hot path is:

```text
official/market event → MarketState v2 → MarketAnalyst → PortfolioTrader
                      → MARKET/LIMIT TradePlan → immediate deterministic risk validation
                      → deterministic RiskAllocation → protected OKX execution
                      → deterministic Position Manager → DecisionEpisode outcome
```

Ordinary quotes update `MarketStateV2`; they do not invoke an LLM. An LLM can be
recalled only for no active thesis, a persisted wake firing, plan/thesis expiry or
invalidation, a trade terminal event, a strict official event, or a bounded retry.
Each WakeCondition instance is claimed once in SQLite, including across restarts.
The claim identity is scoped to its owning Plan/WaitRuntime, so a later LLM plan
may reuse a readable condition label without being blocked by historical state.
New `EXTERNAL_EVENT` waits are restricted to registered producers (currently
`QUANT_PREDICTION_UPDATED`); trusted official events use the dedicated major-event
dispatcher. A `NO_TRADE` wait must retain a market-derived wake and is bounded to
60 minutes (`WAIT` to 120 minutes). Non-trading wait normalization removes
unreachable, already-true, unresolvable, or symbol-scale-invalid non-trading waits,
adds a regime-change fallback, and clamps expiry without changing direction,
entry, stop, target, size, or leverage.
Strict regime/structure and volume-backed 5m/15m trend transitions may recall a
no-thesis decision once even when its narrower price rule has not fired. Ordinary
ticks, persistent conditions, active TradePlans, and positions do not use this
fallback.
Rejected wake decisions receive one durable five-minute retry; repeated invalid
no-thesis decisions enter a 30-minute fail-closed cooldown instead of recalling
the LLM on every quote.

## Market and event data

The launch scope is OKX BTC, ETH, and SOL USDT perpetuals. `MarketStateV2`
contains 1m, 5m, 15m, 1h, 4h, and 1d features, a content hash, `data_as_of`, and
independent `healthy/degraded/unavailable` status for every feature group.
Unavailable values are omitted; zero filling is rejected.

Official-source collectors normalize BLS CPI/NFP and OKX system status into a
bounded event schema. Fed and SEC inputs must enter through the same normalized
contract before being exposed to a model. Raw external prose is never included in
an LLM prompt. ETF flow is explicitly `unavailable` because there is no reliable
official free real-time source.

Risk windows are fixed at CPI/NFP -15/+5 minutes and FOMC -30/+15 minutes.
An active OKX incident or maintenance window blocks new risk until recovery.

## Decision and execution boundaries

`MarketAnalyst` has no trading tools and is not a reviewer. `PortfolioTrader` is
the sole strategy authority and may return only `LONG`,
`SHORT`, `WAIT`, `NO_TRADE`, `MANAGE_EXISTING`, or `EXIT`. `TradePlan` has no
quantity, risk fraction, leverage, or exchange request fields. Invalid structured
output receives one schema-only repair; a second failure creates a
`DECISION_ERROR` DecisionEpisode. There is no semantic reviewer, Critic, logic
repair call, reward/risk approval gate, or strategy-scale approval gate.

Demo/Shadow Quant is explicitly advisory. PortfolioTrader receives position and
operational state but not equity, cash, available margin, quantity, or leverage;
affordability cannot be used as an LLM veto. `NO_TRADE` requires an enumerated hard
blocker. In Demo/Shadow, healthy and operationally certain flat portfolios must
receive a concrete LONG or SHORT plan, including in range/transition regimes.
The LLM entry zone is converted directly into the sole `PRICE_IN_ZONE` execution
trigger; its lifetime is always the TradePlan lifetime. Reward/risk is recorded
as evidence but does not approve or reject the setup.

An entry wake is durably claimed and the plan is moved to `TRIGGERED` before any
exchange side effect. Risk allocation is then computed from equity, stop distance,
liquidity, correlation, exposure, daily drawdown, and authorization limits. The
execution adapter reuses commissioned OKX native TP/SL, idempotent client IDs,
partial-fill handling, private streams, and REST reconciliation. Any failure after
the durable execution claim writes an uncertainty event and halts new risk.

Position changes are handled by deterministic hard stops, staged targets,
break-even, ATR trailing, maximum holding time, and bounded exit conditions. They
never call an LLM. `MANAGE_EXISTING` updates these machine rules while preserving
the commissioned native TP/SL floor. `EXIT` persists an exit request which the
five-second reconciler executes with an idempotent reduce-only order.

Only one active Thesis is retained per symbol. A Thesis has an effective maximum
active age of four hours; expiry cancels its WaitRuntime and any unfilled Plan
before a fresh event-driven decision. `NO_TRADE` cannot retain a Thesis.

## HumanLiveGrant

Saving Live credentials does not authorize an order. `HumanLiveGrant` is bound to
OKX, symbols, per-trade/portfolio risk, leverage, daily drawdown, open-position
count, configuration fingerprint, two explicit confirmations, and expiry. It is
valid for 24 hours by default and at most seven days, and can be revoked at once.

Issuance requires Live credentials, commissioned native protection and position
management, healthy reconciliation, and no unknown/uncertain/unprotected order or
position. If a valid grant is absent, a Live request routes to OKX Demo when Demo
credentials exist, otherwise to Shadow. The Live adapter is never constructed for
the fallback route.

## Quant Intelligence v1

Quant v1 is BTC/ETH/SOL 5m only. Training uses a 2 ATR target, 1 ATR stop, 24-bar
triple barrier, purged walk-forward validation, 24-bar purge/embargo, cost-adjusted
labels, LightGBM multiclass/regression models, out-of-fold probability temperature
calibration, Brier score, log-loss, and a full model fingerprint.

Train a portable artifact with:

```powershell
.\.venv\Scripts\python.exe scripts\train_quant_intelligence_v1.py `
  training-contract.json data\models\quant-intelligence-v1\btc-usdt-swap.json
```

The training contract contains `symbol`, causal 5m `candles`, `feature_names`, and
one `features_by_candle` row per candle. Artifact filenames are
`btc-usdt-swap.json`, `eth-usdt-swap.json`, and `sol-usdt-swap.json`. The runtime
updates MarketState every minute and performs one inference per closed 5m bucket;
predictions expire after ten minutes. Missing or non-finite features reject
inference. Live LONG/SHORT requires a fresh, direction-aligned, positive-cost-edge
prediction from a model whose purged OOF Brier/log-loss beat the uniform
three-class baseline. Shadow and Demo retain Quant as counterfactual/advisory
evidence and remain able to exercise the full protected execution path.

## API and operations

All production state is under `/api/trading-intelligence/*`, including overview,
MarketState refresh, official events, Quant models, episodes, events, artifacts,
decisions, plan execution, migration summary, and Live Grant preview/issue/revoke.

The UI workbench follows:

```text
MarketState → Thesis → Plan → Wake → Trigger → Risk → Position → Outcome
```

Cleanup is deliberately separate from trading. Run the audit first:

```powershell
.\.venv\Scripts\python.exe scripts\audit_v2_cleanup.py
```

The generated manifest uses `KEEP`, `DELETE`, `REBUILD`, and `REVIEW`, includes
absolute paths, sizes, row counts, reasons, and reference sources, and must show no
`DELETE` or `REVIEW` entries after cleanup. Never use `git clean -fdx` on this
workspace. Raw Binance/OKX archives and their checksums are immutable evidence.


## Learning evidence and observational comparisons

Learning remains advisory. Complete outcomes acquire a first-observed timestamp; decision-time
retrieval cannot consume evidence first observed after that decision. A pinned snapshot is
supported, as are disabling learning and reverting to automatic snapshot updates.

The optional shadow Trader reuses the Analyst output and all non-learning inputs. Stable 10%
sampling and a durable daily limit of ten groups (Asia/Shanghai) bound additional calls. Each
group allows one Trader request and at most one format repair, with no HTTP retry or provider
fallback. It never enters the order/plan/wake/position pipeline. Its price outcomes are explicitly
labelled proxies, not executable fills or profit. The default setting is enabled; requests only
run while automation is active and valid learning cases exist.
