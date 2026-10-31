import LearningPanel from './LearningPanel'
import { ArrowUpRight, Clock3, Layers3 } from 'lucide-react'
import { describeWakeCondition, entryStatusLabel, isActualPosition, isPendingEntry, latestEntryOrders, wakeActionLabel } from './api'
import type { EntryOrderRuntime, MarketStateV2, TradingIntelligenceOverview } from './types'

const amount = (value: number) => Number.isFinite(value)
  ? value.toLocaleString('en-US', { maximumFractionDigits: 2 }) : '—'
const quantityAmount = (value: string) => Number(value).toLocaleString('en-US', {
  maximumFractionDigits: 8,
})
const priceAmount = (value?: string | null) => value == null ? '—' : Number(value).toLocaleString('en-US', { maximumFractionDigits: 8 })
const timestamp = (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false })

function EntryOrdersTable({ entries, onSelect }: {
  entries: EntryOrderRuntime[]
  onSelect: (symbol: MarketStateV2['symbol'], planId?: string, entryId?: string) => void
}) {
  return <div className="ti-table-scroll"><table className="ti-position-table ti-entry-table"><thead><tr>
    <th>合约 / 方向</th><th>类型 / 状态</th><th>实际委托价</th><th>已成交 / 委托量</th><th>成交均价</th><th>参考价 / 有效范围</th><th>有效期</th><th>最新原因 / 更新时间</th><th>关联记录</th>
  </tr></thead><tbody>{entries.map(entry => <tr key={entry.entry_id}>
    <td><button onClick={() => onSelect(entry.symbol, entry.plan_id, entry.entry_id)}>{entry.symbol.split('/')[0]} <span className={entry.side === 'LONG' ? 'positive' : 'negative'}>{entry.side === 'LONG' ? '多' : '空'}</span><ArrowUpRight /></button></td>
    <td><span>{entry.order_type === 'LIMIT' ? '限价' : '市价'}</span><small className={`ti-entry-status ${entry.status.toLowerCase()}`}>{entryStatusLabel(entry.status)}</small></td>
    <td>{entry.order_type === 'MARKET' ? '市价' : priceAmount(entry.submitted_price)}<small>请求价 {priceAmount(entry.requested_price)}</small></td>
    <td>{quantityAmount(entry.filled_quantity)} / {quantityAmount(entry.quantity)}</td>
    <td>{Number(entry.filled_quantity) > 0 ? priceAmount(entry.average_fill_price) : '—'}</td>
    <td>{priceAmount(entry.reference_price)}<small>{entry.valid_price_range ? `${priceAmount(entry.valid_price_range.lower)} – ${priceAmount(entry.valid_price_range.upper)}` : '未提供有效范围'}</small></td>
    <td>{timestamp(entry.expires_at)}{isPendingEntry(entry) && Date.parse(entry.updated_at) >= Date.parse(entry.expires_at) && <small>已过期 · 等待订单状态确认</small>}</td>
    <td className="ti-entry-reason">{entry.reason || '未提供原因'}<small>{timestamp(entry.updated_at)}</small></td>
    <td><details><summary>{entry.order_ids.length} 个订单 · 关联计划</summary><small>入场 {entry.entry_id}</small><small>计划 {entry.plan_id}</small><small>决策 {entry.episode_id}</small><small>持仓 {entry.position_id ?? '尚未关联'}</small><small>订单 {entry.order_ids.join(' / ') || '尚无交易所订单编号'}</small><small>创建 {timestamp(entry.created_at)}</small></details></td>
  </tr>)}</tbody></table></div>
}

