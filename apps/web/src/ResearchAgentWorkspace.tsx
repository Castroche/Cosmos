import { useCallback, useEffect, useMemo, useState } from 'react'
import { Beaker, BrainCircuit, Database, RefreshCw, Search, ShieldCheck } from 'lucide-react'
import {
  createAIResearchCandidate,
  createMLChallenger,
  getAIHealth,
  getResearchCandidates,
  getResearchDatasets,
  getResearchDrift,
  getCrypto5mDataContract,
  getDevelopmentAudits,
  getSandboxAdmissions,
} from './api'
import type {
  ConfigurationView,
  AIHealth,
  DriftReport,
  ResearchDataset,
  StrategyCandidate,
  Crypto5mDataContract,
  DevelopmentAuditView,
  SandboxAdmissionView,
} from './types'

type ResearchTab = 'overview' | 'candidates' | 'diagnostics'

function pct(value: string | number | undefined) {
  const number = Number(value ?? 0)
  return `${number > 0 ? '+' : ''}${number.toFixed(2)}%`
}

export default function ResearchAgentWorkspace({ settings, active = true }: { settings: ConfigurationView; active?: boolean }) {
  const [tab, setTab] = useState<ResearchTab>('overview')
  const [candidates, setCandidates] = useState<StrategyCandidate[]>([])
  const [datasets, setDatasets] = useState<ResearchDataset[]>([])
  const [drift, setDrift] = useState<DriftReport | null>(null)
  const [search, setSearch] = useState('')
  const [busy, setBusy] = useState('')
  const [message, setMessage] = useState('')
  const [aiHealth, setAIHealth] = useState<AIHealth | null>(null)
  const [dataContract, setDataContract] = useState<Crypto5mDataContract | null>(null)
  const [developmentAudits, setDevelopmentAudits] = useState<DevelopmentAuditView[]>([])
  const [sandboxAdmissions, setSandboxAdmissions] = useState<SandboxAdmissionView[]>([])

  const refresh = useCallback(async () => {
    try {
      const [nextCandidates, nextDatasets, nextDrift, nextAIHealth, nextContract, nextAudits, nextAdmissions] = await Promise.all([
        getResearchCandidates(), getResearchDatasets(), getResearchDrift(), getAIHealth(), getCrypto5mDataContract(), getDevelopmentAudits(), getSandboxAdmissions(),
      ])
      setCandidates(nextCandidates)
      setDatasets(nextDatasets)
      setDrift(nextDrift)
      setAIHealth(nextAIHealth)
      setDataContract(nextContract)
      setDevelopmentAudits(nextAudits)
      setSandboxAdmissions(nextAdmissions)
    } catch (cause) {
      setMessage(cause instanceof Error ? cause.message : '研究状态读取失败')
    }
  }, [])

  useEffect(() => {
    if (!active) return
    const kickoff = window.setTimeout(() => void refresh(), 0)
    return () => window.clearTimeout(kickoff)
  }, [active, refresh])

  const filteredCandidates = useMemo(() => {
    const needle = search.trim().toLowerCase()
    if (!needle) return candidates
    return candidates.filter(item => item.id.toLowerCase().includes(needle)
      || String(item.spec.family).toLowerCase().includes(needle)
      || item.source.toLowerCase().includes(needle))
  }, [candidates, search])
  const statusCounts = useMemo(() => candidates.reduce<Record<string, number>>((acc, item) => {
    acc[item.status] = (acc[item.status] ?? 0) + 1
    return acc
  }, {}), [candidates])
  const providerHealth = aiHealth ? Object.entries(aiHealth.providers)[0] : null
  const aiStatus = providerHealth ? `${providerHealth[0]} · ${providerHealth[1].status}` : aiHealth ? '未配置' : message ? '暂不可用' : '读取中'

  const generateAI = async () => {
    setBusy('ai'); setMessage('')
    try {
      await createAIResearchCandidate({
        hypothesis: '基于最新确定性市场状态，提出一个参数有界、可证伪且必须经过独立回测的策略候选',
        family: settings.configuration.strategy.default_kind,
        symbol: settings.configuration.default_symbol,
        timeframe: settings.configuration.default_timeframe,
      })
      setMessage('AI 候选已进入隔离研究库')
      await refresh()
    } catch (cause) { setMessage(cause instanceof Error ? cause.message : 'AI 候选生成失败') }
    finally { setBusy('') }
  }

  const generateML = async () => {
    setBusy('ml'); setMessage('')
    try { await createMLChallenger(); setMessage('ML Challenger 已生成'); await refresh() }
    catch (cause) { setMessage(cause instanceof Error ? cause.message : 'ML Challenger 生成失败') }
    finally { setBusy('') }
  }

  return <section className="ops-page research-agent-page">
    <header className="ops-page-header">
      <div><span className="ops-kicker">OFFLINE RESEARCH</span><h1>离线研究</h1><p>研究特征、Primitive、Wake/Entry Trigger 与 Quant；不参与实时开仓决策。</p></div>
      <div className="header-actions"><button className="button ghost" disabled={Boolean(busy)} onClick={() => void refresh()}><RefreshCw />刷新</button><button className="button ghost" disabled={Boolean(busy)} onClick={() => void generateML()}><Beaker />{busy === 'ml' ? '生成中…' : 'ML Challenger'}</button><button className="button primary" disabled={Boolean(busy)} onClick={() => void generateAI()}><BrainCircuit />{busy === 'ai' ? '生成中…' : '生成 AI 候选'}</button></div>
    </header>

    <section className="research-cycle-banner" aria-label="当前研究周期">
      <div><span>研究模式</span><strong>离线 / 按需</strong><small>不会成为实时开仓前置步骤</small></div>
      <div><span>研究数据</span><strong>{datasets.length ? 'ready' : 'unavailable'}</strong><small>{datasets.length ? `${datasets.length} 份不可变数据集` : '等待有效数据集'}</small></div>
      <div><span>AI 研究生成</span><strong className={providerHealth?.[1].status === 'ready' ? 'positive' : 'negative'}>{aiStatus}</strong><small>{providerHealth?.[1].status === 'ready' ? '仅在有效研究事件调用' : '已降级为本地候选，审核与交易继续运行'}</small></div>
      <div><span>运行语义</span><strong>显式作业</strong><small>无定时 Goal、WAIT 或自动扫描</small></div>
    </section>

    <div className="research-status-strip research-status-compact">
      <div><span>隔离候选</span><strong>{candidates.length}</strong><small>{statusCounts.champion ?? 0} Champion · {statusCounts.challenger ?? 0} Challenger</small></div>
      <div><span>不可变数据集</span><strong>{datasets.length}</strong><small>{datasets.filter(item => item.quality_status === 'healthy').length} 份健康</small></div>
      <div><span>策略漂移</span><strong className={drift?.status === 'stable' ? 'positive' : 'negative'}>{drift?.status ?? '读取中'}</strong><small>PSI {drift?.population_stability_index ?? '—'}</small></div>
      <div><span>5m 数据合同</span><strong className={dataContract?.status === 'READY' ? 'positive' : 'negative'}>{dataContract?.status ?? '读取中'}</strong><small>{dataContract?.markets.filter(item => item.status === 'READY').length ?? 0}/6 市场就绪</small></div>
    </div>

    <div className="subnav-tabs research-tabs">{([
      ['overview', '市场与生成'], ['candidates', '候选策略'], ['diagnostics', '技术诊断'],
    ] as Array<[ResearchTab, string]>).map(([key, label]) => <button key={key} className={tab === key ? 'active' : ''} onClick={() => setTab(key)}>{label}</button>)}</div>

    {tab === 'overview' && <div className="research-overview-layout">
      <section className="alpha-readiness-panel"><div className="panel-title"><span>两年 5m 数据就绪度</span><Database /></div><div className="alpha-market-readiness">{dataContract?.markets.map(item => <article key={item.market}><i className={item.status.toLowerCase()} /><strong>{item.market}</strong><span>{Number(item.days).toFixed(1)} 天</span><span>{(Number(item.completeness) * 100).toFixed(2)}%</span><small>{item.reasons[0] ?? '可用于冻结特征立方体'}</small></article>)}</div></section>
      <section><div className="panel-title"><span>假设与显式因子图</span><BrainCircuit /></div><div className="factor-graph-summary"><p>{candidates[0]?.spec.hypothesis ?? '等待一个可证伪经济假设；LLM 只能选择注册特征和有限 DSL。'}</p><div><b>注册特征</b><span>{candidates[0]?.research_context?.required_features.join(' → ') || 'trend → volatility → liquidity → funding → OI → basis → order flow'}</span></div><div><b>Canonical IR</b><span>七类状态门全部 AND 后才允许线性/GBDT 信号进入成本门</span></div></div></section>
      <section><div className="panel-title"><span>模型卡与成本归因</span><Beaker /></div><div className="model-card-list">{sandboxAdmissions.flatMap(item => item.certificate.model_artifact_ids.map((modelId, index) => <article key={modelId}><strong>{index === 0 ? 'Linear' : index === 1 ? 'LightGBM' : '50/50 Ensemble'}</strong><code>{modelId}</code><small>JSON 系数 / LightGBM 原生文本 · 指纹绑定依赖、标签与训练窗口</small></article>))}{!sandboxAdmissions.length && <p className="ops-empty">数据合同和开发审核通过后展示冻结模型卡；当前不会伪造模型工件。</p>}<div className="cost-attribution-copy">下一根 K 线入场 · 手续费 + 滑点 + 市场冲击 · 净优势必须大于 2× 往返成本 · 单品种 ≤2 次/日</div></div></section>
      <section><div className="panel-title"><span>候选漏斗与拒绝原因</span><ShieldCheck /></div><div className="candidate-funnel"><div><span>已生成</span><strong>{candidates.length}</strong></div><div><span>开发审核证书</span><strong>{developmentAudits.filter(item => !item.revoked).length}</strong></div><div><span>OKX 准入</span><strong>{sandboxAdmissions.filter(item => !item.revoked).length}</strong></div><div><span>完整证书</span><strong>{sandboxAdmissions.filter(item => item.progress.maturity.completed_episodes >= 30).length}</strong></div><p>{candidates.find(item => item.status === 'rejected')?.decision_reasons.slice(-2).join(' · ') ?? dataContract?.markets.find(item => item.status !== 'READY')?.reasons.join(' · ') ?? '尚无拒绝证据'}</p></div></section>
      <section className="sandbox-maturity-panel"><div className="panel-title"><span>OKX 模拟盘成熟进度</span><ShieldCheck /></div>{sandboxAdmissions.map(item => <article key={item.certificate.id}><div><strong>{item.certificate.id}</strong><span>{item.handoff?.status ?? '等待 handoff'}</span></div><progress max={30} value={item.progress.maturity.completed_episodes} /><small>{item.progress.maturity.completed_episodes}/30 回合 · {item.progress.maturity.independent_runs}/20 批次 · 最近 {item.progress.maturity.recent_episodes}/10 · PF {item.progress.maturity.profit_factor} · Expected R {item.progress.maturity.expected_r}</small></article>)}{!sandboxAdmissions.length && <p className="ops-empty">暂无准入证书，模拟盘不会新增风险。</p>}</section>
      <section><div className="panel-title"><span>研究流水线 · 与实时交易隔离</span><ShieldCheck /></div><div className="research-pipeline"><article className="done"><b>01</b><div><strong>本地市场摘要</strong><span>确定性指标、状态和覆盖缺口</span></div></article><article className={candidates.length ? 'done' : ''}><b>02</b><div><strong>研究候选生成</strong><span>一次结构化请求，失败仅修复一次</span></div></article><article className={candidates.length ? 'done' : ''}><b>03</b><div><strong>确定性验证</strong><span>本地编译、回测、比较和归因</span></div></article></div></section>
      <section><div className="panel-title"><span>最近产出</span><BrainCircuit /></div><div className="research-recent-list">{candidates.slice(0, 8).map(item => <article key={item.id}><i className={item.status} /><div><strong>{item.spec.family} · {item.source.toUpperCase()}</strong><span>{item.id}</span></div><b>{item.status}</b><small>{item.dataset_id ? `数据集 ${item.dataset_id}` : '等待绑定数据集'}</small></article>)}</div></section>
      <section><div className="panel-title"><span>当前阻塞</span><Database /></div>{!datasets.length && <p className="research-blocker">没有可用研究数据集。</p>}{drift && drift.status !== 'stable' && <p className="research-blocker">漂移状态为 {drift.status}，晋升前必须复核。</p>}{!(statusCounts.champion ?? 0) && <p className="research-blocker">当前没有通过治理的 Champion。</p>}{datasets.length > 0 && drift?.status === 'stable' && (statusCounts.champion ?? 0) > 0 && <p className="research-clear">研究主链路没有显式阻塞。</p>}</section>
    </div>}

    {tab === 'candidates' && <div className="candidate-workspace"><div className="workspace-filterbar"><div><Search /><input value={search} onChange={event => setSearch(event.target.value)} placeholder="搜索候选 ID、来源或策略族" /></div><span>显示 {filteredCandidates.length}/{candidates.length}</span></div><div className="candidate-table"><div className="table-head"><span>候选</span><span>来源</span><span>状态</span><span>收益</span><span>PF</span><span>最大回撤</span><span>数据集</span></div>{filteredCandidates.map(item => <article key={item.id}><div><strong>{String(item.spec.family)}</strong><small>{item.id}</small></div><span>{item.source.toUpperCase()}</span><b className={item.status}>{item.status}</b><span className={Number(item.backtest?.return_percent ?? 0) >= 0 ? 'positive' : 'negative'}>{item.backtest ? pct(item.backtest.return_percent) : '—'}</span><span>{item.backtest ? Number(item.backtest.profit_factor).toFixed(2) : '—'}</span><span>{item.backtest ? `${item.backtest.max_drawdown_percent}%` : '—'}</span><span>{item.dataset_id?.slice(-10) ?? '未绑定'}</span><details><summary>查看策略参数与拒绝依据</summary><p>{item.decision_reasons.slice(-4).join(' · ')}</p><pre>{JSON.stringify(item.spec.parameters ?? {}, null, 2)}</pre></details></article>)}</div></div>}

    {tab === 'diagnostics' && <div className="diagnostics-workspace"><p>技术信息默认折叠；所有验证证据只用于离线 Research 和审计。</p><details><summary>数据集指纹</summary>{datasets.slice(0, 20).map(item => <article key={item.id}><strong>{item.symbol} · {item.timeframe}</strong><code>{item.data_fingerprint}</code><span>{item.quality_status} · gaps {item.gap_count} · invalid {item.invalid_count}</span></article>)}</details><details><summary>漂移原始结果</summary><pre>{JSON.stringify(drift, null, 2)}</pre></details></div>}

    {message && <div className="ops-toast" role="status">{message}</div>}
  </section>
}
