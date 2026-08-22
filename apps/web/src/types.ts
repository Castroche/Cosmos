export type Candle = {
  timestamp: string
  open: string
  high: string
  low: string
  close: string
  volume: string
}

export type StrategySpec = {
  family: StrategyKind
  version?: number
  source?: 'system' | 'operator' | 'ai' | 'ml'
  hypothesis?: string
  parameters: Record<string, string | number>
  fingerprint?: string
}

export type Plan = {
  id: string
  lineage_id?: string
  supersedes_plan_id?: string
  name: string
  version: number
  algorithm: StrategyKind
  source_prompt: string
  exchange: string
  trading_mode: TradingMode
  market_type: MarketType
  margin_mode: 'cash' | 'cross' | 'isolated'
  leverage: string
  symbol: string
  timeframe: string
  side: 'long' | 'short'
  status: 'draft' | 'approved' | 'active' | 'paused' | 'archived'
  created_at: string
  signal_candle_at?: string
  data_as_of?: string
  regime: 'trend' | 'range' | 'breakout' | 'uncertain'
  confidence: string
  strategy_spec: StrategySpec | Record<string, never>
  strategy_spec_fingerprint: string
  entry: {
    order_type: 'limit' | 'market'
    zone: { lower: string; upper: string }
    trigger: string
    expires_at?: string
    max_slippage_bps: string
  }
  exit: {
    stop_loss: string
    take_profits: Array<{ price: string; close_fraction: string; rationale: string }>
    trailing_stop_atr?: string
    trailing_activation?: 'immediate' | 'first_target' | 'one_r'
    invalidation: string
  }
  sizing: {
    risk_fraction: string
    risk_amount: string
    suggested_quantity: string
    notional: string
  }
  minimum_reward_risk: string
  thesis: string
  reasons: string[]
  pause_conditions: string[]
  fingerprint: string
  human_summary: string
  approved_at?: string
  approved_fingerprint?: string
}

export type AgentAutonomyMode = 'observe' | 'research' | 'paper' | 'sandbox' | 'live_capped'

export type AgentPolicy = {
  id: string
  autonomy_mode: AgentAutonomyMode
  allowed_symbols: string[]
  allowed_timeframes: string[]
  max_risk_per_trade: string
  max_daily_drawdown: string
  max_open_positions: number
  max_trades_per_day: number
  max_leverage: string
  require_native_protection: boolean
  allow_agent_entry: boolean
  allow_agent_cancel_entry: boolean
  allow_agent_reduce: boolean
  allow_agent_exit: boolean
  allow_agent_halt: boolean
  fingerprint: string
}

export type AgentGoal = {
  id: string
  objective: string
  symbols: string[]
  timeframes: string[]
  autonomy_mode: AgentAutonomyMode
  risk_policy_id: string
  poll_seconds: number
  status: 'active' | 'paused' | 'completed' | 'archived'
  valid_until?: string
  created_at: string
  updated_at: string
}

export type AgentRun = {
  id: string
  goal_id: string
  trigger_event_id: string
  status: 'running' | 'waiting' | 'completed' | 'failed' | 'halted'
  step_count: number
  max_steps: number
  last_error: string
  created_at: string
  updated_at: string
  completed_at?: string
}

export type AgentDecision = {
  id: string
  run_id: string
  action: 'observe' | 'analyze' | 'research' | 'backtest' | 'propose_entry' | 'execute_entry' | 'wait' | 'cancel_entry' | 'reduce_position' | 'exit_position' | 'halt' | 'review'
  reason: string
  evidence: string[]
  uncertainty: string
  tool_call?: {
    id: string
    name: string
    arguments: Record<string, unknown>
  } | null
  expected_outcome: string
  abort_conditions: string[]
  created_at: string
}

export type AgentObservation = {
  id: string
  run_id: string
  kind: string
  ok: boolean
  summary: string
  data: Record<string, unknown>
  tool_call_id?: string | null
  created_at: string
}

export type AgentRunTrace = {
  run: AgentRun
  decisions: AgentDecision[]
  observations: AgentObservation[]
}

