import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Activity, AlertTriangle, Ban, Check, Clock3, Eye, EyeOff, Play, RefreshCw, ShieldCheck, Square, Zap } from 'lucide-react'
import {
  commissionTradingIntelligenceDemo,
  describeWakeCondition,
  entryStatusLabel,
  getMarketWindow,
  getTradingIntelligenceOverview,
  issueHumanLiveGrantV2,
  isActualPosition,
  isPendingEntry,
  latestEntryOrders,
  openMarketStream,
  previewHumanLiveGrantV2,
  refreshTradingIntelligenceMarketState,
  revokeHumanLiveGrantV2,
  startTradingIntelligenceDemo,
  stopTradingIntelligenceAutomation,
  wakeActionLabel,
  type AutomationCommissioningV2,
  type LiveGrantDraftV2,
} from './api'
import ProfessionalChart from './ProfessionalChart'
import TradingOperationsPanel from './TradingOperationsPanel'
import './tradingTerminal.css'
import { CHART_TIMEFRAMES } from './timeframes'
import type {
  MarketStreamEvent,
  MarketWindow,
  HumanLiveGrantPreviewV2,
  MarketStateV2,
  TradingIntelligenceFeatureGroup,
  TradingIntelligenceOverview,
  TradePlanV2,
  EntryOrderRuntime,
} from './types'

const CHART_CANDLE_LIMIT = 300

const DEFAULT_GRANT: LiveGrantDraftV2 = {
  symbols: ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT'],
  max_risk_per_trade: '0.005',
  max_portfolio_risk: '0.02',
  max_leverage: '3',
  max_daily_drawdown: '0.02',
  max_open_positions: 2,
  valid_hours: 24,
}

function shortSymbol(symbol: string) {
  return symbol.split('/')[0]
}

function percent(value?: string) {
  return value === undefined ? '—' : `${(Number(value) * 100).toFixed(1)}%`
}

function planRewardRisk(plan: TradePlanV2, order?: EntryOrderRuntime) {
  const actualFill = order && Number(order.filled_quantity) > 0 ? order.average_fill_price : null
  const entry = Number(actualFill ?? (plan.order_type === 'LIMIT'
    ? order?.submitted_price ?? order?.requested_price ?? plan.limit_price ?? NaN
    : plan.order_type === 'MARKET' ? order?.reference_price ?? plan.reference_price ?? NaN
      : (Number(plan.entry_zone.lower) + Number(plan.entry_zone.upper)) / 2))
  const stopDistance = Math.abs(entry - Number(plan.management.hard_stop))
  if (!Number.isFinite(stopDistance) || stopDistance <= 0) return null
  const rewards = plan.management.take_profits.map(target => (
    plan.side === 'LONG' ? Number(target.price) - entry : entry - Number(target.price)
  ))
  if (!rewards.length || rewards.some(value => !Number.isFinite(value))) return null
  return Math.min(...rewards) / stopDistance
}

function planPriceLabel(plan: TradePlanV2) {
  if (plan.order_type === 'LIMIT') return `限价 ${plan.limit_price ?? '待确认'}`
  if (plan.order_type === 'MARKET') return `市价 · 参考 ${plan.reference_price ?? '—'}`
  return `旧计划区间 ${plan.entry_zone.lower} – ${plan.entry_zone.upper}`
}

function planStateLabel(state: TradePlanV2['state']) {
  const labels: Record<TradePlanV2['state'], string> = {
    CREATED: '已创建', VALIDATED: '已验证', ACTIVE: '已激活', WAITING_TRIGGER: '等待价格',
    ARMED: '已武装', TRIGGERED: '订单已提交', COMPLETED: '已完成', EXPIRED: '已过期',
    INVALIDATED: '已失效', CANCELLED: '已取消',
  }
  return labels[state]
}

function healthLabel(group?: TradingIntelligenceFeatureGroup) {
  if (!group) return '未采集'
  if (group.health === 'healthy') return '健康'
  if (group.health === 'degraded') return '降级'
  return '不可用'
}

function FeatureRow({ label, group }: { label: string; group?: TradingIntelligenceFeatureGroup }) {
  const entries = Object.entries(group?.values ?? {}).slice(0, 3)
  return <div className={`ti-feature-row ${group?.health ?? 'unavailable'}`}>
    <span><i />{label}</span>
    <strong>{healthLabel(group)}</strong>
    <small>{entries.length ? entries.map(([key, value]) => `${key} ${String(value)}`).join(' · ') : group?.reasons[0] ?? '等待有效数据'}</small>
  </div>
}

