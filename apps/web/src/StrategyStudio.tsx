import { useEffect, useState } from 'react'
import { Activity, ArrowRight, RefreshCw, Sparkles } from 'lucide-react'
import {
  approveStrategy,
  backtestStrategy,
  createMLChallenger,
  createAIResearchCandidate,
  evaluateResearchCandidate,
  generateStrategy,
  getLatestBacktest,
  getResearchCandidates,
  getResearchDatasets,
  getResearchDrift,
  getStrategyMarketFit,
  promoteResearchCandidate,
} from './api'
import BacktestEvidence from './BacktestEvidence'
import ResearchCampaignWorkbench from './ResearchCampaignWorkbench'
import type {
  BacktestResult,
  BacktestJob,
  ConfigurationView,
  DriftReport,
  MLChallengerReport,
  AIProposalResponse,
  Plan,
  PromotionDecision,
  ResearchDataset,
  StrategyCandidate,
  StrategyGenerateRequest,
  StrategyKind,
  StrategyMarketFit,
} from './types'

type StrategyStudioProps = {
  settings: ConfigurationView
  initialPlan?: Plan
  onGenerated: (plan: Plan) => void
  onOpenSettings: () => void
}

const money = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

function pct(value: string | undefined) {
  if (value == null) return '—'
  const numeric = Number(value)
  return `${numeric > 0 ? '+' : ''}${numeric.toFixed(2)}%`
}

