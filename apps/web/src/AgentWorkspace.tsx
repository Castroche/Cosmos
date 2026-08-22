import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Activity, Bot, BrainCircuit, CheckCircle2, CirclePause, Play, RefreshCw, ShieldCheck, XCircle } from 'lucide-react'
import { backtestStrategyByPlanId, createAgentGoal, getAgentForwardPerformance, getAgentReadiness, getAgentRunTrace, getAgentState, getLatestBacktestByPlanId, runAgentOnce, saveAgentPolicy, setAgentGoalStatus, triggerAgentGoal, updateAgentGoalScope } from './api'
import BacktestEvidence from './BacktestEvidence'
import type { AgentAutonomyMode, AgentControlState, AgentForwardPerformance, AgentMarketCoverage, AgentPolicy, AgentReadiness, AgentRunTrace, BacktestJob, BacktestResult, ConfigurationView, ShadowCalibrationSummary, ShadowValidationTrial } from './types'

function localTime(value?: string) {
  return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'
}

function compactNumber(value?: string | null, maximumFractionDigits = 6) {
  if (value === undefined || value === null || value === '') return '—'
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return value
  return parsed.toLocaleString('zh-CN', { maximumFractionDigits })
}

function AgentMarketCoverageEvidence({ items }: { items: AgentMarketCoverage[] }) {
  const ordered = [...items].sort((left, right) => (
    left.symbol.localeCompare(right.symbol)
    || left.timeframe.localeCompare(right.timeframe)
  ))
  const healthy = ordered.filter(item => item.coverage_status === 'healthy').length
  return <section className={`agent-market-coverage ${healthy === ordered.length && ordered.length ? 'healthy' : 'attention'}`}>
    <header>
      <div><span>AUTONOMOUS MARKET COVERAGE</span><strong>三品种独立感知与调度</strong></div>
      <small>{healthy}/{ordered.length} 市场周期正常</small>
    </header>
    {ordered.length ? <div className="agent-market-coverage-grid">{ordered.map(item => <article key={`${item.goal_id}:${item.symbol}:${item.timeframe}`} className={item.coverage_status}>
      <div><i /><strong>{item.symbol}</strong><b>{item.timeframe}</b><span>{item.coverage_status.toUpperCase()}</span></div>
      <dl>
        <div><dt>最近回合</dt><dd>{item.last_run_status?.toUpperCase() ?? '—'} · {item.last_run_steps} steps</dd></div>
        <div><dt>本周期尝试</dt><dd>{item.attempt_count}{item.retry_recovered ? ' · 已安全恢复' : ''}</dd></div>
        <div><dt>下次收盘调度</dt><dd>{localTime(item.next_scheduled_at)}</dd></div>
      </dl>
      {item.last_run_error && <p>{item.last_run_error}</p>}
    </article>)}</div> : <p className="agent-empty">该目标尚无自动调度记录。</p>}
  </section>
}

function modeForPlatform(mode: ConfigurationView['configuration']['trading_mode']): AgentAutonomyMode {
  return mode === 'live' ? 'live_capped' : mode
}

function evidencePayload(data: Record<string, unknown>) {
  const bounded = data.recommendation ?? data.assessment ?? data
  const serialized = JSON.stringify(bounded, null, 2)
  return serialized.length > 900 ? `${serialized.slice(0, 900)}\n…` : serialized
}

