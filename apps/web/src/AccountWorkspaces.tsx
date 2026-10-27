import { useEffect, useMemo, useState } from 'react'
import { AlertTriangle, RadioTower, RefreshCw, ShieldCheck } from 'lucide-react'
import { getTradingIntelligenceOverview, getTradingIntelligenceTradeHistory } from './api'
import TradingOperationsPanel from './TradingOperationsPanel'
import './tradingTerminal.css'
import type { DashboardData, MarketStateV2, TradeHistoryV2, TradingIntelligenceOverview } from './types'

const money = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

function signedMoney(value: string | number) {
  const numeric = Number(value)
  return `${numeric > 0 ? '+' : ''}${money.format(numeric)}`
}

function percentage(value?: string | null) {
  return value == null ? '—' : `${(Number(value) * 100).toFixed(2)}%`
}

function durationLabel(minutes: number) {
  if (minutes < 60) return `${minutes} 分钟`
  if (minutes < 1440) return `${Math.floor(minutes / 60)}小时 ${minutes % 60}分`
  return `${Math.floor(minutes / 1440)}天 ${Math.floor((minutes % 1440) / 60)}小时`
}

function EquityCurve({ history }: { history: TradeHistoryV2 }) {
  const width = 1000
  const height = 190
  const inset = 14
  const points = history.equity_curve
  if (!points.length) return <div className="trade-history-empty-curve"><i /><span>首笔交易平仓后，这里会生成费用后累计收益曲线。</span></div>
  const values = points.map(point => Number(point.cumulative_pnl))
  const low = Math.min(0, ...values)
  const high = Math.max(0, ...values)
  const span = Math.max(high - low, 1)
  const x = (index: number) => inset + (points.length === 1 ? (width - inset * 2) / 2 : index / (points.length - 1) * (width - inset * 2))
  const y = (value: number) => inset + (high - value) / span * (height - inset * 2)
  const path = points.map((point, index) => `${index ? 'L' : 'M'} ${x(index).toFixed(2)} ${y(Number(point.cumulative_pnl)).toFixed(2)}`).join(' ')
  const lastX = x(points.length - 1)
  const zeroY = y(0)
  const area = `${path} L ${lastX.toFixed(2)} ${zeroY.toFixed(2)} L ${x(0).toFixed(2)} ${zeroY.toFixed(2)} Z`
  const positive = Number(points.at(-1)?.cumulative_pnl ?? 0) >= 0
  return <div className={`trade-equity-curve ${positive ? 'positive' : 'negative'}`}>
    <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" role="img" aria-label="费用后累计收益曲线">
      <line x1={inset} x2={width - inset} y1={zeroY} y2={zeroY} className="zero-line" />
      <path d={area} className="curve-area" />
      <path d={path} className="curve-line" />
      <circle cx={lastX} cy={y(Number(points.at(-1)?.cumulative_pnl ?? 0))} r="5" />
    </svg>
    <div><span>{new Date(points[0].closed_at).toLocaleDateString('zh-CN')}</span><strong>{signedMoney(points.at(-1)?.cumulative_pnl ?? 0)} USDT</strong><span>{new Date(points.at(-1)?.closed_at ?? '').toLocaleDateString('zh-CN')}</span></div>
  </div>
}