export type TradeEpisode = {
  id: string
  goal_id: string
  policy_id?: string
  run_id: string
  plan_id: string
  symbol: string
  side: string
  status: 'reserved' | 'pending_entry' | 'open' | 'awaiting_review' | 'closed' | 'canceled' | 'uncertain'
  entry_order_ids: string[]
  exit_order_ids: string[]
  exit_client_order_ids: string[]
  planned_risk: string
  realized_pnl?: string
  fees: string
  return_on_risk?: string
  summary: string
  opened_at: string
  closed_at?: string
}

export type AgentEpisodeRuntime = {
  episode_id: string
  plan_id: string
  symbol: string
  plan_status?: string | null
  leverage?: string | null
  entry_quantity: string
  average_entry?: string | null
  entry_fees: string
  stop_loss?: string | null
  take_profits: Array<{
    price: string
    close_fraction: string
    rationale?: string
  }>
  native_protection_status: 'verified' | 'uncertain' | 'not_applicable'
  protected_order_ids: string[]
  last_reconciled_at?: string | null
}

export type AgentMemory = {
  id: string
  memory_type: 'episodic' | 'strategy' | 'operational'
  content: string
  tags: string[]
  confidence: string
  evidence_refs: string[]
  created_at: string
}

export type AgentControlState = {
  goals: AgentGoal[]
  policies: AgentPolicy[]
  runs: AgentRun[]
  episodes: TradeEpisode[]
  episode_runtime: AgentEpisodeRuntime[]
  memories: AgentMemory[]
  shadow_trials: ShadowValidationTrial[]
  shadow_calibration: ShadowCalibrationSummary
  market_coverage: AgentMarketCoverage[]
  risk_state: RiskControlState
}

export type AgentMarketCoverage = {
  event_id: string
  goal_id: string
  symbol: string
  timeframe: string
  event_status: string
  event_created_at: string
  event_processed_at?: string | null
  dedupe_key: string
  attempt_count: number
  last_run_id?: string | null
  last_run_status?: string | null
  last_run_steps: number
  last_run_error: string
  retry_recovered: boolean
  next_scheduled_at: string
  coverage_status: 'healthy' | 'running' | 'queued' | 'degraded' | 'paused'
}

export type AgentStrategySummary = {
  plan_id: string
  name: string
  symbol: string
  timeframe: string
  family: string
  status: string
  source: string
  fingerprint: string
  updated_at: string
}

export type AgentMarketSense = {
  symbol: string
  timeframe: string
  status: string
  last_event_at?: string | null
  next_scan_at?: string | null
  last_run_status?: string | null
}

export type AgentGoalOverview = {
  goal_id: string
  objective: string
  status: string
  autonomy_mode: string
  symbols: string[]
  timeframes: string[]
  runtime_status: 'running' | 'waiting' | 'paused' | 'blocked' | 'offline'
  last_heartbeat_at?: string | null
  next_scan_at?: string | null
  last_action?: string | null
  last_action_summary?: string | null
  current_strategy?: AgentStrategySummary | null
  recently_generated_strategies: AgentStrategySummary[]
  market_sense: AgentMarketSense[]
  active_episode_count: number
  uncertain_episode_count: number
  blocking_reasons: string[]
}

export type AgentRuntimeOverview = {
  generated_at: string
  active_goal_count: number
  latest_heartbeat_at?: string | null
  risk_halted: boolean
  risk_reason: string
  goals: AgentGoalOverview[]
}

export type AgentLaunchRequest = {
  profile_id: string
  symbols: string[]
  timeframes: string[]
  autonomy_mode: AgentAutonomyMode
  risk_template: string
  objective: string
  poll_seconds: number
  authorize?: boolean
  preview_fingerprint?: string
}

export type AgentLaunchPreview = {
  profile_id: string
  grant_fingerprint: string
  confirmation_required: boolean
  reuses_goal_id?: string | null
  mode: AgentAutonomyMode
  symbols: string[]
  timeframes: string[]
  risk_summary: Record<string, string | number | boolean>
  blocking_reasons: string[]
}

export type AgentLaunchResult = {
  profile: {
    id: string
    goal_id?: string | null
    policy_id: string
    symbols: string[]
    timeframes: string[]
    autonomy_mode: AgentAutonomyMode
    risk_template: string
    grant_fingerprint: string
  }
  goal_id: string
  reused: boolean
  status: string
}

