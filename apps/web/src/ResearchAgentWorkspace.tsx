import { useCallback, useEffect, useMemo, useState } from 'react'
import { Beaker, BrainCircuit, Database, RefreshCw, Search, ShieldCheck } from 'lucide-react'
import {
  createAIResearchCandidate,
  createMLChallenger,
  getAgentState,
  getResearchCampaignJobs,
  getResearchCampaigns,
  getResearchCandidates,
  getResearchDatasets,
  getResearchDrift,
} from './api'
import ResearchCampaignWorkbench from './ResearchCampaignWorkbench'
import type {
  AgentControlState,
  ConfigurationView,
  DriftReport,
  ResearchCampaignJob,
  ResearchCampaignReport,
  ResearchDataset,
  StrategyCandidate,
} from './types'

type ResearchTab = 'overview' | 'candidates' | 'campaigns' | 'evidence' | 'diagnostics'

function pct(value: string | number | undefined) {
  const number = Number(value ?? 0)
  return `${number > 0 ? '+' : ''}${number.toFixed(2)}%`
}

export default function ResearchAgentWorkspace({ settings }: { settings: ConfigurationView }) {
  const [tab, setTab] = useState<ResearchTab>('overview')
  const [candidates, setCandidates] = useState<StrategyCandidate[]>([])
  const [datasets, setDatasets] = useState<ResearchDataset[]>([])
  const [campaigns, setCampaigns] = useState<ResearchCampaignReport[]>([])
  const [jobs, setJobs] = useState<ResearchCampaignJob[]>([])
  const [drift, setDrift] = useState<DriftReport | null>(null)
  const [agentState, setAgentState] = useState<AgentControlState | null>(null)
  const [search, setSearch] = useState('')
  const [busy, setBusy] = useState('')
  const [message, setMessage] = useState('')

  const refresh = useCallback(async () => {
    try {
      const [nextCandidates, nextDatasets, nextCampaigns, nextJobs, nextDrift, nextState] = await Promise.all([
        getResearchCandidates(), getResearchDatasets(), getResearchCampaigns(), getResearchCampaignJobs(), getResearchDrift(), getAgentState(),
      ])
      setCandidates(nextCandidates)
      setDatasets(nextDatasets)
      setCampaigns(nextCampaigns)
      setJobs(nextJobs)
      setDrift(nextDrift)
      setAgentState(nextState)
    } catch (cause) {
      setMessage(cause instanceof Error ? cause.message : '研究状态读取失败')
    }
  }, [])

  useEffect(() => {
    const kickoff = window.setTimeout(() => void refresh(), 0)
    return () => window.clearTimeout(kickoff)
  }, [refresh])

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
  const activeJob = jobs.find(item => item.status === 'queued' || item.status === 'running')

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
      <div><span className="ops-kicker">策略研究 Agent</span><h1>自动研究工作台</h1><p>生成候选、运行跨市场验证并治理 Champion，不直接控制订单。</p></div>
      <div className="header-actions"><button className="button ghost" disabled={Boolean(busy)} onClick={() => void refresh()}><RefreshCw />刷新</button><button className="button ghost" disabled={Boolean(busy)} onClick={() => void generateML()}><Beaker />{busy === 'ml' ? '生成中…' : 'ML Challenger'}</button><button className="button primary" disabled={Boolean(busy)} onClick={() => void generateAI()}><BrainCircuit />{busy === 'ai' ? '生成中…' : '生成 AI 候选'}</button></div>
    </header>

    <div className="research-status-strip">
      <div><span>隔离候选</span><strong>{candidates.length}</strong><small>{statusCounts.champion ?? 0} Champion · {statusCounts.challenger ?? 0} Challenger</small></div>
      <div><span>不可变数据集</span><strong>{datasets.length}</strong><small>{datasets.filter(item => item.quality_status === 'healthy').length} 份健康</small></div>
      <div><span>Campaign</span><strong>{campaigns.length}</strong><small>{activeJob ? `${activeJob.status} · ${activeJob.completed_cells}/${activeJob.total_cells}` : '当前无运行作业'}</small></div>
      <div><span>策略漂移</span><strong className={drift?.status === 'stable' ? 'positive' : 'negative'}>{drift?.status ?? '读取中'}</strong><small>PSI {drift?.population_stability_index ?? '—'}</small></div>
      <div><span>影子验证</span><strong>{agentState?.shadow_calibration.settled_batches ?? 0}</strong><small>{agentState?.shadow_calibration.calibration_ready ? '证据已就绪' : '继续收集样本'}</small></div>
    </div>

    <div className="subnav-tabs research-tabs">{([
      ['overview', '研究概览'], ['candidates', '候选研究'], ['campaigns', 'Campaign'], ['evidence', '验证证据'], ['diagnostics', '技术诊断'],
    ] as Array<[ResearchTab, string]>).map(([key, label]) => <button key={key} className={tab === key ? 'active' : ''} onClick={() => setTab(key)}>{label}</button>)}</div>

    {tab === 'overview' && <div className="research-overview-layout">
      <section><div className="panel-title"><span>研究流水线</span><ShieldCheck /></div><div className="research-pipeline"><article className="done"><b>01</b><div><strong>候选生成</strong><span>公开先验、人工规格、AI/ML</span></div></article><article className={candidates.length ? 'done' : ''}><b>02</b><div><strong>不可变数据绑定</strong><span>禁止使用未冻结样本评估</span></div></article><article className={(statusCounts.challenger ?? 0) > 0 ? 'done' : ''}><b>03</b><div><strong>Walk-forward 与压力测试</strong><span>费用、滑点、近期窗口与参数前推</span></div></article><article className={(statusCounts.champion ?? 0) > 0 ? 'done' : ''}><b>04</b><div><strong>Champion 治理</strong><span>通过后仍不会自动激活交易计划</span></div></article></div></section>
      <section><div className="panel-title"><span>最近产出</span><BrainCircuit /></div><div className="research-recent-list">{candidates.slice(0, 8).map(item => <article key={item.id}><i className={item.status} /><div><strong>{item.spec.family} · {item.source.toUpperCase()}</strong><span>{item.id}</span></div><b>{item.status}</b><small>{item.dataset_id ? `数据集 ${item.dataset_id}` : '等待绑定数据集'}</small></article>)}</div></section>
      <section><div className="panel-title"><span>当前阻塞</span><Database /></div>{!datasets.length && <p className="research-blocker">没有可用研究数据集。</p>}{drift && drift.status !== 'stable' && <p className="research-blocker">漂移状态为 {drift.status}，晋升前必须复核。</p>}{!(statusCounts.champion ?? 0) && <p className="research-blocker">当前没有通过治理的 Champion。</p>}{datasets.length > 0 && drift?.status === 'stable' && (statusCounts.champion ?? 0) > 0 && <p className="research-clear">研究主链路没有显式阻塞。</p>}</section>
    </div>}

    {tab === 'candidates' && <div className="candidate-workspace"><div className="workspace-filterbar"><div><Search /><input value={search} onChange={event => setSearch(event.target.value)} placeholder="搜索候选 ID、来源或策略族" /></div><span>显示 {filteredCandidates.length}/{candidates.length}</span></div><div className="candidate-table"><div className="table-head"><span>候选</span><span>来源</span><span>状态</span><span>收益</span><span>PF</span><span>最大回撤</span><span>数据集</span></div>{filteredCandidates.map(item => <article key={item.id}><div><strong>{String(item.spec.family)}</strong><small>{item.id}</small></div><span>{item.source.toUpperCase()}</span><b className={item.status}>{item.status}</b><span className={Number(item.backtest?.return_percent ?? 0) >= 0 ? 'positive' : 'negative'}>{item.backtest ? pct(item.backtest.return_percent) : '—'}</span><span>{item.backtest ? Number(item.backtest.profit_factor).toFixed(2) : '—'}</span><span>{item.backtest ? `${item.backtest.max_drawdown_percent}%` : '—'}</span><span>{item.dataset_id?.slice(-10) ?? '未绑定'}</span><details><summary>查看策略参数与拒绝依据</summary><p>{item.decision_reasons.slice(-4).join(' · ')}</p><pre>{JSON.stringify(item.spec.parameters ?? {}, null, 2)}</pre></details></article>)}</div></div>}

    {tab === 'campaigns' && <div className="embedded-research-workbench"><ResearchCampaignWorkbench datasets={datasets} candidates={candidates} onGovernanceComplete={() => void refresh()} /></div>}

    {tab === 'evidence' && <div className="evidence-workspace"><section><div className="panel-title"><span>影子前向证据</span><b>{agentState?.shadow_calibration.calibration_ready ? 'READY' : 'COLLECTING'}</b></div><dl><div><dt>冻结候选</dt><dd>{agentState?.shadow_calibration.trial_count ?? 0}</dd></div><div><dt>独立决策批次</dt><dd>{agentState?.shadow_calibration.decision_batches ?? 0}</dd></div><div><dt>已结算批次</dt><dd>{agentState?.shadow_calibration.settled_batches ?? 0}</dd></div><div><dt>累计 R</dt><dd>{agentState?.shadow_calibration.total_r ?? '0'}R</dd></div><div><dt>平均期望</dt><dd>{agentState?.shadow_calibration.expectancy_r ?? '0'}R</dd></div></dl><p>影子验证永不提交订单，仅用于识别正确拦截和误拒绝。</p></section><section><div className="panel-title"><span>最近验证样本</span><b>{agentState?.shadow_trials.length ?? 0}</b></div>{agentState?.shadow_trials.slice(0, 12).map(trial => <article className="evidence-row" key={trial.id}><div><strong>{trial.plan.symbol} · {trial.plan.timeframe}</strong><span>{trial.plan.algorithm} · 排名 #{trial.screening_rank}</span></div><b className={Number(trial.outcome.return_on_risk ?? 0) >= 0 ? 'positive' : 'negative'}>{trial.outcome.return_on_risk ?? 'pending'}R</b><small>{trial.screening_summary}</small></article>)}</section></div>}

    {tab === 'diagnostics' && <div className="diagnostics-workspace"><p>技术信息默认折叠，供故障排查与审计使用。</p><details><summary>数据集指纹</summary>{datasets.slice(0, 20).map(item => <article key={item.id}><strong>{item.symbol} · {item.timeframe}</strong><code>{item.data_fingerprint}</code><span>{item.quality_status} · gaps {item.gap_count} · invalid {item.invalid_count}</span></article>)}</details><details><summary>最近后台作业</summary><pre>{JSON.stringify(jobs.slice(0, 10), null, 2)}</pre></details><details><summary>漂移原始结果</summary><pre>{JSON.stringify(drift, null, 2)}</pre></details></div>}

    {message && <div className="ops-toast" role="status">{message}</div>}
  </section>
}
