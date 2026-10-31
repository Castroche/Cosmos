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
  evidence_tier: string
  executable_environments: string[]
  evidence_refs: string[]
  invalidation_reason: string
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

export type ReviewJobView = {
  id: string
  candidate_id?: string | null
  candidate_name?: string | null
  candidate_fingerprint?: string | null
  review_contract_version: number
  policy_fingerprint: string
  origin_scope: Record<string, string>
  scope_results: ReviewScopeResult[]
  status: string
  stage: string
  evidence_tier: string
  progress_percent: number
  report_id?: string | null
  candidate_count: number
  selected_strategy_id?: string | null
  rejection_reasons: string[]
  evidence_refs: string[]
  metrics: Record<string, string | number | boolean | null>
  dataset_id?: string | null
  attempt_count: number
  error?: string | null
  updated_at: string
}

export type AIHealth = {
  generated_at: string
  providers: Record<string, {
    status: 'ready' | 'degraded' | 'circuit_open'
    consecutive_failures: number
    opened_at?: string | null
    cooldown_until?: string | null
    last_failure_at?: string | null
    last_success_at?: string | null
    last_error_code?: string | null
    last_error_message: string
    half_open_attempts: number
    half_open_in_flight?: boolean
    deterministic_fallback_available: boolean
    automatic_agent_decisions_allowed: boolean
    agent_decision_quality: {
      operation: string
      window_days: number
      sample_size: number
      request_count: number
      succeeded_request_count: number
      failed_request_count: number
      invalid_response_count: number
      success_rate?: number | null
      minimum_success_rate: number
      median_latency_ms?: number | null
      p95_latency_ms?: number | null
      latest_succeeded?: boolean | null
      latest_at?: string | null
      cooldown_until?: string | null
      automatic_allowed: boolean
      status: 'warming_up' | 'healthy' | 'probe_allowed' | 'quality_blocked'
      reason: string
    }
  }>
  research_fallback: 'deterministic'
  review_llm_calls: 0
  execution_llm_calls: 0
  pipeline_continues_when_ai_unavailable: boolean
  role_isolation: {
    research: 'optional_ai_generation_with_quality_gate'
    review: 'deterministic_only'
    trading: 'deterministic_only'
  }
}

export type ReviewOverview = {
  generated_at: string
  queued: number
  unreviewed: number
  running: number
  blocked: number
  paper_eligible: number
  sandbox_canary: number
  sandbox_mature: number
  live_capped: number
  rejected: number
  legacy: number
  data_blocked: number
  jobs: ReviewJobView[]
  pipeline: Record<string, string | number | boolean>
}

export type ReviewEvidenceItem = {
  id: string
  kind: 'strategy' | 'campaign'
  name: string
  status: string
  evidence_tier: string
  source: string
  family: string
  summary: string
  reasons: string[]
  metrics: Record<string, string | number | boolean | null>
  evidence_refs: string[]
  technical_details: string
  review_contract_version: number
  origin_scope: Record<string, string>
  scope_results: ReviewScopeResult[]
  updated_at: string
}

export type ReviewScopeResult = {
  symbol: string
  timeframe: string
  role: string
  status: string
  stage: string
  dataset_id?: string | null
  dataset_fingerprint?: string | null
  metrics: Record<string, string | number | boolean | null>
  reasons: string[]
  evidence_refs: string[]
}

export type Crypto5mDataContract = {
  id: 'crypto_5m_v1'
  status: 'READY' | 'NOT_READY' | 'UNTESTABLE'
  fingerprint: string
  minimum_days: 730
  minimum_completeness: string
  horizons: [3, 6, 12]
  markets: Array<{
    market: string
    status: 'READY' | 'NOT_READY' | 'UNTESTABLE'
    days: string
    completeness: string
    required_feature_coverage: string
    reasons: string[]
  }>
}

export type SandboxAdmissionView = {
  certificate: {
    id: string
    strategy_ir_hash: string
    artifact_fingerprint: string
    model_artifact_ids: string[]
    development_audit_certificate_id: string
    maximum_risk_per_trade: string
    issued_at: string
  }
  revoked: boolean
  handoff?: {
    status: string
    exchange: 'okx'
    maximum_risk_per_trade: string
  } | null
  progress: {
    maturity: {
      completed_episodes: number
      independent_runs: number
      recent_episodes: number
      profit_factor: string
      expected_r: string
      maximum_drawdown_r: string
      maximum_consecutive_losses: number
      execution_cost_aligned: boolean
      slippage_aligned: boolean
      native_protection_aligned: boolean
    }
    execution_evidence_count: number
    aligned_episode_count: number
  }
}

