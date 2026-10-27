import { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Activity, BookOpen, BrainCircuit, Coins, FlaskConical, LayoutDashboard, ListOrdered, PanelLeftClose, PanelLeftOpen, ScrollText, Settings, ShieldCheck, SlidersHorizontal } from 'lucide-react'
import { cancelOrder, getDashboard, getMarketWindow, haltTrading, openMarketStream, resumeTrading } from './api'
import BootstrapGate from './BootstrapGate'
import { PortfolioWorkspace, RiskWorkspace } from './AccountWorkspaces'
import ProfessionalChart from './ProfessionalChart'
import TradingIntelligenceWorkspace from './TradingIntelligenceWorkspace'
import { CHART_TIMEFRAMES } from './timeframes'
import type { ConfigurationView, DashboardData, MarketStreamEvent } from './types'

const AISettingsWorkspace = lazy(() => import('./AISettingsWorkspace'))
const AuditWorkspace = lazy(() => import('./AuditWorkspace'))
const ResearchAgentWorkspace = lazy(() => import('./ResearchAgentWorkspace'))
const SettingsWorkspace = lazy(() => import('./SettingsWorkspace'))
const StrategyEditorWorkspace = lazy(() => import('./StrategyEditorWorkspace'))
const StrategyLibraryWorkspace = lazy(() => import('./StrategyLibraryWorkspace'))

const money = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const CHART_CANDLE_LIMIT = 300
type AppSection = 'overview' | 'portfolio' | 'risk' | 'researchAgent' | 'intelligence' | 'library' | 'editor' | 'logs' | 'ai' | 'settings'

