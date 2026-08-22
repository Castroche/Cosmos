import { useCallback, useEffect, useMemo, useState } from 'react'
import { Archive, Copy, Eye, EyeOff, Filter, Pencil, Plus, Search, Trash2, X } from 'lucide-react'
import {
  archiveStrategyLibraryItem,
  createPersonalStrategy,
  deletePersonalStrategy,
  getStrategyLibrary,
  getStrategySpecContract,
  restoreStrategyLibraryItem,
  updatePersonalStrategy,
} from './api'
import type { StrategyLibraryItem } from './types'

type OriginFilter = 'all' | StrategyLibraryItem['origin']

const originLabel = { builtin: '内置', personal: '个人', agent: 'Agent' }
const familyLabel: Record<string, string> = { trend: '趋势', mean_reversion: '均值回归', breakout: '突破' }

function parameterValue(spec: Record<string, unknown>, key: string) {
  const parameters = spec.parameters as Record<string, unknown> | undefined
  return parameters?.[key] == null ? '' : String(parameters[key])
}

export default function StrategyLibraryWorkspace() {
  const [items, setItems] = useState<StrategyLibraryItem[]>([])
  const [defaults, setDefaults] = useState<Record<string, Record<string, unknown>>>({})
  const [origin, setOrigin] = useState<OriginFilter>('all')
  const [family, setFamily] = useState('all')
  const [search, setSearch] = useState('')
  const [showHidden, setShowHidden] = useState(false)
  const [selected, setSelected] = useState<StrategyLibraryItem | null>(null)
  const [editorOpen, setEditorOpen] = useState(false)
  const [editingId, setEditingId] = useState('')
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [editorFamily, setEditorFamily] = useState('trend')
  const [spec, setSpec] = useState<Record<string, unknown>>({})
  const [advanced, setAdvanced] = useState('')
  const [busy, setBusy] = useState('')
  const [message, setMessage] = useState('')

  const refresh = useCallback(async () => {
    try {
      const result = await getStrategyLibrary({ includeHidden: true })
      setItems(result.items)
    } catch (cause) {
      setMessage(cause instanceof Error ? cause.message : '策略库读取失败')
    }
  }, [])

  useEffect(() => {
    const kickoff = window.setTimeout(() => {
      void Promise.all([refresh(), getStrategySpecContract().then(result => setDefaults(result.defaults))])
    }, 0)
    return () => window.clearTimeout(kickoff)
  }, [refresh])

  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase()
    return items.filter(item => (origin === 'all' || item.origin === origin)
      && (family === 'all' || item.family === family)
      && (showHidden || !item.hidden)
      && (!needle || item.name.toLowerCase().includes(needle) || item.id.toLowerCase().includes(needle) || item.description.toLowerCase().includes(needle)))
  }, [family, items, origin, search, showHidden])

  const counts = useMemo(() => ({
    total: items.filter(item => !item.hidden).length,
    personal: items.filter(item => item.origin === 'personal' && !item.hidden).length,
    agent: items.filter(item => item.origin === 'agent' && !item.hidden).length,
    certified: items.filter(item => item.status === 'champion').length,
  }), [items])

  const openNew = (source?: StrategyLibraryItem) => {
    const nextFamily = source?.family ?? 'trend'
    const nextSpec = source?.strategy_spec ?? defaults[nextFamily] ?? {}
    setEditingId('')
    setName(source ? `${source.name} · 个人副本` : '我的新策略')
    setDescription(source?.description ?? '')
    setEditorFamily(nextFamily)
    setSpec(nextSpec)
    setAdvanced(JSON.stringify(nextSpec, null, 2))
    setEditorOpen(true)
  }

  const openEdit = (item: StrategyLibraryItem) => {
    setEditingId(item.id)
    setName(item.name)
    setDescription(item.description)
    setEditorFamily(item.family)
    setSpec(item.strategy_spec)
    setAdvanced(JSON.stringify(item.strategy_spec, null, 2))
    setEditorOpen(true)
  }

  const changeFamily = (next: string) => {
    const nextSpec = defaults[next] ?? { family: next, source: 'operator', hypothesis: description, parameters: {} }
    setEditorFamily(next)
    setSpec(nextSpec)
    setAdvanced(JSON.stringify(nextSpec, null, 2))
  }

  const setParameter = (key: string, value: string) => {
    const parameters = { ...((spec.parameters as Record<string, unknown> | undefined) ?? {}) }
    parameters[key] = value === '' ? null : value
    const next = { ...spec, family: editorFamily, source: 'operator', hypothesis: description || name, parameters }
    setSpec(next)
    setAdvanced(JSON.stringify(next, null, 2))
  }

  const applyAdvanced = () => {
    try {
      const parsed = JSON.parse(advanced) as Record<string, unknown>
      setSpec(parsed)
      setEditorFamily(String(parsed.family ?? editorFamily))
      setMessage('高级参数已应用，保存时将由 StrategySpec 再次校验')
    } catch {
      setMessage('JSON 格式无效')
    }
  }

  const save = async () => {
    setBusy('save'); setMessage('')
    try {
      const payload = { ...spec, family: editorFamily, source: 'operator', hypothesis: description || name }
      if (editingId) await updatePersonalStrategy(editingId, { name, description, strategy_spec: payload })
      else await createPersonalStrategy({ name, description, strategy_spec: payload })
      setEditorOpen(false)
      setMessage(editingId ? '个人策略已更新' : '个人策略已加入策略库')
      await refresh()
    } catch (cause) { setMessage(cause instanceof Error ? cause.message : '策略保存失败') }
    finally { setBusy('') }
  }

  const clone = async (item: StrategyLibraryItem) => {
    setBusy(item.id)
    try {
      await createPersonalStrategy({
        name: `${item.name} · 个人副本`, description: item.description,
        strategy_spec: item.strategy_spec, origin_ref: item.id,
      })
      setMessage('策略已复制到个人库')
      await refresh()
    } catch (cause) { setMessage(cause instanceof Error ? cause.message : '复制失败') }
    finally { setBusy('') }
  }

  const archive = async (item: StrategyLibraryItem) => {
    const verb = item.origin === 'builtin' ? '隐藏' : '归档'
    if (!window.confirm(`确认${verb}策略“${item.name}”？\n\n${item.origin === 'builtin' ? '内置策略不会被删除，可随时重新显示。' : '历史回测和交易证据会继续保留。'}`)) return
    setBusy(item.id)
    try { await archiveStrategyLibraryItem(item.id); setSelected(null); await refresh() }
    catch (cause) { setMessage(cause instanceof Error ? cause.message : `${verb}失败`) }
    finally { setBusy('') }
  }

  const restore = async (item: StrategyLibraryItem) => {
    setBusy(item.id)
    try { await restoreStrategyLibraryItem(item.id); await refresh() }
    catch (cause) { setMessage(cause instanceof Error ? cause.message : '恢复失败') }
    finally { setBusy('') }
  }

  const remove = async (item: StrategyLibraryItem) => {
    if (!window.confirm(`永久删除未引用草稿“${item.name}”？\n\n此操作不可恢复。`)) return
    setBusy(item.id)
    try { await deletePersonalStrategy(item.id); setSelected(null); await refresh() }
    catch (cause) { setMessage(cause instanceof Error ? cause.message : '删除失败') }
    finally { setBusy('') }
  }

  return <section className="ops-page strategy-library-page">
    <header className="ops-page-header"><div><span className="ops-kicker">统一策略资产</span><h1>策略库</h1><p>管理内置先验、个人策略和 Agent 研究候选；执行资格与研究证据始终分开。</p></div><button className="button primary" onClick={() => openNew()}><Plus />新建个人策略</button></header>
    <div className="library-status-strip"><div><span>可见策略</span><strong>{counts.total}</strong></div><div><span>个人策略</span><strong>{counts.personal}</strong></div><div><span>Agent 候选</span><strong>{counts.agent}</strong></div><div><span>Champion</span><strong>{counts.certified}</strong></div></div>
    <div className="library-filterbar"><div className="search-field"><Search /><input value={search} onChange={event => setSearch(event.target.value)} placeholder="搜索名称、ID 或描述" /></div><label><Filter /><select value={origin} onChange={event => setOrigin(event.target.value as OriginFilter)}><option value="all">全部来源</option><option value="personal">个人策略</option><option value="agent">Agent 策略</option><option value="builtin">内置策略</option></select></label><label><select value={family} onChange={event => setFamily(event.target.value)}><option value="all">全部策略族</option><option value="trend">趋势</option><option value="mean_reversion">均值回归</option><option value="breakout">突破</option></select></label><label className="hidden-toggle"><input type="checkbox" checked={showHidden} onChange={event => setShowHidden(event.target.checked)} />显示已归档/隐藏</label><span>{filtered.length} 项</span></div>
    <div className="library-table"><div className="table-head"><span>策略</span><span>来源</span><span>策略族</span><span>状态</span><span>验证证据</span><span>更新时间</span><span>操作</span></div>{filtered.map(item => <article key={`${item.origin}-${item.id}`} className={item.hidden || item.status === 'archived' || item.status === 'retired' ? 'muted' : ''}><button className="library-identity" onClick={() => setSelected(item)}><strong>{item.name}</strong><small>{item.id} · v{item.version} · {item.spec_fingerprint}</small></button><span className={`origin ${item.origin}`}>{originLabel[item.origin]}</span><span>{familyLabel[item.family] ?? item.family}</span><b className={item.status}>{item.hidden ? 'hidden' : item.status}</b><span className="evidence-copy">{item.evidence_summary}</span><time>{item.updated_at ? new Date(item.updated_at).toLocaleDateString('zh-CN') : '内置版本'}</time><div className="row-actions">{item.can_edit && <button title="编辑" onClick={() => openEdit(item)}><Pencil /></button>}<button title="复制到个人库" disabled={busy === item.id} onClick={() => void clone(item)}><Copy /></button>{item.can_archive && <button title={item.origin === 'builtin' ? '隐藏' : '归档'} disabled={busy === item.id} onClick={() => void archive(item)}>{item.origin === 'builtin' ? <EyeOff /> : <Archive />}</button>}{item.can_restore && <button title="恢复" disabled={busy === item.id} onClick={() => void restore(item)}><Eye /></button>}{item.can_delete && <button className="danger" title="永久删除草稿" disabled={busy === item.id} onClick={() => void remove(item)}><Trash2 /></button>}</div></article>)}{!filtered.length && <p className="ops-empty">没有符合筛选条件的策略。</p>}</div>
    {selected && <div className="detail-drawer-backdrop" onMouseDown={() => setSelected(null)}><aside className="detail-drawer strategy-detail-drawer" onMouseDown={event => event.stopPropagation()}><header><div><span>{originLabel[selected.origin]}策略</span><strong>{selected.name}</strong></div><button onClick={() => setSelected(null)}><X /></button></header><div className="strategy-detail-summary"><p>{selected.description}</p><dl><div><dt>策略族</dt><dd>{familyLabel[selected.family] ?? selected.family}</dd></div><div><dt>状态</dt><dd>{selected.status}</dd></div><div><dt>规格指纹</dt><dd>{selected.spec_fingerprint}</dd></div><div><dt>执行资格</dt><dd>{selected.executable ? '已编译' : '不可直接执行'}</dd></div></dl></div><details open><summary>常用参数</summary><div className="parameter-list">{Object.entries((selected.strategy_spec.parameters as Record<string, unknown> | undefined) ?? {}).slice(0, 20).map(([key, value]) => <div key={key}><span>{key}</span><strong>{String(value)}</strong></div>)}</div></details><details><summary>完整 StrategySpec</summary><pre>{JSON.stringify(selected.strategy_spec, null, 2)}</pre></details></aside></div>}
    {editorOpen && <div className="editor-overlay"><section className="strategy-editor-panel"><header><div><span className="ops-kicker">StrategySpec 编辑器</span><h2>{editingId ? '编辑个人策略' : '创建个人策略'}</h2></div><button onClick={() => setEditorOpen(false)}><X /></button></header><div className="editor-form"><label><span>策略名称</span><input value={name} onChange={event => setName(event.target.value)} /></label><label><span>策略族</span><select value={editorFamily} onChange={event => changeFamily(event.target.value)}><option value="trend">趋势</option><option value="mean_reversion">均值回归</option><option value="breakout">突破</option></select></label><label className="wide"><span>研究假设</span><textarea rows={3} value={description} onChange={event => setDescription(event.target.value)} /></label><div className="editor-section-title"><strong>常用参数</strong><span>复杂字段可在高级参数中编辑</span></div>{['mean_window', 'stop_atr', 'maximum_holding_bars', 'first_target_reward_risk', 'trailing_stop_atr'].map(key => <label key={key}><span>{key}</span><input value={parameterValue(spec, key)} onChange={event => setParameter(key, event.target.value)} placeholder="使用策略默认值" /></label>)}<details className="advanced-editor wide"><summary>高级 StrategySpec JSON</summary><textarea rows={16} value={advanced} onChange={event => setAdvanced(event.target.value)} /><button className="button ghost" onClick={applyAdvanced}>应用高级参数</button></details></div><footer><span>保存前由后端执行完整字段范围和交叉参数校验。</span><div><button className="button ghost" onClick={() => setEditorOpen(false)}>取消</button><button className="button primary" disabled={busy === 'save' || name.trim().length < 2} onClick={() => void save()}>{busy === 'save' ? '校验并保存…' : '保存到个人策略库'}</button></div></footer></section></div>}
    {message && <div className="ops-toast">{message}<button onClick={() => setMessage('')}><X /></button></div>}
  </section>
}