export function PortfolioWorkspace({
  dashboard,
  busy,
  onCancelOrder,
}: {
  dashboard: DashboardData
  busy: boolean
  onCancelOrder: (orderId: string, symbol: string) => void
}) {
  const [history, setHistory] = useState<TradeHistoryV2 | null>(null)
  const [historySymbol, setHistorySymbol] = useState<'ALL' | MarketStateV2['symbol']>('ALL')
  const [historyDays, setHistoryDays] = useState(30)
  const [historyRefresh, setHistoryRefresh] = useState(0)
  const [historyBusy, setHistoryBusy] = useState(true)
  const [historyError, setHistoryError] = useState<string | null>(null)
  const [overview, setOverview] = useState<TradingIntelligenceOverview | null>(null)
  const [entryError, setEntryError] = useState<string | null>(null)

  useEffect(() => {
    let disposed = false
    let requestVersion = 0
    const load = async () => {
      const request = ++requestVersion
      try {
        const result = await getTradingIntelligenceOverview()
        if (!disposed && request === requestVersion) {
          setOverview(result)
          setEntryError(null)
        }
      } catch (reason) {
        if (!disposed && request === requestVersion) setEntryError(reason instanceof Error ? reason.message : '入场订单读取失败')
      }
    }
    void load()
    const timer = window.setInterval(() => void load(), 10_000)
    return () => { disposed = true; window.clearInterval(timer) }
  }, [historyRefresh])

  useEffect(() => {
    let disposed = false
    const load = async (showBusy: boolean) => {
      if (showBusy) setHistoryBusy(true)
      try {
        const result = await getTradingIntelligenceTradeHistory(historySymbol === 'ALL' ? null : historySymbol, historyDays)
        if (!disposed) {
          setHistory(result)
          setHistoryError(null)
        }
      } catch (reason) {
        if (!disposed) setHistoryError(reason instanceof Error ? reason.message : '历史收益读取失败')
      } finally {
        if (!disposed) setHistoryBusy(false)
      }
    }
    void load(true)
    const timer = window.setInterval(() => void load(false), 15_000)
    return () => {
      disposed = true
      window.clearInterval(timer)
    }
  }, [historyDays, historyRefresh, historySymbol])

  const activeOrders = useMemo(
    () => dashboard.orders.filter(item => ['pending', 'submitting', 'open', 'partially_filled', 'cancel_pending', 'uncertain'].includes(item.status.toLowerCase())),
    [dashboard.orders],
  )
  const summary = history?.summary
  const incompleteItems = history?.items.filter(item => item.exit_price == null || item.exit_order_ids.length === 0) ?? []
  const attributionComplete = Boolean(history && incompleteItems.length === 0)

  return <section className="ops-page account-workspace-page">
    <header className="ops-page-header"><div><span className="ops-kicker">账户执行与历史归因</span><h1>持仓、订单与收益</h1><p>当前风险和费用后历史收益使用同一套 OKX Demo 订单证据。</p></div><div className="trade-history-filters"><select value={historySymbol} onChange={event => setHistorySymbol(event.target.value as typeof historySymbol)} aria-label="历史收益品种"><option value="ALL">全部品种</option><option value="BTC/USDT:USDT">BTC</option><option value="ETH/USDT:USDT">ETH</option><option value="SOL/USDT:USDT">SOL</option></select><select value={historyDays} onChange={event => setHistoryDays(Number(event.target.value))} aria-label="历史收益时间范围"><option value={7}>近 7 天</option><option value={30}>近 30 天</option><option value={90}>近 90 天</option><option value={365}>近 1 年</option><option value={0}>全部</option></select><button type="button" disabled={historyBusy} onClick={() => setHistoryRefresh(current => current + 1)}><RefreshCw className={historyBusy ? 'spinning' : ''} />刷新</button></div></header>
    <div className="account-summary-strip"><div><span>账户净值</span><strong>{money.format(Number(dashboard.portfolio.equity))} USDT</strong></div><div><span>可用资金</span><strong>{money.format(Number(dashboard.portfolio.available_margin ?? dashboard.portfolio.cash))}</strong></div><div><span>总敞口</span><strong>{money.format(Number(dashboard.portfolio.gross_exposure))}</strong></div><div><span>当前持仓</span><strong>{dashboard.portfolio.open_positions.length}</strong></div><div><span>活动订单</span><strong>{activeOrders.length}</strong></div></div>

    <section className="trade-performance-section" aria-label="历史收益概览">
      {history && !attributionComplete && <div className="trade-attribution-warning"><AlertTriangle /><div><strong>收益归因不完整</strong><span>{incompleteItems.length} 笔交易缺少平仓价格或退出订单证据。汇总收益暂不展示，避免把不完整数据误判为真实业绩。</span></div><b>待核验</b></div>}
      <div className="trade-performance-metrics">
        <div><span>费用后净收益</span><strong className={attributionComplete ? Number(summary?.net_pnl ?? 0) >= 0 ? 'positive' : 'negative' : 'pending'}>{attributionComplete ? <>{signedMoney(summary?.net_pnl ?? 0)} <small>USDT</small></> : '待核验'}</strong></div>
        <div><span>胜率</span><strong className={!attributionComplete ? 'pending' : ''}>{attributionComplete ? percentage(summary?.win_rate) : '—'}</strong><small>{attributionComplete ? `${summary?.winning_trades ?? 0} 胜 / ${summary?.losing_trades ?? 0} 负` : '退出证据不完整'}</small></div>
        <div><span>Profit Factor</span><strong className={!attributionComplete ? 'pending' : ''}>{attributionComplete && summary?.profit_factor != null ? Number(summary.profit_factor).toFixed(2) : '—'}</strong><small>{summary?.trade_count ?? 0} 笔已平仓</small></div>
        <div><span>最大回撤</span><strong className={attributionComplete ? 'negative' : 'pending'}>{attributionComplete ? money.format(Number(summary?.maximum_drawdown ?? 0)) : '—'}</strong><small>{attributionComplete ? 'USDT' : '等待完整归因'}</small></div>
        <div><span>累计费用</span><strong>{money.format(Number(summary?.total_fees ?? 0))}</strong><small>{attributionComplete ? `平均 ${money.format(Number(summary?.average_pnl ?? 0))} / 笔` : '费用仍按订单证据展示'}</small></div>
      </div>
      {historyError ? <p className="trade-history-error">{historyError}</p> : history ? attributionComplete ? <EquityCurve history={history} /> : <div className="trade-history-empty-curve attribution-pending"><span>权益曲线已暂停：等待完整退出订单与成交价格。</span></div> : <div className="trade-history-empty-curve"><span>历史收益加载中…</span></div>}
    </section>

    <section className="account-table-section"><div className="panel-title"><span>当前持仓</span><b>{dashboard.portfolio.open_positions.length}</b></div><div className="account-data-table positions"><div className="table-head"><span>品种</span><span>方向</span><span>数量</span><span>杠杆</span><span>平均价</span><span>标记价</span><span>未实现盈亏</span><span>清算 / 保护</span></div>{dashboard.portfolio.open_positions.map(position => <article key={`${position.symbol}-${position.side}`}><strong>{position.symbol}</strong><b className={position.side === 'long' ? 'positive' : 'negative'}>{position.side === 'long' ? '多头' : '空头'}</b><span>{position.quantity}</span><span>{position.leverage ? `${position.leverage}x` : '—'}</span><span>{money.format(Number(position.average_entry))}</span><span>{money.format(Number(position.mark_price))}</span><strong className={Number(position.unrealized_pnl) >= 0 ? 'positive' : 'negative'}>{money.format(Number(position.unrealized_pnl))}</strong><small>清算 {position.liquidation_price ? money.format(Number(position.liquidation_price)) : '—'} · 止损 {position.stop_loss ? money.format(Number(position.stop_loss)) : '—'} · 止盈 {position.take_profit ? money.format(Number(position.take_profit)) : '—'}</small></article>)}{!dashboard.portfolio.open_positions.length && <p className="ops-empty">当前没有持仓。</p>}</div></section>
    <section className="account-table-section ti-account-entries" aria-label="入场订单运行状态">
      {entryError && <p className="trade-history-error">{entryError} · 当前订单状态可能延迟</p>}
      <TradingOperationsPanel overview={overview} view="entries" onSelect={symbol => setHistorySymbol(symbol)} />
    </section>
    <section className="account-table-section"><div className="panel-title"><span>近期交易所订单与成交（含退出）</span><b>{dashboard.orders.length}</b></div><div className="account-data-table orders"><div className="table-head"><span>时间</span><span>品种</span><span>方向</span><span>类型</span><span>成交 / 委托</span><span>委托价 / 成交均价</span><span>状态</span><span>操作</span></div>{dashboard.orders.map(order => <article key={order.id}><time>{new Date(order.created_at).toLocaleString('zh-CN', { hour12: false })}</time><strong>{order.symbol}</strong><b className={order.side === 'buy' ? 'positive' : 'negative'}>{order.side === 'buy' ? '买入' : '卖出'}</b><span>{order.order_type.toLowerCase() === 'market' ? '市价' : '限价'}</span><span>{order.filled_quantity} / {order.quantity}</span><span>{order.order_type.toLowerCase() === 'market' ? '市价' : order.limit_price ?? '—'} / {Number(order.filled_quantity) > 0 ? order.average_fill_price ?? '待确认' : '未成交'}</span><b>{order.status}</b><span>{['pending', 'open', 'partially_filled'].includes(order.status.toLowerCase()) ? <button disabled={busy} onClick={() => onCancelOrder(order.id, order.symbol)}>撤单</button> : '—'}</span></article>)}{!dashboard.orders.length && <p className="ops-empty">还没有订单。</p>}</div></section>

    <section className="account-table-section trade-history-section"><div className="panel-title"><span>已平仓交易</span><b>{summary?.trade_count ?? 0}</b></div><div className="account-data-table trade-history-table"><div className="table-head"><span>平仓时间</span><span>品种 / 方向</span><span>开仓 → 平仓</span><span>数量</span><span>净盈亏</span><span>收益 / R</span><span>费用</span><span>持仓 / 原因</span></div>{history?.items.map(item => {
      const complete = item.exit_price != null && item.exit_order_ids.length > 0
      return <article key={item.position_id} className={!complete ? 'incomplete' : ''}><time>{new Date(item.closed_at).toLocaleString('zh-CN', { hour12: false })}</time><div><strong>{item.symbol.replace('/USDT:USDT', '')}</strong><b className={item.side === 'LONG' ? 'positive' : 'negative'}>{item.side === 'LONG' ? '多头' : '空头'}</b></div><span>{money.format(Number(item.entry_price))} → {complete ? money.format(Number(item.exit_price)) : '待归因'}</span><span>{item.quantity}</span><strong className={complete ? Number(item.realized_pnl) >= 0 ? 'positive' : 'negative' : 'pending'}>{complete ? signedMoney(item.realized_pnl) : '待核验'}</strong><span className={complete ? Number(item.return_after_cost) >= 0 ? 'positive' : 'negative' : 'pending'}>{complete ? `${percentage(item.return_after_cost)} · ${Number(item.return_on_risk).toFixed(2)}R` : '—'}</span><span>{money.format(Number(item.fees))}</span><small title={item.exit_reason ?? ''}>{durationLabel(item.duration_minutes)} · {complete ? item.exit_reason ?? '系统退出' : '缺少退出订单证据'}</small></article>
    })}{history && !history.items.length && <p className="ops-empty">筛选范围内还没有已平仓交易。挂单和未平仓头寸不会计入历史收益。</p>}{historyBusy && !history && <p className="ops-empty">正在读取已平仓交易…</p>}</div></section>
  </section>
}