export type StrategyLibraryItem = {
  id: string
  name: string
  description: string
  origin: 'builtin' | 'personal' | 'agent'
  family: string
  status: string
  version: number | string
  strategy_spec: Record<string, unknown>
  spec_fingerprint: string
  executable: boolean
  hidden: boolean
  can_edit: boolean
  can_archive: boolean
  can_restore: boolean
  can_delete: boolean
  can_clone: boolean
  evidence_summary: string
  updated_at?: string | null
}

export type StrategyLibraryPage = {
  items: StrategyLibraryItem[]
  total: number
}

export type AuditEvent = {
  id: string
  timestamp: string
  category: string
  severity: string
  message: string
  data: Record<string, string | number | boolean | null>
}

export type AuditEventPage = {
  items: AuditEvent[]
  next_cursor?: number | null
}

export type ShadowValidationTrial = {
  id: string
  goal_id: string
  run_id: string
  decision_batch_id: string
  plan: Plan
  decision_at: string
  horizon_at: string
  selected_for_execution: boolean
  execution_eligible: boolean
  screening_rank: number
  screening_summary: string
  selection_score: Record<string, unknown>
  adaptation_evidence: Record<string, unknown>
  signal_evidence: Record<string, unknown>
  backtest_evidence: Record<string, unknown>
  outcome: {
    status: 'pending_entry' | 'open' | 'no_entry' | 'closed' | 'horizon_mark'
    return_on_risk?: string | null
    net_pnl: string
    unrealized_pnl: string
    exit_reasons: string[]
    evaluated_through?: string | null
    causal: boolean
    executable: boolean
  }
  created_at: string
  updated_at: string
}

export type ShadowCalibrationSummary = {
  trial_count: number
  decision_batches: number
  settled_outcomes: number
  settled_batches: number
  pending_entry: number
  open_positions: number
  no_entry: number
  wins: number
  losses_or_flat: number
  total_r: string
  expectancy_r: string
  rejected_settled: number
  rejected_positive: number
  rejected_nonpositive: number
  selected_settled: number
  selected_positive: number
  selected_nonpositive: number
  calibration_ready: boolean
  minimum_settled_batches: number
  causal: boolean
  executable: boolean
  independent_performance_evidence: boolean
}

export type AgentReadiness = {
  ready_for_autonomous_trading: boolean
  mode: AgentAutonomyMode
  checks: Array<{
    code: string
    passed: boolean
    message: string
  }>
  evaluated_at: string
}

export type AgentForwardPerformance = {
  scope_mode: AgentAutonomyMode
  status: 'collecting' | 'blocked' | 'failed' | 'qualified'
  qualified_for_live_review: boolean
  closed_episodes: number
  attributable_episodes: number
  independent_runs: number
  uncertain_episodes: number
  wins: number
  losses: number
  breakeven: number
  win_rate_percent: string
  net_pnl: string
  total_fees: string
  total_r: string
  expectancy_r: string
  recent_episodes: number
  recent_total_r: string
  recent_expectancy_r: string
  profit_factor?: string | null
  max_drawdown_r: string
  max_consecutive_losses: number
  observation_days: string
  first_episode_at?: string | null
  last_episode_at?: string | null
  checks: Array<{
    code: string
    passed: boolean
    current: string
    requirement: string
    message: string
  }>
  symbol_reports?: Array<{
    symbol: string
    status: 'collecting' | 'blocked' | 'failed' | 'qualified'
    qualified: boolean
    attributable_episodes: number
    independent_runs: number
    wins: number
    losses: number
    breakeven: number
    win_rate_percent: string
    net_pnl: string
    total_r: string
    expectancy_r: string
    recent_episodes: number
    recent_expectancy_r: string
    profit_factor?: string | null
    max_drawdown_r: string
    max_consecutive_losses: number
    observation_days: string
    checks: Array<{
      code: string
      passed: boolean
      current: string
      requirement: string
      message: string
    }>
  }>
  evaluated_at: string
}

export type RiskControlState = {
  halted: boolean
  reason: string
  source: string
  halted_at?: string
}

export type StrategySignal = {
  algorithm: StrategyKind
  symbol: string
  timeframe: string
  direction: 'long' | 'short' | 'flat'
  regime: 'trend' | 'range' | 'breakout' | 'uncertain'
  confidence: string
  signal_candle_at: string
  data_as_of: string
  reasons: string[]
  features: Record<string, string | boolean>
  actionable: boolean
}

