import { useEffect, useMemo, useRef, useState } from 'react'
import { BrainCircuit, Check, Coins, FlaskConical, LayoutDashboard, ListOrdered, RadioTower, RefreshCw, Settings, ShieldCheck, SlidersHorizontal, Workflow, X } from 'lucide-react'
import { activateStrategy, cancelOrder, executeStrategy, getDashboard, getMarketWindow, getSettings, haltTrading, openMarketStream, resumeTrading } from './api'
import AISettingsWorkspace from './AISettingsWorkspace'
import AgentWorkspace from './AgentWorkspace'
import ProfessionalChart from './ProfessionalChart'
import SettingsWorkspace from './SettingsWorkspace'
import StrategyStudio from './StrategyStudio'
import { CHART_TIMEFRAMES } from './timeframes'
import type { ConfigurationView, DashboardData, MarketStreamEvent, Plan, StrategySignal, TradingMode } from './types'

const money = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const compact = new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 2 })
const CHART_CANDLE_LIMIT = 300
type AppSection = 'overview' | 'risk' | 'positions' | 'orders' | 'lab' | 'strategy' | 'agent' | 'ai' | 'settings'

function initialSection(): AppSection {
  const requested = new URLSearchParams(window.location.search).get('section')
  const supported: AppSection[] = ['overview', 'risk', 'positions', 'orders', 'lab', 'strategy', 'agent', 'ai', 'settings']
  return supported.includes(requested as AppSection) ? requested as AppSection : 'overview'
}

function fmt(value: string | number) {
  return money.format(Number(value))
}

function pct(value: string | number) {
  const number = Number(value)
  return `${number > 0 ? '+' : ''}${number.toFixed(2)}%`
}

function modeLabel(mode: string) {
  if (mode === 'paper') return 'PAPER 模拟盘'
  if (mode === 'sandbox') return 'SANDBOX 测试网'
  if (mode === 'live') return 'LIVE 实盘'
  return mode.toUpperCase()
}

function EquitySparkline({ values }: { values: string[] }) {
  const width = 280
  const height = 56
  const numbers = values.map(Number)
  const min = Math.min(...numbers)
  const max = Math.max(...numbers)
  const points = numbers.map((value, index) => `${index / (numbers.length - 1) * width},${height - (value - min) / Math.max(max - min, 1) * (height - 6)}`).join(' ')
  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="sparkline" aria-hidden="true">
      <polyline points={points} fill="none" />
    </svg>
  )
}