function recordValue(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

type AgentBacktestSelection = {
  planId: string
  strategyKind: string
  side: string
  rank: string
  symbol: string
  timeframe: string
}

function CandidateScreenEvidence({
  data,
  onOpenBacktest,
}: {
  data: Record<string, unknown>
  onOpenBacktest: (selection: AgentBacktestSelection) => void
}) {
  if (!('selection_status' in data)) return null
  const candidates = Array.isArray(data.candidates)
    ? data.candidates.map(recordValue)
    : []
  const failures = Array.isArray(data.generation_failures)
    ? data.generation_failures.map(recordValue)
    : []
  const waitCategory = String(data.wait_category ?? '')
  const verdict = data.selection_status === 'robust_candidate'
    ? '发现稳健候选'
    : data.selection_status === 'sandbox_probe_candidate'
      ? '发现低风险测试网候选'
    : data.selection_status === 'evidence_unavailable'
      ? '外部证据不可用'
      : waitCategory === 'no_actionable_signal'
        ? '当前没有合法入场事件'
        : waitCategory === 'backtest_evidence_insufficient'
          ? '策略证据未通过'
          : waitCategory === 'execution_context_wait'
            ? '执行上下文暂缓'
            : waitCategory === 'market_data_unavailable'
              ? '行情证据不可用'
              : '保持 WAIT'
  return <div className="agent-candidate-screen">
    <header>
      <div>
        <span>MULTI-STRATEGY SCREEN</span>
        <strong className={['robust_candidate', 'sandbox_probe_candidate'].includes(String(data.selection_status)) ? 'passed' : 'held'}>
          {verdict}
        </strong>
      </div>
      <small>{String(data.symbol ?? '—')} · {String(data.timeframe ?? '—')} · 阻塞层 {String(data.blocking_layer ?? '无')} · {candidates.length} 个回测 · {failures.length} 个失败</small>
    </header>
    <p>{String(data.reason ?? '')}</p>
    {candidates.length > 0 && <div className="agent-candidate-grid">{candidates.map(candidate => {
      const backtest = recordValue(candidate.backtest)
      const recent = recordValue(backtest.recent_window)
      const evidenceScore = recordValue(backtest.evidence_score)
      const evidenceComponents = recordValue(evidenceScore.components)
      const selectionScore = recordValue(candidate.selection_score)
      const forwardOutcome = recordValue(candidate.forward_outcome_evidence)
      const adaptation = recordValue(candidate.adaptation)
      const parameters = recordValue(candidate.strategy_parameters)
      const parameterWalkForward = recordValue(candidate.parameter_walk_forward)
      const evidenceTier = String(candidate.evidence_tier ?? 'insufficient')
      const probeEvidence = recordValue(candidate.sandbox_probe_evidence)
      const riskAdjustment = recordValue(candidate.risk_adjustment)
      const passed = candidate.execution_eligible === true
      const sampleStandard = Number(backtest.trade_sample_standard ?? 0)
      const sampleFloor = Number(backtest.trade_sample_floor ?? 0)
      const sampleCorroborated = backtest.trade_sample_corroborated === true
      return <article key={String(candidate.plan_id)}>
        <div><span>#{String(candidate.rank)}</span><strong>{String(candidate.strategy_kind)} · {String(adaptation.label ?? 'baseline')} · {String(candidate.side)}</strong><i className={passed ? 'passed' : 'failed'}>{passed ? 'PASS' : 'HOLD'}</i></div>
        <p className="agent-adaptation-proof">
          <b>{String(adaptation.selection_basis ?? 'current_market_only')}</b>
          {' · '}回测调参 {adaptation.backtest_tuned === true ? '是' : '否'}
          {' · '}{Object.entries(parameters).slice(0, 6).map(([key, value]) => `${key}=${String(value)}`).join(' · ')}
        </p>
        {'effective_risk_fraction' in riskAdjustment && <p className="agent-sample-proof">
          风险处理：请求 {(Number(riskAdjustment.requested_risk_fraction) * 100).toFixed(3)}%
          {' → '}有效 {(Number(riskAdjustment.effective_risk_fraction) * 100).toFixed(3)}%
          {' · '}{String(riskAdjustment.execution_context_class ?? 'aligned_or_neutral')}
          {riskAdjustment.policy_cap_applied === true ? ' · 已按授权上限安全截断' : ''}
          {' · '}{String(riskAdjustment.reason ?? '')}
        </p>}
        {evidenceTier === 'sandbox_probe' && <p className="agent-sample-proof">
          测试网探测层 · 每笔风险已缩小至策略上限的 {Number(probeEvidence.risk_multiplier ?? 0) * 100}% · 只收集前向成交证据，不能用于实盘解锁
        </p>}
        {'mode' in parameterWalkForward && <p className={`agent-parameter-walk ${parameterWalkForward.passed === true ? 'passed' : 'held'}`}>
          参数前推：过去分段选参 → 下一分段验证
          {' · '}{String(parameterWalkForward.positive_steps ?? 0)}/{String(parameterWalkForward.evaluable_steps ?? 0)} 个验证分段为正
          {' · '}累计 {String(parameterWalkForward.cumulative_validation_return_percent ?? '—')}%
          {' · '}仅作稳定性证据，不是新增硬风控
        </p>}
        <dl>
          <div><dt>收益</dt><dd>{String(backtest.return_percent ?? '—')}%</dd></div>
          <div><dt>压力收益</dt><dd>{String(backtest.stress_return_percent ?? '—')}%</dd></div>
          <div><dt>PF</dt><dd>{String(backtest.profit_factor ?? '—')}</dd></div>
          <div><dt>平均盈亏比</dt><dd>{String(backtest.payoff_ratio ?? '—')}</dd></div>
          <div><dt>回撤</dt><dd>{String(backtest.max_drawdown_percent ?? '—')}%</dd></div>
          <div><dt>近期</dt><dd>{String(recent.return_percent ?? '—')}%</dd></div>
          <div><dt>近期 PF</dt><dd>{String(recent.profit_factor ?? '—')}</dd></div>
          <div><dt>历史胜率</dt><dd>{String(backtest.win_rate_percent ?? '—')}%</dd></div>
          <div><dt>交易样本</dt><dd>{String(backtest.total_trades ?? '—')} / {sampleStandard || '—'}</dd></div>
          <div><dt>样本跨度</dt><dd>{String(backtest.sample_years ?? '—')} 年</dd></div>
          <div><dt>证据质量</dt><dd>{String(evidenceScore.total ?? '—')} / 100</dd></div>
          <div><dt>综合排序</dt><dd>{String(selectionScore.total ?? '—')} / 100</dd></div>
          <div><dt>前向记忆</dt><dd>{String(forwardOutcome.sample ?? 0)} 笔 / {forwardOutcome.expectancy_r == null ? '—' : `${String(forwardOutcome.expectancy_r)}R`}</dd></div>
        </dl>
        {'total' in selectionScore && <p className="agent-score-proof">
          综合排序 = 历史证据 {Number(selectionScore.evidence_weight ?? 0.7) * 100}% + 当前信号 {Number(selectionScore.signal_weight ?? 0.2) * 100}% + 市场状态 {Number(selectionScore.regime_weight ?? 0.1) * 100}% + 已复盘前向结果 {Number(selectionScore.forward_weight ?? 0) * 100}%；不是预测胜率。证据构成：
          样本 {String(evidenceComponents.sample ?? '—')} · 成本压力 {String(evidenceComponents.cost_resilience ?? '—')} ·
          近期有效性 {String(evidenceComponents.recent_edge ?? '—')} · 时间稳定性 {String(evidenceComponents.temporal_stability ?? '—')} ·
          回撤控制 {String(evidenceComponents.drawdown_control ?? '—')}
        </p>}
        {forwardOutcome.mature_for_ranking === true && <p className="agent-sample-proof">
          已复盘记忆参与排序：同品种、同周期、同方向、同策略家族 {String(forwardOutcome.sample)} 笔，累计 {String(forwardOutcome.total_r)}R，期望 {String(forwardOutcome.expectancy_r)}R；仅排序，不绕过稳健性或风险闸
        </p>}
        {sampleCorroborated && <p className="agent-sample-proof">低于标准样本，但已超过最低 {sampleFloor} 笔，并由近期窗口与成本压力共同佐证</p>}
        <footer className="agent-candidate-actions">
          <small>{Array.isArray(backtest.validation_reasons) ? backtest.validation_reasons.join('；') : '无门槛说明'} · {String(candidate.plan_id)}</small>
          <button type="button" onClick={() => onOpenBacktest({
            planId: String(candidate.plan_id),
            strategyKind: String(candidate.strategy_kind ?? 'unknown'),
            side: String(candidate.side ?? '—'),
            rank: String(candidate.rank ?? '—'),
            symbol: String(data.symbol ?? '—'),
            timeframe: String(data.timeframe ?? '—'),
          })}>
            查看完整回测
          </button>
        </footer>
      </article>
    })}</div>}
    {failures.length > 0 && <div className="agent-candidate-failures">{failures.map((failure, index) => <article key={`${String(failure.strategy_kind)}-${index}`}>
      <strong>{String(failure.strategy_kind ?? 'unknown')} · {String(failure.adaptive_label ?? 'unknown')} 未完成</strong>
      <p>{String(failure.error ?? '候选生成或回测失败')}</p>
    </article>)}</div>}
  </div>
}

function ForwardPerformanceEvidence({ report }: { report: AgentForwardPerformance }) {
  const statusLabel = {
    collecting: '收集前向证据',
    blocked: '证据链被阻断',
    failed: '未达到评审门槛',
    qualified: '允许进入实盘评审',
  }[report.status]
  const profitFactor = report.profit_factor ?? (report.wins > 0 && report.losses === 0 ? '∞' : '—')
  return <section className={`agent-forward-evidence ${report.status}`}>
    <header>
      <div><span>SANDBOX FORWARD EVIDENCE</span><strong>{statusLabel}</strong></div>
      <small>{report.checks.filter(check => check.passed).length}/{report.checks.length} checks passed · 不自动解锁实盘</small>
    </header>
    <dl>
      <div><dt>完整回合</dt><dd>{report.attributable_episodes}</dd></div>
      <div><dt>独立执行批次</dt><dd>{report.independent_runs}</dd></div>
      <div><dt>净收益</dt><dd className={Number(report.net_pnl) > 0 ? 'positive' : Number(report.net_pnl) < 0 ? 'negative' : ''}>{report.net_pnl} USDT</dd></div>
      <div><dt>累计 / 期望</dt><dd>{report.total_r}R / {report.expectancy_r}R</dd></div>
      <div><dt>Profit Factor</dt><dd>{profitFactor}</dd></div>
      <div><dt>胜率</dt><dd>{report.win_rate_percent}%</dd></div>
      <div><dt>最大回撤</dt><dd>{report.max_drawdown_r}R</dd></div>
      <div><dt>近期 {report.recent_episodes} 笔</dt><dd>{report.recent_expectancy_r}R / 笔</dd></div>
      <div><dt>观察跨度</dt><dd>{report.observation_days} 天</dd></div>
    </dl>
    {(report.symbol_reports ?? []).length > 0 && <div className="agent-forward-symbols">
      {(report.symbol_reports ?? []).map(symbol => <article key={symbol.symbol} className={symbol.qualified ? 'qualified' : symbol.status}>
        <header><strong>{symbol.symbol}</strong><span>{symbol.qualified ? '独立通过' : symbol.status === 'collecting' ? '积累样本' : symbol.status === 'blocked' ? '证据阻断' : '未通过'}</span></header>
        <dl>
          <div><dt>回合</dt><dd>{symbol.attributable_episodes}</dd></div>
          <div><dt>独立批次</dt><dd>{symbol.independent_runs}</dd></div>
          <div><dt>净收益</dt><dd className={Number(symbol.net_pnl) > 0 ? 'positive' : Number(symbol.net_pnl) < 0 ? 'negative' : ''}>{symbol.net_pnl}</dd></div>
          <div><dt>期望</dt><dd>{symbol.expectancy_r}R</dd></div>
          <div><dt>近期</dt><dd>{symbol.recent_expectancy_r}R</dd></div>
          <div><dt>PF</dt><dd>{symbol.profit_factor ?? (symbol.wins > 0 && symbol.losses === 0 ? '∞' : '—')}</dd></div>
          <div><dt>回撤</dt><dd>{symbol.max_drawdown_r}R</dd></div>
        </dl>
        <small>{symbol.checks.filter(check => check.passed).length}/{symbol.checks.length} checks · {symbol.observation_days} 天</small>
      </article>)}
    </div>}
    <div className="agent-forward-checks">{report.checks.map(check => <div key={check.code} className={check.passed ? 'passed' : 'failed'}>
      {check.passed ? <CheckCircle2 /> : <XCircle />}
      <span><strong>{check.code.replaceAll('_', ' ')}</strong><small>{check.message}</small></span>
      <b>{check.current}<small>{check.requirement}</small></b>
    </div>)}</div>
    <footer>该报告只统计由 Agent 完整归因并复盘的测试网回合；账户其他交易和未平仓浮盈不会被计入。</footer>
  </section>
}

function ShadowCalibrationEvidence({
  summary,
  trials,
}: {
  summary: ShadowCalibrationSummary
  trials: ShadowValidationTrial[]
}) {
  const recent = trials
    .filter(trial => trial.outcome.return_on_risk != null)
    .slice(0, 6)
  return <section className={`agent-shadow-evidence ${summary.calibration_ready ? 'ready' : 'collecting'}`}>
    <header>
      <div><span>CAUSAL SHADOW VALIDATION</span><strong>{summary.calibration_ready ? '可用于门槛校准' : '影子前向样本收集中'}</strong></div>
      <small>{summary.settled_batches}/{summary.minimum_settled_batches} 个已结算决策批次 · 永不提交订单</small>
    </header>
    <dl>
      <div><dt>冻结候选</dt><dd>{summary.trial_count}</dd></div>
      <div><dt>独立决策批次</dt><dd>{summary.decision_batches}</dd></div>
      <div><dt>已结算结果</dt><dd>{summary.settled_outcomes}</dd></div>
      <div><dt>等待 / 持有</dt><dd>{summary.pending_entry} / {summary.open_positions}</dd></div>
      <div><dt>拒绝后上涨</dt><dd>{summary.rejected_positive}</dd></div>
      <div><dt>拒绝后非正收益</dt><dd>{summary.rejected_nonpositive}</dd></div>
      <div><dt>累计 / 期望</dt><dd>{summary.total_r}R / {summary.expectancy_r}R</dd></div>
      <div><dt>到期未入场</dt><dd>{summary.no_entry}</dd></div>
    </dl>
    {recent.length > 0 && <div className="agent-shadow-recent">{recent.map(trial => <article key={trial.id}>
      <div><strong>{trial.plan.symbol} · {trial.plan.timeframe} · {trial.plan.algorithm}</strong><span className={Number(trial.outcome.return_on_risk) > 0 ? 'positive' : 'negative'}>{trial.outcome.return_on_risk}R</span></div>
      <small>排名 #{trial.screening_rank} · {trial.selected_for_execution ? '当时被选中' : '当时被拒绝'} · {trial.outcome.status}</small>
    </article>)}</div>}
    <footer>这是严格使用决策后完整 K 线、费用、滑点和资金费率计算的反事实证据。它用于识别误拒绝与正确拦截；同批候选彼此相关，不计入测试网盈利证明，也不会放宽止损、回撤、仓位和保护单硬风控。</footer>
  </section>
}

function AgentStrategyResearch({
  trials,
  onOpenBacktest,
  onRerunBacktest,
}: {
  trials: ShadowValidationTrial[]
  onOpenBacktest: (selection: AgentBacktestSelection) => void
  onRerunBacktest: (selection: AgentBacktestSelection) => void
}) {
  const [symbolFilter, setSymbolFilter] = useState('all')
  const [timeframeFilter, setTimeframeFilter] = useState('all')
  const symbols = [...new Set(trials.map(trial => trial.plan.symbol))]
  const timeframes = [...new Set(trials.map(trial => trial.plan.timeframe))]
  const visible = trials.filter(trial => (
    (symbolFilter === 'all' || trial.plan.symbol === symbolFilter)
    && (timeframeFilter === 'all' || trial.plan.timeframe === timeframeFilter)
  )).slice(0, 18)
  const selectionFor = (trial: ShadowValidationTrial): AgentBacktestSelection => ({
    planId: trial.plan.id,
    strategyKind: trial.plan.algorithm,
    side: trial.plan.side,
    rank: String(trial.screening_rank),
    symbol: trial.plan.symbol,
    timeframe: trial.plan.timeframe,
  })

  return <section className="agent-strategy-research">
    <header>
      <div className="agent-section-title"><span>04</span><div><strong>策略与回测研究台</strong><small>冻结当时策略、证据、参数和拒绝理由；可在本页使用最新行情重跑</small></div></div>
      <div className="agent-research-filters">
        <select aria-label="筛选币种" value={symbolFilter} onChange={event => setSymbolFilter(event.target.value)}>
          <option value="all">全部币种</option>
          {symbols.map(symbol => <option key={symbol} value={symbol}>{symbol}</option>)}
        </select>
        <select aria-label="筛选周期" value={timeframeFilter} onChange={event => setTimeframeFilter(event.target.value)}>
          <option value="all">全部周期</option>
          {timeframes.map(timeframe => <option key={timeframe} value={timeframe}>{timeframe}</option>)}
        </select>
      </div>
    </header>
    {visible.length ? <div className="agent-research-grid">{visible.map(trial => {
      const backtest = recordValue(trial.backtest_evidence)
      const adaptation = recordValue(trial.adaptation_evidence)
      const score = recordValue(trial.selection_score)
      const reasons = Array.isArray(backtest.validation_reasons)
        ? backtest.validation_reasons.map(String)
        : []
      const targets = trial.plan.exit.take_profits
      const disposition = trial.selected_for_execution
        ? 'SELECTED'
        : trial.execution_eligible
          ? 'ELIGIBLE'
          : 'HOLD'
      const passed = backtest.robustness_passed === true
      const shadowR = trial.outcome.return_on_risk
      return <article key={trial.id}>
        <header>
          <span>#{trial.screening_rank}</span>
          <div><strong>{trial.plan.symbol} · {trial.plan.timeframe}</strong><small>{trial.plan.algorithm} · {String(adaptation.label ?? 'frozen')} · {trial.plan.side}</small></div>
          <i className={passed ? 'passed' : 'held'}>{disposition}</i>
        </header>
        <div className="agent-plan-structure">
          <div><span>入场区间</span><strong>{compactNumber(trial.plan.entry.zone.lower, 4)} – {compactNumber(trial.plan.entry.zone.upper, 4)}</strong></div>
          <div><span>止损</span><strong>{compactNumber(trial.plan.exit.stop_loss, 4)}</strong></div>
          <div><span>止盈</span><strong>{targets.map(target => `${compactNumber(target.price, 4)} × ${Number(target.close_fraction) * 100}%`).join(' / ')}</strong></div>
          <div><span>仓位 / 风险</span><strong>{compactNumber(trial.plan.sizing.suggested_quantity)} / {compactNumber(trial.plan.sizing.risk_amount, 2)} USDT</strong></div>
        </div>
        <dl>
          <div><dt>历史收益</dt><dd>{String(backtest.return_percent ?? '—')}%</dd></div>
          <div><dt>压力收益</dt><dd className={Number(backtest.stress_return_percent) > 0 ? 'positive' : Number(backtest.stress_return_percent) < 0 ? 'negative' : ''}>{String(backtest.stress_return_percent ?? '—')}%</dd></div>
          <div><dt>PF</dt><dd>{String(backtest.profit_factor ?? '—')}</dd></div>
          <div><dt>最大回撤</dt><dd>{String(backtest.max_drawdown_percent ?? '—')}%</dd></div>
          <div><dt>交易数</dt><dd>{String(backtest.total_trades ?? '—')}</dd></div>
          <div><dt>历史胜率</dt><dd>{String(backtest.win_rate_percent ?? '—')}%</dd></div>
          <div><dt>证据排序</dt><dd>{String(score.total ?? '—')} / 100</dd></div>
          <div><dt>影子结果</dt><dd>{shadowR == null ? trial.outcome.status : `${shadowR}R`}</dd></div>
        </dl>
        <p className={reasons.length ? 'held' : 'passed'}>
          {reasons.length ? reasons.join('；') : backtest.robustness_passed === true ? '全部研究门槛通过' : '冻结回测证据正在回填'}
        </p>
        <footer>
          <small>决策 {localTime(trial.decision_at)} · 批次 {trial.decision_batch_id}</small>
          <div>
            <button type="button" onClick={() => onOpenBacktest(selectionFor(trial))}>查看完整回测</button>
            <button type="button" className="primary" onClick={() => onRerunBacktest(selectionFor(trial))}><RefreshCw />最新行情重跑</button>
          </div>
        </footer>
      </article>
    })}</div> : <p className="agent-empty">尚无符合当前筛选条件的策略候选。Agent 在出现方向证据并完成候选筛选后会自动写入这里。</p>}
    {trials.length > visible.length && <footer className="agent-research-limit">当前显示最近 {visible.length}/{trials.length} 个候选，可按币种与周期缩小范围。</footer>}
  </section>
}

export default function AgentWorkspace({ settings }: { settings: ConfigurationView }) {
  const executionMode = modeForPlatform(settings.configuration.trading_mode)
  const [state, setState] = useState<AgentControlState | null>(null)
  const [readiness, setReadiness] = useState<AgentReadiness | null>(null)
  const [performance, setPerformance] = useState<AgentForwardPerformance | null>(null)
  const [selectedGoalId, setSelectedGoalId] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [message, setMessage] = useState<{ tone: 'success' | 'error'; text: string } | null>(null)
  const [policyMode, setPolicyMode] = useState<AgentAutonomyMode>('research')
  const [policyId, setPolicyId] = useState('personal-agent')
  const [confirmation, setConfirmation] = useState('')
  const [objective, setObjective] = useState('持续观察授权市场，只在证据、回测和硬风控全部通过时执行交易')
  const [goalSymbols, setGoalSymbols] = useState<string[]>(() => settings.configuration.strategy.watchlist.slice(0, 1))
  const [goalTimeframes, setGoalTimeframes] = useState<string[]>(() => {
    const preferred = settings.configuration.strategy.timeframes.filter(item => ['1h', '4h'].includes(item))
    return preferred.length ? preferred : settings.configuration.strategy.timeframes.slice(0, 1)
  })
  const [selectedRunId, setSelectedRunId] = useState('')
  const [runTrace, setRunTrace] = useState<AgentRunTrace | null>(null)
  const [traceLoading, setTraceLoading] = useState(false)
  const [showAllRuns, setShowAllRuns] = useState(false)
  const [backtestSelection, setBacktestSelection] = useState<AgentBacktestSelection | null>(null)
  const [agentBacktest, setAgentBacktest] = useState<BacktestResult | null>(null)
  const [agentBacktestLoading, setAgentBacktestLoading] = useState(false)
  const [agentBacktestStatus, setAgentBacktestStatus] = useState<BacktestJob['status'] | 'loading' | 'idle'>('idle')
  const [agentBacktestError, setAgentBacktestError] = useState<string | null>(null)
  const backtestRequestId = useRef(0)
  const selectionInitialized = useRef(false)

  const refresh = useCallback(async () => {
    try {
      const [next, nextReadiness, nextPerformance] = await Promise.all([
        getAgentState(),
        getAgentReadiness(),
        getAgentForwardPerformance(),
      ])
      setState(next)
      setReadiness(nextReadiness)
      setPerformance(nextPerformance)
      if (!selectionInitialized.current && next.goals.length) {
        const preferred = (
          next.goals.find(goal => (
            goal.status === 'active'
            && goal.autonomy_mode === executionMode
          ))
          ?? next.goals.find(goal => goal.status === 'active')
          ?? next.goals[0]
        )
        const boundPolicy = next.policies.find(
          policy => policy.id === preferred.risk_policy_id
        )
        setSelectedGoalId(preferred.id)
        setObjective(preferred.objective)
        setGoalSymbols(preferred.symbols)
        setGoalTimeframes(preferred.timeframes)
        if (boundPolicy) {
          setPolicyId(boundPolicy.id)
          setPolicyMode(boundPolicy.autonomy_mode)
          setConfirmation('')
        }
        selectionInitialized.current = true
      }
    } catch (cause) {
      setMessage({ tone: 'error', text: cause instanceof Error ? cause.message : 'Agent 状态读取失败' })
    }
  }, [executionMode])

  useEffect(() => {
    const initial = window.setTimeout(() => void refresh(), 0)
    const timer = window.setInterval(() => void refresh(), 5000)
    return () => { window.clearTimeout(initial); window.clearInterval(timer) }
  }, [refresh])

  const selectedGoal = state?.goals.find(goal => goal.id === selectedGoalId) ?? state?.goals[0]
  const selectedPolicy = state?.policies.find(policy => policy.id === selectedGoal?.risk_policy_id)
  const runs = useMemo(() => state?.runs.filter(run => run.goal_id === selectedGoal?.id) ?? [], [state, selectedGoal])
  const visibleRuns = showAllRuns ? runs : runs.slice(0, 12)
  const episodes = useMemo(() => state?.episodes.filter(item => item.goal_id === selectedGoal?.id) ?? [], [state, selectedGoal])
  const goalShadowTrials = useMemo(
    () => state?.shadow_trials.filter(trial => trial.goal_id === selectedGoal?.id) ?? [],
    [state, selectedGoal],
  )
  const marketCoverage = useMemo(
    () => state?.market_coverage.filter(item => item.goal_id === selectedGoal?.id) ?? [],
    [state, selectedGoal],
  )
  const episodeRuntime = useMemo(
    () => new Map((state?.episode_runtime ?? []).map(item => [item.episode_id, item])),
    [state],
  )
  const selectedGoalTrades = selectedGoal ? ['paper', 'sandbox', 'live_capped'].includes(selectedGoal.autonomy_mode) : false
  const expectedConfirmation = `AUTHORIZE AGENT ${policyId} ${policyMode}`
  const goalPolicy = state?.policies.find(item => item.id === policyId) ?? state?.policies[0]
  const effectiveGoalSymbols = goalPolicy ? goalSymbols.filter(item => goalPolicy.allowed_symbols.includes(item)) : goalSymbols
  const effectiveGoalTimeframes = goalPolicy ? goalTimeframes.filter(item => goalPolicy.allowed_timeframes.includes(item)) : goalTimeframes

  const act = async (key: string, task: () => Promise<unknown>, success: string) => {
    setBusy(key); setMessage(null)
    try { await task(); await refresh(); setMessage({ tone: 'success', text: success }) }
    catch (cause) { setMessage({ tone: 'error', text: cause instanceof Error ? cause.message : '操作失败' }) }
    finally { setBusy(null) }
  }

  const createPolicy = () => {
    const canTrade = policyMode === executionMode && ['paper', 'sandbox', 'live_capped'].includes(policyMode)
    const risk = settings.configuration.risk
    const policy: AgentPolicy = {
      id: policyId.trim(), autonomy_mode: policyMode,
      allowed_symbols: settings.configuration.strategy.watchlist,
      allowed_timeframes: settings.configuration.strategy.timeframes,
      max_risk_per_trade: canTrade ? String(risk.risk_per_trade) : '0',
      max_daily_drawdown: canTrade ? String(risk.max_daily_drawdown) : '0',
      max_open_positions: canTrade ? Math.min(risk.max_open_positions, 2) : 0,
      max_trades_per_day: canTrade ? 3 : 0,
      max_leverage: canTrade ? String(settings.configuration.market_type === 'swap' ? settings.configuration.contract_leverage : 1) : '1',
      require_native_protection: true, allow_agent_entry: canTrade,
      allow_agent_cancel_entry: true, allow_agent_reduce: true, allow_agent_exit: true, allow_agent_halt: true,
      fingerprint: '',
    }
    return act('policy', () => saveAgentPolicy(policy, confirmation), 'Agent 授权已保存；后续计划将绑定此版本的权限指纹')
  }

  const createGoal = () => {
    const policy = goalPolicy
    if (!policy) return setMessage({ tone: 'error', text: '请先创建 Agent 授权' })
    const symbols = effectiveGoalSymbols
    const timeframes = effectiveGoalTimeframes
    if (!symbols.length || !timeframes.length) {
      return setMessage({ tone: 'error', text: '目标至少需要选择 1 个币种和 1 个周期' })
    }
    return act('goal', () => createAgentGoal({
      objective, symbols, timeframes,
      autonomy_mode: policy.autonomy_mode, risk_policy_id: policy.id, poll_seconds: 60,
    }), '持久化 Agent 目标已创建并开始监听事件')
  }

  const updateGoalScope = () => {
    if (!selectedGoal || !goalPolicy || !effectiveGoalSymbols.length || !effectiveGoalTimeframes.length) {
      return setMessage({ tone: 'error', text: '请选择有效目标、币种和周期' })
    }
    return act('goal-scope', () => updateAgentGoalScope(selectedGoal, {
      objective,
      symbols: effectiveGoalSymbols,
      timeframes: effectiveGoalTimeframes,
      poll_seconds: selectedGoal.poll_seconds,
    }), '当前 Agent 目标范围已更新；过期调度将自动被最新范围替代')
  }

  const inspectRun = async (runId: string) => {
    if (selectedRunId === runId) {
      setSelectedRunId('')
      setRunTrace(null)
      return
    }
    setSelectedRunId(runId)
    setTraceLoading(true)
    try {
      setRunTrace(await getAgentRunTrace(runId))
    } catch (cause) {
      setRunTrace(null)
      setMessage({ tone: 'error', text: cause instanceof Error ? cause.message : '运行证据读取失败' })
    } finally {
      setTraceLoading(false)
    }
  }

  const loadCandidateBacktest = async (
    selection: AgentBacktestSelection,
    forceRerun = false,
  ) => {
    const requestId = ++backtestRequestId.current
    setBacktestSelection(selection)
    setAgentBacktestLoading(true)
    setAgentBacktestStatus(forceRerun ? 'queued' : 'loading')
    setAgentBacktestError(null)
    if (!forceRerun) setAgentBacktest(null)
    window.setTimeout(() => {
      document.getElementById('agent-backtest-workbench')?.scrollIntoView({
        behavior: 'smooth',
        block: 'start',
      })
    }, 0)
    try {
      const result = forceRerun
        ? await backtestStrategyByPlanId(selection.planId, job => {
            if (requestId === backtestRequestId.current) setAgentBacktestStatus(job.status)
          })
        : await getLatestBacktestByPlanId(selection.planId)
          ?? await backtestStrategyByPlanId(selection.planId, job => {
            if (requestId === backtestRequestId.current) setAgentBacktestStatus(job.status)
          })
      if (requestId !== backtestRequestId.current) return
      setAgentBacktest(result)
      setAgentBacktestStatus('completed')
    } catch (cause) {
      if (requestId !== backtestRequestId.current) return
      setAgentBacktestStatus('failed')
      setAgentBacktestError(cause instanceof Error ? cause.message : '完整回测读取失败')
    } finally {
      if (requestId === backtestRequestId.current) setAgentBacktestLoading(false)
    }
  }

  const closeCandidateBacktest = () => {
    backtestRequestId.current += 1
    setBacktestSelection(null)
    setAgentBacktest(null)
    setAgentBacktestLoading(false)
    setAgentBacktestStatus('idle')
    setAgentBacktestError(null)
  }

  if (!state) return <div className="agent-loading"><span /><strong>正在读取 Agent 状态账本</strong></div>

  return (
    <section className="agent-workspace">
      <header className="agent-command">
        <div><span className="eyebrow">AUTONOMOUS TRADING CONTROL</span><h1>交易 Agent</h1><p>目标、状态、记忆、工具、行动和反馈均持久化；所有增仓动作仍受非 AI 风控裁决。</p></div>
        <div className={`agent-runtime ${state.risk_state.halted ? 'halted' : 'ready'}`}><Activity /><span>系统风险闸</span><strong>{state.risk_state.halted ? 'HALTED' : 'READY'}</strong><small>{state.risk_state.reason || '未触发全局停机'}</small></div>
      </header>

      {readiness && <section className={`agent-readiness ${readiness.ready_for_autonomous_trading ? 'ready' : 'blocked'}`}>
        <div className="agent-readiness-title"><div><span>AUTONOMOUS READINESS · {readiness.mode.toUpperCase()}</span><strong>{readiness.ready_for_autonomous_trading ? '可自主交易' : '尚未满足自主交易条件'}</strong></div><small>{readiness.checks.filter(check => check.passed).length}/{readiness.checks.length} checks passed</small></div>
        <div className="agent-readiness-checks">{readiness.checks.map(check => <article key={check.code} className={check.passed ? 'passed' : 'failed'}>{check.passed ? <CheckCircle2 /> : <XCircle />}<div><strong>{check.code.replaceAll('_', ' ')}</strong><span>{check.message}</span></div></article>)}</div>
        {!readiness.ready_for_autonomous_trading && <div className="agent-readiness-guide">
          <div><strong>交易链路已通过，当前缺少操作员入场授权</strong><p>现有目标是 RESEARCH，只能分析、生成计划和回测；它主动等待是权限边界生效，不是 Agent 故障。</p></div>
          <ol><li>在左侧选择 <b>{executionMode}</b> 自主级别</li><li>输入精确短语 <code>AUTHORIZE AGENT {policyId} {executionMode}</code> 并保存权限</li><li>选择币种和周期后创建一个绑定该授权的新目标</li></ol>
          <small>当前仍为 {settings.configuration.trading_mode === 'sandbox' ? '交易所测试网' : settings.configuration.trading_mode}；本页面不会替操作员授予入场权限或解锁实盘。</small>
        </div>}
      </section>}
      <AgentMarketCoverageEvidence items={marketCoverage} />
      {performance && <ForwardPerformanceEvidence report={performance} />}
      <ShadowCalibrationEvidence summary={state.shadow_calibration} trials={state.shadow_trials} />

      <div className="agent-layout">
        <aside className="agent-goals">
          <div className="agent-section-title"><span>01</span><div><strong>目标与授权</strong><small>{state.goals.length} 个持久化目标</small></div></div>
          {state.goals.map(goal => <button key={goal.id} className={goal.id === selectedGoal?.id ? 'active' : ''} onClick={() => {
            const boundPolicy = state.policies.find(policy => policy.id === goal.risk_policy_id)
            setSelectedGoalId(goal.id)
            setObjective(goal.objective)
            setGoalSymbols(goal.symbols)
            setGoalTimeframes(goal.timeframes)
            if (boundPolicy) {
              setPolicyId(boundPolicy.id)
              setPolicyMode(boundPolicy.autonomy_mode)
              setConfirmation('')
            }
          }}><i className={goal.status} /><span>{goal.objective}</span><small>{goal.autonomy_mode} · {goal.status}</small></button>)}
          {!state.goals.length && <p className="agent-empty">尚未创建目标。先建立权限边界，再声明 Agent 要持续完成的任务。</p>}
          <div className="agent-create">
            <label><span>授权 ID</span><input value={policyId} onChange={event => setPolicyId(event.target.value)} /></label>
            <label><span>自主级别</span><select value={policyMode} onChange={event => { const mode = event.target.value as AgentAutonomyMode; setPolicyMode(mode); setConfirmation('') }}><option value="observe">observe · 只观察</option><option value="research">research · 可研究</option><option value={executionMode}>{executionMode} · 受限自动交易</option></select></label>
            <label><span>输入精确授权短语</span><input value={confirmation} onChange={event => setConfirmation(event.target.value)} placeholder={expectedConfirmation} /></label>
            <button className="button ghost" disabled={busy !== null || confirmation !== expectedConfirmation} onClick={createPolicy}><ShieldCheck />保存权限边界</button>
            <label><span>持续目标</span><textarea rows={4} value={objective} onChange={event => setObjective(event.target.value)} /></label>
            {goalPolicy && <div className="agent-scope-picker">
              <div><span>目标币种</span><small>仅扫描本目标需要的市场</small></div>
              <div>{goalPolicy.allowed_symbols.map(symbol => <button type="button" key={symbol} className={goalSymbols.includes(symbol) ? 'active' : ''} onClick={() => setGoalSymbols(current => current.includes(symbol) ? current.filter(item => item !== symbol) : [...current, symbol])}>{symbol}</button>)}</div>
              <div><span>目标周期</span><small>推荐主周期与确认周期组合</small></div>
              <div>{goalPolicy.allowed_timeframes.map(timeframe => <button type="button" key={timeframe} className={goalTimeframes.includes(timeframe) ? 'active' : ''} onClick={() => setGoalTimeframes(current => current.includes(timeframe) ? current.filter(item => item !== timeframe) : [...current, timeframe])}>{timeframe}</button>)}</div>
            </div>}
            {selectedGoal && <button className="button ghost" disabled={busy !== null || !effectiveGoalSymbols.length || !effectiveGoalTimeframes.length} onClick={updateGoalScope}><RefreshCw />应用范围到当前目标</button>}
            <button className="button primary" disabled={busy !== null || !state.policies.length || !effectiveGoalSymbols.length || !effectiveGoalTimeframes.length} onClick={createGoal}><Bot />创建 Agent 目标</button>
          </div>
        </aside>

        <main className="agent-operations">
          {selectedGoal ? <>
            <div className="agent-goal-head"><div><span>{selectedGoal.autonomy_mode.toUpperCase()}</span><h2>{selectedGoal.objective}</h2><p>{selectedGoal.symbols.join(' · ')} / {selectedGoal.timeframes.join(' · ')} · 每 {selectedGoal.poll_seconds}s 扫描 · 新收盘 K 线或事件时推理</p></div><strong className={selectedGoal.status}>{selectedGoal.status}</strong></div>
            <div className="agent-actions"><button className="button ghost" disabled={busy !== null} onClick={() => act('status', () => setAgentGoalStatus(selectedGoal, selectedGoal.status === 'active' ? 'paused' : 'active'), selectedGoal.status === 'active' ? 'Agent 已暂停' : 'Agent 已恢复')} >{selectedGoal.status === 'active' ? <CirclePause /> : <Play />}{selectedGoal.status === 'active' ? '暂停目标' : '恢复目标'}</button><button className="button ghost" disabled={busy !== null || selectedGoal.status !== 'active'} onClick={() => act('trigger', () => triggerAgentGoal(selectedGoal.id), '已写入一次人工触发事件')}><RefreshCw />写入事件</button><button className="button primary" disabled={busy !== null || selectedGoal.status !== 'active' || (selectedGoalTrades && !readiness?.ready_for_autonomous_trading)} title={selectedGoalTrades && !readiness?.ready_for_autonomous_trading ? '先通过上方全部自主交易就绪检查' : undefined} onClick={() => act('run', async () => { await triggerAgentGoal(selectedGoal.id); await runAgentOnce(selectedGoal.id) }, 'Agent 已完成一次有界推理运行')}><BrainCircuit />立即运行一次</button></div>
            <div className="agent-budget"><div><span>权限指纹</span><strong>{selectedPolicy?.fingerprint ?? '—'}</strong></div><div><span>单笔风险</span><strong>{selectedPolicy ? `${Number(selectedPolicy.max_risk_per_trade) * 100}%` : '—'}</strong></div><div><span>日内回撤</span><strong>{selectedPolicy ? `${Number(selectedPolicy.max_daily_drawdown) * 100}%` : '—'}</strong></div><div><span>每日交易</span><strong>{selectedPolicy?.max_trades_per_day ?? '—'}</strong></div><div><span>最大持仓</span><strong>{selectedPolicy?.max_open_positions ?? '—'}</strong></div></div>

            <div className="agent-streams">
              <section><div className="agent-section-title"><span>02</span><div><strong>运行与决策</strong><small>点击回合查看模型理由、工具调用与确定性证据</small></div></div>{runs.length ? visibleRuns.map(run => <button type="button" className={`agent-run ${selectedRunId === run.id ? 'selected' : ''}`} key={run.id} onClick={() => void inspectRun(run.id)}><i className={run.status} /><div><strong>{run.status.toUpperCase()}</strong><span>{run.step_count}/{run.max_steps} steps · {run.id}</span><p>{run.last_error || '运行已持久化，无未处理错误'}</p></div><time>{localTime(run.completed_at ?? run.updated_at)}</time></button>) : <p className="agent-empty">等待首个市场、订单、仓位或人工事件。</p>}
                {runs.length > 12 && <button type="button" className="agent-run-history-toggle" onClick={() => setShowAllRuns(current => !current)}>{showAllRuns ? '收起历史回合' : `展开更早回合（${runs.length - 12}）`}</button>}
                {(traceLoading || runTrace) && <div className="agent-trace" aria-live="polite">
                  <header><strong>决策证据链</strong><span>{traceLoading ? '读取中…' : `${runTrace?.decisions.length ?? 0} 次决策 · ${runTrace?.observations.length ?? 0} 条观察`}</span></header>
                  {!traceLoading && runTrace?.decisions.map((decision, index) => {
                    const observation = runTrace.observations.find(item => item.tool_call_id === decision.tool_call?.id)
                    return <article key={decision.id}>
                      <div className="agent-trace-step"><span>{String(index + 1).padStart(2, '0')}</span><strong>{decision.action}</strong><small>不确定性 {(Number(decision.uncertainty) * 100).toFixed(0)}%</small></div>
                      <p>{decision.reason}</p>
                      {decision.evidence.length > 0 && <ul>{decision.evidence.map(item => <li key={item}>{item}</li>)}</ul>}
                      {decision.tool_call && <div className="agent-tool-call"><span>TOOL</span><strong>{decision.tool_call.name}</strong><code>{JSON.stringify(decision.tool_call.arguments)}</code></div>}
                      {observation && <div className={`agent-observation ${observation.ok ? 'ok' : 'failed'}`}><span>{observation.ok ? 'OBSERVED' : 'FAILED'}</span><p>{observation.summary}</p>{Object.keys(observation.data).length > 0 && (observation.tool_call_id && decision.tool_call?.name === 'screen_trade_candidates' ? <CandidateScreenEvidence data={observation.data} onOpenBacktest={selection => void loadCandidateBacktest(selection)} /> : <pre>{evidencePayload(observation.data)}</pre>)}</div>}
                    </article>
                  })}
                  {!traceLoading && runTrace && runTrace.decisions.length === 0 && <p className="agent-empty">该回合在形成有效决策前结束。</p>}
                </div>}
              </section>
              <section><div className="agent-section-title"><span>03</span><div><strong>交易回合</strong><small>从授权入场到平仓复盘</small></div></div>{episodes.length ? episodes.map(item => {
                const execution = episodeRuntime.get(item.id)
                const protectionVerified = execution?.native_protection_status === 'verified'
                const summary = item.status === 'open' && protectionVerified
                  ? '仓位数量、入场归因与交易所原生保护持续对账一致'
                  : item.summary || `计划风险 ${item.planned_risk} USDT`
                return <article className="agent-episode" key={item.id}>
                  <div><strong>{item.symbol} · {item.side}</strong><span className={item.status}>{item.status}</span></div>
                  <p>{summary}</p>
                  {execution && execution.native_protection_status !== 'not_applicable' && <div className="agent-episode-execution">
                    <dl>
                      <div><dt>成交数量</dt><dd>{compactNumber(execution.entry_quantity)}</dd></div>
                      <div><dt>成交均价</dt><dd>{compactNumber(execution.average_entry, 4)}</dd></div>
                      <div><dt>止损</dt><dd>{compactNumber(execution.stop_loss, 4)}</dd></div>
                      <div><dt>止盈</dt><dd>{execution.take_profits.length ? execution.take_profits.map(target => compactNumber(target.price, 4)).join(' / ') : '—'}</dd></div>
                      <div><dt>杠杆</dt><dd>{execution.leverage ? `${compactNumber(execution.leverage, 2)}x` : '—'}</dd></div>
                      <div><dt>入场费用</dt><dd>{compactNumber(execution.entry_fees, 4)} USDT</dd></div>
                    </dl>
                    <span className={`agent-protection-state ${execution.native_protection_status}`}>
                      {protectionVerified ? <CheckCircle2 /> : <XCircle />}
                      {protectionVerified ? `原生保护已核验 · ${execution.protected_order_ids.length}/${item.entry_order_ids?.length ?? execution.protected_order_ids.length}` : '保护状态待对账'}
                    </span>
                    <small>最近对账 {localTime(execution.last_reconciled_at ?? undefined)}</small>
                  </div>}
                  <footer><span>{item.return_on_risk ? `${item.return_on_risk}R` : '未结算'}</span><time>{localTime(item.closed_at ?? item.opened_at)}</time></footer>
                </article>
              }) : <p className="agent-empty">尚无交易回合；研究和等待不会伪装成交易。</p>}</section>
            </div>
            <AgentStrategyResearch
              trials={goalShadowTrials}
              onOpenBacktest={selection => void loadCandidateBacktest(selection)}
              onRerunBacktest={selection => void loadCandidateBacktest(selection, true)}
            />
            {backtestSelection && <section id="agent-backtest-workbench" className="backtest-workbench agent-backtest-workbench">
              <header className="backtest-workbench-head">
                <div>
                  <span className="eyebrow">AGENT EVIDENCE REPLAY · RANK #{backtestSelection.rank}</span>
                  <h2>候选完整回测</h2>
                  <p>{backtestSelection.symbol} · {backtestSelection.timeframe} · {backtestSelection.strategyKind} · {backtestSelection.side}；结果绑定计划、策略和数据指纹。重新回测只更新研究证据，不会提交订单。</p>
                </div>
                <div className="backtest-run-state">
                  <span className={agentBacktestStatus}>
                    {agentBacktestStatus === 'loading'
                      ? '读取中'
                      : agentBacktestStatus === 'queued'
                        ? '排队中'
                        : agentBacktestStatus === 'running'
                          ? '计算中'
                          : agentBacktestStatus === 'completed'
                            ? '已完成'
                            : agentBacktestStatus === 'failed'
                              ? '失败'
                              : '未运行'}
                  </span>
                  <button className="button ghost" type="button" onClick={closeCandidateBacktest}>关闭</button>
                  <button className="button primary" type="button" onClick={() => void loadCandidateBacktest(backtestSelection, true)} disabled={agentBacktestLoading}>
                    <RefreshCw aria-hidden="true" className={agentBacktestLoading ? 'spinning' : ''} />
                    {agentBacktestLoading ? '正在回测' : '使用最新行情重跑'}
                  </button>
                </div>
              </header>
              {agentBacktestError && <div className="agent-backtest-error"><XCircle /><span>{agentBacktestError}</span></div>}
              {agentBacktest ? <BacktestEvidence result={agentBacktest} validation={settings.configuration.strategy.validation} /> : <div className="backtest-empty-state">
                <Activity />
                <strong>{agentBacktestLoading ? '正在读取完整权益曲线和逐笔证据' : '该候选暂无可显示的完整回测'}</strong>
                <p>完整结果包含成本拆分、最近窗口、逐笔成交和每一项稳健性门槛。</p>
              </div>}
            </section>}
            <section className="agent-memory"><div className="agent-section-title"><span>05</span><div><strong>可追溯记忆</strong><small>只保留带证据引用的经验</small></div></div>{state.memories.length ? state.memories.slice(0, 8).map(memory => <article key={memory.id}><span>{memory.memory_type}</span><p>{memory.content}</p><small>{memory.tags.join(' · ')} · confidence {memory.confidence}</small></article>) : <p className="agent-empty">完成并复盘交易后，Episode 记忆会在这里影响后续决策上下文。</p>}</section>
          </> : <div className="agent-zero"><Bot /><strong>先定义权限，再创建目标</strong><p>Agent 不会默认获得交易权，也不能修改自己的授权或恢复全局停机。</p></div>}
        </main>
      </div>
      {message && <div className={`agent-message ${message.tone}`}>{message.text}</div>}
    </section>
  )
}