export type StrategyMarketFit = {
  plan_id: string
  strategy_fingerprint: string
  symbol: string
  timeframe: string
  checked_at: string
  data_as_of: string
  verdict: 'aligned' | 'wait' | 'conflict'
  summary: string
  direction_matches: boolean
  family_matches: boolean
  signal: StrategySignal
  assessment: {
    selected_kind: StrategyKind
    confidence: string
    ambiguous: boolean
    scores: Record<StrategyKind, string>
    reasons: string[]
    features: Record<string, string | boolean>
  }
  market_quality?: {
    status: string
    source: string
    is_cached: boolean
    gap_count: number
    invalid_count: number
    message: string
  } | null
}

export type RiskCheck = {
  code: string
  passed: boolean
  message: string
  blocking?: boolean
}

export type BacktestWindowSummary = {
  label: string
  sample_bars: number
  data_start: string
  data_end: string
  net_profit: string
  return_percent: string
  max_drawdown_percent: string
  win_rate_percent: string
  profit_factor: string
  total_trades: number
}

export type BacktestResult = {
  initial_equity: string
  final_equity: string
  net_profit: string
  return_percent: string
  max_drawdown_percent: string
  win_rate_percent: string
  profit_factor: string
  sharpe: string
  total_trades: number
  annualized_return_percent: string
  calmar: string
  expectancy: string
  average_trade: string
  average_win: string
  average_loss: string
  payoff_ratio: string
  win_loss_quality_passed: boolean
  total_fees: string
  total_slippage: string
  total_funding: string
  exposure_percent: string
  annualization_periods: string
  sample_bars: number
  sample_years: string
  stress_return_percent?: string
  robustness_passed: boolean
  validation_reasons: string[]
  trade_sample_standard: number
  trade_sample_floor: number
  trade_sample_strong_floor?: number
  trade_sample_corroborated: boolean
  trade_sample_strongly_corroborated?: boolean
  trade_sample_tier?: 'standard' | 'near_standard_corroborated' | 'strong_multi_evidence' | 'insufficient'
  cost_model: Record<string, string | boolean>
  recent_window?: BacktestWindowSummary | null
  temporal_stability?: {
    mode: string
    lookahead_safe: boolean
    parameter_mode: string
    fold_count: number
    minimum_trades_per_fold: number
    evaluable_folds: number
    positive_folds: number
    positive_fold_ratio: string
    worst_fold_return_percent?: string | null
    passed: boolean
    reasons: string[]
    windows: BacktestWindowSummary[]
  } | null
  parameter_walk_forward?: {
    mode: string
    lookahead_safe: boolean
    selection_basis: string
    execution_gate: boolean
    candidate_count: number
    step_count: number
    evaluable_steps: number
    positive_steps: number
    positive_step_ratio: string
    cumulative_validation_return_percent: string
    passed: boolean
    reasons: string[]
    steps: Array<{
      validation_fold: string
      selected_label: string
      selected_spec_fingerprint: string
      training_fold_count: number
      training_trades: number
      training_score: string
      validation_trades: number
      validation_return_percent: string
      validation_profit_factor: string
      positive: boolean
    }>
  } | null
  statistical_validation?: {
    trial_count: number
    observation_count: number
    one_sided_sharpe_p_value: string
    deflated_sharpe_probability: string
    win_rate_lower_bound_percent: string
    bootstrap_payoff_lower_bound: string
    monte_carlo_drawdown_95_percent: string
    false_discovery_rate_passed: boolean
    uncertainty_passed: boolean
    reasons: string[]
  } | null
  integrity_checks?: Record<string, string | boolean | number>
  equity_curve: string[]
  strategy_attribution?: Array<{
    algorithm: string
    total_trades: number
    long_trades: number
    short_trades: number
    wins: number
    losses: number
    win_rate_percent: string
    net_profit: string
    return_percent: string
    profit_factor: string
    average_trade: string
    total_costs: string
    trade_share_percent: string
  }>
  trade_episodes?: Array<{
    entry_time: string
    exit_time: string
    side: 'long' | 'short'
    algorithm: string
    signal_regime: string
    pnl: string
    gross_pnl: string
    fees: string
    slippage_cost: string
    funding_cost: string
    exit_reasons: string[]
  }>
  trades: Array<{
    entry_time: string
    exit_time: string
    side: 'long' | 'short'
    algorithm?: string
    signal_regime?: string
    entry_price: string
    exit_price: string
    quantity: string
    pnl: string
    exit_reason: string
    gross_pnl: string
    fees: string
    slippage_cost: string
    funding_cost: string
  }>
  terminal_position_closed: boolean
  strategy_fingerprint?: string
  data_fingerprint?: string
  dataset_id?: string
  data_start?: string
  data_end?: string
  created_at?: string
}

