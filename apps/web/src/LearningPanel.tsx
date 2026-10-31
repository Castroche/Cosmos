import { useEffect, useState } from 'react'
import { getLearningData, updateLearningSettings } from './api'
import type { LearningDecision, LearningSettings, LearningProxy } from './types'

type LearningData = Awaited<ReturnType<typeof getLearningData>>
const statusLabels: Record<string, string> = { COMPLETED: '已完成', QUEUED: '等待对照', RUNNING: '对照中', FAILED: '对照失败', SKIPPED: '已跳过', CANCELLED: '已取消', INTERRUPTED: '已中断' }
const actionLabels: Record<string, string> = { LONG: '做多', SHORT: '做空', WAIT: '等待', NO_TRADE: '无交易', EXIT: '退出', MANAGE_EXISTING: '管理持仓' }
const percent = (n?: number | null) => n == null ? '—' : `${(n * 100).toFixed(2)}%`
function decisionText(d?: LearningDecision | null) {
  if (!d) return '尚无结果'
  const plan = d.trade_plan
  return `${actionLabels[d.action] ?? d.action}${plan ? ` · 入场 ${plan.limit_price ?? plan.reference_price ?? '—'} · 止损 ${plan.management?.hard_stop ?? '—'}` : ''}`
}
function proxyText(p?: LearningProxy) {
  if (!p || p.status !== 'COMPLETE') return p?.status === 'NOT_DIRECTIONAL' ? '非方向建议，不模拟成交' : '等待完整价格证据'
  return `方向收益 ${percent(p.direction_return)} · MFE ${percent(p.mfe)} · MAE ${percent(p.mae)}`
}