function initialSection(): AppSection {
  const requested = new URLSearchParams(window.location.search).get('section')
  const aliases: Record<string, AppSection> = {
    positions: 'portfolio', orders: 'portfolio', lab: 'researchAgent', strategy: 'editor',
    agent: 'intelligence', reviewAgent: 'researchAgent', overview: 'intelligence',
  }
  const supported: AppSection[] = ['portfolio', 'risk', 'researchAgent', 'intelligence', 'library', 'editor', 'logs', 'ai', 'settings']
  if (requested && aliases[requested]) return aliases[requested]
  return supported.includes(requested as AppSection) ? requested as AppSection : 'intelligence'
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

const sectionTitles: Record<AppSection, string> = {
  overview: '交易台', portfolio: '持仓与订单', risk: '风险控制',
  researchAgent: '离线研究', intelligence: '交易智能', library: '策略库',
  editor: '策略编辑器', logs: '运行日志', ai: 'AI 服务', settings: '系统设置',
}

function AppNavigation({
  section,
  collapsed,
  onNavigate,
  onToggle,
}: {
  section: AppSection
  collapsed: boolean
  onNavigate: (section: AppSection) => void
  onToggle: () => void
}) {
  const item = (key: AppSection, label: string, icon: ReactNode) => <button type="button" className={section === key ? 'active' : ''} aria-label={label} title={collapsed ? label : undefined} onClick={() => onNavigate(key)}>{icon}<span>{label}</span></button>
  return <nav className={`primary-navigation ${collapsed ? 'collapsed' : ''}`} aria-label="主导航">
    <button className="navigation-brand" onClick={() => onNavigate('intelligence')}><span>CQ</span><strong>COSMOS</strong><small>QUANT OPS</small></button>
    <div className="navigation-groups">
      <section><label>交易</label>{item('intelligence', '交易工作台', <LayoutDashboard />)}{item('portfolio', '持仓与订单', <ListOrdered />)}{item('risk', '风险控制', <ShieldCheck />)}</section>
      <section><label>智能系统</label>{item('researchAgent', '离线研究', <FlaskConical />)}</section>
      <section><label>策略</label>{item('library', '策略库', <BookOpen />)}{item('editor', '策略编辑器', <SlidersHorizontal />)}</section>
      <section><label>系统</label>{item('logs', '运行日志', <ScrollText />)}{item('ai', 'AI 服务', <BrainCircuit />)}{item('settings', '设置', <Settings />)}</section>
    </div>
    <button className="navigation-collapse" aria-label={collapsed ? '展开导航' : '收起导航'} onClick={onToggle}>{collapsed ? <PanelLeftOpen /> : <PanelLeftClose />}<span>{collapsed ? '展开导航' : '收起导航'}</span></button>
  </nav>
}

function GlobalTopbar({
  section,
  settings,
  dashboard,
  streamState,
  density,
  onOpenAgent,
  onToggleDensity,
}: {
  section: AppSection
  settings: ConfigurationView
  dashboard?: DashboardData | null
  streamState: 'connecting' | 'live' | 'stale' | 'offline'
  density: 'comfortable' | 'compact'
  onOpenAgent: () => void
  onToggleDensity: () => void
}) {
  const marketStreamOnDemand = ['researchAgent', 'intelligence'].includes(section)
  return <header className="global-topbar">
    <div><strong>{sectionTitles[section]}</strong><span>COSMOS QUANT OPERATIONS</span></div>
    <div className="global-runtime-strip">
      <span><i className={marketStreamOnDemand || streamState === 'live' ? `online${streamState === 'live' ? ' live-pulse' : ''}` : 'offline'} />{marketStreamOnDemand ? '行情按需' : streamState === 'live' ? '实时行情' : streamState === 'connecting' ? '行情连接中' : streamState === 'stale' ? '行情延迟' : '行情离线'}</span>
      {dashboard && <span>{dashboard.system.data_latency_ms} ms</span>}
      <span className="mode">{settings.configuration.exchange_id.toUpperCase()} · {modeLabel(settings.configuration.trading_mode)}</span>
      {dashboard && <span className={dashboard.system.risk_state.halted ? 'risk-halted' : 'risk-ready'}>{dashboard.system.risk_state.halted ? '风险停机' : '风险正常'}</span>}
      <button className="global-agent-state" onClick={onOpenAgent}><Activity /><span>Trading Intelligence v2</span><small>事件驱动</small></button>
      <button className="density-toggle" onClick={onToggleDensity}>{density === 'comfortable' ? '舒适' : '紧凑'}</button>
      <span className="avatar" aria-label="当前操作者">OP</span>
    </div>
  </header>
}

function TradingIntelligenceRail({ onOpenIntelligence, onGenerate, busy }: {
  onOpenIntelligence: () => void
  onGenerate: () => void
  busy: boolean
}) {
  return (
    <aside className="strategy-rail no-plan">
      <div className="rail-heading"><div><span className="eyebrow">Trading Intelligence v2</span><h2>事件驱动决策链</h2></div><span className="state-dot paused">隔离运行</span></div>
      <div className="live-signal flat"><span>实时行情仅更新 MarketState</span><strong>普通波动不调用 LLM</strong><p>只有有效 Thesis、WakeCondition、重大官方事件或有界重试才能唤醒决策。</p></div>
      <p className="plan-summary">实时工作台不再生成旧 StrategyPlan，也不会在页面加载时自动扫描或回测。当前 Thesis、TradePlan 与模拟盘状态统一在 v2 决策链查看。</p>
      <div className="rail-actions"><button className="button primary" onClick={onOpenIntelligence} disabled={busy}>查看 v2 决策链</button><button className="button" onClick={onGenerate} disabled={busy}>打开离线研究</button></div>
    </aside>
  )
}

const AGENT_SECTIONS = new Set<AppSection>(['researchAgent', 'intelligence'])

function PersistentAgentWorkspaces({ section, settings, visited }: {
  section: AppSection
  settings: ConfigurationView
  visited: ReadonlySet<AppSection>
}) {
  return <>
    {visited.has('researchAgent') && <div className="persistent-agent-workspace" hidden={section !== 'researchAgent'} aria-hidden={section !== 'researchAgent'}>
      <ResearchAgentWorkspace settings={settings} active={section === 'researchAgent'} />
    </div>}
    {visited.has('intelligence') && <div className="persistent-agent-workspace" hidden={section !== 'intelligence'} aria-hidden={section !== 'intelligence'}>
      <TradingIntelligenceWorkspace active={section === 'intelligence'} />
    </div>}
  </>
}

function DashboardFallbackWorkspace({
  section,
  error,
  onRetry,
}: {
  section: 'overview' | 'portfolio' | 'risk' | 'editor'
  error: string | null
  onRetry: () => void
}) {
  const portfolio = section === 'portfolio'
  const risk = section === 'risk'
  const title = portfolio ? '持仓、订单与收益' : risk ? '风险控制' : '交易工作台'
  const description = portfolio
    ? '正在从 OKX Demo 同步账户净值、当前持仓和活动订单。'
    : risk
      ? '正在同步全局风险闸门与订单对账状态。'
      : '正在同步行情、账户与交易状态。'

  return <section className={`ops-page dashboard-fallback-page ${portfolio ? 'portfolio-fallback' : ''}`}>
    <header className="ops-page-header">
      <div>
        <span className="ops-kicker">{portfolio ? '账户执行与历史归因' : risk ? '确定性风险引擎' : '交易运行时'}</span>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
    </header>
    <div className={`dashboard-fallback-state ${error ? 'failed' : 'loading'}`} role={error ? 'alert' : 'status'}>
      <i aria-hidden="true" />
      <div>
        <strong>{error ? `${portfolio ? '账户' : '运行时'}数据暂不可用` : '正在建立数据连接'}</strong>
        <span>{error ?? '首次同步通常只需要几秒钟，页面会自动重试。'}</span>
      </div>
      {error && <button type="button" className="button ghost" onClick={onRetry}>立即重试</button>}
    </div>
    {portfolio && <>
      <div className="account-summary-strip dashboard-skeleton" aria-hidden="true">
        {['账户净值', '可用资金', '总敞口', '当前持仓', '活动订单'].map(label => <div key={label}><span>{label}</span><strong>—</strong></div>)}
      </div>
      <section className="account-table-section dashboard-skeleton-table" aria-label="持仓数据加载状态">
        <div className="panel-title"><span>当前持仓</span><b>—</b></div>
        <div className="account-data-table positions">
          <div className="table-head"><span>品种</span><span>方向</span><span>数量</span><span>杠杆</span><span>平均价</span><span>标记价</span><span>未实现盈亏</span><span>清算 / 保护</span></div>
          <p className="ops-empty">{error ? '账户连接恢复后将在这里显示真实持仓。' : '正在读取真实持仓…'}</p>
        </div>
      </section>
    </>}
  </section>
}

function TradingApp() {
  const [dashboard, setDashboard] = useState<DashboardData | null>(null)
  const [settingsView, setSettingsView] = useState<ConfigurationView | null>(null)
  const [dashboardError, setDashboardError] = useState<string | null>(null)
  const [section, setSection] = useState<AppSection>(initialSection)
  const [visitedAgentSections, setVisitedAgentSections] = useState<Set<AppSection>>(
    () => AGENT_SECTIONS.has(section) ? new Set([section]) : new Set(),
  )
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [selectedTimeframe, setSelectedTimeframe] = useState('1h')
  const [selectedSymbol, setSelectedSymbol] = useState('')
  const [chartBusy, setChartBusy] = useState(false)
  const [streamState, setStreamState] = useState<'connecting' | 'live' | 'stale' | 'offline'>('connecting')
  const [navCollapsed, setNavCollapsed] = useState(() => localStorage.getItem('cosmos-nav-collapsed') === 'true' || window.innerWidth < 1440)
  const [density, setDensity] = useState<'comfortable' | 'compact'>(() => localStorage.getItem('cosmos-density') === 'compact' ? 'compact' : 'comfortable')
  const requiresDashboard = ['overview', 'portfolio', 'risk', 'editor'].includes(section)
  const lastMarketEventAt = useRef(0)

  const navigate = (next: AppSection, replace = false) => {
    setSection(next)
    if (window.innerWidth <= 1440) setNavCollapsed(true)
    if (AGENT_SECTIONS.has(next)) {
      setVisitedAgentSections(current => current.has(next) ? current : new Set([...current, next]))
    }
    const url = new URL(window.location.href)
    url.searchParams.set('section', next)
    window.history[replace ? 'replaceState' : 'pushState']({}, '', url)
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  useEffect(() => {
    const handlePopState = () => {
      const next = initialSection()
      setSection(next)
      if (AGENT_SECTIONS.has(next)) {
        setVisitedAgentSections(current => current.has(next) ? current : new Set([...current, next]))
      }
    }
    window.addEventListener('popstate', handlePopState)
    return () => window.removeEventListener('popstate', handlePopState)
  }, [])

  useEffect(() => {
    document.documentElement.dataset.density = density
    localStorage.setItem('cosmos-density', density)
  }, [density])

  useEffect(() => {
    localStorage.setItem('cosmos-nav-collapsed', String(navCollapsed))
  }, [navCollapsed])


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
    if (!requiresDashboard || dashboard) return
    let cancelled = false
    const load = () => void getDashboard()
      .then(result => { if (!cancelled) { setDashboard(result); setDashboardError(null) } })
      .catch(error => { if (!cancelled) setDashboardError(error instanceof Error ? error.message : '运行时数据连接失败') })
    load()
    const retryTimer = window.setInterval(load, 20_000)
    return () => {
      cancelled = true
      window.clearInterval(retryTimer)
    }
  }, [dashboard, requiresDashboard])

  useEffect(() => {
    if (!settingsView || !requiresDashboard) return
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
  }, [requiresDashboard, selectedSymbol, selectedTimeframe, settingsView])

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
    return <BootstrapGate onReady={view => {
      setSettingsView(view)
      setSelectedTimeframe(view.configuration.default_timeframe)
      setSelectedSymbol(view.configuration.default_symbol)
    }} />
  }

  if (!dashboard) {
    return (
      <div className="app-shell redesigned-shell">
        <AppNavigation section={section} collapsed={navCollapsed} onNavigate={navigate} onToggle={() => setNavCollapsed(current => !current)} />
        <main className={`workspace redesigned-workspace ${navCollapsed ? 'nav-collapsed' : ''}`}>
          <GlobalTopbar section={section} settings={settingsView} streamState="offline" density={density} onOpenAgent={() => navigate('intelligence')} onToggleDensity={() => setDensity(current => current === 'comfortable' ? 'compact' : 'comfortable')} />
          <PersistentAgentWorkspaces section={section} settings={settingsView} visited={visitedAgentSections} />
          {!AGENT_SECTIONS.has(section) && (section === 'library' ? <StrategyLibraryWorkspace />
                : section === 'editor' ? <StrategyEditorWorkspace settings={settingsView} onGenerated={() => void refresh()} onOpenSettings={() => navigate('settings')} />
                  : section === 'logs' ? <AuditWorkspace />
                    : section === 'ai' ? <AISettingsWorkspace />
                      : section === 'settings' ? <SettingsWorkspace view={settingsView} onSaved={view => {
                        const nextSymbol = view.configuration.strategy.watchlist.includes(selectedSymbol) ? selectedSymbol : view.configuration.default_symbol
                        const nextTimeframe = view.configuration.strategy.timeframes.includes(selectedTimeframe) ? selectedTimeframe : view.configuration.default_timeframe
                        setSettingsView(view); setSelectedSymbol(nextSymbol); setSelectedTimeframe(nextTimeframe)
                        void refresh(view, nextSymbol, nextTimeframe)
                      }} />
                        : <DashboardFallbackWorkspace section={section as 'overview' | 'portfolio' | 'risk' | 'editor'} error={dashboardError} onRetry={() => void refresh()} />)}
        </main>
      </div>
    )
  }

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
    <div className="app-shell redesigned-shell">
      <AppNavigation section={section} collapsed={navCollapsed} onNavigate={navigate} onToggle={() => setNavCollapsed(current => !current)} />

      <main className={`workspace redesigned-workspace ${navCollapsed ? 'nav-collapsed' : ''}`}>
        <GlobalTopbar section={section} settings={settingsView} dashboard={dashboard} streamState={streamState} density={density} onOpenAgent={() => navigate('intelligence')} onToggleDensity={() => setDensity(current => current === 'comfortable' ? 'compact' : 'comfortable')} />

        <PersistentAgentWorkspaces section={section} settings={settingsView} visited={visitedAgentSections} />
        {!AGENT_SECTIONS.has(section) && (section === 'library' ? (
          <StrategyLibraryWorkspace />
        ) : section === 'logs' ? (
          <AuditWorkspace />
        ) : section === 'portfolio' ? (
          <PortfolioWorkspace dashboard={dashboard} busy={busy} onCancelOrder={(orderId, symbol) => {
            if (window.confirm(`确认撤销 ${symbol} 的未成交委托？已成交部分不会反向处理。`)) void act(() => cancelOrder(orderId), '订单已撤销并写入审计日志')
          }} />
        ) : section === 'risk' ? (
          <RiskWorkspace dashboard={dashboard} busy={busy} onToggleHalt={toggleTradingHalt} />
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
        ) : section === 'editor' ? (
          <StrategyEditorWorkspace
            settings={settingsView}
            onGenerated={() => undefined}
            onOpenSettings={() => navigate('settings')}
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
              plan={null}
              exchange={dashboard.system.exchange}
              symbol={dashboard.market.symbol}
              timeframe={selectedTimeframe}
              timeframes={[...CHART_TIMEFRAMES]}
              busy={chartBusy}
              showPlan={false}
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

          <TradingIntelligenceRail
            onOpenIntelligence={() => navigate('intelligence')}
            onGenerate={() => navigate('editor')}
            busy={busy}
          />
        </div>

        <section className="trade-position-summary" aria-label="当前持仓状态">
          <header className="summary-title">
            <div><span>当前持仓</span><small>{dashboard.portfolio.open_positions.length > 0 ? `${dashboard.portfolio.open_positions.length} 个持仓正在由风险系统持续监控` : '当前没有未平仓头寸'}</small></div>
            <button onClick={() => navigate('portfolio')}>管理持仓与订单</button>
          </header>
          {dashboard.portfolio.open_positions.length > 0 ? <div className="trade-position-list">
            <div className="trade-position-head"><span>品种 / 方向</span><span>数量</span><span>开仓均价</span><span>标记价格</span><span>未实现盈亏</span><span>保护状态</span></div>
            {dashboard.portfolio.open_positions.slice(0, 6).map(position => <article key={`${position.symbol}-${position.side}`}>
              <div><strong>{position.symbol}</strong><b className={position.side}>{position.side === 'long' ? '多仓' : '空仓'}{position.leverage ? ` · ${position.leverage}x` : ''}</b></div>
              <span>{position.quantity}</span>
              <span>{fmt(position.average_entry)}</span>
              <span>{fmt(position.mark_price)}</span>
              <strong className={Number(position.unrealized_pnl) >= 0 ? 'positive' : 'negative'}>{Number(position.unrealized_pnl) > 0 ? '+' : ''}{fmt(position.unrealized_pnl)} USDT</strong>
              <small>{position.stop_loss ? `止损 ${fmt(position.stop_loss)}` : '未设置止损'}{position.take_profit ? ` · 止盈 ${fmt(position.take_profit)}` : ''}</small>
            </article>)}
          </div> : <div className="trade-position-empty"><i /><div><strong>当前空仓</strong><span>Agent 启动不代表立即开仓；出现合格信号并通过硬风控后，持仓会实时显示在这里。</span></div></div>}
        </section>

        <section className="lower-deck trade-overview-deck">
          <div className="overview-deck">
            <div className="backtest-summary">
              <div className="summary-title"><span>离线研究隔离</span><small>Strategy DSL · Campaign · PBO/CPCV · Walk-forward</small></div>
              <div><span>实时自动扫描</span><strong>已移除</strong></div>
              <div><span>页面加载回测</span><strong>已移除</strong></div>
              <div><span>模拟盘决策</span><strong>Trading Intelligence v2</strong></div>
              <button className="button" onClick={() => navigate('researchAgent')}>查看离线研究结果</button>
            </div>
            <div className="audit-stream">
              <div className="summary-title"><span>最近重要事件</span><button onClick={() => navigate('logs')}>查看全部日志</button></div>
              {dashboard.audit.slice(0, 3).map(event => <div className="audit-item" key={event.id}><i className={event.severity} /><time>{new Date(event.timestamp).toLocaleTimeString('zh-CN', { hour12: false })}</time><span>{event.message}</span></div>)}
            </div>
          </div>
        </section>
        </>))}
      </main>

      {notice && <div className="toast" role="status" aria-live="polite">{notice}</div>}
    </div>
  )
}

export default function App() {
  return <Suspense fallback={<div className="boot" role="status">正在加载工作区…</div>}>
    <TradingApp />
  </Suspense>
}