export type BacktestJob = {
  id: string
  plan_id: string
  status: 'queued' | 'running' | 'completed' | 'failed'
  result?: BacktestResult
  error?: string
}

export type ResearchDataset = {
  schema_version: number
  id: string
  exchange: string
  market_type: string
  symbol: string
  timeframe: string
  candle_count: number
  data_start: string
  data_end: string
  data_fingerprint: string
  quality_status: string
  source: string
  gap_count: number
  duplicate_count: number
  invalid_count: number
  funding_count: number
  funding_status: string
  open_interest_count: number
  open_interest_status: string
  open_interest_coverage_ratio: string
  positioning_count: number
  positioning_status: string
  positioning_coverage_ratio: string
  basis_count: number
  basis_status: string
  basis_coverage_ratio: string
  order_flow_count: number
  order_flow_status: string
  order_flow_coverage_ratio: string
  timeframe_seconds: number
  expected_candle_count: number
  coverage_ratio: string
  closed_bars_only: boolean
  research_role: 'general' | 'development' | 'blind'
  source_dataset_id?: string | null
  blind_set_id?: string | null
  split_boundary?: string | null
  embargo_bars: number
  blind_sealed: boolean
  acquisition_manifest: Record<string, string | number | boolean>
  created_at: string
}

export type ResearchCampaignCandidate = {
  library_entry_id: string
  strategy_fingerprint: string
  family: string
  archetype: string
  market_count: number
  robust_market_count: number
  positive_stress_market_count: number
  unique_symbol_count: number
  unique_timeframe_count: number
  high_quality_market_count: number
  robust_market_ratio: string
  positive_stress_ratio: string
  high_quality_market_ratio: string
  median_return_percent: string
  median_profit_factor: string
  median_win_rate_percent: string
  median_payoff_ratio: string
  worst_drawdown_percent: string
  cross_market_passed: boolean
  rank_score: string
  reasons: string[]
}

export type ResearchCampaignReport = {
  id: string
  engine_version: number
  status: string
  dataset_ids: string[]
  research_dataset_ids: string[]
  blind_dataset_ids: string[]
  blind_set_id?: string | null
  blind_consumption_id?: string | null
  library_entry_ids: string[]
  trial_count: number
  candidates: ResearchCampaignCandidate[]
  pbo: {
    candidate_count: number
    fold_count: number
    split_count: number
    overfit_splits: number
    total_possible_splits: number
    sampling_mode: string
    standard_error: string
    upper_confidence_bound: string
    probability_of_backtest_overfitting: string
    passed: boolean
    reasons: string[]
  }
  portfolio_allocations: Array<{
    library_entry_id: string
    family: string
    archetype: string
    weight: string
    evidence_score: string
    risk_budget_role: string
  }>
  selected_library_entry_id?: string | null
  autonomous_execution_eligible: boolean
  blind_selected_candidate_passed: boolean
  blind_candidate?: ResearchCampaignCandidate | null
  execution_certificate?: {
    certificate_version: number
    campaign_id: string
    engine_version: number
    selected_library_entry_id: string
    strategy_fingerprint: string
    blind_set_id: string
    blind_consumption_id: string
    fingerprint: string
  } | null
  selection_reasons: string[]
  created_at: string
}

export type PairStrategySpec = {
  version: number
  model: 'log_spread_equal_dollar'
  formation_window: number
  entry_zscore: string
  exit_zscore: string
  stop_zscore: string
  maximum_holding_bars: number
  gross_leverage: string
  source_url: string
  hypothesis: string
}