export default function TradingOperationsPanel({ overview, view, onSelect }: {
  overview: TradingIntelligenceOverview | null
  view: 'positions' | 'entries' | 'waits' | 'learning'
  onSelect: (symbol: MarketStateV2['symbol'], planId?: string, entryId?: string) => void
}) {
  if (view === 'entries') {
    const entries = latestEntryOrders(overview?.entry_orders)
    const pending = entries.filter(isPendingEntry)
    const settled = entries.filter(entry => !isPendingEntry(entry))
    return <section className="ti-positions-plane" aria-label="入场订单">
      <header className="ti-learning-heading"><div><Layers3 /><strong>待成交入场订单</strong><span>提交、成交和撤单状态以最新订单记录为准</span></div><small>{pending.length} 笔待处理</small></header>
      {pending.length ? <EntryOrdersTable entries={pending} onSelect={onSelect} /> : <div className="ti-operations-empty"><Layers3 /><strong>{!overview ? '正在读取订单' : overview.entry_orders == null ? '服务尚未提供入场订单明细' : '当前没有待成交入场订单'}</strong><span>等待策略不会计为挂单。部分成交的已成交数量在持仓中展示。</span></div>}
      {settled.length > 0 && <><header className="ti-learning-heading"><div><strong>已结束订单与影子记录</strong></div><small>{settled.length} 笔</small></header><EntryOrdersTable entries={settled} onSelect={onSelect} /></>}
    </section>
  }
  if (view === 'waits') {
    const waits = overview?.active_waits.filter(wait => wait.status === 'ACTIVE') ?? []
    return <section className="ti-positions-plane" aria-label="等待策略">
      <header className="ti-learning-heading"><div><Clock3 /><strong>等待策略</strong><span>满足条件后执行对应动作</span></div><small>{waits.length} 个等待策略</small></header>
      {waits.length ? <div className="ti-wait-list">{waits.map(wait => <article key={wait.wait_runtime_id}>
        <header><button onClick={() => onSelect(wait.symbol)}>{wait.symbol.split('/')[0]} <ArrowUpRight /></button><small>有效至 {timestamp(wait.wait_plan.expires_at)}</small></header>
        <p>{wait.wait_plan.reason}</p>
        <ul>{wait.wait_plan.wake_conditions.map(condition => <li key={condition.condition_id}><span>{describeWakeCondition(condition)}</span><strong>{wakeActionLabel(condition.action)}</strong></li>)}</ul>
      </article>)}</div> : <div className="ti-operations-empty"><Clock3 /><strong>{overview ? '当前没有等待策略' : '正在读取等待策略'}</strong><span>等待条件与实际入场委托分别记录。</span></div>}
    </section>
  }
  if (view === 'learning') return <LearningPanel />
  const positions = overview?.positions.filter(isActualPosition) ?? []
  return <section className="ti-positions-plane" aria-label="组合持仓">
    <header className="ti-learning-heading"><div><Layers3 /><strong>全部品种持仓</strong><span>仅已成交且尚未平仓数量 · 同向加仓关联多个计划</span></div><small>{positions.length} / {overview?.portfolio_policy?.max_open_positions ?? '—'} 仓位</small></header>
    {positions.length ? <div className="ti-table-scroll"><table className="ti-position-table"><thead><tr><th>合约 / 方向</th><th>状态 / 关联计划</th><th>剩余 / 累计成交量</th><th>入场均价</th><th>最新价</th><th>浮动盈亏</th><th>保护止损</th><th>至止损风险</th><th>止盈进度</th></tr></thead><tbody>{positions.map(position => {
      const market = overview?.market_states.find(item => item.symbol === position.symbol)
      const rawMark = market?.market.values.last
      const mark = rawMark == null ? NaN : Number(rawMark)
      const quantity = Number(position.remaining_quantity)
      const direction = position.side === 'LONG' ? 1 : -1
      const pnl = (mark - Number(position.average_entry)) * quantity * direction
      const stopRisk = Math.max((mark - Number(position.current_stop)) * direction, 0) * quantity
      const planIds = [...new Set([position.plan_id, ...(position.plan_ids ?? [])])]
      return <tr key={position.position_id}><td><button onClick={() => onSelect(position.symbol, position.plan_id)}>{position.symbol.split('/')[0]} <span className={direction === 1 ? 'positive' : 'negative'}>{position.side === 'LONG' ? '多' : '空'}</span><ArrowUpRight /></button></td><td>{position.state}<small title={planIds.join('\n')}>{planIds.length} 个计划 · v{position.version ?? '—'}</small></td><td>{quantityAmount(position.remaining_quantity)} / {quantityAmount(position.original_quantity)}</td><td>{priceAmount(position.average_entry)}</td><td>{amount(mark)}</td><td className={pnl >= 0 ? 'positive' : 'negative'}>{quantity ? amount(pnl) : '—'}</td><td>{priceAmount(position.current_stop)}</td><td>{amount(stopRisk)} USDT</td><td>{position.targets_reached} 档</td></tr>
    })}</tbody></table></div> : <div className="ti-operations-empty"><Layers3 /><strong>{overview ? '当前组合空仓' : '正在读取组合状态'}</strong><span>BTC、ETH、SOL 分别运行独立决策，触发后共享账户风险预算。</span></div>}
  </section>
}