export function RiskWorkspace({
  dashboard,
  busy,
  onToggleHalt,
}: {
  dashboard: DashboardData
  busy: boolean
  onToggleHalt: () => void
}) {
  const halted = dashboard.system.risk_state.halted
  return <section className="ops-page risk-workspace-page">
    <header className="ops-page-header"><div><span className="ops-kicker">确定性风险引擎</span><h1>风险控制</h1><p>AI 和 Agent 不能修改这些门禁，也不能自行恢复全局停机。</p></div><button className={`button ${halted ? 'primary' : 'danger'}`} disabled={busy} onClick={onToggleHalt}>{halted ? <RefreshCw /> : <AlertTriangle />}{halted ? '恢复新开仓' : '紧急停止开仓'}</button></header>
    <div className={`risk-hero-state ${halted ? 'halted' : dashboard.risk.approved ? 'passed' : 'held'}`}><ShieldCheck /><div><span>当前风险结论</span><strong>{halted ? '全局停机' : dashboard.risk.approved ? '允许按计划交易' : '拒绝新开仓'}</strong><p>{halted ? dashboard.system.risk_state.reason : '每笔订单仍会在提交前重新评估。'}</p></div><b>{dashboard.risk.checks.filter(item => item.passed).length}/{dashboard.risk.checks.length}</b></div>
    <div className="risk-workspace-layout"><section><div className="panel-title"><span>预交易检查</span><b>{dashboard.risk.approved ? 'PASS' : 'HOLD'}</b></div><div className="risk-rule-list">{dashboard.risk.checks.map(check => <article key={check.code} className={check.passed ? 'passed' : 'failed'}><i>{check.passed ? '✓' : '×'}</i><div><strong>{check.code === 'trading_intelligence_v2_boundary' ? '交易决策边界' : check.code.replaceAll('_', ' ')}</strong><span>{check.message}</span></div><b>{check.blocking === false ? '提示' : '硬门禁'}</b></article>)}</div></section><aside><div className="panel-title"><span>执行链路</span><RadioTower /></div><dl className="risk-runtime-facts"><div><dt>订单对账</dt><dd>{dashboard.system.reconciliation ? `${dashboard.system.reconciliation.confirmed} 已确认` : '读取中'}</dd></div><div><dt>不确定订单</dt><dd>{dashboard.system.reconciliation?.uncertain_order_ids.length ?? 0}</dd></div><div><dt>外部仓位</dt><dd>{dashboard.system.reconciliation?.unknown_position_symbols.length ?? 0}</dd></div><div><dt>缺少保护</dt><dd>{dashboard.system.reconciliation?.unprotected_order_ids.length ?? 0}</dd></div><div><dt>行情延迟</dt><dd>{dashboard.system.data_latency_ms} ms</dd></div><div><dt>风险来源</dt><dd>{dashboard.system.risk_state.source}</dd></div></dl></aside></div>
  </section>
}