export type PairBacktestReport = {
  engine_version: number
  id: string
  strategy_spec: PairStrategySpec
  strategy_fingerprint: string
  dataset_a_id: string
  dataset_b_id: string
  data_fingerprint_a: string
  data_fingerprint_b: string
  symbol_a: string
  symbol_b: string
  timeframe: string
  aligned_bars: number
  data_start: string
  data_end: string
  initial_equity: string
  final_equity: string
  return_percent: string
  max_drawdown_percent: string
  total_trades: number
  win_rate_percent: string
  payoff_ratio: string
  profit_factor: string
  total_fees: string
  total_slippage: string
  total_funding: string
  funding_coverage_percent_a: string
  funding_coverage_percent_b: string
  terminal_position_closed: boolean
  statistical_validation: {
    trial_count: number
    observation_count: number
    one_sided_sharpe_p_value: string
    deflated_sharpe_probability: string
    win_rate_lower_bound_percent: string
    bootstrap_payoff_lower_bound: string
    monte_carlo_drawdown_95_percent: string
    false_discovery_rate_passed: boolean
    uncertainty_passed: boolean
    reasons: string[]
  }
  research_gate_passed: boolean
  robustness_passed: boolean
  execution_eligible: false
  validation_reasons: string[]
  integrity_checks: Record<string, boolean | number | string>
  created_at: string
}

export type ResearchCampaignJob = {
  id: string
  request: {
    dataset_ids: string[]
    blind_dataset_ids: string[]
    library_entry_ids: string[]
    candidate_ids: string[]
    max_backtests: number
  }
  request_fingerprint: string
  status: 'queued' | 'running' | 'completed' | 'failed'
  total_cells: number
  completed_cells: number
  cache_hits: number
  computed_cells: number
  failed_cells: number
  report_id?: string | null
  error?: string | null
  created_at: string
  started_at?: string | null
  completed_at?: string | null
  updated_at: string
}

export type ProductAcceptanceCheck = {
  code: string
  label: string
  weight: number
  passed: boolean
  evidence: string
}

export type ProductAcceptanceDimension = {
  code: string
  label: string
  score: number
  hard_gate_passed: boolean
  checks: ProductAcceptanceCheck[]
}

export type ProductAcceptanceReport = {
  schema_version: 1
  scope: string
  generated_at: string
  overall_score: string
  engineering_score: string
  capability_score: string
  strategy_certified: boolean
  strategy_certification_status: 'certified' | 'hold'
  profitability_evidence_score: number
  profitability_guaranteed: false
  dimensions: ProductAcceptanceDimension[]
  blockers: string[]
  evidence?: {
    strict_data_coverage?: {
      score: string
      passed: boolean
      feature_scores: Record<string, string>
      blockers: string[]
    }
  }
}

export type StrategyCandidate = {
  id: string
  spec: StrategySpec
  spec_fingerprint: string
  source: 'system' | 'operator' | 'ai' | 'ml'
  status: 'proposed' | 'validated' | 'challenger' | 'champion' | 'rejected' | 'retired'
  dataset_id?: string
  backtest?: BacktestResult
  decision_reasons: string[]
}

export type PromotionDecision = {
  approved: boolean
  checks: Record<string, boolean>
  reasons: string[]
}

export type MLChallengerReport = {
  training_samples: number
  validation_samples: number
  training_accuracy: string
  validation_accuracy: string
  validation_brier_score: string
  accepted: boolean
  rejection_reasons: string[]
  candidate_spec?: StrategySpec
}

export type DriftReport = {
  baseline_samples: number
  recent_samples: number
  population_stability_index: string
  volatility_ratio: string
  mean_return_shift: string
  status: 'stable' | 'warning' | 'critical'
  reasons: string[]
}