function StrategyRail({ plan, signal, mode, onGenerate, onActivate, onExecute, busy, disabled }: {
  plan: Plan | null
  signal: StrategySignal
  mode: TradingMode
  onGenerate: () => void
  onActivate: () => void
  onExecute: () => void
  busy: boolean
  disabled: boolean
}) {
  if (!plan) {
    return (
      <aside className="strategy-rail no-plan">
        <div className="rail-heading"><div><span className="eyebrow">当前市场状态</span><h2>观望 / NO TRADE</h2></div><span className="state-dot paused">等待</span></div>
        <div className={`live-signal ${signal.direction}`}><span>{signal.regime.toUpperCase()}</span><strong>暂无可执行计划 <small>{(Number(signal.confidence) * 100).toFixed(1)}%</small></strong><p>{signal.reasons.at(-1)}</p></div>
        <p className="plan-summary">系统没有强行选择多空方向。只有最新已收盘 K 线和高周期确认同时达到门槛后，才会生成可审核计划。</p>
        <div className="rail-actions"><button className="button primary" onClick={onGenerate} disabled={busy}>打开策略研究</button></div>
      </aside>
    )
  }
  return (
    <aside className="strategy-rail">
      <div className="rail-heading">
        <div>
          <span className="eyebrow">当前交易计划</span>
          <h2>{plan.side === 'long' ? '做多' : '做空'} {plan.symbol}</h2>
        </div>
        <span className={`state-dot ${plan.status}`}>{plan.status === 'active' ? '已激活' : plan.status === 'approved' ? '已审核' : '草稿'}</span>
      </div>

      <p className="plan-summary">{plan.human_summary}</p>

      <div className={`live-signal ${signal.direction}`}>
        <span>最新已收盘信号 · {signal.regime.toUpperCase()}</span>
        <strong>{signal.direction === 'flat' ? '观望 / NO TRADE' : signal.direction === 'long' ? '做多候选' : '做空候选'} <small>{(Number(signal.confidence) * 100).toFixed(1)}%</small></strong>
        <p>{signal.reasons.at(-1)}</p>
      </div>

      <div className="levels">
        <div className="level entry">
          <span>开仓区间</span>
          <strong>{fmt(plan.entry.zone.lower)} <small>—</small> {fmt(plan.entry.zone.upper)}</strong>
          <i>限价 · 最大滑点 {plan.entry.max_slippage_bps} bps</i>
        </div>
        {plan.exit.take_profits.map((target, index) => (
          <div className="level target" key={target.price}>
            <span>止盈 {index + 1} · {Number(target.close_fraction) * 100}%</span>
            <strong>{fmt(target.price)}</strong>
            <i>{target.rationale}</i>
          </div>
        ))}
        <div className="level stop">
          <span>硬止损</span>
          <strong>{fmt(plan.exit.stop_loss)}</strong>
          <i>
            预计最大亏损 {fmt(plan.sizing.risk_amount)} USDT
            {plan.exit.trailing_stop_atr
              ? ` · ${plan.exit.trailing_stop_atr} ATR 追踪（${
                  plan.exit.trailing_activation === 'immediate'
                    ? '立即'
                    : plan.exit.trailing_activation === 'one_r'
                      ? '1R 后'
                      : 'TP1 后'
                }激活）`
              : ''}
          </i>
        </div>
      </div>

      <div className="sizing-line">
        <span>建议仓位</span>
        <strong>{plan.sizing.suggested_quantity} {plan.symbol.split('/')[0]}</strong>
        <span>{compact.format(Number(plan.sizing.notional))} USDT 名义价值{plan.market_type === 'swap' ? ` · ${plan.leverage}x ${plan.margin_mode === 'cross' ? '全仓' : '逐仓'} · 约 ${fmt(Number(plan.sizing.notional) / Number(plan.leverage))} USDT 保证金` : ''}</span>
      </div>

      <details className="rationale" open>
        <summary>为什么是这些点位</summary>
        <ul>{plan.reasons.map(reason => <li key={reason}>{reason}</li>)}</ul>
      </details>

      <div className="rail-actions">
        <button className="button ghost" onClick={onGenerate} disabled={busy}>重新生成</button>
        {plan.status === 'active' ? (
          <button className="button primary" onClick={onExecute} disabled={busy || disabled}>{mode === 'live' ? '提交真实订单' : mode === 'sandbox' ? '提交测试网订单' : '提交纸面订单'}</button>
        ) : plan.status === 'approved' ? (
          <button className="button primary" onClick={onActivate} disabled={busy || disabled}>激活策略</button>
        ) : (
          <button className="button primary" onClick={onGenerate} disabled={busy}>先回测并审核</button>
        )}
      </div>
      <div className="fingerprint">策略指纹 {plan.fingerprint} · v{plan.version}</div>
    </aside>
  )
}