export type DevelopmentAuditView = {
  certificate: {
    id: string
    strategy_ir_hash: string
    artifact_fingerprint: string
    campaign_id: string
    market_evaluation_fingerprints: Record<string, string>
    evidence: {
      expected_r_bootstrap_lower: string
      profit_factor: string
      stress_net_return: string
      maximum_drawdown_fraction: string
      pbo_95_upper: string
      online_fdr_passed: boolean
      passing_markets: string[]
    }
    issued_at: string
  }
  revoked: boolean
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
  research_context?: {
    generation_contract_version: string
    exchange: string
    market_type: string
    symbol: string
    timeframe: string
    entry_profile: string
    market_as_of?: string | null
    summary_fingerprint: string
    fee_bps: string
    slippage_bps: string
    risk_fraction: string
    max_gross_exposure: string
    max_leverage: string
    required_features: string[]
    trial_group: string
    trial_count: number
  } | null
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
  system: {
    mode: string
    data_latency_ms: number
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
    failed_request_count: number
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
    succeeded: boolean
    error_code?: string | null
    error_detail?: string | null
    finish_reason?: string | null
    response_char_count: number
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
  mode: 'manual' | 'failover' | 'consensus' | 'single_efficient'
  proposals: Array<{
    provider: AIProvider
    model: string
    candidate: StrategyCandidate
    usage: AIUsage
    latency_ms: number
    route: AIProvider[]
    preflight: Record<string, string | number | boolean | string[] | null>
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
      maximum_estimated_round_trip_cost_r: string
      cross_market_minimum_passes: number
      review_contract_version: number
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

export type FeatureHealth = 'healthy' | 'degraded' | 'unavailable'

export type TradingIntelligenceFeatureGroup = {
  health: FeatureHealth
  values: Record<string, string | number | boolean | null>
  reasons: string[]
  observed_at?: string | null
}

export type MarketStateV2 = {
  version: 2
  state_id: string
  state_hash: string
  exchange: 'okx'
  symbol: 'BTC/USDT:USDT' | 'ETH/USDT:USDT' | 'SOL/USDT:USDT'
  data_as_of: string
  created_at: string
  regime: 'TREND_UP' | 'TREND_DOWN' | 'STRONG_TREND_UP' | 'STRONG_TREND_DOWN' | 'RANGE' | 'COMPRESSION' | 'VOLATILITY_EXPANSION' | 'PANIC' | 'REVERSAL' | 'TRANSITION'
  healthy_for_new_risk: boolean
  market: TradingIntelligenceFeatureGroup
  structure: TradingIntelligenceFeatureGroup
  trend: TradingIntelligenceFeatureGroup
  momentum: TradingIntelligenceFeatureGroup
  volatility: TradingIntelligenceFeatureGroup
  volume: TradingIntelligenceFeatureGroup
  microstructure: TradingIntelligenceFeatureGroup
  derivatives: TradingIntelligenceFeatureGroup
  cross_market: TradingIntelligenceFeatureGroup
  events: TradingIntelligenceFeatureGroup
  timeframe_features: Record<string, Record<string, string | number | boolean | null>>
}

export type ActiveThesisV2 = {
  thesis_id: string
  version: number
  symbol: MarketStateV2['symbol']
  direction: 'LONG' | 'SHORT' | 'NEUTRAL'
  statement: string
  invalidation_rules: string[]
  state: 'CREATED' | 'ACTIVE' | 'COMPLETED' | 'INVALIDATED' | 'EXPIRED' | 'CANCELLED'
  created_at: string
  expires_at: string
}

export type WakeConditionV2 = {
  condition_id: string
  kind: string
  action: 'RECALL_LLM' | 'EXECUTE_ENTRY' | 'CANCEL_PLAN' | 'EXIT_POSITION'
  path?: string | null
  value?: string | number | boolean | null
  lower?: string | null
  upper?: string | null
  timeframe?: string | null
  children: WakeConditionV2[]
  expires_at?: string | null
}

export type TradePlanV2 = {
  plan_id: string
  thesis_id: string
  version: number
  symbol: MarketStateV2['symbol']
  side: 'LONG' | 'SHORT'
  order_type?: 'MARKET' | 'LIMIT' | null
  limit_price?: string | null
  reference_price?: string | null
  valid_price_range?: { lower: string; upper: string } | null
  position_id?: string | null
  position_version?: number | null
  entry_zone: { lower: string; upper: string }
  entry_conditions: WakeConditionV2[]
  management: {
    hard_stop: string
    take_profits: Array<{ price: string; close_fraction: string; rationale: string }>
    break_even_after_target?: number | null
    atr_trailing_multiple?: string | null
    maximum_holding_minutes?: number | null
  }
  minimum_reward_risk: string
  rationale: string[]
  state: 'CREATED' | 'VALIDATED' | 'ACTIVE' | 'WAITING_TRIGGER' | 'ARMED' | 'TRIGGERED' | 'COMPLETED' | 'EXPIRED' | 'INVALIDATED' | 'CANCELLED'
  created_at: string
  expires_at: string
}

export type QuantPredictionV1 = {
  prediction_id: string
  model_fingerprint: string
  symbol: MarketStateV2['symbol']
  timeframe: '5m'
  data_as_of: string
  expires_at: string
  probability_long_tp_first: string
  probability_short_tp_first: string
  probability_timeout: string
  expected_return_after_cost: string
  expected_mfe: string
  expected_mae: string
  calibrated: boolean
}

export type QuantModelStatusV1 = {
  symbol: MarketStateV2['symbol']
  timeframe: '5m'
  health: 'healthy' | 'degraded' | 'unavailable'
  entry_eligible?: boolean
  reason?: string | null
  model_fingerprint?: string | null
  data_as_of?: string | null
}

export type DecisionEpisodeV2 = {
  episode_id: string
  symbol: MarketStateV2['symbol']
  status: string
  market_state_hash: string
  analysis_id?: string | null
  quant_prediction_id?: string | null
  decision_id?: string | null
  thesis_id?: string | null
  plan_id?: string | null
  trigger_event_id?: string | null
  risk_allocation_id?: string | null
  order_ids: string[]
  position_id?: string | null
  outcome?: Record<string, unknown> | null
  prompt_versions: Record<string, string>
  model_versions: Record<string, string>
  token_usage: Record<string, number>
  latency_ms: Record<string, number>
  created_at: string
  updated_at: string
}

export type WaitRuntimeV2 = {
  wait_runtime_id: string
  version: number
  episode_id: string
  symbol: MarketStateV2['symbol']
  status: 'ACTIVE' | 'FIRED' | 'EXPIRED' | 'CANCELLED'
  wait_plan: {
    thesis_id?: string | null
    reason: string
    wake_conditions: WakeConditionV2[]
    expires_at: string
  }
  created_at: string
  updated_at: string
}

export type PositionRuntimeV2 = {
  position_id: string
  plan_id: string
  plan_ids?: string[]
  episode_ids?: string[]
  entry_filled_quantities?: Record<string, string>
  management_quantity?: string | null
  management_started_at?: string | null
  management_version?: number
  version?: number
  episode_id?: string | null
  symbol: MarketStateV2['symbol']
  side: 'LONG' | 'SHORT'
  state: 'FLAT' | 'OPENING' | 'PARTIALLY_FILLED' | 'OPEN' | 'PARTIAL_EXIT' | 'CLOSING' | 'CLOSED'
  original_quantity: string
  remaining_quantity: string
  average_entry: string
  initial_stop: string
  current_stop: string
  targets_reached: number
  entry_order_ids: string[]
  exit_order_ids: string[]
  opened_at: string
  updated_at: string
}

export type EntryOrderRuntime = {
  entry_id: string
  plan_id: string
  episode_id: string
  mode?: string
  version?: number
  plan_version?: number
  symbol: MarketStateV2['symbol']
  side: 'LONG' | 'SHORT'
  order_type: 'MARKET' | 'LIMIT'
  requested_price?: string | null
  submitted_price?: string | null
  reference_price?: string | null
  valid_price_range?: { lower: string; upper: string } | null
  expires_at: string
  order_ids: string[]
  quantity: string
  filled_quantity: string
  average_fill_price?: string | null
  status: 'SUBMITTING' | 'OPEN' | 'PARTIALLY_FILLED' | 'FILLED' | 'CANCEL_PENDING' | 'CANCELLED' | 'REJECTED' | 'UNCERTAIN' | 'SHADOW_RECORDED'
  reason?: string | null
  created_at: string
  updated_at: string
  position_id?: string | null
}

export type AutomationRuntimeV2 = {
  runtime_id: 'trading-intelligence-v2'
  enabled: boolean
  requested_mode: 'SHADOW' | 'OKX_DEMO'
  symbols: MarketStateV2['symbol'][]
  status: 'STOPPED' | 'STARTING' | 'RUNNING' | 'DEGRADED' | 'HALTED'
  consecutive_failures: number
  last_cycle_at?: string | null
  last_decision_at?: string | null
  last_execution_at?: string | null
  last_reconciliation_at?: string | null
  last_error_code?: string | null
  next_retry_at?: string | null
  updated_at: string
}

export type HumanLiveGrantV2 = {
  grant_id: string
  exchange: 'okx'
  symbols: MarketStateV2['symbol'][]
  max_risk_per_trade: string
  max_portfolio_risk: string
  max_leverage: string
  max_daily_drawdown: string
  max_open_positions: number
  configuration_fingerprint: string
  issued_at: string
  expires_at: string
  revoked_at?: string | null
}

export type TradingIntelligenceOverview = {
  version: 2
  runtime_modes: Array<'SHADOW' | 'OKX_DEMO' | 'LIVE_AUTHORIZED'>
  symbols: MarketStateV2['symbol'][]
  market_states: MarketStateV2[]
  active_theses: ActiveThesisV2[]
  active_plans: TradePlanV2[]
  active_waits: WaitRuntimeV2[]
  // Optional while older servers do not yet expose entry runtimes.
  entry_orders?: EntryOrderRuntime[]
  positions: PositionRuntimeV2[]
  quant_predictions: QuantPredictionV1[]
  quant_models: QuantModelStatusV1[]
  episodes: DecisionEpisodeV2[]
  automation: AutomationRuntimeV2
  live_grant?: HumanLiveGrantV2 | null
  llm_health: Record<string, { status: string; last_error_message?: string }>
  portfolio_policy?: {
    risk_per_trade: string
    max_portfolio_risk: string
    max_open_positions: number
    max_leverage: string
    max_daily_drawdown: string
  }
  learning?: TradingLearningSnapshot
}

export type TradingLearningSnapshot = {
  version: string
  policy: string
  sample_count: number
  skipped_count: number
  updated_at: string
  groups: Array<{
    mode: string; symbol: string; setup: string; regime: string; side: string
    sample_count: number; wins: number; posterior_win_rate: string
    expectancy_r: string; learned_expectancy_r: string; average_mfe_r: string; lessons: string[]
  }>
  recent: Array<{
    episode_id: string; symbol: string; mode: string; side: string
    return_on_risk: string; realized_pnl: string; fees: string
  }>
}

export type TradeHistoryV2 = {
  generated_at: string
  symbol?: MarketStateV2['symbol'] | null
  days: number
  summary: {
    trade_count: number
    winning_trades: number
    losing_trades: number
    win_rate?: string | null
    net_pnl: string
    gross_profit: string
    gross_loss: string
    profit_factor?: string | null
    average_pnl: string
    total_fees: string
    maximum_drawdown: string
    average_return_on_risk: string
  }
  equity_curve: Array<{
    closed_at: string
    cumulative_pnl: string
    position_id: string
  }>
  items: Array<{
    position_id: string
    episode_id: string
    plan_id: string
    symbol: MarketStateV2['symbol']
    side: 'LONG' | 'SHORT'
    opened_at: string
    closed_at: string
    duration_minutes: number
    quantity: string
    entry_price: string
    exit_price?: string | null
    stop_price: string
    realized_pnl: string
    gross_pnl: string
    fees: string
    return_after_cost: string
    return_on_risk: string
    mfe: string
    mae: string
    targets_reached: number
    exit_reason?: string | null
    entry_order_ids: string[]
    exit_order_ids: string[]
  }>
}

export type HumanLiveGrantPreviewV2 = {
  eligible: boolean
  blockers: string[]
  configuration_fingerprint: string
  exchange: 'okx'
  symbols: MarketStateV2['symbol'][]
  expires_at: string
  requires_two_confirmations: true
}
export type LearningSettings = { enabled: boolean; pinned_version: string | null; shadow_enabled: boolean }
export type LearningVersion = { version: string; sample_count: number; updated_at: string; algorithm: string }
export type LearningCase = { episode_id: string; campaign_id: string; symbol: string; mode: string; side: string; regime: string; available_at: string; realized_pnl: string }
export type LearningEvidence = TradingLearningSnapshot & { records?: LearningCase[]; excluded?: Array<{episode_id: string; reason: string}> }
export type LearningSummary = { completed: number; daily_used: number; daily_limit: number; sample_rate: number; evidence_status: string; format_valid_rate: number | null; cost: number | null; counts: Record<string, number> }
export type LearningDecision = { action: string; trade_plan?: { limit_price?: string; reference_price?: string; management?: { hard_stop: string } } | null }
export type LearningProxy = { status: string; direction_return?: number; mfe?: number; mae?: number }
export type LearningComparison = { decision_id: string; status: string; created_at: string; payload: { symbol: string; mode: string; primary: LearningDecision; control?: LearningDecision | null; case_ids: string[]; learning_version?: string; skip_reason?: string | null; detail?: string; primary_latency_ms?: number; control_latency_ms?: number; primary_usage?: { total_tokens: number }; control_usage?: Array<{total_tokens: number}>; proxy: {status: string; primary?: LearningProxy; control?: LearningProxy} } }
export type LearningPage<T> = { items: T[]; total: number; limit: number; offset: number }