export type DashboardData = {
  market: {
    symbol: string
    timestamp: string
    bid: string
    ask: string
    last: string
    source: string
    spread_bps: string
    funding_rate?: string | null
    funding_time?: string | null
    funding_interval?: string | null
  }
  candles: Candle[]
  signal: StrategySignal
  portfolio: {
    equity: string
    cash: string
    daily_start_equity: string
    gross_exposure: string
    available_margin?: string
    used_margin: string
    margin_ratio: string
    daily_drawdown_fraction: string
    open_positions: Array<{
      symbol: string
      side: 'long' | 'short'
      quantity: string
      average_entry: string
      mark_price: string
      unrealized_pnl: string
      stop_loss?: string
      take_profit?: string
      leverage?: string
      liquidation_price?: string
      margin_mode?: string
      margin_used?: string
    }>
  }
  plan: Plan | null
  risk: { approved: boolean; checks: RiskCheck[] }
  orders: Array<{
    id: string
    client_order_id: string
    plan_id: string
    symbol: string
    side: string
    order_type: string
    status: string
    quantity: string
    filled_quantity: string
    limit_price?: string
    average_fill_price?: string
    fee: string
    mode: string
    market_type: MarketType
    margin_mode: 'cross' | 'isolated'
    contract_leverage: string
    created_at: string
    updated_at: string
  }>
  audit: Array<{
    id: string
    timestamp: string
    category: string
    severity: string
    message: string
  }>
  backtest: BacktestResult
  system: {
    mode: string
    live_locked: boolean
    data_latency_ms: number
    strategy_workers: number
    market_data_real: boolean
    exchange: string
    market_quality?: {
      status: string
      source: string
      is_real: boolean
      is_cached: boolean
      candle_count: number
      gap_count: number
      duplicate_count: number
      invalid_count: number
      last_candle_at: string
      last_tick_at: string
      message: string
    }
    risk_state: {
      halted: boolean
      reason: string
      source: string
      halted_at?: string
      session_date: string
    }
    reconciliation?: {
      confirmed: number
      uncertain_order_ids: string[]
      unknown_open_order_ids: string[]
      unknown_position_symbols: string[]
      unprotected_order_ids: string[]
      checked_at: string
    }
    reconciliation_health?: {
      healthy: boolean
      entry_allowed: boolean
      consecutive_failures: number
      failure_threshold: number
      last_success_at?: string
      last_failure_at?: string
      last_error: string
    }
    private_order_stream?: {
      connected: boolean
      authenticated: boolean
      reconnect_count: number
      last_event_at?: string
      last_error: string
    }
  }
}

export type MarketStreamEvent = {
  type: 'market'
  market: DashboardData['market']
  candles: Record<string, Candle>
  server_time: string
}

export type MarketWindow = {
  market: DashboardData['market']
  candles: Candle[]
  timeframe: string
  quality?: DashboardData['system']['market_quality']
}

export type TradingMode = 'paper' | 'sandbox' | 'live'
export type MarketType = 'spot' | 'swap'
export type StrategyKind = 'trend' | 'mean_reversion' | 'breakout'
export type AIProvider = 'openai' | 'anthropic' | 'google' | 'xai' | 'deepseek' | 'mistral' | 'qwen' | 'custom'

export type AIProviderConfiguration = {
  provider: AIProvider
  enabled: boolean
  model: string
  base_url: string
  priority: number
  timeout_seconds: number
  allow_preview_models: boolean
  thinking_enabled: boolean
  reasoning_effort: 'high' | 'max'
}

export type AIUsage = {
  input_tokens: number
  output_tokens: number
  total_tokens: number
  cache_hit_input_tokens: number
  cache_miss_input_tokens: number
  reasoning_tokens: number
  visible_output_tokens: number
  estimated_cost_cny?: string | null
  pricing_tier?: string | null
  cache_hit_rate: number
}

export type AIUsageReport = {
  summary: {
    since: string
    until: string
    request_count: number
    input_tokens: number
    output_tokens: number
    total_tokens: number
    cache_hit_input_tokens: number
    cache_miss_input_tokens: number
    reasoning_tokens: number
    visible_output_tokens: number
    estimated_cost_cny: string
    unpriced_request_count: number
    cache_hit_rate: number
  }
  recent: Array<{
    id: string
    provider_request_id?: string | null
    provider: AIProvider
    model: string
    operation: string
    thinking_enabled?: boolean | null
    reasoning_effort?: 'high' | 'max' | null
    prompt_version: string
    schema_version: string
    usage: AIUsage
    latency_ms: number
    created_at: string
  }>
}

export type AIConfiguration = {
  routing_mode: 'manual' | 'failover' | 'consensus'
  primary_provider: AIProvider
  daily_budget_usd: string
  providers: AIProviderConfiguration[]
}

export type AIConfigurationView = {
  configuration: AIConfiguration
  credentials: Record<AIProvider, { api_key_configured: boolean }>
  runtime: Record<AIProvider, {
    status: 'untested' | 'ready' | 'error'
    latency_ms?: number | null
    tested_at?: string | null
    model_count: number
    models: string[]
    selected_model_available?: boolean | null
    failure_code?: string | null
    message: string
  }>
  updated_at?: string | null
}

