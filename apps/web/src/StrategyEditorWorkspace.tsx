import { useEffect, useState } from 'react'
import { BookMarked, RefreshCw, Settings2, Sparkles } from 'lucide-react'
import {
  backtestStrategy,
  createPersonalStrategy,
  generateStrategy,
  getLatestBacktest,
} from './api'
import type {
  BacktestJob,
  BacktestResult,
  ConfigurationView,
  Plan,
  StrategyGenerateRequest,
} from './types'

const money = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 4 })

export default function StrategyEditorWorkspace({
  settings,
  initialPlan,
  onGenerated,
  onOpenSettings,
}: {
  settings: ConfigurationView
  initialPlan?: Plan
  onGenerated: (plan: Plan) => void
  onOpenSettings: () => void
}) {
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
  const [status, setStatus] = useState<BacktestJob['status'] | 'idle'>('idle')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')

  useEffect(() => {
    if (!initialPlan) return
    let cancelled = false
    void getLatestBacktest(initialPlan).then(result => { if (!cancelled) setBacktest(result) }).catch(() => undefined)
    return () => { cancelled = true }
  }, [initialPlan])

  const update = <K extends keyof StrategyGenerateRequest>(key: K, value: StrategyGenerateRequest[K]) => setRequest(current => ({ ...current, [key]: value }))

  const generate = async () => {
    setBusy(true); setMessage(''); setBacktest(null); setStatus('queued')
    try {
      const next = await generateStrategy({ ...request, revision_of: plan?.id })
      setPlan(next)
      onGenerated(next)
      const result = await backtestStrategy(next, job => setStatus(job.status))
      setBacktest(result)
      setStatus('completed')
    } catch (cause) {
      setStatus('failed')
      setMessage(cause instanceof Error ? cause.message : '策略生成失败')
    } finally { setBusy(false) }
  }

  const saveToLibrary = async () => {
    if (!plan?.strategy_spec) return
    setBusy(true)
    try {
      await createPersonalStrategy({ name: plan.name, description: plan.thesis, strategy_spec: plan.strategy_spec, origin_ref: plan.id })
      setMessage('当前计划已复制到个人策略库')
    } catch (cause) { setMessage(cause instanceof Error ? cause.message : '保存到策略库失败') }
    finally { setBusy(false) }
  }

  return <section className="ops-page strategy-editor-workspace">
    <header className="ops-page-header"><div><span className="ops-kicker">离线研究编译器</span><h1>策略编辑器</h1><p>记录研究意图、生成候选并回测；这里不会激活计划或提交订单。</p></div><button className="button ghost" onClick={onOpenSettings}><Settings2 />修改默认风险</button></header>
    <div className="strategy-editor-layout">
      <main className="strategy-editor-form">
        <section><div className="editor-step"><b>01</b><div><strong>研究意图</strong><span>用于版本审计，不直接控制订单。</span></div></div><textarea rows={6} value={request.prompt} onChange={event => update('prompt', event.target.value)} /><div className="prompt-presets"><button onClick={() => update('prompt', '顺应主要趋势，等待回踩确认后入场，避免追涨杀跌。')}>趋势回踩</button><button onClick={() => update('prompt', '仅在明确区间内寻找均值回归，价格重回均值后退出。')}>均值回归</button><button onClick={() => update('prompt', '等待区间突破和成交确认，假突破时立即退出。')}>区间突破</button></div></section>
        <section><div className="editor-step"><b>02</b><div><strong>运行范围</strong><span>只影响本次策略生成。</span></div></div><div className="editor-field-grid"><label><span>策略算法</span><select value={request.strategy_kind} onChange={event => update('strategy_kind', event.target.value as StrategyGenerateRequest['strategy_kind'])}><option value="trend">EMA 趋势回踩</option><option value="mean_reversion">布林 + RSI 均值回归</option><option value="breakout">Donchian 区间突破</option></select></label><label><span>交易品种</span><select value={request.symbol} onChange={event => update('symbol', event.target.value)}>{config.strategy.watchlist.map(symbol => <option key={symbol}>{symbol}</option>)}</select></label><label><span>周期</span><select value={request.timeframe} onChange={event => update('timeframe', event.target.value)}>{config.strategy.timeframes.map(timeframe => <option key={timeframe}>{timeframe}</option>)}</select></label><label><span>单笔风险 %</span><input type="number" step="0.05" min="0.05" max="5" value={Number(request.risk_fraction) * 100} onChange={event => update('risk_fraction', String(Number(event.target.value) / 100))} /></label></div></section>
        <button className="strategy-compile-button" disabled={busy || request.prompt.trim().length < 8} onClick={() => void generate()}><Sparkles />{busy ? '生成并验证中…' : '生成策略并运行回测'}</button>
        {message && <div className="editor-message">{message}</div>}
      </main>
      <aside className="strategy-plan-inspector">
        {!plan ? <div className="plan-empty"><Sparkles /><strong>等待生成策略</strong><p>右侧只展示当前人工计划和验证结果，不再混入 Campaign 与 Agent 研究日志。</p></div> : <>
          <header><div><span>{plan.name} · v{plan.version}</span><h2>{plan.side === 'long' ? '做多' : '做空'} {plan.symbol}</h2><p>{plan.thesis}</p></div><b className={plan.status}>{plan.status}</b></header>
          <div className="plan-price-levels"><article className="entry"><span>开仓区间</span><strong>{money.format(Number(plan.entry.zone.lower))} — {money.format(Number(plan.entry.zone.upper))}</strong><small>{plan.entry.trigger}</small></article><article className="stop"><span>硬止损</span><strong>{money.format(Number(plan.exit.stop_loss))}</strong><small>{plan.exit.invalidation}</small></article>{plan.exit.take_profits.map((target, index) => <article key={target.price}><span>止盈 {index + 1}</span><strong>{money.format(Number(target.price))}</strong><small>平仓 {Number(target.close_fraction) * 100}%</small></article>)}</div>
          <dl className="plan-sizing"><div><dt>建议数量</dt><dd>{plan.sizing.suggested_quantity}</dd></div><div><dt>名义价值</dt><dd>{money.format(Number(plan.sizing.notional))}</dd></div><div><dt>计划亏损</dt><dd>{money.format(Number(plan.sizing.risk_amount))}</dd></div><div><dt>杠杆</dt><dd>{plan.leverage}x</dd></div></dl>
          <div className={`backtest-verdict ${backtest?.robustness_passed ? 'passed' : status}`}><div><span>稳健性验证</span><strong>{backtest?.robustness_passed ? 'PASS' : status === 'running' || status === 'queued' ? '正在计算' : 'HOLD'}</strong></div>{backtest && <dl><div><dt>收益</dt><dd>{backtest.return_percent}%</dd></div><div><dt>PF</dt><dd>{backtest.profit_factor}</dd></div><div><dt>最大回撤</dt><dd>{backtest.max_drawdown_percent}%</dd></div><div><dt>交易数</dt><dd>{backtest.total_trades}</dd></div></dl>}{backtest && <p>{backtest.validation_reasons.join(' · ')}</p>}</div>
          <div className="plan-inspector-actions"><button className="button ghost" disabled={busy || !plan.strategy_spec} onClick={() => void saveToLibrary()}><BookMarked />保存到个人策略库</button><button className="button ghost" disabled={busy} onClick={() => void generate()}><RefreshCw />重新生成</button></div>
        </>}
      </aside>
    </div>
  </section>
}