function App() {
  const [dashboard, setDashboard] = useState<DashboardData | null>(null)
  const [settingsView, setSettingsView] = useState<ConfigurationView | null>(null)
  const [dashboardError, setDashboardError] = useState<string | null>(null)
  const [section, setSection] = useState<AppSection>(initialSection)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [confirming, setConfirming] = useState(false)
  const [planReviewed, setPlanReviewed] = useState(false)
  const [selectedTimeframe, setSelectedTimeframe] = useState('1h')
  const [selectedSymbol, setSelectedSymbol] = useState('')
  const [chartBusy, setChartBusy] = useState(false)
  const [streamState, setStreamState] = useState<'connecting' | 'live' | 'stale' | 'offline'>('connecting')
  const lastMarketEventAt = useRef(0)
  const confirmationDialogRef = useRef<HTMLDivElement>(null)
  const reviewInputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (!confirming) return
    const previouslyFocused = document.activeElement as HTMLElement | null
    reviewInputRef.current?.focus()
    const handleDialogKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setConfirming(false)
        return
      }
      if (event.key !== 'Tab') return
      const focusable = [...(confirmationDialogRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled)') ?? [])]
      if (!focusable.length) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    window.addEventListener('keydown', handleDialogKey)
    return () => {
      window.removeEventListener('keydown', handleDialogKey)
      previouslyFocused?.focus()
    }
  }, [confirming])

  const refresh = async (view = settingsView, symbolOverride = selectedSymbol, timeframeOverride = selectedTimeframe) => {
    try {
      const result = await getDashboard()
      const symbol = symbolOverride || view?.configuration.default_symbol
      if (view && symbol && (symbol !== view.configuration.default_symbol || timeframeOverride !== view.configuration.default_timeframe)) {
        const window = await getMarketWindow(
          symbol,
          timeframeOverride,
        )
        result.market = window.market
        result.candles = window.candles
        result.system.market_quality = window.quality
      }
      setDashboard(result)
      setDashboardError(null)
    } catch (error) {
      setDashboard(null)
      setDashboardError(error instanceof Error ? error.message : '真实行情连接失败')
    }
  }

  useEffect(() => {
    let cancelled = false
    void getSettings().then(result => {
      if (!cancelled) {
        setSettingsView(result)
        setSelectedTimeframe(result.configuration.default_timeframe)
        setSelectedSymbol(result.configuration.default_symbol)
      }
    })
    void getDashboard()
      .then(result => { if (!cancelled) { setDashboard(result); setDashboardError(null) } })
      .catch(error => { if (!cancelled) setDashboardError(error instanceof Error ? error.message : '真实行情连接失败') })
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    if (!settingsView) return
    let stopped = false
    let socket: WebSocket | null = null
    let reconnectTimer: number | undefined
    let reconnectDelay = 1_000
    const timeframe = selectedTimeframe
    const symbol = selectedSymbol || settingsView.configuration.default_symbol

    const applyMarket = (event: MarketStreamEvent) => {
      const receivedAt = Date.now()
      lastMarketEventAt.current = receivedAt
      setStreamState('live')
      setDashboard(current => {
        if (!current || current.market.symbol !== event.market.symbol) return current
        const forming = event.candles[timeframe]
        let candles = current.candles
        if (forming) {
          candles = [...current.candles]
          const last = candles[candles.length - 1]
          const formingTime = Date.parse(forming.timestamp)
          const lastTime = last ? Date.parse(last.timestamp) : 0
          if (formingTime === lastTime) candles[candles.length - 1] = forming
          else if (formingTime > lastTime) candles = [...candles.slice(-(CHART_CANDLE_LIMIT - 1)), forming]
        }
        return {
          ...current,
          market: {
            ...event.market,
            funding_rate: event.market.funding_rate ?? current.market.funding_rate,
            funding_time: event.market.funding_time ?? current.market.funding_time,
            funding_interval: event.market.funding_interval ?? current.market.funding_interval,
          },
          candles,
          system: {
            ...current.system,
            data_latency_ms: Math.max(receivedAt - Date.parse(event.market.timestamp), 0),
            market_quality: current.system.market_quality
              ? { ...current.system.market_quality, status: 'healthy', source: event.market.source, last_tick_at: event.market.timestamp }
              : current.system.market_quality,
          },
        }
      })
    }

    const connect = () => {
      if (stopped) return
      setStreamState(current => current === 'live' ? current : 'connecting')
      socket = openMarketStream(symbol, timeframe, applyMarket, state => {
        if (stopped) return
        if (state === 'connected') {
          reconnectDelay = 1_000
          return
        }
        if (state === 'error') setStreamState('offline')
        if (state === 'disconnected') {
          setStreamState('offline')
          reconnectTimer = window.setTimeout(connect, reconnectDelay)
          reconnectDelay = Math.min(reconnectDelay * 2, 30_000)
        }
      })
    }

    connect()
    const staleTimer = window.setInterval(() => {
      if (lastMarketEventAt.current && Date.now() - lastMarketEventAt.current > 15_000) setStreamState('stale')
    }, 1_000)
    const reconcileTimer = window.setInterval(() => {
      void Promise.all([
        getDashboard(),
        timeframe === settingsView.configuration.default_timeframe && symbol === settingsView.configuration.default_symbol
          ? Promise.resolve(null)
          : getMarketWindow(symbol, timeframe),
      ]).then(([result, window]) => {
        if (!stopped) {
          if (window) {
            result.market = window.market
            result.candles = window.candles
            result.system.market_quality = window.quality
          }
          setDashboard(result)
          setDashboardError(null)
        }
      }).catch(() => { if (!stopped) setStreamState('offline') })
    }, 60_000)
    return () => {
      stopped = true
      socket?.close()
      if (reconnectTimer) window.clearTimeout(reconnectTimer)
      window.clearInterval(staleTimer)
      window.clearInterval(reconcileTimer)
    }
  }, [selectedSymbol, selectedTimeframe, settingsView])

  const selectTimeframe = async (timeframe: string) => {
    if (!settingsView || timeframe === selectedTimeframe) return
    setChartBusy(true)
    try {
      const window = await getMarketWindow(
        selectedSymbol || settingsView.configuration.default_symbol,
        timeframe,
      )
      setDashboard(current => current ? {
        ...current,
        market: window.market,
        candles: window.candles,
        system: { ...current.system, market_quality: window.quality },
      } : current)
      setSelectedTimeframe(timeframe)
      setStreamState('connecting')
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '周期行情加载失败')
    } finally {
      setChartBusy(false)
    }
  }

  const selectSymbol = async (symbol: string) => {
    if (!settingsView || symbol === selectedSymbol) return
    setChartBusy(true)
    try {
      const window = await getMarketWindow(symbol, selectedTimeframe)
      setDashboard(current => current ? {
        ...current,
        market: window.market,
        candles: window.candles,
        system: { ...current.system, market_quality: window.quality },
      } : current)
      setSelectedSymbol(symbol)
      setStreamState('connecting')
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '交易品种行情加载失败')
    } finally {
      setChartBusy(false)
    }
  }

  const marketMove = useMemo(() => {
    if (!dashboard?.candles.length) return 0
    const start = Number(dashboard.candles[0].close)
    return (Number(dashboard.market.last) - start) / start * 100
  }, [dashboard])

  const act = async (action: () => Promise<void>, success: string) => {
    setBusy(true)
    try {
      await action()
      await refresh()
      setNotice(success)
      setTimeout(() => setNotice(null), 3200)
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '操作失败')
    } finally {
      setBusy(false)
    }
  }

  if (!settingsView) {
    return <div className="boot"><div className="boot-mark">C</div><span>正在读取本机配置…</span></div>
  }

  if (!dashboard) {
    return (
      <div className="app-shell">
        <nav className="side-nav" aria-label="主导航">
          <div className="brand" aria-label="COSMOS">C<span>Q</span></div>
          <div className="nav-stack">
            <button aria-label="重新连接" data-label="重新连接" onClick={() => void refresh()}><LayoutDashboard aria-hidden="true" /></button>
            <button aria-label="策略" className={section === 'strategy' ? 'active' : ''} data-label="策略" onClick={() => setSection('strategy')}><SlidersHorizontal aria-hidden="true" /></button>
            <button aria-label="交易 Agent" className={section === 'agent' ? 'active' : ''} data-label="交易 Agent" onClick={() => setSection('agent')}><Workflow aria-hidden="true" /></button>
            <button aria-label="AI 服务" className={section === 'ai' ? 'active' : ''} data-label="AI 服务" onClick={() => setSection('ai')}><BrainCircuit aria-hidden="true" /></button>
          </div>
          <button aria-label="设置" className={`nav-settings ${section === 'settings' ? 'active' : ''}`} data-label="设置" onClick={() => setSection('settings')}><Settings aria-hidden="true" /></button>
        </nav>
        <main className="workspace">
          <header className="topbar"><div className="product-title"><strong>COSMOS</strong><span>Quant Operations</span></div><div className="system-strip"><span><i className="offline" />真实行情离线</span><span className="mode">{settingsView.configuration.exchange_id.toUpperCase()}</span><span className="avatar" aria-label="当前操作者">OP</span></div></header>
          {dashboardError && <div className="market-error" role="alert"><div><span className="eyebrow">Market data unavailable</span><strong>没有使用演示数据</strong><p>{dashboardError}</p></div><button className="button ghost" onClick={() => void refresh()}>重新连接</button></div>}
          {section === 'agent' ? (
            <AgentWorkspace settings={settingsView} />
          ) : section === 'ai' ? (
            <AISettingsWorkspace />
          ) : section === 'strategy' ? (
            <StrategyStudio settings={settingsView} onGenerated={() => void refresh()} onOpenSettings={() => setSection('settings')} />
          ) : (
            <SettingsWorkspace view={settingsView} onSaved={view => {
              const nextSymbol = view.configuration.strategy.watchlist.includes(selectedSymbol) ? selectedSymbol : view.configuration.default_symbol
              const nextTimeframe = view.configuration.strategy.timeframes.includes(selectedTimeframe) ? selectedTimeframe : view.configuration.default_timeframe
              setSettingsView(view)
              setSelectedSymbol(nextSymbol)
              setSelectedTimeframe(nextTimeframe)
              void refresh(view, nextSymbol, nextTimeframe)
            }} />
          )}
        </main>
      </div>
    )
  }

  const plan = dashboard.plan
  const activate = () => act(async () => {
    if (!plan) throw new Error('当前没有可激活的策略计划')
    const next = await activateStrategy(plan)
    setDashboard(current => current ? { ...current, plan: next } : current)
    setConfirming(false)
  }, '策略已激活，正在等待计划入场区间')

  const execute = () => act(() => {
    if (!plan) throw new Error('当前没有可执行的策略计划')
    return executeStrategy(plan)
  }, `${settingsView.configuration.trading_mode === 'live' ? '真实' : settingsView.configuration.trading_mode === 'sandbox' ? '测试网' : '纸面'}订单已提交，保护单与幂等键已写入账本`)
  const toggleTradingHalt = () => {
    const state = dashboard.system.risk_state
    const confirmed = window.confirm(state.halted
      ? '确认恢复所有新开仓权限？恢复短语将由系统严格校验。'
      : '确认立即停止所有新开仓？已有保护单不会被撤销。')
    if (!confirmed) return
    void act(
      () => state.halted ? resumeTrading() : haltTrading('用户在交易台触发紧急停机'),
      state.halted ? '新开仓权限已恢复' : '全局紧急停机已生效',
    )
  }

  return (
    <div className="app-shell">
      <nav className="side-nav" aria-label="主导航">
        <button className="brand" aria-label="COSMOS 首页" onClick={() => setSection('overview')}>C<span>Q</span></button>
        <div className="nav-stack">
          <button aria-label="交易台" className={section === 'overview' ? 'active' : ''} onClick={() => setSection('overview')} data-label="交易台"><LayoutDashboard aria-hidden="true" /></button>
          <button aria-label="策略" className={section === 'strategy' ? 'active' : ''} onClick={() => setSection('strategy')} data-label="策略"><SlidersHorizontal aria-hidden="true" /></button>
          <button aria-label="持仓与订单" className={section === 'positions' || section === 'orders' ? 'active' : ''} onClick={() => setSection('positions')} data-label="持仓与订单"><ListOrdered aria-hidden="true" /></button>
          <button aria-label="风控" className={section === 'risk' ? 'active' : ''} onClick={() => setSection('risk')} data-label="风控"><ShieldCheck aria-hidden="true" /></button>
          <button aria-label="研究" className={section === 'lab' ? 'active' : ''} onClick={() => setSection('lab')} data-label="研究"><FlaskConical aria-hidden="true" /></button>
          <button aria-label="交易 Agent" className={section === 'agent' ? 'active' : ''} onClick={() => setSection('agent')} data-label="交易 Agent"><Workflow aria-hidden="true" /></button>
          <button aria-label="AI 服务" className={section === 'ai' ? 'active' : ''} onClick={() => setSection('ai')} data-label="AI 服务"><BrainCircuit aria-hidden="true" /></button>
        </div>
        <button aria-label="设置" className={`nav-settings ${section === 'settings' ? 'active' : ''}`} onClick={() => setSection('settings')} data-label="设置"><Settings aria-hidden="true" /></button>
      </nav>

      <main className="workspace">
        <header className="topbar">
          <div className="product-title">
            <strong>COSMOS</strong><span>Quant Operations</span>
          </div>
          <div className="system-strip">
            <span><i className={streamState === 'live' ? 'online live-pulse' : 'offline'} /> {streamState === 'live' ? '实时行情' : streamState === 'connecting' ? '行情连接中' : streamState === 'stale' ? '行情已延迟' : '行情离线'}</span>
            <span>{dashboard.system.data_latency_ms} ms</span>
            <span className="mode">{dashboard.system.exchange.toUpperCase()} · {modeLabel(dashboard.system.mode)}</span>
            <span className="locked">{dashboard.system.live_locked ? '实盘已锁定' : '实盘已解锁'}</span>
            <span className="avatar" aria-label="当前操作者">OP</span>
          </div>
        </header>

        {section === 'agent' ? (
          <AgentWorkspace settings={settingsView} />
        ) : section === 'ai' ? (
          <AISettingsWorkspace />
        ) : section === 'settings' ? (
          <SettingsWorkspace view={settingsView} onSaved={view => {
            const nextSymbol = view.configuration.strategy.watchlist.includes(selectedSymbol) ? selectedSymbol : view.configuration.default_symbol
            const nextTimeframe = view.configuration.strategy.timeframes.includes(selectedTimeframe) ? selectedTimeframe : view.configuration.default_timeframe
            setSettingsView(view)
            setSelectedSymbol(nextSymbol)
            setSelectedTimeframe(nextTimeframe)
            void refresh(view, nextSymbol, nextTimeframe)
          }} />
        ) : section === 'strategy' ? (
          <StrategyStudio
            settings={settingsView}
            initialPlan={dashboard.plan ?? undefined}
            onGenerated={next => setDashboard(current => current ? { ...current, plan: next } : current)}
            onOpenSettings={() => setSection('settings')}
          />
        ) : (<>
        {dashboard.system.market_quality?.is_cached && <div className="offline-banner" role="status">当前显示经过校验的真实行情缓存：{dashboard.system.market_quality.message}。风险引擎会因行情陈旧拒绝新开仓。</div>}
        <section className="market-header">
          <div className="instrument">
            <span className="asset-mark" aria-hidden="true"><Coins /></span>
            <div className="instrument-control"><select className="symbol-select" aria-label="交易品种" value={dashboard.market.symbol} onChange={event => void selectSymbol(event.target.value)} disabled={chartBusy}>{settingsView.configuration.strategy.watchlist.map(symbol => <option key={symbol} value={symbol}>{symbol}</option>)}</select><small>{settingsView.configuration.market_type === 'spot' ? '现货' : 'USDT 永续'} · {selectedTimeframe} · 真实行情</small></div>
          </div>
          <div className="ticker-price">
            <strong key={dashboard.market.last} className="live-price">{fmt(dashboard.market.last)}</strong>
            <span className={marketMove >= 0 ? 'positive' : 'negative'}>{pct(marketMove)} <small>显示区间</small></span>
          </div>
          <div className="portfolio-kpis">
            <div><span>组合净值</span><strong>{fmt(dashboard.portfolio.equity)} <small>USDT</small></strong></div>
            <div><span>{settingsView.configuration.market_type === 'swap' ? '可用保证金' : '可用资金'}</span><strong>{fmt(dashboard.portfolio.available_margin ?? dashboard.portfolio.cash)}</strong></div>
            <div><span>日内盈亏</span><strong className={Number(dashboard.portfolio.equity) >= Number(dashboard.portfolio.daily_start_equity) ? 'positive' : 'negative'}>{fmt(Number(dashboard.portfolio.equity) - Number(dashboard.portfolio.daily_start_equity))}</strong></div>
            <div><span>总敞口</span><strong>{fmt(dashboard.portfolio.gross_exposure)} <small>{(Number(dashboard.portfolio.gross_exposure) / Math.max(Number(dashboard.portfolio.equity), 1) * 100).toFixed(1)}%</small></strong></div>
          </div>
        </section>

        <div className="main-grid">
          <section className="chart-panel">
            <ProfessionalChart
              candles={dashboard.candles}
              plan={plan}
              exchange={dashboard.system.exchange}
              symbol={dashboard.market.symbol}
              timeframe={selectedTimeframe}
              timeframes={[...CHART_TIMEFRAMES]}
              busy={chartBusy}
              showPlan={Boolean(plan && plan.symbol === dashboard.market.symbol)}
              onTimeframeChange={timeframe => void selectTimeframe(timeframe)}
            />
            <div className="quote-row">
              <span>买一 <strong>{fmt(dashboard.market.bid)}</strong></span>
              <span>卖一 <strong>{fmt(dashboard.market.ask)}</strong></span>
              <span>价差 <strong>{dashboard.market.spread_bps} bps</strong></span>
              {settingsView.configuration.market_type === 'swap' && <span>资金费率 <strong className={Number(dashboard.market.funding_rate ?? 0) > 0 ? 'negative' : 'positive'}>{dashboard.market.funding_rate == null ? '获取中' : `${(Number(dashboard.market.funding_rate) * 100).toFixed(4)}%`}</strong></span>}
              <span>数据源 <strong>{dashboard.market.source}</strong></span>
              <span>更新 <strong>{new Date(dashboard.market.timestamp).toLocaleTimeString('zh-CN')}</strong></span>
            </div>
          </section>

          <StrategyRail
            plan={plan}
            signal={dashboard.signal}
            mode={settingsView.configuration.trading_mode}
            onGenerate={() => setSection('strategy')}
            onActivate={() => { setPlanReviewed(false); setConfirming(true) }}
            onExecute={execute}
            busy={busy}
            disabled={dashboard.system.risk_state.halted || !dashboard.risk.approved}
          />
        </div>

        <section className="lower-deck">
          <div className="deck-tabs" role="tablist" aria-label="交易台明细">
            <button type="button" role="tab" aria-selected={section === 'overview'} className={section === 'overview' ? 'active' : ''} onClick={() => setSection('overview')}>运行概览</button>
            <button type="button" role="tab" aria-selected={section === 'risk'} className={section === 'risk' ? 'active' : ''} onClick={() => setSection('risk')}>风险检查 <span>{dashboard.risk.checks.filter(item => item.passed).length}/{dashboard.risk.checks.length}</span></button>
            <button type="button" role="tab" aria-selected={section === 'positions'} className={section === 'positions' ? 'active' : ''} onClick={() => setSection('positions')}>当前持仓 <span>{dashboard.portfolio.open_positions.length}</span></button>
            <button type="button" role="tab" aria-selected={section === 'orders'} className={section === 'orders' ? 'active' : ''} onClick={() => setSection('orders')}>订单与成交 <span>{dashboard.orders.length}</span></button>
            <button type="button" role="tab" aria-selected={section === 'lab'} className={section === 'lab' ? 'active' : ''} onClick={() => setSection('lab')}>回测实验</button>
          </div>

          {section === 'overview' && (
            <div className="overview-deck">
              <div className="backtest-summary">
                <div className="summary-title"><span>最近回测</span><small>策略周期 {plan?.timeframe ?? selectedTimeframe} · 真实历史 K 线 · 含手续费与滑点 · 历史资金费不可用时明确披露</small></div>
                <div><span>收益率</span><strong className={Number(dashboard.backtest.return_percent) >= 0 ? 'positive' : 'negative'}>{pct(dashboard.backtest.return_percent)}</strong></div>
                <div><span>最大回撤</span><strong>{dashboard.backtest.max_drawdown_percent}%</strong></div>
                <div><span>Sharpe</span><strong>{dashboard.backtest.sharpe}</strong></div>
                <div><span>胜率</span><strong>{dashboard.backtest.win_rate_percent}%</strong></div>
                <div><span>Profit Factor</span><strong>{dashboard.backtest.profit_factor}</strong></div>
                <EquitySparkline values={dashboard.backtest.equity_curve} />
              </div>
              <div className="audit-stream">
                <div className="summary-title"><span>实时审计</span><small>策略、风控与执行事件</small></div>
                {dashboard.audit.slice(0, 4).map(event => (
                  <div className="audit-item" key={event.id}><i className={event.severity} /><time>{new Date(event.timestamp).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</time><span>{event.message}</span></div>
                ))}
              </div>
            </div>
          )}

          {section === 'positions' && (
            <div className="positions-deck data-table" role="tabpanel">
              <table className="positions-table">
                <caption className="sr-only">当前账户持仓、盈亏与保护价格</caption>
                <thead><tr><th scope="col">品种</th><th scope="col">方向</th><th scope="col">数量</th><th scope="col">杠杆</th><th scope="col">平均价</th><th scope="col">标记价</th><th scope="col">未实现盈亏</th><th scope="col">名义价值</th><th scope="col">清算 / 保护</th></tr></thead>
                <tbody>{dashboard.portfolio.open_positions.length ? dashboard.portfolio.open_positions.map(position => {
                  const notional = Number(position.quantity) * Number(position.mark_price)
                  return <tr key={`${position.symbol}-${position.side}`}><td>{position.symbol}</td><td className={position.side === 'long' ? 'positive' : 'negative'}>{position.side === 'long' ? '多头' : '空头'}</td><td>{position.quantity}</td><td>{position.leverage ? `${position.leverage}x` : '—'}</td><td>{fmt(position.average_entry)}</td><td>{fmt(position.mark_price)}</td><td className={Number(position.unrealized_pnl) >= 0 ? 'positive' : 'negative'}>{fmt(position.unrealized_pnl)}</td><td>{fmt(notional)}</td><td className="protection-cell">清算 {position.liquidation_price ? fmt(position.liquidation_price) : '—'} · 止损 {position.stop_loss ? fmt(position.stop_loss) : '—'} · 止盈 {position.take_profit ? fmt(position.take_profit) : '—'}</td></tr>
                }) : <tr><td className="empty-row" colSpan={9}>当前没有持仓。策略成交后，杠杆、标记价格、清算价、未实现盈亏和保护价格会在这里实时显示。</td></tr>}</tbody>
              </table>
            </div>
          )}

          {section === 'risk' && (
            <div className="risk-deck">
              <div className="risk-score"><strong>{dashboard.system.risk_state.halted ? 'HALT' : dashboard.risk.approved ? 'PASS' : 'HOLD'}</strong><span>{dashboard.system.risk_state.halted ? dashboard.system.risk_state.reason : '预交易风险结论'}</span><button className={`kill-switch ${dashboard.system.risk_state.halted ? 'resume' : ''}`} onClick={toggleTradingHalt} disabled={busy}>{dashboard.system.risk_state.halted ? '恢复新开仓' : '紧急停止开仓'}</button></div>
              <div className="risk-checks">
                {dashboard.risk.checks.map(check => <div key={check.code}><i className={check.passed ? 'pass' : 'fail'}>{check.passed ? <Check aria-hidden="true" /> : <X aria-hidden="true" />}</i><span className="risk-check-copy"><strong>{check.code.replaceAll('_', ' ')} · {check.blocking === false ? '提示' : '硬门禁'}</strong><small>{check.message}</small></span></div>)}
                {dashboard.system.reconciliation && <div><i className={dashboard.system.reconciliation.uncertain_order_ids.length || dashboard.system.reconciliation.unknown_position_symbols.length || dashboard.system.reconciliation.unprotected_order_ids.length ? 'fail' : 'pass'}><RefreshCw aria-hidden="true" /></i><span>订单对账 {dashboard.system.reconciliation.confirmed} 已确认 · {dashboard.system.reconciliation.uncertain_order_ids.length} 不确定 · {dashboard.system.reconciliation.unknown_position_symbols.length} 外部仓位 · {dashboard.system.reconciliation.unprotected_order_ids.length} 缺保护</span></div>}
                <div><i className={dashboard.system.private_order_stream?.authenticated || settingsView.configuration.trading_mode === 'paper' || settingsView.configuration.exchange_id === 'binance' ? 'pass' : 'fail'}><RadioTower aria-hidden="true" /></i><span>{settingsView.configuration.trading_mode === 'paper' ? '纸面账户本地事件流' : dashboard.system.private_order_stream ? `私有订单流 ${dashboard.system.private_order_stream.authenticated ? '已认证' : '重连中'}` : 'Binance 使用 10 秒 REST 对账降级'}</span></div>
              </div>
            </div>
          )}

          {section === 'orders' && (
            <div className="orders-deck data-table" role="tabpanel">
              <table className="orders-table">
                <caption className="sr-only">最近订单、成交状态与可用操作</caption>
                <thead><tr><th scope="col">时间</th><th scope="col">品种</th><th scope="col">方向</th><th scope="col">类型</th><th scope="col">成交 / 委托</th><th scope="col">价格</th><th scope="col">环境</th><th scope="col">状态</th><th scope="col">操作</th></tr></thead>
                <tbody>{dashboard.orders.length ? dashboard.orders.map(order => (
                  <tr key={order.id}><td>{new Date(order.created_at).toLocaleTimeString('zh-CN')}</td><td>{order.symbol}</td><td className={order.side === 'buy' ? 'positive' : 'negative'}>{order.side === 'buy' ? '买入' : '卖出'}</td><td>{order.order_type === 'market' ? '市价' : '限价'}</td><td>{order.filled_quantity} / {order.quantity}</td><td>{order.average_fill_price ? fmt(order.average_fill_price) : order.limit_price ? fmt(order.limit_price) : '市价'}</td><td>{modeLabel(order.mode)}</td><td>{({ pending: '待提交', open: '挂单中', partially_filled: '部分成交', filled: '已成交', canceled: '已撤销', rejected: '已拒绝' } as Record<string, string>)[order.status] ?? order.status}</td><td>{['pending', 'open', 'partially_filled'].includes(order.status) ? <button type="button" className="cancel-order" disabled={busy} onClick={() => { if (window.confirm(`确认撤销 ${order.symbol} 的未成交委托？已成交部分不会反向处理。`)) void act(() => cancelOrder(order.id), '订单已撤销并写入审计日志') }}>撤单</button> : '—'}</td></tr>
                )) : <tr><td className="empty-row" colSpan={9}>还没有订单。激活策略后，可提交受风控约束、同时带止盈止损保护的限价单。</td></tr>}</tbody>
              </table>
            </div>
          )}

          {section === 'lab' && (
            <div className="lab-deck">
              <div><span>样本内交易</span><strong>{dashboard.backtest.total_trades}</strong></div>
              <div><span>最终净值</span><strong>{fmt(dashboard.backtest.final_equity)}</strong></div>
              <div><span>净利润</span><strong className={Number(dashboard.backtest.net_profit) >= 0 ? 'positive' : 'negative'}>{fmt(dashboard.backtest.net_profit)}</strong></div>
              <div><span>成本压力收益</span><strong className={Number(dashboard.backtest.stress_return_percent ?? 0) >= 0 ? 'positive' : 'negative'}>{dashboard.backtest.stress_return_percent ?? '—'}%</strong></div>
              <div><span>敞口 / 样本</span><strong>{dashboard.backtest.exposure_percent}% / {dashboard.backtest.sample_bars}</strong></div>
              <div><span>稳健性验收</span><strong className={dashboard.backtest.robustness_passed ? 'positive' : 'negative'}>{dashboard.backtest.robustness_passed ? 'PASS' : 'HOLD'}</strong></div>
              <p>回测采用逐根 Walk-forward、下一根 K 线才能成交和同根止损优先；手续费 1.5 倍、滑点 2 倍压力场景必须为正。{dashboard.backtest.validation_reasons.join('；')}。历史资金费不可用时不会拿当前费率回填历史。</p>
            </div>
          )}
        </section>
        </>)}
      </main>

      {confirming && plan && (
        <div className="modal-backdrop" role="presentation" onMouseDown={() => setConfirming(false)}>
          <div ref={confirmationDialogRef} className="confirm-modal" role="dialog" aria-modal="true" aria-labelledby="confirm-title" aria-describedby="confirm-description" onMouseDown={event => event.stopPropagation()}>
            <span className="eyebrow">策略激活检查</span>
            <h2 id="confirm-title">确认让系统等待开仓条件</h2>
            <p id="confirm-description">{plan.human_summary}</p>
            <div className="confirm-risk"><span>最大计划亏损</span><strong>{fmt(plan.sizing.risk_amount)} USDT</strong><span>占组合权益 {(Number(plan.sizing.risk_fraction) * 100).toFixed(2)}%{plan.market_type === 'swap' ? ` · ${plan.leverage}x ${plan.margin_mode === 'cross' ? '全仓' : '逐仓'} · 预计保证金 ${fmt(Number(plan.sizing.notional) / Number(plan.leverage))} USDT` : ''}</span></div>
            <label><input ref={reviewInputRef} type="checkbox" checked={planReviewed} onChange={event => setPlanReviewed(event.target.checked)} /> 我已核对开仓、止盈、止损和失效条件</label>
            <div className="modal-actions"><button className="button ghost" onClick={() => setConfirming(false)}>返回修改</button><button className="button primary" onClick={activate} disabled={busy || !planReviewed}>确认激活{settingsView.configuration.trading_mode === 'live' ? '实盘' : settingsView.configuration.trading_mode === 'sandbox' ? '测试网' : '纸面'}策略</button></div>
          </div>
        </div>
      )}

      {notice && <div className="toast" role="status" aria-live="polite">{notice}</div>}
    </div>
  )
}

export default App