export type AIProviderTestResult = {
  ok: boolean
  provider: AIProvider
  model: string
  latency_ms: number
  model_count: number
  models: string[]
  selected_model_available?: boolean | null
  failure_code?: string | null
  message: string
  tested_at: string
}

export type AIProposalResponse = {
  mode: 'manual' | 'failover' | 'consensus'
  proposals: Array<{
    provider: AIProvider
    model: string
    candidate: StrategyCandidate
    usage: AIUsage
    latency_ms: number
    route: AIProvider[]
  }>
}

export type AIConfigurationUpdate = {
  configuration: AIConfiguration
  credential?: {
    provider: AIProvider
    api_key?: string
    clear_existing?: boolean
  }
}

export type UserConfiguration = {
  exchange_id: 'okx' | 'binance'
  trading_mode: TradingMode
  market_type: MarketType
  margin_mode: 'cross' | 'isolated'
  contract_leverage: string
  default_symbol: string
  default_timeframe: string
  network_proxy_url: string
  paper_equity: string
  fee_bps: string
  slippage_bps: string
  live_trading_enabled: boolean
  automatic_execution_enabled: boolean
  strategy_poll_seconds: number
  risk: {
    risk_per_trade: string
    max_daily_drawdown: string
    max_gross_exposure: string
    max_portfolio_risk: string
    existing_position_stress: string
    max_directional_exposure: string
    max_leverage: string
    max_symbol_exposure: string
    max_open_positions: number
    minimum_reward_risk: string
    max_spread_bps: string
    max_abs_funding_rate: string
  }
  agent_forward_validation: {
    minimum_closed_episodes: number
    minimum_independent_runs: number
    minimum_recent_episodes: number
    minimum_profit_factor: string
    minimum_expectancy_r: string
    maximum_drawdown_r: string
    maximum_consecutive_losses: number
  }
  strategy: {
    default_kind: StrategyKind
    automatic_regime_selection: boolean
    default_prompt: string
    watchlist: string[]
    timeframes: string[]
    research_bars: number
    validation: {
      minimum_trades: number
      minimum_trade_evidence_ratio: string
      minimum_recent_trades: number
      minimum_sample_bars: number
      minimum_profit_factor: string
      minimum_win_rate_percent: string
      minimum_payoff_ratio: string
      maximum_drawdown_percent: string
      stress_fee_multiplier: string
      stress_slippage_multiplier: string
      require_higher_timeframe_confirmation: boolean
    }
  }
}

export type ConfigurationView = {
  configuration: UserConfiguration
  credentials: {
    api_key_configured: boolean
    secret_configured: boolean
    passphrase_configured: boolean
  }
  credential_scopes: Record<string, {
    api_key_configured: boolean
    secret_configured: boolean
    passphrase_configured: boolean
  }>
  commissioning?: {
    exchange: string
    market_type: MarketType
    symbol: string
    protection_verified: boolean
    cancellation_confirmed: boolean
    agent_reduction_verified: boolean
    agent_exit_verified: boolean
    position_flat_confirmed: boolean
    order_ids: string[]
    message: string
    tested_at: string
  }
  execution_ready: boolean
  promoted_agent_goal_id?: string | null
  promoted_agent_policy_id?: string | null
  updated_at?: string
}

export type CredentialDraft = {
  api_key?: string
  secret?: string
  passphrase?: string
  clear_existing?: boolean
}

export type ConfigurationUpdate = {
  configuration: UserConfiguration
  credentials?: CredentialDraft
  live_unlock_phrase?: string
  promote_agent_goal_id?: string
}

export type ConnectionTestResult = {
  ok: boolean
  exchange: string
  mode: TradingMode
  public_market_ok: boolean
  private_account_ok?: boolean | null
  protected_order_ok: boolean
  latency_ms: number
  symbol: string
  last_price?: string
  candle_count: number
  failure_code?: string
  required_action?: string
  message: string
  tested_at: string
}

export type StrategyGenerateRequest = {
  symbol: string
  timeframe: string
  equity?: string
  risk_fraction: string
  prompt: string
  strategy_kind: StrategyKind
  strategy_spec?: StrategySpec
  revision_of?: string
}