export default function LearningPanel() {
  const [data, setData] = useState<LearningData | null>(null)
  const [error, setError] = useState('')
  const [revision, setRevision] = useState(0)
  const [saving, setSaving] = useState(false)
  useEffect(() => {
    if (saving) return
    const controller = new AbortController()
    let running = false
    async function load() {
      if (running) return
      running = true
      try {
        const next = await getLearningData(controller.signal)
        if (!controller.signal.aborted) { setData(next); setError('') }
      } catch (e) {
        if (!controller.signal.aborted) setError(e instanceof Error ? e.message : '学习数据暂不可用')
      } finally { running = false }
    }
    void load()
    const timer = window.setInterval(() => { void load() }, 30_000)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [revision, saving])

  async function save(next: LearningSettings) {
    const previous = data?.[1]
    setSaving(true)
    setData(old => old ? [old[0], next, old[2], old[3], old[4]] : old)
    try {
      const settings = await updateLearningSettings(next)
      setData(old => old ? [old[0], settings, old[2], old[3], old[4]] : old)
      setRevision(n => n + 1)
    } catch (e) {
      if (previous) setData(old => old ? [old[0], previous, old[2], old[3], old[4]] : old)
      setError(e instanceof Error ? e.message : '设置保存失败')
    }
    finally { setSaving(false) }
  }

  return <section className="ti-learning-plane learning-upgrade" aria-label="交易学习">
    <header className="ti-learning-heading"><div><strong>可验证的交易经验</strong><span>历史案例辅助决策 · 独立影子对照</span></div><button className="button" onClick={() => setRevision(n => n + 1)}>刷新</button></header>
    {error && <p className="learning-error" role="alert">{error}。保留上次读取结果，可点击刷新重试。</p>}
    {!data ? <p role="status">{error ? '当前没有可显示的数据。' : '正在读取经验与对照证据…'}</p> : (() => {
      const [snapshot, settings, versions, summary, comparisons] = data
      return <>
        <div className="learning-controls">
          <label><input type="checkbox" checked={settings.enabled} disabled={saving} onChange={e => { void save({ ...settings, enabled: e.target.checked }) }} />启用学习辅助</label>
          <label><input type="checkbox" checked={settings.shadow_enabled} disabled={saving} onChange={e => { void save({ ...settings, shadow_enabled: e.target.checked }) }} />在线影子对照</label>
          <label>经验版本<select value={settings.pinned_version ?? ''} disabled={saving} onChange={e => { void save({ ...settings, pinned_version: e.target.value || null }) }}><option value="">自动更新</option>{settings.pinned_version && !versions.items.some(v => v.version === settings.pinned_version) && <option value={settings.pinned_version}>{settings.pinned_version.slice(0, 8)} · 已固定</option>}{versions.items.map(v => <option key={v.version} value={v.version}>{v.version.slice(0, 8)} · {v.sample_count} 笔</option>)}</select></label>
        </div>
        <div className="learning-metrics">
          <article><span>有效交易经验</span><strong>{snapshot.sample_count}</strong><small>{snapshot.skipped_count} 条排除 · 完整归因后可用</small></article>
          <article><span>今日对照额度</span><strong>{summary.daily_used} / {summary.daily_limit}</strong><small>10% 抽样 · 上海时间每日重置</small></article>
          <article><span>完成对照</span><strong>{summary.completed}</strong><small>{summary.completed < 30 ? '样本不足，尚不能评估效果' : '观察性证据，不证明因果收益'}</small></article>
          <article><span>格式有效率</span><strong>{percent(summary.format_valid_rate)}</strong><small>费用：{summary.cost == null ? '未知，以厂商账单为准' : summary.cost}</small></article>
        </div>
        <p className="ti-learning-note">对照建议不下单，也不进入成交经验库。价格代理指标不含成交、滑点或费用，不代表交易利润。学习不会自动修改风控或晋升策略。</p>
        <h3>经验质量与案例</h3>
        {snapshot.records?.length ? <div className="learning-case-grid">{snapshot.records.slice(0, 5).map(r => <article key={r.campaign_id}><strong>{r.symbol} · {r.side} · {r.mode}</strong><p>{r.regime} · 已实现结果 {r.realized_pnl} USDT</p><small>首次可用：{new Date(r.available_at).toLocaleString('zh-CN')}</small><code>{r.episode_id}</code></article>)}</div> : <p className="ti-operations-empty">等待完整成交证据；历史快照缺少时间证据时不会进入决策检索。</p>}
        {!!snapshot.excluded?.length && <details><summary>查看排除原因（{snapshot.excluded.length}）</summary>{snapshot.excluded.map((r, i) => <p key={`${r.episode_id}-${i}`}><code>{r.episode_id}</code> · {r.reason}</p>)}</details>}
        <h3>有学习 / 无学习对照</h3>
        {!comparisons.items.length ? <p className="ti-operations-empty">尚无对照记录。启用自动化且存在有效经验后，系统按额度抽样。</p> : <div className="learning-comparisons">{comparisons.items.map(c => <article key={c.decision_id}>
          <header><strong>{c.payload.symbol} · {c.payload.mode}</strong><span>{statusLabels[c.status] ?? c.status}</span></header>
          <div className="learning-pair"><div><small>有学习</small><p>{decisionText(c.payload.primary)}</p><small>{c.payload.primary_usage?.total_tokens ?? '—'} tokens · {c.payload.primary_latency_ms ?? '—'} ms</small><p>{proxyText(c.payload.proxy.primary)}</p></div><div><small>无学习 · 影子建议</small><p>{decisionText(c.payload.control)}</p><small>{c.payload.control_usage?.reduce((n, x) => n + x.total_tokens, 0) ?? '—'} tokens · {c.payload.control_latency_ms ?? '—'} ms</small><p>{c.payload.proxy.status === 'UNAVAILABLE' ? '价格证据不可评估' : proxyText(c.payload.proxy.control)}</p></div></div>
          {c.payload.skip_reason && <p>跳过原因：{c.payload.skip_reason}</p>}{c.payload.detail && <p>{c.payload.detail}</p>}
          <details><summary>引用证据 · {c.payload.case_ids.length} 个案例</summary><p>学习版本：{c.payload.learning_version ?? '无'}</p>{c.payload.case_ids.map(id => <code key={id}>{id}</code>)}<code>{c.decision_id}</code></details>
        </article>)}</div>}
      </>
    })()}
  </section>
}