export default function TradingIntelligenceWorkspace({ active }: { active: boolean }) {
  const [overview, setOverview] = useState<TradingIntelligenceOverview | null>(null)
  const [selected, setSelected] = useState<MarketStateV2['symbol']>('BTC/USDT:USDT')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [grantPreview, setGrantPreview] = useState<HumanLiveGrantPreviewV2 | null>(null)
  const [confirmOne, setConfirmOne] = useState(false)
  const [confirmTwo, setConfirmTwo] = useState(false)
  const [commissioning, setCommissioning] = useState<AutomationCommissioningV2 | null>(null)
  const [timeframe, setTimeframe] = useState('5m')
  const [marketWindow, setMarketWindow] = useState<MarketWindow | null>(null)
  const [chartBusy, setChartBusy] = useState(false)
  const [chartError, setChartError] = useState<string | null>(null)
  const [streamState, setStreamState] = useState<'connecting' | 'live' | 'stale' | 'offline'>('connecting')
  const [focusedPlanId, setFocusedPlanId] = useState<string | null>(null)
  const [focusedEntryId, setFocusedEntryId] = useState<string | null>(null)
  const [showPlanOnChart, setShowPlanOnChart] = useState(true)
  const [currentTime, setCurrentTime] = useState(0)
  const [workbenchTab, setWorkbenchTab] = useState('positions')
  const lastMarketEventAt = useRef(0)
  const overviewRequest = useRef(0)
  const overviewInFlight = useRef<Promise<void> | null>(null)

  const refresh = useCallback((): Promise<void> => {
    if (overviewInFlight.current) return overviewInFlight.current
    const request = ++overviewRequest.current
    const pending = (async () => {
      try {
        const result = await getTradingIntelligenceOverview()
        if (request !== overviewRequest.current) return
        setOverview(result)
        setError(null)
      } catch (reason) {
        if (request !== overviewRequest.current) return
        setError(reason instanceof Error ? reason.message : 'Trading Intelligence 状态不可用')
      } finally {
        overviewInFlight.current = null
      }
    })()
    overviewInFlight.current = pending
    return pending
  }, [])

  useEffect(() => {
    if (!active) return
    let stopped = false
    let timer: number | undefined
    const poll = async () => {
      await refresh()
      if (!stopped) timer = window.setTimeout(() => void poll(), 10_000)
    }
    timer = window.setTimeout(() => void poll(), 0)
    return () => { stopped = true; overviewRequest.current += 1; window.clearTimeout(timer) }
  }, [active, refresh])

  useEffect(() => {
    if (!active) return
    const updateClock = () => setCurrentTime(Date.now())
    const kickoff = window.setTimeout(updateClock, 0)
    const timer = window.setInterval(updateClock, 60_000)
    return () => { window.clearTimeout(kickoff); window.clearInterval(timer) }
  }, [active])

  useEffect(() => {
    if (!active) return
    let stopped = false
    let socket: WebSocket | null = null
    let reconnectTimer: number | undefined
    let reconnectDelay = 1_000
    let frame: number | undefined
    let pendingMarket: MarketStreamEvent | null = null
    let windowInFlight = false
    const marketSymbol = selected.replace(':USDT', '')
    const sessionStartedAt = Date.now()

    const loadWindow = async (showBusy: boolean) => {
      if (windowInFlight) return
      windowInFlight = true
      const requestedAt = Date.now()
      if (showBusy) setChartBusy(true)
      try {
        const result = await getMarketWindow(marketSymbol, timeframe, CHART_CANDLE_LIMIT)
        if (!stopped) {
          setMarketWindow(current => {
            if (!current || current.market.symbol !== marketSymbol || current.timeframe !== timeframe
              || lastMarketEventAt.current <= requestedAt) return result
            // REST repairs history; quotes received while it was in flight own the live tail.
            const tailStart = Date.parse(result.candles.at(-1)?.timestamp ?? '')
            const merged = new Map(result.candles.map(candle => [candle.timestamp, candle]))
            for (const candle of current.candles) {
              if (!Number.isFinite(tailStart) || Date.parse(candle.timestamp) >= tailStart) merged.set(candle.timestamp, candle)
            }
            return {
              ...result,
              market: Date.parse(current.market.timestamp) >= Date.parse(result.market.timestamp) ? current.market : result.market,
              candles: [...merged.values()].sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp)).slice(-CHART_CANDLE_LIMIT),
            }
          })
          setChartError(null)
        }
      } catch (reason) {
        if (!stopped) {
          setChartError(reason instanceof Error ? reason.message : '实时 K 线加载失败')
          setStreamState('offline')
        }
      } finally {
        windowInFlight = false
        if (!stopped && showBusy) setChartBusy(false)
      }
    }

    const renderMarket = (event: MarketStreamEvent) => {
      if (stopped || event.market.symbol !== marketSymbol) return
      const receivedAt = Date.now()
      lastMarketEventAt.current = receivedAt
      setStreamState('live')
      setMarketWindow(current => {
        if (!current || current.market.symbol !== marketSymbol || current.timeframe !== timeframe) return current
        if (Date.parse(event.market.timestamp) < Date.parse(current.market.timestamp)) return current
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
        return { ...current, market: event.market, candles }
      })
    }

    const applyMarket = (event: MarketStreamEvent) => {
      if (stopped || event.market.symbol !== marketSymbol) return
      lastMarketEventAt.current = Date.now()
      pendingMarket = event
      if (frame !== undefined) return
      frame = window.requestAnimationFrame(() => {
        frame = undefined
        if (pendingMarket) renderMarket(pendingMarket)
        pendingMarket = null
      })
    }

    const connect = () => {
      if (stopped) return
      setStreamState('connecting')
      socket = openMarketStream(marketSymbol, timeframe, applyMarket, state => {
        if (stopped) return
        if (state === 'connected') {
          reconnectDelay = 1_000
          // An open socket is not evidence of a fresh quote.
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

    lastMarketEventAt.current = 0
    void loadWindow(true)
    connect()
    const staleTimer = window.setInterval(() => {
      if (Date.now() - (lastMarketEventAt.current || sessionStartedAt) > 15_000) {
        setStreamState(value => value === 'offline' ? value : 'stale')
      }
    }, 1_000)
    const reconcileTimer = window.setInterval(() => void loadWindow(false), 60_000)
    return () => {
      stopped = true
      if (frame !== undefined) window.cancelAnimationFrame(frame)
      socket?.close()
      if (reconnectTimer) window.clearTimeout(reconnectTimer)
      window.clearInterval(staleTimer)
      window.clearInterval(reconcileTimer)
    }
  }, [active, selected, timeframe])

  const state = overview?.market_states.find(item => item.symbol === selected)
  const thesis = overview?.active_theses.find(item => item.symbol === selected)
  const plans = overview?.active_plans.filter(item => item.symbol === selected) ?? []
  const allEntries = latestEntryOrders(overview?.entry_orders)
  const entries = allEntries.filter(item => item.symbol === selected)
  const focusedEntry = entries.find(item => item.entry_id === focusedEntryId)
  const plan = focusedEntry ? plans.find(item => item.plan_id === focusedEntry.plan_id) : plans.find(item => item.plan_id === focusedPlanId)
    ?? plans.find(item => ['WAITING_TRIGGER', 'ARMED', 'ACTIVE'].includes(item.state))
    ?? plans[0]
  const entry = focusedEntry ?? (plan ? entries.find(item => item.plan_id === plan.plan_id) : entries.find(isPendingEntry) ?? entries[0])
  const quant = overview?.quant_predictions.find(item => item.symbol === selected)
  const quantModel = overview?.quant_models.find(item => item.symbol === selected)
  const waits = overview?.active_waits.filter(item => item.status === 'ACTIVE') ?? []
  const wait = waits.find(item => item.symbol === selected)
  const allPositions = overview?.positions.filter(isActualPosition) ?? []
  const positions = allPositions.filter(item => item.symbol === selected)
  const position = positions.find(item => item.position_id === entry?.position_id || item.position_id === plan?.position_id)
    ?? (plan ? positions.find(item => item.plan_id === plan.plan_id || item.plan_ids?.includes(plan.plan_id)) : positions[0])
  const automation = overview?.automation
  const episodes = useMemo(
    () => overview?.episodes.filter(item => item.symbol === selected).slice(0, 8) ?? [],
    [overview, selected],
  )
  const currentEpisode = entry ? episodes.find(item => item.episode_id === entry.episode_id) : plan ? episodes.find(item => item.plan_id === plan.plan_id) : episodes[0]
  const rewardRisk = plan ? planRewardRisk(plan, entry) : null
  const planExpired = Boolean(currentTime && plan && Date.parse(plan.expires_at) < currentTime)
  const alerts = [
    ...(plans.length > 1 ? [`${shortSymbol(selected)} 存在 ${plans.length} 个活动计划，请逐一核对。`] : []),
    ...(planExpired ? [`当前计划已超过有效期，但状态仍为 ${plan?.state}。`] : []),
    ...(rewardRisk !== null && plan && rewardRisk < Number(plan.minimum_reward_risk)
      ? [`当前计划实际最近目标盈亏比 ${rewardRisk.toFixed(2)}，低于声明值 ${plan.minimum_reward_risk}。`]
      : []),
    ...entries.filter(item => item.status === 'UNCERTAIN').map(item => `入场订单 ${item.entry_id.slice(-8)} 状态待核对：${item.reason || '等待交易所确认'}`),
    ...entries.filter(item => isPendingEntry(item) && currentTime > Date.parse(item.expires_at)).map(item => `入场订单 ${item.entry_id.slice(-8)} 已过期，${entryStatusLabel(item.status)}；需以撤单或成交确认结束。`),
  ]
  const workflow = [
    ['市场状态', state ? state.regime : '等待'],
    ['Thesis', thesis ? thesis.state : '空'],
    ['Plan', plan ? plan.state : '空'],
    ['等待条件', plan?.entry_conditions.length ? `${plan.entry_conditions.length} 条` : wait ? `${wait.wait_plan.wake_conditions.length} 条` : '无'],
    ['入场订单', entry ? entryStatusLabel(entry.status) : '无'],
    ['风险分配', currentEpisode?.risk_allocation_id ? '完成' : '未触发'],
    ['持仓', position ? position.state : '空仓'],
    ['学习反馈', overview?.learning?.sample_count ? `${overview.learning.sample_count} 笔经验` : '等待平仓'],
  ]

  const previewGrant = async () => {
    setBusy(true)
    try {
      setGrantPreview(await previewHumanLiveGrantV2(DEFAULT_GRANT))
      setError(null)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '授权预检失败')
    } finally {
      setBusy(false)
    }
  }

  const collectState = async () => {
    setBusy(true)
    try {
      await refreshTradingIntelligenceMarketState(selected)
      await refresh()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'MarketState 采集失败')
    } finally {
      setBusy(false)
    }
  }

  const issueGrant = async () => {
    if (!grantPreview) return
    setBusy(true)
    try {
      await issueHumanLiveGrantV2({
        ...DEFAULT_GRANT,
        configuration_fingerprint: grantPreview.configuration_fingerprint,
        risk_confirmation_one: confirmOne,
        risk_confirmation_two: confirmTwo,
      })
      setGrantPreview(null)
      setConfirmOne(false)
      setConfirmTwo(false)
      await refresh()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '授权签发失败')
    } finally {
      setBusy(false)
    }
  }

  const revokeGrant = async () => {
    if (!overview?.live_grant) return
    setBusy(true)
    try {
      await revokeHumanLiveGrantV2(overview.live_grant.grant_id)
      await refresh()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '授权撤销失败')
    } finally {
      setBusy(false)
    }
  }

  const commissionDemo = async () => {
    setBusy(true)
    try {
      const result = await commissionTradingIntelligenceDemo(DEFAULT_GRANT.symbols)
      setCommissioning(result)
      setError(null)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'OKX Demo 验收失败')
    } finally {
      setBusy(false)
    }
  }

  const startDemo = async () => {
    setBusy(true)
    try {
      await startTradingIntelligenceDemo(DEFAULT_GRANT.symbols)
      setCommissioning(null)
      await refresh()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '自动模拟盘启动失败')
    } finally {
      setBusy(false)
    }
  }

  const stopAutomation = async () => {
    setBusy(true)
    try {
      await stopTradingIntelligenceAutomation()
      await refresh()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '停止自动运行失败')
    } finally {
      setBusy(false)
    }
  }

  return <div className="ti-workspace ti-terminal">
    <header className="ti-header">
      <div>
        <span>COSMOS / AI QUANT TERMINAL</span>
        <h1>自动量化交易台<span className="ti-terminal-tag">PRO</span></h1>
      </div>
      <div className="ti-header-actions">
        <span className={automation?.enabled ? 'live' : 'shadow'}>{automation?.enabled
          ? `${automation.requested_mode} · ${automation.status}`
          : `${automation?.requested_mode ?? 'OKX_DEMO'} 已停止`}</span>
        <button onClick={() => void collectState()} disabled={busy}><Activity />采集状态</button>
        <button onClick={() => void refresh()} disabled={busy}><RefreshCw />刷新</button>
      </div>
    </header>

    {error && <div className="ti-error"><Ban />{error}</div>}

    <section className="ti-account-strip" aria-label="组合运行概览">
      <div><span>监控合约</span><strong>{overview?.symbols.length ?? 3}<small> USDT 永续</small></strong></div>
      <div><span>运行品种</span><strong>{automation?.enabled ? automation.symbols.length : 0}<small> 独立决策</small></strong></div>
      <div><span>持仓 / 容量</span><strong>{allPositions.length}<small> / {overview?.portfolio_policy?.max_open_positions ?? '—'}</small></strong></div>
      <div><span>单笔风险上限</span><strong>{percent(overview?.portfolio_policy?.risk_per_trade)}</strong></div>
      <div><span>组合风险上限</span><strong>{percent(overview?.portfolio_policy?.max_portfolio_risk)}</strong></div>
      <button onClick={() => setWorkbenchTab('learning')}><span>学习样本</span><strong>{overview?.learning?.sample_count ?? 0}<small> 笔已归因 ↗</small></strong></button>
    </section>

    <section className={`ti-runtime ${automation?.status?.toLowerCase() ?? 'stopped'}`} aria-label="自动模拟盘运行状态">
      <div><span>AUTONOMOUS {automation?.requested_mode ?? 'OKX_DEMO'}</span><strong>{automation?.status ?? 'STOPPED'}</strong><small>{automation?.enabled
        ? automation.requested_mode === 'SHADOW'
          ? '影子运行中；不会调用交易所下单 API'
          : '事件驱动运行中；普通行情不调用 LLM'
        : '自动执行当前关闭'}</small></div>
      <div><span>RECONCILIATION</span><strong>{automation?.last_reconciliation_at ? new Date(automation.last_reconciliation_at).toLocaleTimeString('zh-CN', { hour12: false }) : '—'}</strong><small>每 5 秒核对持仓、订单与原生保护</small></div>
      <div><span>FAILURE BUDGET</span><strong>{automation?.consecutive_failures ?? 0}</strong><small>{automation?.last_error_code ?? '没有运行错误'}</small></div>
      <div className="ti-runtime-actions">
        <button onClick={() => void commissionDemo()} disabled={busy || automation?.enabled}>验收 Demo</button>
        {automation?.enabled
          ? <button className="stop" onClick={() => void stopAutomation()} disabled={busy}><Square />停止</button>
          : <button onClick={() => void startDemo()} disabled={busy || commissioning?.eligible === false}><Play />启动全自动</button>}
      </div>
      {commissioning && <p className={commissioning.eligible ? 'eligible' : 'blocked'}>{commissioning.eligible
        ? `验收通过 · Equity ${commissioning.equity ?? '—'} · 账外订单/持仓均为 0`
        : `验收阻断 · ${commissioning.blockers.join(' · ')}`}</p>}
    </section>

    <div className="ti-terminal-grid">
      <div className="ti-terminal-center">
    <section className="ti-chart-plane" aria-label="实时行情 K 线">
      <header>
        <div><span>REAL-TIME MARKET</span><h2>{shortSymbol(selected)} / USDT 永续</h2></div>
        <div className="ti-chart-status">
          <button type="button" className={showPlanOnChart ? 'active' : ''} aria-pressed={showPlanOnChart} onClick={() => setShowPlanOnChart(value => !value)} disabled={!plan && !entry}>
            {showPlanOnChart ? <Eye /> : <EyeOff />}{showPlanOnChart ? '隐藏计划点位' : '显示计划点位'}
          </button>
          <div className={`ti-stream-state ${active ? streamState : 'offline'}`}><i />{active && streamState === 'live' ? '实时行情' : active && streamState === 'connecting' ? '连接中' : active && streamState === 'stale' ? '行情延迟' : '行情离线'}</div>
        </div>
      </header>
      {chartError && <div className="ti-chart-error"><Ban />{chartError}</div>}
      <div className="chart-panel ti-live-chart">
        <ProfessionalChart
          candles={marketWindow?.market.symbol === selected.replace(':USDT', '') && marketWindow.timeframe === timeframe ? marketWindow.candles : []}
          plan={plan ?? null}
          entryOrder={entry ?? null}
          exchange="OKX"
          symbol={selected}
          timeframe={timeframe}
          timeframes={[...CHART_TIMEFRAMES]}
          busy={chartBusy}
          showPlan={showPlanOnChart}
          onTimeframeChange={setTimeframe}
        />
      </div>
    </section>

    <nav className="ti-workbench-tabs" aria-label="交易工作区">
      {[['positions', '组合持仓'], ['entries', '入场订单'], ['waits', '等待策略'], ['plans', '活动计划'], ['learning', '学习复盘'], ['evidence', '决策记录'], ['market', '市场分析'], ['access', '实盘授权']].map(([key, label]) => <button key={key} aria-pressed={workbenchTab === key} className={workbenchTab === key ? 'active' : ''} onClick={() => setWorkbenchTab(key)}>{label}{key === 'positions' && <b>{allPositions.length}</b>}{key === 'entries' && <b>{allEntries.filter(isPendingEntry).length}</b>}{key === 'waits' && <b>{waits.length}</b>}{key === 'plans' && <b>{overview?.active_plans.length ?? 0}</b>}</button>)}
    </nav>
    {(workbenchTab === 'positions' || workbenchTab === 'entries' || workbenchTab === 'waits' || workbenchTab === 'learning') && <TradingOperationsPanel overview={overview} view={workbenchTab} onSelect={(symbol, planId, entryId) => { setSelected(symbol); setFocusedPlanId(planId ?? null); setFocusedEntryId(entryId ?? null) }} />}

    <section className="ti-plan-register" hidden={workbenchTab !== 'plans'} aria-label="活动计划">
      <header>
        <div><span>ACTIVE PLANS</span><strong>{shortSymbol(selected)} 计划登记簿</strong></div>
        <small>{plans.length} 个活动计划 · 点击切换图表点位与证据链</small>
      </header>
      {plans.length ? <div className="ti-plan-tabs">{plans.map(item => {
        const itemRewardRisk = planRewardRisk(item, entries.find(order => order.plan_id === item.plan_id))
        const expired = Boolean(currentTime && Date.parse(item.expires_at) < currentTime)
        const selectedPlan = item.plan_id === plan?.plan_id
        return <button type="button" key={item.plan_id} className={`${selectedPlan ? 'active' : ''} ${expired ? 'warning' : ''}`} aria-pressed={selectedPlan} onClick={() => { setFocusedPlanId(item.plan_id); setFocusedEntryId(null) }}>
          <span><b className={item.side.toLowerCase()}>{item.side}</b>{planStateLabel(item.state)}</span>
          <strong>{planPriceLabel(item)}</strong>
          <small>RR {itemRewardRisk?.toFixed(2) ?? '—'} · {expired ? '已过期' : `至 ${new Date(item.expires_at).toLocaleTimeString('zh-CN', { hour12: false })}`}</small>
        </button>
      })}</div> : <div className="ti-empty"><Clock3 />当前品种没有活动计划。</div>}
    </section>

    <div className="ti-primary-grid" hidden={workbenchTab !== 'market'}>
      <section className="ti-market-plane">
        <div className="ti-section-heading"><div><span>MARKETSTATE V2</span><h2>{state?.regime ?? '尚无有效市场状态'}</h2></div><small>{state ? new Date(state.data_as_of).toLocaleString('zh-CN', { hour12: false }) : 'data_as_of —'}</small></div>
        <div className="ti-market-price"><span>{shortSymbol(selected)} 最新价</span><strong>{String(state?.market.values.last ?? '—')}</strong><small>Spread {String(state?.market.values.spread_bps ?? '—')} bps · {state?.healthy_for_new_risk ? '允许进入风险判断' : '禁止新增风险'}</small></div>
        <div className="ti-feature-list">
          <FeatureRow label="结构" group={state?.structure} />
          <FeatureRow label="趋势" group={state?.trend} />
          <FeatureRow label="动量" group={state?.momentum} />
          <FeatureRow label="波动" group={state?.volatility} />
          <FeatureRow label="成交量" group={state?.volume} />
          <FeatureRow label="微观结构" group={state?.microstructure} />
          <FeatureRow label="衍生品" group={state?.derivatives} />
          <FeatureRow label="跨市场" group={state?.cross_market} />
          <FeatureRow label="官方事件" group={state?.events} />
        </div>
        <div className="ti-quant-line">
          <span><Activity />QUANT · 5m · {quantModel?.health === 'healthy' ? '可参与开仓' : quantModel?.health === 'degraded' ? '模型降级' : '不可用'}</span>
          <strong>LONG {percent(quant?.probability_long_tp_first)}</strong>
          <strong>SHORT {percent(quant?.probability_short_tp_first)}</strong>
          <small>{quantModel?.reason ?? `成本后期望 ${quant?.expected_return_after_cost ?? '—'} · TTL ${quant ? new Date(quant.expires_at).toLocaleTimeString('zh-CN', { hour12: false }) : '—'}`}</small>
        </div>
      </section>


    </div>

    <div className="ti-lower-grid" hidden={!['evidence', 'access'].includes(workbenchTab)}>
      <section className="ti-ledger" hidden={workbenchTab !== 'evidence'}>
        <div className="ti-section-heading"><div><span>DECISION LEDGER</span><h2>最近证据链</h2></div><small>{episodes.length} episodes</small></div>
        {episodes.length ? episodes.map(episode => <details key={episode.episode_id}>
          <summary><span><i className={episode.status === 'PLAN_REJECTED' || episode.status === 'DECISION_ERROR' ? 'rejected' : 'ready'} />{episode.status}</span><strong>{episode.episode_id.slice(-10)}</strong><small>{new Date(episode.created_at).toLocaleString('zh-CN', { hour12: false })}</small></summary>
          <div><span>MarketState <b>{episode.market_state_hash.slice(0, 12)}</b></span><span>Analyst <b>{episode.analysis_id?.slice(-8) ?? '—'}</b></span><span>Quant <b>{episode.quant_prediction_id?.slice(-8) ?? '—'}</b></span><span>Trader <b>{episode.decision_id?.slice(-8) ?? '—'}</b></span><span>Risk <b>{episode.risk_allocation_id?.slice(-8) ?? '—'}</b></span><span>Orders <b>{episode.order_ids.length}</b></span>{typeof episode.outcome?.summary === 'string' && <span>拒绝原因 <b>{episode.outcome.summary}</b></span>}</div>
        </details>) : <div className="ti-empty"><Clock3 />尚无 DecisionEpisode。普通行情更新不会创建 LLM 决策。</div>}
      </section>

      <aside className="ti-grant" hidden={workbenchTab !== 'access'}>
        <div className="ti-section-heading"><div><span>HUMAN LIVE GRANT</span><h2>{overview?.live_grant ? '实盘权限有效' : '实盘 API 锁定'}</h2></div><ShieldCheck /></div>
        {overview?.live_grant ? <>
          <p>{overview.live_grant.symbols.map(shortSymbol).join(' / ')} · 单笔风险上限 {percent(overview.live_grant.max_risk_per_trade)} · {overview.live_grant.max_leverage}x</p>
          <small>到期 {new Date(overview.live_grant.expires_at).toLocaleString('zh-CN', { hour12: false })}</small>
          <button className="danger" onClick={() => void revokeGrant()} disabled={busy}>立即撤销</button>
        </> : grantPreview ? <>
          <p className={grantPreview.eligible ? 'eligible' : 'blocked'}>{grantPreview.eligible ? '所有运行门禁已通过' : `${grantPreview.blockers.length} 项门禁未通过`}</p>
          {grantPreview.blockers.map(blocker => <small key={blocker}>{blocker}</small>)}
          <label><input type="checkbox" checked={confirmOne} onChange={event => setConfirmOne(event.target.checked)} />我确认实盘可能产生不可逆损失</label>
          <label><input type="checkbox" checked={confirmTwo} onChange={event => setConfirmTwo(event.target.checked)} />我确认风险上限、配置指纹和 24 小时有效期</label>
          <button onClick={() => void issueGrant()} disabled={busy || !grantPreview.eligible || !confirmOne || !confirmTwo}><Check />签发授权</button>
        </> : <>
          <p>无有效 Grant 时不会调用 OKX Live API；具备 Demo 凭证时自动路由测试网，否则保持 Shadow。</p>
          <button onClick={() => void previewGrant()} disabled={busy}>运行授权预检</button>
        </>}
      </aside>
    </div>
      </div>
      <aside className="ti-terminal-inspector" aria-label="市场与策略详情">
        <header className="ti-watchlist-heading"><strong>合约市场</strong><span>OKX · USDT</span></header>
        <nav className="ti-watchlist" aria-label="交易品种">
          {(overview?.symbols ?? DEFAULT_GRANT.symbols).map(symbol => {
            const market = overview?.market_states.find(item => item.symbol === symbol)
            const symbolPosition = allPositions.find(item => item.symbol === symbol)
            const symbolEntries = allEntries.filter(item => item.symbol === symbol && isPendingEntry(item))
            const symbolPlan = overview?.active_plans.find(item => item.symbol === symbol)
            return <button key={symbol} aria-pressed={selected === symbol} className={selected === symbol ? 'active' : ''} onClick={() => { setSelected(symbol as MarketStateV2['symbol']); setFocusedPlanId(null); setFocusedEntryId(null) }}>
              <span className="ti-asset-letter">{shortSymbol(symbol).slice(0, 1)}</span>
              <span><strong>{shortSymbol(symbol)}<small>USDT 永续</small></strong><small>{symbolPosition ? `${symbolPosition.side} · 持仓` : symbolPlan ? planStateLabel(symbolPlan.state) : '空仓'}{symbolEntries.length ? ` · ${symbolEntries.length} 笔入场待处理` : ''}{waits.some(item => item.symbol === symbol) ? ' · 等待策略' : ''}</small></span>
              <span><strong>{market?.market.values.last == null ? '—' : Number(market.market.values.last).toLocaleString('en-US', { maximumFractionDigits: 2 })}</strong><small>{market?.regime ?? '等待行情'}</small></span>
            </button>
          })}
        </nav>
      <aside className="ti-thesis-plane">
        <div className="ti-section-heading"><div><span>ACTIVE THESIS</span><h2>{thesis ? `${thesis.direction} · ${thesis.state}` : '没有有效 Thesis'}</h2></div>{thesis && <small>v{thesis.version}</small>}</div>
        <p className="ti-thesis-statement">{thesis?.statement ?? '等待下一次市场事件生成入场策略。'}</p>
        {thesis && <div className="ti-invalidation"><span>失效条件</span>{thesis.invalidation_rules.map(rule => <p key={rule}>{rule}</p>)}</div>}
        <div className="ti-plan-block">
          <span>TRADE PLAN</span>
          {plan ? <>
            <h3>{plan.side} · {planPriceLabel(plan)}</h3>
            {plan.valid_price_range && <small>有效价格范围 {plan.valid_price_range.lower} – {plan.valid_price_range.upper}</small>}
            {plan.position_id && <small>管理持仓 {plan.position_id} · 基于版本 {plan.position_version}</small>}
            <div className="ti-plan-levels"><span>Stop <b>{plan.management.hard_stop}</b></span>{plan.management.take_profits.map((target, index) => <span key={target.price}>TP{index + 1} <b>{target.price}</b></span>)}</div>
            <div className="ti-wakes">{plan.entry_conditions.map(condition => <div key={condition.condition_id}><Zap /><span>{describeWakeCondition(condition)}</span><strong>{wakeActionLabel(condition.action)}</strong></div>)}</div>
            <small>{entry ? `入场订单：${entryStatusLabel(entry.status)}` : plan.order_type ? '尚无入场订单记录；委托状态请查看入场订单。' : '旧计划：等待触发条件，尚不能据此确认已有挂单。'}</small>
          </> : <p>尚无机器可执行计划。</p>}
        </div>
        {entry && <div className="ti-position-block"><span>ENTRY ORDER · {entry.order_type}</span><strong>{entryStatusLabel(entry.status)}</strong><p>{entry.order_type === 'LIMIT' ? `委托 ${entry.submitted_price ?? '待确认'} · 请求 ${entry.requested_price ?? '—'}` : `市价 · 参考 ${entry.reference_price ?? '—'}`}</p><p>成交 {entry.filled_quantity} / {entry.quantity} · 均价 {Number(entry.filled_quantity) > 0 ? entry.average_fill_price ?? '待确认' : '—'}</p><small>{entry.reason || '未提供原因'} · 更新 {new Date(entry.updated_at).toLocaleString('zh-CN', { hour12: false })}</small></div>}
        {wait && <div className="ti-plan-block"><span>WAIT · 等待策略</span><p>{wait.wait_plan.reason}</p><div className="ti-wakes">{wait.wait_plan.wake_conditions.map(condition => <div key={condition.condition_id}><Clock3 /><span>{describeWakeCondition(condition)}</span><strong>{wakeActionLabel(condition.action)}</strong></div>)}</div><small>有效至 {new Date(wait.wait_plan.expires_at).toLocaleString('zh-CN', { hour12: false })}</small></div>}
        {position && <div className="ti-position-block"><span>DEMO POSITION</span><strong>{position.side} · {position.state}</strong><p>{position.remaining_quantity} / {position.original_quantity} @ {position.average_entry}</p><small>Hard stop {position.initial_stop} · Effective stop {position.current_stop} · TP {position.targets_reached}</small></div>}
      </aside>
        <div className="ti-inspector-quant"><header><Activity /><strong>量化信号</strong><span>{quantModel?.health === 'healthy' ? '在线' : '待就绪'}</span></header>
          <div><span>多头概率</span><strong className="positive">{percent(quant?.probability_long_tp_first)}</strong></div>
          <div><span>空头概率</span><strong className="negative">{percent(quant?.probability_short_tp_first)}</strong></div>
          <div><span>成本后期望</span><strong>{quant?.expected_return_after_cost ?? '—'}</strong></div>
          <small>{quantModel?.reason ?? '量化信号为当前决策提供参考'}</small>
        </div>
      </aside>
    </div>
    {alerts.length > 0 && <section className="ti-alert-stack" aria-label="需要处理的交易异常">
      <header><AlertTriangle /><strong>需要核对</strong><span>{alerts.length} 项</span></header>
      <div>{alerts.map(message => <p key={message}>{message}</p>)}</div>
    </section>}

    <section className="ti-flow" aria-label="决策状态机">
      {workflow.map(([label, value], index) => <div key={label} className={value === '空' || value === '等待' || value === '无' || value === '未触发' || value === '空仓' ? 'idle' : 'ready'}>
        <span>{String(index + 1).padStart(2, '0')} · {label}</span><strong>{value}</strong>
      </div>)}
    </section>

  </div>
}
