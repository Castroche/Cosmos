import { AlertTriangle, RadioTower, RefreshCw, ShieldCheck } from 'lucide-react'
import type { DashboardData } from './types'

const money = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

export function PortfolioWorkspace({
  dashboard,
  busy,
  onCancelOrder,
}: {
  dashboard: DashboardData
  busy: boolean
  onCancelOrder: (orderId: string, symbol: string) => void
}) {
  return <section className="ops-page account-workspace-page">
    <header className="ops-page-header"><div><span className="ops-kicker">账户执行状态</span><h1>持仓与订单</h1><p>集中查看敞口、保护价格、挂单和成交，不需要返回交易图表寻找记录。</p></div></header>
    <div className="account-summary-strip"><div><span>账户净值</span><strong>{money.format(Number(dashboard.portfolio.equity))} USDT</strong></div><div><span>可用资金</span><strong>{money.format(Number(dashboard.portfolio.available_margin ?? dashboard.portfolio.cash))}</strong></div><div><span>总敞口</span><strong>{money.format(Number(dashboard.portfolio.gross_exposure))}</strong></div><div><span>当前持仓</span><strong>{dashboard.portfolio.open_positions.length}</strong></div><div><span>活动订单</span><strong>{dashboard.orders.filter(item => ['pending', 'open', 'partially_filled'].includes(item.status)).length}</strong></div></div>
    <section className="account-table-section"><div className="panel-title"><span>当前持仓</span><b>{dashboard.portfolio.open_positions.length}</b></div><div className="account-data-table positions"><div className="table-head"><span>品种</span><span>方向</span><span>数量</span><span>杠杆</span><span>平均价</span><span>标记价</span><span>未实现盈亏</span><span>清算 / 保护</span></div>{dashboard.portfolio.open_positions.map(position => <article key={`${position.symbol}-${position.side}`}><strong>{position.symbol}</strong><b className={position.side === 'long' ? 'positive' : 'negative'}>{position.side === 'long' ? '多头' : '空头'}</b><span>{position.quantity}</span><span>{position.leverage ? `${position.leverage}x` : '—'}</span><span>{money.format(Number(position.average_entry))}</span><span>{money.format(Number(position.mark_price))}</span><strong className={Number(position.unrealized_pnl) >= 0 ? 'positive' : 'negative'}>{money.format(Number(position.unrealized_pnl))}</strong><small>清算 {position.liquidation_price ? money.format(Number(position.liquidation_price)) : '—'} · 止损 {position.stop_loss ? money.format(Number(position.stop_loss)) : '—'} · 止盈 {position.take_profit ? money.format(Number(position.take_profit)) : '—'}</small></article>)}{!dashboard.portfolio.open_positions.length && <p className="ops-empty">当前没有持仓。</p>}</div></section>
    <section className="account-table-section"><div className="panel-title"><span>订单与成交</span><b>{dashboard.orders.length}</b></div><div className="account-data-table orders"><div className="table-head"><span>时间</span><span>品种</span><span>方向</span><span>类型</span><span>成交 / 委托</span><span>价格</span><span>状态</span><span>操作</span></div>{dashboard.orders.map(order => <article key={order.id}><time>{new Date(order.created_at).toLocaleString('zh-CN', { hour12: false })}</time><strong>{order.symbol}</strong><b className={order.side === 'buy' ? 'positive' : 'negative'}>{order.side === 'buy' ? '买入' : '卖出'}</b><span>{order.order_type === 'market' ? '市价' : '限价'}</span><span>{order.filled_quantity} / {order.quantity}</span><span>{order.average_fill_price ? money.format(Number(order.average_fill_price)) : order.limit_price ? money.format(Number(order.limit_price)) : '市价'}</span><b>{order.status}</b><span>{['pending', 'open', 'partially_filled'].includes(order.status) ? <button disabled={busy} onClick={() => onCancelOrder(order.id, order.symbol)}>撤单</button> : '—'}</span></article>)}{!dashboard.orders.length && <p className="ops-empty">还没有订单。</p>}</div></section>
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
    <div className="risk-workspace-layout"><section><div className="panel-title"><span>预交易检查</span><b>{dashboard.risk.approved ? 'PASS' : 'HOLD'}</b></div><div className="risk-rule-list">{dashboard.risk.checks.map(check => <article key={check.code} className={check.passed ? 'passed' : 'failed'}><i>{check.passed ? '✓' : '×'}</i><div><strong>{check.code.replaceAll('_', ' ')}</strong><span>{check.message}</span></div><b>{check.blocking === false ? '提示' : '硬门禁'}</b></article>)}</div></section><aside><div className="panel-title"><span>执行链路</span><RadioTower /></div><dl className="risk-runtime-facts"><div><dt>订单对账</dt><dd>{dashboard.system.reconciliation ? `${dashboard.system.reconciliation.confirmed} 已确认` : '读取中'}</dd></div><div><dt>不确定订单</dt><dd>{dashboard.system.reconciliation?.uncertain_order_ids.length ?? 0}</dd></div><div><dt>外部仓位</dt><dd>{dashboard.system.reconciliation?.unknown_position_symbols.length ?? 0}</dd></div><div><dt>缺少保护</dt><dd>{dashboard.system.reconciliation?.unprotected_order_ids.length ?? 0}</dd></div><div><dt>行情延迟</dt><dd>{dashboard.system.data_latency_ms} ms</dd></div><div><dt>风险来源</dt><dd>{dashboard.system.risk_state.source}</dd></div></dl></aside></div>
  </section>
}