export default function StrategyStudio({ settings, initialPlan, onGenerated, onOpenSettings }: StrategyStudioProps) {
  const config = settings.configuration
  const [request, setRequest] = useState<StrategyGenerateRequest>({
    symbol: config.default_symbol,
    timeframe: config.default_timeframe,
    equity: config.trading_mode === 'paper' ? config.paper_equity : undefined,
    risk_fraction: config.risk.risk_per_trade,
    prompt: config.strategy.default_prompt,
    strategy_kind: config.strategy.default_kind,
  })
  const [plan, setPlan] = useState<Plan | undefined>(initialPlan)
  const [backtest, setBacktest] = useState<BacktestResult | null>(null)
  const [backtestLoading, setBacktestLoading] = useState(Boolean(initialPlan))
  const [backtestStatus, setBacktestStatus] = useState<BacktestJob['status'] | 'idle'>(
    initialPlan ? 'queued' : 'idle',
  )
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [drift, setDrift] = useState<DriftReport | null>(null)
  const [candidates, setCandidates] = useState<StrategyCandidate[]>([])
  const [datasets, setDatasets] = useState<ResearchDataset[]>([])
  const [selectedDatasetId, setSelectedDatasetId] = useState('')
  const [candidateBusy, setCandidateBusy] = useState('')
  const [candidateEvidenceId, setCandidateEvidenceId] = useState('')
  const [promotionResult, setPromotionResult] = useState<{ candidateId: string; decision: PromotionDecision } | null>(null)
  const [mlReport, setMlReport] = useState<MLChallengerReport | null>(null)
  const [mlBusy, setMlBusy] = useState(false)
  const [aiBusy, setAiBusy] = useState(false)
  const [aiReport, setAiReport] = useState<AIProposalResponse | null>(null)
  const [marketFit, setMarketFit] = useState<StrategyMarketFit | null>(null)
  const [marketFitLoading, setMarketFitLoading] = useState(Boolean(initialPlan))

  useEffect(() => {
    let cancelled = false
    if (!initialPlan || backtest?.strategy_fingerprint === initialPlan.fingerprint) {
      return () => { cancelled = true }
    }
    void getLatestBacktest(initialPlan)
      .then(result => result ?? backtestStrategy(initialPlan))
      .then(result => {
        if (!cancelled) {
          setBacktest(result)
          setBacktestStatus('completed')
        }
      })
      .catch(cause => {
        if (!cancelled) {
          setBacktestStatus('failed')
          setError(cause instanceof Error ? cause.message : '策略回测失败')
        }
      })
      .finally(() => { if (!cancelled) setBacktestLoading(false) })
    return () => { cancelled = true }
  }, [backtest?.strategy_fingerprint, initialPlan])

  useEffect(() => {
    let cancelled = false
    if (!plan) {
      return () => { cancelled = true }
    }
    void getStrategyMarketFit(plan)
      .then(result => {
        if (!cancelled) setMarketFit(result)
      })
      .catch(() => {
        if (!cancelled) setMarketFit(null)
      })
      .finally(() => {
        if (!cancelled) setMarketFitLoading(false)
      })
    return () => { cancelled = true }
  }, [plan])

  useEffect(() => {
    let cancelled = false
    void Promise.all([getResearchDrift(), getResearchCandidates(), getResearchDatasets()])
      .then(([driftReport, nextCandidates, nextDatasets]) => {
        if (!cancelled) {
          setDrift(driftReport)
          setCandidates(nextCandidates)
          setDatasets(nextDatasets)
        }
      })
      .catch(() => undefined)
    return () => { cancelled = true }
  }, [])

  const compatibleDatasets = datasets.filter(item => item.symbol === request.symbol && item.timeframe === request.timeframe)
  const activeDatasetId = compatibleDatasets.some(item => item.id === selectedDatasetId)
    ? selectedDatasetId
    : compatibleDatasets[0]?.id ?? ''
  const candidateEvidence = candidates.find(
    item => item.id === candidateEvidenceId,
  )

  const update = <K extends keyof StrategyGenerateRequest>(key: K, value: StrategyGenerateRequest[K]) => {
    setRequest(current => ({ ...current, [key]: value }))
  }

  const generate = async () => {
    setBusy(true)
    setError(null)
    setBacktest(null)
    try {
      const revisionOf = plan?.symbol === request.symbol && plan.timeframe === request.timeframe
        ? plan.id
        : undefined
      const generated = await generateStrategy({ ...request, revision_of: revisionOf })
      setMarketFit(null)
      setMarketFitLoading(true)
      setPlan(generated)
      setBacktestLoading(true)
      setBacktestStatus('queued')
      const result = await backtestStrategy(generated, job => setBacktestStatus(job.status))
      setBacktest(result)
      setBacktestStatus('completed')
      setDatasets(await getResearchDatasets())
      onGenerated(generated)
    } catch (cause) {
      setBacktestStatus('failed')
      setError(cause instanceof Error ? cause.message : '策略生成失败')
    } finally {
      setBacktestLoading(false)
      setBusy(false)
    }
  }

  const runBacktest = async () => {
    if (!plan) return
    setBacktestLoading(true)
    setBacktestStatus('queued')
    setError(null)
    try {
      const result = await backtestStrategy(plan, job => setBacktestStatus(job.status))
      setBacktest(result)
      setBacktestStatus('completed')
      setDatasets(await getResearchDatasets())
      void getStrategyMarketFit(plan)
        .then(setMarketFit)
        .catch(() => undefined)
    } catch (cause) {
      setBacktestStatus('failed')
      setError(cause instanceof Error ? cause.message : '策略回测失败')
    } finally {
      setBacktestLoading(false)
    }
  }

  const approve = async () => {
    if (!plan || !backtest) return
    setBusy(true)
    setError(null)
    try {
      const approved = await approveStrategy(plan)
      setPlan(approved)
      onGenerated(approved)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '策略审核失败')
    } finally {
      setBusy(false)
    }
  }

  const runMLChallenger = async () => {
    setMlBusy(true)
    setError(null)
    try {
      const report = await createMLChallenger()
      setMlReport(report)
      if (report.accepted) setCandidates(await getResearchCandidates())
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'ML Challenger 研究失败')
    } finally {
      setMlBusy(false)
    }
  }

  const runAIResearch = async () => {
    setAiBusy(true)
    setError(null)
    try {
      const report = await createAIResearchCandidate({
        hypothesis: request.prompt,
        family: request.strategy_kind,
        symbol: request.symbol,
        timeframe: request.timeframe,
      })
      setAiReport(report)
      setCandidates(await getResearchCandidates())
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'AI 研究候选生成失败')
    } finally {
      setAiBusy(false)
    }
  }

  const evaluateCandidate = async (candidate: StrategyCandidate) => {
    if (!activeDatasetId) {
      setError('当前品种和周期没有不可变研究数据集；请先生成策略或运行一次回测')
      return
    }
    setCandidateBusy(candidate.id)
    setPromotionResult(null)
    setError(null)
    try {
      const evaluated = await evaluateResearchCandidate(candidate.id, activeDatasetId)
      setCandidates(current => current.map(item => item.id === evaluated.id ? evaluated : item))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '候选回测失败')
    } finally {
      setCandidateBusy('')
    }
  }

  const promoteCandidate = async (candidate: StrategyCandidate) => {
    setCandidateBusy(candidate.id)
    setPromotionResult(null)
    setError(null)
    try {
      const decision = await promoteResearchCandidate(candidate)
      setPromotionResult({ candidateId: candidate.id, decision })
      setCandidates(await getResearchCandidates())
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '候选晋升失败')
    } finally {
      setCandidateBusy('')
    }
  }

  return (
    <section className="strategy-studio">
      <header className="workspace-heading">
        <div>
          <span className="eyebrow">Explainable strategy compiler</span>
          <h1>策略生成中心</h1>
          <p>确定性规则编译可审计点位；自然语言只记录研究意图，不会绕过参数约束或直接控制订单。</p>
        </div>
        <button className="button ghost" onClick={onOpenSettings}>修改默认风险</button>
      </header>

      <div className="studio-layout">
        <div className="strategy-composer">
          <div className="composer-section">
            <span className="section-number">01</span>
            <div><h2>记录你的研究意图</h2><p>该文本进入版本审计；实际点位仍由所选 StrategySpec 与确定性规则计算。</p></div>
          </div>
          <textarea
            className="strategy-prompt"
            rows={7}
            value={request.prompt}
            onChange={event => update('prompt', event.target.value)}
            placeholder="例如：只顺应 4 小时主要趋势，在 1 小时级别等待回踩，不追涨；分两次止盈，盈利后移动止损。"
          />
          <div className="prompt-examples">
            <button onClick={() => update('prompt', '顺应主要趋势，等待回踩到均线附近确认后入场，避免追涨；分两次止盈。')}>趋势回踩</button>
            <button onClick={() => update('prompt', '只在价格明显偏离统计均值且 RSI 出现反转时交易，快速止盈，不扛单。')}>均值回归</button>
            <button onClick={() => update('prompt', '等待 20 周期区间被放量突破，突破确认后跟随趋势，并使用 ATR 移动止损。')}>区间突破</button>
          </div>

          <div className="composer-section second">
            <span className="section-number">02</span>
            <div><h2>确定运行范围</h2><p>这里的值只影响本次生成，不会修改系统设置。</p></div>
          </div>
          <div className="field-grid three studio-fields">
            <label><span>策略算法</span><select value={request.strategy_kind} onChange={event => update('strategy_kind', event.target.value as StrategyKind)}><option value="trend">EMA 趋势回踩</option><option value="mean_reversion">布林 + RSI 均值回归</option><option value="breakout">Donchian 区间突破</option></select></label>
            <label><span>交易品种</span><select value={request.symbol} onChange={event => update('symbol', event.target.value)}>{config.strategy.watchlist.map(symbol => <option key={symbol}>{symbol}</option>)}</select></label>
            <label><span>周期</span><select value={request.timeframe} onChange={event => update('timeframe', event.target.value)}>{config.strategy.timeframes.map(timeframe => <option key={timeframe}>{timeframe}</option>)}</select></label>
            <label><span>计算权益 USDT</span><input type="number" value={request.equity ?? ''} disabled={config.trading_mode !== 'paper'} onChange={event => update('equity', event.target.value)} placeholder={config.trading_mode === 'paper' ? '模拟账户本金' : '由交易所账户实时读取'} /><small>{config.trading_mode === 'paper' ? '仅影响纸面账户策略定仓' : '测试网和实盘禁止使用模拟本金定仓'}</small></label>
            <label><span>单笔风险 %</span><input type="number" step="0.1" value={Number(request.risk_fraction) * 100} onChange={event => update('risk_fraction', String(Number(event.target.value) / 100))} /></label>
            <label><span>最低盈亏比</span><input value={config.risk.minimum_reward_risk} disabled title="在系统风险设置中修改" /></label>
          </div>
          <button className="generate-button" onClick={generate} disabled={busy || request.prompt.trim().length < 8}>
            <span>{busy ? '正在读取真实行情并回测…' : '生成可执行策略'}</span>
            <ArrowRight aria-hidden="true" />
          </button>
          <div className="research-console">
            <div className="research-console-summary">
              <strong>Champion / Challenger 研究隔离</strong>
              <p>候选 {candidates.length} 个 · 漂移状态 {drift?.status ?? '读取中'} · PSI {drift?.population_stability_index ?? '—'}。AI/ML 只提出受约束候选，必须另行回测、确认和晋升，不会自动替换交易计划。</p>
            </div>
            <div className="research-console-actions">
              <button className="button ghost" type="button" onClick={runAIResearch} disabled={aiBusy || request.prompt.trim().length < 12}><Sparkles aria-hidden="true" />{aiBusy ? '正在生成 AI 候选…' : '生成 AI 研究候选'}</button>
              <button className="button ghost" type="button" onClick={runMLChallenger} disabled={mlBusy}>{mlBusy ? '正在时间顺序训练…' : '运行 ML Challenger'}</button>
            </div>
            {(aiReport || mlReport) && (
              <div className="research-console-results" aria-live="polite">
                {aiReport && <p><span>AI</span>{aiReport.proposals.map(item => `${item.provider} / ${item.model} · ${item.candidate.id}`).join(' · ')}<small>候选已隔离登记，尚不可执行</small></p>}
                {mlReport && <p><span>ML</span>{mlReport.accepted ? `候选已登记 · 验证准确率 ${(Number(mlReport.validation_accuracy) * 100).toFixed(1)}%` : `未入选 · ${mlReport.rejection_reasons.join(' · ')}`}</p>}
              </div>
            )}
            <ResearchCampaignWorkbench
              datasets={datasets}
              candidates={candidates}
              onGovernanceComplete={() => {
                void getResearchCandidates().then(setCandidates)
              }}
            />
            <div className="candidate-lifecycle">
              <header>
                <div><strong>候选验证流水线</strong><small>固定数据集 → Walk-forward / 成本压力 → Challenger → Champion</small></div>
                <label>
                  <span>评估数据集</span>
                  <select value={activeDatasetId} disabled={!compatibleDatasets.length} onChange={event => setSelectedDatasetId(event.target.value)}>
                    {!compatibleDatasets.length && <option value="">当前范围暂无数据集</option>}
                    {compatibleDatasets.slice(0, 12).map(dataset => <option key={dataset.id} value={dataset.id}>{dataset.symbol} · {dataset.timeframe} · {dataset.candle_count} bars · {dataset.data_fingerprint.slice(0, 8)}</option>)}
                  </select>
                </label>
              </header>
              {activeDatasetId && (() => {
                const dataset = compatibleDatasets.find(item => item.id === activeDatasetId)
                return dataset ? <div className="candidate-dataset-evidence"><span>{dataset.quality_status.toUpperCase()}</span><strong>{dataset.data_start.slice(0, 10)} → {dataset.data_end.slice(0, 10)}</strong><small>指纹 {dataset.data_fingerprint} · gaps {dataset.gap_count} · invalid {dataset.invalid_count} · funding {dataset.funding_status}</small></div> : null
              })()}
              <div className="candidate-list">
                {candidates.slice(0, 8).map(candidate => <article key={candidate.id}>
                  <div className="candidate-identity">
                    <span>{candidate.source.toUpperCase()} · {candidate.spec.family}</span>
                    <strong>{candidate.id}</strong>
                    <small>规格指纹 {candidate.spec_fingerprint}</small>
                  </div>
                  <div className="candidate-status">
                    <span className={candidate.status}>{candidate.status}</span>
                    {candidate.backtest
                      ? <small>PF {Number(candidate.backtest.profit_factor).toFixed(2)} · 收益 {pct(candidate.backtest.return_percent)} · 回撤 {candidate.backtest.max_drawdown_percent}% · {candidate.backtest.total_trades} 笔</small>
                      : <small>尚未绑定不可变数据集回测</small>}
                  </div>
                  <p className="candidate-parameters">
                    {Object.entries(candidate.spec.parameters ?? {}).map(
                      ([key, value]) => `${key}=${String(value)}`,
                    ).join(' · ')}
                  </p>
                  <div className="candidate-actions">
                    {candidate.backtest && <button type="button" className="button ghost" onClick={() => setCandidateEvidenceId(candidate.id)}>查看完整回测</button>}
                    {(candidate.status === 'proposed' || candidate.status === 'rejected') && <button type="button" className="button ghost" disabled={candidateBusy !== '' || !activeDatasetId} onClick={() => void evaluateCandidate(candidate)}>{candidateBusy === candidate.id ? '评估中…' : candidate.status === 'rejected' ? '用所选数据重评' : '绑定并评估'}</button>}
                    {candidate.status === 'challenger' && <button type="button" className="button primary" disabled={candidateBusy !== ''} onClick={() => void promoteCandidate(candidate)}>{candidateBusy === candidate.id ? '比较中…' : '比较并晋升'}</button>}
                    {candidate.status === 'champion' && <strong>当前 Champion</strong>}
                  </div>
                  {candidate.backtest && <p className="candidate-reasons">{candidate.decision_reasons.slice(-3).join(' · ')}</p>}
                  {promotionResult?.candidateId === candidate.id && <div className={`candidate-decision ${promotionResult.decision.approved ? 'approved' : 'rejected'}`}><strong>{promotionResult.decision.approved ? '晋升通过' : '保持 Challenger'}</strong><span>{Object.entries(promotionResult.decision.checks).map(([key, passed]) => `${passed ? '✓' : '×'} ${key}`).join(' · ')}</span><small>{promotionResult.decision.reasons.join(' · ')}</small></div>}
                </article>)}
                {!candidates.length && <p>尚无隔离候选。生成 AI 候选或运行 ML Challenger 后会显示在这里。</p>}
              </div>
            </div>
            {candidateEvidence?.backtest && (
              <section className="candidate-backtest-panel">
                <header>
                  <div>
                    <span>IMMUTABLE CANDIDATE EVIDENCE</span>
                    <strong>{candidateEvidence.id} · {String(candidateEvidence.spec.family)}</strong>
                    <small>规格 {candidateEvidence.spec_fingerprint} · 数据集 {candidateEvidence.dataset_id ?? '—'}</small>
                  </div>
                  <button type="button" className="button ghost" onClick={() => setCandidateEvidenceId('')}>关闭</button>
                </header>
                <BacktestEvidence
                  result={candidateEvidence.backtest}
                  validation={config.strategy.validation}
                />
              </section>
            )}
          </div>
          {error && <div className="studio-error">{error}</div>}
        </div>

        <aside className="strategy-output">
          {!plan ? (
            <div className="empty-output"><span>STRATEGY PLAN</span><strong>等待生成</strong><p>系统不会用占位数据填充这里。真实行情连接成功后才会产生点位。</p></div>
          ) : (
            <>
              <div className="output-title"><span>{plan.name} · v{plan.version}</span><i>{plan.status === 'draft' ? '待审核' : plan.status === 'approved' ? '已审核' : plan.status === 'active' ? '已激活' : plan.status === 'paused' ? '已暂停' : '已归档'}</i><h2>{plan.side === 'long' ? '做多' : '做空'} {plan.symbol}</h2><p>{plan.thesis}</p></div>
              <div className="output-level entry"><span>开仓区间</span><strong>{money.format(Number(plan.entry.zone.lower))} — {money.format(Number(plan.entry.zone.upper))}</strong><p>{plan.entry.trigger}</p></div>
              <div className="output-levels-split">
                {plan.exit.take_profits.map((target, index) => <div className="output-level target" key={target.price}><span>止盈 {index + 1} · {Number(target.close_fraction) * 100}%</span><strong>{money.format(Number(target.price))}</strong><p>{target.rationale}</p></div>)}
              </div>
              <div className="output-level stop"><span>硬止损</span><strong>{money.format(Number(plan.exit.stop_loss))}</strong><p>{plan.exit.invalidation}</p></div>
              {plan.exit.trailing_stop_atr && (
                <div className="output-level">
                  <span>ATR 追踪 · {
                    plan.exit.trailing_activation === 'immediate'
                      ? '入场后立即激活'
                      : plan.exit.trailing_activation === 'one_r'
                        ? '达到 1R 后激活'
                        : '第一止盈后激活'
                  }</span>
                  <strong>{plan.exit.trailing_stop_atr} ATR</strong>
                  <p>原生硬止损始终保留；Agent 在已收盘 K 线上复核追踪退出。</p>
                </div>
              )}
              <div className="output-sizing"><div><span>建议数量</span><strong>{plan.sizing.suggested_quantity}</strong></div><div><span>名义价值</span><strong>{money.format(Number(plan.sizing.notional))}</strong></div><div><span>计划亏损</span><strong>{money.format(Number(plan.sizing.risk_amount))}</strong></div><div><span>合约参数</span><strong>{plan.market_type === 'swap' ? `${plan.leverage}x · ${plan.margin_mode === 'cross' ? '全仓' : '逐仓'}` : '现货'}</strong></div></div>
              {backtest ? <div className="studio-backtest"><div><span>Walk-forward 收益</span><strong className={Number(backtest.return_percent) >= 0 ? 'positive' : 'negative'}>{Number(backtest.return_percent) > 0 ? '+' : ''}{backtest.return_percent}%</strong></div><div><span>成本压力收益</span><strong className={Number(backtest.stress_return_percent ?? 0) >= 0 ? 'positive' : 'negative'}>{backtest.stress_return_percent ?? '—'}%</strong></div><div><span>交易数 / 样本</span><strong>{backtest.total_trades} / {backtest.sample_bars}</strong></div><div><span>稳健性门槛</span><strong className={backtest.robustness_passed ? 'positive' : 'negative'}>{backtest.robustness_passed ? 'PASS' : 'HOLD'}</strong></div></div> : backtestLoading ? <div className="backtest-running">正在使用真实历史 K 线回测…</div> : <div className="backtest-running failed">尚无有效回测结果</div>}
              {backtest && <div className={`backtest-provenance ${backtest.robustness_passed ? '' : 'negative'}`}>{backtest.validation_reasons.join(' · ')} · 手续费 {backtest.cost_model.fee_bps} bps · 滑点 {backtest.cost_model.slippage_bps} bps</div>}
              {backtest?.data_fingerprint && <div className="backtest-provenance">数据指纹 {backtest.data_fingerprint} · {backtest.data_start?.slice(0, 10)} 至 {backtest.data_end?.slice(0, 10)}</div>}
              <div className="output-footer"><span>策略指纹 {plan.fingerprint}</span>{plan.status === 'draft' ? <button onClick={approve} disabled={!backtest?.robustness_passed || busy} title={!backtest?.robustness_passed ? '稳健性验收通过后才能审核' : undefined}>审核此版本</button> : <button onClick={() => onGenerated(plan)}>设为当前策略</button>}</div>
            </>
          )}
        </aside>
      </div>
      {plan && (
        <section className="backtest-workbench">
          <header className="backtest-workbench-head">
            <div>
              <span className="eyebrow">Evidence-bound validation</span>
              <h2>回测与稳健性验证</h2>
              <p>{plan.symbol} · {plan.timeframe} · {plan.name}；结果绑定策略与数据指纹，重新生成策略后必须重新回测。</p>
            </div>
            <div className="backtest-run-state">
              <span className={backtestStatus}>{backtestStatus === 'queued' ? '排队中' : backtestStatus === 'running' ? '计算中' : backtestStatus === 'completed' ? '已完成' : backtestStatus === 'failed' ? '失败' : '未运行'}</span>
              <button className="button primary" type="button" onClick={runBacktest} disabled={backtestLoading || busy}>
                <RefreshCw aria-hidden="true" className={backtestLoading ? 'spinning' : ''} />
                {backtestLoading ? '正在回测' : backtest ? '使用最新行情重跑' : '立即运行回测'}
              </button>
            </div>
          </header>

          <div className={`market-fit-strip ${marketFit?.verdict ?? 'checking'}`}>
            <div className="market-fit-verdict">
              <span>最新市场适配</span>
              <strong>
                {marketFitLoading
                  ? '检查中'
                  : marketFit?.verdict === 'aligned'
                    ? '方向一致'
                    : marketFit?.verdict === 'conflict'
                      ? '方向冲突'
                      : '等待信号'}
              </strong>
            </div>
            <p>{marketFit?.summary ?? '正在用最新已收盘 K 线检查策略家族、方向与高周期确认。'}</p>
            {marketFit && (
              <div className="market-fit-facts">
                <span>计划 {plan.algorithm}</span>
                <span>市场 {marketFit.assessment.selected_kind} · {(Number(marketFit.assessment.confidence) * 100).toFixed(1)}%</span>
                <span>信号 {marketFit.signal.direction} · {(Number(marketFit.signal.confidence) * 100).toFixed(1)}%</span>
                <span>{new Date(marketFit.data_as_of).toLocaleString('zh-CN')}</span>
              </div>
            )}
          </div>

          {backtest ? (
            <BacktestEvidence
              result={backtest}
              validation={config.strategy.validation}
            />
          ) : (
            <div className="backtest-empty-state">
              <Activity />
              <strong>{backtestLoading ? '正在构建不可变数据集并计算逐笔结果' : '该策略还没有绑定回测证据'}</strong>
              <p>回测完成后，这里会显示权益曲线、成本、逐笔交易和每一项未通过原因。</p>
            </div>
          )}
        </section>
      )}
    </section>
  )
}
