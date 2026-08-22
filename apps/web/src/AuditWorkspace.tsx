import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, Download, RefreshCw, Search } from 'lucide-react'
import { getAuditEvents } from './api'
import type { AuditEvent } from './types'

export default function AuditWorkspace() {
  const [items, setItems] = useState<AuditEvent[]>([])
  const [cursor, setCursor] = useState<number | null>(null)
  const [category, setCategory] = useState('')
  const [severity, setSeverity] = useState('')
  const [search, setSearch] = useState('')
  const [appliedSearch, setAppliedSearch] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback(async (append = false, nextCursor?: number | null) => {
    setBusy(true); setError('')
    try {
      const result = await getAuditEvents({
        limit: 60, cursor: nextCursor ?? undefined,
        category: category || undefined, severity: severity || undefined,
        search: appliedSearch || undefined,
      })
      setItems(current => append ? [...current, ...result.items] : result.items)
      setCursor(result.next_cursor ?? null)
    } catch (cause) { setError(cause instanceof Error ? cause.message : '日志读取失败') }
    finally { setBusy(false) }
  }, [appliedSearch, category, severity])

  useEffect(() => {
    const kickoff = window.setTimeout(() => void load(), 0)
    return () => window.clearTimeout(kickoff)
  }, [load])

  const submitSearch = () => setAppliedSearch(search.trim())
  const exportCurrent = () => {
    const content = items.map(item => `${item.timestamp}\t${item.severity}\t${item.category}\t${item.message}`).join('\n')
    const url = URL.createObjectURL(new Blob([content], { type: 'text/plain;charset=utf-8' }))
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `cosmos-audit-${new Date().toISOString().slice(0, 10)}.txt`
    anchor.click()
    URL.revokeObjectURL(url)
  }

  return <section className="ops-page audit-workspace-page">
    <header className="ops-page-header"><div><span className="ops-kicker">系统证据流</span><h1>运行日志</h1><p>独立查看策略、Agent、风控和执行事件，不再与研究工作台混排。</p></div><div className="header-actions"><button className="button ghost" onClick={exportCurrent} disabled={!items.length}><Download />导出当前结果</button><button className="button primary" disabled={busy} onClick={() => void load()}><RefreshCw />刷新</button></div></header>
    <div className="audit-filterbar"><div className="search-field"><Search /><input value={search} onChange={event => setSearch(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') submitSearch() }} placeholder="搜索事件、策略 ID 或 Agent ID" /><button onClick={submitSearch}>搜索</button></div><select value={category} onChange={event => setCategory(event.target.value)}><option value="">全部类别</option><option value="agent">Agent</option><option value="strategy">策略</option><option value="risk">风控</option><option value="execution">执行</option><option value="research">研究</option><option value="ai">AI 服务</option></select><select value={severity} onChange={event => setSeverity(event.target.value)}><option value="">全部级别</option><option value="info">信息</option><option value="warning">警告</option><option value="critical">严重</option></select><span>{items.length} 条已加载</span></div>
    {error && <div className="audit-error"><AlertTriangle />{error}</div>}
    <div className="audit-ledger"><div className="table-head"><span>时间</span><span>级别</span><span>类别</span><span>事件</span><span>关联数据</span></div>{items.map(item => <article key={item.id}><time>{new Date(item.timestamp).toLocaleString('zh-CN', { hour12: false })}</time><b className={item.severity}>{item.severity === 'critical' ? '严重' : item.severity === 'warning' ? '警告' : '信息'}</b><span>{item.category}</span><strong>{item.message}</strong><details><summary>{Object.keys(item.data ?? {}).length ? '查看关联数据' : '无关联数据'}</summary><dl>{Object.entries(item.data ?? {}).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{String(value)}</dd></div>)}</dl></details></article>)}{!items.length && !busy && <p className="ops-empty">没有符合条件的审计事件。</p>}</div>
    {cursor && <button className="audit-load-more" disabled={busy} onClick={() => void load(true, cursor)}>{busy ? '加载中…' : '加载更早日志'}</button>}
  </section>
}
